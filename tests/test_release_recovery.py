"""Pre-release recovery regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    ActivationType,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.storage import serialize_storage
from custom_components.reminders.triggers.models import TriggerDefinition

from .conftest import FakeDispatcher, FakeStore


async def test_interrupted_trigger_delivery_is_reclaimed_on_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable trigger delivery claim must not be lost across a restart."""

    def schedule(_hass: Any, _callback: Any, _when: datetime) -> Any:
        return lambda: None

    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        schedule,
    )

    now = datetime.now(UTC)
    due = now - timedelta(minutes=1)
    occurrence = Occurrence(
        id="triggered-occurrence",
        scheduled_due=due,
        due=due,
        status=OccurrenceStatus.DELIVERING,
        triggered_at=due,
    )
    reminder = Reminder(
        id="triggered",
        user_id="u1",
        title="Recovered trigger",
        due=due,
        created_at=due - timedelta(hours=1),
        updated_at=due,
        status=ReminderStatus.DELIVERING,
        activation_type=ActivationType.TRIGGER,
        trigger=TriggerDefinition.from_dict(
            {"type": "named", "trigger_id": "recover_me"}
        ),
        trigger_summary="Named trigger recover_me",
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        last_triggered_at=due,
    )
    store = FakeStore(serialize_storage({reminder.id: reminder}, {}))
    dispatcher = FakeDispatcher()
    manager = ReminderManager(  # type: ignore[arg-type]
        SimpleNamespace(states={}, bus=SimpleNamespace()), store, dispatcher
    )

    await manager.async_load()

    recovered = await manager.async_get(reminder.id)
    assert len(dispatcher.calls) == 1
    assert recovered.status is ReminderStatus.COMPLETED
    assert all(
        item.status is not OccurrenceStatus.DELIVERING
        for item in recovered.occurrence_history
    )
