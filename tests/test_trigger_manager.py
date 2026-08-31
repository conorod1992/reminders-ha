"""Manager tests for named trigger repeat, cooldown, and ownership behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import Event, State

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    AcknowledgementPolicy,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
    TriggerRepeatPolicy,
)
from custom_components.reminders.storage import serialize_storage
from custom_components.reminders.triggers.models import TriggerDefinition

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


async def test_state_listener_schedules_safely_from_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
    fake_store: FakeStore,
    no_timers: None,
) -> None:
    """State callbacks may be dispatched by Home Assistant's executor."""
    listener: list[Any] = []

    def listen(_hass: Any, _entity_ids: list[str], callback: Any) -> Any:
        listener.append(callback)
        return lambda: None

    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        listen,
    )
    loop = asyncio.get_running_loop()

    def create_task(coroutine: Any, name: str | None = None) -> None:
        loop.call_soon_threadsafe(lambda: asyncio.create_task(coroutine, name=name))

    hass = SimpleNamespace(
        states={"light.study": State("light.study", "on")},
        bus=SimpleNamespace(),
        create_task=create_task,
    )
    dispatcher = FakeDispatcher()
    manager = ReminderManager(hass, fake_store, dispatcher)  # type: ignore[arg-type]
    await manager.async_load()
    await manager.async_create_triggered(
        user_id="u1",
        title="Light off",
        trigger={"type": "state", "entity_id": "light.study", "to": "off"},
    )

    event = Event(
        "state_changed",
        {
            "entity_id": "light.study",
            "old_state": State("light.study", "on"),
            "new_state": State("light.study", "off"),
        },
    )
    await asyncio.to_thread(listener[0], event)
    for _ in range(100):
        if dispatcher.calls:
            break
        await asyncio.sleep(0)

    assert len(dispatcher.calls) == 1


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


