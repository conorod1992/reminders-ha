"""Focused lifecycle tests for Home Assistant-native reminder rules."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.reminders.models import (
    ActivationType,
    Occurrence,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.native_automation import NativeAutomationRuntime
from custom_components.reminders.native_crud import async_update_native_triggered
from custom_components.reminders.native_manager import NativeReminderManager
from custom_components.reminders.storage import empty_storage

from .conftest import FakeDispatcher, FakeStore


def _manager(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[NativeReminderManager, FakeDispatcher]:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _due: lambda: None,
    )
    hass = SimpleNamespace(
        states={},
        bus=SimpleNamespace(),
        config=SimpleNamespace(time_zone="UTC"),
        create_task=lambda coroutine, _name=None: coroutine.close(),
    )
    dispatcher = FakeDispatcher()
    manager = NativeReminderManager(
        hass,
        FakeStore(empty_storage()),
        dispatcher,  # type: ignore[arg-type]
    )
    manager._listeners.clear()
    manager._loaded = True
    manager._native_runtime.async_sync = AsyncMock()  # type: ignore[method-assign]
    return manager, dispatcher


def _timed_reminder(now: datetime, **changes: object) -> Reminder:
    occurrence = Occurrence("occurrence", now, now)
    reminder = Reminder(
        id="native-timed",
        user_id="user",
        title="Native timed",
        due=now,
        created_at=now,
        updated_at=now,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
    )
    return reminder.updated(**changes)


async def test_wait_trigger_is_not_bypassed_when_condition_already_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wait-for plus Only-if means the wake trigger is always required."""
    now = datetime.now(UTC)
    manager, dispatcher = _manager(monkeypatch)
    reminder = _timed_reminder(
        now,
        delivery_triggers=(
            {"trigger": "state", "entity_id": "person.user", "to": "home"},
        ),
        delivery_conditions=(
            {"condition": "state", "entity_id": "input_boolean.ready", "state": "on"},
        ),
    )
    manager._reminders = {reminder.id: reminder}
    manager._native_runtime.async_conditions_match = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )

    await manager._async_process_due(now)

    updated = manager._reminders[reminder.id]
    assert updated.status is ReminderStatus.WAITING_FOR_CONTEXT
    assert dispatcher.calls == []


async def test_native_activation_uses_normal_delivery_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native trigger hit is claimed and delivered exactly like a legacy hit."""
    now = datetime.now(UTC)
    manager, dispatcher = _manager(monkeypatch)
    reminder = Reminder(
        id="native-triggered",
        user_id="user",
        title="Native triggered",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        activation_triggers=(
            {"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"},
        ),
    )
    manager._reminders = {reminder.id: reminder}

    outcome = await manager.async_activate_native_trigger(
        reminder.id,
        cause="home_assistant_state",
        context={"trigger_type": "state", "entity_id": "binary_sensor.door"},
    )

    assert outcome == "activated"
    assert len(dispatcher.calls) == 1
    assert manager._reminders[reminder.id].status is ReminderStatus.COMPLETED


async def test_native_completion_resolves_scheduled_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic completion is event-driven and can finish before delivery."""
    now = datetime.now(UTC)
    manager, dispatcher = _manager(monkeypatch)
    reminder = _timed_reminder(
        now,
        completion_triggers=(
            {"trigger": "state", "entity_id": "binary_sensor.task", "to": "on"},
        ),
    )
    manager._reminders = {reminder.id: reminder}

    outcome = await manager.async_complete_native(
        reminder.id,
        cause="home_assistant_state",
        context={"trigger_type": "state", "entity_id": "binary_sensor.task"},
    )

    updated = manager._reminders[reminder.id]
    assert outcome == "completed"
    assert updated.status is ReminderStatus.COMPLETED
    assert updated.occurrence_history[0].status.value == "completed"
    assert dispatcher.calls == []


def test_native_rule_fields_round_trip_through_storage_model() -> None:
    """Native HA dictionaries survive Reminder serialization without interpretation."""
    now = datetime.now(UTC)
    reminder = Reminder(
        id="round-trip",
        user_id="user",
        title="Round trip",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        activation_triggers=(
            {"trigger": "sun", "event": "sunset", "offset": "00:10:00"},
        ),
        completion_triggers=({"trigger": "event", "event_type": "task_finished"},),
        delivery_conditions=(
            {
                "condition": "or",
                "conditions": [
                    {"condition": "state", "entity_id": "person.user", "state": "home"},
                    {"condition": "time", "after": "20:00:00"},
                ],
            },
        ),
    )

    restored = Reminder.from_dict(reminder.to_dict())

    assert restored.activation_triggers == reminder.activation_triggers
    assert restored.completion_triggers == reminder.completion_triggers
    assert restored.delivery_conditions == reminder.delivery_conditions


