"""CRUD helpers for reminders backed by Home Assistant-native trigger lists."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from homeassistant.util import dt as dt_util

from .manager import (
    ReminderValidationError,
    _coerce_escalation,
    _find_occurrence,
    _normalize_optional_time,
    _replace_occurrence,
    _rotate_notification_action_tokens,
    _trigger_waiting_status,
    _validate_external_actions,
    _validate_policy,
    _validate_source_metadata,
    _validate_title,
    _validate_trigger_options,
)
from .models import (
    AcknowledgementPolicy,
    ActivationType,
    DeliveryPolicy,
    EscalationPolicy,
    Occurrence,
    OccurrenceStatus,
    QuietHoursPolicy,
    Reminder,
    ReminderStatus,
    TriggerRepeatPolicy,
    WhileAwaitingAcknowledgement,
)
from .native_automation import async_validate_native_triggers
from .native_manager import NativeReminderManager

_UNSET = object()


async def async_create_native_triggered(
    manager: NativeReminderManager,
    *,
    user_id: str,
    title: str,
    activation_triggers: list[dict[str, Any]],
    message: str | None = None,
    delivery_policy: DeliveryPolicy | None = None,
    acknowledgement_policy: AcknowledgementPolicy = AcknowledgementPolicy.DEFAULT,
    quiet_hours_policy: QuietHoursPolicy = QuietHoursPolicy.RESPECT,
    repeat_policy: TriggerRepeatPolicy = TriggerRepeatPolicy.ONCE,
    while_awaiting_acknowledgement: WhileAwaitingAcknowledgement = (
        WhileAwaitingAcknowledgement.SKIP
    ),
    cooldown_seconds: int = 0,
    available_from: datetime | None = None,
    expires_at: datetime | None = None,
    trigger_description: str | None = None,
    completion_triggers: list[dict[str, Any]] | None = None,
    escalation: EscalationPolicy | dict[str, Any] | None = None,
    source: str | None = None,
    source_id: str | None = None,
    source_event: str | None = None,
    managed_externally: bool = False,
    allow_manual_completion: bool = False,
    external_actions: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
) -> Reminder:
    """Create and arm a triggered reminder using HA's native trigger model."""
    if not activation_triggers:
        raise ReminderValidationError("Choose at least one activation trigger")
    await async_validate_native_triggers(manager._hass, activation_triggers)
    if completion_triggers:
        await async_validate_native_triggers(manager._hass, completion_triggers)
    _validate_policy(delivery_policy)
    _validate_title(title)
    available = _normalize_optional_time(available_from)
    expiry = _normalize_optional_time(expires_at)
    _validate_trigger_options(cooldown_seconds, available, expiry)
    escalation_policy = _coerce_escalation(escalation)
    source, source_id, source_event = _validate_source_metadata(
        source, source_id, source_event
    )
    source_actions = _validate_external_actions(external_actions, managed_externally)
    now = dt_util.utcnow()
    reminder = Reminder(
        id=str(uuid4()),
        user_id=user_id,
        title=title.strip(),
        message=message.strip() if message else None,
        due=None,
        created_at=now,
        updated_at=now,
        status=_trigger_waiting_status(now, available, expiry),
        delivery_policy=delivery_policy,
        acknowledgement_policy=acknowledgement_policy,
        quiet_hours_policy=quiet_hours_policy,
        activation_type=ActivationType.TRIGGER,
        trigger=None,
        trigger_summary=_native_summary(activation_triggers),
        trigger_description=(
            trigger_description.strip() if trigger_description else None
        ),
        activation_triggers=tuple(dict(item) for item in activation_triggers),
        repeat_policy=repeat_policy,
        fire_if_already_matching=False,
        while_awaiting_acknowledgement=while_awaiting_acknowledgement,
        cooldown_seconds=cooldown_seconds,
        available_from=available,
        expires_at=expiry,
        immediate_evaluated=True,
        completion_triggers=tuple(dict(item) for item in (completion_triggers or ())),
        escalation=escalation_policy,
        source=source,
        source_id=source_id,
        source_event=source_event,
        managed_externally=managed_externally,
        allow_manual_completion=allow_manual_completion,
        external_actions=source_actions,
    )
    await manager._async_add(reminder)
    await manager._native_runtime.async_sync(manager._reminders.values())
    manager._notify_changed({user_id})
    return await manager.async_get(reminder.id)


