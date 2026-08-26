"""Typed persistent models for Reminders."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Any, Self

from .recurrence import RecurrenceRule
from .triggers.models import TriggerDefinition


class ActivationType(StrEnum):
    """How a reminder becomes active."""

    TIME = "time"
    TRIGGER = "trigger"


class TriggerRepeatPolicy(StrEnum):
    """How a triggered reminder re-arms."""

    ONCE = "once"
    EVERY_TRIGGER = "every_trigger"
    REARM_AFTER_ACKNOWLEDGEMENT = "rearm_after_acknowledgement"


class WhileAwaitingAcknowledgement(StrEnum):
    """Behavior for a new hit while an older occurrence awaits completion."""

    SKIP = "skip"
    DELIVER_NEW_OCCURRENCE = "deliver_new_occurrence"


class ReminderStatus(StrEnum):
    """Reminder or active-series lifecycle state."""

    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    AWAITING_ACKNOWLEDGEMENT = "awaiting_acknowledgement"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_FOR_TRIGGER = "waiting_for_trigger"
    WAITING_FOR_CONTEXT = "waiting_for_context"
    INACTIVE_BEFORE_AVAILABLE_FROM = "inactive_before_available_from"
    EXPIRED = "expired"
    COMPLETED = "completed"
    PAUSED = "paused"
    SKIPPED = "skipped"


class OccurrenceStatus(StrEnum):
    """Immutable-in-meaning lifecycle state for one scheduled occurrence."""

    SCHEDULED = "scheduled"
    WAITING_FOR_CONTEXT = "waiting_for_context"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    AWAITING_ACKNOWLEDGEMENT = "awaiting_acknowledgement"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class MissedOccurrencePolicy(StrEnum):
    """How an anchored series handles an occurrence missed while HA was offline."""

    REMIND_ON_STARTUP = "remind_on_startup"
    SKIP = "skip"


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
class EscalationPolicy:
    """Bounded redelivery policy for an unacknowledged occurrence."""

    initial_delay_minutes: int = 30
    repeat_minutes: int = 60
    max_attempts: int = 3

    def to_dict(self) -> dict[str, int]:
        return {
            "initial_delay_minutes": self.initial_delay_minutes,
            "repeat_minutes": self.repeat_minutes,
            "max_attempts": self.max_attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            initial_delay_minutes=int(data.get("initial_delay_minutes", 30)),
            repeat_minutes=int(data.get("repeat_minutes", 60)),
            max_attempts=int(data.get("max_attempts", 3)),
        )


@dataclass(frozen=True, slots=True)
class EscalationAttempt:
    """Persisted result of one escalation delivery attempt."""

    number: int
    attempted_at: datetime
    succeeded_channels: tuple[str, ...] = ()
    failed_channels: tuple[str, ...] = ()
    delivery_errors: tuple[str, ...] = ()
    suppressed_channels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "attempted_at": _format_datetime(self.attempted_at),
            "succeeded_channels": list(self.succeeded_channels),
            "failed_channels": list(self.failed_channels),
            "delivery_errors": list(self.delivery_errors),
            "suppressed_channels": list(self.suppressed_channels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            number=int(data["number"]),
            attempted_at=_parse_datetime(data["attempted_at"]),
            succeeded_channels=tuple(data.get("succeeded_channels", ())),
            failed_channels=tuple(data.get("failed_channels", ())),
            delivery_errors=tuple(data.get("delivery_errors", ())),
            suppressed_channels=tuple(data.get("suppressed_channels", ())),
        )


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """Logical delivery policy resolved to endpoints at delivery time."""

    channels: tuple[str, ...]
    notify_targets: tuple[str, ...] = ()
    mobile_app_services: tuple[str, ...] = ()
    voice_targets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize policy."""
        return {
            "channels": list(self.channels),
            "notify_targets": list(self.notify_targets),
            "mobile_app_services": list(self.mobile_app_services),
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
            mobile_app_services=tuple(
                str(value) for value in data.get("mobile_app_services", [])
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
    completion_source: str | None = None
    completion_reason: str | None = None
    completed_at: datetime | None = None
    completed_by: str | None = None
    external_action_id: str | None = None
    external_action_selected_at: datetime | None = None
    external_action_selected_by: str | None = None
    snoozed: bool = False
    snoozed_at: datetime | None = None
    trigger_type: str | None = None
    trigger_summary: str | None = None
    triggered_at: datetime | None = None
    activation_cause: str | None = None
    trigger_context: dict[str, Any] | None = None
    context_eligible_at: datetime | None = None
    expires_at: datetime | None = None
    notification_action_token: str | None = None
    next_escalation_at: datetime | None = None
    escalation_attempt_count: int = 0
    escalation_history: tuple[EscalationAttempt, ...] = ()
    redelivery_count: int = 0

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
            "completion_source": self.completion_source,
            "completion_reason": self.completion_reason,
            "completed_at": (
                _format_datetime(self.completed_at) if self.completed_at else None
            ),
            "completed_by": self.completed_by,
            "external_action_id": self.external_action_id,
            "external_action_selected_at": (
                _format_datetime(self.external_action_selected_at)
                if self.external_action_selected_at
                else None
            ),
            "external_action_selected_by": self.external_action_selected_by,
            "snoozed": self.snoozed,
            "snoozed_at": (
                _format_datetime(self.snoozed_at) if self.snoozed_at else None
            ),
            "trigger_type": self.trigger_type,
            "trigger_summary": self.trigger_summary,
            "triggered_at": (
                _format_datetime(self.triggered_at) if self.triggered_at else None
            ),
            "activation_cause": self.activation_cause,
            "trigger_context": self.trigger_context,
            "context_eligible_at": (
                _format_datetime(self.context_eligible_at)
                if self.context_eligible_at
                else None
            ),
            "expires_at": _format_datetime(self.expires_at)
            if self.expires_at
            else None,
            "notification_action_token": self.notification_action_token,
            "next_escalation_at": (
                _format_datetime(self.next_escalation_at)
                if self.next_escalation_at
                else None
            ),
            "escalation_attempt_count": self.escalation_attempt_count,
            "escalation_history": [item.to_dict() for item in self.escalation_history],
            "redelivery_count": self.redelivery_count,
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
            completion_source=(
                str(data["completion_source"])
                if data.get("completion_source")
                else None
            ),
            completion_reason=(
                str(data["completion_reason"])
                if data.get("completion_reason")
                else None
            ),
            completed_at=_optional_datetime(data.get("completed_at")),
            completed_by=(
                str(data["completed_by"])
                if data.get("completed_by") is not None
                else None
            ),
            external_action_id=(
                str(data["external_action_id"])
                if data.get("external_action_id")
                else None
            ),
            external_action_selected_at=_optional_datetime(
                data.get("external_action_selected_at")
            ),
            external_action_selected_by=(
                str(data["external_action_selected_by"])
                if data.get("external_action_selected_by") is not None
                else None
            ),
            snoozed=bool(data.get("snoozed", False)),
            snoozed_at=_optional_datetime(data.get("snoozed_at")),
            trigger_type=(
                str(data["trigger_type"]) if data.get("trigger_type") else None
            ),
            trigger_summary=(
                str(data["trigger_summary"]) if data.get("trigger_summary") else None
            ),
            triggered_at=_optional_datetime(data.get("triggered_at")),
            activation_cause=(
                str(data["activation_cause"]) if data.get("activation_cause") else None
            ),
            trigger_context=(
                dict(data["trigger_context"])
                if isinstance(data.get("trigger_context"), dict)
                else None
            ),
            context_eligible_at=_optional_datetime(data.get("context_eligible_at")),
            expires_at=_optional_datetime(data.get("expires_at")),
            notification_action_token=(
                str(data["notification_action_token"])
                if data.get("notification_action_token")
                else None
            ),
            next_escalation_at=_optional_datetime(data.get("next_escalation_at")),
            escalation_attempt_count=int(data.get("escalation_attempt_count", 0)),
            escalation_history=tuple(
                EscalationAttempt.from_dict(item)
                for item in data.get("escalation_history", [])
            ),
            redelivery_count=int(data.get("redelivery_count", 0)),
        )


@dataclass(frozen=True, slots=True)
class TriggerDurationWait:
    """One durable duration wait for an activation or contextual trigger role."""

    role: str
    started_at: datetime
    cause: str
    context: dict[str, Any]
    observed_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "started_at": _format_datetime(self.started_at),
            "cause": self.cause,
            "context": self.context,
            "observed_value": self.observed_value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            role=str(data["role"]),
            started_at=_parse_datetime(data["started_at"]),
            cause=str(data.get("cause", "future_transition")),
            context=dict(data["context"])
            if isinstance(data.get("context"), dict)
            else {},
            observed_value=data.get("observed_value"),
        )


