"""Behavioral regressions from the repository reliability review."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import CoreState, Event, State
from homeassistant.util.file import WriteError

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    ActivationType,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
    UserPreferences,
)
from custom_components.reminders.native_crud import async_update_native_triggered
from custom_components.reminders.recurrence import RecurrenceFrequency, RecurrenceRule
from custom_components.reminders.storage import ReminderStore, deserialize_storage
from custom_components.reminders.triggers.models import (
    TriggerDefinition,
    canonical_trigger_key,
)
from custom_components.reminders.triggers.registry import TriggerRegistry, _Entry
from tests.conftest import FakeDispatcher
from tests.test_native_rules import _manager, _timed_reminder


async def test_native_condition_edit_zone_unknown_and_history_retention(monkeypatch):
    patch = monkeypatch
    manager, _dispatcher = _manager(patch)
    now = datetime.now(UTC)
    reminder = _timed_reminder(
        now,
        delivery_conditions=(
            {"condition": "state", "entity_id": "input_boolean.ready", "state": "on"},
        ),
    )
    manager._reminders = {reminder.id: reminder}
    tomorrow = now + timedelta(days=1)

    async def evaluated_old_conditions(old):
        await manager.async_update(reminder.id, due=tomorrow)

        return False

    manager._native_runtime.async_conditions_match = AsyncMock(
        side_effect=evaluated_old_conditions
    )
    await manager._async_process_due(now)
    result = manager._reminders[reminder.id]
    assert result.status is ReminderStatus.PENDING
    assert result.due == tomorrow
    patch.undo()

    zone = State("zone.home", "0", {"latitude": 0, "longitude": 0, "radius": 100})
    person = State("person.user", "unavailable", {})
    registry = TriggerRegistry(
        SimpleNamespace(states={"zone.home": zone, "person.user": person}), AsyncMock()
    )
    definition = TriggerDefinition.from_dict(
        {
            "type": "zone",
            "entity_id": "person.user",
            "zone_entity_id": "zone.home",
            "event": "leave",
        }
    )
    key = canonical_trigger_key(definition)
    registry._entries[key] = _Entry(
        definition=definition, reminder_ids={"leave-reminder"}
    )
    await registry._async_state_changed(
        key,
        Event(
            "state_changed",
            {
                "old_state": State(
                    "person.user", "home", {"latitude": 0, "longitude": 0}
                ),
                "new_state": person,
            },
        ),
    )
    assert all(
        call.args[1] == "duration_cancelled"
        for call in registry._callback.await_args_list
    )
    assert not registry.condition_is_currently_matching(definition)

    patch = monkeypatch
    manager, _dispatcher = _manager(patch)
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    rule = RecurrenceRule(
        RecurrenceFrequency.DAILY, 1, "UTC", tomorrow.replace(tzinfo=None)
    )
    reminder = _timed_reminder(tomorrow, recurrence=rule, scheduled_due=tomorrow)
    manager._reminders = {reminder.id: reminder}
    for _ in range(300):
        await manager.async_skip_next(reminder.id)
    result = manager._reminders[reminder.id]
    assert len(result.occurrence_history) <= UserPreferences().history_max_occurrences
    patch.undo()


async def test_detached_native_callback_is_retired(monkeypatch):
    patch = monkeypatch
    manager, dispatcher = _manager(patch)
    now = datetime.now(UTC)
    reminder = _timed_reminder(
        now,
        status=ReminderStatus.WAITING_FOR_CONTEXT,
        delivery_triggers=({"trigger": "event", "event_type": "old_event"},),
    )
    reminder = reminder.updated(
        occurrence_history=(
            reminder.occurrence_history[0].updated(
                status=OccurrenceStatus.WAITING_FOR_CONTEXT
            ),
        )
    )
    manager._reminders[reminder.id] = reminder
    old_callback = manager._native_runtime._action((reminder.id, "delivery"))
    patch.setattr(
        "custom_components.reminders.native_automation.async_validate_native_triggers",
        AsyncMock(return_value=[]),
    )
    await manager.async_set_native_rules(
        reminder.id, delivery_triggers=[{"trigger": "event", "event_type": "new_event"}]
    )
    await old_callback({"trigger": {"platform": "event", "event": "old_event"}})
    assert not dispatcher.calls
    patch.undo()


async def test_native_title_edit_preserves_snoozed_redelivery(monkeypatch):
    patch = monkeypatch
    manager, dispatcher = _manager(patch)
    patch.setattr(
        "custom_components.reminders.native_crud.async_validate_native_triggers",
        AsyncMock(return_value=[]),
    )
    now = datetime.now(UTC)
    trigger = {"trigger": "event", "event_type": "task_started"}
    r = Reminder(
        id="r",
        user_id="user",
        title="before",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        activation_triggers=(trigger,),
    )
    manager._reminders[r.id] = r
    await manager.async_activate_native_trigger(r.id, cause="event", context={})
    r = manager._reminders[r.id]
    await manager.async_snooze_occurrence(
        r.id, r.current_occurrence_id, timedelta(minutes=10)
    )
    r = manager._reminders[r.id]
    expected_due = r.due
    await async_update_native_triggered(
        manager, r.id, activation_triggers=[trigger], title="after"
    )
    r = manager._reminders[r.id]
    await manager._async_process_due(expected_due + timedelta(seconds=1))
    assert r.due == expected_due
    assert len(dispatcher.calls) == 2
    patch.undo()


async def test_real_store_write_error_aborts_runtime_commit(monkeypatch, tmp_path):
    hass = SimpleNamespace(
        data={},
        state=CoreState.running,
        config=SimpleNamespace(config_dir=str(tmp_path)),
    )
    store = ReminderStore(hass)
    store._async_write_data = AsyncMock(side_effect=WriteError("disk full"))
    manager = ReminderManager(hass, store, FakeDispatcher())
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda *args: lambda: None,
    )
    with pytest.raises(WriteError):
        await manager.async_create(
            user_id="u1", title="Durable", due=datetime.now(UTC) + timedelta(days=1)
        )
    assert not manager._reminders
    assert manager.scheduled_for is None


@pytest.mark.parametrize("value", [[], None, 1, {"id": "bad", "delivery_policy": []}])
def test_malformed_storage_records_are_isolated(value):
    now = datetime.now(UTC)
    good = Reminder(
        id="good", user_id="u1", title="Good", due=now, created_at=now, updated_at=now
    )
    reminders, users = deserialize_storage(
        {"reminders": {"good": good.to_dict(), "bad": value}, "users": {"bad": value}}
    )
    assert list(reminders) == ["good"]
    assert not users or isinstance(value, dict)


async def test_shared_trigger_fanout_is_bounded_and_independent():
    import asyncio

    definition = TriggerDefinition.from_dict({"type": "named", "trigger_id": "shared"})
    active = peak = 0
    release = asyncio.Event()
    started = asyncio.Event()

    async def callback(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            started.set()
        await release.wait()
        active -= 1

    registry = TriggerRegistry(SimpleNamespace(), callback)
    task = asyncio.create_task(
        registry._activate(
            _Entry(definition=definition, reminder_ids={str(i) for i in range(8)}),
            "event",
            {},
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    release.set()
    await task
    assert peak == 4
