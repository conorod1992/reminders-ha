"""Typed persistent models for Reminders."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Any, Self

from .recurrence import RecurrenceRule


class ReminderStatus(StrEnum):
    """Reminder or active-series lifecycle state."""

    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    AWAITING_ACKNOWLEDGEMENT = "awaiting_acknowledgement"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OccurrenceStatus(StrEnum):
    """Immutable-in-meaning lifecycle state for one scheduled occurrence."""

    SCHEDULED = "scheduled"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    AWAITING_ACKNOWLEDGEMENT = "awaiting_acknowledgement"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AcknowledgementPolicy(StrEnum):
    """Whether an occurrence needs explicit completion."""

    DEFAULT = "default"
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


class QuietHoursPolicy(StrEnum):
    """Whether a reminder follows the owner's current quiet hours."""

    RESPECT = "respect"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """Logical delivery policy resolved to endpoints at delivery time."""

    channels: tuple[str, ...]
    notify_targets: tuple[str, ...] = ()
    voice_targets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize policy."""
        return {
            "channels": list(self.channels),
            "notify_targets": list(self.notify_targets),
            "voice_targets": list(self.voice_targets),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize policy."""
        return cls(
            channels=tuple(str(value) for value in data.get("channels", [])),
            notify_targets=tuple(
                str(value) for value in data.get("notify_targets", [])
            ),
            voice_targets=tuple(str(value) for value in data.get("voice_targets", [])),
        )


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """Live defaults for one Home Assistant user."""

    default_delivery_policy: DeliveryPolicy = field(
        default_factory=lambda: DeliveryPolicy(("persistent_notification",))
    )
    require_acknowledgement: bool = False
    configured: bool = False
    history_retention_days: int = 90
    history_max_occurrences: int = 250
    quiet_hours_enabled: bool = False
    quiet_hours_start: time = time(23, 0)
    quiet_hours_end: time = time(7, 0)
    quiet_hours_channels: tuple[str, ...] = ("voice",)
    quiet_hours_fallback_channels: tuple[str, ...] = ("persistent_notification",)

    def to_dict(self) -> dict[str, Any]:
        """Serialize preferences."""
        return {
            "default_delivery_policy": self.default_delivery_policy.to_dict(),
            "require_acknowledgement": self.require_acknowledgement,
            "configured": self.configured,
            "history_retention_days": self.history_retention_days,
            "history_max_occurrences": self.history_max_occurrences,
            "quiet_hours_enabled": self.quiet_hours_enabled,
            "quiet_hours_start": self.quiet_hours_start.isoformat(timespec="minutes"),
            "quiet_hours_end": self.quiet_hours_end.isoformat(timespec="minutes"),
            "quiet_hours_channels": list(self.quiet_hours_channels),
            "quiet_hours_fallback_channels": list(self.quiet_hours_fallback_channels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize preferences with conservative legacy defaults."""
        policy = data.get("default_delivery_policy", {})
        return cls(
            default_delivery_policy=DeliveryPolicy.from_dict(policy),
            require_acknowledgement=bool(data.get("require_acknowledgement", False)),
            configured=bool(data.get("configured", bool(data))),
            history_retention_days=int(data.get("history_retention_days", 90)),
            history_max_occurrences=int(data.get("history_max_occurrences", 250)),
            quiet_hours_enabled=bool(data.get("quiet_hours_enabled", False)),
            quiet_hours_start=_parse_time(data.get("quiet_hours_start", "23:00")),
            quiet_hours_end=_parse_time(data.get("quiet_hours_end", "07:00")),
            quiet_hours_channels=tuple(
                str(value) for value in data.get("quiet_hours_channels", ["voice"])
            ),
            quiet_hours_fallback_channels=tuple(
                str(value)
                for value in data.get(
                    "quiet_hours_fallback_channels", ["persistent_notification"]
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class Occurrence:
    """Bounded lifecycle history for one scheduled occurrence."""

    id: str
    scheduled_due: datetime
    due: datetime
    status: OccurrenceStatus = OccurrenceStatus.SCHEDULED
    delivered_at: datetime | None = None
    succeeded_channels: tuple[str, ...] = ()
    failed_channels: tuple[str, ...] = ()
    delivery_errors: tuple[str, ...] = ()
    suppressed_channels: tuple[str, ...] = ()
    acknowledgement_required: bool = False
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    snoozed: bool = False
    snoozed_at: datetime | None = None

    def updated(self, **changes: Any) -> Self:
        """Return an updated immutable occurrence."""
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """Serialize occurrence history."""
        return {
            "id": self.id,
            "scheduled_due": _format_datetime(self.scheduled_due),
            "due": _format_datetime(self.due),
            "status": self.status.value,
            "delivered_at": (
                _format_datetime(self.delivered_at) if self.delivered_at else None
            ),
            "succeeded_channels": list(self.succeeded_channels),
            "failed_channels": list(self.failed_channels),
            "delivery_errors": list(self.delivery_errors),
            "suppressed_channels": list(self.suppressed_channels),
            "acknowledgement_required": self.acknowledgement_required,
            "acknowledged_at": (
                _format_datetime(self.acknowledged_at) if self.acknowledged_at else None
            ),
            "acknowledged_by": self.acknowledged_by,
            "snoozed": self.snoozed,
            "snoozed_at": (
                _format_datetime(self.snoozed_at) if self.snoozed_at else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize occurrence history."""
        return cls(
            id=str(data["id"]),
            scheduled_due=_parse_datetime(data["scheduled_due"]),
            due=_parse_datetime(data.get("due", data["scheduled_due"])),
            status=OccurrenceStatus(data.get("status", OccurrenceStatus.SCHEDULED)),
            delivered_at=_optional_datetime(data.get("delivered_at")),
            succeeded_channels=tuple(
                str(value) for value in data.get("succeeded_channels", [])
            ),
            failed_channels=tuple(
                str(value) for value in data.get("failed_channels", [])
            ),
            delivery_errors=tuple(
                str(value) for value in data.get("delivery_errors", [])
            ),
            suppressed_channels=tuple(
                str(value) for value in data.get("suppressed_channels", [])
            ),
            acknowledgement_required=bool(data.get("acknowledgement_required", False)),
            acknowledged_at=_optional_datetime(data.get("acknowledged_at")),
            acknowledged_by=(
                str(data["acknowledged_by"])
                if data.get("acknowledged_by") is not None
                else None
            ),
            snoozed=bool(data.get("snoozed", False)),
            snoozed_at=_optional_datetime(data.get("snoozed_at")),
        )


@dataclass(frozen=True, slots=True)
class Reminder:
    """A persistent one-shot reminder or recurring series."""

    id: str
    user_id: str
    title: str
    due: datetime
    created_at: datetime
    updated_at: datetime
    message: str | None = None
    status: ReminderStatus = ReminderStatus.PENDING
    delivery_policy: DeliveryPolicy | None = None
    delivered_at: datetime | None = None
    delivery_errors: tuple[str, ...] = ()
    recurrence: RecurrenceRule | None = None
    scheduled_due: datetime | None = None
    last_occurrence_due: datetime | None = None
    last_occurrence_status: ReminderStatus | None = None
    acknowledgement_policy: AcknowledgementPolicy = AcknowledgementPolicy.DEFAULT
    quiet_hours_policy: QuietHoursPolicy = QuietHoursPolicy.RESPECT
    current_occurrence_id: str | None = None
    current_occurrence_number: int = 1
    occurrence_history: tuple[Occurrence, ...] = ()

    def updated(self, **changes: Any) -> Self:
        """Return an updated immutable reminder."""
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """Serialize reminder."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "message": self.message,
            "due": _format_datetime(self.due),
            "created_at": _format_datetime(self.created_at),
            "updated_at": _format_datetime(self.updated_at),
            "status": self.status.value,
            "delivery_policy": (
                self.delivery_policy.to_dict() if self.delivery_policy else None
            ),
            "delivered_at": (
                _format_datetime(self.delivered_at) if self.delivered_at else None
            ),
            "delivery_errors": list(self.delivery_errors),
            "recurring": self.recurrence is not None,
            "recurrence": self.recurrence.to_dict() if self.recurrence else None,
            "scheduled_due": (
                _format_datetime(self.scheduled_due) if self.scheduled_due else None
            ),
            "last_occurrence_due": (
                _format_datetime(self.last_occurrence_due)
                if self.last_occurrence_due
                else None
            ),
            "last_occurrence_status": (
                self.last_occurrence_status.value
                if self.last_occurrence_status
                else None
            ),
            "acknowledgement_policy": self.acknowledgement_policy.value,
            "quiet_hours_policy": self.quiet_hours_policy.value,
            "current_occurrence_id": self.current_occurrence_id,
            "current_occurrence_number": self.current_occurrence_number,
            "occurrence_history": [item.to_dict() for item in self.occurrence_history],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize and validate a reminder."""
        policy = data.get("delivery_policy")
        recurrence = data.get("recurrence")
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            title=str(data["title"]),
            message=str(data["message"]) if data.get("message") is not None else None,
            due=_parse_datetime(data["due"]),
            created_at=_parse_datetime(data["created_at"]),
            updated_at=_parse_datetime(data["updated_at"]),
            status=ReminderStatus(data.get("status", ReminderStatus.PENDING)),
            delivery_policy=DeliveryPolicy.from_dict(policy) if policy else None,
            delivered_at=_optional_datetime(data.get("delivered_at")),
            delivery_errors=tuple(
                str(value) for value in data.get("delivery_errors", [])
            ),
            recurrence=RecurrenceRule.from_dict(recurrence) if recurrence else None,
            scheduled_due=_optional_datetime(data.get("scheduled_due")),
            last_occurrence_due=_optional_datetime(data.get("last_occurrence_due")),
            last_occurrence_status=(
                ReminderStatus(data["last_occurrence_status"])
                if data.get("last_occurrence_status")
                else None
            ),
            acknowledgement_policy=AcknowledgementPolicy(
                data.get("acknowledgement_policy", AcknowledgementPolicy.DEFAULT)
            ),
            quiet_hours_policy=QuietHoursPolicy(
                data.get("quiet_hours_policy", QuietHoursPolicy.RESPECT)
            ),
            current_occurrence_id=(
                str(data["current_occurrence_id"])
                if data.get("current_occurrence_id")
                else None
            ),
            current_occurrence_number=int(data.get("current_occurrence_number", 1)),
            occurrence_history=tuple(
                Occurrence.from_dict(item)
                for item in data.get("occurrence_history", [])
            ),
        )


def _format_datetime(value: datetime) -> str:
    """Format an aware datetime as UTC ISO 8601."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: Any) -> datetime:
    """Parse an ISO datetime and normalize it to UTC."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Datetime must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_datetime(value: Any) -> datetime | None:
    return _parse_datetime(value) if value else None


def _parse_time(value: Any) -> time:
    parsed = time.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        raise ValueError("Quiet-hours times must be local wall-clock values")
    return parsed.replace(second=0, microsecond=0)
