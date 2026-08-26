"""Focused lifecycle tests for Home Assistant-native reminder rules."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.reminders.models import (
    ActivationType,
    Occurrence,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.native_manager import NativeReminderManager
from custom_components.reminders.storage import empty_storage

from .conftest import FakeDispatcher, FakeStore


def _manager(monkeypatch: pytest.MonkeyPatch) -> tuple[NativeReminderManager, FakeDispatcher]:
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
        hass, FakeStore(empty_storage()), dispatcher  # type: ignore[arg-type]
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
        completion_triggers=(
            {"trigger": "event", "event_type": "task_finished"},
        ),
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
