"""Atomic scheduled-reminder/native-rule update regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.reminders.models import Occurrence, OccurrenceStatus, Reminder, ReminderStatus
from custom_components.reminders.native_manager import NativeReminderManager
from custom_components.reminders.native_scheduled_crud import async_update_native_scheduled
from custom_components.reminders.native_websocket import websocket_update_native_scheduled
from custom_components.reminders.storage import serialize_storage
from custom_components.reminders.triggers.models import TriggerDefinition

from .conftest import FakeDispatcher
from .test_atomic_persistence import FailingStore, SaveError


def _manager(
    monkeypatch: pytest.MonkeyPatch,
    store: FailingStore,
    reminder: Reminder,
) -> NativeReminderManager:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _due: lambda: None,
    )
    monkeypatch.setattr(
        "custom_components.reminders.native_scheduled_crud.async_validate_native_triggers",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "custom_components.reminders.native_scheduled_crud.async_validate_native_conditions",
        AsyncMock(return_value=[]),
    )
    hass = SimpleNamespace(
        states={},
        bus=SimpleNamespace(),
        config=SimpleNamespace(time_zone="UTC"),
        create_task=lambda coroutine, _name=None: coroutine.close(),
    )
    manager = NativeReminderManager(
        hass,
        store,
        FakeDispatcher(),  # type: ignore[arg-type]
    )
    manager._listeners.clear()
    manager._loaded = True
    manager._reminders = {reminder.id: reminder}
    manager._trigger_registry.async_sync = AsyncMock()  # type: ignore[method-assign]
    manager._native_runtime.async_sync = AsyncMock()  # type: ignore[method-assign]
    manager._async_restore_trigger_durations = AsyncMock()  # type: ignore[method-assign]
    return manager


def _reminder(*, waiting: bool = False) -> Reminder:
    now = datetime.now(UTC)
    due = now + timedelta(days=2)
    occurrence = Occurrence(
        id="occurrence-1",
        scheduled_due=due,
        due=due,
        status=(
            OccurrenceStatus.WAITING_FOR_CONTEXT
            if waiting
            else OccurrenceStatus.SCHEDULED
        ),
        context_eligible_at=now if waiting else None,
    )
    legacy = TriggerDefinition.from_dict(
        {"type": "state", "entity_id": "binary_sensor.ready", "to": "on"}
    )
    return Reminder(
        id="scheduled",
        user_id="u1",
        title="Original",
        message="Before",
        due=due,
        scheduled_due=due,
        created_at=now,
        updated_at=now,
        status=(
            ReminderStatus.WAITING_FOR_CONTEXT if waiting else ReminderStatus.PENDING
        ),
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        deliver_when=legacy,
        complete_when=legacy,
        immediate_evaluated=True,
        delivery_triggers=(
            {"trigger": "state", "entity_id": "binary_sensor.old", "to": "on"},
        ),
    )


async def test_failed_atomic_edit_changes_neither_reminder_nor_native_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store failure leaves both halves of an edit at their previous values."""
    original = _reminder()
    initial_storage = serialize_storage({original.id: original}, {})
    store = FailingStore(initial_storage, fail_on_calls={1})
    manager = _manager(monkeypatch, store, original)

    with pytest.raises(SaveError):
        await async_update_native_scheduled(
            manager,
            original.id,
            title="Changed",
            due=original.due + timedelta(hours=1),  # type: ignore[operator]
            delivery_triggers=[
                {"trigger": "state", "entity_id": "binary_sensor.new", "to": "on"}
            ],
            delivery_conditions=[
                {"condition": "state", "entity_id": "input_boolean.ready", "state": "on"}
            ],
            completion_triggers=[
                {"trigger": "event", "event_type": "task_complete"}
            ],
        )

    assert manager._reminders[original.id] == original
    assert store.data == initial_storage
    manager._native_runtime.async_sync.assert_not_awaited()  # type: ignore[attr-defined]
    manager._trigger_registry.async_sync.assert_not_awaited()  # type: ignore[attr-defined]


