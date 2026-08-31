"""Behavior tests for acknowledgement, history, quiet hours, and richer recurrence."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.manager import (
    ReminderManager,
    ReminderValidationError,
    quiet_hours_active,
)
from custom_components.reminders.models import (
    AcknowledgementPolicy,
    DeliveryPolicy,
    Occurrence,
    OccurrenceStatus,
    QuietHoursPolicy,
    Reminder,
    ReminderStatus,
    UserPreferences,
)
from custom_components.reminders.recurrence import (
    MonthlyMode,
    RecurrenceError,
    RecurrenceFrequency,
    RecurrenceRule,
    Weekday,
    next_due_after,
    next_occurrence_after,
)
from custom_components.reminders.storage import (
    ReminderStore,
    deserialize_storage,
    serialize_storage,
)

from .conftest import FakeDispatcher, FakeStore


class Scheduler:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def schedule(self, hass: Any, callback: Any, due: datetime) -> Any:
        self.calls.append(due)
        return lambda: None


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> Scheduler:
    value = Scheduler()
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        value.schedule,
    )
    return value


async def _manager(
    store: FakeStore, dispatcher: FakeDispatcher, scheduler: Scheduler
) -> ReminderManager:
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Dublin"))
    manager = ReminderManager(hass, store, dispatcher)  # type: ignore[arg-type]
    await manager.async_load()
    return manager


async def test_acknowledgement_policy_and_dynamic_user_default(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher, scheduler)
    due = datetime.now(UTC) + timedelta(hours=1)
    inherited = await manager.async_create(user_id="u1", title="Inherited", due=due)
    opt_out = await manager.async_create(
        user_id="u1",
        title="No done",
        due=due,
        acknowledgement_policy=AcknowledgementPolicy.NOT_REQUIRED,
    )
    await manager.async_set_user_preferences(
        "u1",
        DeliveryPolicy(("persistent_notification",)),
        require_acknowledgement=True,
    )
    await manager._async_process_due(due)

    inherited = await manager.async_get(inherited.id)
    opt_out = await manager.async_get(opt_out.id)
    assert inherited.status is ReminderStatus.AWAITING_ACKNOWLEDGEMENT
    assert inherited.occurrence_history[-1].acknowledgement_required is True
    assert opt_out.status is ReminderStatus.DELIVERED
    assert opt_out.occurrence_history[-1].acknowledgement_required is False

    occurrence = await manager.async_acknowledge(inherited.id, acknowledged_by="u1")
    assert occurrence.status is OccurrenceStatus.ACKNOWLEDGED
    assert occurrence.acknowledged_at is not None
    assert occurrence.acknowledged_by == "u1"


async def test_recurring_acknowledges_occurrence_not_series(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    manager = await _manager(fake_store, FakeDispatcher(), scheduler)
    anchor = datetime.now().replace(tzinfo=None) + timedelta(hours=2)
    rule = RecurrenceRule(RecurrenceFrequency.DAILY, 1, "Europe/Dublin", anchor)
    reminder = await manager.async_create_recurring(
        user_id="u1",
        title="Daily",
        recurrence=rule,
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
    )
    first_due = reminder.due
    await manager._async_process_due(first_due)
    advanced = await manager.async_get(reminder.id)
    awaiting = [
        item
        for item in advanced.occurrence_history
        if item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
    ]
    assert advanced.status is ReminderStatus.PENDING
    assert advanced.due > first_due
    await manager.async_acknowledge(
        reminder.id, occurrence_id=awaiting[0].id, acknowledged_by="u1"
    )
    final = await manager.async_get(reminder.id)
    assert final.status is ReminderStatus.PENDING
    assert final.due == advanced.due
    assert any(
        item.status is OccurrenceStatus.ACKNOWLEDGED
        for item in final.occurrence_history
    )


async def test_occurrence_history_persists_and_is_bounded(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    history = tuple(
        Occurrence(
            id=str(index),
            scheduled_due=now - timedelta(days=30 - index),
            due=now - timedelta(days=30 - index),
            status=OccurrenceStatus.DELIVERED,
            delivered_at=now - timedelta(days=30 - index),
            succeeded_channels=("persistent_notification",),
        )
        for index in range(20)
    )
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="History",
        due=now + timedelta(days=1),
        created_at=now - timedelta(days=40),
        updated_at=now,
        occurrence_history=history,
    )
    store = FakeStore(serialize_storage({reminder.id: reminder}, {}))
    manager = await _manager(store, FakeDispatcher(), scheduler)
    await manager.async_set_user_preferences(
        "u1",
        DeliveryPolicy(("persistent_notification",)),
        history_retention_days=365,
        history_max_occurrences=10,
    )
    bounded = await manager.async_get(reminder.id)
    assert len(bounded.occurrence_history) == 10
    restored, _ = deserialize_storage(store.data)
    assert restored[reminder.id].occurrence_history == bounded.occurrence_history


@pytest.mark.parametrize(
    ("value", "expected"),
    [(time(23, 30), True), (time(6, 59), True), (time(12), False)],
)
def test_quiet_hours_cross_midnight(value: time, expected: bool) -> None:
    assert quiet_hours_active(value, time(23), time(7)) is expected


async def test_quiet_hours_suppress_voice_and_add_fallback(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher, scheduler)
    await manager.async_set_user_preferences(
        "u1",
        DeliveryPolicy(("voice",), voice_targets=("assist_satellite.kitchen",)),
        quiet_hours_enabled=True,
        quiet_hours_start="23:00",
        quiet_hours_end="07:00",
        quiet_hours_channels=("voice",),
        quiet_hours_fallback_channels=("persistent_notification",),
    )
    due = datetime(2027, 1, 2, 0, 30, tzinfo=UTC)
    reminder = await manager.async_create(user_id="u1", title="Quiet", due=due)
    await manager._async_process_due(due)
    _, policy = dispatcher.calls[-1]
    assert policy.channels == ("persistent_notification",)
    delivered = await manager.async_get(reminder.id)
    assert delivered.occurrence_history[-1].suppressed_channels == ("voice",)


def test_quiet_hours_phone_fallback_uses_default_targets() -> None:
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Dublin"))
    manager = ReminderManager(hass, FakeStore(), FakeDispatcher())  # type: ignore[arg-type]
    due = datetime(2027, 1, 2, 0, 30, tzinfo=UTC)
    reminder = Reminder(
        id="quiet-phone",
        user_id="u1",
        title="Quiet phone",
        due=due,
        created_at=due - timedelta(hours=1),
        updated_at=due - timedelta(hours=1),
    )
    policy = DeliveryPolicy(("voice",), voice_targets=("assist_satellite.kitchen",))
    preferences = UserPreferences(
        default_delivery_policy=DeliveryPolicy(
            ("phone",), mobile_app_services=("notify.mobile_app_phone",)
        ),
        quiet_hours_enabled=True,
        quiet_hours_start=time(23),
        quiet_hours_end=time(7),
        quiet_hours_channels=("voice",),
        quiet_hours_fallback_channels=("phone",),
    )

    delivery, suppressed = manager._delivery_plan(reminder, policy, preferences, due)

    assert suppressed == ("voice",)
    assert delivery.channels == ("phone",)
    assert delivery.mobile_app_services == ("notify.mobile_app_phone",)
    assert delivery.notify_targets == ()


def test_quiet_hours_voice_fallback_uses_default_targets() -> None:
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Dublin"))
    manager = ReminderManager(hass, FakeStore(), FakeDispatcher())  # type: ignore[arg-type]
    due = datetime(2027, 1, 2, 0, 30, tzinfo=UTC)
    reminder = Reminder(
        id="quiet-voice",
        user_id="u1",
        title="Quiet voice",
        due=due,
        created_at=due - timedelta(hours=1),
        updated_at=due - timedelta(hours=1),
    )
    policy = DeliveryPolicy(
        ("phone",), mobile_app_services=("notify.mobile_app_phone",)
    )
    preferences = UserPreferences(
        default_delivery_policy=DeliveryPolicy(
            ("voice",), voice_targets=("assist_satellite.bedroom",)
        ),
        quiet_hours_enabled=True,
        quiet_hours_start=time(23),
        quiet_hours_end=time(7),
        quiet_hours_channels=("phone",),
        quiet_hours_fallback_channels=("voice",),
    )

    delivery, suppressed = manager._delivery_plan(reminder, policy, preferences, due)

    assert suppressed == ("phone",)
    assert delivery.channels == ("voice",)
    assert delivery.voice_targets == ("assist_satellite.bedroom",)


async def test_urgent_reminder_ignores_quiet_hours(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher, scheduler)
    await manager.async_set_user_preferences(
        "u1",
        DeliveryPolicy(("voice",), voice_targets=("assist_satellite.kitchen",)),
        quiet_hours_enabled=True,
    )
    due = datetime(2027, 1, 2, 0, 30, tzinfo=UTC)
    await manager.async_create(
        user_id="u1",
        title="Urgent",
        due=due,
        quiet_hours_policy=QuietHoursPolicy.IGNORE,
    )
    await manager._async_process_due(due)
    assert dispatcher.calls[-1][1].channels == ("voice",)


def test_yearly_nth_last_weekday_and_last_day_rules() -> None:
    yearly = RecurrenceRule(
        RecurrenceFrequency.YEARLY,
        1,
        "Europe/Dublin",
        datetime(2024, 2, 29, 9),
    )
    assert next_occurrence_after(yearly, yearly.anchor_utc) == datetime(
        2028, 2, 29, 9, tzinfo=UTC
    )
    nth = RecurrenceRule(
        RecurrenceFrequency.MONTHLY,
        1,
        "Europe/Dublin",
        datetime(2026, 8, 17, 9),
        monthly_mode=MonthlyMode.NTH_WEEKDAY,
        monthly_weekday=Weekday.MONDAY,
        monthly_week=3,
    )
    assert next_occurrence_after(nth, nth.anchor_utc) == datetime(
        2026, 9, 21, 8, tzinfo=UTC
    )
    last_weekday = RecurrenceRule(
        RecurrenceFrequency.MONTHLY,
        1,
        "Europe/Dublin",
        datetime(2026, 8, 31, 9),
        monthly_mode=MonthlyMode.LAST_WEEKDAY,
        monthly_weekday=Weekday.MONDAY,
    )
    assert next_occurrence_after(last_weekday, last_weekday.anchor_utc) == datetime(
        2026, 9, 28, 8, tzinfo=UTC
    )
    last_day = RecurrenceRule(
        RecurrenceFrequency.MONTHLY,
        1,
        "Europe/Dublin",
        datetime(2026, 1, 31, 9),
        monthly_mode=MonthlyMode.LAST_DAY,
    )
    assert next_occurrence_after(last_day, last_day.anchor_utc) == datetime(
        2026, 2, 28, 9, tzinfo=UTC
    )


def test_recurrence_end_date_and_count_are_anchored() -> None:
    count_limited = RecurrenceRule(
        RecurrenceFrequency.DAILY,
        1,
        "UTC",
        datetime(2026, 1, 1, 9),
        occurrence_count=3,
    )
    third = datetime(2026, 1, 3, 9, tzinfo=UTC)
    assert next_due_after(count_limited, third) is None
    with pytest.raises(RecurrenceError, match="no more"):
        next_occurrence_after(count_limited, third)

    date_limited = RecurrenceRule(
        RecurrenceFrequency.DAILY,
        1,
        "UTC",
        datetime(2026, 1, 1, 9),
        end_date=date(2026, 1, 2),
    )
    assert next_due_after(date_limited, datetime(2026, 1, 2, 9, tzinfo=UTC)) is None

    leap_limited = RecurrenceRule(
        RecurrenceFrequency.YEARLY,
        1,
        "UTC",
        datetime(2024, 2, 29, 9),
        occurrence_count=2,
    )
    assert next_due_after(leap_limited, leap_limited.anchor_utc) == datetime(
        2028, 2, 29, 9, tzinfo=UTC
    )


async def test_storage_1_2_migration_adds_safe_defaults() -> None:
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    raw = serialize_storage(
        {
            "legacy": Reminder(
                id="legacy",
                user_id="u1",
                title="Legacy",
                due=now,
                created_at=now,
                updated_at=now,
            )
        },
        {},
    )
    for field in (
        "acknowledgement_policy",
        "quiet_hours_policy",
        "current_occurrence_id",
        "current_occurrence_number",
        "occurrence_history",
    ):
        raw["reminders"]["legacy"].pop(field, None)
    store = object.__new__(ReminderStore)
    migrated = await store._async_migrate_func(1, 2, raw)
    assert migrated["reminders"]["legacy"]["acknowledgement_policy"] == "default"
    assert migrated["reminders"]["legacy"]["occurrence_history"]


@pytest.mark.parametrize(
    ("fallback_channels", "policy", "message"),
    [
        (
            ("phone",),
            DeliveryPolicy(("persistent_notification",)),
            "phone fallback needs at least one default notify target",
        ),
        (
            ("voice",),
            DeliveryPolicy(("persistent_notification",)),
            "voice fallback needs at least one default Assist satellite",
        ),
    ],
)
async def test_quiet_hours_fallback_rejects_missing_default_targets(
    fake_store: FakeStore,
    scheduler: Scheduler,
    fallback_channels: tuple[str, ...],
    policy: DeliveryPolicy,
    message: str,
) -> None:
    manager = await _manager(fake_store, FakeDispatcher(), scheduler)

    with pytest.raises(ReminderValidationError, match=message):
        await manager.async_set_user_preferences(
            "u1",
            policy,
            quiet_hours_enabled=True,
            quiet_hours_channels=("voice",),
            quiet_hours_fallback_channels=fallback_channels,
        )

    assert (await manager.async_get_user_preferences("u1")).configured is False


@pytest.mark.parametrize(
    ("fallback_channels", "policy"),
    [
        (
            ("phone",),
            DeliveryPolicy(
                ("persistent_notification",),
                mobile_app_services=("notify.mobile_app_phone",),
            ),
        ),
        (
            ("voice",),
            DeliveryPolicy(
                ("persistent_notification",),
                voice_targets=("assist_satellite.bedroom",),
            ),
        ),
    ],
)
async def test_quiet_hours_fallback_accepts_configured_default_targets(
    fake_store: FakeStore,
    scheduler: Scheduler,
    fallback_channels: tuple[str, ...],
    policy: DeliveryPolicy,
) -> None:
    manager = await _manager(fake_store, FakeDispatcher(), scheduler)

    preferences = await manager.async_set_user_preferences(
        "u1",
        policy,
        quiet_hours_enabled=True,
        quiet_hours_channels=("voice",),
        quiet_hours_fallback_channels=fallback_channels,
    )

    assert preferences.quiet_hours_fallback_channels == fallback_channels
