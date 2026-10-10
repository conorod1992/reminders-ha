"""Regressions for rescheduling reminders that are waiting on delivery context."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import State

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
    TriggerDurationWait,
)
from custom_components.reminders.storage import deserialize_storage, serialize_storage
from custom_components.reminders.triggers.models import TriggerDefinition

from .conftest import FakeDispatcher, FakeStore


@pytest.fixture
def no_runtime_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        lambda _hass, _entities, _callback: lambda: None,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _due: lambda: None,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_call_later",
        lambda _hass, _delay, _callback: lambda: None,
    )


def _hass(states: dict[str, State] | None = None) -> Any:
    loop = asyncio.get_running_loop()
    return SimpleNamespace(
        states=states or {},
        bus=SimpleNamespace(),
        create_task=lambda coroutine, name=None: loop.create_task(coroutine, name=name),
    )


async def test_due_edit_reuses_waiting_context_occurrence(
    no_runtime_timers: None,
) -> None:
    """Rescheduling a context wait must not leave an orphan active occurrence."""
    manager = ReminderManager(  # type: ignore[arg-type]
        _hass({"person.conor": State("person.conor", "not_home")}),
        FakeStore({"reminders": {}, "users": {}}),
        FakeDispatcher(),
    )
    await manager.async_load()
    due = datetime.now(UTC) + timedelta(minutes=1)
    reminder = await manager.async_create(
        user_id="u1",
        title="Parcel",
        due=due,
        deliver_when={"type": "state", "entity_id": "person.conor", "to": "home"},
        expires_after_seconds=300,
    )
    await manager._async_process_due(due)
    waiting = await manager.async_get(reminder.id)
    assert waiting.status is ReminderStatus.WAITING_FOR_CONTEXT
    occurrence_id = waiting.current_occurrence_id
    assert occurrence_id is not None

    new_due = due + timedelta(hours=2)
    updated = await manager.async_update(reminder.id, due=new_due)

    assert updated.status is ReminderStatus.PENDING
    assert updated.current_occurrence_id == occurrence_id
    assert len(updated.occurrence_history) == 1
    occurrence = updated.occurrence_history[0]
    assert occurrence.id == occurrence_id
    assert occurrence.status is OccurrenceStatus.SCHEDULED
    assert occurrence.scheduled_due == new_due
    assert occurrence.due == new_due
    assert occurrence.context_eligible_at is None
    assert occurrence.expires_at is None
    assert all(
        item.status is not OccurrenceStatus.WAITING_FOR_CONTEXT
        for item in updated.occurrence_history
    )


async def test_due_edit_discards_old_delivery_duration_progress(
    no_runtime_timers: None,
) -> None:
    """A new due time must not inherit a duration wait from the superseded due."""
    now = datetime.now(UTC)
    due = now + timedelta(minutes=1)
    occurrence = Occurrence(
        "waiting",
        due,
        due,
        status=OccurrenceStatus.WAITING_FOR_CONTEXT,
        context_eligible_at=due,
        expires_at=due + timedelta(minutes=5),
    )
    duration_wait = TriggerDurationWait(
        "deliver_when",
        now - timedelta(seconds=10),
        "future_transition",
        {"to": "ready"},
        "ready",
    )
    reminder = Reminder(
        id="duration-reschedule",
        user_id="u1",
        title="Wait for context",
        due=due,
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
        trigger_duration_waits=(duration_wait,),
    )
    manager = ReminderManager(  # type: ignore[arg-type]
        _hass({"sensor.context": State("sensor.context", "ready")}),
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        FakeDispatcher(),
    )
    await manager.async_load()

    new_due = due + timedelta(hours=1)
    updated = await manager.async_update(reminder.id, due=new_due)

    assert updated.trigger_duration_waits == ()
    current = updated.occurrence_history[0]
    assert current.status is OccurrenceStatus.SCHEDULED
    assert current.due == new_due
    assert current.context_eligible_at is None
    assert current.expires_at is None


def test_storage_recovers_legacy_non_current_context_wait() -> None:
    """Stored ghost context waits from older versions are resolved on load."""
    now = datetime.now(UTC)
    orphan = Occurrence(
        "orphan",
        now - timedelta(hours=2),
        now - timedelta(hours=2),
        status=OccurrenceStatus.WAITING_FOR_CONTEXT,
        context_eligible_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    current = Occurrence(
        "current",
        now + timedelta(hours=1),
        now + timedelta(hours=1),
    )
    reminder = Reminder(
        id="legacy-context-ghost",
        user_id="u1",
        title="Legacy ghost",
        due=current.due,
        created_at=now - timedelta(days=1),
        updated_at=now,
        status=ReminderStatus.PENDING,
        current_occurrence_id=current.id,
        occurrence_history=(orphan, current),
    )

    reminders, _users = deserialize_storage(
        serialize_storage({reminder.id: reminder}, {})
    )
    recovered = reminders[reminder.id]
    old = next(item for item in recovered.occurrence_history if item.id == orphan.id)

    assert old.status is OccurrenceStatus.CANCELLED
    assert old.context_eligible_at is None
    assert old.expires_at is None
    assert old.completion_reason == "superseded_context_wait_recovered"
    assert recovered.current_occurrence_id == current.id
