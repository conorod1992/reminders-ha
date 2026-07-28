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
from homeassistant.util import dt as dt_util

from .authorization import (
    async_get_authorized,
    async_resolve_list_user,
    async_resolve_target_user,
)
from .const import DOMAIN, SUPPORTED_CHANNELS
from .manager import ReminderManager, ReminderNotFoundError, ReminderValidationError
from .models import (
    AcknowledgementPolicy,
    DeliveryPolicy,
    OccurrenceStatus,
    QuietHoursPolicy,
    ReminderStatus,
)
from .recurrence import (
    MonthlyMode,
    RecurrenceError,
    RecurrenceFrequency,
    Weekday,
    preview_occurrences,
)
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
    vol.Optional("monthly_mode"): vol.In(tuple(MonthlyMode)),
    vol.Optional("monthly_weekday"): vol.In(tuple(day.label for day in Weekday)),
    vol.Optional("monthly_week"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
    vol.Optional("end_date"): cv.string,
    vol.Optional("occurrence_count"): vol.All(vol.Coerce(int), vol.Range(min=1)),
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
        query=msg.get("query"),
        limit=msg["limit"],
        offset=msg["offset"],
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
    vol.Optional("acknowledgement_policy", default="default"): vol.In(
        tuple(AcknowledgementPolicy)
    ),
    vol.Optional("quiet_hours_policy", default="respect"): vol.In(
        tuple(QuietHoursPolicy)
    ),
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
        acknowledgement_policy=AcknowledgementPolicy(msg["acknowledgement_policy"]),
        quiet_hours_policy=QuietHoursPolicy(msg["quiet_hours_policy"]),
    )
    connection.send_result(msg["id"], {"reminder": reminder.to_dict()})


