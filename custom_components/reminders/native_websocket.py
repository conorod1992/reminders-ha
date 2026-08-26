"""WebSocket endpoints for Home Assistant-native reminder rules."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    websocket_command,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .authorization import async_get_authorized, async_resolve_target_user
from .models import (
    AcknowledgementPolicy,
    QuietHoursPolicy,
    TriggerRepeatPolicy,
    WhileAwaitingAcknowledgement,
)
from .native_crud import (
    async_create_native_triggered,
    async_update_native_triggered,
)
from .native_manager import NativeReminderManager
from .services import _parse_datetime, _policy_from_data, _source_from_data
from .websocket_api import (
    COMMAND_PREFIX,
    POLICY_SCHEMA,
    SOURCE_SCHEMA,
    _api_errors,
    _manager,
)

NATIVE_LIST = vol.All(cv.ensure_list, [dict])


def _native_manager(hass: HomeAssistant) -> NativeReminderManager:
    manager = _manager(hass)
    if not isinstance(manager, NativeReminderManager):
        raise HomeAssistantError("Native reminder rules are not available")
    return manager


@websocket_command(
    vol.All(
        {
            vol.Required("type"): f"{COMMAND_PREFIX}set_native_rules",
            vol.Required("reminder_id"): cv.string,
            vol.Optional("activation_triggers"): NATIVE_LIST,
            vol.Optional("delivery_triggers"): NATIVE_LIST,
            vol.Optional("delivery_conditions"): NATIVE_LIST,
            vol.Optional("completion_triggers"): NATIVE_LIST,
        },
        cv.has_at_least_one_key(
            "activation_triggers",
            "delivery_triggers",
            "delivery_conditions",
            "completion_triggers",
        ),
    )
)
@async_response
@_api_errors
async def websocket_set_native_rules(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Validate and replace native rules on an authorized reminder."""
    manager = _native_manager(hass)
    reminder = await async_get_authorized(
        manager, connection.user, msg["reminder_id"]
    )
    updated = await manager.async_set_native_rules(
        reminder.id,
        **{
            key: msg[key]
            for key in (
                "activation_triggers",
                "delivery_triggers",
                "delivery_conditions",
                "completion_triggers",
            )
            if key in msg
        },
    )
    connection.send_result(msg["id"], {"reminder": updated.to_dict()})


CREATE_NATIVE_TRIGGERED_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): f"{COMMAND_PREFIX}create_native_triggered",
    vol.Required("title"): cv.string,
    vol.Optional("message"): vol.Any(None, cv.string),
    vol.Required("activation_triggers"): vol.All(NATIVE_LIST, vol.Length(min=1)),
    vol.Optional("completion_triggers", default=[]): NATIVE_LIST,
    vol.Optional("user_id"): cv.string,
    vol.Optional("acknowledgement_policy", default="default"): vol.In(
        tuple(AcknowledgementPolicy)
    ),
    vol.Optional("quiet_hours_policy", default="respect"): vol.In(
        tuple(QuietHoursPolicy)
    ),
    vol.Optional("repeat_policy", default="once"): vol.In(tuple(TriggerRepeatPolicy)),
    vol.Optional("while_awaiting_acknowledgement", default="skip"): vol.In(
        tuple(WhileAwaitingAcknowledgement)
    ),
    vol.Optional("cooldown_seconds", default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=31_536_000)
    ),
    vol.Optional("available_from"): cv.string,
    vol.Optional("expires_at"): cv.string,
    vol.Optional("trigger_description"): cv.string,
    **POLICY_SCHEMA,
    vol.Optional("escalation"): dict,
    vol.Optional("allow_manual_completion", default=False): cv.boolean,
    **SOURCE_SCHEMA,
}


