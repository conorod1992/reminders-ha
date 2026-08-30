"""Interoperability contract and external-source synchronization tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.const import LIFECYCLE_SCHEMA_VERSION
from custom_components.reminders.interop_manager import InteropReminderManager
from custom_components.reminders.interop_services import (
    RECONCILE_SOURCE_SCHEMA,
    UPSERT_SCHEMA,
    _validate_existing_kind,
)
from custom_components.reminders.manager import ReminderValidationError
from custom_components.reminders.models import (
    ActivationType,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.recurrence import RecurrenceFrequency, RecurrenceRule

from .conftest import FakeDispatcher, FakeStore


class EventBus:
    """Capture lifecycle events without a Home Assistant runtime."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))


async def _noop_sync(_values: Any) -> None:
    return None


def _reminder(
    reminder_id: str,
    source_id: str,
    *,
    managed_externally: bool = True,
    status: ReminderStatus = ReminderStatus.COMPLETED,
    source: str = "annual_events",
) -> Reminder:
    now = datetime.now(UTC)
    return Reminder(
        id=reminder_id,
        user_id="u1",
        title=reminder_id,
        due=None,
        created_at=now,
        updated_at=now,
        status=status,
        source=source,
        source_id=source_id,
        managed_externally=managed_externally,
    )


async def test_external_upsert_matches_only_managed_owner_source_key() -> None:
    manager = InteropReminderManager(  # type: ignore[arg-type]
        SimpleNamespace(), FakeStore(), FakeDispatcher()
    )
    existing = _reminder("existing", "birthday")
    manager._reminders[existing.id] = existing

    updated_ids: list[str] = []

    async def create() -> str:
        return "created"

    async def update(reminder: Reminder) -> str:
        updated_ids.append(reminder.id)
        return "updated"

    created, result = await manager.async_upsert_external(
        user_id="u1",
        source=" annual_events ",
        source_id=" birthday ",
        create=create,
        update=update,
    )
    assert created is False
    assert result == "updated"
    assert updated_ids == ["existing"]

    manager._reminders[existing.id] = existing.updated(managed_externally=False)
    with pytest.raises(ReminderValidationError, match="not externally managed"):
        await manager.async_upsert_external(
            user_id="u1",
            source="annual_events",
            source_id="birthday",
            create=create,
            update=update,
        )


async def test_reconcile_is_atomic_scoped_and_skips_active_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    manager = InteropReminderManager(  # type: ignore[arg-type]
        SimpleNamespace(bus=bus), FakeStore(), FakeDispatcher()
    )
    keep = _reminder("keep", "keep")
    stale = _reminder("stale", "stale")
    ordinary = _reminder("ordinary", "ordinary", managed_externally=False)
    other_source = _reminder("other-source", "stale", source="other")
    delivering = _reminder("delivering", "delivering", status=ReminderStatus.DELIVERING)
    manager._reminders = {
        item.id: item for item in (keep, stale, ordinary, other_source, delivering)
    }
    monkeypatch.setattr(manager._trigger_registry, "async_sync", _noop_sync)
    monkeypatch.setattr(manager._native_runtime, "async_sync", _noop_sync)

    deleted, skipped = await manager.async_reconcile_external_source(
        user_id="u1",
        source="annual_events",
        keep_source_ids=["keep"],
    )

    assert deleted == ("stale",)
    assert skipped == ("delivering",)
    assert set(manager._reminders) == {
        "keep",
        "ordinary",
        "other-source",
        "delivering",
    }
    assert len(manager._store.saved) == 1
    event_type, payload = bus.events[0]
    assert event_type == "reminders_lifecycle"
    assert payload["action"] == "deleted"
    assert payload["schema_version"] == LIFECYCLE_SCHEMA_VERSION


def test_lifecycle_payload_is_versioned_enriched_and_content_free() -> None:
    bus = EventBus()
    manager = InteropReminderManager(  # type: ignore[arg-type]
        SimpleNamespace(bus=bus), FakeStore(), FakeDispatcher()
    )
    now = datetime.now(UTC)
    occurrence = Occurrence(
        id="occ-1",
        scheduled_due=now,
        due=now,
        status=OccurrenceStatus.ACKNOWLEDGED,
    )
    reminder = Reminder(
        id="external",
        user_id="u1",
        title="Private title",
        message="Private message",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.ACKNOWLEDGED,
        source="expiry_tracker",
        source_id="milk",
        managed_externally=True,
        occurrence_history=(occurrence,),
    )

    manager._fire_lifecycle_event(
        reminder,
        "acknowledged",
        occurrence_id=occurrence.id,
    )

    payload = bus.events[0][1]
    assert payload["schema_version"] == 1
    assert payload["source"] == "expiry_tracker"
    assert payload["source_id"] == "milk"
    assert payload["managed_externally"] is True
    assert payload["activation_type"] == "time"
    assert payload["recurring"] is False
    assert payload["reminder_status"] == "acknowledged"
    assert payload["occurrence_status"] == "acknowledged"
    assert "event_time" in payload
    assert "title" not in payload
    assert "message" not in payload


def test_interop_service_schemas_keep_payload_and_reconciliation_bounded() -> None:
    validated = UPSERT_SCHEMA(
        {
            "source": "annual_events",
            "source_id": "birthday",
            "kind": "one_time",
            "data": {"title": "Birthday", "due": "2026-09-01T12:00:00+00:00"},
        }
    )
    assert validated["kind"] == "one_time"
    assert RECONCILE_SOURCE_SCHEMA({"source": "annual_events"})["keep_source_ids"] == []


def test_upsert_rejects_kind_changes_for_stable_external_key() -> None:
    now = datetime.now(UTC)
    one_time = _reminder("one", "same", status=ReminderStatus.PENDING)
    recurring = one_time.updated(
        recurrence=RecurrenceRule(
            frequency=RecurrenceFrequency.DAILY,
            interval=1,
            timezone="UTC",
            anchor_local=now.replace(tzinfo=None),
        )
    )
    triggered = one_time.updated(
        activation_type=ActivationType.TRIGGER,
        recurrence=None,
    )

    _validate_existing_kind(one_time, "one_time")
    _validate_existing_kind(recurring, "recurring")
    _validate_existing_kind(triggered, "triggered")
    with pytest.raises(ReminderValidationError, match="different kind"):
        _validate_existing_kind(one_time, "recurring")