CREATE_RECURRING_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): f"{COMMAND_PREFIX}create_recurring",
    vol.Required("title"): cv.string,
    vol.Optional("message"): vol.Any(None, cv.string),
    vol.Optional("user_id"): cv.string,
    vol.Optional("acknowledgement_policy", default="default"): vol.In(
        tuple(AcknowledgementPolicy)
    ),
    vol.Optional("quiet_hours_policy", default="respect"): vol.In(
        tuple(QuietHoursPolicy)
    ),
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
        acknowledgement_policy=AcknowledgementPolicy(msg["acknowledgement_policy"]),
        quiet_hours_policy=QuietHoursPolicy(msg["quiet_hours_policy"]),
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
    vol.Optional("monthly_mode"): vol.In(tuple(MonthlyMode)),
    vol.Optional("monthly_weekday"): vol.In(tuple(day.label for day in Weekday)),
    vol.Optional("monthly_week"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
    vol.Optional("end_date"): vol.Any(None, cv.string),
    vol.Optional("occurrence_count"): vol.Any(
        None, vol.All(vol.Coerce(int), vol.Range(min=1))
    ),
    vol.Optional("acknowledgement_policy"): vol.In(tuple(AcknowledgementPolicy)),
    vol.Optional("quiet_hours_policy"): vol.In(tuple(QuietHoursPolicy)),
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
    if "acknowledgement_policy" in msg:
        changes["acknowledgement_policy"] = AcknowledgementPolicy(
            msg["acknowledgement_policy"]
        )
    if "quiet_hours_policy" in msg:
        changes["quiet_hours_policy"] = QuietHoursPolicy(msg["quiet_hours_policy"])
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
        "monthly_mode",
        "monthly_weekday",
        "monthly_week",
        "end_date",
        "occurrence_count",
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
        vol.Required("type"): f"{COMMAND_PREFIX}acknowledge",
        vol.Required("reminder_id"): cv.string,
        vol.Optional("occurrence_id"): cv.string,
    }
)
@async_response
@_api_errors
async def websocket_acknowledge(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    manager = _manager(hass)
    reminder = await async_get_authorized(manager, connection.user, msg["reminder_id"])
    occurrence = await manager.async_acknowledge(
        reminder.id,
        occurrence_id=msg.get("occurrence_id"),
        acknowledged_by=connection.user.id,
    )
    connection.send_result(msg["id"], {"occurrence": occurrence.to_dict()})


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}history",
        vol.Optional("scope", default="mine"): vol.In(("mine", "all", "user")),
        vol.Optional("user_id"): cv.string,
        vol.Optional("query"): cv.string,
        vol.Optional("statuses"): vol.All(
            cv.ensure_list, [vol.In(tuple(OccurrenceStatus))]
        ),
        vol.Optional("due_after"): cv.string,
        vol.Optional("due_before"): cv.string,
        vol.Optional("limit", default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=200)
        ),
        vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    }
)
@async_response
@_api_errors
async def websocket_history(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    scope = msg["scope"]
    if scope == "user" and "user_id" not in msg:
        raise HomeAssistantError("user_id is required for user scope")
    user_id = await async_resolve_list_user(
        hass,
        connection.user,
        msg.get("user_id") if scope == "user" else None,
        all_users=scope == "all",
    )
    rows, total = await _manager(hass).async_history(
        user_id=user_id,
        query=msg.get("query"),
        statuses=(
            {OccurrenceStatus(value) for value in msg["statuses"]}
            if "statuses" in msg
            else None
        ),
        due_after=(
            _parse_datetime(hass, msg["due_after"]) if "due_after" in msg else None
        ),
        due_before=(
            _parse_datetime(hass, msg["due_before"]) if "due_before" in msg else None
        ),
        limit=msg["limit"],
        offset=msg["offset"],
    )
    if connection.user.is_admin and scope != "mine":
        names = await _user_names(hass)
        for row in rows:
            if row["user_id"] in names:
                row["owner_name"] = names[row["user_id"]]
    connection.send_result(msg["id"], {"history": rows, "total": total})


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}preview_recurrence",
        **RECURRENCE_SCHEMA,
        vol.Optional("limit", default=5): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=10)
        ),
    }
)
@async_response
@_api_errors
async def websocket_preview_recurrence(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    rule = _recurrence_from_data(hass, msg)
    values = preview_occurrences(rule, after=dt_util.utcnow(), limit=msg["limit"])
    connection.send_result(
        msg["id"], {"occurrences": [value.isoformat() for value in values]}
    )


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
        vol.Optional("require_acknowledgement", default=False): cv.boolean,
        vol.Optional("configured", default=True): cv.boolean,
        vol.Optional("history_retention_days", default=90): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=3650)
        ),
        vol.Optional("history_max_occurrences", default=250): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=5000)
        ),
        vol.Optional("quiet_hours_enabled", default=False): cv.boolean,
        vol.Optional("quiet_hours_start", default="23:00"): cv.time,
        vol.Optional("quiet_hours_end", default="07:00"): cv.time,
        vol.Optional("quiet_hours_channels", default=["voice"]): vol.All(
            cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]
        ),
        vol.Optional(
            "quiet_hours_fallback_channels", default=["persistent_notification"]
        ): vol.All(cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]),
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
        **{
            key: msg[key]
            for key in (
                "require_acknowledgement",
                "configured",
                "history_retention_days",
                "history_max_occurrences",
                "quiet_hours_enabled",
                "quiet_hours_start",
                "quiet_hours_end",
                "quiet_hours_channels",
                "quiet_hours_fallback_channels",
            )
            if key in msg
        },
    )
    connection.send_result(
        msg["id"], {"user_id": user_id, "preferences": preferences.to_dict()}
    )


@websocket_command(
    {
        vol.Required("type"): f"{COMMAND_PREFIX}test_delivery",
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
async def websocket_test_delivery(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    user_id = await async_resolve_target_user(hass, connection.user, msg.get("user_id"))
    result = await _manager(hass).async_test_delivery(
        user_id=user_id,
        policy=DeliveryPolicy(
            tuple(msg["channels"]),
            tuple(msg["notify_targets"]),
            tuple(msg["voice_targets"]),
        ),
    )
    connection.send_result(
        msg["id"],
        {
            "succeeded_channels": list(result.succeeded),
            "failed_channels": list(result.failed_channels),
            "errors": list(result.errors),
        },
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
