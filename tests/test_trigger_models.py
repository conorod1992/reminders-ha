"""Validation, canonicalisation, storage, and lifecycle tests for triggers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.reminders.models import (
    ActivationType,
    Reminder,
    ReminderStatus,
    TriggerRepeatPolicy,
)
from custom_components.reminders.storage import deserialize_storage, serialize_storage
from custom_components.reminders.triggers.models import (
    TriggerDefinition,
    TriggerValidationError,
    canonical_trigger_key,
    event_data_matches,
)


def test_state_trigger_validation_and_canonicalisation() -> None:
    first = TriggerDefinition.from_dict(
        {"type": "STATE", "entity_id": "Sensor.Work", "to": "Finished"}
    )
    second = TriggerDefinition.from_dict(
        {"to": "Finished", "entity_id": "sensor.work", "type": "state"}
    )
    assert first == second
    assert canonical_trigger_key(first) == canonical_trigger_key(second)
    assert first.to_dict() == {
        "type": "state",
        "entity_id": "sensor.work",
        "to": "Finished",
    }
    with pytest.raises(TriggerValidationError, match="needs from, to"):
        TriggerDefinition.from_dict({"type": "state", "entity_id": "sensor.work"})
    with pytest.raises(TriggerValidationError, match="Unsupported trigger fields"):
        TriggerDefinition.from_dict(
            {"type": "state", "entity_id": "sensor.work", "to": "on", "bad": 1}
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "numeric_state", "entity_id": "sensor.value"},
        {
            "type": "numeric_state",
            "entity_id": "sensor.value",
            "above": 10,
            "below": 5,
        },
        {
            "type": "zone",
            "entity_id": "sensor.not_a_tracker",
            "zone_entity_id": "zone.home",
            "event": "enter",
        },
        {"type": "event", "event_type": "Not Valid"},
        {"type": "named", "trigger_id": "Bad ID"},
    ],
)
def test_invalid_trigger_definitions(raw: dict[str, object]) -> None:
    with pytest.raises(TriggerValidationError):
        TriggerDefinition.from_dict(raw)


def test_all_trigger_types_and_event_subset_match() -> None:
    numeric = TriggerDefinition.from_dict(
        {
            "type": "numeric_state",
            "entity_id": "sensor.toner",
            "below": 10,
            "attribute": "level",
            "for_seconds": 30,
        }
    )
    zone = TriggerDefinition.from_dict(
        {
            "type": "zone",
            "entity_id": "person.conor",
            "zone_entity_id": "zone.woodies",
            "event": "enter",
        }
    )
    event = TriggerDefinition.from_dict(
        {
            "type": "event",
            "event_type": "jarvis_opportunity",
            "event_data": {"context": {"type": "printing"}},
        }
    )
    named = TriggerDefinition.from_dict(
        {"type": "named", "trigger_id": "Printing_Started"}
    )
    assert numeric.below == 10
    assert zone.zone_entity_id == "zone.woodies"
    assert named.trigger_id == "printing_started"
    assert event_data_matches(
        event.event_data or {},
        {"context": {"type": "printing", "private": "not persisted"}, "extra": 1},
    )


def test_event_data_limits_and_json_serialisability() -> None:
    with pytest.raises(TriggerValidationError, match="JSON"):
        TriggerDefinition.from_dict(
            {
                "type": "event",
                "event_type": "test_event",
                "event_data": {"bad": object()},
            }
        )
    with pytest.raises(TriggerValidationError, match="too large"):
        TriggerDefinition.from_dict(
            {
                "type": "event",
                "event_type": "test_event",
                "event_data": {"value": "x" * 5000},
            }
        )


def test_triggered_reminder_storage_round_trip() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    reminder = Reminder(
        id="triggered",
        user_id="u1",
        title="Printer",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        trigger=TriggerDefinition.from_dict(
            {"type": "named", "trigger_id": "printing_started"}
        ),
        trigger_summary="When named trigger printing_started fires",
        repeat_policy=TriggerRepeatPolicy.EVERY_TRIGGER,
        cooldown_seconds=300,
    )
    restored, _ = deserialize_storage(serialize_storage({reminder.id: reminder}, {}))
    assert restored == {reminder.id: reminder}


def test_malformed_trigger_isolated_from_scheduled_reminder() -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    valid = Reminder(
        id="valid",
        user_id="u1",
        title="Scheduled",
        due=now,
        created_at=now,
        updated_at=now,
    )
    data = serialize_storage({valid.id: valid}, {})
    broken = valid.updated(id="broken", due=None).to_dict()
    broken.update(
        activation_type="trigger",
        trigger={"type": "named", "trigger_id": "Bad ID"},
    )
    data["reminders"]["broken"] = broken
    restored, _ = deserialize_storage(data)
    assert restored == {valid.id: valid}
