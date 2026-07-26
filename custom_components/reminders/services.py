"""Home Assistant actions exposed by Reminders."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    SERVICE_CREATE,
    SERVICE_CREATE_RECURRING,
    SERVICE_DELETE,
    SERVICE_GET,
    SERVICE_LIST,
    SERVICE_SET_USER_PREFERENCES,
    SERVICE_SNOOZE,
    SERVICE_UPDATE,
    SUPPORTED_CHANNELS,
)
from .manager import ReminderManager, ReminderNotFoundError, ReminderValidationError
from .models import DeliveryPolicy, Reminder
from .recurrence import (
    RecurrenceError,
    RecurrenceFrequency,
    RecurrenceRule,
    Weekday,
)

POLICY_FIELDS: dict[Any, Any] = {
    vol.Optional("channels"): vol.All(cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]),
    vol.Optional("notify_targets"): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional("voice_targets"): vol.All(cv.ensure_list, [cv.entity_id]),
}

CREATE_FIELDS: dict[Any, Any] = {
    vol.Required("title"): cv.string,
    vol.Optional("message"): cv.string,
    vol.Required("due"): vol.Any(datetime, cv.string),
    vol.Optional("user_id"): cv.string,
    vol.Optional("delivery_mode", default="default"): vol.In(("default", "custom")),
}
CREATE_FIELDS.update(POLICY_FIELDS)
CREATE_SCHEMA = vol.Schema(CREATE_FIELDS)
CREATE_RECURRING_FIELDS: dict[Any, Any] = {
    vol.Required("title"): cv.string,
    vol.Optional("message"): cv.string,
    vol.Required("first_reminder"): vol.Any(datetime, cv.string),
    vol.Required("frequency"): vol.In(tuple(RecurrenceFrequency)),
    vol.Optional("interval", default=1): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("weekdays"): vol.All(
        cv.ensure_list, [vol.In(tuple(day.label for day in Weekday))]
    ),
    vol.Optional("day_of_month"): vol.All(vol.Coerce(int), vol.Range(min=1, max=31)),
    vol.Optional("timezone"): cv.string,
    vol.Optional("user_id"): cv.string,
    vol.Optional("delivery_mode", default="default"): vol.In(("default", "custom")),
}
CREATE_RECURRING_FIELDS.update(POLICY_FIELDS)
CREATE_RECURRING_SCHEMA = vol.Schema(CREATE_RECURRING_FIELDS)
ID_SCHEMA = vol.Schema({vol.Required("reminder_id"): cv.string})
UPDATE_FIELDS: dict[Any, Any] = {
    vol.Required("reminder_id"): cv.string,
    vol.Optional("title"): cv.string,
    vol.Optional("message"): vol.Any(None, cv.string),
    vol.Optional("due"): vol.Any(datetime, cv.string),
    vol.Optional("user_id"): cv.string,
    vol.Optional("delivery_mode"): vol.In(("default", "custom")),
    vol.Optional("first_reminder"): vol.Any(datetime, cv.string),
    vol.Optional("frequency"): vol.In(tuple(RecurrenceFrequency)),
    vol.Optional("interval"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("weekdays"): vol.All(
        cv.ensure_list, [vol.In(tuple(day.label for day in Weekday))]
    ),
    vol.Optional("day_of_month"): vol.All(vol.Coerce(int), vol.Range(min=1, max=31)),
    vol.Optional("timezone"): cv.string,
}
UPDATE_FIELDS.update(POLICY_FIELDS)
UPDATE_SCHEMA = vol.Schema(UPDATE_FIELDS)
LIST_SCHEMA = vol.Schema(
    {
        vol.Optional("user_id"): cv.string,
        vol.Optional("pending_only", default=False): cv.boolean,
        vol.Optional("due_after"): vol.Any(datetime, cv.string),
        vol.Optional("due_before"): vol.Any(datetime, cv.string),
    }
)
SNOOZE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("reminder_id"): cv.string,
            vol.Optional("due"): vol.Any(datetime, cv.string),
            vol.Optional("duration"): cv.time_period,
        }
    ),
    cv.has_at_least_one_key("due", "duration"),
)
PREFERENCES_SCHEMA = vol.Schema(
    {
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


def async_register_services(hass: HomeAssistant) -> None:
    """Register integration actions."""
    if hass.services.has_service(DOMAIN, SERVICE_CREATE):
        return

    async def create(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        user_id = await _resolve_user(hass, call, call.data.get("user_id"))
        policy = _policy_from_data(call.data)
        reminder = await manager.async_create(
            user_id=user_id,
            title=call.data["title"],
            message=call.data.get("message"),
            due=_parse_datetime(hass, call.data["due"]),
            delivery_policy=policy,
        )
        return {"reminder": reminder.to_dict()} if call.return_response else None

    async def get(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _get_authorized(hass, manager, call, call.data["reminder_id"])
        return {"reminder": reminder.to_dict()}

    async def create_recurring(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        user_id = await _resolve_user(hass, call, call.data.get("user_id"))
        policy = _policy_from_data(call.data)
        recurrence = _recurrence_from_data(hass, call.data)
        reminder = await manager.async_create_recurring(
            user_id=user_id,
            title=call.data["title"],
            message=call.data.get("message"),
            recurrence=recurrence,
            delivery_policy=policy,
        )
        return {"reminder": reminder.to_dict()} if call.return_response else None

    async def list_reminders(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        requested = call.data.get("user_id")
        user_id = await _resolve_list_user(hass, call, requested)
        reminders = await manager.async_list(
            user_id=user_id,
            pending_only=call.data["pending_only"],
            due_after=(
                _parse_datetime(hass, call.data["due_after"])
                if "due_after" in call.data
                else None
            ),
            due_before=(
                _parse_datetime(hass, call.data["due_before"])
                if "due_before" in call.data
                else None
            ),
        )
        return {"reminders": [item.to_dict() for item in reminders]}

    async def update(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _get_authorized(hass, manager, call, call.data["reminder_id"])
        changes: dict[str, Any] = {
            key: call.data[key] for key in ("title", "message") if key in call.data
        }
        if "due" in call.data:
            changes["due"] = _parse_datetime(hass, call.data["due"])
        if "user_id" in call.data:
            changes["user_id"] = await _resolve_user(hass, call, call.data["user_id"])
        if "delivery_mode" in call.data or any(
            key in call.data for key in POLICY_FIELDS
        ):
            changes["delivery_policy"] = _policy_from_data(call.data)
        recurrence_fields = {
            "first_reminder",
            "frequency",
            "interval",
            "weekdays",
            "day_of_month",
            "timezone",
        }
        if recurrence_fields.intersection(call.data):
            if reminder.recurrence is None:
                raise ReminderValidationError(
                    "Recurrence fields can only update a recurring reminder"
                )
            changes["recurrence"] = _recurrence_from_data(
                hass, call.data, reminder.recurrence
            )
        updated = await manager.async_update(reminder.id, **changes)
        return {"reminder": updated.to_dict()} if call.return_response else None

    async def delete(call: ServiceCall) -> None:
        manager = _manager(hass)
        reminder = await _get_authorized(hass, manager, call, call.data["reminder_id"])
        await manager.async_delete(reminder.id)

    async def snooze(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _get_authorized(hass, manager, call, call.data["reminder_id"])
        if "due" in call.data and "duration" in call.data:
            raise ServiceValidationError("Provide due or duration, not both")
        updated = await manager.async_snooze(
            reminder.id,
            due=(
                _parse_datetime(hass, call.data["due"]) if "due" in call.data else None
            ),
            duration=call.data.get("duration"),
        )
        return {"reminder": updated.to_dict()} if call.return_response else None

    async def set_preferences(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        user_id = await _resolve_user(hass, call, call.data.get("user_id"))
        policy = DeliveryPolicy(
            channels=tuple(call.data["channels"]),
            notify_targets=tuple(call.data["notify_targets"]),
            voice_targets=tuple(call.data["voice_targets"]),
        )
        preferences = await manager.async_set_user_preferences(user_id, policy)
        if not call.return_response:
            return None
        return {"preferences": preferences.to_dict(), "user_id": user_id}

    handlers = (
        (SERVICE_CREATE, create, CREATE_SCHEMA, SupportsResponse.OPTIONAL),
        (
            SERVICE_CREATE_RECURRING,
            create_recurring,
            CREATE_RECURRING_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (SERVICE_GET, get, ID_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_LIST, list_reminders, LIST_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_UPDATE, update, UPDATE_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_DELETE, delete, ID_SCHEMA, SupportsResponse.NONE),
        (SERVICE_SNOOZE, snooze, SNOOZE_SCHEMA, SupportsResponse.OPTIONAL),
        (
            SERVICE_SET_USER_PREFERENCES,
            set_preferences,
            PREFERENCES_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
    )
    for name, handler, schema, response in handlers:
        hass.services.async_register(
            DOMAIN, name, _translate_errors(handler), schema, supports_response=response
        )


def _manager(hass: HomeAssistant) -> ReminderManager:
    """Return the loaded manager or a useful action error."""
    manager = hass.data.get(DOMAIN)
    if not isinstance(manager, ReminderManager):
        raise HomeAssistantError("The Reminders integration is not loaded")
    return manager


def _translate_errors(handler: Any) -> Any:
    async def wrapped(call: ServiceCall) -> Any:
        try:
            return await handler(call)
        except ReminderNotFoundError as err:
            raise ServiceValidationError(f"Unknown reminder ID: {err.args[0]}") from err
        except ReminderValidationError as err:
            raise ServiceValidationError(str(err)) from err
        except RecurrenceError as err:
            raise ServiceValidationError(str(err)) from err

    return wrapped


async def _get_authorized(
    hass: HomeAssistant,
    manager: ReminderManager,
    call: ServiceCall,
    reminder_id: str,
) -> Reminder:
    reminder = await manager.async_get(reminder_id)
    if call.context.user_id is None or call.context.user_id == reminder.user_id:
        return reminder
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None or not user.is_admin:
        raise HomeAssistantError("You cannot access another user's reminder")
    return reminder


async def _resolve_user(
    hass: HomeAssistant, call: ServiceCall, requested: str | None
) -> str:
    context_user_id = call.context.user_id
    if context_user_id is None:
        if requested is None:
            raise ServiceValidationError(
                "user_id is required when the action has no authenticated user context"
            )
        return requested
    if requested is None or requested == context_user_id:
        return context_user_id
    user = await hass.auth.async_get_user(context_user_id)
    if user is None or not user.is_admin:
        raise HomeAssistantError("Only administrators may select another user")
    return requested


async def _resolve_list_user(
    hass: HomeAssistant, call: ServiceCall, requested: str | None
) -> str | None:
    context_user_id = call.context.user_id
    if context_user_id is None:
        if requested is None:
            raise ServiceValidationError(
                "user_id is required when the action has no authenticated user context"
            )
        return requested
    if requested is None or requested == context_user_id:
        user = await hass.auth.async_get_user(context_user_id)
        return (
            None
            if user is not None and user.is_admin and requested is None
            else context_user_id
        )
    user = await hass.auth.async_get_user(context_user_id)
    if user is None or not user.is_admin:
        raise HomeAssistantError("You cannot list another user's reminders")
    return requested


def _policy_from_data(data: Any) -> DeliveryPolicy | None:
    mode = data.get("delivery_mode", "default")
    policy_present = any(key in data for key in POLICY_FIELDS)
    if mode == "default" and not policy_present:
        return None
    if mode == "default" and policy_present:
        raise ServiceValidationError(
            "Delivery targets/channels require delivery_mode custom"
        )
    channels = tuple(data.get("channels", ()))
    if not channels:
        raise ServiceValidationError("Custom delivery requires at least one channel")
    return DeliveryPolicy(
        channels=channels,
        notify_targets=tuple(data.get("notify_targets", ())),
        voice_targets=tuple(data.get("voice_targets", ())),
    )


def _parse_datetime(hass: HomeAssistant, value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else dt_util.parse_datetime(value)
    if parsed is None:
        raise ServiceValidationError("Datetime is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        timezone = dt_util.get_time_zone(hass.config.time_zone)
        if timezone is None:
            raise ServiceValidationError("Home Assistant timezone is invalid")
        parsed = parsed.replace(tzinfo=timezone)
    return dt_util.as_utc(parsed)


def _recurrence_from_data(
    hass: HomeAssistant,
    data: Any,
    current: RecurrenceRule | None = None,
) -> RecurrenceRule:
    """Build a complete recurrence rule from action fields."""
    timezone = str(
        data.get(
            "timezone",
            current.timezone if current is not None else hass.config.time_zone,
        )
    )
    if "first_reminder" in data:
        anchor_local = _parse_local_datetime(data["first_reminder"], timezone)
    elif current is not None:
        anchor_local = current.anchor_local
    else:
        raise RecurrenceError("First reminder is required")
    frequency = RecurrenceFrequency(
        data.get("frequency", current.frequency if current is not None else None)
    )
    interval = int(data.get("interval", current.interval if current else 1))

    supplied_weekdays = tuple(data.get("weekdays", ()))
    if frequency is not RecurrenceFrequency.WEEKLY and supplied_weekdays:
        raise RecurrenceError("Only weekly recurrence can define weekdays")
    if frequency is not RecurrenceFrequency.MONTHLY and "day_of_month" in data:
        raise RecurrenceError("Only monthly recurrence can define day of month")

    if frequency is RecurrenceFrequency.WEEKLY:
        if "weekdays" in data:
            weekdays = tuple(Weekday.from_name(value) for value in data["weekdays"])
        elif current is not None and current.frequency is frequency:
            weekdays = current.weekdays
        else:
            weekdays = (Weekday(anchor_local.weekday()),)
    else:
        weekdays = ()

    day_of_month: int | None
    if frequency is RecurrenceFrequency.MONTHLY:
        if "day_of_month" in data:
            day_of_month = int(data["day_of_month"])
        elif current is not None and current.frequency is frequency:
            day_of_month = current.day_of_month
        else:
            day_of_month = anchor_local.day
    else:
        day_of_month = None

    return RecurrenceRule(
        frequency=frequency,
        interval=interval,
        timezone=timezone,
        anchor_local=anchor_local,
        weekdays=weekdays,
        day_of_month=day_of_month,
    )


def _parse_local_datetime(value: datetime | str, timezone: str) -> datetime:
    """Parse a wall-clock anchor, converting aware input to the rule timezone."""
    parsed = value if isinstance(value, datetime) else dt_util.parse_datetime(value)
    if parsed is None:
        raise RecurrenceError("First reminder datetime is invalid")
    try:
        zone = ZoneInfo(timezone)
    except Exception as err:
        raise RecurrenceError(f"Unknown timezone: {timezone}") from err
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(zone).replace(tzinfo=None)
    return parsed.replace(tzinfo=None)
