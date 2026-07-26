"""Central reminders runtime manager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import SAVE_DELAY, SUPPORTED_CHANNELS
from .delivery import DeliveryDispatcher
from .models import DeliveryPolicy, Reminder, ReminderStatus, UserPreferences
from .recurrence import RecurrenceRule, first_due, next_occurrence_after
from .storage import ReminderStore, StoredData, deserialize_storage, serialize_storage


class ReminderNotFoundError(KeyError):
    """Raised when a reminder does not exist."""


class ReminderValidationError(ValueError):
    """Raised when reminder input is invalid."""


class ReminderManager:
    """Own reminder state, persistence, scheduling, and delivery."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: ReminderStore,
        dispatcher: DeliveryDispatcher,
    ) -> None:
        self._hass = hass
        self._store = store
        self._dispatcher = dispatcher
        self._reminders: dict[str, Reminder] = {}
        self._users: dict[str, UserPreferences] = {}
        self._lock = asyncio.Lock()
        self._unsub_timer: Callable[[], None] | None = None
        self._scheduled_for: datetime | None = None
        self._loaded = False
        self._unloaded = False

    @property
    def scheduled_for(self) -> datetime | None:
        """Return the timestamp of the sole scheduled callback."""
        return self._scheduled_for

    async def async_load(self) -> None:
        """Load once, recover interrupted/overdue work, and schedule next due."""
        async with self._lock:
            if self._loaded:
                return
            self._reminders, self._users = deserialize_storage(
                await self._store.async_load()
            )
            self._loaded = True
        await self._async_process_due(dt_util.utcnow())

    async def async_unload(self) -> None:
        """Cancel callbacks and stop this manager."""
        async with self._lock:
            self._unloaded = True
            self._cancel_timer()

    async def async_create(
        self,
        *,
        user_id: str,
        title: str,
        due: datetime,
        message: str | None = None,
        delivery_policy: DeliveryPolicy | None = None,
    ) -> Reminder:
        """Create and schedule a reminder."""
        due = _normalize_due(due)
        _validate_policy(delivery_policy)
        if not title.strip():
            raise ReminderValidationError("Title must not be empty")
        now = dt_util.utcnow()
        reminder = Reminder(
            id=str(uuid4()),
            user_id=user_id,
            title=title.strip(),
            message=message.strip() if message else None,
            due=due,
            created_at=now,
            updated_at=now,
            delivery_policy=delivery_policy,
        )
        async with self._lock:
            candidate = dict(self._reminders)
            candidate[reminder.id] = reminder
            await self._async_persist_candidate(candidate)
            self._reschedule_if_needed(reminder.due)
        if due <= now:
            await self._async_process_due(now)
        return reminder

    async def async_create_recurring(
        self,
        *,
        user_id: str,
        title: str,
        recurrence: RecurrenceRule,
        message: str | None = None,
        delivery_policy: DeliveryPolicy | None = None,
    ) -> Reminder:
        """Create and durably persist an anchored recurring reminder."""
        _validate_policy(delivery_policy)
        if not title.strip():
            raise ReminderValidationError("Title must not be empty")
        now = dt_util.utcnow()
        due = first_due(recurrence, now)
        reminder = Reminder(
            id=str(uuid4()),
            user_id=user_id,
            title=title.strip(),
            message=message.strip() if message else None,
            due=due,
            scheduled_due=due,
            created_at=now,
            updated_at=now,
            delivery_policy=delivery_policy,
            recurrence=recurrence,
        )
        async with self._lock:
            candidate = dict(self._reminders)
            candidate[reminder.id] = reminder
            await self._async_persist_candidate(candidate)
            self._reschedule_if_needed(reminder.due)
        return reminder

    async def async_get(self, reminder_id: str) -> Reminder:
        """Get one reminder."""
        async with self._lock:
            return self._require(reminder_id)

    async def async_list(
        self,
        *,
        user_id: str | None = None,
        pending_only: bool = False,
        due_after: datetime | None = None,
        due_before: datetime | None = None,
    ) -> list[Reminder]:
        """List reminders with filters."""
        after = _normalize_due(due_after) if due_after else None
        before = _normalize_due(due_before) if due_before else None
        async with self._lock:
            values = [
                reminder
                for reminder in self._reminders.values()
                if (user_id is None or reminder.user_id == user_id)
                and (not pending_only or reminder.status is ReminderStatus.PENDING)
                and (after is None or reminder.due >= after)
                and (before is None or reminder.due <= before)
            ]
        return sorted(values, key=lambda reminder: (reminder.due, reminder.id))

    async def async_update(self, reminder_id: str, **changes: Any) -> Reminder:
        """Update mutable fields and reschedule."""
        async with self._lock:
            current = self._require(reminder_id)
            if current.status is ReminderStatus.DELIVERING:
                raise ReminderValidationError("Reminder is currently being delivered")
            allowed = {
                "title",
                "message",
                "due",
                "delivery_policy",
                "user_id",
                "recurrence",
            }
            unknown = set(changes) - allowed
            if unknown:
                raise ReminderValidationError(f"Unsupported fields: {sorted(unknown)}")
            if "due" in changes:
                if current.recurrence is not None:
                    raise ReminderValidationError(
                        "Recurring due time is derived from its recurrence rule"
                    )
                changes["due"] = _normalize_due(changes["due"])
            if "title" in changes:
                changes["title"] = str(changes["title"]).strip()
                if not changes["title"]:
                    raise ReminderValidationError("Title must not be empty")
            _validate_policy(changes.get("delivery_policy"))
            if "recurrence" in changes:
                recurrence = changes["recurrence"]
                if not isinstance(recurrence, RecurrenceRule):
                    raise ReminderValidationError("Recurrence rule is invalid")
                next_due = first_due(recurrence, dt_util.utcnow())
                changes["due"] = next_due
                changes["scheduled_due"] = next_due
            updated = current.updated(
                **changes,
                status=ReminderStatus.PENDING,
                delivered_at=None,
                delivery_errors=(),
                updated_at=dt_util.utcnow(),
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_candidate(candidate)
            self._reschedule(force=True)
        if updated.due <= dt_util.utcnow():
            await self._async_process_due(dt_util.utcnow())
        return updated

    async def async_delete(self, reminder_id: str) -> None:
        """Delete a reminder."""
        async with self._lock:
            reminder = self._require(reminder_id)
            if reminder.status is ReminderStatus.DELIVERING:
                raise ReminderValidationError("Reminder is currently being delivered")
            candidate = dict(self._reminders)
            del candidate[reminder_id]
            await self._async_persist_candidate(candidate)
            self._reschedule(force=True)

    async def async_snooze(
        self,
        reminder_id: str,
        *,
        due: datetime | None = None,
        duration: timedelta | None = None,
    ) -> Reminder:
        """Move a reminder to a new due time."""
        if (due is None) == (duration is None):
            raise ReminderValidationError("Provide exactly one of due or duration")
        new_due = due if due is not None else dt_util.utcnow() + duration  # type: ignore[operator]
        new_due = _normalize_due(new_due)
        async with self._lock:
            current = self._require(reminder_id)
            if current.status is ReminderStatus.DELIVERING:
                raise ReminderValidationError("Reminder is currently being delivered")
            updated = current.updated(
                due=new_due,
                status=ReminderStatus.PENDING,
                delivered_at=None,
                delivery_errors=(),
                updated_at=dt_util.utcnow(),
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_candidate(candidate)
            self._reschedule(force=True)
        if updated.due <= dt_util.utcnow():
            await self._async_process_due(dt_util.utcnow())
        return updated

    async def async_set_user_preferences(
        self, user_id: str, policy: DeliveryPolicy
    ) -> UserPreferences:
        """Set live defaults for one HA user."""
        _validate_policy(policy)
        preferences = UserPreferences(policy)
        async with self._lock:
            self._users[user_id] = preferences
            self._queue_save()
        return preferences

    async def async_get_user_preferences(self, user_id: str) -> UserPreferences:
        """Return a user's preferences or reliable fallback."""
        async with self._lock:
            return self._users.get(user_id, UserPreferences())

    async def _async_process_due(self, effective_now: datetime) -> None:
        """Claim and process every reminder due at the effective current time."""
        effective_now = _normalize_due(effective_now)
        try:
            async with self._lock:
                if self._unloaded:
                    return
                self._cancel_timer()
                due = [
                    reminder
                    for reminder in self._reminders.values()
                    if reminder.status is ReminderStatus.PENDING
                    and reminder.due <= effective_now
                ]
                if due:
                    candidate = dict(self._reminders)
                    for reminder in due:
                        candidate[reminder.id] = reminder.updated(
                            status=ReminderStatus.DELIVERING,
                            updated_at=effective_now,
                        )
                    await self._async_persist_candidate(candidate)
            for claimed in due:
                policy = (
                    claimed.delivery_policy
                    or self._users.get(
                        claimed.user_id, UserPreferences()
                    ).default_delivery_policy
                )
                result = await self._dispatcher.async_deliver(claimed, policy)
                async with self._lock:
                    current = self._reminders.get(claimed.id)
                    if (
                        current is None
                        or current.status is not ReminderStatus.DELIVERING
                    ):
                        continue
                    occurrence_status = (
                        ReminderStatus.DELIVERED
                        if result.succeeded
                        else ReminderStatus.FAILED
                    )
                    if current.recurrence is not None:
                        scheduled_due = current.scheduled_due or claimed.due
                        next_due = next_occurrence_after(
                            current.recurrence, max(effective_now, scheduled_due)
                        )
                        updated = current.updated(
                            status=ReminderStatus.PENDING,
                            due=next_due,
                            scheduled_due=next_due,
                            last_occurrence_due=scheduled_due,
                            last_occurrence_status=occurrence_status,
                            delivered_at=(
                                effective_now
                                if result.succeeded
                                else current.delivered_at
                            ),
                            delivery_errors=result.errors,
                            updated_at=effective_now,
                        )
                    else:
                        updated = current.updated(
                            status=occurrence_status,
                            delivered_at=(effective_now if result.succeeded else None),
                            delivery_errors=result.errors,
                            updated_at=effective_now,
                        )
                    candidate = dict(self._reminders)
                    candidate[claimed.id] = updated
                    await self._async_persist_candidate(candidate)
        finally:
            async with self._lock:
                self._reschedule(force=True)

    def _reschedule_if_needed(self, candidate: datetime) -> None:
        if self._scheduled_for is None or candidate < self._scheduled_for:
            self._reschedule(force=True)

    def _reschedule(self, *, force: bool) -> None:
        next_due = min(
            (
                reminder.due
                for reminder in self._reminders.values()
                if reminder.status is ReminderStatus.PENDING
            ),
            default=None,
        )
        if not force and next_due == self._scheduled_for:
            return
        if next_due == self._scheduled_for and self._unsub_timer is not None:
            return
        self._cancel_timer()
        if next_due is None or self._unloaded:
            return
        self._scheduled_for = next_due
        self._unsub_timer = async_track_point_in_utc_time(
            self._hass, self._async_timer_fired, next_due
        )

    async def _async_timer_fired(self, now: datetime) -> None:
        self._unsub_timer = None
        self._scheduled_for = None
        await self._async_process_due(max(now, dt_util.utcnow()))

    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        self._scheduled_for = None

    def _queue_save(self) -> None:
        self._store.async_delay_save(self._snapshot, SAVE_DELAY)

    async def _async_persist_candidate(self, candidate: dict[str, Reminder]) -> None:
        """Persist proposed reminder state, then commit it to runtime memory."""
        await self._store.async_save(
            serialize_storage(dict(candidate), dict(self._users))
        )
        self._reminders = candidate

    def _snapshot(self) -> StoredData:
        return serialize_storage(dict(self._reminders), dict(self._users))

    def _require(self, reminder_id: str) -> Reminder:
        try:
            return self._reminders[reminder_id]
        except KeyError as err:
            raise ReminderNotFoundError(reminder_id) from err


def _normalize_due(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ReminderValidationError("Due time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReminderValidationError("Due time must be timezone-aware")
    return value.astimezone(UTC)


def _validate_policy(policy: DeliveryPolicy | None) -> None:
    if policy is None:
        return
    if not policy.channels:
        raise ReminderValidationError("Delivery policy needs at least one channel")
    unknown = set(policy.channels) - SUPPORTED_CHANNELS
    if unknown:
        raise ReminderValidationError(f"Unknown delivery channels: {sorted(unknown)}")
