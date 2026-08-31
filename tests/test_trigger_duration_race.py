"""Concurrency regressions for durable trigger-duration timers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import State

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    ActivationType,
    Reminder,
    ReminderStatus,
    TriggerDurationWait,
)
from custom_components.reminders.triggers.models import TriggerDefinition

from .conftest import FakeDispatcher, FakeStore


class RaceTimers:
    """Capture callbacks even after cancellation to simulate queued HA work."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, Any]] = []
        self.cancelled: list[bool] = []

    def schedule(self, _hass: Any, delay: float, callback: Any) -> Any:
        index = len(self.calls)
        self.calls.append((delay, callback))
        self.cancelled.append(False)

        def cancel() -> None:
            self.cancelled[index] = True

        return cancel


def _build_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ReminderManager, FakeDispatcher, RaceTimers]:
    timers = RaceTimers()
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_call_later", timers.schedule
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _when: lambda: None,
    )
    hass = SimpleNamespace(
        states={"sensor.work": State("sensor.work", "done")},
        bus=SimpleNamespace(),
    )
    dispatcher = FakeDispatcher()
    manager = ReminderManager(
        hass,
        FakeStore(),
        dispatcher,  # type: ignore[arg-type]
    )
    manager._loaded = True
    manager._trigger_registry.async_sync = AsyncMock()  # type: ignore[method-assign]
    return manager, dispatcher, timers


def _duration_reminder(wait: TriggerDurationWait) -> Reminder:
    now = datetime.now(UTC)
    return Reminder(
        id="duration-race",
        user_id="u1",
        title="Duration race",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        trigger=TriggerDefinition.from_dict(
            {
                "type": "state",
                "entity_id": "sensor.work",
                "to": "done",
                "for_seconds": 30,
            }
        ),
        trigger_duration_waits=(wait,),
    )


async def test_running_old_callback_cannot_clear_replacement_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback already running must become inert when its wait is replaced."""
    manager, dispatcher, timers = _build_manager(monkeypatch)
    now = datetime.now(UTC)
    first_wait = TriggerDurationWait(
        "activation", now - timedelta(seconds=31), "first", {}
    )
    first = _duration_reminder(first_wait)
    manager._reminders[first.id] = first
    manager._schedule_trigger_duration(first, "activation")
    old_callback = timers.calls[-1][1]

    await manager._lock.acquire()
    old_task = asyncio.create_task(old_callback(now))
    await asyncio.sleep(0)

    replacement_wait = TriggerDurationWait(
        "activation", now, "replacement", {"generation": 2}
    )
    replacement = first.updated(trigger_duration_waits=(replacement_wait,))
    manager._reminders[first.id] = replacement
    manager._schedule_trigger_duration(replacement, "activation")
    replacement_callback = timers.calls[-1][1]
    manager._lock.release()

    await old_task

    current = await manager.async_get(first.id)
    assert current.trigger_duration_waits == (replacement_wait,)
    assert (first.id, "activation") in manager._trigger_duration_timers
    assert dispatcher.calls == []

    await replacement_callback(now + timedelta(seconds=31))
    assert len(dispatcher.calls) == 1
    assert (await manager.async_get(first.id)).trigger_duration_waits == ()


async def test_cancelled_same_wait_callback_cannot_consume_new_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timer identity, not only wait equality, decides which callback may fire."""
    manager, dispatcher, timers = _build_manager(monkeypatch)
    now = datetime.now(UTC)
    wait = TriggerDurationWait(
        "activation", now - timedelta(seconds=31), "same-wait", {}
    )
    reminder = _duration_reminder(wait)
    manager._reminders[reminder.id] = reminder

    manager._schedule_trigger_duration(reminder, "activation")
    old_callback = timers.calls[-1][1]
    manager._schedule_trigger_duration(reminder, "activation")
    current_callback = timers.calls[-1][1]
    assert timers.cancelled[0] is True

    await old_callback(now)

    assert dispatcher.calls == []
    assert (reminder.id, "activation") in manager._trigger_duration_timers
    assert (await manager.async_get(reminder.id)).trigger_duration_waits == (wait,)

    await current_callback(now)
    assert len(dispatcher.calls) == 1
    assert (await manager.async_get(reminder.id)).trigger_duration_waits == ()


async def test_cancelled_callback_after_delete_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued callback cannot raise after its reminder has been deleted."""
    manager, dispatcher, timers = _build_manager(monkeypatch)
    now = datetime.now(UTC)
    wait = TriggerDurationWait("activation", now - timedelta(seconds=31), "delete", {})
    reminder = _duration_reminder(wait)
    manager._reminders[reminder.id] = reminder
    manager._schedule_trigger_duration(reminder, "activation")
    old_callback = timers.calls[-1][1]

    manager._cancel_trigger_duration_timers(reminder.id)
    manager._reminders.pop(reminder.id)

    await old_callback(now)

    assert dispatcher.calls == []
    assert manager._trigger_duration_timers == {}
    assert manager._trigger_duration_timer_tokens == {}
