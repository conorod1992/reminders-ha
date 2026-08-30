"""Reminder manager extensions for Home Assistant-native automation rules."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import Context
from homeassistant.util import dt as dt_util

from .manager import (
    ReminderManager,
    ReminderValidationError,
    _advance_recurring_series,
    _automatic_completion_occurrence,
    _find_occurrence,
    _new_occurrence,
    _replace_occurrence,
    _sanitise_trigger_context,
    _trigger_waiting_status,
)
from .models import (
    ActivationType,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
    TriggerRepeatPolicy,
    WhileAwaitingAcknowledgement,
)
from .native_automation import NativeAutomationRuntime
from .recurrence import next_due_after, occurrence_number

_LOGGER = logging.getLogger(__name__)


class NativeReminderManager(ReminderManager):
    """Reminder manager with additive HA-native trigger/condition support."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._native_runtime = NativeAutomationRuntime(
            self._hass, self._async_native_trigger_callback
        )
        self._native_change_unsub: Callable[[], None] | None = self.async_subscribe(
            self._native_changed
        )

    async def async_load(self) -> None:
        """Load legacy state, then arm native rules."""
        await super().async_load()
        await self._native_runtime.async_sync(self._reminders.values())

    async def async_unload(self) -> None:
        """Unload native and legacy runtime resources."""
        if self._native_change_unsub is not None:
            self._native_change_unsub()
            self._native_change_unsub = None
        await self._native_runtime.async_unload()
        await super().async_unload()

    async def async_set_native_rules(
        self,
        reminder_id: str,
        *,
        activation_triggers: list[dict[str, Any]] | None = None,
        delivery_triggers: list[dict[str, Any]] | None = None,
        delivery_conditions: list[dict[str, Any]] | None = None,
        completion_triggers: list[dict[str, Any]] | None = None,
    ) -> Reminder:
        """Validate and atomically replace native rules on one reminder."""
        from .native_automation import (
            async_validate_native_conditions,
            async_validate_native_triggers,
        )

        values: dict[str, tuple[dict[str, Any], ...]] = {}
        if activation_triggers is not None:
            await async_validate_native_triggers(self._hass, activation_triggers)
            values["activation_triggers"] = tuple(
                dict(item) for item in activation_triggers
            )
        if delivery_triggers is not None:
            await async_validate_native_triggers(self._hass, delivery_triggers)
            values["delivery_triggers"] = tuple(
                dict(item) for item in delivery_triggers
            )
        if delivery_conditions is not None:
            await async_validate_native_conditions(self._hass, delivery_conditions)
            values["delivery_conditions"] = tuple(
                dict(item) for item in delivery_conditions
            )
        if completion_triggers is not None:
            await async_validate_native_triggers(self._hass, completion_triggers)
            values["completion_triggers"] = tuple(
                dict(item) for item in completion_triggers
            )

        async with self._lock:
            current = self._require(reminder_id)
            if current.status is ReminderStatus.DELIVERING:
                raise ReminderValidationError("Reminder is currently being delivered")
            activation = values.get("activation_triggers", current.activation_triggers)
            if current.activation_type is ActivationType.TIME and activation:
                raise ReminderValidationError(
                    "Time reminders cannot contain activation triggers"
                )
            if (
                current.activation_type is ActivationType.TRIGGER
                and current.trigger is None
                and not activation
            ):
                raise ReminderValidationError(
                    "Triggered reminder requires at least one activation trigger"
                )
            updated = current.updated(**values, updated_at=dt_util.utcnow())
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        await self._native_runtime.async_sync(self._reminders.values())
        self._notify_changed({current.user_id})
        return updated

    async def _async_process_due(
        self, effective_now: datetime, *, recover_missed: bool = False
    ) -> None:
        """Apply native delivery gates before the established due processor."""
        effective_now = effective_now.astimezone(UTC)
        candidates = [
            reminder
            for reminder in tuple(self._reminders.values())
            if reminder.activation_type is ActivationType.TIME
            and reminder.status is ReminderStatus.PENDING
            and reminder.due is not None
            and reminder.due <= effective_now
            and (reminder.delivery_triggers or reminder.delivery_conditions)
        ]
        for reminder in candidates:
            conditions_match = await self._native_runtime.async_conditions_match(
                reminder
            )
            if reminder.delivery_triggers:
                await self._async_begin_native_delivery_wait(reminder.id, effective_now)
                continue
            if conditions_match and reminder.delivery_conditions:
                continue
            if reminder.delivery_conditions and not conditions_match:
                await self._async_skip_native_condition_failure(
                    reminder.id, effective_now
                )
        await super()._async_process_due(effective_now, recover_missed=recover_missed)
        await self._native_runtime.async_sync(self._reminders.values())

    async def _async_begin_native_delivery_wait(
        self, reminder_id: str, now: datetime
    ) -> None:
        async with self._lock:
            reminder = self._reminders.get(reminder_id)
            if (
                reminder is None
                or reminder.status is not ReminderStatus.PENDING
                or reminder.due is None
                or reminder.due > now
            ):
                return
            occurrence = _find_occurrence(reminder, reminder.current_occurrence_id)
            if occurrence is None:
                occurrence = _new_occurrence(
                    reminder.due,
                    scheduled_due=reminder.scheduled_due or reminder.due,
                )
                history = [*reminder.occurrence_history, occurrence]
            else:
                history = list(reminder.occurrence_history)
            expiry = (
                occurrence.scheduled_due
                + timedelta(seconds=reminder.expires_after_seconds)
                if reminder.expires_after_seconds is not None
                else None
            )
            waiting = occurrence.updated(
                status=OccurrenceStatus.WAITING_FOR_CONTEXT,
                context_eligible_at=now,
                expires_at=expiry,
            )
            history = _replace_occurrence(history, waiting)
            updated = reminder.updated(
                status=ReminderStatus.WAITING_FOR_CONTEXT,
                current_occurrence_id=waiting.id,
                occurrence_history=tuple(history),
                updated_at=now,
            )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        if expiry is not None and expiry <= now:
            await self._async_process_occurrence_expiry(now)
        self._notify_changed({reminder.user_id})

    async def _async_skip_native_condition_failure(
        self, reminder_id: str, now: datetime
    ) -> None:
        async with self._lock:
            reminder = self._reminders.get(reminder_id)
            if reminder is None or reminder.status is not ReminderStatus.PENDING:
                return
            occurrence = _find_occurrence(reminder, reminder.current_occurrence_id)
            if (
                occurrence is None
                or occurrence.status is not OccurrenceStatus.SCHEDULED
            ):
                return
            skipped = occurrence.updated(
                status=OccurrenceStatus.SKIPPED,
                completed_at=now,
                completion_reason="conditions_not_met",
            )
            history = _replace_occurrence(list(reminder.occurrence_history), skipped)
            if reminder.recurrence is not None:
                updated = _advance_recurring_series(
                    reminder,
                    history,
                    resolved_due=occurrence.scheduled_due,
                    resolved_status=ReminderStatus.SKIPPED,
                    now=now,
                    after=max(now, occurrence.scheduled_due),
                )
            else:
                updated = reminder.updated(
                    status=ReminderStatus.SKIPPED,
                    due=None,
                    occurrence_history=tuple(history),
                    updated_at=now,
                )
            candidate = dict(self._reminders)
            candidate[reminder_id] = updated
            await self._async_persist_state(candidate, self._users)
            self._reschedule(force=True)
        self._notify_changed({reminder.user_id})
        self._fire_lifecycle_event(updated, "skipped", occurrence_id=occurrence.id)

    async def _async_native_trigger_callback(
        self,
        reminder_id: str,
        role: str,
        run_variables: dict[str, Any],
        context: Context | None,
    ) -> None:
        """Route one HA-native trigger firing to the reminder lifecycle."""
        trigger_context = _native_trigger_context(run_variables)
        cause = _native_trigger_cause(run_variables)
        if role == "activation":
            await self.async_activate_native_trigger(
                reminder_id, cause=cause, context=trigger_context
            )
        elif role == "delivery":
            reminder = await self.async_get(reminder_id)
            if await self._native_runtime.async_conditions_match(reminder):
                await self.async_activate_native_delivery(
                    reminder_id, cause=cause, context=trigger_context
                )
        elif role == "completion":
            await self.async_complete_native(
                reminder_id, cause=cause, context=trigger_context
            )
        await self._native_runtime.async_sync(self._reminders.values())

    async def async_activate_native_trigger(
        self, reminder_id: str, *, cause: str, context: dict[str, Any]
    ) -> str:
        """Claim one HA-native activation trigger hit."""
        now = dt_util.utcnow()
        owner: str | None = None
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if (
                self._unloaded
                or current is None
                or current.activation_type is not ActivationType.TRIGGER
                or not current.activation_triggers
            ):
                return "inactive"
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
                        trigger_type=context.get("trigger_type", "home_assistant"),
                        trigger_summary=context.get("description"),
                        triggered_at=now,
                        activation_cause=cause,
                        trigger_context=_sanitise_trigger_context(context),
                    )
                    claimed = current.updated(
                        status=ReminderStatus.DELIVERING,
                        current_occurrence_id=occurrence.id,
                        occurrence_history=(*current.occurrence_history, occurrence),
                        last_triggered_at=now,
                        snoozed_until=None,
                        updated_at=now,
                    )
                    candidate = dict(self._reminders)
                    candidate[reminder_id] = claimed
                    await self._async_persist_state(candidate, self._users)
                    outcome = "activated"
        if outcome == "activated":
            await self._async_deliver_claimed(reminder_id, now)
        if owner is not None:
            self._notify_changed({owner})
        return outcome

    async def async_activate_native_delivery(
        self, reminder_id: str, *, cause: str, context: dict[str, Any]
    ) -> str:
        """Claim a native context-waiting occurrence and deliver it once."""
        now = dt_util.utcnow()
        expired = False
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if (
                self._unloaded
                or current is None
                or current.status is not ReminderStatus.WAITING_FOR_CONTEXT
                or not current.delivery_triggers
            ):
                return "inactive"
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
                    trigger_type=context.get("trigger_type", "home_assistant"),
                    trigger_summary=context.get("description"),
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
        await self._async_deliver_claimed(reminder_id, now)
        return "activated"

    async def async_complete_native(
        self, reminder_id: str, *, cause: str, context: dict[str, Any]
    ) -> str:
        """Resolve an occurrence after an HA-native completion trigger."""
        now = dt_util.utcnow()
        async with self._lock:
            current = self._reminders.get(reminder_id)
            if self._unloaded or current is None or not current.completion_triggers:
                return "inactive"
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
                trigger_type=context.get("trigger_type", "home_assistant"),
                trigger_summary=context.get("description"),
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
        self._notify_changed({current.user_id})
        self._fire_lifecycle_event(
            current, "automatically_completed", occurrence_id=completed.id
        )
        return "completed"

    def _native_changed(self, _owners: frozenset[str]) -> None:
        if self._unloaded:
            return
        self._hass.create_task(
            self._native_runtime.async_sync(tuple(self._reminders.values())),
            "reminders native trigger sync",
        )


def _native_trigger_context(run_variables: dict[str, Any]) -> dict[str, Any]:
    """Keep a small privacy-conscious summary of the HA trigger payload."""
    trigger = run_variables.get("trigger")
    if not isinstance(trigger, dict):
        return {"trigger_type": "home_assistant"}
    trigger_type = trigger.get("platform") or trigger.get("trigger") or "home_assistant"
    result: dict[str, Any] = {"trigger_type": str(trigger_type)[:128]}
    for source, target in (
        ("description", "description"),
        ("id", "trigger_id"),
        ("entity_id", "entity_id"),
        ("event", "event"),
    ):
        value = trigger.get(source)
        if isinstance(value, str):
            result[target] = value[:256]
    return result


def _native_trigger_cause(run_variables: dict[str, Any]) -> str:
    trigger = run_variables.get("trigger")
    if not isinstance(trigger, dict):
        return "home_assistant_trigger"
    value = trigger.get("platform") or trigger.get("trigger")
    return f"home_assistant_{str(value)[:64]}" if value else "home_assistant_trigger"
