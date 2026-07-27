"""Authenticated WebSocket API for the Reminders management panel."""

from __future__ import annotations

from datetime import timedelta
from functools import wraps
from typing import Any, cast

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

from .authorization import (
    async_get_authorized,
    async_resolve_list_user,
    async_resolve_target_user,
)
from .const import DOMAIN, SUPPORTED_CHANNELS
from .manager import ReminderManager, ReminderNotFoundError, ReminderValidationError
from .models import DeliveryPolicy, ReminderStatus
from .recurrence import RecurrenceError, RecurrenceFrequency, Weekday
from .services import _parse_datetime, _policy_from_data, _recurrence_from_data

COMMAND_PREFIX = f"{DOMAIN}/"
POLICY_SCHEMA: dict[Any, Any] = {
    vol.Optional("delivery_mode", default="default"): vol.In(("default", "custom")),
    vol.Optional("channels"): vol.All(cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]),
    vol.Optional("notify_targets"): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional("voice_targets"): vol.All(cv.ensure_list, [cv.entity_id]),
}
RECURRENCE_SCHEMA: dict[Any, Any] = {
    vol.Required("first_reminder"): cv.string,
    vol.Required("frequency"): vol.In(tuple(RecurrenceFrequency)),
    vol.Optional("interval", default=1): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("weekdays"): vol.All(
        cv.ensure_list, [vol.In(tuple(day.label for day in Weekday))]
    ),
    vol.Optional("day_of_month"): vol.All(vol.Coerce(int), vol.Range(min=1, max=31)),
    vol.Optional("timezone"): cv.string,
}


def _manager(hass: HomeAssistant) -> ReminderManager:
    manager = hass.data.get(DOMAIN)
    if not isinstance(manager, ReminderManager):
        raise HomeAssistantError("The Reminders integration is not loaded")
    return manager


def _api_errors(handler: Any) -> Any:
    """Translate domain validation into stable WebSocket failures."""

    @wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except ReminderNotFoundError as err:
            raise HomeAssistantError(f"Unknown reminder ID: {err.args[0]}") from err
        except (ReminderValidationError, RecurrenceError) as err:
            raise HomeAssistantError(str(err)) from err

    return wrapped


def _serialize(reminder: Any, names: dict[str, str]) -> dict[str, Any]:
    result = cast(dict[str, Any], reminder.to_dict())
    if reminder.user_id in names:
        result["owner_name"] = names[reminder.user_id]
    return result


async def _user_names(hass: HomeAssistant) -> dict[str, str]:
    return {
        user.id: user.name or "Unnamed user"
        for user in await hass.auth.async_get_users()
        if user.is_active and not user.system_generated
    }


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
    }
)
@async_response
@_api_errors
async def websocket_list(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """List only records authorized for the authenticated connection."""
    scope = msg["scope"]
    if scope == "user" and "user_id" not in msg:
        raise HomeAssistantError("user_id is required for user scope")
    user_id = await async_resolve_list_user(
        hass,
        connection.user,
        msg.get("user_id") if scope == "user" else None,
        all_users=scope == "all",
    )
    reminders = await _manager(hass).async_list(
        user_id=user_id,
        due_after=(
            _parse_datetime(hass, msg["due_after"]) if "due_after" in msg else None
        ),
        due_before=(
            _parse_datetime(hass, msg["due_before"]) if "due_before" in msg else None
        ),
    )
    view = msg["view"]
    if view == "upcoming":
        reminders = [
            item for item in reminders if item.status is ReminderStatus.PENDING
        ]
    elif view == "recurring":
        reminders = [item for item in reminders if item.recurrence is not None]
    elif view == "failed":
        reminders = [
            item
            for item in reminders
            if item.status is ReminderStatus.FAILED
            or item.last_occurrence_status is ReminderStatus.FAILED
        ]
    names = (
        await _user_names(hass) if connection.user.is_admin and scope != "mine" else {}
    )
    connection.send_result(
        msg["id"],
        {"reminders": [_serialize(item, names) for item in reminders]},
    )


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}get",
        vol.Required("reminder_id"): cv.string,
    }
)
@async_response
@_api_errors
async def websocket_get(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    reminder = await async_get_authorized(
        _manager(hass), connection.user, msg["reminder_id"]
    )
    connection.send_result(msg["id"], {"reminder": reminder.to_dict()})


CREATE_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): f"{COMMAND_PREFIX}create",
    vol.Required("title"): cv.string,
    vol.Optional("message"): vol.Any(None, cv.string),
    vol.Required("due"): cv.string,
    vol.Optional("user_id"): cv.string,
    **POLICY_SCHEMA,
}


