"""Keep Home Assistant persistent notifications aligned with reminder state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import LIFECYCLE_EVENT

_LOGGER = logging.getLogger(__name__)

# External actions intentionally remain visible because selecting one does not
# necessarily resolve the occurrence. Delivery/failure events likewise keep the
# notification visible. These actions represent terminal or deferred resolution.
_DISMISS_ACTIONS = {
    "acknowledged",
    "automatically_completed",
    "cancelled",
    "completed",
    "deleted",
    "expired",
    "skipped",
    "snoozed",
}


def async_register_persistent_cleanup(hass: HomeAssistant) -> Callable[[], None]:
    """Dismiss a reminder's persistent notification when it is resolved."""

    @callback
    def lifecycle_event(event: Event[Any]) -> None:
        action = event.data.get("action")
        reminder_id = event.data.get("reminder_id")
        if action not in _DISMISS_ACTIONS or not isinstance(reminder_id, str):
            return
        hass.async_create_task(
            _async_dismiss(hass, reminder_id),
            f"reminders dismiss persistent notification {reminder_id}",
        )

    return hass.bus.async_listen(LIFECYCLE_EVENT, lifecycle_event)


async def _async_dismiss(hass: HomeAssistant, reminder_id: str) -> None:
    """Dismiss one reminder-scoped persistent notification without failing state."""
    try:
        await hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": f"reminders_{reminder_id}"},
            blocking=True,
        )
    except Exception:
        # Reminder state is already durable at this point. Notification cleanup
        # must never roll back or surface as failure of the user action itself.
        _LOGGER.exception(
            "Unable to dismiss persistent notification for reminder %s",
            reminder_id,
        )
