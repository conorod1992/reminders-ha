"""Tests for model serialization and storage recovery."""

from datetime import UTC, datetime

from custom_components.reminders.models import (
    DeliveryPolicy,
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
    assert reminders["abc"].status is ReminderStatus.PENDING


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