@websocket_command(CREATE_SCHEMA)
@async_response
@_api_errors
async def websocket_create(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    user_id = await async_resolve_target_user(hass, connection.user, msg.get("user_id"))
    reminder = await _manager(hass).async_create(
        user_id=user_id,
        title=msg["title"],
        message=msg.get("message"),
        due=_parse_datetime(hass, msg["due"]),
        delivery_policy=_policy_from_data(msg),
    )
    connection.send_result(msg["id"], {"reminder": reminder.to_dict()})


CREATE_RECURRING_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): f"{COMMAND_PREFIX}create_recurring",
    vol.Required("title"): cv.string,
    vol.Optional("message"): vol.Any(None, cv.string),
    vol.Optional("user_id"): cv.string,
    **RECURRENCE_SCHEMA,
    **POLICY_SCHEMA,
}


@websocket_command(CREATE_RECURRING_SCHEMA)
@async_response
@_api_errors
async def websocket_create_recurring(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    user_id = await async_resolve_target_user(hass, connection.user, msg.get("user_id"))
    reminder = await _manager(hass).async_create_recurring(
        user_id=user_id,
        title=msg["title"],
        message=msg.get("message"),
        recurrence=_recurrence_from_data(hass, msg),
        delivery_policy=_policy_from_data(msg),
    )
    connection.send_result(msg["id"], {"reminder": reminder.to_dict()})


UPDATE_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): f"{COMMAND_PREFIX}update",
    vol.Required("reminder_id"): cv.string,
    vol.Optional("title"): cv.string,
    vol.Optional("message"): vol.Any(None, cv.string),
    vol.Optional("due"): cv.string,
    vol.Optional("user_id"): cv.string,
    vol.Optional("first_reminder"): cv.string,
    vol.Optional("frequency"): vol.In(tuple(RecurrenceFrequency)),
    vol.Optional("interval"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("weekdays"): vol.All(
        cv.ensure_list, [vol.In(tuple(day.label for day in Weekday))]
    ),
    vol.Optional("day_of_month"): vol.All(vol.Coerce(int), vol.Range(min=1, max=31)),
    vol.Optional("timezone"): cv.string,
    vol.Optional("delivery_mode"): vol.In(("default", "custom")),
    vol.Optional("channels"): vol.All(cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]),
    vol.Optional("notify_targets"): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional("voice_targets"): vol.All(cv.ensure_list, [cv.entity_id]),
}


