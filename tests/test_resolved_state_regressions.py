"""Regression tests for resolved reminder state correctness."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.native_automation import NativeAutomationRuntime
from custom_components.reminders.native_manager import NativeReminderManager
from custom_components.reminders.native_scheduled_crud import (
    async_update_native_scheduled,
)
from custom_components.reminders.recurrence import RecurrenceFrequency, RecurrenceRule
from custom_components.reminders.storage import serialize_storage
from custom_components.reminders.triggers.models import TriggerDefinition

from .conftest import FakeDispatcher, FakeStore


class Bus:
    """Minimal event bus used by manager tests."""

    def async_listen(self, _event_type: str, _callback: Any) -> Any:
        return lambda: None


@pytest.fixture
def runtime_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests focused on state transitions rather than HA listeners."""
    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        lambda _hass, _entities, _callback: lambda: None,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _due: lambda: None,
    )


@pytest.fixture
def native_runtime_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable native listener attachment while exercising native manager logic."""

    async def async_sync(_self: NativeAutomationRuntime, _reminders: Any) -> None:
        return None

    monkeypatch.setattr(NativeAutomationRuntime, "async_sync", async_sync)


async def _manager(
    reminder: Reminder, dispatcher: FakeDispatcher | None = None
) -> tuple[ReminderManager, FakeDispatcher]:
    delivery = dispatcher or FakeDispatcher()
    hass = SimpleNamespace(
        states={},
        bus=Bus(),
        config=SimpleNamespace(time_zone="UTC"),
        create_task=lambda coroutine, _name=None: asyncio.create_task(coroutine),
    )
    manager = ReminderManager(  # type: ignore[arg-type]
        hass,
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        delivery,
    )
    await manager.async_load()
    return manager, delivery


async def _native_manager(
    reminder: Reminder, dispatcher: FakeDispatcher | None = None
) -> tuple[NativeReminderManager, FakeDispatcher]:
    delivery = dispatcher or FakeDispatcher()
    hass = SimpleNamespace(
        states={},
        bus=Bus(),
        config=SimpleNamespace(time_zone="UTC"),
        create_task=lambda coroutine, _name=None: asyncio.create_task(coroutine),
    )
    manager = NativeReminderManager(  # type: ignore[arg-type]
        hass,
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        delivery,
    )
    await manager.async_load()
    return manager, delivery


def _resolved_reminder(
    status: ReminderStatus, occurrence_status: OccurrenceStatus
) -> Reminder:
    now = datetime.now(UTC)
    due = now - timedelta(hours=1)
    delivered_at = due + timedelta(minutes=1)
    occurrence = Occurrence(
        id="occurrence",
        scheduled_due=due,
        due=due,
        status=occurrence_status,
        delivered_at=delivered_at,
        acknowledgement_required=(occurrence_status is OccurrenceStatus.ACKNOWLEDGED),
        acknowledged_at=(
            delivered_at + timedelta(minutes=1)
            if occurrence_status is OccurrenceStatus.ACKNOWLEDGED
            else None
        ),
        completed_at=(
            delivered_at + timedelta(minutes=1)
            if occurrence_status is OccurrenceStatus.COMPLETED
            else None
        ),
        delivery_errors=("original-error",),
    )
    return Reminder(
        id="resolved",
        user_id="u1",
        title="Original",
        due=due,
        created_at=due - timedelta(days=1),
        updated_at=delivered_at,
        status=status,
        delivered_at=delivered_at,
        delivery_errors=("original-error",),
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
    )


@pytest.mark.parametrize(
    ("status", "occurrence_status"),
    [
        (ReminderStatus.DELIVERED, OccurrenceStatus.DELIVERED),
        (ReminderStatus.ACKNOWLEDGED, OccurrenceStatus.ACKNOWLEDGED),
        (ReminderStatus.COMPLETED, OccurrenceStatus.COMPLETED),
        (ReminderStatus.FAILED, OccurrenceStatus.FAILED),
    ],
)
async def test_metadata_edit_does_not_rearm_resolved_timed_reminder(
    runtime_stubs: None,
    status: ReminderStatus,
    occurrence_status: OccurrenceStatus,
) -> None:
    original = _resolved_reminder(status, occurrence_status)
    manager, dispatcher = await _manager(original)

    updated = await manager.async_update(original.id, title="Edited")

    assert updated.title == "Edited"
    assert updated.status is status
    assert updated.current_occurrence_id == original.current_occurrence_id
    assert updated.occurrence_history == original.occurrence_history
    assert updated.delivered_at == original.delivered_at
    assert updated.delivery_errors == original.delivery_errors
    assert dispatcher.calls == []


async def test_metadata_edit_does_not_redeliver_awaiting_acknowledgement(
    runtime_stubs: None,
) -> None:
    now = datetime.now(UTC)
    due = now - timedelta(hours=1)
    occurrence = Occurrence(
        id="awaiting",
        scheduled_due=due,
        due=due,
        status=OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
        delivered_at=due + timedelta(minutes=1),
        acknowledgement_required=True,
    )
    reminder = Reminder(
        id="awaiting",
        user_id="u1",
        title="Original",
        due=due,
        created_at=due - timedelta(days=1),
        updated_at=due,
        status=ReminderStatus.AWAITING_ACKNOWLEDGEMENT,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
    )
    manager, dispatcher = await _manager(reminder)

    updated = await manager.async_update(reminder.id, title="Edited")

    assert updated.status is ReminderStatus.AWAITING_ACKNOWLEDGEMENT
    assert updated.occurrence_history == reminder.occurrence_history
    assert dispatcher.calls == []


async def test_explicit_due_change_still_rearms_resolved_timed_reminder(
    runtime_stubs: None,
) -> None:
    original = _resolved_reminder(ReminderStatus.COMPLETED, OccurrenceStatus.COMPLETED)
    manager, dispatcher = await _manager(original)
    new_due = datetime.now(UTC) + timedelta(hours=2)

    updated = await manager.async_update(original.id, due=new_due)

    assert updated.status is ReminderStatus.PENDING
    assert updated.due == new_due
    assert updated.current_occurrence_id != original.current_occurrence_id
    assert updated.occurrence_history[0] == original.occurrence_history[0]
    assert updated.occurrence_history[-1].status is OccurrenceStatus.SCHEDULED
    assert updated.delivered_at is None
    assert updated.delivery_errors == ()
    assert dispatcher.calls == []


async def test_recurring_automatic_completion_records_completed_series_status(
    runtime_stubs: None,
) -> None:
    now = datetime.now(UTC)
    due = now + timedelta(minutes=10)
    occurrence = Occurrence("current", due, due)
    rule = RecurrenceRule(RecurrenceFrequency.DAILY, 1, "UTC", due.replace(tzinfo=None))
    reminder = Reminder(
        id="recurring",
        user_id="u1",
        title="Recurring",
        due=due,
        scheduled_due=due,
        created_at=now,
        updated_at=now,
        recurrence=rule,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        complete_when=TriggerDefinition.from_dict(
            {"type": "named", "trigger_id": "done"}
        ),
    )
    manager, _ = await _manager(reminder)

    outcome = await manager.async_complete_automatically(
        reminder.id, cause="test", context={}
    )
    updated = await manager.async_get(reminder.id)

    assert outcome == "completed"
    assert updated.status is ReminderStatus.PENDING
    assert updated.last_occurrence_status is ReminderStatus.COMPLETED
    assert updated.occurrence_history[0].status is OccurrenceStatus.COMPLETED


async def test_native_automatic_completion_records_completed_series_status(
    runtime_stubs: None,
    native_runtime_stub: None,
) -> None:
    now = datetime.now(UTC)
    due = now + timedelta(minutes=10)
    occurrence = Occurrence("current", due, due)
    rule = RecurrenceRule(RecurrenceFrequency.DAILY, 1, "UTC", due.replace(tzinfo=None))
    reminder = Reminder(
        id="native-recurring",
        user_id="u1",
        title="Native recurring",
        due=due,
        scheduled_due=due,
        created_at=now,
        updated_at=now,
        recurrence=rule,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        completion_triggers=({"trigger": "event", "event_type": "done"},),
    )
    manager, _ = await _native_manager(reminder)

    outcome = await manager.async_complete_native(reminder.id, cause="test", context={})
    updated = await manager.async_get(reminder.id)

    assert outcome == "completed"
    assert updated.status is ReminderStatus.PENDING
    assert updated.last_occurrence_status is ReminderStatus.COMPLETED
    assert updated.occurrence_history[0].status is OccurrenceStatus.COMPLETED


async def test_native_scheduled_metadata_edit_preserves_resolved_state(
    runtime_stubs: None,
    native_runtime_stub: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def validate(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        "custom_components.reminders.native_scheduled_crud.async_validate_native_triggers",
        validate,
    )
    monkeypatch.setattr(
        "custom_components.reminders.native_scheduled_crud.async_validate_native_conditions",
        validate,
    )
    original = _resolved_reminder(ReminderStatus.COMPLETED, OccurrenceStatus.COMPLETED)
    manager, dispatcher = await _native_manager(original)

    updated = await async_update_native_scheduled(
        manager,
        original.id,
        delivery_triggers=[],
        delivery_conditions=[],
        completion_triggers=[],
        title="Edited",
    )

    assert updated.title == "Edited"
    assert updated.status is ReminderStatus.COMPLETED
    assert updated.occurrence_history == original.occurrence_history
    assert updated.delivered_at == original.delivered_at
    assert updated.delivery_errors == original.delivery_errors
    assert dispatcher.calls == []
