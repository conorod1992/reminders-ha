"""Central reminders runtime manager."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import (
    LIFECYCLE_EVENT,
    MOBILE_ACTION_EVENT,
    MOBILE_ACTION_PREFIX,
    SAVE_DELAY,
    SUPPORTED_CHANNELS,
)
from .delivery import DeliveryDispatcher, DeliveryResult
from .models import (
    AcknowledgementPolicy,
    ActivationType,
    DeliveryPolicy,
    EscalationAttempt,
    EscalationPolicy,
    MissedOccurrencePolicy,
    Occurrence,
    OccurrenceStatus,
    QuietHoursPolicy,
    Reminder,
    ReminderStatus,
    TriggerDurationWait,
    TriggerRepeatPolicy,
    UserPreferences,
    WhileAwaitingAcknowledgement,
)
from .persistent_cleanup import (
    async_finalize_persistent_delivery_cleanup,
    async_prepare_persistent_cleanup,
)
from .recurrence import (
    RecurrenceRule,
    first_due,
    next_due_after,
    occurrence_number,
)
from .storage import ReminderStore, StoredData, deserialize_storage, serialize_storage
from .triggers.models import TriggerDefinition, TriggerType, trigger_summary
from .triggers.registry import TriggerRegistry

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
        self._trigger_registry = TriggerRegistry(hass, self._async_trigger_callback)
        self._trigger_duration_timers: dict[tuple[str, str], Callable[[], None]] = {}
        self._trigger_duration_timer_tokens: dict[tuple[str, str], object] = {}
        self._unsub_mobile_actions: Callable[[], None] | None = None

    @property
    def scheduled_for(self) -> datetime | None:
        """Return the timestamp of the sole scheduled callback."""
        return self._scheduled_for

    @property
    def trigger_listener_count(self) -> int:
        """Return the number of deduplicated runtime trigger listeners."""
        return self._trigger_registry.listener_count

    async def async_load(self) -> None:
        """Load once, recover interrupted/overdue work, and schedule next due."""
        async with self._lock:
            if self._loaded:
                return
            self._reminders, self._users = deserialize_storage(
                await self._store.async_load()
            )
            self._loaded = True
            bus = getattr(self._hass, "bus", None)
            listen = getattr(bus, "async_listen", None)
            if callable(listen):
                self._unsub_mobile_actions = listen(
                    MOBILE_ACTION_EVENT, self._mobile_action_received
                )
        await self._trigger_registry.async_sync(self._reminders.values())
        await self._async_restore_trigger_durations()
        await self._async_evaluate_immediate()
        await self._async_process_due(dt_util.utcnow(), recover_missed=True)

    async def async_unload(self) -> None:
        """Cancel callbacks and stop this manager."""
        async with self._lock:
            self._unloaded = True
            self._cancel_timer()
            for cancel in self._trigger_duration_timers.values():
                cancel()
            self._trigger_duration_timers.clear()
            self._trigger_duration_timer_tokens.clear()
            self._listeners.clear()
            if self._unsub_mobile_actions is not None:
                self._unsub_mobile_actions()
                self._unsub_mobile_actions = None
        await self._trigger_registry.async_unload()

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
        deliver_when: TriggerDefinition | dict[str, Any] | None = None,
        complete_when: TriggerDefinition | dict[str, Any] | None = None,
        escalation: EscalationPolicy | dict[str, Any] | None = None,
        source: str | None = None,
        source_id: str | None = None,
        source_event: str | None = None,
        managed_externally: bool = False,
        allow_manual_completion: bool = False,
        external_actions: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
        expires_after_seconds: int | None = None,
    ) -> Reminder:
        """Create and schedule a one-shot reminder."""
        due = _normalize_due(due)
        _validate_policy(delivery_policy)
        _validate_title(title)
        delivery_trigger = _coerce_trigger(deliver_when)
        completion_trigger = _coerce_trigger(complete_when)
        escalation_policy = _coerce_escalation(escalation)
        source, source_id, source_event = _validate_source_metadata(
            source, source_id, source_event
        )
        source_actions = _validate_external_actions(
            external_actions, managed_externally
        )
        expiry_window = _validate_expiry_window(expires_after_seconds)
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
            deliver_when=delivery_trigger,
            deliver_when_summary=(
                self._summary(delivery_trigger) if delivery_trigger else None
            ),
            complete_when=completion_trigger,
            complete_when_summary=(
                self._summary(completion_trigger) if completion_trigger else None
            ),
            escalation=escalation_policy,
            source=source,
            source_id=source_id,
            source_event=source_event,
            managed_externally=managed_externally,
            allow_manual_completion=allow_manual_completion,
            external_actions=source_actions,
            expires_after_seconds=expiry_window,
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
        deliver_when: TriggerDefinition | dict[str, Any] | None = None,
        complete_when: TriggerDefinition | dict[str, Any] | None = None,
        escalation: EscalationPolicy | dict[str, Any] | None = None,
        source: str | None = None,
        source_id: str | None = None,
        source_event: str | None = None,
        managed_externally: bool = False,
        allow_manual_completion: bool = False,
        external_actions: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
        missed_occurrence_policy: MissedOccurrencePolicy = (
            MissedOccurrencePolicy.REMIND_ON_STARTUP
        ),
        expires_after_seconds: int | None = None,
    ) -> Reminder:
        """Create and durably persist an anchored recurring reminder."""
        _validate_policy(delivery_policy)
        _validate_title(title)
        delivery_trigger = _coerce_trigger(deliver_when)
        completion_trigger = _coerce_trigger(complete_when)
        escalation_policy = _coerce_escalation(escalation)
        source, source_id, source_event = _validate_source_metadata(
            source, source_id, source_event
        )
        source_actions = _validate_external_actions(
            external_actions, managed_externally
        )
        expiry_window = _validate_expiry_window(expires_after_seconds)
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
            deliver_when=delivery_trigger,
            deliver_when_summary=(
                self._summary(delivery_trigger) if delivery_trigger else None
            ),
            complete_when=completion_trigger,
            complete_when_summary=(
                self._summary(completion_trigger) if completion_trigger else None
            ),
            escalation=escalation_policy,
            source=source,
            source_id=source_id,
            source_event=source_event,
            managed_externally=managed_externally,
            allow_manual_completion=allow_manual_completion,
            external_actions=source_actions,
            missed_occurrence_policy=MissedOccurrencePolicy(missed_occurrence_policy),
            expires_after_seconds=expiry_window,
        )
        await self._async_add(reminder)
        self._notify_changed({user_id})
        return reminder

    async def async_create_triggered(
        self,
        *,
        user_id: str,
        title: str,
        trigger: TriggerDefinition | dict[str, Any],
        message: str | None = None,
        delivery_policy: DeliveryPolicy | None = None,
        acknowledgement_policy: AcknowledgementPolicy = AcknowledgementPolicy.DEFAULT,
        quiet_hours_policy: QuietHoursPolicy = QuietHoursPolicy.RESPECT,
        repeat_policy: TriggerRepeatPolicy = TriggerRepeatPolicy.ONCE,
        fire_if_already_matching: bool = False,
        while_awaiting_acknowledgement: WhileAwaitingAcknowledgement = (
            WhileAwaitingAcknowledgement.SKIP
        ),
        cooldown_seconds: int = 0,
        available_from: datetime | None = None,
        expires_at: datetime | None = None,
        trigger_description: str | None = None,
        complete_when: TriggerDefinition | dict[str, Any] | None = None,
        escalation: EscalationPolicy | dict[str, Any] | None = None,
        source: str | None = None,
        source_id: str | None = None,
        source_event: str | None = None,
        managed_externally: bool = False,
        allow_manual_completion: bool = False,
        external_actions: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
    ) -> Reminder:
        """Create and durably arm a listener-backed triggered reminder."""
        _validate_policy(delivery_policy)
        _validate_title(title)
        definition = (
            trigger
            if isinstance(trigger, TriggerDefinition)
            else TriggerDefinition.from_dict(trigger)
        )
        available = _normalize_optional_time(available_from)
        expiry = _normalize_optional_time(expires_at)
        _validate_trigger_options(cooldown_seconds, available, expiry)
        completion_trigger = _coerce_trigger(complete_when)
        escalation_policy = _coerce_escalation(escalation)
        source, source_id, source_event = _validate_source_metadata(
            source, source_id, source_event
        )
        source_actions = _validate_external_actions(
            external_actions, managed_externally
        )
        now = dt_util.utcnow()
        status = _trigger_waiting_status(now, available, expiry)
        reminder = Reminder(
            id=str(uuid4()),
            user_id=user_id,
            title=title.strip(),
            message=message.strip() if message else None,
            due=None,
            created_at=now,
            updated_at=now,
            status=status,
            delivery_policy=delivery_policy,
            acknowledgement_policy=acknowledgement_policy,
            quiet_hours_policy=quiet_hours_policy,
            activation_type=ActivationType.TRIGGER,
            trigger=definition,
            trigger_summary=self._summary(definition),
            trigger_description=(
                trigger_description.strip() if trigger_description else None
            ),
            repeat_policy=repeat_policy,
            fire_if_already_matching=fire_if_already_matching,
            while_awaiting_acknowledgement=while_awaiting_acknowledgement,
            cooldown_seconds=cooldown_seconds,
            available_from=available,
            expires_at=expiry,
            complete_when=completion_trigger,
            complete_when_summary=(
                self._summary(completion_trigger) if completion_trigger else None
            ),
            escalation=escalation_policy,
            source=source,
            source_id=source_id,
            source_event=source_event,
            managed_externally=managed_externally,
            allow_manual_completion=allow_manual_completion,
            external_actions=source_actions,
        )
        await self._async_add(reminder)
        await self._async_evaluate_immediate({reminder.id})
        self._notify_changed({user_id})
        return await self.async_get(reminder.id)

    async def _async_add(self, reminder: Reminder) -> None:
        async with self._lock:
            candidate = dict(self._reminders)
            candidate[reminder.id] = reminder
            await self._async_persist_state(candidate, self._users)
            if reminder.due is not None:
                self._reschedule_if_needed(reminder.due)
            else:
                self._reschedule(force=True)
        await self._trigger_registry.async_sync(self._reminders.values())

    async def async_get(self, reminder_id: str) -> Reminder:
        """Get one reminder."""
        async with self._lock:
            return self._require(reminder_id)

    async def async_list_page(
        self,
        *,
        user_id: str | None = None,
        pending_only: bool = False,
        due_after: datetime | None = None,
        due_before: datetime | None = None,
        query: str | None = None,
        recurring: bool | None = None,
        activation_type: ActivationType | None = None,
        statuses: set[ReminderStatus] | None = None,
        source: str | None = None,
        source_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[list[Reminder], int]:
        """Return one filtered reminder page and its pre-pagination total."""
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
                and (
                    after is None
                    or (reminder.due is not None and reminder.due >= after)
                )
                and (
                    before is None
                    or (reminder.due is not None and reminder.due <= before)
                )
                and (
                    recurring is None or (reminder.recurrence is not None) is recurring
                )
                and (statuses is None or reminder.status in statuses)
                and (source is None or reminder.source == source)
                and (source_id is None or reminder.source_id == source_id)
                and (
                    activation_type is None
                    or reminder.activation_type is activation_type
                )
                and (
                    needle is None
                    or needle in reminder.title.casefold()
                    or needle in (reminder.message or "").casefold()
                )
            ]
            ordered = sorted(
                values,
                key=lambda item: (
                    item.due is None,
                    item.due or item.created_at,
                    item.id,
                ),
            )
        return ordered[offset : offset + limit], len(ordered)

    async def async_list(
        self,
        *,
        user_id: str | None = None,
        pending_only: bool = False,
        due_after: datetime | None = None,
        due_before: datetime | None = None,
        query: str | None = None,
        recurring: bool | None = None,
        activation_type: ActivationType | None = None,
        statuses: set[ReminderStatus] | None = None,
        source: str | None = None,
        source_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Reminder]:
        """List bounded reminders with backend-supported filters."""
        page, _ = await self.async_list_page(
            user_id=user_id,
            pending_only=pending_only,
            due_after=due_after,
            due_before=due_before,
            query=query,
            recurring=recurring,
            activation_type=activation_type,
            statuses=statuses,
            source=source,
            source_id=source_id,
            limit=limit,
            offset=offset,
        )
        return page

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
                            "activation_type": reminder.activation_type.value,
                            "trigger_summary": reminder.trigger_summary,
                            "occurrence": occurrence.to_dict(),
                        }
                    )
        rows.sort(key=lambda row: row["occurrence"]["scheduled_due"], reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def async_update(self, reminder_id: str, **changes: Any) -> Reminder:
        """Update mutable fields and reschedule while retaining prior history."""
        expiry_window_changed = "expires_after_seconds" in changes
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
                "activation_type",
                "trigger",
                "trigger_description",
                "repeat_policy",
                "fire_if_already_matching",
                "while_awaiting_acknowledgement",
                "cooldown_seconds",
                "available_from",
                "expires_at",
                "deliver_when",
                "complete_when",
                "escalation",
                "source",
                "source_id",
                "source_event",
                "managed_externally",
                "allow_manual_completion",
                "external_actions",
                "missed_occurrence_policy",
                "expires_after_seconds",
            }
            unknown = set(changes) - allowed
            if unknown:
                raise ReminderValidationError(f"Unsupported fields: {sorted(unknown)}")
            if "title" in changes:
                changes["title"] = str(changes["title"]).strip()
                _validate_title(changes["title"])
            if {"source", "source_id", "source_event"}.intersection(changes):
                changes["source"], changes["source_id"], changes["source_event"] = (
                    _validate_source_metadata(
                        changes.get("source", current.source),
                        changes.get("source_id", current.source_id),
                        changes.get("source_event", current.source_event),
                    )
                )
            if "managed_externally" in changes or "external_actions" in changes:
                changes["external_actions"] = _validate_external_actions(
                    changes.get("external_actions", current.external_actions),
                    bool(changes.get("managed_externally", current.managed_externally)),
                )
            _validate_policy(changes.get("delivery_policy"))
            now = dt_util.utcnow()
            history = list(current.occurrence_history)
            active = _find_occurrence(current, current.current_occurrence_id)
            target_type = ActivationType(
                changes.pop("activation_type", current.activation_type)
            )
            time_rearmed = False
            if "available_from" in changes:
                changes["available_from"] = _normalize_optional_time(
                    changes["available_from"]
                )
            if "expires_at" in changes:
                changes["expires_at"] = _normalize_optional_time(changes["expires_at"])
            available = changes.get("available_from", current.available_from)
            expiry = changes.get("expires_at", current.expires_at)
            cooldown = int(changes.get("cooldown_seconds", current.cooldown_seconds))
            _validate_trigger_options(cooldown, available, expiry)
            changes["cooldown_seconds"] = cooldown
            if "trigger" in changes and not isinstance(
                changes["trigger"], TriggerDefinition
            ):
                changes["trigger"] = TriggerDefinition.from_dict(changes["trigger"])
            for field_name in ("deliver_when", "complete_when"):
                if field_name in changes:
                    changes[field_name] = _coerce_trigger(changes[field_name])
                    changes[f"{field_name}_summary"] = (
                        self._summary(changes[field_name])
                        if changes[field_name] is not None
                        else None
                    )
                    changes["trigger_duration_waits"] = tuple(
                        wait
                        for wait in changes.get(
                            "trigger_duration_waits", current.trigger_duration_waits
                        )
                        if wait.role != field_name
                    )
            if "escalation" in changes:
                changes["escalation"] = _coerce_escalation(changes["escalation"])
            if "missed_occurrence_policy" in changes:
                changes["missed_occurrence_policy"] = MissedOccurrencePolicy(
                    changes["missed_occurrence_policy"]
                )
            if "expires_after_seconds" in changes:
                changes["expires_after_seconds"] = _validate_expiry_window(
                    changes["expires_after_seconds"]
                )
            if "repeat_policy" in changes:
                changes["repeat_policy"] = TriggerRepeatPolicy(changes["repeat_policy"])
            if "while_awaiting_acknowledgement" in changes:
                changes["while_awaiting_acknowledgement"] = (
                    WhileAwaitingAcknowledgement(
                        changes["while_awaiting_acknowledgement"]
                    )
                )
            trigger_fields = {
                "trigger",
                "repeat_policy",
                "fire_if_already_matching",
                "available_from",
                "expires_at",
            }
            rearm_trigger = target_type is ActivationType.TRIGGER and (
                current.activation_type is not ActivationType.TRIGGER
                or bool(trigger_fields.intersection(changes))
            )
            if target_type is ActivationType.TRIGGER:
                if "due" in changes or "recurrence" in changes:
                    raise ReminderValidationError(
                        "Triggered reminders cannot have due or recurrence fields"
                    )
                definition = changes.get("trigger", current.trigger)
                if not isinstance(definition, TriggerDefinition):
                    raise ReminderValidationError(
                        "A trigger definition is required for trigger activation"
                    )
                if active and active.status in {
                    OccurrenceStatus.SCHEDULED,
                    OccurrenceStatus.WAITING_FOR_CONTEXT,
                }:
                    history = _replace_occurrence(
                        history, active.updated(status=OccurrenceStatus.CANCELLED)
                    )
                changes.update(
                    activation_type=ActivationType.TRIGGER,
                    trigger=definition,
                    trigger_summary=self._summary(definition),
                    due=None,
                    recurrence=None,
                    scheduled_due=None,
                    current_occurrence_id=None,
                    paused=False,
                    paused_at=None,
                )
                if rearm_trigger:
                    changes.update(
                        status=_trigger_waiting_status(now, available, expiry),
                        immediate_evaluated=False,
                        trigger_duration_waits=(),
                    )
                else:
                    changes["status"] = current.status
            elif "recurrence" in changes:
                recurrence = changes["recurrence"]
                if not isinstance(recurrence, RecurrenceRule):
                    raise ReminderValidationError("Recurrence rule is invalid")
                next_due = first_due(recurrence, now)
                if active and active.status in {
                    OccurrenceStatus.SCHEDULED,
                    OccurrenceStatus.WAITING_FOR_CONTEXT,
                }:
                    history = _replace_occurrence(
                        history, active.updated(status=OccurrenceStatus.CANCELLED)
                    )
                if current.paused:
                    changes.update(
                        activation_type=ActivationType.TIME,
                        trigger=None,
                        due=None,
                        scheduled_due=None,
                        current_occurrence_id=None,
                        status=ReminderStatus.PAUSED,
                    )
                else:
                    time_rearmed = True
                    new_occurrence = _new_occurrence(next_due)
                    history.append(new_occurrence)
                    changes.update(
                        activation_type=ActivationType.TIME,
                        trigger=None,
                        due=next_due,
                        scheduled_due=next_due,
                        current_occurrence_id=new_occurrence.id,
                        current_occurrence_number=occurrence_number(
                            recurrence, next_due
                        ),
                    )
            elif "due" in changes:
                if current.recurrence is not None:
                    raise ReminderValidationError(
                        "Recurring due time is derived from its recurrence rule"
                    )
                time_rearmed = True
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
                changes.update(
                    activation_type=ActivationType.TIME,
                    trigger=None,
                    trigger_summary=None,
                    status=ReminderStatus.PENDING,
                )
            elif target_type is ActivationType.TIME:
                if current.activation_type is ActivationType.TRIGGER:
                    raise ReminderValidationError(
                        "Changing to time activation requires a due time"
                    )
                changes["activation_type"] = ActivationType.TIME
            if (
                current.status is ReminderStatus.WAITING_FOR_CONTEXT
                and "deliver_when" in changes
            ):
                time_rearmed = True
            if (
                current.status is ReminderStatus.WAITING_FOR_CONTEXT
                and "deliver_when" in changes
                and changes["deliver_when"] is None
            ):
                if active is not None:
                    history = _replace_occurrence(
                        history, active.updated(status=OccurrenceStatus.SCHEDULED)
                    )
                changes["status"] = ReminderStatus.PENDING
            if "escalation" in changes:
                policy = changes["escalation"]
                rewritten: list[Occurrence] = []
                for item in history:
                    if item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT:
                        rewritten.append(
                            item.updated(
                                next_escalation_at=(
                                    now
                                    + timedelta(minutes=policy.initial_delay_minutes)
                                    if policy is not None
                                    else None
                                ),
                                escalation_attempt_count=0,
                                escalation_history=(),
                            )
                        )
                    else:
                        # Completed/resolved occurrence history is durable evidence.
                        rewritten.append(item)
                history = rewritten
            if (
                "expires_after_seconds" in changes
                and current.status is ReminderStatus.WAITING_FOR_CONTEXT
                and active is not None
            ):
                history = _replace_occurrence(
                    history,
                    active.updated(
                        expires_at=(
                            active.scheduled_due
                            + timedelta(seconds=changes["expires_after_seconds"])
                            if changes["expires_after_seconds"] is not None
                            else None
                        )
                    ),
                )
            if changes.get("user_id", current.user_id) != current.user_id:
                history = _rotate_notification_action_tokens(history)
            changes["occurrence_history"] = tuple(history)
            changes.setdefault(
                "status",
                ReminderStatus.PENDING if time_rearmed else current.status,
            )
            updated = current.updated(**changes, updated_at=now)
            if time_rearmed:
                updated = updated.updated(delivered_at=None, delivery_errors=())
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        self._cancel_trigger_duration_timers(reminder_id)
        await self._trigger_registry.async_sync(self._reminders.values())
        if expiry_window_changed:
            await self._async_process_occurrence_expiry(dt_util.utcnow())
            updated = await self.async_get(reminder_id)
        if not updated.immediate_evaluated:
            await self._async_evaluate_immediate({updated.id})
        await self._async_restore_trigger_durations({updated.id})
        if updated.due is not None and updated.due <= dt_util.utcnow():
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
        self._cancel_trigger_duration_timers(reminder_id)
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({reminder.user_id})
        self._fire_lifecycle_event(reminder, "deleted")

    async def async_pause(self, reminder_id: str) -> Reminder:
        """Pause an anchored recurring series without changing its rule."""
        now = dt_util.utcnow()
        async with self._lock:
            current = self._require(reminder_id)
            if current.recurrence is None:
                raise ReminderValidationError("Only recurring reminders can be paused")
            if current.status is ReminderStatus.DELIVERING:
                raise ReminderValidationError("Reminder is currently being delivered")
            if current.paused:
                return current
            history = list(current.occurrence_history)
            active = _find_occurrence(current, current.current_occurrence_id)
            preserve_scheduled = (
                active is not None
                and active.status is OccurrenceStatus.SCHEDULED
                and active.due > now
            )
            if (
                active is not None
                and active.status is OccurrenceStatus.WAITING_FOR_CONTEXT
            ):
                history = _replace_occurrence(
                    history,
                    active.updated(
                        status=OccurrenceStatus.CANCELLED,
                        completed_at=now,
                        completion_reason="series_paused",
                    ),
                )
            elif (
                active is not None
                and active.status is OccurrenceStatus.SCHEDULED
                and not preserve_scheduled
            ):
                history = _replace_occurrence(
                    history,
                    active.updated(
                        status=OccurrenceStatus.SKIPPED,
                        completed_at=now,
                        completion_reason="paused_occurrence_missed",
                    ),
                )
            preserved_scheduled_due = None
            preserved_occurrence_id = None
            if preserve_scheduled:
                assert active is not None
                preserved_scheduled_due = active.scheduled_due
                preserved_occurrence_id = active.id
            updated = current.updated(
                paused=True,
                paused_at=now,
                status=ReminderStatus.PAUSED,
                due=None,
                scheduled_due=preserved_scheduled_due,
                current_occurrence_id=preserved_occurrence_id,
                occurrence_history=tuple(history),
                trigger_duration_waits=tuple(
                    wait
                    for wait in current.trigger_duration_waits
                    if wait.role != "deliver_when"
                ),
                updated_at=now,
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        self._cancel_trigger_duration_timers(reminder_id, "deliver_when")
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({current.user_id})
        self._fire_lifecycle_event(updated, "paused")
        return updated

    async def async_resume(self, reminder_id: str) -> Reminder:
        """Resume from the next future instant of the unchanged anchored rule."""
        now = dt_util.utcnow()
        async with self._lock:
            current = self._require(reminder_id)
            if current.recurrence is None:
                raise ReminderValidationError("Only recurring reminders can be resumed")
            if not current.paused:
                return current
            history = list(current.occurrence_history)
            active = _find_occurrence(current, current.current_occurrence_id)
            if (
                active is not None
                and active.status is OccurrenceStatus.SCHEDULED
                and active.due > now
            ):
                updated = current.updated(
                    paused=False,
                    paused_at=None,
                    status=ReminderStatus.PENDING,
                    due=active.due,
                    scheduled_due=active.scheduled_due,
                    current_occurrence_number=occurrence_number(
                        current.recurrence, active.scheduled_due
                    ),
                    updated_at=now,
                )
            elif active is not None and active.status is OccurrenceStatus.SCHEDULED:
                skipped = active.updated(
                    status=OccurrenceStatus.SKIPPED,
                    completed_at=now,
                    completion_reason="paused_occurrence_missed",
                )
                history = _replace_occurrence(history, skipped)
                updated = _advance_recurring_series(
                    current,
                    history,
                    resolved_due=active.scheduled_due,
                    resolved_status=ReminderStatus.SKIPPED,
                    now=now,
                    after=max(now, active.scheduled_due),
                ).updated(paused=False, paused_at=None)
            else:
                due = next_due_after(current.recurrence, now)
                if due is None:
                    updated = current.updated(
                        paused=False,
                        paused_at=None,
                        status=current.last_occurrence_status or ReminderStatus.SKIPPED,
                        updated_at=now,
                    )
                else:
                    occurrence = _new_occurrence(due)
                    updated = current.updated(
                        paused=False,
                        paused_at=None,
                        status=ReminderStatus.PENDING,
                        due=due,
                        scheduled_due=due,
                        current_occurrence_id=occurrence.id,
                        current_occurrence_number=occurrence_number(
                            current.recurrence, due
                        ),
                        occurrence_history=(*current.occurrence_history, occurrence),
                        updated_at=now,
                    )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({current.user_id})
        self._fire_lifecycle_event(updated, "resumed")
        await self._async_process_due(now)
        return await self.async_get(reminder_id)

    async def async_skip_next(self, reminder_id: str) -> Reminder:
        """Skip exactly the active anchored occurrence and retain series phase."""
        now = dt_util.utcnow()
        async with self._lock:
            current = self._require(reminder_id)
            if current.recurrence is None:
                raise ReminderValidationError("Only recurring reminders can be skipped")
            if current.paused:
                raise ReminderValidationError("Resume the series before skipping")
            active = _find_occurrence(current, current.current_occurrence_id)
            if active is None or active.status not in {
                OccurrenceStatus.SCHEDULED,
                OccurrenceStatus.WAITING_FOR_CONTEXT,
            }:
                raise ReminderValidationError(
                    "There is no scheduled occurrence to skip"
                )
            skipped = active.updated(
                status=OccurrenceStatus.SKIPPED,
                completed_at=now,
                completion_reason="user_skipped",
            )
            history = _replace_occurrence(list(current.occurrence_history), skipped)
            updated = _advance_recurring_series(
                current,
                history,
                resolved_due=active.scheduled_due,
                resolved_status=ReminderStatus.SKIPPED,
                now=now,
                after=active.scheduled_due,
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        self._cancel_trigger_duration_timers(reminder_id, "deliver_when")
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({current.user_id})
        self._fire_lifecycle_event(updated, "skipped", occurrence_id=active.id)
        return updated

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
        current = await self.async_get(reminder_id)
        if current.activation_type is ActivationType.TRIGGER:
            if duration is None:
                raise ReminderValidationError(
                    "Triggered reminders can only be snoozed by duration"
                )
            snoozed_until = _normalize_due(dt_util.utcnow() + duration)
            async with self._lock:
                current = self._require(reminder_id)
                if current.status is ReminderStatus.DELIVERING:
                    raise ReminderValidationError(
                        "Reminder is currently being delivered"
                    )
                history = list(current.occurrence_history)
                active = _find_occurrence(current, current.current_occurrence_id)
                if active and active.status in {
                    OccurrenceStatus.DELIVERED,
                    OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
                }:
                    history = _replace_occurrence(
                        history,
                        active.updated(
                            status=(
                                OccurrenceStatus.ACKNOWLEDGED
                                if active.status
                                is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                                else active.status
                            ),
                            snoozed=True,
                            snoozed_at=dt_util.utcnow(),
                            completion_source="snooze",
                            next_escalation_at=None,
                        ),
                    )
                updated = current.updated(
                    status=ReminderStatus.WAITING_FOR_TRIGGER,
                    snoozed_until=snoozed_until,
                    last_triggered_at=(
                        None
                        if current.repeat_policy is TriggerRepeatPolicy.ONCE
                        else current.last_triggered_at
                    ),
                    occurrence_history=tuple(history),
                    current_occurrence_id=None,
                    updated_at=dt_util.utcnow(),
                )
                candidate = dict(self._reminders)
                candidate[reminder_id] = updated
                await self._async_persist_state(candidate, self._users)
                self._reschedule(force=True)
            await self._trigger_registry.async_sync(self._reminders.values())
            self._notify_changed({current.user_id})
            return updated
        new_due = due if due is not None else dt_util.utcnow() + duration  # type: ignore[operator]
        new_due = _normalize_due(new_due)
        async with self._lock:
            current = self._require(reminder_id)
            if current.status is ReminderStatus.DELIVERING:
                raise ReminderValidationError("Reminder is currently being delivered")
            occurrence = _find_occurrence(current, current.current_occurrence_id)
            if occurrence is None:
                if current.due is None:
                    raise ReminderValidationError("Timed reminder has no due time")
                occurrence = _new_occurrence(
                    current.due,
                    scheduled_due=current.scheduled_due or current.due,
                )
                history = [*current.occurrence_history, occurrence]
            else:
                history = list(current.occurrence_history)
            if occurrence.status not in {
                OccurrenceStatus.SCHEDULED,
                OccurrenceStatus.WAITING_FOR_CONTEXT,
                OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
                OccurrenceStatus.DELIVERED,
                OccurrenceStatus.FAILED,
            }:
                raise ReminderValidationError("Reminder has no occurrence to snooze")
            if occurrence.status is OccurrenceStatus.WAITING_FOR_CONTEXT:
                occurrence = occurrence.updated(
                    status=OccurrenceStatus.SCHEDULED,
                    due=new_due,
                    context_eligible_at=None,
                    snoozed=True,
                    snoozed_at=dt_util.utcnow(),
                )
                history = _replace_occurrence(history, occurrence)
            elif occurrence.status is not OccurrenceStatus.SCHEDULED:
                history = _replace_occurrence(
                    history,
                    occurrence.updated(
                        status=OccurrenceStatus.CANCELLED,
                        completion_source="snooze",
                        next_escalation_at=None,
                    ),
                )
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
        if updated.due is not None and updated.due <= dt_util.utcnow():
            await self._async_process_due(dt_util.utcnow())
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({current.user_id})
        self._fire_lifecycle_event(
            current, "snoozed", occurrence_id=current.current_occurrence_id
        )
        return await self.async_get(updated.id)

    async def async_wait_for_next_trigger(self, reminder_id: str) -> Reminder:
        """Resolve the active occurrence and re-arm for a future transition."""
        async with self._lock:
            current = self._require(reminder_id)
            if current.activation_type is not ActivationType.TRIGGER:
                raise ReminderValidationError(
                    "Wait for next trigger is only valid for triggered reminders"
                )
            history = list(current.occurrence_history)
            active = _find_occurrence(current, current.current_occurrence_id)
            if active and active.status in {
                OccurrenceStatus.DELIVERED,
                OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
                OccurrenceStatus.FAILED,
            }:
                history = _replace_occurrence(
                    history, active.updated(status=OccurrenceStatus.CANCELLED)
                )
            updated = current.updated(
                status=_trigger_waiting_status(
                    dt_util.utcnow(), current.available_from, current.expires_at
                ),
                current_occurrence_id=None,
                occurrence_history=tuple(history),
                snoozed_until=None,
                last_triggered_at=(
                    None
                    if current.repeat_policy is TriggerRepeatPolicy.ONCE
                    else current.last_triggered_at
                ),
                immediate_evaluated=True,
                updated_at=dt_util.utcnow(),
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({current.user_id})
        return updated

    async def async_snooze_occurrence(
        self, reminder_id: str, occurrence_id: str, duration: timedelta
    ) -> Reminder | None:
        """Snooze exactly one delivered occurrence, idempotently."""
        if duration <= timedelta(0):
            raise ReminderValidationError("Snooze duration must be positive")
        now = dt_util.utcnow()
        new_due = _normalize_due(now + duration)
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if current is None:
                return None
            target = _find_occurrence(current, occurrence_id)
            if target is None or target.status not in {
                OccurrenceStatus.DELIVERED,
                OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
            }:
                return None
            if (
                current.status is ReminderStatus.DELIVERING
                and target.id == current.current_occurrence_id
            ):
                return None
            history = list(current.occurrence_history)
            if (
                current.recurrence is not None
                and target.id != current.current_occurrence_id
            ):
                retry = target.updated(
                    due=new_due,
                    status=OccurrenceStatus.SCHEDULED,
                    delivered_at=None,
                    succeeded_channels=(),
                    failed_channels=(),
                    delivery_errors=(),
                    suppressed_channels=(),
                    acknowledgement_required=False,
                    acknowledged_at=None,
                    acknowledged_by=None,
                    completion_source=None,
                    completion_reason=None,
                    snoozed=True,
                    snoozed_at=now,
                    notification_action_token=secrets.token_urlsafe(24),
                    next_escalation_at=None,
                    redelivery_count=target.redelivery_count + 1,
                )
                history = _replace_occurrence(history, retry)
                updated = current.updated(
                    occurrence_history=tuple(history), updated_at=now
                )
                candidate = dict(self._reminders)
                candidate[reminder_id] = updated
                await self._async_persist_state(candidate, self._users)
                self._reschedule(force=True)
            else:
                history = _replace_occurrence(
                    history,
                    target.updated(
                        status=OccurrenceStatus.CANCELLED,
                        snoozed=True,
                        snoozed_at=now,
                        completion_source="snooze",
                        completion_reason="mobile_action",
                        next_escalation_at=None,
                    ),
                )
                active = _find_occurrence(current, current.current_occurrence_id)
                if (
                    active is not None
                    and active.id != target.id
                    and active.status
                    in {
                        OccurrenceStatus.SCHEDULED,
                        OccurrenceStatus.WAITING_FOR_CONTEXT,
                    }
                ):
                    history = _replace_occurrence(
                        history, active.updated(status=OccurrenceStatus.CANCELLED)
                    )
                snoozed = _new_occurrence(
                    new_due, scheduled_due=target.scheduled_due
                ).updated(snoozed=True, snoozed_at=now)
                history.append(snoozed)
                updated = current.updated(
                    due=new_due,
                    scheduled_due=(
                        target.scheduled_due
                        if current.recurrence is not None
                        else current.scheduled_due
                    ),
                    current_occurrence_number=(
                        occurrence_number(current.recurrence, target.scheduled_due)
                        if current.recurrence is not None
                        else current.current_occurrence_number
                    ),
                    status=(
                        ReminderStatus.WAITING_FOR_TRIGGER
                        if current.activation_type is ActivationType.TRIGGER
                        else ReminderStatus.PENDING
                    ),
                    current_occurrence_id=snoozed.id,
                    occurrence_history=tuple(history),
                    snoozed_until=(
                        new_due
                        if current.activation_type is ActivationType.TRIGGER
                        else None
                    ),
                    delivered_at=None,
                    delivery_errors=(),
                    updated_at=now,
                )
                candidate = dict(self._reminders)
                candidate[reminder_id] = updated
                await self._async_persist_state(candidate, self._users)
                self._reschedule(force=True)
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({current.user_id})
        self._fire_lifecycle_event(current, "snoozed", occurrence_id=target.id)
        return updated

    async def async_acknowledge(
        self,
        reminder_id: str,
        *,
        occurrence_id: str | None = None,
        acknowledged_by: str | None = None,
        completion_source: str = "manual",
        completion_reason: str | None = None,
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
                completion_source=completion_source,
                completion_reason=completion_reason,
                next_escalation_at=None,
            )
            history = _replace_occurrence(
                list(reminder.occurrence_history), acknowledged
            )
            status = reminder.status
            if reminder.status is ReminderStatus.AWAITING_ACKNOWLEDGEMENT:
                status = ReminderStatus.ACKNOWLEDGED
            current_occurrence_id = reminder.current_occurrence_id
            if (
                reminder.activation_type is ActivationType.TRIGGER
                and reminder.repeat_policy
                is TriggerRepeatPolicy.REARM_AFTER_ACKNOWLEDGEMENT
            ):
                status = _trigger_waiting_status(
                    now, reminder.available_from, reminder.expires_at
                )
                current_occurrence_id = None
            updated = reminder.updated(
                occurrence_history=tuple(history),
                status=status,
                current_occurrence_id=current_occurrence_id,
                updated_at=now,
            )
            candidate = dict(self._reminders)
            candidate[reminder.id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({reminder.user_id})
        self._fire_lifecycle_event(
            reminder,
            (
                "acknowledged"
                if completion_source != "automatic"
                else "automatically_completed"
            ),
            occurrence_id=acknowledged.id,
        )
        return acknowledged

    async def async_complete(
        self,
        reminder_id: str,
        *,
        occurrence_id: str | None = None,
        completed_by: str | None = None,
        completion_source: str = "manual",
    ) -> Occurrence:
        """Record that a user explicitly completed the underlying task."""
        async with self._lock:
            reminder = self._require(reminder_id)
            if not reminder.allow_manual_completion:
                raise ReminderValidationError("Manual completion is not enabled")
            candidates = [
                item
                for item in reminder.occurrence_history
                if item.status
                in {
                    OccurrenceStatus.DELIVERED,
                    OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
                }
                and (occurrence_id is None or item.id == occurrence_id)
            ]
            if len(candidates) != 1:
                raise ReminderValidationError(
                    "Exactly one matching delivered occurrence is required"
                )
            now = dt_util.utcnow()
            completed = candidates[0].updated(
                status=OccurrenceStatus.COMPLETED,
                completed_at=now,
                completed_by=completed_by,
                completion_source=completion_source,
                completion_reason="done",
                next_escalation_at=None,
            )
            history = _replace_occurrence(list(reminder.occurrence_history), completed)
            status = reminder.status
            current_occurrence_id = reminder.current_occurrence_id
            if (
                completed.id == reminder.current_occurrence_id
                and reminder.recurrence is None
            ):
                if (
                    reminder.activation_type is ActivationType.TRIGGER
                    and reminder.repeat_policy is not TriggerRepeatPolicy.ONCE
                ):
                    status = _trigger_waiting_status(
                        now, reminder.available_from, reminder.expires_at
                    )
                    current_occurrence_id = None
                else:
                    status = ReminderStatus.COMPLETED
            updated = reminder.updated(
                occurrence_history=tuple(history),
                status=status,
                current_occurrence_id=current_occurrence_id,
                updated_at=now,
            )
            candidate = dict(self._reminders)
            candidate[reminder.id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({reminder.user_id})
        self._fire_lifecycle_event(reminder, "completed", occurrence_id=completed.id)
        return completed

    async def async_select_external_action(
        self,
        reminder_id: str,
        external_action_id: str,
        *,
        occurrence_id: str | None = None,
        selected_by: str | None = None,
    ) -> Occurrence:
        """Persist and report one bounded source-defined action selection."""
        async with self._lock:
            reminder = self._require(reminder_id)
            valid_ids = {item["id"] for item in reminder.external_actions}
            if not reminder.managed_externally or external_action_id not in valid_ids:
                raise ReminderValidationError("Unknown external action")
            candidates = [
                item
                for item in reminder.occurrence_history
                if item.status
                in {
                    OccurrenceStatus.DELIVERED,
                    OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
                }
                and item.external_action_id is None
                and (occurrence_id is None or item.id == occurrence_id)
            ]
            if len(candidates) != 1:
                raise ReminderValidationError(
                    "Exactly one unresolved delivered occurrence is required"
                )
            now = dt_util.utcnow()
            selected = candidates[0].updated(
                external_action_id=external_action_id,
                external_action_selected_at=now,
                external_action_selected_by=selected_by,
            )
            updated = reminder.updated(
                occurrence_history=tuple(
                    _replace_occurrence(list(reminder.occurrence_history), selected)
                ),
                updated_at=now,
            )
            candidate = dict(self._reminders)
            candidate[reminder.id] = updated
            await self._async_persist_state(candidate, self._users)
        self._notify_changed({reminder.user_id})
        self._fire_lifecycle_event(
            reminder,
            "external_action",
            occurrence_id=selected.id,
            external_action_id=external_action_id,
        )
        return selected

    def _mobile_action_received(self, event: Event[Any]) -> None:
        """Dispatch an opaque mobile action token without exposing owner data."""
        action = event.data.get("action")
        if not isinstance(action, str) or not action.startswith(MOBILE_ACTION_PREFIX):
            return
        self._hass.create_task(
            self._async_handle_mobile_action(action), "reminders mobile action"
        )

    async def _async_handle_mobile_action(self, action: str) -> None:
        payload = action.removeprefix(MOBILE_ACTION_PREFIX)
        token, separator, operation = payload.partition(":")
        if not separator or not token:
            return
        match: tuple[str, str] | None = None
        async with self._lock:
            for reminder in self._reminders.values():
                for occurrence in reminder.occurrence_history:
                    if secrets.compare_digest(
                        occurrence.notification_action_token or "", token
                    ):
                        match = (reminder.id, occurrence.id)
                        break
                if match is not None:
                    break
        if match is None:
            return
        reminder_id, occurrence_id = match
        try:
            if operation == "DONE":
                await self.async_complete(
                    reminder_id,
                    occurrence_id=occurrence_id,
                    completed_by=None,
                    completion_source="mobile_action",
                )
            elif operation == "DISMISS":
                await self.async_acknowledge(
                    reminder_id,
                    occurrence_id=occurrence_id,
                    acknowledged_by=None,
                    completion_source="mobile_action",
                    completion_reason="dismissed",
                )
            elif operation.startswith("EXTERNAL_"):
                await self.async_select_external_action(
                    reminder_id,
                    operation.removeprefix("EXTERNAL_"),
                    occurrence_id=occurrence_id,
                )
            elif operation == "SNOOZE_10":
                await self.async_snooze_occurrence(
                    reminder_id, occurrence_id, timedelta(minutes=10)
                )
            elif operation == "SNOOZE_60":
                await self.async_snooze_occurrence(
                    reminder_id, occurrence_id, timedelta(hours=1)
                )
        except ReminderNotFoundError, ReminderValidationError:
            # Old, duplicate, forged, and already-resolved actions are safe no-ops.
            return

    async def async_fire_named_trigger(
        self, trigger_id: str, *, user_id: str
    ) -> dict[str, int | str]:
        """Activate one user's reminders indexed under a named trigger."""
        definition = TriggerDefinition.from_dict(
            {"type": TriggerType.NAMED, "trigger_id": trigger_id}
        )
        normalized = definition.trigger_id or ""
        references = self._trigger_registry.named_references(normalized)
        owned = []
        async with self._lock:
            for reference in references:
                reminder_id = reference.partition("::")[0]
                reminder = self._reminders.get(reminder_id)
                if reminder is not None and reminder.user_id == user_id:
                    owned.append(reference)
        result: dict[str, int | str] = {
            "trigger_id": normalized,
            "matched": len(owned),
            "activated": 0,
            "skipped_cooldown": 0,
            "skipped_inactive": 0,
            "failed": 0,
        }
        for reference in owned:
            try:
                if "::" not in reference:
                    outcome = await self.async_activate_trigger(
                        reference,
                        cause="named_trigger_service",
                        context={"trigger_id": normalized},
                    )
                else:
                    await self._async_trigger_callback(
                        reference,
                        "named_trigger_service",
                        {"trigger_id": normalized},
                    )
                    outcome = "activated"
            except Exception:
                _LOGGER.exception("Error firing named reminder trigger")
                outcome = "failed"
            key = {
                "activated": "activated",
                "cooldown": "skipped_cooldown",
                "inactive": "skipped_inactive",
                "failed": "failed",
            }[outcome]
            result[key] = int(result[key]) + 1
        return result

    async def _async_trigger_callback(
        self, reminder_id: str, cause: str, context: dict[str, Any]
    ) -> None:
        reminder_id, separator, role = reminder_id.partition("::")
        if self._unloaded:
            return
        duration_role = role if separator else "activation"
        if cause == "duration_started":
            await self._async_start_trigger_duration(
                reminder_id,
                role=duration_role,
                cause="future_transition",
                context=context,
            )
            return
        if cause == "duration_cancelled":
            await self._async_clear_trigger_duration(reminder_id, duration_role)
            return
        if not separator:
            await self.async_activate_trigger(reminder_id, cause=cause, context=context)
        elif role == "deliver_when":
            await self.async_activate_delivery_context(
                reminder_id, cause=cause, context=context
            )
        elif role == "complete_when":
            await self.async_complete_automatically(
                reminder_id, cause=cause, context=context
            )

    async def async_activate_delivery_context(
        self,
        reminder_id: str,
        *,
        cause: str,
        context: dict[str, Any],
        _expected_duration_wait: TriggerDurationWait | None = None,
    ) -> str:
        """Claim a context-waiting occurrence and deliver it once."""
        now = dt_util.utcnow()
        expired = False
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if (
                self._unloaded
                or current is None
                or current.status is not ReminderStatus.WAITING_FOR_CONTEXT
                or current.deliver_when is None
            ):
                return "inactive"
            current_wait = _trigger_duration_wait(current, "deliver_when")
            if (
                _expected_duration_wait is not None
                and current_wait != _expected_duration_wait
            ):
                return "inactive"
            deliver_when = current.deliver_when
            current = _replace_trigger_duration_wait(current, "deliver_when", None)
            occurrence = _find_occurrence(current, current.current_occurrence_id)
            if (
                occurrence is None
                or occurrence.status is not OccurrenceStatus.WAITING_FOR_CONTEXT
            ):
                return "inactive"
            expired = occurrence.expires_at is not None and now >= occurrence.expires_at
            if not expired:
                activated = occurrence.updated(
                    status=OccurrenceStatus.DELIVERING,
                    trigger_type=deliver_when.type.value,
                    trigger_summary=current.deliver_when_summary,
                    triggered_at=now,
                    activation_cause=cause,
                    trigger_context=_sanitise_trigger_context(context),
                )
                history = _replace_occurrence(
                    list(current.occurrence_history), activated
                )
                candidate = dict(self._reminders)
                candidate[reminder_id] = current.updated(
                    status=ReminderStatus.DELIVERING,
                    occurrence_history=tuple(history),
                    updated_at=now,
                )
                await self._async_persist_state(candidate, self._users)
        if expired:
            await self._async_process_occurrence_expiry(now)
            return "expired"
        await self._trigger_registry.async_sync(self._reminders.values())
        await self._async_deliver_claimed(reminder_id, now)
        await self._trigger_registry.async_sync(self._reminders.values())
        return "activated"

    async def async_complete_automatically(
        self,
        reminder_id: str,
        *,
        cause: str,
        context: dict[str, Any],
        _expected_duration_wait: TriggerDurationWait | None = None,
    ) -> str:
        """Resolve the occurrence affected by a bounded completion trigger."""
        now = dt_util.utcnow()
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if self._unloaded or current is None or current.complete_when is None:
                return "inactive"
            current_wait = _trigger_duration_wait(current, "complete_when")
            if (
                _expected_duration_wait is not None
                and current_wait != _expected_duration_wait
            ):
                return "inactive"
            complete_when = current.complete_when
            current = _replace_trigger_duration_wait(current, "complete_when", None)
            occurrence = _automatic_completion_occurrence(current)
            if occurrence is None or occurrence.status not in {
                OccurrenceStatus.SCHEDULED,
                OccurrenceStatus.WAITING_FOR_CONTEXT,
                OccurrenceStatus.DELIVERING,
                OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
            }:
                return "inactive"
            completed = occurrence.updated(
                status=OccurrenceStatus.COMPLETED,
                completed_at=now,
                completed_by=None,
                completion_source="automatic",
                completion_reason=cause,
                trigger_type=complete_when.type.value,
                trigger_summary=current.complete_when_summary,
                triggered_at=now,
                activation_cause=cause,
                trigger_context=_sanitise_trigger_context(context),
                next_escalation_at=None,
            )
            history = _replace_occurrence(list(current.occurrence_history), completed)
            if (
                current.recurrence is not None
                and occurrence.id != current.current_occurrence_id
            ):
                updated = current.updated(
                    occurrence_history=tuple(history), updated_at=now
                )
            elif current.recurrence is not None:
                scheduled_due = current.scheduled_due or occurrence.scheduled_due
                next_due = next_due_after(current.recurrence, max(now, scheduled_due))
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
                        last_occurrence_status=ReminderStatus.COMPLETED,
                        occurrence_history=tuple(history),
                        updated_at=now,
                    )
                else:
                    updated = current.updated(
                        status=ReminderStatus.COMPLETED,
                        current_occurrence_id=completed.id,
                        occurrence_history=tuple(history),
                        updated_at=now,
                    )
            elif (
                current.activation_type is ActivationType.TRIGGER
                and current.repeat_policy is not TriggerRepeatPolicy.ONCE
            ):
                updated = current.updated(
                    status=_trigger_waiting_status(
                        now, current.available_from, current.expires_at
                    ),
                    current_occurrence_id=None,
                    occurrence_history=tuple(history),
                    updated_at=now,
                )
            else:
                updated = current.updated(
                    status=ReminderStatus.COMPLETED,
                    occurrence_history=tuple(history),
                    updated_at=now,
                )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        await self._trigger_registry.async_sync(self._reminders.values())
        self._notify_changed({current.user_id})
        self._fire_lifecycle_event(
            current, "automatically_completed", occurrence_id=completed.id
        )
        return "completed"

    async def async_activate_trigger(
        self,
        reminder_id: str,
        *,
        cause: str,
        context: dict[str, Any],
        _expected_duration_wait: TriggerDurationWait | None = None,
    ) -> str:
        """Claim one eligible trigger hit and route it through normal delivery."""
        now = dt_util.utcnow()
        owner: str | None = None
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if (
                self._unloaded
                or current is None
                or current.activation_type is not ActivationType.TRIGGER
                or current.trigger is None
            ):
                return "inactive"
            trigger = current.trigger
            current_wait = _trigger_duration_wait(current, "activation")
            if (
                _expected_duration_wait is not None
                and current_wait != _expected_duration_wait
            ):
                return "inactive"
            duration_pending = current_wait is not None
            if duration_pending:
                current = _replace_trigger_duration_wait(current, "activation", None)
            owner = current.user_id
            if current.expires_at is not None and now >= current.expires_at:
                updated = current.updated(status=ReminderStatus.EXPIRED, updated_at=now)
                candidate = dict(self._reminders)
                candidate[reminder_id] = updated
                await self._async_persist_state(candidate, self._users)
                self._reschedule(force=True)
                outcome = "inactive"
            elif (
                (current.available_from is not None and now < current.available_from)
                or (current.snoozed_until is not None and now < current.snoozed_until)
                or current.status
                in {
                    ReminderStatus.DELIVERING,
                    ReminderStatus.EXPIRED,
                    ReminderStatus.COMPLETED,
                    ReminderStatus.CANCELLED,
                    ReminderStatus.DELIVERED,
                    ReminderStatus.ACKNOWLEDGED,
                    ReminderStatus.FAILED,
                }
                or (
                    current.repeat_policy is TriggerRepeatPolicy.ONCE
                    and current.last_triggered_at is not None
                )
            ):
                outcome = "inactive"
            elif (
                current.last_triggered_at is not None
                and now
                < current.last_triggered_at
                + timedelta(seconds=current.cooldown_seconds)
            ):
                candidate = dict(self._reminders)
                candidate[reminder_id] = current.updated(
                    cooldown_skip_count=current.cooldown_skip_count + 1,
                    updated_at=now,
                )
                await self._async_persist_state(candidate, self._users)
                outcome = "cooldown"
            else:
                awaiting = any(
                    item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                    for item in current.occurrence_history
                )
                if (
                    current.repeat_policy
                    is TriggerRepeatPolicy.REARM_AFTER_ACKNOWLEDGEMENT
                    and awaiting
                ) or (
                    current.repeat_policy is TriggerRepeatPolicy.EVERY_TRIGGER
                    and awaiting
                    and current.while_awaiting_acknowledgement
                    is WhileAwaitingAcknowledgement.SKIP
                ):
                    outcome = "inactive"
                else:
                    occurrence = _new_occurrence(now).updated(
                        status=OccurrenceStatus.DELIVERING,
                        trigger_type=trigger.type.value,
                        trigger_summary=current.trigger_summary,
                        triggered_at=now,
                        activation_cause=cause,
                        trigger_context=_sanitise_trigger_context(context),
                    )
                    history = [*current.occurrence_history, occurrence]
                    claimed = current.updated(
                        status=ReminderStatus.DELIVERING,
                        current_occurrence_id=occurrence.id,
                        occurrence_history=tuple(history),
                        last_triggered_at=now,
                        snoozed_until=None,
                        updated_at=now,
                    )
                    candidate = dict(self._reminders)
                    candidate[reminder_id] = claimed
                    await self._async_persist_state(candidate, self._users)
                    outcome = "activated"
            if (
                duration_pending
                and self._reminders.get(reminder_id) is not None
                and _trigger_duration_wait(self._reminders[reminder_id], "activation")
                is not None
            ):
                candidate = dict(self._reminders)
                candidate[reminder_id] = current.updated(updated_at=now)
                await self._async_persist_state(candidate, self._users)
        if _expected_duration_wait is None:
            self._cancel_trigger_duration_timers(reminder_id, "activation")
        await self._trigger_registry.async_sync(self._reminders.values())
        if outcome == "activated":
            await self._async_deliver_claimed(reminder_id, now)
            await self._trigger_registry.async_sync(self._reminders.values())
        if owner is not None:
            self._notify_changed({owner})
        return outcome

    async def _async_evaluate_immediate(
        self, reminder_ids: set[str] | None = None
    ) -> None:
        """Safely perform each reminder's one-time already-matching evaluation."""
        candidates: list[Reminder] = []
        now = dt_util.utcnow()
        async with self._lock:
            proposed = dict(self._reminders)
            changed = False
            for reminder in self._reminders.values():
                if reminder_ids is not None and reminder.id not in reminder_ids:
                    continue
                if (
                    reminder.activation_type is not ActivationType.TRIGGER
                    or reminder.trigger is None
                    or reminder.immediate_evaluated
                    or reminder.trigger.type in {TriggerType.EVENT, TriggerType.NAMED}
                ):
                    continue
                matching = (
                    reminder.fire_if_already_matching
                    and self._trigger_registry.condition_is_currently_matching(
                        reminder.trigger
                    )
                )
                updated = reminder.updated(immediate_evaluated=True, updated_at=now)
                if matching and reminder.trigger.for_seconds:
                    updated = _replace_trigger_duration_wait(
                        updated,
                        "activation",
                        TriggerDurationWait(
                            "activation",
                            now,
                            "already_matching",
                            {"already_matching": True},
                        ),
                    )
                proposed[reminder.id] = updated
                changed = True
                if matching:
                    candidates.append(updated)
            if changed:
                await self._async_persist_state(proposed, self._users)
        for reminder in candidates:
            trigger = reminder.trigger
            if trigger is None:
                continue
            if trigger.for_seconds:
                self._schedule_trigger_duration(reminder, "activation")
            else:
                await self.async_activate_trigger(
                    reminder.id,
                    cause="already_matching",
                    context={"already_matching": True},
                )

    async def _async_start_trigger_duration(
        self,
        reminder_id: str,
        *,
        role: str,
        cause: str,
        context: dict[str, Any],
    ) -> None:
        """Persist a transition duration before installing its in-memory timer."""
        now = dt_util.utcnow()
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if (
                self._unloaded
                or current is None
                or (trigger := _trigger_for_duration_role(current, role)) is None
                or not trigger.for_seconds
            ):
                return
            observed_value = context.get("duration_observed_value")
            stored_context = {
                key: value
                for key, value in context.items()
                if key != "duration_observed_value"
            }
            updated = _replace_trigger_duration_wait(
                current,
                role,
                TriggerDurationWait(
                    role,
                    now,
                    cause,
                    _sanitise_trigger_context(stored_context),
                    observed_value,
                ),
            ).updated(updated_at=now)
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
        self._schedule_trigger_duration(updated, role)

    async def _async_clear_trigger_duration(
        self,
        reminder_id: str,
        role: str,
        *,
        expected_wait: TriggerDurationWait | None = None,
    ) -> None:
        """Discard durable duration progress after the condition stops matching."""
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if current is None:
                return
            current_wait = _trigger_duration_wait(current, role)
            if current_wait is None or (
                expected_wait is not None and current_wait != expected_wait
            ):
                return
            updated = _replace_trigger_duration_wait(current, role, None).updated(
                updated_at=dt_util.utcnow()
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
        if expected_wait is None:
            self._cancel_trigger_duration_timers(reminder_id, role)

    async def _async_restore_trigger_durations(
        self, reminder_ids: set[str] | None = None
    ) -> None:
        """Resume persisted waits from their original matching timestamps."""
        valid: list[tuple[Reminder, str]] = []
        invalid: list[tuple[str, str]] = []
        async with self._lock:
            for reminder in self._reminders.values():
                if reminder_ids is not None and reminder.id not in reminder_ids:
                    continue
                for wait in reminder.trigger_duration_waits:
                    trigger = _trigger_for_duration_role(reminder, wait.role)
                    if (
                        trigger is not None
                        and trigger.for_seconds
                        and self._trigger_registry.duration_is_still_matching(
                            trigger, wait.observed_value
                        )
                    ):
                        valid.append((reminder, wait.role))
                    else:
                        invalid.append((reminder.id, wait.role))
            if invalid:
                candidate = dict(self._reminders)
                now = dt_util.utcnow()
                for reminder_id, role in invalid:
                    candidate[reminder_id] = _replace_trigger_duration_wait(
                        candidate[reminder_id], role, None
                    ).updated(updated_at=now)
                await self._async_persist_state(candidate, self._users)
        for reminder, role in valid:
            self._schedule_trigger_duration(reminder, role)

    def _schedule_trigger_duration(self, reminder: Reminder, role: str) -> None:
        """Install one reminder-local timer for a durable shared-listener wait."""
        trigger = _trigger_for_duration_role(reminder, role)
        wait = _trigger_duration_wait(reminder, role)
        if trigger is None or wait is None:
            return
        key = (reminder.id, role)
        self._cancel_trigger_duration_timer(key)
        token = object()
        self._trigger_duration_timer_tokens[key] = token
        remaining = max(
            0.0,
            (
                wait.started_at
                + timedelta(seconds=trigger.for_seconds)
                - dt_util.utcnow()
            ).total_seconds(),
        )

        async def elapsed(_now: datetime, reminder_id: str = reminder.id) -> None:
            if (
                self._unloaded
                or self._trigger_duration_timer_tokens.get(key) is not token
            ):
                return
            try:
                current = await self.async_get(reminder_id)
            except ReminderNotFoundError:
                self._consume_trigger_duration_timer(key, token)
                return
            if self._trigger_duration_timer_tokens.get(key) is not token:
                return
            current_wait = _trigger_duration_wait(current, role)
            if current_wait != wait:
                self._consume_trigger_duration_timer(key, token)
                return
            current_trigger = _trigger_for_duration_role(current, role)
            if current_trigger is None or not (
                self._trigger_registry.duration_is_still_matching(
                    current_trigger, wait.observed_value
                )
            ):
                if self._consume_trigger_duration_timer(key, token):
                    await self._async_clear_trigger_duration(
                        reminder_id, role, expected_wait=wait
                    )
                return
            if not self._consume_trigger_duration_timer(key, token):
                return
            if role == "activation":
                await self.async_activate_trigger(
                    reminder_id,
                    cause=wait.cause,
                    context=wait.context,
                    _expected_duration_wait=wait,
                )
            elif role == "deliver_when":
                outcome = await self.async_activate_delivery_context(
                    reminder_id,
                    cause=wait.cause,
                    context=wait.context,
                    _expected_duration_wait=wait,
                )
                if outcome != "activated":
                    await self._async_clear_trigger_duration(
                        reminder_id, role, expected_wait=wait
                    )
            else:
                outcome = await self.async_complete_automatically(
                    reminder_id,
                    cause=wait.cause,
                    context=wait.context,
                    _expected_duration_wait=wait,
                )
                if outcome != "completed":
                    await self._async_clear_trigger_duration(
                        reminder_id, role, expected_wait=wait
                    )

        self._trigger_duration_timers[key] = async_call_later(
            self._hass, remaining, elapsed
        )

    def _cancel_trigger_duration_timer(self, key: tuple[str, str]) -> None:
        """Cancel one timer and invalidate any callback already queued for it."""
        self._trigger_duration_timer_tokens.pop(key, None)
        cancel = self._trigger_duration_timers.pop(key, None)
        if cancel is not None:
            cancel()

    def _consume_trigger_duration_timer(
        self, key: tuple[str, str], token: object
    ) -> bool:
        """Detach only the timer instance whose callback is currently running."""
        if self._trigger_duration_timer_tokens.get(key) is not token:
            return False
        self._trigger_duration_timer_tokens.pop(key, None)
        self._trigger_duration_timers.pop(key, None)
        return True

    def _cancel_trigger_duration_timers(
        self, reminder_id: str, role: str | None = None
    ) -> None:
        for key in tuple(self._trigger_duration_timers):
            if key[0] != reminder_id or (role is not None and key[1] != role):
                continue
            self._cancel_trigger_duration_timer(key)

    def _summary(self, trigger: TriggerDefinition) -> str:
        names: dict[str, str] = {}
        for entity_id in (trigger.entity_id, trigger.zone_entity_id):
            if entity_id and (state := self._hass.states.get(entity_id)) is not None:
                names[entity_id] = str(state.attributes.get("friendly_name", entity_id))
        return trigger_summary(trigger, names)

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

    async def _async_process_occurrence_expiry(self, now: datetime) -> None:
        """Resolve context-waiting occurrences at their exact durable deadline."""
        owners: set[str] = set()
        events: list[tuple[Reminder, Occurrence]] = []
        async with self._lock:
            candidate = dict(self._reminders)
            for reminder in self._reminders.values():
                if reminder.status is not ReminderStatus.WAITING_FOR_CONTEXT:
                    continue
                occurrence = _find_occurrence(reminder, reminder.current_occurrence_id)
                if (
                    occurrence is None
                    or occurrence.expires_at is None
                    or now < occurrence.expires_at
                ):
                    continue
                expired = occurrence.updated(
                    status=OccurrenceStatus.EXPIRED,
                    completed_at=now,
                    completion_reason="context_wait_expired",
                )
                history = _replace_occurrence(
                    list(reminder.occurrence_history), expired
                )
                if reminder.recurrence is not None:
                    updated = _advance_recurring_series(
                        reminder,
                        history,
                        resolved_due=occurrence.scheduled_due,
                        resolved_status=ReminderStatus.EXPIRED,
                        now=now,
                        after=max(now, occurrence.scheduled_due),
                    ).updated(
                        trigger_duration_waits=tuple(
                            wait
                            for wait in reminder.trigger_duration_waits
                            if wait.role != "deliver_when"
                        )
                    )
                else:
                    updated = reminder.updated(
                        status=ReminderStatus.EXPIRED,
                        due=None,
                        occurrence_history=tuple(history),
                        trigger_duration_waits=tuple(
                            wait
                            for wait in reminder.trigger_duration_waits
                            if wait.role != "deliver_when"
                        ),
                        updated_at=now,
                    )
                candidate[reminder.id] = updated
                owners.add(reminder.user_id)
                events.append((updated, expired))
            if events:
                await self._async_persist_state(candidate, self._users)
                self._reschedule(force=True)
        for reminder, occurrence in events:
            self._cancel_trigger_duration_timers(reminder.id, "deliver_when")
            self._fire_lifecycle_event(reminder, "expired", occurrence_id=occurrence.id)
        if events:
            await self._trigger_registry.async_sync(self._reminders.values())
            self._notify_changed(owners)

    async def _async_process_due(
        self, effective_now: datetime, *, recover_missed: bool = False
    ) -> None:
        """Claim and process every reminder due at the effective current time."""
        effective_now = _normalize_due(effective_now)
        await self._async_process_trigger_temporal(effective_now)
        await self._async_process_occurrence_expiry(effective_now)
        await self._async_process_escalations(effective_now)
        await self._async_process_snoozed_retries(effective_now)
        due: list[Reminder] = []
        try:
            async with self._lock:
                if self._unloaded:
                    return
                self._cancel_timer()
                eligible = [
                    reminder
                    for reminder in self._reminders.values()
                    if (
                        (
                            reminder.activation_type is ActivationType.TIME
                            and reminder.status is ReminderStatus.PENDING
                        )
                        or (
                            reminder.activation_type is ActivationType.TRIGGER
                            and reminder.status is ReminderStatus.WAITING_FOR_TRIGGER
                            and reminder.current_occurrence_id is not None
                        )
                    )
                    and reminder.due is not None
                    and reminder.due <= effective_now
                ]
                candidate = dict(self._reminders)
                missed: list[tuple[Reminder, Occurrence]] = []
                if recover_missed:
                    retained: list[Reminder] = []
                    for reminder in eligible:
                        if (
                            reminder.recurrence is None
                            or reminder.missed_occurrence_policy
                            is not MissedOccurrencePolicy.SKIP
                            or reminder.due == effective_now
                        ):
                            retained.append(reminder)
                            continue
                        occurrence = _find_occurrence(
                            reminder, reminder.current_occurrence_id
                        )
                        if occurrence is None:
                            assert reminder.due is not None
                            occurrence = _new_occurrence(
                                reminder.due,
                                scheduled_due=reminder.scheduled_due or reminder.due,
                            )
                            reminder = reminder.updated(
                                current_occurrence_id=occurrence.id,
                                occurrence_history=(
                                    *reminder.occurrence_history,
                                    occurrence,
                                ),
                            )
                        skipped = occurrence.updated(
                            status=OccurrenceStatus.SKIPPED,
                            completed_at=effective_now,
                            completion_reason="home_assistant_offline",
                        )
                        history = _replace_occurrence(
                            list(reminder.occurrence_history), skipped
                        )
                        candidate[reminder.id] = _advance_recurring_series(
                            reminder,
                            history,
                            resolved_due=occurrence.scheduled_due,
                            resolved_status=ReminderStatus.SKIPPED,
                            now=effective_now,
                            after=effective_now,
                        )
                        missed.append((reminder, skipped))
                    eligible = retained
                waiting: list[Reminder] = []
                expired: list[tuple[Reminder, Occurrence]] = []
                for reminder in eligible:
                    trigger = reminder.deliver_when
                    if trigger is None or (
                        trigger.type not in {TriggerType.EVENT, TriggerType.NAMED}
                        and not trigger.for_seconds
                        and self._trigger_registry.condition_is_currently_matching(
                            trigger
                        )
                    ):
                        due.append(reminder)
                    else:
                        occurrence = _find_occurrence(
                            reminder, reminder.current_occurrence_id
                        )
                        if occurrence is None:
                            assert reminder.due is not None
                            occurrence = _new_occurrence(
                                reminder.due,
                                scheduled_due=reminder.scheduled_due or reminder.due,
                            )
                            reminder = reminder.updated(
                                current_occurrence_id=occurrence.id,
                                occurrence_history=(
                                    *reminder.occurrence_history,
                                    occurrence,
                                ),
                            )
                        expiry_base = occurrence.scheduled_due
                        deadline = (
                            expiry_base
                            + timedelta(seconds=reminder.expires_after_seconds)
                            if expiry_base is not None
                            and reminder.expires_after_seconds is not None
                            else None
                        )
                        if deadline is not None and effective_now >= deadline:
                            expired.append((reminder, occurrence))
                            continue
                        waiting.append(reminder)
                if due or waiting or missed or expired:
                    for reminder, occurrence in expired:
                        finished = occurrence.updated(
                            status=OccurrenceStatus.EXPIRED,
                            expires_at=(
                                occurrence.scheduled_due
                                + timedelta(seconds=reminder.expires_after_seconds or 0)
                            ),
                            completed_at=effective_now,
                            completion_reason="context_wait_expired",
                        )
                        history = _replace_occurrence(
                            list(reminder.occurrence_history), finished
                        )
                        if reminder.recurrence is not None:
                            candidate[reminder.id] = _advance_recurring_series(
                                reminder,
                                history,
                                resolved_due=occurrence.scheduled_due,
                                resolved_status=ReminderStatus.EXPIRED,
                                now=effective_now,
                                after=max(effective_now, occurrence.scheduled_due),
                            )
                        else:
                            candidate[reminder.id] = reminder.updated(
                                status=ReminderStatus.EXPIRED,
                                due=None,
                                occurrence_history=tuple(history),
                                updated_at=effective_now,
                            )
                    for reminder in waiting:
                        assert reminder.due is not None
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
                            status=OccurrenceStatus.WAITING_FOR_CONTEXT,
                            context_eligible_at=effective_now,
                            expires_at=(
                                occurrence.scheduled_due
                                + timedelta(seconds=reminder.expires_after_seconds)
                                if reminder.expires_after_seconds is not None
                                else None
                            ),
                        )
                        history = _replace_occurrence(history, occurrence)
                        candidate[reminder.id] = reminder.updated(
                            status=ReminderStatus.WAITING_FOR_CONTEXT,
                            current_occurrence_id=occurrence.id,
                            occurrence_history=tuple(history),
                            snoozed_until=None,
                            updated_at=effective_now,
                        )
                    for reminder in due:
                        assert reminder.due is not None
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
            if waiting:
                await self._trigger_registry.async_sync(self._reminders.values())
            for claimed in due:
                await self._async_deliver_claimed(claimed.id, effective_now)
            for reminder, occurrence in (*missed, *expired):
                self._fire_lifecycle_event(
                    self._reminders.get(reminder.id, reminder),
                    "skipped"
                    if occurrence.status is OccurrenceStatus.SKIPPED
                    else "expired",
                    occurrence_id=occurrence.id,
                )
        finally:
            async with self._lock:
                self._reschedule(force=True)

    async def _async_process_snoozed_retries(self, effective_now: datetime) -> None:
        """Claim due retries without replacing a recurring series occurrence."""
        claims: list[tuple[str, str]] = []
        async with self._lock:
            candidate = dict(self._reminders)
            for reminder in self._reminders.values():
                if reminder.recurrence is None or reminder.paused:
                    continue
                history = list(reminder.occurrence_history)
                changed = False
                for occurrence in reminder.occurrence_history:
                    if (
                        occurrence.id == reminder.current_occurrence_id
                        or not occurrence.snoozed
                        or occurrence.status is not OccurrenceStatus.SCHEDULED
                        or occurrence.due > effective_now
                    ):
                        continue
                    history = _replace_occurrence(
                        history,
                        occurrence.updated(status=OccurrenceStatus.DELIVERING),
                    )
                    claims.append((reminder.id, occurrence.id))
                    changed = True
                if changed:
                    candidate[reminder.id] = reminder.updated(
                        occurrence_history=tuple(history), updated_at=effective_now
                    )
            if claims:
                await self._async_persist_state(candidate, self._users)
        for reminder_id, occurrence_id in claims:
            await self._async_deliver_snoozed_retry(
                reminder_id, occurrence_id, effective_now
            )

    async def _async_deliver_snoozed_retry(
        self, reminder_id: str, occurrence_id: str, effective_now: datetime
    ) -> None:
        """Deliver one durably claimed retry while leaving the series anchor alone."""
        async with self._lock:
            claimed = self._reminders.get(reminder_id)
            if claimed is None:
                return
            occurrence = _find_occurrence(claimed, occurrence_id)
            if (
                occurrence is None
                or occurrence.status is not OccurrenceStatus.DELIVERING
            ):
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
            token = occurrence.notification_action_token
            if token is None:
                token = secrets.token_urlsafe(24)
                occurrence = occurrence.updated(notification_action_token=token)
                history = _replace_occurrence(
                    list(claimed.occurrence_history), occurrence
                )
                claimed = claimed.updated(occurrence_history=tuple(history))
                candidate = dict(self._reminders)
                candidate[reminder_id] = claimed
                await self._async_persist_state(candidate, self._users)
            delivery_reminder = claimed.updated(
                notification_actions=_notification_actions(
                    token,
                    ack_required,
                    claimed.allow_manual_completion,
                    claimed.external_actions,
                    occurrence.external_action_id,
                )
            )
        result = await self._dispatcher.async_deliver(
            delivery_reminder, delivery_policy
        )
        stale_delivery = False
        async with self._lock:
            current = self._reminders.get(reminder_id)
            occurrence = (
                _find_occurrence(current, occurrence_id)
                if current is not None
                else None
            )
            if (
                current is None
                or occurrence is None
                or occurrence.status is not OccurrenceStatus.DELIVERING
            ):
                stale_delivery = True
            else:
                status = (
                    OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                    if result.succeeded and ack_required
                    else (
                        OccurrenceStatus.DELIVERED
                        if result.succeeded
                        else OccurrenceStatus.FAILED
                    )
                )
                finished = occurrence.updated(
                    status=status,
                    delivered_at=effective_now if result.succeeded else None,
                    succeeded_channels=result.succeeded,
                    failed_channels=result.failed_channels,
                    delivery_errors=result.errors,
                    suppressed_channels=suppressed,
                    acknowledgement_required=ack_required,
                    next_escalation_at=(
                        effective_now
                        + timedelta(minutes=current.escalation.initial_delay_minutes)
                        if result.succeeded and ack_required and current.escalation
                        else None
                    ),
                )
                history = _replace_occurrence(
                    list(current.occurrence_history), finished
                )
                updated = _prune_history(
                    current.updated(
                        occurrence_history=tuple(history), updated_at=effective_now
                    ),
                    preferences,
                    effective_now,
                )
                candidate = dict(self._reminders)
                candidate[reminder_id] = updated
                await self._async_persist_state(candidate, self._users)
        if stale_delivery:
            await async_finalize_persistent_delivery_cleanup(
                self._hass, reminder_id, occurrence_id
            )
            return
        self._notify_changed({claimed.user_id})

    async def _async_process_escalations(self, effective_now: datetime) -> None:
        """Claim due escalation attempts durably and redeliver without polling."""
        claims: list[tuple[str, str, int, Reminder, DeliveryPolicy]] = []
        async with self._lock:
            candidate = dict(self._reminders)
            for reminder in self._reminders.values():
                escalation = reminder.escalation
                if escalation is None or reminder.paused:
                    continue
                preferences = self._users.get(reminder.user_id, UserPreferences())
                policy = reminder.delivery_policy or preferences.default_delivery_policy
                delivery_policy, suppressed = self._delivery_plan(
                    reminder, policy, preferences, effective_now
                )
                history = list(reminder.occurrence_history)
                changed = False
                for occurrence in reminder.occurrence_history:
                    if (
                        occurrence.status
                        is not OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                        or occurrence.next_escalation_at is None
                        or occurrence.next_escalation_at > effective_now
                        or occurrence.escalation_attempt_count
                        >= escalation.max_attempts
                    ):
                        continue
                    number = occurrence.escalation_attempt_count + 1
                    placeholder = EscalationAttempt(
                        number,
                        effective_now,
                        suppressed_channels=suppressed,
                        in_flight=True,
                    )
                    claimed_occurrence = occurrence.updated(
                        escalation_attempt_count=number,
                        escalation_history=(
                            *occurrence.escalation_history,
                            placeholder,
                        ),
                    )
                    history = _replace_occurrence(history, claimed_occurrence)
                    delivery_reminder = reminder.updated(
                        notification_actions=_notification_actions(
                            occurrence.notification_action_token,
                            True,
                            reminder.allow_manual_completion,
                            reminder.external_actions,
                            occurrence.external_action_id,
                        )
                    )
                    claims.append(
                        (
                            reminder.id,
                            occurrence.id,
                            number,
                            delivery_reminder,
                            delivery_policy,
                        )
                    )
                    changed = True
                if changed:
                    candidate[reminder.id] = reminder.updated(
                        occurrence_history=tuple(history), updated_at=effective_now
                    )
            if claims:
                await self._async_persist_state(candidate, self._users)
        for reminder_id, occurrence_id, number, delivery_reminder, policy in claims:
            result = await self._dispatcher.async_deliver(delivery_reminder, policy)
            finalize_cleanup = False
            async with self._lock:
                current = self._reminders.get(reminder_id)
                if current is None:
                    finalize_cleanup = True
                else:
                    found_occurrence = _find_occurrence(current, occurrence_id)
                    if found_occurrence is None:
                        finalize_cleanup = True
                    else:
                        finalize_cleanup = (
                            found_occurrence.status
                            is not OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                        )
                        attempts = list(found_occurrence.escalation_history)
                        completed_claim = False
                        for index, attempt in enumerate(attempts):
                            if attempt.number == number and attempt.in_flight:
                                attempts[index] = EscalationAttempt(
                                    number=number,
                                    attempted_at=attempt.attempted_at,
                                    succeeded_channels=result.succeeded,
                                    failed_channels=result.failed_channels,
                                    delivery_errors=result.errors,
                                    suppressed_channels=attempt.suppressed_channels,
                                    in_flight=False,
                                )
                                completed_claim = True
                                break
                        if completed_claim:
                            current_escalation = current.escalation
                            next_at = (
                                effective_now
                                + timedelta(minutes=current_escalation.repeat_minutes)
                                if found_occurrence.status
                                is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                                and current_escalation is not None
                                and number < current_escalation.max_attempts
                                else None
                            )
                            updated_occurrence = found_occurrence.updated(
                                escalation_history=tuple(attempts),
                                next_escalation_at=next_at,
                            )
                            history = _replace_occurrence(
                                list(current.occurrence_history), updated_occurrence
                            )
                            candidate = dict(self._reminders)
                            candidate[reminder_id] = current.updated(
                                occurrence_history=tuple(history),
                                updated_at=effective_now,
                            )
                            await self._async_persist_state(candidate, self._users)
            if finalize_cleanup:
                await async_finalize_persistent_delivery_cleanup(
                    self._hass, reminder_id, occurrence_id
                )
        if claims:
            self._notify_changed({item.user_id for item in self._reminders.values()})

    async def _async_process_trigger_temporal(self, now: datetime) -> None:
        """Apply exact availability, snooze, and expiry boundaries."""
        changed = False
        owners: set[str] = set()
        async with self._lock:
            proposed = dict(self._reminders)
            for reminder in self._reminders.values():
                if reminder.activation_type is not ActivationType.TRIGGER:
                    continue
                updated = reminder
                if reminder.expires_at is not None and now >= reminder.expires_at:
                    if reminder.status not in {
                        ReminderStatus.EXPIRED,
                        ReminderStatus.COMPLETED,
                        ReminderStatus.CANCELLED,
                    }:
                        updated = reminder.updated(
                            status=ReminderStatus.EXPIRED, updated_at=now
                        )
                else:
                    if (
                        reminder.status is ReminderStatus.INACTIVE_BEFORE_AVAILABLE_FROM
                        and (
                            reminder.available_from is None
                            or now >= reminder.available_from
                        )
                    ):
                        updated = updated.updated(
                            status=ReminderStatus.WAITING_FOR_TRIGGER,
                            updated_at=now,
                        )
                    if (
                        updated.snoozed_until is not None
                        and now >= updated.snoozed_until
                    ):
                        updated = updated.updated(snoozed_until=None, updated_at=now)
                if updated != reminder:
                    proposed[reminder.id] = updated
                    changed = True
                    owners.add(reminder.user_id)
            if changed:
                await self._async_persist_state(proposed, self._users)
        if changed:
            await self._trigger_registry.async_sync(self._reminders.values())
            self._notify_changed(owners)

    async def _async_deliver_claimed(
        self, reminder_id: str, effective_now: datetime
    ) -> None:
        async with self._lock:
            claimed = self._reminders.get(reminder_id)
            if (
                self._unloaded
                or claimed is None
                or claimed.status is not ReminderStatus.DELIVERING
            ):
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
            occurrence = _find_occurrence(claimed, claimed.current_occurrence_id)
            if occurrence is None:
                raise ReminderValidationError("Active occurrence history is missing")
            if occurrence.notification_action_token is None:
                occurrence = occurrence.updated(
                    notification_action_token=secrets.token_urlsafe(24)
                )
                history = _replace_occurrence(
                    list(claimed.occurrence_history), occurrence
                )
                claimed = claimed.updated(occurrence_history=tuple(history))
                candidate = dict(self._reminders)
                candidate[reminder_id] = claimed
                await self._async_persist_state(candidate, self._users)
            delivery_reminder = claimed.updated(
                notification_actions=_notification_actions(
                    occurrence.notification_action_token,
                    ack_required,
                    claimed.allow_manual_completion,
                    claimed.external_actions,
                    occurrence.external_action_id,
                )
            )
        result = await self._dispatcher.async_deliver(
            delivery_reminder, delivery_policy
        )
        stale_delivery = False
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if (
                self._unloaded
                or current is None
                or current.status is not ReminderStatus.DELIVERING
            ):
                stale_delivery = True
                occurrence = None
            else:
                occurrence = _find_occurrence(current, current.current_occurrence_id)
                if occurrence is None:
                    raise ReminderValidationError(
                        "Active occurrence history is missing"
                    )
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
                    next_escalation_at=(
                        effective_now
                        + timedelta(minutes=current.escalation.initial_delay_minutes)
                        if result.succeeded and ack_required and current.escalation
                        else None
                    ),
                )
                history = _replace_occurrence(
                    list(current.occurrence_history), finished
                )
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
                                effective_now
                                if result.succeeded
                                else current.delivered_at
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
                                effective_now
                                if result.succeeded
                                else current.delivered_at
                            ),
                            delivery_errors=result.errors,
                            occurrence_history=tuple(history),
                            updated_at=effective_now,
                        )
                elif current.activation_type is ActivationType.TRIGGER:
                    if current.repeat_policy is TriggerRepeatPolicy.ONCE:
                        if (
                            occurrence_status
                            is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                        ):
                            status = ReminderStatus.AWAITING_ACKNOWLEDGEMENT
                        elif occurrence_status is OccurrenceStatus.FAILED:
                            status = ReminderStatus.FAILED
                        else:
                            status = ReminderStatus.COMPLETED
                    elif (
                        current.repeat_policy
                        is TriggerRepeatPolicy.REARM_AFTER_ACKNOWLEDGEMENT
                        and occurrence_status
                        is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                    ):
                        status = ReminderStatus.AWAITING_ACKNOWLEDGEMENT
                    else:
                        status = _trigger_waiting_status(
                            effective_now, current.available_from, current.expires_at
                        )
                    updated = current.updated(
                        status=status,
                        due=None,
                        last_occurrence_due=effective_now,
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
                self._reschedule(force=True)
        if stale_delivery:
            await async_finalize_persistent_delivery_cleanup(
                self._hass, reminder_id, claimed.current_occurrence_id
            )
            return
        self._notify_changed({claimed.user_id})
        await self._trigger_registry.async_sync(self._reminders.values())

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

        notify_targets = policy.notify_targets
        mobile_app_services = policy.mobile_app_services
        voice_targets = policy.voice_targets
        defaults = preferences.default_delivery_policy
        if "phone" in channels and not (notify_targets or mobile_app_services):
            notify_targets = defaults.notify_targets
            mobile_app_services = defaults.mobile_app_services
        if "voice" in channels and not voice_targets:
            voice_targets = defaults.voice_targets

        return (
            DeliveryPolicy(
                channels=tuple(channels),
                notify_targets=notify_targets,
                mobile_app_services=mobile_app_services,
                voice_targets=voice_targets,
            ),
            suppressed,
        )

    def _reschedule_if_needed(self, candidate: datetime) -> None:
        if self._scheduled_for is None or candidate < self._scheduled_for:
            self._reschedule(force=True)

    def _reschedule(self, *, force: bool) -> None:
        now = dt_util.utcnow()
        candidates: list[datetime] = []
        for reminder in self._reminders.values():
            candidates.extend(
                occurrence.expires_at
                for occurrence in reminder.occurrence_history
                if not reminder.paused
                and occurrence.status is OccurrenceStatus.WAITING_FOR_CONTEXT
                and occurrence.expires_at is not None
                and occurrence.expires_at > now
            )
            candidates.extend(
                occurrence.next_escalation_at
                for occurrence in reminder.occurrence_history
                if not reminder.paused
                and occurrence.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
                and occurrence.next_escalation_at is not None
                and occurrence.next_escalation_at > now
            )
            candidates.extend(
                occurrence.due
                for occurrence in reminder.occurrence_history
                if reminder.recurrence is not None
                and not reminder.paused
                and occurrence.id != reminder.current_occurrence_id
                and occurrence.snoozed
                and occurrence.status is OccurrenceStatus.SCHEDULED
                and occurrence.due > now
            )
            if (
                reminder.activation_type is ActivationType.TIME
                and reminder.status is ReminderStatus.PENDING
                and reminder.due is not None
            ):
                candidates.append(reminder.due)
                continue
            if reminder.activation_type is not ActivationType.TRIGGER:
                continue
            if reminder.available_from is not None and reminder.available_from > now:
                candidates.append(reminder.available_from)
            if reminder.expires_at is not None and reminder.expires_at > now:
                candidates.append(reminder.expires_at)
            if reminder.snoozed_until is not None and reminder.snoozed_until > now:
                candidates.append(reminder.snoozed_until)
        next_due = min(candidates, default=None)
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
        await async_prepare_persistent_cleanup(
            self._hass, self._reminders, reminder_copy
        )
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

    def _fire_lifecycle_event(
        self,
        reminder: Reminder,
        action: str,
        *,
        occurrence_id: str | None = None,
        external_action_id: str | None = None,
    ) -> None:
        """Publish a durable lifecycle transition without reminder content."""
        bus = getattr(self._hass, "bus", None)
        fire = getattr(bus, "async_fire", None)
        if not callable(fire):
            return
        payload = {
            "reminder_id": reminder.id,
            "occurrence_id": occurrence_id,
            "user_id": reminder.user_id,
            "source": reminder.source,
            "source_id": reminder.source_id,
            "source_event": reminder.source_event,
            "action": action,
        }
        if external_action_id is not None:
            payload["external_action_id"] = external_action_id
        fire(LIFECYCLE_EVENT, payload)

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


def _normalize_optional_time(value: datetime | None) -> datetime | None:
    return _normalize_due(value) if value is not None else None


def _validate_source_metadata(
    source: str | None, source_id: str | None, source_event: str | None
) -> tuple[str | None, str | None, str | None]:
    """Normalize bounded external correlation metadata."""
    values = (
        ("source", source, 128),
        ("source_id", source_id, 255),
        ("source_event", source_event, 128),
    )
    normalized: list[str | None] = []
    for name, value, maximum in values:
        if value is None:
            normalized.append(None)
            continue
        if not isinstance(value, str) or not (item := value.strip()):
            raise ReminderValidationError(f"{name} must be a non-empty string")
        if len(item) > maximum:
            raise ReminderValidationError(
                f"{name} must be at most {maximum} characters"
            )
        normalized.append(item)
    return tuple(normalized)  # type: ignore[return-value]


def _validate_external_actions(
    actions: list[dict[str, str]] | tuple[dict[str, str], ...],
    managed_externally: bool,
) -> tuple[dict[str, str], ...]:
    """Validate inert, bounded action identifiers and labels."""
    if actions and not managed_externally:
        raise ReminderValidationError(
            "External actions are only allowed on externally managed reminders"
        )
    if not isinstance(actions, (list, tuple)) or len(actions) > 5:
        raise ReminderValidationError("External actions must contain at most 5 items")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in actions:
        if not isinstance(item, dict) or set(item) != {"id", "label"}:
            raise ReminderValidationError("External actions require only id and label")
        action_id = item["id"].strip() if isinstance(item["id"], str) else ""
        label = item["label"].strip() if isinstance(item["label"], str) else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", action_id):
            raise ReminderValidationError(
                "External action IDs must be 1-64 letters, numbers, "
                "underscores, or hyphens"
            )
        if not label or len(label) > 64:
            raise ReminderValidationError(
                "External action labels must be 1-64 characters"
            )
        if action_id in seen:
            raise ReminderValidationError("External action IDs must be unique")
        seen.add(action_id)
        result.append({"id": action_id, "label": label})
    return tuple(result)


def _validate_trigger_options(
    cooldown_seconds: int,
    available_from: datetime | None,
    expires_at: datetime | None,
) -> None:
    if cooldown_seconds < 0 or cooldown_seconds > 31_536_000:
        raise ReminderValidationError("Cooldown must be between 0 and 31536000 seconds")
    if (
        available_from is not None
        and expires_at is not None
        and available_from >= expires_at
    ):
        raise ReminderValidationError("Expiry must be after available_from")


def _validate_expiry_window(value: int | None) -> int | None:
    if value is None:
        return None
    seconds = int(value)
    if seconds < 60 or seconds > 31_536_000:
        raise ReminderValidationError(
            "Stop-waiting duration must be between 60 and 31536000 seconds"
        )
    return seconds


def _trigger_waiting_status(
    now: datetime, available_from: datetime | None, expires_at: datetime | None
) -> ReminderStatus:
    if expires_at is not None and now >= expires_at:
        return ReminderStatus.EXPIRED
    if available_from is not None and now < available_from:
        return ReminderStatus.INACTIVE_BEFORE_AVAILABLE_FROM
    return ReminderStatus.WAITING_FOR_TRIGGER


def _sanitise_trigger_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep bounded primitive trigger context; never persist full HA events."""
    result: dict[str, Any] = {}
    for key, value in list(context.items())[:16]:
        safe_key = str(key)[:64]
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[safe_key] = value[:256] if isinstance(value, str) else value
        elif isinstance(value, dict):
            result[safe_key] = {
                str(child_key)[:64]: (
                    child_value[:256] if isinstance(child_value, str) else child_value
                )
                for child_key, child_value in list(value.items())[:16]
                if isinstance(child_value, (str, int, float, bool))
                or child_value is None
            }
    return result


def _trigger_for_duration_role(
    reminder: Reminder, role: str
) -> TriggerDefinition | None:
    return {
        "activation": reminder.trigger,
        "deliver_when": reminder.deliver_when,
        "complete_when": reminder.complete_when,
    }.get(role)


def _trigger_duration_wait(reminder: Reminder, role: str) -> TriggerDurationWait | None:
    return next(
        (item for item in reminder.trigger_duration_waits if item.role == role), None
    )


def _replace_trigger_duration_wait(
    reminder: Reminder, role: str, wait: TriggerDurationWait | None
) -> Reminder:
    waits = [item for item in reminder.trigger_duration_waits if item.role != role]
    if wait is not None:
        waits.append(wait)
    return reminder.updated(trigger_duration_waits=tuple(waits))


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
        not service.startswith("notify.mobile_app_") or service.count(".") != 1
        for service in policy.mobile_app_services
    ):
        raise ReminderValidationError(
            "Actionable phone targets must be registered notify.mobile_app services"
        )
    if any(
        not target.startswith("assist_satellite.") for target in policy.voice_targets
    ):
        raise ReminderValidationError("Voice targets must be Assist satellites")
    if (
        "phone" in policy.channels
        and not policy.notify_targets
        and not policy.mobile_app_services
    ):
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


def _coerce_trigger(
    value: TriggerDefinition | dict[str, Any] | None,
) -> TriggerDefinition | None:
    if value is None or isinstance(value, TriggerDefinition):
        return value
    return TriggerDefinition.from_dict(value)


def _coerce_escalation(
    value: EscalationPolicy | dict[str, Any] | None,
) -> EscalationPolicy | None:
    if value is None:
        return None
    policy = (
        value
        if isinstance(value, EscalationPolicy)
        else EscalationPolicy.from_dict(value)
    )
    if not 1 <= policy.initial_delay_minutes <= 10080:
        raise ReminderValidationError(
            "Escalation initial delay must be between 1 and 10080 minutes"
        )
    if not 1 <= policy.repeat_minutes <= 10080:
        raise ReminderValidationError(
            "Escalation repeat must be between 1 and 10080 minutes"
        )
    if not 1 <= policy.max_attempts <= 20:
        raise ReminderValidationError(
            "Escalation max attempts must be between 1 and 20"
        )
    return policy


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
    return Occurrence(
        str(uuid4()),
        scheduled_due or due,
        due,
        notification_action_token=secrets.token_urlsafe(24),
    )


def _notification_actions(
    token: str | None,
    acknowledgement_required: bool,
    allow_manual_completion: bool = False,
    external_actions: tuple[dict[str, str], ...] = (),
    selected_external_action_id: str | None = None,
) -> tuple[dict[str, str], ...]:
    if not token:
        return ()
    actions: list[dict[str, str]] = []
    if allow_manual_completion:
        actions.append(
            {"action": f"{MOBILE_ACTION_PREFIX}{token}:DONE", "title": "Done"}
        )
    for item in external_actions:
        if item["id"] == selected_external_action_id:
            continue
        actions.append(
            {
                "action": f"{MOBILE_ACTION_PREFIX}{token}:EXTERNAL_{item['id']}",
                "title": item["label"],
            }
        )
    if acknowledgement_required:
        actions.append(
            {"action": f"{MOBILE_ACTION_PREFIX}{token}:DISMISS", "title": "Dismiss"}
        )
    actions.extend(
        (
            {
                "action": f"{MOBILE_ACTION_PREFIX}{token}:SNOOZE_10",
                "title": "Snooze 10 minutes",
            },
            {
                "action": f"{MOBILE_ACTION_PREFIX}{token}:SNOOZE_60",
                "title": "Snooze 1 hour",
            },
        )
    )
    return tuple(actions)


def _find_occurrence(
    reminder: Reminder, occurrence_id: str | None
) -> Occurrence | None:
    if occurrence_id is None:
        return None
    return next(
        (item for item in reminder.occurrence_history if item.id == occurrence_id),
        None,
    )


def _automatic_completion_occurrence(reminder: Reminder) -> Occurrence | None:
    """Prefer the earliest active retry, then older awaiting work, then current."""
    snoozed_retry_statuses = {
        OccurrenceStatus.SCHEDULED,
        OccurrenceStatus.DELIVERING,
        OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
    }
    snoozed_retries = [
        occurrence
        for occurrence in reminder.occurrence_history
        if occurrence.id != reminder.current_occurrence_id
        and occurrence.snoozed
        and occurrence.status in snoozed_retry_statuses
    ]
    if snoozed_retries:
        return min(snoozed_retries, key=_completion_order)
    older_awaiting = [
        occurrence
        for occurrence in reminder.occurrence_history
        if occurrence.id != reminder.current_occurrence_id
        and occurrence.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
    ]
    if older_awaiting:
        return min(older_awaiting, key=_completion_order)
    return _find_occurrence(reminder, reminder.current_occurrence_id)


def _completion_order(occurrence: Occurrence) -> tuple[datetime, datetime, str]:
    """Order competing historical completion targets without history-order bias."""
    return occurrence.due, occurrence.scheduled_due, occurrence.id


def _replace_occurrence(
    values: list[Occurrence], replacement: Occurrence
) -> list[Occurrence]:
    return [replacement if item.id == replacement.id else item for item in values]


def _rotate_notification_action_tokens(values: list[Occurrence]) -> list[Occurrence]:
    """Invalidate capabilities exposed to a reminder's previous owner."""
    return [
        item.updated(notification_action_token=secrets.token_urlsafe(24))
        if item.notification_action_token
        else item
        for item in values
    ]


def _reminder_status(status: OccurrenceStatus) -> ReminderStatus:
    return {
        OccurrenceStatus.DELIVERED: ReminderStatus.DELIVERED,
        OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT: (
            ReminderStatus.AWAITING_ACKNOWLEDGEMENT
        ),
        OccurrenceStatus.ACKNOWLEDGED: ReminderStatus.ACKNOWLEDGED,
        OccurrenceStatus.COMPLETED: ReminderStatus.COMPLETED,
        OccurrenceStatus.FAILED: ReminderStatus.FAILED,
        OccurrenceStatus.CANCELLED: ReminderStatus.CANCELLED,
        OccurrenceStatus.SCHEDULED: ReminderStatus.PENDING,
        OccurrenceStatus.WAITING_FOR_CONTEXT: ReminderStatus.WAITING_FOR_CONTEXT,
        OccurrenceStatus.DELIVERING: ReminderStatus.DELIVERING,
        OccurrenceStatus.SKIPPED: ReminderStatus.SKIPPED,
        OccurrenceStatus.EXPIRED: ReminderStatus.EXPIRED,
    }[status]


def _advance_recurring_series(
    reminder: Reminder,
    history: list[Occurrence],
    *,
    resolved_due: datetime,
    resolved_status: ReminderStatus,
    now: datetime,
    after: datetime,
) -> Reminder:
    """Advance one resolved series occurrence without changing its anchored rule."""
    if reminder.recurrence is None:
        raise ReminderValidationError("Reminder is not recurring")
    next_due = next_due_after(reminder.recurrence, after)
    common = {
        "last_occurrence_due": resolved_due,
        "last_occurrence_status": resolved_status,
        "occurrence_history": tuple(history),
        "updated_at": now,
    }
    if next_due is None:
        return reminder.updated(
            status=resolved_status,
            due=None,
            scheduled_due=None,
            current_occurrence_id=None,
            **common,
        )
    occurrence = _new_occurrence(next_due)
    history.append(occurrence)
    return reminder.updated(
        status=ReminderStatus.PENDING,
        due=next_due,
        scheduled_due=next_due,
        current_occurrence_id=occurrence.id,
        current_occurrence_number=occurrence_number(reminder.recurrence, next_due),
        occurrence_history=tuple(history),
        last_occurrence_due=resolved_due,
        last_occurrence_status=resolved_status,
        updated_at=now,
    )


def _prune_history(
    reminder: Reminder, preferences: UserPreferences, now: datetime
) -> Reminder:
    cutoff = now - timedelta(days=preferences.history_retention_days)
    protected = {
        item.id
        for item in reminder.occurrence_history
        if item.id == reminder.current_occurrence_id
        or item.status
        in {
            OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
            OccurrenceStatus.WAITING_FOR_CONTEXT,
        }
        or (
            item.snoozed
            and item.status in {OccurrenceStatus.SCHEDULED, OccurrenceStatus.DELIVERING}
        )
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
