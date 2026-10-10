"""Config-entry setup failure cleanup regressions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import Event, State

from custom_components.reminders import async_setup_entry, async_unload_entry
from custom_components.reminders.models import ActivationType, Reminder, ReminderStatus
from custom_components.reminders.storage import serialize_storage
from custom_components.reminders.triggers.models import TriggerDefinition

from .conftest import FakeDispatcher
from .test_atomic_persistence import FailingStore, SaveError


class TrackingBus:
    """Track installed integration event-bus listeners."""

    def __init__(self) -> None:
        self.listeners: list[Any] = []

    def async_listen(self, _event_type: str, callback: Any) -> Any:
        self.listeners.append(callback)

        def unsubscribe() -> None:
            self.listeners.remove(callback)

        return unsubscribe


async def test_failed_setup_unloads_listeners_before_successful_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    due = Reminder(
        id="due",
        user_id="u1",
        title="Due",
        due=now - timedelta(seconds=1),
        created_at=now,
        updated_at=now,
    )
    trigger = Reminder(
        id="trigger",
        user_id="u1",
        title="Triggered",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        trigger=TriggerDefinition.from_dict(
            {"type": "state", "entity_id": "sensor.work", "to": "done"}
        ),
    )
    store = FailingStore(
        serialize_storage({due.id: due, trigger.id: trigger}, {}),
        fail_on_calls={1},
    )
    active_state_callbacks: list[Any] = []
    all_state_callbacks: list[Any] = []

    def listen(_hass: Any, _entities: list[str], callback: Any) -> Any:
        active_state_callbacks.append(callback)
        all_state_callbacks.append(callback)

        def unsubscribe() -> None:
            active_state_callbacks.remove(callback)

        return unsubscribe

    dispatcher = FakeDispatcher()
    bus = TrackingBus()
    loop = asyncio.get_running_loop()
    hass = SimpleNamespace(
        states={"sensor.work": State("sensor.work", "idle")},
        bus=bus,
        data={},
        config=SimpleNamespace(time_zone="UTC"),
        create_task=lambda coroutine, name=None: loop.create_task(coroutine, name=name),
    )
    monkeypatch.setattr(
        "custom_components.reminders.ReminderStore", lambda _hass: store
    )
    monkeypatch.setattr(
        "custom_components.reminders.DeliveryDispatcher", lambda _providers: dispatcher
    )

    class FakeCleanupCoordinator:
        def __init__(self, _hass: Any) -> None:
            return None

        async def async_load(self) -> None:
            return None

        async def async_reconcile(self, _reminders: Any) -> None:
            return None

    monkeypatch.setattr(
        "custom_components.reminders.PersistentCleanupCoordinator",
        FakeCleanupCoordinator,
    )
    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        listen,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _due: lambda: None,
    )

    async def register_frontend(_hass: Any) -> None:
        return None

    monkeypatch.setattr(
        "custom_components.reminders.async_register_frontend", register_frontend
    )
    monkeypatch.setattr(
        "custom_components.reminders.async_unregister_panel", lambda _hass: None
    )
    entry = SimpleNamespace()

    with pytest.raises(SaveError):
        await async_setup_entry(hass, entry)  # type: ignore[arg-type]
    assert active_state_callbacks == []
    assert bus.listeners == []

    store.fail_on_calls.clear()
    await async_setup_entry(hass, entry)  # type: ignore[arg-type]
    assert len(active_state_callbacks) == 1
    # Mobile actions and persistent-notification lifecycle cleanup both listen on
    # the HA event bus after a successful setup.
    assert len(bus.listeners) == 2
    delivered_after_retry = len(dispatcher.calls)

    all_state_callbacks[0](
        Event(
            "state_changed",
            {
                "old_state": State("sensor.work", "idle"),
                "new_state": State("sensor.work", "done"),
            },
        )
    )
    await asyncio.sleep(0)
    assert len(dispatcher.calls) == delivered_after_retry

    assert await async_unload_entry(hass, entry) is True  # type: ignore[arg-type]
    assert active_state_callbacks == []
    assert bus.listeners == []