@websocket_command(CREATE_NATIVE_TRIGGERED_SCHEMA)
@async_response
@_api_errors
async def websocket_create_native_triggered(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Create a trigger-based reminder using native HA trigger configurations."""
    manager = _native_manager(hass)
    user_id = await async_resolve_target_user(hass, connection.user, msg.get("user_id"))
    reminder = await async_create_native_triggered(
        manager,
        user_id=user_id,
        title=msg["title"],
        message=msg.get("message"),
        activation_triggers=msg["activation_triggers"],
        delivery_policy=_policy_from_data(msg),
        acknowledgement_policy=AcknowledgementPolicy(msg["acknowledgement_policy"]),
        quiet_hours_policy=QuietHoursPolicy(msg["quiet_hours_policy"]),
        repeat_policy=TriggerRepeatPolicy(msg["repeat_policy"]),
        while_awaiting_acknowledgement=WhileAwaitingAcknowledgement(
            msg["while_awaiting_acknowledgement"]
        ),
        cooldown_seconds=msg["cooldown_seconds"],
        available_from=(
            _parse_datetime(hass, msg["available_from"])
            if msg.get("available_from")
            else None
        ),
        expires_at=(
            _parse_datetime(hass, msg["expires_at"])
            if msg.get("expires_at")
            else None
        ),
        trigger_description=msg.get("trigger_description"),
        completion_triggers=msg["completion_triggers"],
        escalation=msg.get("escalation"),
        allow_manual_completion=msg["allow_manual_completion"],
        **_source_from_data(msg),
    )
    connection.send_result(msg["id"], {"reminder": reminder.to_dict()})


UPDATE_NATIVE_TRIGGERED_SCHEMA: dict[Any, Any] = {
    vol.Required("type"): f"{COMMAND_PREFIX}update_native_triggered",
    vol.Required("reminder_id"): cv.string,
    vol.Required("activation_triggers"): vol.All(NATIVE_LIST, vol.Length(min=1)),
    vol.Optional("title"): cv.string,
    vol.Optional("message"): vol.Any(None, cv.string),
    vol.Optional("completion_triggers"): NATIVE_LIST,
    vol.Optional("user_id"): cv.string,
    vol.Optional("acknowledgement_policy"): vol.In(tuple(AcknowledgementPolicy)),
    vol.Optional("quiet_hours_policy"): vol.In(tuple(QuietHoursPolicy)),
    vol.Optional("repeat_policy"): vol.In(tuple(TriggerRepeatPolicy)),
    vol.Optional("while_awaiting_acknowledgement"): vol.In(
        tuple(WhileAwaitingAcknowledgement)
    ),
    vol.Optional("cooldown_seconds"): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=31_536_000)
    ),
    vol.Optional("available_from"): vol.Any(None, cv.string),
    vol.Optional("expires_at"): vol.Any(None, cv.string),
    vol.Optional("trigger_description"): vol.Any(None, cv.string),
    **{key: value for key, value in POLICY_SCHEMA.items()},
    vol.Optional("escalation"): vol.Any(None, dict),
    vol.Optional("allow_manual_completion"): cv.boolean,
}


@websocket_command(UPDATE_NATIVE_TRIGGERED_SCHEMA)
@async_response
@_api_errors
async def websocket_update_native_triggered(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Update a native trigger-based reminder."""
    manager = _native_manager(hass)
    reminder = await async_get_authorized(
        manager, connection.user, msg["reminder_id"]
    )
    kwargs: dict[str, Any] = {
        "activation_triggers": msg["activation_triggers"],
    }
    for key in (
        "title",
        "message",
        "cooldown_seconds",
        "trigger_description",
        "completion_triggers",
        "allow_manual_completion",
    ):
        if key in msg:
            kwargs[key] = msg[key]
    if "user_id" in msg:
        kwargs["user_id"] = await async_resolve_target_user(
            hass, connection.user, msg["user_id"]
        )
    if "acknowledgement_policy" in msg:
        kwargs["acknowledgement_policy"] = AcknowledgementPolicy(
            msg["acknowledgement_policy"]
        )
    if "quiet_hours_policy" in msg:
        kwargs["quiet_hours_policy"] = QuietHoursPolicy(msg["quiet_hours_policy"])
    if "repeat_policy" in msg:
        kwargs["repeat_policy"] = TriggerRepeatPolicy(msg["repeat_policy"])
    if "while_awaiting_acknowledgement" in msg:
        kwargs["while_awaiting_acknowledgement"] = WhileAwaitingAcknowledgement(
            msg["while_awaiting_acknowledgement"]
        )
    if "available_from" in msg:
        kwargs["available_from"] = (
            _parse_datetime(hass, msg["available_from"])
            if msg["available_from"]
            else None
        )
    if "expires_at" in msg:
        kwargs["expires_at"] = (
            _parse_datetime(hass, msg["expires_at"])
            if msg["expires_at"]
            else None
        )
    if "escalation" in msg:
        kwargs["escalation"] = msg["escalation"]
    if "delivery_mode" in msg or any(
        key in msg
        for key in (
            "channels",
            "notify_targets",
            "mobile_app_services",
            "voice_targets",
        )
    ):
        kwargs["delivery_policy"] = _policy_from_data(msg)
    updated = await async_update_native_triggered(
        manager, reminder.id, **kwargs
    )
    connection.send_result(msg["id"], {"reminder": updated.to_dict()})


NATIVE_WEBSOCKET_HANDLERS = (
    websocket_set_native_rules,
    websocket_create_native_triggered,
    websocket_update_native_triggered,
)
