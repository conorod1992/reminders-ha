"""Public services intended for automations and peer integrations."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .authorization import async_actor, async_get_authorized, async_resolve_target_user
from .const import (
    DOMAIN,
    SERVICE_CREATE,
    SERVICE_CREATE_RECURRING,
    SERVICE_CREATE_TRIGGERED,
    SERVICE_PAUSE,
    SERVICE_RECONCILE_SOURCE,
    SERVICE_RESUME,
    SERVICE_SET_NATIVE_RULES,
    SERVICE_SKIP_NEXT,
    SERVICE_UPDATE,
    SERVICE_UPSERT,
)
from .interop_manager import InteropReminderManager
from .manager import ReminderNotFoundError, ReminderValidationError
from .models import ActivationType, Reminder
from .recurrence import RecurrenceError
from .services import CREATE_RECURRING_SCHEMA, CREATE_SCHEMA, CREATE_TRIGGERED_SCHEMA
from .triggers.models import TriggerValidationError

_EXTERNAL_KINDS = ("one_time", "recurring", "triggered")
_RESERVED_UPSERT_FIELDS = {
    "managed_externally",
    "reminder_id",
    "source",
    "source_id",
    "user_id",
}
_NATIVE_LIST = vol.All(cv.ensure_list, [dict])

ID_SCHEMA = vol.Schema({vol.Required("reminder_id"): cv.string})
SET_NATIVE_RULES_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("reminder_id"): cv.string,
            vol.Optional("activation_triggers"): _NATIVE_LIST,
            vol.Optional("delivery_triggers"): _NATIVE_LIST,
            vol.Optional("delivery_conditions"): _NATIVE_LIST,
            vol.Optional("completion_triggers"): _NATIVE_LIST,
        }
    ),
    cv.has_at_least_one_key(
        "activation_triggers",
        "delivery_triggers",
        "delivery_conditions",
        "completion_triggers",
    ),
)
UPSERT_SCHEMA = vol.Schema(
    {
        vol.Required("source"): vol.All(cv.string, vol.Length(min=1, max=128)),
        vol.Required("source_id"): vol.All(cv.string, vol.Length(min=1, max=255)),
        vol.Optional("user_id"): cv.string,
        vol.Required("kind"): vol.In(_EXTERNAL_KINDS),
        vol.Required("data"): dict,
    }
)
RECONCILE_SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required("source"): vol.All(cv.string, vol.Length(min=1, max=128)),
        vol.Optional("user_id"): cv.string,
        vol.Optional("keep_source_ids", default=[]): vol.All(
            cv.ensure_list,
            vol.Length(max=1000),
            [vol.All(cv.string, vol.Length(min=1, max=255))],
        ),
    }
)


def async_register_interop_services(hass: HomeAssistant) -> None:
    """Register stable integration-facing reminder actions."""
    if hass.services.has_service(DOMAIN, SERVICE_UPSERT):
        return

    async def pause(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _authorized(hass, manager, call, call.data["reminder_id"])
        updated = await manager.async_pause(
            reminder.id, expected_user_id=reminder.user_id
        )
        return {"reminder": updated.to_dict()} if call.return_response else None

    async def resume(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _authorized(hass, manager, call, call.data["reminder_id"])
        updated = await manager.async_resume(
            reminder.id, expected_user_id=reminder.user_id
        )
        return {"reminder": updated.to_dict()} if call.return_response else None

    async def skip_next(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _authorized(hass, manager, call, call.data["reminder_id"])
        updated = await manager.async_skip_next(
            reminder.id, expected_user_id=reminder.user_id
        )
        return {"reminder": updated.to_dict()} if call.return_response else None

    async def set_native_rules(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _authorized(hass, manager, call, call.data["reminder_id"])
        updated = await manager.async_set_native_rules(
            reminder.id,
            expected_user_id=reminder.user_id,
            **{
                key: call.data[key]
                for key in (
                    "activation_triggers",
                    "delivery_triggers",
                    "delivery_conditions",
                    "completion_triggers",
                )
                if key in call.data
            },
        )
        return {"reminder": updated.to_dict()} if call.return_response else None

    async def upsert(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        actor = await async_actor(hass, call.context.user_id)
        user_id = await async_resolve_target_user(hass, actor, call.data.get("user_id"))
        supplied = dict(call.data["data"])
        reserved = _RESERVED_UPSERT_FIELDS.intersection(supplied)
        if reserved:
            raise ServiceValidationError(
                "Put ownership and external key fields at the top level, not in data: "
                f"{sorted(reserved)}"
            )
        source = call.data["source"]
        source_id = call.data["source_id"]
        kind = call.data["kind"]
        service_name, create_schema = _create_service(kind)
        desired = cast(
            dict[str, Any],
            create_schema(
                {
                    **supplied,
                    "user_id": user_id,
                    "source": source,
                    "source_id": source_id,
                    "managed_externally": True,
                }
            ),
        )

        async def create() -> dict[str, Any]:
            return await _call_service(
                hass,
                call,
                service_name,
                desired,
            )

        async def update(existing: Reminder) -> dict[str, Any]:
            _validate_existing_kind(existing, kind)
            return await _call_service(
                hass,
                call,
                SERVICE_UPDATE,
                {**desired, "reminder_id": existing.id},
            )

        created, result = await manager.async_upsert_external(
            user_id=user_id,
            source=source,
            source_id=source_id,
            create=create,
            update=update,
        )
        return {"created": created, **result}

    async def reconcile_source(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        actor = await async_actor(hass, call.context.user_id)
        user_id = await async_resolve_target_user(hass, actor, call.data.get("user_id"))
        deleted, skipped = await manager.async_reconcile_external_source(
            user_id=user_id,
            source=call.data["source"],
            keep_source_ids=call.data["keep_source_ids"],
        )
        return {
            "deleted_reminder_ids": list(deleted),
            "skipped_delivering_reminder_ids": list(skipped),
        }

    handlers = (
        (SERVICE_PAUSE, pause, ID_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_RESUME, resume, ID_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_SKIP_NEXT, skip_next, ID_SCHEMA, SupportsResponse.OPTIONAL),
        (
            SERVICE_SET_NATIVE_RULES,
            set_native_rules,
            SET_NATIVE_RULES_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (SERVICE_UPSERT, upsert, UPSERT_SCHEMA, SupportsResponse.ONLY),
        (
            SERVICE_RECONCILE_SOURCE,
            reconcile_source,
            RECONCILE_SOURCE_SCHEMA,
            SupportsResponse.ONLY,
        ),
    )
    for name, handler, schema, response in handlers:
        hass.services.async_register(
            DOMAIN,
            name,
            _translate_errors(handler),
            schema,
            supports_response=response,
        )


def _manager(hass: HomeAssistant) -> InteropReminderManager:
    manager = hass.data.get(DOMAIN)
    if not isinstance(manager, InteropReminderManager):
        raise HomeAssistantError("The Reminders integration is not loaded")
    return manager


async def _authorized(
    hass: HomeAssistant,
    manager: InteropReminderManager,
    call: ServiceCall,
    reminder_id: str,
) -> Reminder:
    return await async_get_authorized(
        manager, await async_actor(hass, call.context.user_id), reminder_id
    )


def _create_service(kind: str) -> tuple[str, vol.Schema]:
    if kind == "one_time":
        return SERVICE_CREATE, CREATE_SCHEMA
    if kind == "recurring":
        return SERVICE_CREATE_RECURRING, CREATE_RECURRING_SCHEMA
    return SERVICE_CREATE_TRIGGERED, CREATE_TRIGGERED_SCHEMA


def _validate_existing_kind(reminder: Reminder, kind: str) -> None:
    if kind == "triggered":
        matches = reminder.activation_type is ActivationType.TRIGGER
    elif kind == "recurring":
        matches = (
            reminder.activation_type is ActivationType.TIME
            and reminder.recurrence is not None
        )
    else:
        matches = (
            reminder.activation_type is ActivationType.TIME
            and reminder.recurrence is None
        )
    if not matches:
        raise ReminderValidationError(
            "Existing external reminder has a different kind; delete or reconcile it "
            "before changing activation type"
        )


async def _call_service(
    hass: HomeAssistant,
    call: ServiceCall,
    service: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    result = await hass.services.async_call(
        DOMAIN,
        service,
        data,
        blocking=True,
        context=call.context,
        return_response=True,
    )
    if not isinstance(result, dict):
        raise HomeAssistantError(f"{DOMAIN}.{service} did not return a response")
    return cast(dict[str, Any], result)


def _translate_errors(handler: Any) -> Any:
    async def wrapped(call: ServiceCall) -> Any:
        try:
            return await handler(call)
        except ReminderNotFoundError as err:
            raise ServiceValidationError(f"Unknown reminder ID: {err.args[0]}") from err
        except (
            ReminderValidationError,
            RecurrenceError,
            TriggerValidationError,
            vol.Invalid,
        ) as err:
            raise ServiceValidationError(str(err)) from err

    return wrapped
