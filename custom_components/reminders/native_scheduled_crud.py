"""Atomic CRUD for scheduled reminders with Home Assistant-native rules."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .manager import (
    ReminderValidationError,
    _coerce_escalation,
    _find_occurrence,
    _new_occurrence,
    _normalize_due,
    _replace_occurrence,
    _rotate_notification_action_tokens,
    _validate_expiry_window,
    _validate_external_actions,
    _validate_policy,
    _validate_source_metadata,
    _validate_title,
)
from .models import (
    ActivationType,
    MissedOccurrencePolicy,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from .native_automation import (
    async_validate_native_conditions,
    async_validate_native_triggers,
)
from .native_manager import NativeReminderManager
from .recurrence import RecurrenceRule, first_due, occurrence_number


async def async_update_native_scheduled(
    manager: NativeReminderManager,
    reminder_id: str,
    *,
    expected_user_id: str | None = None,
    delivery_triggers: list[dict[str, Any]],
    delivery_conditions: list[dict[str, Any]],
    completion_triggers: list[dict[str, Any]],
    **changes: Any,
) -> Reminder:
    """Update one timed reminder and its native rules in a single store write.

    The management UI previously saved the ordinary reminder first and native
    rules second. If the second write failed, the dialog reported an error even
    though part of the edit had already been committed. This helper validates
    both halves first, builds one candidate Reminder, and persists it exactly
    once before any runtime subscriptions are changed.
    """
    await async_validate_native_triggers(manager._hass, delivery_triggers)
    await async_validate_native_conditions(manager._hass, delivery_conditions)
    await async_validate_native_triggers(manager._hass, completion_triggers)

    expiry_window_changed = "expires_after_seconds" in changes
    async with manager._lock:
        current = manager._require(reminder_id, expected_user_id=expected_user_id)
        if current.status is ReminderStatus.DELIVERING:
            raise ReminderValidationError("Reminder is currently being delivered")
        if current.activation_type is not ActivationType.TIME:
            raise ReminderValidationError(
                "Native scheduled update requires a time-based reminder"
            )

        allowed = {
            "title",
            "message",
            "due",
            "delivery_policy",
            "user_id",
            "recurrence",
            "acknowledgement_policy",
            "quiet_hours_policy",
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

        now = dt_util.utcnow()
        history = list(current.occurrence_history)
        active = _find_occurrence(current, current.current_occurrence_id)
        time_rearmed = False

        # An edit to the complete delivery-rule snapshot rearms a context wait.
        # Normalizing the active occurrence to scheduled lets the new rule set be
        # evaluated cleanly after the single commit, including when all rules are
        # removed from an already-due reminder.
        if active is not None and active.status is OccurrenceStatus.WAITING_FOR_CONTEXT:
            time_rearmed = True
            active = active.updated(
                status=OccurrenceStatus.SCHEDULED,
                context_eligible_at=None,
                expires_at=None,
            )
            history = _replace_occurrence(history, active)

        if "recurrence" in changes:
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
                    trigger_summary=None,
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
                    trigger_summary=None,
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
        else:
            changes.update(
                activation_type=ActivationType.TIME,
                trigger=None,
                trigger_summary=None,
            )

        if "escalation" in changes:
            policy = changes["escalation"]
            rewritten: list[Occurrence] = []
            for item in history:
                if item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT:
                    rewritten.append(
                        item.updated(
                            next_escalation_at=(
                                now + timedelta(minutes=policy.initial_delay_minutes)
                                if policy is not None
                                else None
                            ),
                            escalation_attempt_count=0,
                            escalation_history=(),
                        )
                    )
                else:
                    rewritten.append(item)
            history = rewritten

        if (
            "expires_after_seconds" in changes
            and active is not None
            and active.status is OccurrenceStatus.WAITING_FOR_CONTEXT
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
            "status", ReminderStatus.PENDING if time_rearmed else current.status
        )
        changes.update(
            activation_triggers=(),
            delivery_triggers=tuple(dict(item) for item in delivery_triggers),
            delivery_conditions=tuple(dict(item) for item in delivery_conditions),
            completion_triggers=tuple(dict(item) for item in completion_triggers),
            # Saving through the HA-native editor is an explicit conversion away
            # from the legacy single-rule fields. Keeping both would allow stale
            # classic rules to continue affecting delivery/completion.
            deliver_when=None,
            deliver_when_summary=None,
            complete_when=None,
            complete_when_summary=None,
            trigger_duration_waits=tuple(
                wait
                for wait in current.trigger_duration_waits
                if wait.role not in {"deliver_when", "complete_when"}
            ),
        )

        updated = current.updated(**changes, updated_at=now)
        if time_rearmed:
            updated = updated.updated(delivered_at=None, delivery_errors=())
        await manager._async_validate_security(updated)
        candidate = dict(manager._reminders)
        candidate[reminder_id] = updated
        await manager._async_persist_state(candidate, manager._users)
        manager._reschedule(force=True)

    # Runtime reconciliation happens only after durable state is committed.
    manager._cancel_trigger_duration_timers(reminder_id)
    await manager._trigger_registry.async_sync(manager._reminders.values())
    await manager._native_runtime.async_sync(manager._reminders.values())
    if expiry_window_changed:
        await manager._async_process_occurrence_expiry(dt_util.utcnow())
        updated = await manager.async_get(reminder_id)
    if not updated.immediate_evaluated:
        await manager._async_evaluate_immediate({updated.id})
    await manager._async_restore_trigger_durations({updated.id})
    if updated.due is not None and updated.due <= dt_util.utcnow():
        await manager._async_process_due(dt_util.utcnow())
    manager._notify_changed({current.user_id, updated.user_id})
    return await manager.async_get(reminder_id)
