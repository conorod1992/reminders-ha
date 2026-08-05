"""Persistent storage for Reminders."""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_MINOR_VERSION, STORAGE_VERSION
from .models import Reminder, ReminderStatus, UserPreferences

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
            if reminder.status is ReminderStatus.DELIVERING:
                reminder = reminder.updated(
                    status=(
                        ReminderStatus.WAITING_FOR_TRIGGER
                        if reminder.trigger is not None
                        else ReminderStatus.PENDING
                    )
                )
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


def serialize_storage(
    reminders: dict[str, Reminder], users: dict[str, UserPreferences]
) -> StoredData:
    """Serialize runtime data."""
    return {
        "reminders": {key: value.to_dict() for key, value in reminders.items()},
        "users": {key: value.to_dict() for key, value in users.items()},
    }


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
