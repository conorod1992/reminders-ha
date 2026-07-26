"""Privacy-safe diagnostics for Reminders."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import RemindersConfigEntry
from .models import ReminderStatus


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RemindersConfigEntry
) -> dict[str, Any]:
    """Return aggregate diagnostics without reminder content or user IDs."""
    manager = entry.runtime_data
    reminders = await manager.async_list()
    return {
        "reminder_count": len(reminders),
        "pending_count": sum(
            reminder.status is ReminderStatus.PENDING for reminder in reminders
        ),
        "failed_count": sum(
            reminder.status is ReminderStatus.FAILED for reminder in reminders
        ),
        "next_due_scheduled": manager.scheduled_for is not None,
    }