@dataclass(frozen=True, slots=True)
class Reminder:
    """A persistent one-shot reminder or recurring series."""

    id: str
    user_id: str
    title: str
    due: datetime | None
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
    activation_type: ActivationType = ActivationType.TIME
    trigger: TriggerDefinition | None = None
    trigger_summary: str | None = None
    trigger_description: str | None = None
    repeat_policy: TriggerRepeatPolicy = TriggerRepeatPolicy.ONCE
    fire_if_already_matching: bool = False
    while_awaiting_acknowledgement: WhileAwaitingAcknowledgement = (
        WhileAwaitingAcknowledgement.SKIP
    )
    cooldown_seconds: int = 0
    available_from: datetime | None = None
    expires_at: datetime | None = None
    last_triggered_at: datetime | None = None
    snoozed_until: datetime | None = None
    immediate_evaluated: bool = False
    trigger_duration_waits: tuple[TriggerDurationWait, ...] = ()
    cooldown_skip_count: int = 0
    deliver_when: TriggerDefinition | None = None
    deliver_when_summary: str | None = None
    complete_when: TriggerDefinition | None = None
    complete_when_summary: str | None = None
    escalation: EscalationPolicy | None = None
    notification_actions: tuple[dict[str, str], ...] = field(
        default=(), compare=False, repr=False
    )
    source: str | None = None
    source_id: str | None = None
    source_event: str | None = None
    managed_externally: bool = False
    allow_manual_completion: bool = False
    external_actions: tuple[dict[str, str], ...] = ()
    paused: bool = False
    paused_at: datetime | None = None
    missed_occurrence_policy: MissedOccurrencePolicy = (
        MissedOccurrencePolicy.REMIND_ON_STARTUP
    )
    expires_after_seconds: int | None = None

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
            "due": _format_datetime(self.due) if self.due else None,
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
            "activation_type": self.activation_type.value,
            "trigger": self.trigger.to_dict() if self.trigger else None,
            "trigger_summary": self.trigger_summary,
            "trigger_description": self.trigger_description,
            "repeat_policy": self.repeat_policy.value,
            "fire_if_already_matching": self.fire_if_already_matching,
            "while_awaiting_acknowledgement": (
                self.while_awaiting_acknowledgement.value
            ),
            "cooldown_seconds": self.cooldown_seconds,
            "available_from": (
                _format_datetime(self.available_from) if self.available_from else None
            ),
            "expires_at": (
                _format_datetime(self.expires_at) if self.expires_at else None
            ),
            "last_triggered_at": (
                _format_datetime(self.last_triggered_at)
                if self.last_triggered_at
                else None
            ),
            "snoozed_until": (
                _format_datetime(self.snoozed_until) if self.snoozed_until else None
            ),
            "immediate_evaluated": self.immediate_evaluated,
            "trigger_duration_waits": [
                item.to_dict() for item in self.trigger_duration_waits
            ],
            "cooldown_skip_count": self.cooldown_skip_count,
            "deliver_when": self.deliver_when.to_dict() if self.deliver_when else None,
            "deliver_when_summary": self.deliver_when_summary,
            "complete_when": (
                self.complete_when.to_dict() if self.complete_when else None
            ),
            "complete_when_summary": self.complete_when_summary,
            "escalation": self.escalation.to_dict() if self.escalation else None,
            "source": self.source,
            "source_id": self.source_id,
            "source_event": self.source_event,
            "managed_externally": self.managed_externally,
            "allow_manual_completion": self.allow_manual_completion,
            "external_actions": [dict(item) for item in self.external_actions],
            "paused": self.paused,
            "paused_at": _format_datetime(self.paused_at) if self.paused_at else None,
            "missed_occurrence_policy": self.missed_occurrence_policy.value,
            "expires_after_seconds": self.expires_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize and validate a reminder."""
        policy = data.get("delivery_policy")
        recurrence = data.get("recurrence")
        activation_type = ActivationType(
            data.get("activation_type", ActivationType.TIME)
        )
        trigger_data = data.get("trigger")
        trigger = TriggerDefinition.from_dict(trigger_data) if trigger_data else None
        deliver_when_data = data.get("deliver_when")
        complete_when_data = data.get("complete_when")
        escalation_data = data.get("escalation")
        if activation_type is ActivationType.TIME and data.get("due") is None:
            raise ValueError("Time reminder requires due")
        if activation_type is ActivationType.TRIGGER and trigger is None:
            raise ValueError("Triggered reminder requires trigger")
        if activation_type is ActivationType.TIME and trigger is not None:
            raise ValueError("Time reminder cannot contain a trigger")
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            title=str(data["title"]),
            message=str(data["message"]) if data.get("message") is not None else None,
            due=_optional_datetime(data.get("due")),
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
            activation_type=activation_type,
            trigger=trigger,
            trigger_summary=(
                str(data["trigger_summary"]) if data.get("trigger_summary") else None
            ),
            trigger_description=(
                str(data["trigger_description"])
                if data.get("trigger_description")
                else None
            ),
            repeat_policy=TriggerRepeatPolicy(
                data.get("repeat_policy", TriggerRepeatPolicy.ONCE)
            ),
            fire_if_already_matching=bool(data.get("fire_if_already_matching", False)),
            while_awaiting_acknowledgement=WhileAwaitingAcknowledgement(
                data.get(
                    "while_awaiting_acknowledgement",
                    WhileAwaitingAcknowledgement.SKIP,
                )
            ),
            cooldown_seconds=int(data.get("cooldown_seconds", 0)),
            available_from=_optional_datetime(data.get("available_from")),
            expires_at=_optional_datetime(data.get("expires_at")),
            last_triggered_at=_optional_datetime(data.get("last_triggered_at")),
            snoozed_until=_optional_datetime(data.get("snoozed_until")),
            immediate_evaluated=bool(data.get("immediate_evaluated", False)),
            trigger_duration_waits=tuple(
                TriggerDurationWait.from_dict(item)
                for item in data.get("trigger_duration_waits", [])
                if isinstance(item, dict)
            ),
            cooldown_skip_count=int(data.get("cooldown_skip_count", 0)),
            deliver_when=(
                TriggerDefinition.from_dict(deliver_when_data)
                if deliver_when_data
                else None
            ),
            deliver_when_summary=(
                str(data["deliver_when_summary"])
                if data.get("deliver_when_summary")
                else None
            ),
            complete_when=(
                TriggerDefinition.from_dict(complete_when_data)
                if complete_when_data
                else None
            ),
            complete_when_summary=(
                str(data["complete_when_summary"])
                if data.get("complete_when_summary")
                else None
            ),
            escalation=(
                EscalationPolicy.from_dict(escalation_data) if escalation_data else None
            ),
            source=str(data["source"]) if data.get("source") else None,
            source_id=str(data["source_id"]) if data.get("source_id") else None,
            source_event=(
                str(data["source_event"]) if data.get("source_event") else None
            ),
            managed_externally=bool(data.get("managed_externally", False)),
            allow_manual_completion=bool(data.get("allow_manual_completion", False)),
            external_actions=tuple(
                {"id": str(item["id"]), "label": str(item["label"])}
                for item in data.get("external_actions", [])
            ),
            paused=bool(data.get("paused", False)),
            paused_at=_optional_datetime(data.get("paused_at")),
            missed_occurrence_policy=MissedOccurrencePolicy(
                data.get(
                    "missed_occurrence_policy",
                    MissedOccurrencePolicy.REMIND_ON_STARTUP,
                )
            ),
            expires_after_seconds=(
                int(data["expires_after_seconds"])
                if data.get("expires_after_seconds") is not None
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


def _optional_datetime(value: Any) -> datetime | None:
    return _parse_datetime(value) if value else None


def _parse_time(value: Any) -> time:
    parsed = time.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        raise ValueError("Quiet-hours times must be local wall-clock values")
    return parsed.replace(second=0, microsecond=0)
