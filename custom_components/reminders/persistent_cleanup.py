"""Keep Home Assistant persistent notifications aligned with reminder state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, LIFECYCLE_EVENT

_LOGGER = logging.getLogger(__name__)
PERSISTENT_NOTIFICATION_DATA = f"{DOMAIN}_persistent_notification_ids"

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


def persistent_notification_id(reminder_id: str, occurrence_id: str | None) -> str:
    """Return a stable notification ID without conflating series occurrences."""
    if occurrence_id:
        return f"reminders_{reminder_id}_{occurrence_id}"
    return f"reminders_{reminder_id}"


@callback
def track_persistent_notification(
    hass: HomeAssistant, reminder_id: str, notification_id: str
) -> None:
    """Track notification IDs so deleting a series can dismiss all live copies."""
    tracked = hass.data.setdefault(PERSISTENT_NOTIFICATION_DATA, {})
    tracked.setdefault(reminder_id, set()).add(notification_id)


def async_register_persistent_cleanup(hass: HomeAssistant) -> Callable[[], None]:
    """Dismiss persistent notifications when their reminder occurrence resolves."""

    @callback
    def lifecycle_event(event: Event[Any]) -> None:
        action = event.data.get("action")
        reminder_id = event.data.get("reminder_id")
        occurrence_id = event.data.get("occurrence_id")
        if action not in _DISMISS_ACTIONS or not isinstance(reminder_id, str):
            return
        hass.async_create_task(
            _async_dismiss(
                hass,
                reminder_id,
                occurrence_id if isinstance(occurrence_id, str) else None,
                dismiss_all=action == "deleted",
            ),
            f"reminders dismiss persistent notification {reminder_id}",
        )

    return hass.bus.async_listen(LIFECYCLE_EVENT, lifecycle_event)


async def _async_dismiss(
    hass: HomeAssistant,
    reminder_id: str,
    occurrence_id: str | None,
    *,
    dismiss_all: bool = False,
) -> None:
    """Dismiss matching persistent notifications without failing reminder state."""
    tracked = hass.data.setdefault(PERSISTENT_NOTIFICATION_DATA, {})
    known = tracked.get(reminder_id, set())
    if dismiss_all:
        notification_ids = set(known)
        # Also remove the pre-occurrence-scoping ID left by older releases.
        notification_ids.add(persistent_notification_id(reminder_id, None))
    else:
        notification_ids = {persistent_notification_id(reminder_id, occurrence_id)}
        # If an older version delivered this occurrence, its notification used
        # the reminder-scoped ID. Dismissing it as well makes upgrades self-heal.
        notification_ids.add(persistent_notification_id(reminder_id, None))

    for notification_id in notification_ids:
        try:
            await hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": notification_id},
                blocking=True,
            )
        except Exception:
            # Reminder state is already durable at this point. Notification cleanup
            # must never roll back or surface as failure of the user action itself.
            _LOGGER.exception(
                "Unable to dismiss persistent notification %s for reminder %s",
                notification_id,
                reminder_id,
            )
        else:
            known.discard(notification_id)

    if dismiss_all or not known:
        tracked.pop(reminder_id, None)
