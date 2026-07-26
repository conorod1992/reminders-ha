"""Typed models for Reminders."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from .recurrence import RecurrenceRule


class ReminderStatus(StrEnum):
    """Reminder lifecycle state."""

    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    """Delivery defaults for one Home Assistant user."""

    default_delivery_policy: DeliveryPolicy = field(
        default_factory=lambda: DeliveryPolicy(("persistent_notification",))
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize preferences."""
        return {"default_delivery_policy": self.default_delivery_policy.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize preferences."""
        policy = data.get("default_delivery_policy", {})
        return cls(DeliveryPolicy.from_dict(policy))


@dataclass(frozen=True, slots=True)
class Reminder:
    """A persistent reminder."""

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
            delivered_at=(
                _parse_datetime(data["delivered_at"])
                if data.get("delivered_at")
                else None
            ),
            delivery_errors=tuple(
                str(value) for value in data.get("delivery_errors", [])
            ),
            recurrence=RecurrenceRule.from_dict(recurrence) if recurrence else None,
            scheduled_due=(
                _parse_datetime(data["scheduled_due"])
                if data.get("scheduled_due")
                else None
            ),
            last_occurrence_due=(
                _parse_datetime(data["last_occurrence_due"])
                if data.get("last_occurrence_due")
                else None
            ),
            last_occurrence_status=(
                ReminderStatus(data["last_occurrence_status"])
                if data.get("last_occurrence_status")
                else None
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