async def test_successful_atomic_edit_uses_one_write_and_preserves_occurrence_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary fields and native rules commit together without replacing history."""
    original = _reminder()
    store = FailingStore(serialize_storage({original.id: original}, {}))
    manager = _manager(monkeypatch, store, original)
    new_due = original.due + timedelta(hours=3)  # type: ignore[operator]

    updated = await async_update_native_scheduled(
        manager,
        original.id,
        title="Changed",
        message="After",
        due=new_due,
        delivery_triggers=[
            {"trigger": "state", "entity_id": "binary_sensor.new", "to": "on"}
        ],
        delivery_conditions=[
            {"condition": "state", "entity_id": "input_boolean.ready", "state": "on"}
        ],
        completion_triggers=[{"trigger": "event", "event_type": "task_complete"}],
    )

    assert store.calls == 1
    assert updated.title == "Changed"
    assert updated.message == "After"
    assert updated.due == new_due
    assert updated.delivery_triggers[0]["entity_id"] == "binary_sensor.new"
    assert updated.delivery_conditions[0]["entity_id"] == "input_boolean.ready"
    assert updated.completion_triggers[0]["event_type"] == "task_complete"
    assert updated.deliver_when is None
    assert updated.complete_when is None
    assert len(updated.occurrence_history) == 1
    assert updated.occurrence_history[0].id == "occurrence-1"
    assert updated.occurrence_history[0].due == new_due
    manager._native_runtime.async_sync.assert_awaited_once()  # type: ignore[attr-defined]
    manager._trigger_registry.async_sync.assert_awaited_once()  # type: ignore[attr-defined]


async def test_atomic_edit_rearms_existing_native_context_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing/changing rules cannot leave a stale WAITING_FOR_CONTEXT occurrence."""
    original = _reminder(waiting=True)
    store = FailingStore(serialize_storage({original.id: original}, {}))
    manager = _manager(monkeypatch, store, original)

    updated = await async_update_native_scheduled(
        manager,
        original.id,
        delivery_triggers=[],
        delivery_conditions=[],
        completion_triggers=[],
    )

    assert updated.status is ReminderStatus.PENDING
    assert updated.occurrence_history[0].status is OccurrenceStatus.SCHEDULED
    assert updated.occurrence_history[0].context_eligible_at is None
    assert updated.delivery_triggers == ()


def test_atomic_scheduled_websocket_schema_requires_complete_native_snapshot() -> None:
    """The endpoint accepts the panel payload and always receives all native rule roles."""
    schema = websocket_update_native_scheduled._ws_schema
    message = schema(
        {
            "id": 1,
            "type": "reminders/update_native_scheduled",
            "reminder_id": "scheduled",
            "activation_type": "time",
            "title": "Changed",
            "delivery_triggers": [],
            "delivery_conditions": [],
            "completion_triggers": [],
        }
    )
    assert message["activation_type"] == ActivationType.TIME

    with pytest.raises(Exception):
        schema(
            {
                "id": 1,
                "type": "reminders/update_native_scheduled",
                "reminder_id": "scheduled",
                "delivery_triggers": [],
                "delivery_conditions": [],
            }
        )


def test_frontend_routes_existing_scheduled_native_edits_to_atomic_command() -> None:
    """The module chain must bypass the old update + set_native_rules save path."""
    frontend_dir = (
        Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "frontend"
    )
    atomic_source = (frontend_dir / "reminders-panel-atomic.js").read_text(
        encoding="utf-8"
    )
    robust_source = (frontend_dir / "reminders-panel-robust.js").read_text(
        encoding="utf-8"
    )

    assert 'import "./reminders-panel-atomic.js"' in robust_source
    assert '"update_native_scheduled"' in atomic_source
    assert "data.delivery_triggers" in atomic_source
    assert "data.delivery_conditions" in atomic_source
    assert "data.completion_triggers" in atomic_source
    assert 'nativeCall.call(this, "set_native_rules"' not in atomic_source
