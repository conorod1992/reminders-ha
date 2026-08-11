"""Privacy-safe diagnostics for Reminders."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import RemindersConfigEntry
from .models import ActivationType, ReminderStatus


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
        "recurring_count": sum(
            reminder.recurrence is not None for reminder in reminders
        ),
        "triggered_count": sum(
            reminder.activation_type is ActivationType.TRIGGER for reminder in reminders
        ),
        "trigger_listener_count": manager.trigger_listener_count,
        "contextual_delivery_count": sum(
            reminder.deliver_when is not None for reminder in reminders
        ),
        "automatic_completion_count": sum(
            reminder.complete_when is not None for reminder in reminders
        ),
        "escalation_count": sum(
            reminder.escalation is not None for reminder in reminders
        ),
        "waiting_for_context_count": sum(
            reminder.status is ReminderStatus.WAITING_FOR_CONTEXT
            for reminder in reminders
        ),
        "next_due_scheduled": manager.scheduled_for is not None,
    }