class DurationTimers:
    """Capture reminder-local duration timers and their remaining delay."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, Any]] = []

    def schedule(self, _hass: Any, delay: float, callback: Any) -> Any:
        self.calls.append((delay, callback))
        return lambda: None


async def test_restart_resumes_already_matching_duration_from_original_start(
    monkeypatch: pytest.MonkeyPatch,
    fake_store: FakeStore,
    no_timers: None,
    trigger_listeners: list[str],
) -> None:
    del trigger_listeners
    timers = DurationTimers()
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_call_later", timers.schedule
    )
    hass = SimpleNamespace(
        states={"sensor.work": State("sensor.work", "done")},
        bus=SimpleNamespace(),
    )
    first = ReminderManager(hass, fake_store, FakeDispatcher())  # type: ignore[arg-type]
    await first.async_load()
    reminder = await first.async_create_triggered(
        user_id="u1",
        title="Now after duration",
        trigger={
            "type": "state",
            "entity_id": "sensor.work",
            "to": "done",
            "for_seconds": 30,
        },
        fire_if_already_matching=True,
    )
    persisted_start = (
        (await first.async_get(reminder.id)).trigger_duration_waits[0].started_at
    )
    assert persisted_start is not None
    assert len(timers.calls) == 1
    await first.async_unload()

    twenty_seconds_ago = datetime.now(persisted_start.tzinfo) - timedelta(seconds=20)
    fake_store.data["reminders"][reminder.id]["trigger_duration_waits"][0][
        "started_at"
    ] = twenty_seconds_ago.isoformat()
    restarted_dispatcher = FakeDispatcher()
    restarted = ReminderManager(  # type: ignore[arg-type]
        hass, fake_store, restarted_dispatcher
    )
    await restarted.async_load()

    remaining, callback = timers.calls[-1]
    assert 0 < remaining <= 11
    assert (await restarted.async_get(reminder.id)).immediate_evaluated is True
    await callback(datetime.now(persisted_start.tzinfo))
    assert len(restarted_dispatcher.calls) == 1
    assert (await restarted.async_get(reminder.id)).trigger_duration_waits == ()


async def test_restart_resumes_ordinary_duration_for_deduplicated_reminders(
    monkeypatch: pytest.MonkeyPatch,
    fake_store: FakeStore,
    no_timers: None,
) -> None:
    listeners: list[Any] = []

    def listen(_hass: Any, _entities: list[str], callback: Any) -> Any:
        listeners.append(callback)
        return lambda: None

    timers = DurationTimers()
    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        listen,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_call_later", timers.schedule
    )
    loop = asyncio.get_running_loop()
    hass = SimpleNamespace(
        states={"sensor.work": State("sensor.work", "idle")},
        bus=SimpleNamespace(),
        create_task=lambda coroutine, name=None: loop.create_task(coroutine, name=name),
    )
    first = ReminderManager(hass, fake_store, FakeDispatcher())  # type: ignore[arg-type]
    await first.async_load()
    reminders = [
        await first.async_create_triggered(
            user_id="u1",
            title=title,
            trigger={
                "type": "state",
                "entity_id": "sensor.work",
                "to": "done",
                "for_seconds": 30,
            },
        )
        for title in ("One", "Two")
    ]
    assert first.trigger_listener_count == 1
    hass.states["sensor.work"] = State("sensor.work", "done")
    listeners[0](
        Event(
            "state_changed",
            {
                "old_state": State("sensor.work", "idle"),
                "new_state": hass.states["sensor.work"],
            },
        )
    )
    for _ in range(20):
        starts = [
            (await first.async_get(item.id)).trigger_duration_waits
            for item in reminders
        ]
        if all(starts):
            break
        await asyncio.sleep(0)
    assert len(timers.calls) == 2
    await first.async_unload()

    twenty_seconds_ago = datetime.now().astimezone() - timedelta(seconds=20)
    for reminder in reminders:
        fake_store.data["reminders"][reminder.id]["trigger_duration_waits"][0][
            "started_at"
        ] = twenty_seconds_ago.isoformat()
    restarted = ReminderManager(  # type: ignore[arg-type]
        hass, fake_store, FakeDispatcher()
    )
    await restarted.async_load()
    resumed = timers.calls[-2:]
    assert len(resumed) == 2
    assert all(0 < delay <= 11 for delay, _callback in resumed)
    assert restarted.trigger_listener_count == 1


async def test_trigger_task_queued_immediately_before_unload_is_inert(
    monkeypatch: pytest.MonkeyPatch,
    fake_store: FakeStore,
    no_timers: None,
) -> None:
    listeners: list[Any] = []

    def listen(_hass: Any, _entities: list[str], callback: Any) -> Any:
        listeners.append(callback)
        return lambda: None

    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        listen,
    )
    dispatcher = FakeDispatcher()
    loop = asyncio.get_running_loop()
    hass = SimpleNamespace(
        states={"light.study": State("light.study", "on")},
        bus=SimpleNamespace(),
        create_task=lambda coroutine, name=None: loop.create_task(coroutine, name=name),
    )
    manager = ReminderManager(hass, fake_store, dispatcher)  # type: ignore[arg-type]
    await manager.async_load()
    reminder = await manager.async_create_triggered(
        user_id="u1",
        title="Light off",
        trigger={"type": "state", "entity_id": "light.study", "to": "off"},
    )
    listeners[0](
        Event(
            "state_changed",
            {
                "old_state": State("light.study", "on"),
                "new_state": State("light.study", "off"),
            },
        )
    )
    await manager.async_unload()
    await asyncio.sleep(0)
    assert dispatcher.calls == []
    assert (await manager.async_get(reminder.id)).last_triggered_at is None


async def test_restart_resumes_deliver_when_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners: list[Any] = []
    timers = DurationTimers()

    def listen(_hass: Any, _entities: list[str], callback: Any) -> Any:
        listeners.append(callback)
        return lambda: None

    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        listen,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_call_later", timers.schedule
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _due: lambda: None,
    )
    now = datetime.now().astimezone()
    occurrence = Occurrence(
        "waiting", now, now, status=OccurrenceStatus.WAITING_FOR_CONTEXT
    )
    reminder = Reminder(
        id="deliver-wait",
        user_id="u1",
        title="Deliver with context",
        due=now,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_CONTEXT,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        deliver_when=TriggerDefinition.from_dict(
            {
                "type": "state",
                "entity_id": "sensor.context",
                "to": "ready",
                "for_seconds": 30,
            }
        ),
    )
    loop = asyncio.get_running_loop()
    hass = SimpleNamespace(
        states={"sensor.context": State("sensor.context", "idle")},
        bus=SimpleNamespace(),
        create_task=lambda coroutine, name=None: loop.create_task(coroutine, name=name),
    )
    store = FakeStore(serialize_storage({reminder.id: reminder}, {}))
    first = ReminderManager(hass, store, FakeDispatcher())  # type: ignore[arg-type]
    await first.async_load()
    hass.states["sensor.context"] = State("sensor.context", "ready")
    listeners[0](
        Event(
            "state_changed",
            {
                "old_state": State("sensor.context", "idle"),
                "new_state": hass.states["sensor.context"],
            },
        )
    )
    for _ in range(20):
        if (await first.async_get(reminder.id)).trigger_duration_waits:
            break
        await asyncio.sleep(0)
    await first.async_unload()
    store.data["reminders"][reminder.id]["trigger_duration_waits"][0]["started_at"] = (
        now - timedelta(seconds=20)
    ).isoformat()

    dispatcher = FakeDispatcher()
    restarted = ReminderManager(hass, store, dispatcher)  # type: ignore[arg-type]
    await restarted.async_load()
    delay, callback = timers.calls[-1]
    assert 0 < delay <= 11
    await callback(now)
    assert len(dispatcher.calls) == 1
    assert (await restarted.async_get(reminder.id)).trigger_duration_waits == ()


async def test_restart_resumes_complete_when_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners: list[Any] = []
    timers = DurationTimers()

    def listen(_hass: Any, _entities: list[str], callback: Any) -> Any:
        listeners.append(callback)
        return lambda: None

    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        listen,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_call_later", timers.schedule
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _due: lambda: None,
    )
    now = datetime.now().astimezone()
    occurrence = Occurrence("scheduled", now, now)
    reminder = Reminder(
        id="complete-wait",
        user_id="u1",
        title="Complete with context",
        due=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        complete_when=TriggerDefinition.from_dict(
            {
                "type": "state",
                "entity_id": "sensor.complete",
                "to": "done",
                "for_seconds": 30,
            }
        ),
    )
    loop = asyncio.get_running_loop()
    hass = SimpleNamespace(
        states={"sensor.complete": State("sensor.complete", "idle")},
        bus=SimpleNamespace(),
        create_task=lambda coroutine, name=None: loop.create_task(coroutine, name=name),
    )
    store = FakeStore(serialize_storage({reminder.id: reminder}, {}))
    first = ReminderManager(hass, store, FakeDispatcher())  # type: ignore[arg-type]
    await first.async_load()
    hass.states["sensor.complete"] = State("sensor.complete", "done")
    listeners[0](
        Event(
            "state_changed",
            {
                "old_state": State("sensor.complete", "idle"),
                "new_state": hass.states["sensor.complete"],
            },
        )
    )
    for _ in range(20):
        if (await first.async_get(reminder.id)).trigger_duration_waits:
            break
        await asyncio.sleep(0)
    await first.async_unload()
    store.data["reminders"][reminder.id]["trigger_duration_waits"][0]["started_at"] = (
        now - timedelta(seconds=20)
    ).isoformat()

    restarted = ReminderManager(hass, store, FakeDispatcher())  # type: ignore[arg-type]
    await restarted.async_load()
    delay, callback = timers.calls[-1]
    assert 0 < delay <= 11
    await callback(now)
    completed = await restarted.async_get(reminder.id)
    assert completed.trigger_duration_waits == ()
    assert completed.occurrence_history[0].status is OccurrenceStatus.COMPLETED


async def test_attribute_only_duration_restart_requires_original_value(
    monkeypatch: pytest.MonkeyPatch,
    fake_store: FakeStore,
    no_timers: None,
) -> None:
    listeners: list[Any] = []
    timers = DurationTimers()

    def listen(_hass: Any, _entities: list[str], callback: Any) -> Any:
        listeners.append(callback)
        return lambda: None

    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        listen,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_call_later", timers.schedule
    )
    loop = asyncio.get_running_loop()
    hass = SimpleNamespace(
        states={"climate.study": State("climate.study", "heat", {"preset": "eco"})},
        bus=SimpleNamespace(),
        create_task=lambda coroutine, name=None: loop.create_task(coroutine, name=name),
    )
    first = ReminderManager(hass, fake_store, FakeDispatcher())  # type: ignore[arg-type]
    await first.async_load()
    reminder = await first.async_create_triggered(
        user_id="u1",
        title="Stable preset",
        trigger={
            "type": "state",
            "entity_id": "climate.study",
            "attribute": "preset",
            "for_seconds": 30,
        },
    )
    hass.states["climate.study"] = State("climate.study", "heat", {"preset": "away"})
    listeners[0](
        Event(
            "state_changed",
            {
                "old_state": State("climate.study", "heat", {"preset": "eco"}),
                "new_state": hass.states["climate.study"],
            },
        )
    )
    for _ in range(20):
        waits = (await first.async_get(reminder.id)).trigger_duration_waits
        if waits:
            break
        await asyncio.sleep(0)
    assert waits[0].observed_value == "away"
    await first.async_unload()

    restarted = ReminderManager(hass, fake_store, FakeDispatcher())  # type: ignore[arg-type]
    await restarted.async_load()
    assert len(timers.calls) == 2
    _delay, callback = timers.calls[-1]
    hass.states["climate.study"] = State("climate.study", "heat", {"preset": "eco"})
    await callback(datetime.now().astimezone())
    assert (await restarted.async_get(reminder.id)).trigger_duration_waits == ()


async def test_classic_trigger_metadata_edit_preserves_awaiting_occurrence(
    fake_store: FakeStore, no_timers: None
) -> None:
    manager = await _manager(fake_store, FakeDispatcher())
    reminder = await manager.async_create_triggered(
        user_id="u1",
        title="Handover",
        trigger={"type": "named", "trigger_id": "handover"},
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        allow_manual_completion=True,
    )
    await manager.async_fire_named_trigger("handover", user_id="u1")
    awaiting = await manager.async_get(reminder.id)
    occurrence_id = awaiting.current_occurrence_id
    assert occurrence_id is not None
    assert awaiting.status is ReminderStatus.AWAITING_ACKNOWLEDGEMENT

    edited = await manager.async_update(reminder.id, title="Renamed handover")
    assert edited.current_occurrence_id == occurrence_id
    active = next(
        item for item in edited.occurrence_history if item.id == occurrence_id
    )
    assert active.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT

    await manager.async_complete(
        reminder.id, occurrence_id=occurrence_id, completed_by="u1"
    )
    completed = await manager.async_get(reminder.id)
    assert completed.status is ReminderStatus.COMPLETED


async def test_classic_trigger_metadata_edit_preserves_context_wait(
    monkeypatch: pytest.MonkeyPatch,
    fake_store: FakeStore,
    no_timers: None,
) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        lambda _hass, _entities, _callback: lambda: None,
    )
    manager = await _manager(fake_store, FakeDispatcher())
    reminder = await manager.async_create_triggered(
        user_id="u1",
        title="Wait for ready",
        trigger={"type": "named", "trigger_id": "activate"},
    )
    now = datetime.now().astimezone()
    occurrence = Occurrence(
        "waiting", now, now, status=OccurrenceStatus.WAITING_FOR_CONTEXT
    )
    manager._reminders[reminder.id] = reminder.updated(
        status=ReminderStatus.WAITING_FOR_CONTEXT,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        deliver_when=TriggerDefinition.from_dict(
            {
                "type": "state",
                "entity_id": "sensor.context",
                "to": "ready",
            }
        ),
    )

    edited = await manager.async_update(reminder.id, title="Still wait for ready")
    assert edited.current_occurrence_id == occurrence.id
    active = next(
        item for item in edited.occurrence_history if item.id == occurrence.id
    )
    assert active.status is OccurrenceStatus.WAITING_FOR_CONTEXT