async def async_update_native_triggered(
    manager: NativeReminderManager,
    reminder_id: str,
    *,
    expected_user_id: str | None = None,
    activation_triggers: list[dict[str, Any]],
    title: str | None = None,
    message: str | object | None = _UNSET,
    delivery_policy: DeliveryPolicy | object | None = _UNSET,
    user_id: str | None = None,
    acknowledgement_policy: AcknowledgementPolicy | None = None,
    quiet_hours_policy: QuietHoursPolicy | None = None,
    repeat_policy: TriggerRepeatPolicy | None = None,
    while_awaiting_acknowledgement: WhileAwaitingAcknowledgement | None = None,
    cooldown_seconds: int | None = None,
    available_from: datetime | object | None = _UNSET,
    expires_at: datetime | object | None = _UNSET,
    trigger_description: str | object | None = _UNSET,
    completion_triggers: list[dict[str, Any]] | None = None,
    escalation: EscalationPolicy | dict[str, Any] | object | None = _UNSET,
    managed_externally: bool | None = None,
    allow_manual_completion: bool | None = None,
    external_actions: list[dict[str, str]] | tuple[dict[str, str], ...] | None = None,
) -> Reminder:
    """Update a native triggered reminder without legacy trigger CRUD."""
    if not activation_triggers:
        raise ReminderValidationError("Choose at least one activation trigger")
    await async_validate_native_triggers(manager._hass, activation_triggers)
    if completion_triggers is not None:
        await async_validate_native_triggers(manager._hass, completion_triggers)

    async with manager._lock:
        current = manager._require(reminder_id, expected_user_id=expected_user_id)
        if current.status is ReminderStatus.DELIVERING:
            raise ReminderValidationError("Reminder is currently being delivered")
        if title is not None:
            title = title.strip()
            _validate_title(title)
        policy = (
            current.delivery_policy if delivery_policy is _UNSET else delivery_policy
        )
        if policy is not None and not isinstance(policy, DeliveryPolicy):
            raise ReminderValidationError("Delivery policy is invalid")
        _validate_policy(policy)
        available = (
            current.available_from
            if available_from is _UNSET
            else _normalize_optional_time(available_from)  # type: ignore[arg-type]
        )
        expiry = (
            current.expires_at
            if expires_at is _UNSET
            else _normalize_optional_time(expires_at)  # type: ignore[arg-type]
        )
        cooldown = (
            current.cooldown_seconds if cooldown_seconds is None else cooldown_seconds
        )
        _validate_trigger_options(cooldown, available, expiry)
        next_escalation = (
            current.escalation
            if escalation is _UNSET
            else _coerce_escalation(escalation)  # type: ignore[arg-type]
        )
        next_managed = (
            current.managed_externally
            if managed_externally is None
            else managed_externally
        )
        next_actions = _validate_external_actions(
            current.external_actions if external_actions is None else external_actions,
            next_managed,
        )
        now = dt_util.utcnow()
        history = list(current.occurrence_history)
        active = _find_occurrence(current, current.current_occurrence_id)
        rearm = (
            current.activation_type is not ActivationType.TRIGGER
            or current.trigger is not None
            or tuple(dict(item) for item in activation_triggers)
            != current.activation_triggers
            or (
                repeat_policy is not None and repeat_policy is not current.repeat_policy
            )
            or available != current.available_from
            or expiry != current.expires_at
        )
        if (
            rearm
            and active is not None
            and active.status
            in {
                OccurrenceStatus.SCHEDULED,
                OccurrenceStatus.WAITING_FOR_CONTEXT,
            }
        ):
            history = _replace_occurrence(
                history,
                active.updated(
                    status=OccurrenceStatus.CANCELLED,
                    completed_at=now,
                    completion_reason="trigger_configuration_changed",
                ),
            )
        if next_escalation != current.escalation:
            rewritten: list[Occurrence] = []
            for item in history:
                if item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT:
                    rewritten.append(
                        item.updated(
                            next_escalation_at=(
                                now
                                + timedelta(
                                    minutes=next_escalation.initial_delay_minutes
                                )
                                if next_escalation is not None
                                else None
                            ),
                            escalation_attempt_count=0,
                            escalation_history=(),
                        )
                    )
                else:
                    rewritten.append(item)
            history = rewritten
        if user_id is not None and user_id != current.user_id:
            history = _rotate_notification_action_tokens(history)
        updates: dict[str, Any] = {
            "activation_type": ActivationType.TRIGGER,
            "trigger": None,
            "trigger_summary": _native_summary(activation_triggers),
            "activation_triggers": tuple(dict(item) for item in activation_triggers),
            "due": None,
            "recurrence": None,
            "scheduled_due": None,
            "paused": False,
            "paused_at": None,
            "delivery_policy": policy,
            "available_from": available,
            "expires_at": expiry,
            "cooldown_seconds": cooldown,
            "escalation": next_escalation,
            "managed_externally": next_managed,
            "external_actions": next_actions,
            "occurrence_history": tuple(history),
            "updated_at": now,
            "immediate_evaluated": True,
            "fire_if_already_matching": False,
        }
        if rearm:
            updates.update(
                status=_trigger_waiting_status(now, available, expiry),
                current_occurrence_id=None,
                trigger_duration_waits=tuple(
                    wait
                    for wait in current.trigger_duration_waits
                    if wait.role != "activation"
                ),
            )
        if title is not None:
            updates["title"] = title
        if message is not _UNSET:
            updates["message"] = str(message).strip() if message else None
        if user_id is not None:
            updates["user_id"] = user_id
        if acknowledgement_policy is not None:
            updates["acknowledgement_policy"] = acknowledgement_policy
        if quiet_hours_policy is not None:
            updates["quiet_hours_policy"] = quiet_hours_policy
        if repeat_policy is not None:
            updates["repeat_policy"] = repeat_policy
        if while_awaiting_acknowledgement is not None:
            updates["while_awaiting_acknowledgement"] = while_awaiting_acknowledgement
        if trigger_description is not _UNSET:
            updates["trigger_description"] = (
                str(trigger_description).strip() if trigger_description else None
            )
        if completion_triggers is not None:
            updates["completion_triggers"] = tuple(
                dict(item) for item in completion_triggers
            )
            updates["complete_when"] = None
            updates["complete_when_summary"] = None
            updates["trigger_duration_waits"] = tuple(
                wait
                for wait in updates.get(
                    "trigger_duration_waits", current.trigger_duration_waits
                )
                if wait.role != "complete_when"
            )
        if allow_manual_completion is not None:
            updates["allow_manual_completion"] = allow_manual_completion
        updated = current.updated(**updates)
        await manager._async_validate_security(updated)
        candidate = dict(manager._reminders)
        candidate[reminder_id] = updated
        await manager._async_persist_state(candidate, manager._users)
        manager._reschedule(force=True)
    manager._cancel_trigger_duration_timers(reminder_id, "activation")
    await manager._trigger_registry.async_sync(manager._reminders.values())
    await manager._native_runtime.async_sync(manager._reminders.values())
    manager._notify_changed({current.user_id, updated.user_id})
    return await manager.async_get(reminder_id)


def _native_summary(triggers: list[dict[str, Any]]) -> str:
    if len(triggers) == 1:
        item = triggers[0]
        alias = item.get("alias")
        if isinstance(alias, str) and alias.strip():
            return alias.strip()[:160]
        trigger_type = item.get("trigger") or item.get("platform") or "trigger"
        return f"Home Assistant {trigger_type} trigger"
    return f"Any of {len(triggers)} Home Assistant triggers"
