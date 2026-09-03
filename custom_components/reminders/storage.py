"""Persistent storage for Reminders."""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import SAVE_DELAY, STORAGE_KEY, STORAGE_MINOR_VERSION, STORAGE_VERSION
from .models import (
    ActivationType,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
    UserPreferences,
)

_LOGGER = logging.getLogger(__name__)


class StoredData(TypedDict):
    """Stored reminders schema."""

    reminders: dict[str, dict[str, Any]]
    users: dict[str, dict[str, Any]]


class ReminderStore(Store[StoredData]):
    """Versioned Home Assistant store."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        super().__init__(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            minor_version=STORAGE_MINOR_VERSION,
            atomic_writes=True,
        )
        self._last_requested_data: StoredData | None = None

    async def async_load(self) -> StoredData | None:
        """Load storage and remember the latest logical state."""
        data = await super().async_load()
        self._last_requested_data = data
        return data

    async def async_save(self, data: StoredData) -> None:
        """Persist state, debouncing diagnostic-only cooldown counter changes."""
        previous = self._last_requested_data
        self._last_requested_data = data
        if _is_cooldown_counter_only_update(previous, data):
            self.async_delay_save(self._latest_requested_data, SAVE_DELAY)
            return
        await super().async_save(data)

    def _latest_requested_data(self) -> StoredData:
        """Return the most recent state for a delayed write."""
        assert self._last_requested_data is not None
        return self._last_requested_data

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> StoredData:
        """Migrate old storage versions."""
        if old_major_version == 1:
            normalized = _normalize_storage(old_data)
            if old_minor_version < 2:
                for raw in normalized["reminders"].values():
                    if not isinstance(raw, dict):
                        continue
                    raw.setdefault("recurrence", None)
                    raw.setdefault("scheduled_due", None)
                    raw.setdefault("last_occurrence_due", None)
                    raw.setdefault("last_occurrence_status", None)
            if old_minor_version < 3:
                for raw in normalized["reminders"].values():
                    if not isinstance(raw, dict):
                        continue
                    raw.setdefault("acknowledgement_policy", "default")
                    raw.setdefault("quiet_hours_policy", "respect")
                    raw.setdefault("current_occurrence_number", 1)
                    due = raw.get("due")
                    occurrence_id = f"legacy-{raw.get('id', 'occurrence')}"
                    raw.setdefault("current_occurrence_id", occurrence_id)
                    if due is None:
                        raw.setdefault("occurrence_history", [])
                        continue
                    status = str(raw.get("status", "pending"))
                    occurrence_status = {
                        "pending": "scheduled",
                        "delivering": "delivering",
                    }.get(status, status)
                    raw.setdefault(
                        "occurrence_history",
                        [
                            {
                                "id": occurrence_id,
                                "scheduled_due": raw.get("scheduled_due") or due,
                                "due": due,
                                "status": occurrence_status,
                                "delivered_at": raw.get("delivered_at"),
                                "succeeded_channels": [],
                                "failed_channels": [],
                                "delivery_errors": raw.get("delivery_errors", []),
                                "suppressed_channels": [],
                                "acknowledgement_required": False,
                                "acknowledged_at": None,
                                "acknowledged_by": None,
                                "snoozed": False,
                                "snoozed_at": None,
                            }
                        ],
                    )
                for raw in normalized["users"].values():
                    if not isinstance(raw, dict):
                        continue
                    raw.setdefault("require_acknowledgement", False)
                    raw.setdefault("configured", True)
                    raw.setdefault("history_retention_days", 90)
                    raw.setdefault("history_max_occurrences", 250)
                    raw.setdefault("quiet_hours_enabled", False)
                    raw.setdefault("quiet_hours_start", "23:00")
                    raw.setdefault("quiet_hours_end", "07:00")
                    raw.setdefault("quiet_hours_channels", ["voice"])
                    raw.setdefault(
                        "quiet_hours_fallback_channels",
                        ["persistent_notification"],
                    )
            if old_minor_version < 4:
                for raw in normalized["reminders"].values():
                    if not isinstance(raw, dict):
                        continue
                    raw.setdefault("activation_type", "time")
                    raw.setdefault("trigger", None)
                    raw.setdefault("trigger_summary", None)
                    raw.setdefault("trigger_description", None)
                    raw.setdefault("repeat_policy", "once")
                    raw.setdefault("fire_if_already_matching", False)
                    raw.setdefault("while_awaiting_acknowledgement", "skip")
                    raw.setdefault("cooldown_seconds", 0)
                    raw.setdefault("available_from", None)
                    raw.setdefault("expires_at", None)
                    raw.setdefault("last_triggered_at", None)
                    raw.setdefault("snoozed_until", None)
                    raw.setdefault("immediate_evaluated", False)
                    raw.setdefault("cooldown_skip_count", 0)
            if old_minor_version < 5:
                for raw in normalized["reminders"].values():
                    if not isinstance(raw, dict):
                        continue
                    raw.setdefault("deliver_when", None)
                    raw.setdefault("deliver_when_summary", None)
                    raw.setdefault("complete_when", None)
                    raw.setdefault("complete_when_summary", None)
                    raw.setdefault("escalation", None)
                    for occurrence in raw.get("occurrence_history", []):
                        if not isinstance(occurrence, dict):
                            continue
                        occurrence.setdefault("completion_source", None)
                        occurrence.setdefault("completion_reason", None)
                        occurrence.setdefault("context_eligible_at", None)
                        occurrence.setdefault("notification_action_token", None)
                        occurrence.setdefault("next_escalation_at", None)
                        occurrence.setdefault("escalation_attempt_count", 0)
                        occurrence.setdefault("escalation_history", [])
            if old_minor_version < 6:
                for raw in normalized["reminders"].values():
                    if not isinstance(raw, dict):
                        continue
                    policy = raw.get("delivery_policy")
                    if isinstance(policy, dict):
                        policy.setdefault("mobile_app_services", [])
                    for occurrence in raw.get("occurrence_history", []):
                        if isinstance(occurrence, dict):
                            occurrence.setdefault("redelivery_count", 0)
                for raw in normalized["users"].values():
                    if not isinstance(raw, dict):
                        continue
                    policy = raw.get("default_delivery_policy")
                    if isinstance(policy, dict):
                        policy.setdefault("mobile_app_services", [])
            if old_minor_version < 7:
                for raw in normalized["reminders"].values():
                    if not isinstance(raw, dict):
                        continue
                    raw.setdefault("trigger_duration_started_at", None)
                    raw.setdefault("trigger_duration_cause", None)
                    raw.setdefault("trigger_duration_context", None)
            if old_minor_version < 8:
                for raw in normalized["reminders"].values():
                    if not isinstance(raw, dict):
                        continue
                    started_at = raw.pop("trigger_duration_started_at", None)
                    cause = raw.pop("trigger_duration_cause", None)
                    context = raw.pop("trigger_duration_context", None)
                    raw.setdefault(
                        "trigger_duration_waits",
                        (
                            [
                                {
                                    "role": "activation",
                                    "started_at": started_at,
                                    "cause": cause or "future_transition",
                                    "context": context
                                    if isinstance(context, dict)
                                    else {},
                                    "observed_value": None,
                                }
                            ]
                            if started_at
                            else []
                        ),
                    )
            return normalized
        raise NotImplementedError(
            f"Cannot migrate reminders storage version {old_major_version}."
        )


def empty_storage() -> StoredData:
    """Return empty storage data."""
    return {"reminders": {}, "users": {}}


def deserialize_storage(
    data: StoredData | None,
) -> tuple[dict[str, Reminder], dict[str, UserPreferences]]:
    """Deserialize storage, isolating malformed individual records."""
    reminders: dict[str, Reminder] = {}
    users: dict[str, UserPreferences] = {}
    normalized = _normalize_storage(data)
    for reminder_id, raw in normalized["reminders"].items():
        try:
            reminder = Reminder.from_dict(raw)
            if reminder.id != reminder_id:
                raise ValueError("Reminder ID does not match storage key")
            interrupted_delivery = reminder.status is ReminderStatus.DELIVERING
            recovered_history = tuple(
                _recover_occurrence_state(reminder, occurrence)
                for occurrence in reminder.occurrence_history
            )
            recovered_history = tuple(
                _recover_interrupted_escalation(occurrence)
                for occurrence in recovered_history
            )
            if recovered_history != reminder.occurrence_history:
                reminder = reminder.updated(occurrence_history=recovered_history)
            if interrupted_delivery:
                # No provider currently supplies an idempotency key or delivery
                # receipt. A crash may therefore have happened on either side of
                # the external side effect. Retrying is deliberately conservative:
                # it can duplicate a delivered notification, but never invents
                # success and still recovers a claim whose side effect never ran.
                if reminder.activation_type is ActivationType.TRIGGER:
                    active = next(
                        (
                            occurrence
                            for occurrence in reminder.occurrence_history
                            if occurrence.id == reminder.current_occurrence_id
                        ),
                        None,
                    )
                    reminder = reminder.updated(
                        status=ReminderStatus.WAITING_FOR_TRIGGER,
                        due=active.due if active is not None else reminder.due,
                    )
                else:
                    reminder = reminder.updated(status=ReminderStatus.PENDING)
            if reminder.recurrence and reminder.scheduled_due is None:
                reminder = reminder.updated(scheduled_due=reminder.due)
            reminders[reminder_id] = reminder
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.error("Skipping malformed stored reminder %s: %s", reminder_id, err)
    for user_id, raw in normalized["users"].items():
        try:
            users[user_id] = UserPreferences.from_dict(raw)
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.error(
                "Skipping malformed stored user preferences for %s: %s", user_id, err
            )
    return reminders, users


def _recover_occurrence_state(reminder: Reminder, occurrence: Occurrence) -> Occurrence:
    """Recover interrupted work and stale context waits from durable history."""
    if occurrence.status is OccurrenceStatus.DELIVERING:
        return occurrence.updated(status=OccurrenceStatus.SCHEDULED)
    if (
        occurrence.status is OccurrenceStatus.WAITING_FOR_CONTEXT
        and occurrence.id != reminder.current_occurrence_id
    ):
        return occurrence.updated(
            status=OccurrenceStatus.CANCELLED,
            context_eligible_at=None,
            expires_at=None,
            completion_reason=(
                occurrence.completion_reason or "superseded_context_wait_recovered"
            ),
        )
    return occurrence


def _recover_interrupted_escalation(occurrence: Any) -> Any:
    """Roll back a durable escalation claim whose result was never persisted."""
    in_flight = tuple(
        attempt for attempt in occurrence.escalation_history if attempt.in_flight
    )
    if not in_flight:
        return occurrence
    if occurrence.status is not OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT:
        return occurrence.updated(
            escalation_history=tuple(
                attempt
                if not attempt.in_flight
                else type(attempt)(
                    number=attempt.number,
                    attempted_at=attempt.attempted_at,
                    succeeded_channels=attempt.succeeded_channels,
                    failed_channels=attempt.failed_channels,
                    delivery_errors=attempt.delivery_errors,
                    suppressed_channels=attempt.suppressed_channels,
                    in_flight=False,
                )
                for attempt in occurrence.escalation_history
            )
        )
    completed = tuple(
        attempt for attempt in occurrence.escalation_history if not attempt.in_flight
    )
    retry_at = min(attempt.attempted_at for attempt in in_flight)
    return occurrence.updated(
        escalation_attempt_count=max(
            (attempt.number for attempt in completed), default=0
        ),
        escalation_history=completed,
        next_escalation_at=retry_at,
    )


def serialize_storage(
    reminders: dict[str, Reminder], users: dict[str, UserPreferences]
) -> StoredData:
    """Serialize runtime data."""
    return {
        "reminders": {key: value.to_dict() for key, value in reminders.items()},
        "users": {key: value.to_dict() for key, value in users.items()},
    }


def _is_cooldown_counter_only_update(
    previous: StoredData | None, current: StoredData
) -> bool:
    """Return whether only cooldown diagnostics advanced since the last save request."""
    if previous is None or previous["users"] != current["users"]:
        return False
    before_reminders = previous["reminders"]
    after_reminders = current["reminders"]
    if before_reminders.keys() != after_reminders.keys():
        return False

    changed = False
    for reminder_id, before in before_reminders.items():
        after = after_reminders[reminder_id]
        if before == after:
            continue
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        before_count = before.get("cooldown_skip_count")
        after_count = after.get("cooldown_skip_count")
        if (
            not isinstance(before_count, int)
            or not isinstance(after_count, int)
            or after_count <= before_count
        ):
            return False
        before_payload = {
            key: value
            for key, value in before.items()
            if key not in {"cooldown_skip_count", "updated_at"}
        }
        after_payload = {
            key: value
            for key, value in after.items()
            if key not in {"cooldown_skip_count", "updated_at"}
        }
        if before_payload != after_payload:
            return False
        changed = True
    return changed


def _normalize_storage(data: Any) -> StoredData:
    """Normalize a storage document to the current shape."""
    if not isinstance(data, dict):
        return empty_storage()
    reminders = data.get("reminders", {})
    users = data.get("users", {})
    return {
        "reminders": reminders if isinstance(reminders, dict) else {},
        "users": users if isinstance(users, dict) else {},
    }
