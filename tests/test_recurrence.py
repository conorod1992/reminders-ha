"""Pure recurrence calculation tests."""

from datetime import UTC, datetime

import pytest

from custom_components.reminders.recurrence import (
    RecurrenceError,
    RecurrenceFrequency,
    RecurrenceRule,
    Weekday,
    first_due,
    next_occurrence_after,
    resolve_local_datetime,
)


def rule(
    frequency: RecurrenceFrequency,
    anchor: datetime,
    *,
    interval: int = 1,
    weekdays: tuple[Weekday, ...] = (),
    day: int | None = None,
) -> RecurrenceRule:
    return RecurrenceRule(
        frequency=frequency,
        interval=interval,
        timezone="Europe/Dublin",
        anchor_local=anchor,
        weekdays=weekdays,
        day_of_month=day,
    )


@pytest.mark.parametrize("interval", [1, 2, 5])
def test_daily_interval_preserves_phase(interval: int) -> None:
    recurrence = rule(
        RecurrenceFrequency.DAILY, datetime(2026, 8, 1, 18, 30), interval=interval
    )
    expected = datetime(2026, 8, 1 + interval, 17, 30, tzinfo=UTC)
    assert next_occurrence_after(recurrence, recurrence.anchor_utc) == expected


def test_past_anchor_selects_next_occurrence_without_replay() -> None:
    recurrence = rule(RecurrenceFrequency.DAILY, datetime(2026, 8, 1, 8), interval=2)
    assert first_due(recurrence, datetime(2026, 8, 6, 12, tzinfo=UTC)) == datetime(
        2026, 8, 7, 7, tzinfo=UTC
    )


def test_future_anchor_is_first_due() -> None:
    recurrence = rule(RecurrenceFrequency.DAILY, datetime(2026, 8, 10, 8))
    assert first_due(recurrence, datetime(2026, 8, 1, tzinfo=UTC)) == datetime(
        2026, 8, 10, 7, tzinfo=UTC
    )


def test_weekly_monday() -> None:
    recurrence = rule(
        RecurrenceFrequency.WEEKLY,
        datetime(2026, 8, 3, 20),
        weekdays=(Weekday.MONDAY,),
    )
    assert next_occurrence_after(recurrence, recurrence.anchor_utc) == datetime(
        2026, 8, 10, 19, tzinfo=UTC
    )


def test_late_delivery_does_not_shift_weekly_phase() -> None:
    recurrence = rule(
        RecurrenceFrequency.WEEKLY,
        datetime(2026, 8, 3, 20),
        weekdays=(Weekday.MONDAY,),
    )
    late_delivery = datetime(2026, 8, 3, 19, 17, tzinfo=UTC)
    assert next_occurrence_after(recurrence, late_delivery) == datetime(
        2026, 8, 10, 19, tzinfo=UTC
    )


def test_weekly_multiple_days_share_active_week() -> None:
    recurrence = rule(
        RecurrenceFrequency.WEEKLY,
        datetime(2026, 8, 4, 9),
        interval=2,
        weekdays=(Weekday.TUESDAY, Weekday.THURSDAY),
    )
    tuesday = recurrence.anchor_utc
    thursday = next_occurrence_after(recurrence, tuesday)
    next_tuesday = next_occurrence_after(recurrence, thursday)
    assert thursday == datetime(2026, 8, 6, 8, tzinfo=UTC)
    assert next_tuesday == datetime(2026, 8, 18, 8, tzinfo=UTC)
    assert next_occurrence_after(recurrence, next_tuesday) == datetime(
        2026, 8, 20, 8, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("interval", "anchor", "weekday", "expected"),
    [
        (
            2,
            datetime(2026, 7, 27, 20),
            Weekday.MONDAY,
            datetime(2026, 8, 10, 19, tzinfo=UTC),
        ),
        (
            3,
            datetime(2026, 7, 31, 17, 30),
            Weekday.FRIDAY,
            datetime(2026, 8, 21, 16, 30, tzinfo=UTC),
        ),
    ],
)
def test_every_x_active_weeks(
    interval: int,
    anchor: datetime,
    weekday: Weekday,
    expected: datetime,
) -> None:
    recurrence = rule(
        RecurrenceFrequency.WEEKLY,
        anchor,
        interval=interval,
        weekdays=(weekday,),
    )
    assert next_occurrence_after(recurrence, recurrence.anchor_utc) == expected


def test_inconsistent_weekday_rejected() -> None:
    with pytest.raises(RecurrenceError, match="weekday"):
        rule(
            RecurrenceFrequency.WEEKLY,
            datetime(2026, 8, 4, 20),
            weekdays=(Weekday.MONDAY,),
        )


@pytest.mark.parametrize(
    ("day", "anchor", "expected"),
    [
        (1, datetime(2026, 8, 1, 9), datetime(2026, 9, 1, 8, tzinfo=UTC)),
        (15, datetime(2026, 8, 15, 18), datetime(2026, 9, 15, 17, tzinfo=UTC)),
    ],
)
def test_monthly_common_days(day: int, anchor: datetime, expected: datetime) -> None:
    recurrence = rule(RecurrenceFrequency.MONTHLY, anchor, day=day)
    assert next_occurrence_after(recurrence, recurrence.anchor_utc) == expected


def test_every_two_months_phase() -> None:
    recurrence = rule(
        RecurrenceFrequency.MONTHLY,
        datetime(2026, 8, 10, 12),
        interval=2,
        day=10,
    )
    assert next_occurrence_after(recurrence, recurrence.anchor_utc) == datetime(
        2026, 10, 10, 11, tzinfo=UTC
    )


def test_day_31_skips_invalid_months_without_phase_drift() -> None:
    recurrence = rule(RecurrenceFrequency.MONTHLY, datetime(2027, 1, 31, 9), day=31)
    march = next_occurrence_after(recurrence, recurrence.anchor_utc)
    may = next_occurrence_after(recurrence, march)
    assert march == datetime(2027, 3, 31, 8, tzinfo=UTC)
    assert may == datetime(2027, 5, 31, 8, tzinfo=UTC)


def test_daily_wall_clock_survives_spring_dst() -> None:
    recurrence = rule(RecurrenceFrequency.DAILY, datetime(2026, 3, 28, 8))
    first = recurrence.anchor_utc
    second = next_occurrence_after(recurrence, first)
    third = next_occurrence_after(recurrence, second)
    assert first == datetime(2026, 3, 28, 8, tzinfo=UTC)
    assert second == datetime(2026, 3, 29, 7, tzinfo=UTC)
    assert third == datetime(2026, 3, 30, 7, tzinfo=UTC)


def test_daily_wall_clock_survives_autumn_dst() -> None:
    recurrence = rule(RecurrenceFrequency.DAILY, datetime(2026, 10, 24, 8))
    first = recurrence.anchor_utc
    second = next_occurrence_after(recurrence, first)
    third = next_occurrence_after(recurrence, second)
    assert first == datetime(2026, 10, 24, 7, tzinfo=UTC)
    assert second == datetime(2026, 10, 25, 8, tzinfo=UTC)
    assert third == datetime(2026, 10, 26, 8, tzinfo=UTC)


def test_nonexistent_time_moves_to_first_valid_time() -> None:
    assert resolve_local_datetime(
        datetime(2026, 3, 29, 1, 30), "Europe/Dublin"
    ) == datetime(2026, 3, 29, 1, tzinfo=UTC)


def test_ambiguous_time_uses_first_occurrence() -> None:
    assert resolve_local_datetime(
        datetime(2026, 10, 25, 1, 30), "Europe/Dublin"
    ) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
