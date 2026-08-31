"""Regression coverage for bounded inputs and terminal recurrence exhaustion."""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from custom_components.reminders.manager import (
    ReminderManager,
    ReminderValidationError,
    _validate_preferences,
)
from custom_components.reminders.models import DeliveryPolicy, Reminder, UserPreferences
from custom_components.reminders.native_manager import _validate_native_rule_resources
from custom_components.reminders.recurrence import (
    MAX_RECURRENCE_INTERVAL,
    RecurrenceError,
    RecurrenceFrequency,
    RecurrenceRule,
    next_due_after,
)

from .conftest import FakeDispatcher, FakeStore


def _disable_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda *_args, **_kwargs: lambda: None,
    )


def test_recurrence_interval_is_bounded() -> None:
    with pytest.raises(RecurrenceError, match="between 1 and"):
        RecurrenceRule(
            frequency=RecurrenceFrequency.DAILY,
            interval=MAX_RECURRENCE_INTERVAL + 1,
            timezone="UTC",
            anchor_local=datetime(2026, 8, 31, 12, 0),
        )


def test_recurrence_exhausts_at_datetime_max_instead_of_overflowing() -> None:
    rule = RecurrenceRule(
        frequency=RecurrenceFrequency.YEARLY,
        interval=1,
        timezone="UTC",
        anchor_local=datetime(9999, 12, 31, 12, 0),
    )
    after = datetime(9999, 12, 31, 12, 0, tzinfo=UTC)
    assert next_due_after(rule, after) is None


async def test_backend_enforces_title_and_message_limits(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_scheduler(monkeypatch)
    manager = ReminderManager(SimpleNamespace(), fake_store, FakeDispatcher())  # type: ignore[arg-type]
    await manager.async_load()
    due = datetime.now(UTC) + timedelta(hours=1)

    with pytest.raises(ReminderValidationError, match="Title must be at most 255"):
        await manager.async_create(user_id="u1", title="T" * 256, due=due)

    reminder = await manager.async_create(
        user_id="u1", title="T" * 255, message="M" * 4000, due=due
    )
    assert len(reminder.title) == 255
    assert len(reminder.message or "") == 4000

    with pytest.raises(ReminderValidationError, match="Message must be at most 4000"):
        await manager.async_update(reminder.id, message="M" * 4001)


def test_native_rule_resources_are_bounded_before_listener_setup() -> None:
    with pytest.raises(ReminderValidationError, match="at most 32 rules"):
        _validate_native_rule_resources(activation_triggers=[{"trigger": "state"}] * 33)

    with pytest.raises(ReminderValidationError, match="at most 64 native rules"):
        _validate_native_rule_resources(
            activation_triggers=[{"trigger": "state"}] * 32,
            completion_triggers=[{"trigger": "state"}] * 32,
            delivery_triggers=[{"trigger": "state"}],
        )

    with pytest.raises(ReminderValidationError, match="65536 serialized bytes"):
        _validate_native_rule_resources(
            activation_triggers=[
                {"trigger": "template", "value_template": "x" * 70_000}
            ]
        )


def test_quiet_hours_rejects_fallback_suppression_overlap() -> None:
    preferences = UserPreferences(
        default_delivery_policy=DeliveryPolicy(("persistent_notification",)),
        quiet_hours_channels=("persistent_notification",),
        quiet_hours_fallback_channels=("persistent_notification",),
    )
    with pytest.raises(ReminderValidationError, match="cannot also be suppressed"):
        _validate_preferences(preferences)


def test_quiet_hours_legacy_overlap_cannot_reenable_suppressed_channel() -> None:
    now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    manager = ReminderManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="UTC")),
        FakeStore(),
        FakeDispatcher(),
    )  # type: ignore[arg-type]
    reminder = Reminder(
        id="r",
        user_id="u",
        title="Private",
        due=now,
        created_at=now,
        updated_at=now,
    )
    preferences = UserPreferences(
        default_delivery_policy=DeliveryPolicy(("persistent_notification",)),
        quiet_hours_enabled=True,
        quiet_hours_start=time(0, 0),
        quiet_hours_end=time(0, 0),
        quiet_hours_channels=("voice",),
        quiet_hours_fallback_channels=("voice", "persistent_notification"),
    )
    policy = DeliveryPolicy(("voice",), voice_targets=("assist_satellite.kitchen",))

    planned, suppressed = manager._delivery_plan(reminder, policy, preferences, now)

    assert suppressed == ("voice",)
    assert planned.channels == ("persistent_notification",)
