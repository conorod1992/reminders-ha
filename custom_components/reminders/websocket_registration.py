"""Register Reminders WebSocket handlers with compact, view-aware listing."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    websocket_command,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .authorization import async_resolve_list_user
from .const import DOMAIN
from .models import ActivationType, OccurrenceStatus, Reminder, ReminderStatus
from .native_websocket import NATIVE_WEBSOCKET_HANDLERS
from .services import _parse_datetime
from .websocket_api import (
    _api_errors,
    _manager,
    _user_names,
    websocket_acknowledge,
    websocket_complete,
    websocket_create,
    websocket_create_recurring,
    websocket_create_triggered,
    websocket_delete,
    websocket_external_action,
    websocket_fire_trigger,
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


def _attention_reason(reminder: Reminder) -> str | None:
    """Return why a reminder currently needs user attention, if it does."""
    if reminder.status is ReminderStatus.FAILED:
        return "delivery_failed"
    if reminder.status is ReminderStatus.AWAITING_ACKNOWLEDGEMENT or any(
        item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
        for item in reminder.occurrence_history
    ):
        return "awaiting_acknowledgement"
    if (reminder.allow_manual_completion or reminder.external_actions) and (
        reminder.status is ReminderStatus.DELIVERED
        or any(
            item.status is OccurrenceStatus.DELIVERED
            for item in reminder.occurrence_history
        )
    ):
        return "action_available"
    if reminder.last_occurrence_status is ReminderStatus.FAILED:
        return "recent_delivery_failed"
    return None


def _serialize_summary(reminder: Reminder, names: dict[str, str]) -> dict[str, Any]:
    """Serialize a list row without embedding unrelated retained history."""
    result = reminder.to_dict()
    result["occurrence_history"] = [
        item.to_dict()
        for item in reminder.occurrence_history
        if item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
        or (
            item.status is OccurrenceStatus.DELIVERED
            and (reminder.allow_manual_completion or reminder.external_actions)
        )
    ]
    attention_reason = _attention_reason(reminder)
    if attention_reason is not None:
        result["attention_reason"] = attention_reason
    if reminder.user_id in names:
        result["owner_name"] = names[reminder.user_id]
    return result


async def _filtered_page(
    hass: HomeAssistant,
    *,
    user_id: str | None,
    query: str | None,
    due_after: Any,
    due_before: Any,
    limit: int,
    offset: int,
    predicate: Any,
) -> list[Reminder]:
    """Apply a derived predicate before the caller's requested page bounds."""
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
        matches.extend(item for item in page if predicate(item))
        if len(page) < 1000 or len(matches) >= offset + limit:
            break
        source_offset += 1000
    return matches[offset : offset + limit]


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
    return await _filtered_page(
        hass,
        user_id=user_id,
        query=query,
        due_after=due_after,
        due_before=due_before,
        limit=limit,
        offset=offset,
        predicate=lambda item: item.status is ReminderStatus.FAILED
        or item.last_occurrence_status is ReminderStatus.FAILED,
    )


async def _attention_page(
    hass: HomeAssistant,
    *,
    user_id: str | None,
    query: str | None,
    due_after: Any,
    due_before: Any,
    limit: int,
    offset: int,
) -> list[Reminder]:
    """Filter actionable/problem reminders before applying page bounds."""
    return await _filtered_page(
        hass,
        user_id=user_id,
        query=query,
        due_after=due_after,
        due_before=due_before,
        limit=limit,
        offset=offset,
        predicate=lambda item: _attention_reason(item) is not None,
    )


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}list",
        vol.Optional("scope", default="mine"): vol.In(("mine", "all", "user")),
        vol.Optional("user_id"): cv.string,
        vol.Optional("view", default="upcoming"): vol.In(
            (
                "attention",
                "upcoming",
                "recurring",
                "triggered",
                "waiting_for_trigger",
                "expired",
                "failed",
                "all",
            )
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
    due_after = _parse_datetime(hass, msg["due_after"]) if "due_after" in msg else None
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
    elif view == "attention":
        reminders = await _attention_page(
            hass,
            user_id=user_id,
            query=msg.get("query"),
            due_after=due_after,
            due_before=due_before,
            limit=msg["limit"],
            offset=msg["offset"],
        )
    else:
        statuses: set[ReminderStatus] | None = None
        activation_type = None
        recurring = None
        if view == "upcoming":
            statuses = {
                ReminderStatus.PENDING,
                ReminderStatus.AWAITING_ACKNOWLEDGEMENT,
                ReminderStatus.DELIVERED,
            }
            activation_type = ActivationType.TIME
        elif view == "recurring":
            recurring = True
        elif view == "triggered":
            activation_type = ActivationType.TRIGGER
        elif view == "waiting_for_trigger":
            statuses = {ReminderStatus.WAITING_FOR_TRIGGER}
        elif view == "expired":
            statuses = {ReminderStatus.EXPIRED}
        reminders = await manager.async_list(
            user_id=user_id,
            due_after=due_after,
            due_before=due_before,
            query=msg.get("query"),
            recurring=recurring,
            activation_type=activation_type,
            statuses=statuses,
            limit=msg["limit"],
            offset=msg["offset"],
        )
        if view == "upcoming":
            reminders = [
                item
                for item in reminders
                if item.status is not ReminderStatus.DELIVERED
                or item.allow_manual_completion
                or bool(item.external_actions)
            ]
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
        websocket_create_triggered,
        websocket_fire_trigger,
        websocket_update,
        websocket_delete,
        websocket_snooze,
        websocket_acknowledge,
        websocket_complete,
        websocket_external_action,
        websocket_history,
        websocket_preview_recurrence,
        websocket_get_preferences,
        websocket_set_preferences,
        websocket_test_delivery,
        websocket_users,
        websocket_subscribe,
        *NATIVE_WEBSOCKET_HANDLERS,
    ):
        async_register_command(hass, handler)
