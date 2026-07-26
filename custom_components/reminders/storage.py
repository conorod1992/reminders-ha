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
