"""Register Reminders WebSocket handlers with compact, view-aware listing."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import async_response, websocket_command
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .authorization import async_resolve_list_user
from .const import DOMAIN
from .models import OccurrenceStatus, Reminder, ReminderStatus
from .services import _parse_datetime
from .websocket_api import (
    _api_errors,
    _manager,
    _user_names,
    websocket_acknowledge,
    websocket_create,
    websocket_create_recurring,
    websocket_delete,
    websocket_get,
    websocket_get_preferences,
    websocket_history,
    websocket_preview_recurrence,
    websocket_set_preferences,
    websocket_snooze,
    websocket_subscribe,
    websocket_test_delivery,
    websocket_update,
    websocket_users,
)

COMMAND_PREFIX = f"{DOMAIN}/"


def _serialize_summary(reminder: Reminder, names: dict[str, str]) -> dict[str, Any]:
    """Serialize a list row without embedding unrelated retained history."""
    result = cast(dict[str, Any], reminder.to_dict())
    result["occurrence_history"] = [
        item.to_dict()
        for item in reminder.occurrence_history
        if item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
    ]
    if reminder.user_id in names:
        result["owner_name"] = names[reminder.user_id]
    return result


async def _failed_page(
    hass: HomeAssistant,
    *,
    user_id: str | None,
    query: str | None,
    due_after: Any,
    due_before: Any,
    limit: int,
    offset: int,
) -> list[Reminder]:
    """Filter failed reminders before applying the caller's page bounds."""
    manager = _manager(hass)
    matches: list[Reminder] = []
    source_offset = 0
    while True:
        page = await manager.async_list(
            user_id=user_id,
            due_after=due_after,
            due_before=due_before,
            query=query,
            limit=1000,
            offset=source_offset,
        )
        matches.extend(
            item
            for item in page
            if item.status is ReminderStatus.FAILED
            or item.last_occurrence_status is ReminderStatus.FAILED
        )
        if len(page) < 1000:
            break
        source_offset += 1000
        if len(matches) >= offset + limit and page[-1].due > matches[-1].due:
            # Results are due-sorted, but continue only when later pages could still
            # contribute before the requested slice.
            break
    return matches[offset : offset + limit]


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}list",
        vol.Optional("scope", default="mine"): vol.In(("mine", "all", "user")),
        vol.Optional("user_id"): cv.string,
        vol.Optional("view", default="upcoming"): vol.In(
            ("upcoming", "recurring", "failed", "all")
        ),
        vol.Optional("due_after"): cv.string,
        vol.Optional("due_before"): cv.string,
        vol.Optional("query"): cv.string,
        vol.Optional("limit", default=500): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1000)
        ),
        vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    }
)
@async_response
@_api_errors
async def websocket_list(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """List authorized reminders with filtering applied before pagination."""
    scope = msg["scope"]
    if scope == "user" and "user_id" not in msg:
        raise HomeAssistantError("user_id is required for user scope")
    user_id = await async_resolve_list_user(
        hass,
        connection.user,
        msg.get("user_id") if scope == "user" else None,
        all_users=scope == "all",
    )
    due_after = (
        _parse_datetime(hass, msg["due_after"]) if "due_after" in msg else None
    )
    due_before = (
        _parse_datetime(hass, msg["due_before"]) if "due_before" in msg else None
    )
    manager = _manager(hass)
    view = msg["view"]
    if view == "failed":
        reminders = await _failed_page(
            hass,
            user_id=user_id,
            query=msg.get("query"),
            due_after=due_after,
            due_before=due_before,
            limit=msg["limit"],
            offset=msg["offset"],
        )
    else:
        statuses = (
            {
                ReminderStatus.PENDING,
                ReminderStatus.AWAITING_ACKNOWLEDGEMENT,
            }
            if view == "upcoming"
            else None
        )
        reminders = await manager.async_list(
            user_id=user_id,
            due_after=due_after,
            due_before=due_before,
            query=msg.get("query"),
            recurring=True if view == "recurring" else None,
            statuses=statuses,
            limit=msg["limit"],
            offset=msg["offset"],
        )
    names = (
        await _user_names(hass) if connection.user.is_admin and scope != "mine" else {}
    )
    connection.send_result(
        msg["id"],
        {"reminders": [_serialize_summary(item, names) for item in reminders]},
    )


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register integration-global command handlers exactly once."""
    for handler in (
        websocket_list,
        websocket_get,
        websocket_create,
        websocket_create_recurring,
        websocket_update,
        websocket_delete,
        websocket_snooze,
        websocket_acknowledge,
        websocket_history,
        websocket_preview_recurrence,
        websocket_get_preferences,
        websocket_set_preferences,
        websocket_test_delivery,
        websocket_users,
        websocket_subscribe,
    ):
        async_register_command(hass, handler)