@websocket_command(UPDATE_SCHEMA)
@async_response
@_api_errors
async def websocket_update(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    manager = _manager(hass)
    reminder = await async_get_authorized(manager, connection.user, msg["reminder_id"])
    changes: dict[str, Any] = {
        key: msg[key] for key in ("title", "message") if key in msg
    }
    if "due" in msg:
        changes["due"] = _parse_datetime(hass, msg["due"])
    if "user_id" in msg:
        changes["user_id"] = await async_resolve_target_user(
            hass, connection.user, msg["user_id"]
        )
    policy_keys = {"delivery_mode", "channels", "notify_targets", "voice_targets"}
    if policy_keys.intersection(msg):
        changes["delivery_policy"] = _policy_from_data(msg)
    recurrence_keys = {
        "first_reminder",
        "frequency",
        "interval",
        "weekdays",
        "day_of_month",
        "timezone",
    }
    if recurrence_keys.intersection(msg):
        if reminder.recurrence is None:
            raise HomeAssistantError("Recurrence fields require a recurring reminder")
        changes["recurrence"] = _recurrence_from_data(hass, msg, reminder.recurrence)
    updated = await manager.async_update(reminder.id, **changes)
    connection.send_result(msg["id"], {"reminder": updated.to_dict()})


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}delete",
        vol.Required("reminder_id"): cv.string,
    }
)
@async_response
@_api_errors
async def websocket_delete(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    manager = _manager(hass)
    reminder = await async_get_authorized(manager, connection.user, msg["reminder_id"])
    await manager.async_delete(reminder.id)
    connection.send_result(msg["id"])


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}snooze",
        vol.Required("reminder_id"): cv.string,
        vol.Exclusive("due", "when"): cv.string,
        vol.Exclusive("duration_seconds", "when"): vol.All(
            vol.Coerce(int), vol.Range(min=1)
        ),
    }
)
@async_response
@_api_errors
async def websocket_snooze(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    if "due" not in msg and "duration_seconds" not in msg:
        raise HomeAssistantError("Provide due or duration_seconds")
    manager = _manager(hass)
    reminder = await async_get_authorized(manager, connection.user, msg["reminder_id"])
    updated = await manager.async_snooze(
        reminder.id,
        due=_parse_datetime(hass, msg["due"]) if "due" in msg else None,
        duration=(
            timedelta(seconds=msg["duration_seconds"])
            if "duration_seconds" in msg
            else None
        ),
    )
    connection.send_result(msg["id"], {"reminder": updated.to_dict()})


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}get_preferences",
        vol.Optional("user_id"): cv.string,
    }
)
@async_response
@_api_errors
async def websocket_get_preferences(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    user_id = await async_resolve_target_user(hass, connection.user, msg.get("user_id"))
    preferences = await _manager(hass).async_get_user_preferences(user_id)
    connection.send_result(
        msg["id"], {"user_id": user_id, "preferences": preferences.to_dict()}
    )


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}set_preferences",
        vol.Optional("user_id"): cv.string,
        vol.Required("channels"): vol.All(cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]),
        vol.Optional("notify_targets", default=[]): vol.All(
            cv.ensure_list, [cv.entity_id]
        ),
        vol.Optional("voice_targets", default=[]): vol.All(
            cv.ensure_list, [cv.entity_id]
        ),
    }
)
@async_response
@_api_errors
async def websocket_set_preferences(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    user_id = await async_resolve_target_user(hass, connection.user, msg.get("user_id"))
    preferences = await _manager(hass).async_set_user_preferences(
        user_id,
        DeliveryPolicy(
            tuple(msg["channels"]),
            tuple(msg["notify_targets"]),
            tuple(msg["voice_targets"]),
        ),
    )
    connection.send_result(
        msg["id"], {"user_id": user_id, "preferences": preferences.to_dict()}
    )


@websocket_command({vol.Required("type"): f"{COMMAND_PREFIX}users"})
@async_response
@_api_errors
async def websocket_users(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    if not connection.user.is_admin:
        raise HomeAssistantError("Only administrators can list users")
    users = [
        {"id": user.id, "name": user.name or "Unnamed user"}
        for user in await hass.auth.async_get_users()
        if user.is_active and not user.system_generated
    ]
    connection.send_result(msg["id"], {"users": users})


@websocket_command({vol.Required("type"): f"{COMMAND_PREFIX}subscribe"})
@callback
def websocket_subscribe(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Subscribe to relevant invalidations, never reminder contents."""

    @callback
    def changed(user_ids: frozenset[str]) -> None:
        if connection.user.is_admin or connection.user.id in user_ids:
            connection.send_event(msg["id"], {"changed": True})

    connection.subscriptions[msg["id"]] = _manager(hass).async_subscribe(changed)
    connection.send_result(msg["id"])


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
        websocket_get_preferences,
        websocket_set_preferences,
        websocket_users,
        websocket_subscribe,
    ):
        async_register_command(hass, handler)
