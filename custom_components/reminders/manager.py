"""Central reminders runtime manager."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import SAVE_DELAY, SUPPORTED_CHANNELS
from .delivery import DeliveryDispatcher, DeliveryResult
from .models import (
    AcknowledgementPolicy,
    DeliveryPolicy,
    Occurrence,
    OccurrenceStatus,
    QuietHoursPolicy,
    Reminder,
    ReminderStatus,
    UserPreferences,
)
from .recurrence import (
    RecurrenceRule,
    first_due,
    next_due_after,
    occurrence_number,
)
from .storage import ReminderStore, StoredData, deserialize_storage, serialize_storage

_LOGGER = logging.getLogger(__name__)


class ReminderNotFoundError(KeyError):
    """Raised when a reminder does not exist."""


class ReminderValidationError(ValueError):
    """Raised when reminder input is invalid."""


class ReminderManager:
    """Own reminder state, persistence, scheduling, history, and delivery."""

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
        self._listeners: set[Callable[[frozenset[str]], None]] = set()

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
            self._listeners.clear()

    def async_subscribe(
        self, listener: Callable[[frozenset[str]], None]
    ) -> Callable[[], None]:
        """Subscribe to privacy-preserving state invalidations."""
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    async def async_create(
        self,
        *,
        user_id: str,
        title: str,
        due: datetime,
        message: str | None = None,
        delivery_policy: DeliveryPolicy | None = None,
        acknowledgement_policy: AcknowledgementPolicy = AcknowledgementPolicy.DEFAULT,
        quiet_hours_policy: QuietHoursPolicy = QuietHoursPolicy.RESPECT,
    ) -> Reminder:
        """Create and schedule a one-shot reminder."""
        due = _normalize_due(due)
        _validate_policy(delivery_policy)
        _validate_title(title)
        now = dt_util.utcnow()
        occurrence = _new_occurrence(due)
        reminder = Reminder(
            id=str(uuid4()),
            user_id=user_id,
            title=title.strip(),
            message=message.strip() if message else None,
            due=due,
            created_at=now,
            updated_at=now,
            delivery_policy=delivery_policy,
            acknowledgement_policy=acknowledgement_policy,
            quiet_hours_policy=quiet_hours_policy,
            current_occurrence_id=occurrence.id,
            occurrence_history=(occurrence,),
        )
        await self._async_add(reminder)
        if due <= now:
            await self._async_process_due(now)
        self._notify_changed({user_id})
        return await self.async_get(reminder.id)

    async def async_create_recurring(
        self,
        *,
        user_id: str,
        title: str,
        recurrence: RecurrenceRule,
        message: str | None = None,
        delivery_policy: DeliveryPolicy | None = None,
        acknowledgement_policy: AcknowledgementPolicy = AcknowledgementPolicy.DEFAULT,
        quiet_hours_policy: QuietHoursPolicy = QuietHoursPolicy.RESPECT,
    ) -> Reminder:
        """Create and durably persist an anchored recurring reminder."""
        _validate_policy(delivery_policy)
        _validate_title(title)
        now = dt_util.utcnow()
        due = first_due(recurrence, now)
        occurrence = _new_occurrence(due)
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
            acknowledgement_policy=acknowledgement_policy,
            quiet_hours_policy=quiet_hours_policy,
            current_occurrence_id=occurrence.id,
            current_occurrence_number=occurrence_number(recurrence, due),
            occurrence_history=(occurrence,),
        )
        await self._async_add(reminder)
        self._notify_changed({user_id})
        return reminder

    async def _async_add(self, reminder: Reminder) -> None:
        async with self._lock:
            candidate = dict(self._reminders)
            candidate[reminder.id] = reminder
            await self._async_persist_state(candidate, self._users)
            self._reschedule_if_needed(reminder.due)

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
        query: str | None = None,
        recurring: bool | None = None,
        statuses: set[ReminderStatus] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Reminder]:
        """List bounded reminders with backend-supported filters."""
        after = _normalize_due(due_after) if due_after else None
        before = _normalize_due(due_before) if due_before else None
        needle = query.casefold().strip() if query else None
        if limit < 1 or limit > 1000 or offset < 0:
            raise ReminderValidationError("Invalid list limit or offset")
        async with self._lock:
            values = [
                reminder
                for reminder in self._reminders.values()
                if (user_id is None or reminder.user_id == user_id)
                and (not pending_only or reminder.status is ReminderStatus.PENDING)
                and (after is None or reminder.due >= after)
                and (before is None or reminder.due <= before)
                and (
                    recurring is None or (reminder.recurrence is not None) is recurring
                )
                and (statuses is None or reminder.status in statuses)
                and (
                    needle is None
                    or needle in reminder.title.casefold()
                    or needle in (reminder.message or "").casefold()
                )
            ]
        return sorted(values, key=lambda item: (item.due, item.id))[
            offset : offset + limit
        ]

    async def async_history(
        self,
        *,
        user_id: str | None,
        query: str | None = None,
        statuses: set[OccurrenceStatus] | None = None,
        due_after: datetime | None = None,
        due_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return a filtered bounded occurrence-history page."""
        if limit < 1 or limit > 200 or offset < 0:
            raise ReminderValidationError("Invalid history limit or offset")
        after = _normalize_due(due_after) if due_after else None
        before = _normalize_due(due_before) if due_before else None
        needle = query.casefold().strip() if query else None
        rows: list[dict[str, Any]] = []
        async with self._lock:
            for reminder in self._reminders.values():
                if user_id is not None and reminder.user_id != user_id:
                    continue
                if (
                    needle
                    and needle not in reminder.title.casefold()
                    and needle not in (reminder.message or "").casefold()
                ):
                    continue
                for occurrence in reminder.occurrence_history:
                    if occurrence.status in {
                        OccurrenceStatus.SCHEDULED,
                        OccurrenceStatus.DELIVERING,
                    }:
                        continue
                    if statuses is not None and occurrence.status not in statuses:
                        continue
                    if after is not None and occurrence.scheduled_due < after:
                        continue
                    if before is not None and occurrence.scheduled_due > before:
                        continue
                    rows.append(
                        {
                            "reminder_id": reminder.id,
                            "user_id": reminder.user_id,
                            "title": reminder.title,
                            "message": reminder.message,
                            "recurring": reminder.recurrence is not None,
                            "occurrence": occurrence.to_dict(),
                        }
                    )
        rows.sort(key=lambda row: row["occurrence"]["scheduled_due"], reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def async_update(self, reminder_id: str, **changes: Any) -> Reminder:
        """Update mutable fields and reschedule while retaining prior history."""
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
                "acknowledgement_policy",
                "quiet_hours_policy",
            }
            unknown = set(changes) - allowed
            if unknown:
                raise ReminderValidationError(f"Unsupported fields: {sorted(unknown)}")
            if "title" in changes:
                changes["title"] = str(changes["title"]).strip()
                _validate_title(changes["title"])
            _validate_policy(changes.get("delivery_policy"))
            now = dt_util.utcnow()
            history = list(current.occurrence_history)
            active = _find_occurrence(current, current.current_occurrence_id)
            if "recurrence" in changes:
                recurrence = changes["recurrence"]
                if not isinstance(recurrence, RecurrenceRule):
                    raise ReminderValidationError("Recurrence rule is invalid")
                next_due = first_due(recurrence, now)
                if active and active.status is OccurrenceStatus.SCHEDULED:
                    history = _replace_occurrence(
                        history, active.updated(status=OccurrenceStatus.CANCELLED)
                    )
                new_occurrence = _new_occurrence(next_due)
                history.append(new_occurrence)
                changes.update(
                    due=next_due,
                    scheduled_due=next_due,
                    current_occurrence_id=new_occurrence.id,
                    current_occurrence_number=occurrence_number(recurrence, next_due),
                )
            elif "due" in changes:
                if current.recurrence is not None:
                    raise ReminderValidationError(
                        "Recurring due time is derived from its recurrence rule"
                    )
                new_due = _normalize_due(changes["due"])
                changes["due"] = new_due
                if active and active.status is OccurrenceStatus.SCHEDULED:
                    history = _replace_occurrence(
                        history,
                        active.updated(scheduled_due=new_due, due=new_due),
                    )
                else:
                    new_occurrence = _new_occurrence(new_due)
                    history.append(new_occurrence)
                    changes["current_occurrence_id"] = new_occurrence.id
            changes["occurrence_history"] = tuple(history)
            updated = current.updated(
                **changes,
                status=ReminderStatus.PENDING,
                delivered_at=None,
                delivery_errors=(),
                updated_at=now,
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        if updated.due <= dt_util.utcnow():
            await self._async_process_due(dt_util.utcnow())
        self._notify_changed({current.user_id, updated.user_id})
        return await self.async_get(updated.id)

    async def async_delete(self, reminder_id: str) -> None:
        """Delete a reminder or recurring series using legacy delete semantics."""
        async with self._lock:
            reminder = self._require(reminder_id)
            if reminder.status is ReminderStatus.DELIVERING:
                raise ReminderValidationError("Reminder is currently being delivered")
            candidate = dict(self._reminders)
            del candidate[reminder_id]
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        self._notify_changed({reminder.user_id})

    async def async_snooze(
        self,
        reminder_id: str,
        *,
        due: datetime | None = None,
        duration: timedelta | None = None,
    ) -> Reminder:
        """Move only the active occurrence to a new due time."""
        if (due is None) == (duration is None):
            raise ReminderValidationError("Provide exactly one of due or duration")
        new_due = due if due is not None else dt_util.utcnow() + duration  # type: ignore[operator]
        new_due = _normalize_due(new_due)
        async with self._lock:
            current = self._require(reminder_id)
            if current.status is ReminderStatus.DELIVERING:
                raise ReminderValidationError("Reminder is currently being delivered")
            occurrence = _find_occurrence(current, current.current_occurrence_id)
            if occurrence is None:
                occurrence = _new_occurrence(
                    current.due,
                    scheduled_due=current.scheduled_due or current.due,
                )
                history = [*current.occurrence_history, occurrence]
            else:
                history = list(current.occurrence_history)
            if occurrence.status not in {
                OccurrenceStatus.SCHEDULED,
                OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
                OccurrenceStatus.DELIVERED,
                OccurrenceStatus.FAILED,
            }:
                raise ReminderValidationError("Reminder has no occurrence to snooze")
            if occurrence.status is not OccurrenceStatus.SCHEDULED:
                occurrence = _new_occurrence(
                    new_due,
                    scheduled_due=(
                        current.scheduled_due if current.recurrence else new_due
                    ),
                ).updated(snoozed=True, snoozed_at=dt_util.utcnow())
                history.append(occurrence)
            else:
                occurrence = occurrence.updated(
                    due=new_due, snoozed=True, snoozed_at=dt_util.utcnow()
                )
                history = _replace_occurrence(history, occurrence)
            updated = current.updated(
                due=new_due,
                status=ReminderStatus.PENDING,
                current_occurrence_id=occurrence.id,
                occurrence_history=tuple(history),
                delivered_at=None,
                delivery_errors=(),
                updated_at=dt_util.utcnow(),
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        if updated.due <= dt_util.utcnow():
            await self._async_process_due(dt_util.utcnow())
        self._notify_changed({current.user_id})
        return await self.async_get(updated.id)

    async def async_acknowledge(
        self,
        reminder_id: str,
        *,
        occurrence_id: str | None = None,
        acknowledged_by: str | None = None,
    ) -> Occurrence:
        """Acknowledge one delivered occurrence without changing a series schedule."""
        async with self._lock:
            reminder = self._require(reminder_id)
            awaiting = [
                item
                for item in reminder.occurrence_history
                if item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                and (occurrence_id is None or item.id == occurrence_id)
            ]
            if not awaiting:
                raise ReminderValidationError(
                    "No matching occurrence is awaiting acknowledgement"
                )
            if len(awaiting) > 1:
                raise ReminderValidationError(
                    "More than one occurrence is awaiting acknowledgement; "
                    "provide occurrence_id"
                )
            now = dt_util.utcnow()
            acknowledged = awaiting[0].updated(
                status=OccurrenceStatus.ACKNOWLEDGED,
                acknowledged_at=now,
                acknowledged_by=acknowledged_by,
            )
            history = _replace_occurrence(
                list(reminder.occurrence_history), acknowledged
            )
            status = reminder.status
            if reminder.status is ReminderStatus.AWAITING_ACKNOWLEDGEMENT:
                status = ReminderStatus.ACKNOWLEDGED
            updated = reminder.updated(
                occurrence_history=tuple(history), status=status, updated_at=now
            )
            candidate = dict(self._reminders)
            candidate[reminder.id] = updated
            await self._async_persist_state(candidate, self._users)
        self._notify_changed({reminder.user_id})
        return acknowledged

    async def async_set_user_preferences(
        self,
        user_id: str,
        policy: DeliveryPolicy,
        **changes: Any,
    ) -> UserPreferences:
        """Durably set live defaults for one HA user."""
        _validate_policy(policy)
        current = self._users.get(user_id, UserPreferences())
        allowed = {
            "require_acknowledgement",
            "configured",
            "history_retention_days",
            "history_max_occurrences",
            "quiet_hours_enabled",
            "quiet_hours_start",
            "quiet_hours_end",
            "quiet_hours_channels",
            "quiet_hours_fallback_channels",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ReminderValidationError(f"Unsupported preferences: {sorted(unknown)}")
        preferences = current.__class__(
            default_delivery_policy=policy,
            require_acknowledgement=bool(
                changes.get("require_acknowledgement", current.require_acknowledgement)
            ),
            configured=bool(changes.get("configured", True)),
            history_retention_days=int(
                changes.get("history_retention_days", current.history_retention_days)
            ),
            history_max_occurrences=int(
                changes.get("history_max_occurrences", current.history_max_occurrences)
            ),
            quiet_hours_enabled=bool(
                changes.get("quiet_hours_enabled", current.quiet_hours_enabled)
            ),
            quiet_hours_start=_coerce_time(
                changes.get("quiet_hours_start", current.quiet_hours_start)
            ),
            quiet_hours_end=_coerce_time(
                changes.get("quiet_hours_end", current.quiet_hours_end)
            ),
            quiet_hours_channels=tuple(
                changes.get("quiet_hours_channels", current.quiet_hours_channels)
            ),
            quiet_hours_fallback_channels=tuple(
                changes.get(
                    "quiet_hours_fallback_channels",
                    current.quiet_hours_fallback_channels,
                )
            ),
        )
        _validate_preferences(preferences)
        async with self._lock:
            users = dict(self._users)
            users[user_id] = preferences
            reminders = {
                key: _prune_history(value, preferences, dt_util.utcnow())
                if value.user_id == user_id
                else value
                for key, value in self._reminders.items()
            }
            await self._async_persist_state(reminders, users)
        self._notify_changed({user_id})
        return preferences

    async def async_get_user_preferences(self, user_id: str) -> UserPreferences:
        """Return a user's preferences or reliable fallback."""
        async with self._lock:
            return self._users.get(user_id, UserPreferences())

    async def async_test_delivery(
        self, *, user_id: str, policy: DeliveryPolicy
    ) -> DeliveryResult:
        """Exercise real providers without creating a reminder or history record."""
        _validate_policy(policy)
        now = dt_util.utcnow()
        test = Reminder(
            id=f"test-{uuid4()}",
            user_id=user_id,
            title="Test reminder",
            message="This is a test reminder from Home Assistant.",
            due=now,
            created_at=now,
            updated_at=now,
        )
        return await self._dispatcher.async_deliver(test, policy)

    async def _async_process_due(self, effective_now: datetime) -> None:
        """Claim and process every reminder due at the effective current time."""
        effective_now = _normalize_due(effective_now)
        due: list[Reminder] = []
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
                        occurrence = _find_occurrence(
                            reminder, reminder.current_occurrence_id
                        )
                        history = list(reminder.occurrence_history)
                        if occurrence is None:
                            occurrence = _new_occurrence(
                                reminder.due,
                                scheduled_due=reminder.scheduled_due or reminder.due,
                            )
                            history.append(occurrence)
                        occurrence = occurrence.updated(
                            status=OccurrenceStatus.DELIVERING
                        )
                        history = _replace_occurrence(history, occurrence)
                        candidate[reminder.id] = reminder.updated(
                            status=ReminderStatus.DELIVERING,
                            current_occurrence_id=occurrence.id,
                            occurrence_history=tuple(history),
                            updated_at=effective_now,
                        )
                    await self._async_persist_state(candidate, self._users)
            for claimed in due:
                await self._async_deliver_claimed(claimed.id, effective_now)
        finally:
            async with self._lock:
                self._reschedule(force=True)

    async def _async_deliver_claimed(
        self, reminder_id: str, effective_now: datetime
    ) -> None:
        async with self._lock:
            claimed = self._reminders.get(reminder_id)
            if claimed is None or claimed.status is not ReminderStatus.DELIVERING:
                return
            preferences = self._users.get(claimed.user_id, UserPreferences())
            policy = claimed.delivery_policy or preferences.default_delivery_policy
            delivery_policy, suppressed = self._delivery_plan(
                claimed, policy, preferences, effective_now
            )
            ack_required = (
                claimed.acknowledgement_policy is AcknowledgementPolicy.REQUIRED
                or (
                    claimed.acknowledgement_policy is AcknowledgementPolicy.DEFAULT
                    and preferences.require_acknowledgement
                )
            )
        result = await self._dispatcher.async_deliver(claimed, delivery_policy)
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if current is None or current.status is not ReminderStatus.DELIVERING:
                return
            occurrence = _find_occurrence(current, current.current_occurrence_id)
            if occurrence is None:
                raise ReminderValidationError("Active occurrence history is missing")
            if result.succeeded:
                occurrence_status = (
                    OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                    if ack_required
                    else OccurrenceStatus.DELIVERED
                )
            else:
                occurrence_status = OccurrenceStatus.FAILED
            finished = occurrence.updated(
                status=occurrence_status,
                delivered_at=effective_now if result.succeeded else None,
                succeeded_channels=result.succeeded,
                failed_channels=result.failed_channels,
                delivery_errors=result.errors,
                suppressed_channels=suppressed,
                acknowledgement_required=ack_required,
            )
            history = _replace_occurrence(list(current.occurrence_history), finished)
            occurrence_reminder_status = _reminder_status(occurrence_status)
            if current.recurrence is not None:
                scheduled_due = current.scheduled_due or occurrence.scheduled_due
                next_due = next_due_after(
                    current.recurrence, max(effective_now, scheduled_due)
                )
                if next_due is not None:
                    next_item = _new_occurrence(next_due)
                    history.append(next_item)
                    updated = current.updated(
                        status=ReminderStatus.PENDING,
                        due=next_due,
                        scheduled_due=next_due,
                        current_occurrence_id=next_item.id,
                        current_occurrence_number=occurrence_number(
                            current.recurrence, next_due
                        ),
                        last_occurrence_due=scheduled_due,
                        last_occurrence_status=occurrence_reminder_status,
                        delivered_at=(
                            effective_now if result.succeeded else current.delivered_at
                        ),
                        delivery_errors=result.errors,
                        occurrence_history=tuple(history),
                        updated_at=effective_now,
                    )
                else:
                    updated = current.updated(
                        status=occurrence_reminder_status,
                        last_occurrence_due=scheduled_due,
                        last_occurrence_status=occurrence_reminder_status,
                        delivered_at=(
                            effective_now if result.succeeded else current.delivered_at
                        ),
                        delivery_errors=result.errors,
                        occurrence_history=tuple(history),
                        updated_at=effective_now,
                    )
            else:
                updated = current.updated(
                    status=occurrence_reminder_status,
                    delivered_at=effective_now if result.succeeded else None,
                    delivery_errors=result.errors,
                    occurrence_history=tuple(history),
                    updated_at=effective_now,
                )
            updated = _prune_history(updated, preferences, effective_now)
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
        self._notify_changed({claimed.user_id})

    def _delivery_plan(
        self,
        reminder: Reminder,
        policy: DeliveryPolicy,
        preferences: UserPreferences,
        now: datetime,
    ) -> tuple[DeliveryPolicy, tuple[str, ...]]:
        if (
            reminder.quiet_hours_policy is QuietHoursPolicy.IGNORE
            or not preferences.quiet_hours_enabled
            or not _is_quiet_now(self._hass, now, preferences)
        ):
            return policy, ()
        suppressed = tuple(
            channel
            for channel in policy.channels
            if channel in preferences.quiet_hours_channels
        )
        channels = [channel for channel in policy.channels if channel not in suppressed]
        if suppressed:
            for fallback in preferences.quiet_hours_fallback_channels:
                if fallback not in channels:
                    channels.append(fallback)
        return (
            DeliveryPolicy(
                tuple(channels), policy.notify_targets, policy.voice_targets
            ),
            suppressed,
        )

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
        """Retained for backwards compatibility with older test doubles."""
        self._store.async_delay_save(self._snapshot, SAVE_DELAY)

    async def _async_persist_state(
        self,
        reminders: dict[str, Reminder],
        users: dict[str, UserPreferences],
    ) -> None:
        """Persist proposed state, then commit it to runtime memory."""
        reminder_copy = dict(reminders)
        user_copy = dict(users)
        await self._store.async_save(serialize_storage(reminder_copy, user_copy))
        self._reminders = reminder_copy
        self._users = user_copy

    def _snapshot(self) -> StoredData:
        return serialize_storage(dict(self._reminders), dict(self._users))

    def _notify_changed(self, user_ids: set[str]) -> None:
        """Notify API subscribers without exposing reminder data."""
        owners = frozenset(user_ids)
        for listener in tuple(self._listeners):
            try:
                listener(owners)
            except Exception:
                _LOGGER.exception("Error notifying a reminder state subscriber")

    def _require(self, reminder_id: str) -> Reminder:
        try:
            return self._reminders[reminder_id]
        except KeyError as err:
            raise ReminderNotFoundError(reminder_id) from err


def quiet_hours_active(value: time, start: time, end: time) -> bool:
    """Return whether local wall time falls in a possibly overnight window."""
    if start == end:
        return True
    if start < end:
        return start <= value < end
    return value >= start or value < end


def _is_quiet_now(
    hass: HomeAssistant, now: datetime, preferences: UserPreferences
) -> bool:
    timezone_name = getattr(getattr(hass, "config", None), "time_zone", "UTC")
    local_time = now.astimezone(ZoneInfo(timezone_name)).time().replace(tzinfo=None)
    return quiet_hours_active(
        local_time, preferences.quiet_hours_start, preferences.quiet_hours_end
    )


def _normalize_due(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ReminderValidationError("Due time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReminderValidationError("Due time must be timezone-aware")
    return value.astimezone(UTC)


def _validate_title(title: str) -> None:
    if not title.strip():
        raise ReminderValidationError("Title must not be empty")


def _validate_policy(policy: DeliveryPolicy | None) -> None:
    if policy is None:
        return
    if not policy.channels:
        raise ReminderValidationError("Delivery policy needs at least one channel")
    unknown = set(policy.channels) - SUPPORTED_CHANNELS
    if unknown:
        raise ReminderValidationError(f"Unknown delivery channels: {sorted(unknown)}")
    if any(not target.startswith("notify.") for target in policy.notify_targets):
        raise ReminderValidationError("Phone targets must be notify entities")
    if any(
        not target.startswith("assist_satellite.") for target in policy.voice_targets
    ):
        raise ReminderValidationError("Voice targets must be Assist satellites")
    if "phone" in policy.channels and not policy.notify_targets:
        raise ReminderValidationError("Phone delivery needs at least one notify target")
    if "voice" in policy.channels and not policy.voice_targets:
        raise ReminderValidationError("Voice delivery needs at least one satellite")


def _validate_preferences(preferences: UserPreferences) -> None:
    if not 1 <= preferences.history_retention_days <= 3650:
        raise ReminderValidationError(
            "History retention must be between 1 and 3650 days"
        )
    if not 10 <= preferences.history_max_occurrences <= 5000:
        raise ReminderValidationError("History limit must be between 10 and 5000")
    unknown = (
        set(preferences.quiet_hours_channels)
        | set(preferences.quiet_hours_fallback_channels)
    ) - SUPPORTED_CHANNELS
    if unknown:
        raise ReminderValidationError(
            f"Unknown quiet-hours channels: {sorted(unknown)}"
        )


def _coerce_time(value: time | str) -> time:
    parsed = value if isinstance(value, time) else time.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        raise ReminderValidationError(
            "Quiet-hours times must be local wall-clock times"
        )
    return parsed.replace(second=0, microsecond=0)


def _new_occurrence(
    due: datetime, *, scheduled_due: datetime | None = None
) -> Occurrence:
    return Occurrence(str(uuid4()), scheduled_due or due, due)


def _find_occurrence(
    reminder: Reminder, occurrence_id: str | None
) -> Occurrence | None:
    if occurrence_id is None:
        return None
    return next(
        (item for item in reminder.occurrence_history if item.id == occurrence_id),
        None,
    )


def _replace_occurrence(
    values: list[Occurrence], replacement: Occurrence
) -> list[Occurrence]:
    return [replacement if item.id == replacement.id else item for item in values]


def _reminder_status(status: OccurrenceStatus) -> ReminderStatus:
    return {
        OccurrenceStatus.DELIVERED: ReminderStatus.DELIVERED,
        OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT: (
            ReminderStatus.AWAITING_ACKNOWLEDGEMENT
        ),
        OccurrenceStatus.ACKNOWLEDGED: ReminderStatus.ACKNOWLEDGED,
        OccurrenceStatus.FAILED: ReminderStatus.FAILED,
        OccurrenceStatus.CANCELLED: ReminderStatus.CANCELLED,
        OccurrenceStatus.SCHEDULED: ReminderStatus.PENDING,
        OccurrenceStatus.DELIVERING: ReminderStatus.DELIVERING,
    }[status]


def _prune_history(
    reminder: Reminder, preferences: UserPreferences, now: datetime
) -> Reminder:
    cutoff = now - timedelta(days=preferences.history_retention_days)
    protected = {
        item.id
        for item in reminder.occurrence_history
        if item.id == reminder.current_occurrence_id
        or item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
    }
    retained = [
        item
        for item in reminder.occurrence_history
        if item.id in protected or item.scheduled_due >= cutoff
    ]
    if len(retained) > preferences.history_max_occurrences:
        removable = [item for item in retained if item.id not in protected]
        keep_removable = max(0, preferences.history_max_occurrences - len(protected))
        keep_ids = (
            {item.id for item in removable[-keep_removable:]}
            if keep_removable
            else set()
        )
        retained = [
            item for item in retained if item.id in protected or item.id in keep_ids
        ]
    return reminder.updated(occurrence_history=tuple(retained))