async def test_native_triggered_owner_transfer_rotates_mobile_action_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.native_crud.async_validate_native_triggers",
        AsyncMock(return_value=[]),
    )
    now = datetime.now(UTC)
    manager, _ = _manager(monkeypatch)
    trigger = {
        "trigger": "state",
        "entity_id": "binary_sensor.door",
        "to": "on",
    }
    occurrence = Occurrence(
        "delivered",
        now,
        now,
        notification_action_token="previous-owner-token",
    )
    reminder = Reminder(
        id="native-transfer",
        user_id="old-owner",
        title="Native transfer",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        activation_triggers=(trigger,),
        occurrence_history=(occurrence,),
    )
    manager._reminders = {reminder.id: reminder}

    updated = await async_update_native_triggered(
        manager,
        reminder.id,
        activation_triggers=[trigger],
        user_id="new-owner",
    )

    assert updated.user_id == "new-owner"
    assert updated.occurrence_history[0].notification_action_token != (
        "previous-owner-token"
    )


async def test_native_runtime_retries_transient_trigger_attachment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A startup trigger failure retries without waiting for another reminder change."""
    now = datetime.now(UTC)
    reminder = Reminder(
        id="retry-native",
        user_id="user",
        title="Retry native",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        activation_triggers=(
            {"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"},
        ),
    )
    unsubscribe = Mock()
    initialize = AsyncMock(
        side_effect=[RuntimeError("dependency not ready"), unsubscribe]
    )
    monkeypatch.setattr(
        "custom_components.reminders.native_automation.async_validate_native_triggers",
        AsyncMock(return_value=[{"trigger": "state"}]),
    )
    monkeypatch.setattr(
        "custom_components.reminders.native_automation.trigger_helper.async_initialize_triggers",
        initialize,
    )
    monkeypatch.setattr(
        "custom_components.reminders.native_automation._TRIGGER_RETRY_DELAYS",
        (0.0,),
    )
    runtime = NativeAutomationRuntime(SimpleNamespace(), AsyncMock())

    await runtime.async_sync([reminder])

    assert runtime.listener_count == 0
    assert runtime.failed_listener_count == 1
    assert runtime.failed_listener_roles == {
        "activation": 1,
        "delivery": 0,
        "completion": 0,
    }

    for _ in range(5):
        await asyncio.sleep(0)
        if runtime.listener_count == 1:
            break

    assert initialize.await_count == 2
    assert runtime.listener_count == 1
    assert runtime.failed_listener_count == 0
    assert runtime.failed_listener_roles == {
        "activation": 0,
        "delivery": 0,
        "completion": 0,
    }

    await runtime.async_unload()
    unsubscribe.assert_called_once_with()


async def test_native_runtime_cancels_retry_when_trigger_is_no_longer_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed or resolved rules cannot be resurrected by an old retry task."""
    now = datetime.now(UTC)
    reminder = Reminder(
        id="cancel-native-retry",
        user_id="user",
        title="Cancel native retry",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        activation_triggers=(
            {"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"},
        ),
    )
    initialize = AsyncMock(side_effect=RuntimeError("dependency not ready"))
    monkeypatch.setattr(
        "custom_components.reminders.native_automation.async_validate_native_triggers",
        AsyncMock(return_value=[{"trigger": "state"}]),
    )
    monkeypatch.setattr(
        "custom_components.reminders.native_automation.trigger_helper.async_initialize_triggers",
        initialize,
    )
    monkeypatch.setattr(
        "custom_components.reminders.native_automation._TRIGGER_RETRY_DELAYS",
        (60.0,),
    )
    runtime = NativeAutomationRuntime(SimpleNamespace(), AsyncMock())

    await runtime.async_sync([reminder])
    assert runtime.failed_listener_count == 1
    assert runtime._trigger_retry_tasks

    await runtime.async_sync([])
    await asyncio.sleep(0)

    assert runtime.failed_listener_count == 0
    assert runtime.listener_count == 0
    assert not runtime._trigger_retry_tasks
    assert initialize.await_count == 1

    await runtime.async_unload()
