"""Pure recurrence validation and occurrence calculation helpers."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RecurrenceError(ValueError):
    """Raised when a recurrence rule is invalid."""


class RecurrenceFrequency(StrEnum):
    """Supported recurrence frequencies."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Weekday(IntEnum):
    """Weekday values matching datetime.weekday()."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6

    @classmethod
    def from_name(cls, value: str) -> Self:
        """Create a weekday from its English name."""
        try:
            return cls[value.strip().upper()]
        except KeyError as err:
            raise RecurrenceError(f"Unknown weekday: {value}") from err

    @property
    def label(self) -> str:
        """Return the stable lowercase representation."""
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    """An anchored local-wall-clock recurrence rule."""

    frequency: RecurrenceFrequency
    interval: int
    timezone: str
    anchor_local: datetime
    weekdays: tuple[Weekday, ...] = ()
    day_of_month: int | None = None

    def __post_init__(self) -> None:
        """Validate and canonicalize the rule."""
        if self.anchor_local.tzinfo is not None:
            raise RecurrenceError("Recurrence anchor must be a local naive datetime")
        if self.interval < 1:
            raise RecurrenceError("Recurrence interval must be at least 1")
        _get_timezone(self.timezone)
        canonical_weekdays = tuple(sorted(set(self.weekdays)))
        object.__setattr__(self, "weekdays", canonical_weekdays)
        if self.frequency is RecurrenceFrequency.DAILY:
            if canonical_weekdays or self.day_of_month is not None:
                raise RecurrenceError("Daily recurrence cannot define weekdays or day")
        elif self.frequency is RecurrenceFrequency.WEEKLY:
            if not canonical_weekdays:
                raise RecurrenceError("Weekly recurrence needs at least one weekday")
            if self.anchor_local.weekday() not in canonical_weekdays:
                raise RecurrenceError(
                    "First reminder weekday must be one of the selected weekdays"
                )
            if self.day_of_month is not None:
                raise RecurrenceError("Weekly recurrence cannot define day of month")
        elif self.frequency is RecurrenceFrequency.MONTHLY:
            if self.day_of_month is None or not 1 <= self.day_of_month <= 31:
                raise RecurrenceError("Monthly day must be between 1 and 31")
            if self.anchor_local.day != self.day_of_month:
                raise RecurrenceError(
                    "First reminder date must match the selected day of month"
                )
            if canonical_weekdays:
                raise RecurrenceError("Monthly recurrence cannot define weekdays")

    @property
    def anchor_utc(self) -> datetime:
        """Return the resolved first occurrence instant."""
        return resolve_local_datetime(self.anchor_local, self.timezone)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the rule."""
        return {
            "frequency": self.frequency.value,
            "interval": self.interval,
            "timezone": self.timezone,
            "anchor_local": self.anchor_local.isoformat(),
            "weekdays": [weekday.label for weekday in self.weekdays],
            "day_of_month": self.day_of_month,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize and validate a rule."""
        anchor = datetime.fromisoformat(str(data["anchor_local"]))
        return cls(
            frequency=RecurrenceFrequency(data["frequency"]),
            interval=int(data["interval"]),
            timezone=str(data["timezone"]),
            anchor_local=anchor,
            weekdays=tuple(
                Weekday.from_name(str(value)) for value in data.get("weekdays", [])
            ),
            day_of_month=(
                int(data["day_of_month"])
                if data.get("day_of_month") is not None
                else None
            ),
        )


def first_due(rule: RecurrenceRule, now: datetime) -> datetime:
    """Return the anchor if future, otherwise the next phased occurrence."""
    now = _as_utc(now)
    anchor = rule.anchor_utc
    return anchor if anchor >= now else next_occurrence_after(rule, now)


def next_occurrence_after(rule: RecurrenceRule, after: datetime) -> datetime:
    """Return the first occurrence strictly after an instant."""
    after = _as_utc(after)
    if rule.frequency is RecurrenceFrequency.DAILY:
        return _next_daily(rule, after)
    if rule.frequency is RecurrenceFrequency.WEEKLY:
        return _next_weekly(rule, after)
    return _next_monthly(rule, after)


def resolve_local_datetime(local_value: datetime, timezone: str) -> datetime:
    """Resolve local wall time to UTC with explicit DST policies.

    Ambiguous times use fold=0 (the first occurrence). Nonexistent times move
    forward to the first valid wall-clock second after the gap.
    """
    if local_value.tzinfo is not None:
        raise RecurrenceError("Expected a local naive datetime")
    zone = _get_timezone(timezone)
    candidate = local_value
    for _ in range(4 * 60 * 60 + 1):
        aware = candidate.replace(tzinfo=zone, fold=0)
        round_trip = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip == candidate:
            return aware.astimezone(UTC)
        candidate += timedelta(seconds=1)
    raise RecurrenceError("Could not resolve local datetime around DST transition")


def _next_daily(rule: RecurrenceRule, after: datetime) -> datetime:
    local_after = after.astimezone(_get_timezone(rule.timezone)).replace(tzinfo=None)
    day_delta = max(0, (local_after.date() - rule.anchor_local.date()).days)
    steps = day_delta // rule.interval
    candidate = rule.anchor_local + timedelta(days=steps * rule.interval)
    while (instant := resolve_local_datetime(candidate, rule.timezone)) <= after:
        candidate += timedelta(days=rule.interval)
    return instant


def _next_weekly(rule: RecurrenceRule, after: datetime) -> datetime:
    zone = _get_timezone(rule.timezone)
    local_after = after.astimezone(zone).replace(tzinfo=None)
    anchor_week = rule.anchor_local.date() - timedelta(days=rule.anchor_local.weekday())
    weeks_since = max(0, (local_after.date() - anchor_week).days // 7)
    active_week = weeks_since - (weeks_since % rule.interval)
    wall_time = rule.anchor_local.time()
    while True:
        week_start = anchor_week + timedelta(weeks=active_week)
        for weekday in rule.weekdays:
            candidate = datetime.combine(
                week_start + timedelta(days=int(weekday)), wall_time
            )
            if candidate < rule.anchor_local:
                continue
            instant = resolve_local_datetime(candidate, rule.timezone)
            if instant > after:
                return instant
        active_week += rule.interval


def _next_monthly(rule: RecurrenceRule, after: datetime) -> datetime:
    zone = _get_timezone(rule.timezone)
    local_after = after.astimezone(zone).replace(tzinfo=None)
    anchor_month = rule.anchor_local.year * 12 + rule.anchor_local.month - 1
    after_month = local_after.year * 12 + local_after.month - 1
    elapsed = max(0, after_month - anchor_month)
    month_index = anchor_month + (elapsed // rule.interval) * rule.interval
    assert rule.day_of_month is not None
    while True:
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        if rule.day_of_month <= calendar.monthrange(year, month)[1]:
            candidate = datetime.combine(
                date(year, month, rule.day_of_month), rule.anchor_local.time()
            )
            if candidate >= rule.anchor_local:
                instant = resolve_local_datetime(candidate, rule.timezone)
                if instant > after:
                    return instant
        month_index += rule.interval


def _get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as err:
        raise RecurrenceError(f"Unknown timezone: {name}") from err


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecurrenceError("Datetime must be timezone-aware")
    return value.astimezone(UTC)
