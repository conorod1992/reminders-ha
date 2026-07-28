"""Pure anchored recurrence validation and occurrence calculation helpers."""

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
    YEARLY = "yearly"


class MonthlyMode(StrEnum):
    """How a monthly occurrence date is selected."""

    DAY_OF_MONTH = "day_of_month"
    NTH_WEEKDAY = "nth_weekday"
    LAST_WEEKDAY = "last_weekday"
    LAST_DAY = "last_day"


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
    monthly_mode: MonthlyMode = MonthlyMode.DAY_OF_MONTH
    monthly_weekday: Weekday | None = None
    monthly_week: int | None = None
    end_date: date | None = None
    occurrence_count: int | None = None

    def __post_init__(self) -> None:
        """Validate and canonicalize the rule without changing legacy semantics."""
        if self.anchor_local.tzinfo is not None:
            raise RecurrenceError("Recurrence anchor must be a local naive datetime")
        if self.interval < 1:
            raise RecurrenceError("Recurrence interval must be at least 1")
        _get_timezone(self.timezone)
        if self.end_date is not None and self.end_date < self.anchor_local.date():
            raise RecurrenceError(
                "Recurrence end date cannot precede the first reminder"
            )
        if self.occurrence_count is not None and self.occurrence_count < 1:
            raise RecurrenceError("Recurrence occurrence count must be at least 1")
        canonical_weekdays = tuple(sorted(set(self.weekdays)))
        object.__setattr__(self, "weekdays", canonical_weekdays)

        if self.frequency is RecurrenceFrequency.DAILY:
            self._reject_monthly_fields(canonical_weekdays)
        elif self.frequency is RecurrenceFrequency.WEEKLY:
            if not canonical_weekdays:
                raise RecurrenceError("Weekly recurrence needs at least one weekday")
            if self.anchor_local.weekday() not in canonical_weekdays:
                raise RecurrenceError(
                    "First reminder weekday must be one of the selected weekdays"
                )
            if self.day_of_month is not None or self.monthly_weekday is not None:
                raise RecurrenceError("Weekly recurrence cannot define monthly fields")
        elif self.frequency is RecurrenceFrequency.MONTHLY:
            if canonical_weekdays:
                raise RecurrenceError(
                    "Monthly recurrence cannot define weekly weekdays"
                )
            expected = _monthly_date(
                self, self.anchor_local.year, self.anchor_local.month
            )
            if expected != self.anchor_local.date():
                raise RecurrenceError(
                    "First reminder date must match the selected monthly pattern"
                )
        elif self.frequency is RecurrenceFrequency.YEARLY:
            if canonical_weekdays or self.monthly_weekday is not None:
                raise RecurrenceError(
                    "Yearly recurrence cannot define weekday patterns"
                )
            day = self.day_of_month or self.anchor_local.day
            if day != self.anchor_local.day:
                raise RecurrenceError("First reminder date must match the yearly date")
            object.__setattr__(self, "day_of_month", day)

    def _reject_monthly_fields(self, weekdays: tuple[Weekday, ...]) -> None:
        if (
            weekdays
            or self.day_of_month is not None
            or self.monthly_weekday is not None
        ):
            raise RecurrenceError("Daily recurrence cannot define weekdays or a day")

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
            "monthly_mode": self.monthly_mode.value,
            "monthly_weekday": (
                self.monthly_weekday.label if self.monthly_weekday is not None else None
            ),
            "monthly_week": self.monthly_week,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "occurrence_count": self.occurrence_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize and validate a rule."""
        monthly_weekday = data.get("monthly_weekday")
        end_date = data.get("end_date")
        return cls(
            frequency=RecurrenceFrequency(data["frequency"]),
            interval=int(data["interval"]),
            timezone=str(data["timezone"]),
            anchor_local=datetime.fromisoformat(str(data["anchor_local"])),
            weekdays=tuple(
                Weekday.from_name(str(value)) for value in data.get("weekdays", [])
            ),
            day_of_month=(
                int(data["day_of_month"])
                if data.get("day_of_month") is not None
                else None
            ),
            monthly_mode=MonthlyMode(
                data.get("monthly_mode", MonthlyMode.DAY_OF_MONTH)
            ),
            monthly_weekday=(
                Weekday.from_name(str(monthly_weekday))
                if monthly_weekday is not None
                else None
            ),
            monthly_week=(
                int(data["monthly_week"])
                if data.get("monthly_week") is not None
                else None
            ),
            end_date=date.fromisoformat(str(end_date)) if end_date else None,
            occurrence_count=(
                int(data["occurrence_count"])
                if data.get("occurrence_count") is not None
                else None
            ),
        )


def first_due(rule: RecurrenceRule, now: datetime) -> datetime:
    """Return the first eligible occurrence at or after now."""
    now = _as_utc(now)
    candidate = (
        rule.anchor_utc if rule.anchor_utc >= now else _next_unbounded(rule, now)
    )
    if not _is_allowed(rule, candidate):
        raise RecurrenceError("The recurrence has no future occurrences")
    return candidate


def next_occurrence_after(rule: RecurrenceRule, after: datetime) -> datetime:
    """Return the first occurrence strictly after an instant.

    This backwards-compatible helper raises when an end/count limit is exhausted.
    Runtime code that expects a finished series should use ``next_due_after``.
    """
    candidate = next_due_after(rule, after)
    if candidate is None:
        raise RecurrenceError("The recurrence has no more occurrences")
    return candidate


def next_due_after(rule: RecurrenceRule, after: datetime) -> datetime | None:
    """Return the next occurrence, or None when the anchored series is complete."""
    candidate = _next_unbounded(rule, _as_utc(after))
    return candidate if _is_allowed(rule, candidate) else None


def occurrence_number(rule: RecurrenceRule, occurrence: datetime) -> int:
    """Return the one-based anchored sequence number without replaying daily events."""
    local = (
        _as_utc(occurrence)
        .astimezone(_get_timezone(rule.timezone))
        .replace(tzinfo=None)
    )
    if rule.frequency is RecurrenceFrequency.DAILY:
        return (local.date() - rule.anchor_local.date()).days // rule.interval + 1
    if rule.frequency is RecurrenceFrequency.WEEKLY:
        anchor_week = rule.anchor_local.date() - timedelta(
            days=rule.anchor_local.weekday()
        )
        week = (local.date() - anchor_week).days // 7
        active = week // rule.interval
        first_days = [
            day for day in rule.weekdays if day >= rule.anchor_local.weekday()
        ]
        if active == 0:
            return first_days.index(Weekday(local.weekday())) + 1
        before = len(first_days) + (active - 1) * len(rule.weekdays)
        return before + list(rule.weekdays).index(Weekday(local.weekday())) + 1
    if rule.frequency is RecurrenceFrequency.YEARLY:
        return (local.year - rule.anchor_local.year) // rule.interval + 1

    anchor_month = rule.anchor_local.year * 12 + rule.anchor_local.month - 1
    target_month = local.year * 12 + local.month - 1
    count = 0
    for month_index in range(anchor_month, target_month + 1, rule.interval):
        year, month0 = divmod(month_index, 12)
        candidate = _monthly_date(rule, year, month0 + 1)
        if candidate is not None and candidate >= rule.anchor_local.date():
            count += 1
    return count


def preview_occurrences(
    rule: RecurrenceRule, *, after: datetime, limit: int = 5
) -> list[datetime]:
    """Return a bounded direct preview for services and the management UI."""
    if limit < 1 or limit > 20:
        raise RecurrenceError("Preview limit must be between 1 and 20")
    values: list[datetime] = []
    cursor = _as_utc(after)
    if rule.anchor_utc > cursor and _is_allowed(rule, rule.anchor_utc):
        values.append(rule.anchor_utc)
        cursor = rule.anchor_utc
    while len(values) < limit and (candidate := next_due_after(rule, cursor)):
        values.append(candidate)
        cursor = candidate
    return values


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


def _is_allowed(rule: RecurrenceRule, candidate: datetime) -> bool:
    local_date = candidate.astimezone(_get_timezone(rule.timezone)).date()
    if rule.end_date is not None and local_date > rule.end_date:
        return False
    return not (
        rule.occurrence_count is not None
        and occurrence_number(rule, candidate) > rule.occurrence_count
    )


def _next_unbounded(rule: RecurrenceRule, after: datetime) -> datetime:
    if rule.frequency is RecurrenceFrequency.DAILY:
        return _next_daily(rule, after)
    if rule.frequency is RecurrenceFrequency.WEEKLY:
        return _next_weekly(rule, after)
    if rule.frequency is RecurrenceFrequency.MONTHLY:
        return _next_monthly(rule, after)
    return _next_yearly(rule, after)


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
    while True:
        year, month0 = divmod(month_index, 12)
        candidate_date = _monthly_date(rule, year, month0 + 1)
        if candidate_date is not None:
            candidate = datetime.combine(candidate_date, rule.anchor_local.time())
            if candidate >= rule.anchor_local:
                instant = resolve_local_datetime(candidate, rule.timezone)
                if instant > after:
                    return instant
        month_index += rule.interval


def _next_yearly(rule: RecurrenceRule, after: datetime) -> datetime:
    local_after = after.astimezone(_get_timezone(rule.timezone)).replace(tzinfo=None)
    elapsed = max(0, local_after.year - rule.anchor_local.year)
    year = rule.anchor_local.year + (elapsed // rule.interval) * rule.interval
    while True:
        try:
            candidate = rule.anchor_local.replace(year=year)
        except ValueError:  # 29 February: skip non-leap years.
            year += rule.interval
            continue
        instant = resolve_local_datetime(candidate, rule.timezone)
        if instant > after:
            return instant
        year += rule.interval


def _monthly_date(rule: RecurrenceRule, year: int, month: int) -> date | None:
    last = calendar.monthrange(year, month)[1]
    if rule.monthly_mode is MonthlyMode.LAST_DAY:
        return date(year, month, last)
    if rule.monthly_mode is MonthlyMode.DAY_OF_MONTH:
        day = rule.day_of_month
        if day is None or not 1 <= day <= 31:
            raise RecurrenceError("Monthly day must be between 1 and 31")
        return date(year, month, day) if day <= last else None
    weekday = rule.monthly_weekday
    if weekday is None:
        raise RecurrenceError("Monthly weekday pattern needs a weekday")
    if rule.monthly_mode is MonthlyMode.LAST_WEEKDAY:
        return date(
            year, month, last - ((date(year, month, last).weekday() - weekday) % 7)
        )
    week = rule.monthly_week
    if week is None or not 1 <= week <= 5:
        raise RecurrenceError("Monthly weekday occurrence must be between 1 and 5")
    first = 1 + (weekday - date(year, month, 1).weekday()) % 7
    day = first + (week - 1) * 7
    return date(year, month, day) if day <= last else None


def _get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as err:
        raise RecurrenceError(f"Unknown timezone: {name}") from err


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecurrenceError("Datetime must be timezone-aware")
    return value.astimezone(UTC)
