"""Tests for model serialization and storage recovery."""

from datetime import UTC, datetime

from custom_components.reminders.models import (
    ActivationType,
    DeliveryPolicy,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
    UserPreferences,
)
from custom_components.reminders.storage import (
    ReminderStore,
    deserialize_storage,
    empty_storage,
    serialize_storage,
)


def test_empty_initial_storage() -> None:
    assert deserialize_storage(None) == ({}, {})
    assert empty_storage() == {"reminders": {}, "users": {}}


async def test_storage_v1_minor_migration_normalizes_shape() -> None:
    store = object.__new__(ReminderStore)
    migrated = await store._async_migrate_func(1, 0, {"reminders": []})
    assert migrated == {"reminders": {}, "users": {}}


async def test_v1_migration_preserves_one_shot_and_preferences() -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    reminder = Reminder(
        id="legacy",
        user_id="u1",
        title="Legacy",
        message="Body",
        due=now,
        created_at=now,
        updated_at=now,
    )
    preferences = UserPreferences(DeliveryPolicy(("phone",), ("notify.phone",)))
    old = serialize_storage({reminder.id: reminder}, {"u1": preferences})
    for field in (
        "recurring",
        "recurrence",
        "scheduled_due",
        "last_occurrence_due",
        "last_occurrence_status",
    ):
        old["reminders"][reminder.id].pop(field, None)
    store = object.__new__(ReminderStore)
    migrated = await store._async_migrate_func(1, 1, old)
    reminders, users = deserialize_storage(migrated)
    assert reminders[reminder.id].title == reminder.title
    assert reminders[reminder.id].message == reminder.message
    assert reminders[reminder.id].recurrence is None
    assert users == {"u1": preferences}


async def test_storage_1_7_migration_converts_activation_duration_wait() -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    reminder = Reminder(
        id="legacy-duration",
        user_id="u1",
        title="Legacy",
        due=now,
        created_at=now,
        updated_at=now,
    )
    old = serialize_storage({reminder.id: reminder}, {})
    raw = old["reminders"][reminder.id]
    raw.pop("trigger_duration_waits")
    raw["trigger_duration_started_at"] = now.isoformat()
    raw["trigger_duration_cause"] = "future_transition"
    raw["trigger_duration_context"] = {"entity_id": "sensor.work"}
    store = object.__new__(ReminderStore)

    migrated = await store._async_migrate_func(1, 7, old)

    waits = migrated["reminders"][reminder.id]["trigger_duration_waits"]
    assert waits == [
        {
            "role": "activation",
            "started_at": now.isoformat(),
            "cause": "future_transition",
            "context": {"entity_id": "sensor.work"},
            "observed_value": None,
        }
    ]


def test_serialize_deserialize_round_trip() -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    reminder = Reminder(
        id="abc",
        user_id="user-1",
        title="Test",
        message="Private body",
        due=now,
        created_at=now,
        updated_at=now,
        delivery_policy=DeliveryPolicy(("phone",), ("notify.phone",)),
    )
    preferences = UserPreferences(DeliveryPolicy(("persistent_notification",)))
    encoded = serialize_storage({reminder.id: reminder}, {"user-1": preferences})
    reminders, users = deserialize_storage(encoded)
    assert reminders == {"abc": reminder}
    assert users == {"user-1": preferences}
    assert reminder.to_dict()["recurring"] is False


def test_external_source_metadata_round_trips_and_legacy_records_default() -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    reminder = Reminder(
        id="external",
        user_id="user-1",
        title="Test",
        due=now,
        created_at=now,
        updated_at=now,
        source="expiry_tracker",
        source_id="item-42",
        source_event="warning_30",
        managed_externally=True,
    )
    encoded = reminder.to_dict()
    assert Reminder.from_dict(encoded) == reminder
    for key in ("source", "source_id", "source_event", "managed_externally"):
        encoded.pop(key)
    legacy = Reminder.from_dict(encoded)
    assert legacy.source is None
    assert legacy.source_id is None
    assert legacy.source_event is None
    assert legacy.managed_externally is False


def test_interrupted_delivery_is_recovered_as_pending() -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    reminder = Reminder(
        id="abc",
        user_id="user-1",
        title="Test",
        due=now,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.DELIVERING,
    )
    reminders, _ = deserialize_storage(serialize_storage({"abc": reminder}, {}))
    # Recovery cannot know whether the provider side effect ran. It retries rather
    # than silently inventing success or dropping a claim that may not have run.
    assert reminders["abc"].status is ReminderStatus.PENDING


def test_interrupted_triggered_delivery_recovers_claim_for_retry() -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    occurrence = Occurrence(
        id="triggered-occurrence",
        due=now,
        scheduled_due=now,
        status=OccurrenceStatus.DELIVERING,
        triggered_at=now,
    )
    reminder = Reminder(
        id="triggered",
        user_id="user-1",
        title="Triggered",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.DELIVERING,
        activation_type=ActivationType.TRIGGER,
        activation_triggers=(
            {"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"},
        ),
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        last_triggered_at=now,
    )

    reminders, _ = deserialize_storage(
        serialize_storage({reminder.id: reminder}, {})
    )
    recovered = reminders[reminder.id]

    assert recovered.status is ReminderStatus.WAITING_FOR_TRIGGER
    assert recovered.due == occurrence.due
    assert recovered.current_occurrence_id == occurrence.id
    assert recovered.occurrence_history[0].status is OccurrenceStatus.SCHEDULED


def test_malformed_record_does_not_discard_valid_records() -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    valid = Reminder(
        id="valid",
        user_id="user-1",
        title="Test",
        due=now,
        created_at=now,
        updated_at=now,
    )
    data = serialize_storage({"valid": valid}, {})
    data["reminders"]["broken"] = {"id": "broken"}
    reminders, _ = deserialize_storage(data)
    assert reminders == {"valid": valid}


def test_malformed_recurrence_isolated_from_other_records() -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    valid = Reminder(
        id="valid",
        user_id="user-1",
        title="Test",
        due=now,
        created_at=now,
        updated_at=now,
    )
    data = serialize_storage({"valid": valid}, {})
    broken = valid.updated(id="broken").to_dict()
    broken["recurrence"] = {
        "frequency": "weekly",
        "interval": 0,
        "timezone": "Europe/Dublin",
        "anchor_local": "2026-07-27T20:00:00",
        "weekdays": ["monday"],
    }
    data["reminders"]["broken"] = broken
    reminders, _ = deserialize_storage(data)
    assert reminders == {"valid": valid}


def test_naive_datetime_is_rejected() -> None:
    now = datetime(2026, 7, 26, 12)
    reminder = Reminder(
        id="abc",
        user_id="user-1",
        title="Test",
        due=now,
        created_at=now,
        updated_at=now,
    )
    try:
        reminder.to_dict()
    except ValueError as err:
        assert "timezone-aware" in str(err)
    else:
        raise AssertionError("Naive datetime was accepted")
