"""Manager tests for named trigger repeat, cooldown, and ownership behavior."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import State

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    AcknowledgementPolicy,
    ReminderStatus,
    TriggerRepeatPolicy,
)

from .conftest import FakeDispatcher, FakeStore


@pytest.fixture
def no_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    def schedule(_hass: Any, _callback: Any, _when: datetime) -> Any:
        return lambda: None

    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time", schedule
    )


async def _manager(store: FakeStore, dispatcher: FakeDispatcher) -> ReminderManager:
    hass = SimpleNamespace(states={}, bus=SimpleNamespace())
    manager = ReminderManager(hass, store, dispatcher)  # type: ignore[arg-type]
    await manager.async_load()
    return manager


@pytest.fixture
def trigger_listeners(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    active: list[str] = []

    def listen(_hass: Any, entity_ids: list[str], _callback: Any) -> Any:
        entity_id = entity_ids[0]
        active.append(entity_id)

        def unsubscribe() -> None:
            active.remove(entity_id)

        return unsubscribe

    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        listen,
    )
    return active


async def test_named_trigger_once_and_zero_matches(
    fake_store: FakeStore, no_timers: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    reminder = await manager.async_create_triggered(
        user_id="u1",
        title="Print",
        trigger={"type": "named", "trigger_id": "printing_started"},
    )
    result = await manager.async_fire_named_trigger("PRINTING_STARTED", user_id="u1")
    assert result["matched"] == 1
    assert result["activated"] == 1
    assert len(dispatcher.calls) == 1
    assert (await manager.async_get(reminder.id)).status is ReminderStatus.COMPLETED
    again = await manager.async_fire_named_trigger("printing_started", user_id="u1")
    assert again["matched"] == 0
    assert again["activated"] == 0
    await manager.async_wait_for_next_trigger(reminder.id)
    rearmed = await manager.async_fire_named_trigger("printing_started", user_id="u1")
    assert rearmed["matched"] == 1
    assert rearmed["activated"] == 1


async def test_named_trigger_is_owner_scoped(
    fake_store: FakeStore, no_timers: None
) -> None:
    manager = await _manager(fake_store, FakeDispatcher())
    await manager.async_create_triggered(
        user_id="u1",
        title="One",
        trigger={"type": "named", "trigger_id": "shared"},
    )
    await manager.async_create_triggered(
        user_id="u2",
        title="Two",
        trigger={"type": "named", "trigger_id": "shared"},
    )
    result = await manager.async_fire_named_trigger("shared", user_id="u1")
    assert result["matched"] == 1
    assert result["activated"] == 1
    assert (await manager.async_list(user_id="u2"))[0].last_triggered_at is None


async def test_every_trigger_cooldown_and_acknowledgement_skip(
    fake_store: FakeStore, no_timers: None
) -> None:
    manager = await _manager(fake_store, FakeDispatcher())
    reminder = await manager.async_create_triggered(
        user_id="u1",
        title="Every print",
        trigger={"type": "named", "trigger_id": "printing"},
        repeat_policy=TriggerRepeatPolicy.EVERY_TRIGGER,
        cooldown_seconds=300,
    )
    first = await manager.async_fire_named_trigger("printing", user_id="u1")
    second = await manager.async_fire_named_trigger("printing", user_id="u1")
    assert first["activated"] == 1
    assert second["skipped_cooldown"] == 1
    assert (await manager.async_get(reminder.id)).cooldown_skip_count == 1

    awaiting = await manager.async_create_triggered(
        user_id="u1",
        title="Needs done",
        trigger={"type": "named", "trigger_id": "handover"},
        repeat_policy=TriggerRepeatPolicy.EVERY_TRIGGER,
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
    )
    await manager.async_fire_named_trigger("handover", user_id="u1")
    skipped = await manager.async_fire_named_trigger("handover", user_id="u1")
    assert skipped["skipped_inactive"] == 1
    current = await manager.async_get(awaiting.id)
    assert len(current.occurrence_history) == 1


async def test_equivalent_state_triggers_share_and_release_listener(
    fake_store: FakeStore,
    no_timers: None,
    trigger_listeners: list[str],
) -> None:
    manager = await _manager(fake_store, FakeDispatcher())
    first = await manager.async_create_triggered(
        user_id="u1",
        title="First",
        trigger={"type": "state", "entity_id": "sensor.work", "to": "done"},
    )
    second = await manager.async_create_triggered(
        user_id="u1",
        title="Second",
        trigger={"to": "done", "entity_id": "SENSOR.WORK", "type": "state"},
    )
    assert manager.trigger_listener_count == 1
    assert trigger_listeners == ["sensor.work"]
    await manager.async_delete(first.id)
    assert manager.trigger_listener_count == 1
    await manager.async_delete(second.id)
    assert manager.trigger_listener_count == 0
    assert trigger_listeners == []


async def test_already_matching_default_and_opt_in(
    fake_store: FakeStore,
    no_timers: None,
    trigger_listeners: list[str],
) -> None:
    del trigger_listeners
    dispatcher = FakeDispatcher()
    hass = SimpleNamespace(
        states={"sensor.work": State("sensor.work", "done")},
        bus=SimpleNamespace(),
    )
    manager = ReminderManager(hass, fake_store, dispatcher)  # type: ignore[arg-type]
    await manager.async_load()
    waiting = await manager.async_create_triggered(
        user_id="u1",
        title="Wait",
        trigger={"type": "state", "entity_id": "sensor.work", "to": "done"},
    )
    assert (
        await manager.async_get(waiting.id)
    ).status is ReminderStatus.WAITING_FOR_TRIGGER
    assert dispatcher.calls == []
    immediate = await manager.async_create_triggered(
        user_id="u1",
        title="Now",
        trigger={"type": "state", "entity_id": "sensor.work", "to": "done"},
        fire_if_already_matching=True,
    )
    assert (await manager.async_get(immediate.id)).status is ReminderStatus.COMPLETED
    assert len(dispatcher.calls) == 1
