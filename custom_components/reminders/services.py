"""Home Assistant actions exposed by Reminders."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast
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

from .authorization import (
    async_actor,
    async_resolve_target_user,
)
from .authorization import (
    async_get_authorized as auth_get_authorized,
)
from .authorization import (
    async_resolve_list_user as auth_resolve_list_user,
)
from .const import (
    DOMAIN,
    SERVICE_ACKNOWLEDGE,
    SERVICE_COMPLETE,
    SERVICE_CREATE,
    SERVICE_CREATE_RECURRING,
    SERVICE_CREATE_TRIGGERED,
    SERVICE_DELETE,
    SERVICE_EXTERNAL_ACTION,
    SERVICE_FIRE_TRIGGER,
    SERVICE_GET,
    SERVICE_LIST,
    SERVICE_SET_USER_PREFERENCES,
    SERVICE_SNOOZE,
    SERVICE_TEST_DELIVERY,
    SERVICE_UPDATE,
    SUPPORTED_CHANNELS,
)
from .manager import ReminderManager, ReminderNotFoundError, ReminderValidationError
from .models import (
    AcknowledgementPolicy,
    ActivationType,
    DeliveryPolicy,
    MissedOccurrencePolicy,
    QuietHoursPolicy,
    Reminder,
    TriggerRepeatPolicy,
    WhileAwaitingAcknowledgement,
)
from .recurrence import (
    MonthlyMode,
    RecurrenceError,
    RecurrenceFrequency,
    RecurrenceRule,
    Weekday,
)
from .triggers.models import TriggerDefinition, TriggerValidationError

POLICY_FIELDS: dict[Any, Any] = {
    vol.Optional("channels"): vol.All(cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]),
    vol.Optional("notify_targets"): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional("mobile_app_services"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("voice_targets"): vol.All(cv.ensure_list, [cv.entity_id]),
}
ADVANCED_FIELDS: dict[Any, Any] = {
    vol.Optional("deliver_when"): dict,
    vol.Optional("complete_when"): dict,
    vol.Optional("escalation"): dict,
    vol.Optional("allow_manual_completion", default=False): cv.boolean,
    vol.Optional("expires_after_seconds"): vol.All(
        vol.Coerce(int), vol.Range(min=60, max=31_536_000)
    ),
}
EXTERNAL_ACTION_SCHEMA = vol.All(
    cv.ensure_list,
    vol.Length(max=5),
    [
        {
            vol.Required("id"): vol.All(cv.string, vol.Match(r"^[A-Za-z0-9_-]{1,64}$")),
            vol.Required("label"): vol.All(cv.string, vol.Length(min=1, max=64)),
        }
    ],
)
SOURCE_FIELDS: dict[Any, Any] = {
    vol.Optional("source"): vol.All(cv.string, vol.Length(min=1, max=128)),
    vol.Optional("source_id"): vol.All(cv.string, vol.Length(min=1, max=255)),
    vol.Optional("source_event"): vol.All(cv.string, vol.Length(min=1, max=128)),
    vol.Optional("managed_externally"): cv.boolean,
    vol.Optional("external_actions"): EXTERNAL_ACTION_SCHEMA,
}
SOURCE_UPDATE_FIELDS: dict[Any, Any] = {
    vol.Optional("source"): vol.Any(None, cv.string),
    vol.Optional("source_id"): vol.Any(None, cv.string),
    vol.Optional("source_event"): vol.Any(None, cv.string),
    vol.Optional("managed_externally"): cv.boolean,
    vol.Optional("external_actions"): EXTERNAL_ACTION_SCHEMA,
}

CREATE_FIELDS: dict[Any, Any] = {
    vol.Required("title"): cv.string,
    vol.Optional("message"): cv.string,
    vol.Required("due"): vol.Any(datetime, cv.string),
    vol.Optional("user_id"): cv.string,
    vol.Optional("delivery_mode", default="default"): vol.In(("default", "custom")),
    vol.Optional("acknowledgement_policy", default="default"): vol.In(
        tuple(AcknowledgementPolicy)
    ),
    vol.Optional("quiet_hours_policy", default="respect"): vol.In(
        tuple(QuietHoursPolicy)
    ),
}
CREATE_FIELDS.update(POLICY_FIELDS)
CREATE_FIELDS.update(ADVANCED_FIELDS)
CREATE_FIELDS.update(SOURCE_FIELDS)
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
    vol.Optional("monthly_mode"): vol.In(tuple(MonthlyMode)),
    vol.Optional("monthly_weekday"): vol.In(tuple(day.label for day in Weekday)),
    vol.Optional("monthly_week"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
    vol.Optional("end_date"): cv.date,
    vol.Optional("occurrence_count"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("timezone"): cv.string,
    vol.Optional("user_id"): cv.string,
    vol.Optional("delivery_mode", default="default"): vol.In(("default", "custom")),
    vol.Optional("acknowledgement_policy", default="default"): vol.In(
        tuple(AcknowledgementPolicy)
    ),
    vol.Optional("quiet_hours_policy", default="respect"): vol.In(
        tuple(QuietHoursPolicy)
    ),
    vol.Optional("missed_occurrence_policy", default="remind_on_startup"): vol.In(
        tuple(MissedOccurrencePolicy)
    ),
}
CREATE_RECURRING_FIELDS.update(POLICY_FIELDS)
CREATE_RECURRING_FIELDS.update(ADVANCED_FIELDS)
CREATE_RECURRING_FIELDS.update(SOURCE_FIELDS)
CREATE_RECURRING_SCHEMA = vol.Schema(CREATE_RECURRING_FIELDS)
CREATE_TRIGGERED_FIELDS: dict[Any, Any] = {
    vol.Required("title"): cv.string,
    vol.Optional("message"): cv.string,
    vol.Required("trigger"): dict,
    vol.Optional("user_id"): cv.string,
    vol.Optional("delivery_mode", default="default"): vol.In(("default", "custom")),
    vol.Optional("acknowledgement_policy", default="default"): vol.In(
        tuple(AcknowledgementPolicy)
    ),
    vol.Optional("quiet_hours_policy", default="respect"): vol.In(
        tuple(QuietHoursPolicy)
    ),
    vol.Optional("repeat_policy", default="once"): vol.In(tuple(TriggerRepeatPolicy)),
    vol.Optional("fire_if_already_matching", default=False): cv.boolean,
    vol.Optional("while_awaiting_acknowledgement", default="skip"): vol.In(
        tuple(WhileAwaitingAcknowledgement)
    ),
    vol.Optional("cooldown_seconds", default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=31_536_000)
    ),
    vol.Optional("available_from"): vol.Any(datetime, cv.string),
    vol.Optional("expires_at"): vol.Any(datetime, cv.string),
    vol.Optional("trigger_description"): cv.string,
    vol.Optional("complete_when"): dict,
    vol.Optional("escalation"): dict,
    vol.Optional("allow_manual_completion", default=False): cv.boolean,
}
CREATE_TRIGGERED_FIELDS.update(POLICY_FIELDS)
CREATE_TRIGGERED_FIELDS.update(SOURCE_FIELDS)
CREATE_TRIGGERED_SCHEMA = vol.Schema(CREATE_TRIGGERED_FIELDS)
FIRE_TRIGGER_SCHEMA = vol.Schema(
    {
        vol.Required("trigger_id"): cv.string,
        vol.Optional("user_id"): cv.string,
    }
)
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
    vol.Optional("monthly_mode"): vol.In(tuple(MonthlyMode)),
    vol.Optional("monthly_weekday"): vol.In(tuple(day.label for day in Weekday)),
    vol.Optional("monthly_week"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
    vol.Optional("end_date"): vol.Any(None, cv.date),
    vol.Optional("occurrence_count"): vol.Any(
        None, vol.All(vol.Coerce(int), vol.Range(min=1))
    ),
    vol.Optional("acknowledgement_policy"): vol.In(tuple(AcknowledgementPolicy)),
    vol.Optional("quiet_hours_policy"): vol.In(tuple(QuietHoursPolicy)),
    vol.Optional("activation_type"): vol.In(tuple(ActivationType)),
    vol.Optional("trigger"): dict,
    vol.Optional("trigger_description"): vol.Any(None, cv.string),
    vol.Optional("repeat_policy"): vol.In(tuple(TriggerRepeatPolicy)),
    vol.Optional("fire_if_already_matching"): cv.boolean,
    vol.Optional("while_awaiting_acknowledgement"): vol.In(
        tuple(WhileAwaitingAcknowledgement)
    ),
    vol.Optional("cooldown_seconds"): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=31_536_000)
    ),
    vol.Optional("available_from"): vol.Any(None, datetime, cv.string),
    vol.Optional("expires_at"): vol.Any(None, datetime, cv.string),
    vol.Optional("deliver_when"): vol.Any(None, dict),
    vol.Optional("complete_when"): vol.Any(None, dict),
    vol.Optional("escalation"): vol.Any(None, dict),
    vol.Optional("allow_manual_completion"): cv.boolean,
    vol.Optional("missed_occurrence_policy"): vol.In(tuple(MissedOccurrencePolicy)),
    vol.Optional("expires_after_seconds"): vol.Any(
        None, vol.All(vol.Coerce(int), vol.Range(min=60, max=31_536_000))
    ),
}
UPDATE_FIELDS.update(SOURCE_UPDATE_FIELDS)
UPDATE_FIELDS.update(POLICY_FIELDS)
UPDATE_SCHEMA = vol.Schema(UPDATE_FIELDS)
LIST_SCHEMA = vol.Schema(
    {
        vol.Optional("user_id"): cv.string,
        vol.Optional("pending_only", default=False): cv.boolean,
        vol.Optional("due_after"): vol.Any(datetime, cv.string),
        vol.Optional("due_before"): vol.Any(datetime, cv.string),
        vol.Optional("query"): cv.string,
        vol.Optional("activation_type"): vol.In(tuple(ActivationType)),
        vol.Optional("source"): vol.All(cv.string, vol.Length(min=1, max=128)),
        vol.Optional("source_id"): vol.All(cv.string, vol.Length(min=1, max=255)),
    }
)
SNOOZE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("reminder_id"): cv.string,
            vol.Optional("due"): vol.Any(datetime, cv.string),
            vol.Optional("duration"): cv.time_period,
            vol.Optional("wait_for_next_trigger"): cv.boolean,
        }
    ),
    cv.has_at_least_one_key("due", "duration", "wait_for_next_trigger"),
)
PREFERENCES_SCHEMA = vol.Schema(
    {
        vol.Optional("user_id"): cv.string,
        vol.Required("channels"): vol.All(cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]),
        vol.Optional("notify_targets", default=[]): vol.All(
            cv.ensure_list, [cv.entity_id]
        ),
        vol.Optional("mobile_app_services", default=[]): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional("voice_targets", default=[]): vol.All(
            cv.ensure_list, [cv.entity_id]
        ),
        vol.Optional("require_acknowledgement", default=False): cv.boolean,
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
ACKNOWLEDGE_SCHEMA = vol.Schema(
    {
        vol.Required("reminder_id"): cv.string,
        vol.Optional("occurrence_id"): cv.string,
    }
)
COMPLETE_SCHEMA = ACKNOWLEDGE_SCHEMA
EXTERNAL_ACTION_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("reminder_id"): cv.string,
        vol.Required("occurrence_id"): cv.string,
        vol.Required("external_action_id"): cv.string,
    }
)
TEST_DELIVERY_FIELDS: dict[Any, Any] = {
    vol.Optional("user_id"): cv.string,
    vol.Required("channels"): vol.All(cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]),
    vol.Optional("notify_targets", default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional("mobile_app_services", default=[]): vol.All(
        cv.ensure_list, [cv.string]
    ),
    vol.Optional("voice_targets", default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
}
TEST_DELIVERY_SCHEMA = vol.Schema(TEST_DELIVERY_FIELDS)


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
            acknowledgement_policy=AcknowledgementPolicy(
                call.data["acknowledgement_policy"]
            ),
            quiet_hours_policy=QuietHoursPolicy(call.data["quiet_hours_policy"]),
            deliver_when=call.data.get("deliver_when"),
            complete_when=call.data.get("complete_when"),
            escalation=call.data.get("escalation"),
            allow_manual_completion=call.data["allow_manual_completion"],
            expires_after_seconds=call.data.get("expires_after_seconds"),
            **_source_from_data(call.data),
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
            acknowledgement_policy=AcknowledgementPolicy(
                call.data["acknowledgement_policy"]
            ),
            quiet_hours_policy=QuietHoursPolicy(call.data["quiet_hours_policy"]),
            deliver_when=call.data.get("deliver_when"),
            complete_when=call.data.get("complete_when"),
            escalation=call.data.get("escalation"),
            allow_manual_completion=call.data["allow_manual_completion"],
            missed_occurrence_policy=MissedOccurrencePolicy(
                call.data["missed_occurrence_policy"]
            ),
            expires_after_seconds=call.data.get("expires_after_seconds"),
            **_source_from_data(call.data),
        )
        return {"reminder": reminder.to_dict()} if call.return_response else None

    async def create_triggered(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        user_id = await _resolve_user(hass, call, call.data.get("user_id"))
        reminder = await manager.async_create_triggered(
            user_id=user_id,
            title=call.data["title"],
            message=call.data.get("message"),
            trigger=TriggerDefinition.from_dict(call.data["trigger"]),
            delivery_policy=_policy_from_data(call.data),
            acknowledgement_policy=AcknowledgementPolicy(
                call.data["acknowledgement_policy"]
            ),
            quiet_hours_policy=QuietHoursPolicy(call.data["quiet_hours_policy"]),
            repeat_policy=TriggerRepeatPolicy(call.data["repeat_policy"]),
            fire_if_already_matching=call.data["fire_if_already_matching"],
            while_awaiting_acknowledgement=WhileAwaitingAcknowledgement(
                call.data["while_awaiting_acknowledgement"]
            ),
            cooldown_seconds=call.data["cooldown_seconds"],
            available_from=(
                _parse_datetime(hass, call.data["available_from"])
                if "available_from" in call.data
                else None
            ),
            expires_at=(
                _parse_datetime(hass, call.data["expires_at"])
                if "expires_at" in call.data
                else None
            ),
            trigger_description=call.data.get("trigger_description"),
            complete_when=call.data.get("complete_when"),
            escalation=call.data.get("escalation"),
            allow_manual_completion=call.data["allow_manual_completion"],
            **_source_from_data(call.data),
        )
        return {"reminder": reminder.to_dict()} if call.return_response else None

    async def fire_trigger(call: ServiceCall) -> ServiceResponse:
        user_id = await _resolve_user(hass, call, call.data.get("user_id"))
        return cast(
            ServiceResponse,
            await _manager(hass).async_fire_named_trigger(
                call.data["trigger_id"], user_id=user_id
            ),
        )

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
            query=call.data.get("query"),
            activation_type=(
                ActivationType(call.data["activation_type"])
                if "activation_type" in call.data
                else None
            ),
            source=call.data.get("source"),
            source_id=call.data.get("source_id"),
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
        if "acknowledgement_policy" in call.data:
            changes["acknowledgement_policy"] = AcknowledgementPolicy(
                call.data["acknowledgement_policy"]
            )
        if "quiet_hours_policy" in call.data:
            changes["quiet_hours_policy"] = QuietHoursPolicy(
                call.data["quiet_hours_policy"]
            )
        if "activation_type" in call.data:
            changes["activation_type"] = ActivationType(call.data["activation_type"])
        if "trigger" in call.data:
            changes["trigger"] = TriggerDefinition.from_dict(call.data["trigger"])
        for key in (
            "trigger_description",
            "fire_if_already_matching",
            "cooldown_seconds",
            "deliver_when",
            "complete_when",
            "escalation",
            "source",
            "source_id",
            "source_event",
            "managed_externally",
            "allow_manual_completion",
            "external_actions",
            "missed_occurrence_policy",
            "expires_after_seconds",
        ):
            if key in call.data:
                changes[key] = call.data[key]
        if "repeat_policy" in call.data:
            changes["repeat_policy"] = TriggerRepeatPolicy(call.data["repeat_policy"])
        if "while_awaiting_acknowledgement" in call.data:
            changes["while_awaiting_acknowledgement"] = WhileAwaitingAcknowledgement(
                call.data["while_awaiting_acknowledgement"]
            )
        for key in ("available_from", "expires_at"):
            if key in call.data:
                changes[key] = (
                    _parse_datetime(hass, call.data[key])
                    if call.data[key] is not None
                    else None
                )
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
            "monthly_mode",
            "monthly_weekday",
            "monthly_week",
            "end_date",
            "occurrence_count",
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
        if call.data.get("wait_for_next_trigger"):
            if "due" in call.data or "duration" in call.data:
                raise ServiceValidationError(
                    "Wait for next trigger cannot be combined with due or duration"
                )
            updated = await manager.async_wait_for_next_trigger(reminder.id)
            return {"reminder": updated.to_dict()} if call.return_response else None
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
            mobile_app_services=tuple(call.data["mobile_app_services"]),
            voice_targets=tuple(call.data["voice_targets"]),
        )
        preferences = await manager.async_set_user_preferences(
            user_id,
            policy,
            **{
                key: call.data[key]
                for key in (
                    "require_acknowledgement",
                    "history_retention_days",
                    "history_max_occurrences",
                    "quiet_hours_enabled",
                    "quiet_hours_start",
                    "quiet_hours_end",
                    "quiet_hours_channels",
                    "quiet_hours_fallback_channels",
                )
                if key in call.data
            },
        )
        if not call.return_response:
            return None
        return {"preferences": preferences.to_dict(), "user_id": user_id}

    async def acknowledge(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _get_authorized(hass, manager, call, call.data["reminder_id"])
        occurrence = await manager.async_acknowledge(
            reminder.id,
            occurrence_id=call.data.get("occurrence_id"),
            acknowledged_by=call.context.user_id,
        )
        return {"occurrence": occurrence.to_dict()} if call.return_response else None

    async def complete(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _get_authorized(hass, manager, call, call.data["reminder_id"])
        occurrence = await manager.async_complete(
            reminder.id,
            occurrence_id=call.data.get("occurrence_id"),
            completed_by=call.context.user_id,
        )
        return {"occurrence": occurrence.to_dict()} if call.return_response else None

    async def external_action(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        reminder = await _get_authorized(hass, manager, call, call.data["reminder_id"])
        occurrence = await manager.async_select_external_action(
            reminder.id,
            call.data["external_action_id"],
            occurrence_id=call.data["occurrence_id"],
            selected_by=call.context.user_id,
        )
        return {"occurrence": occurrence.to_dict()} if call.return_response else None

    async def test_delivery(call: ServiceCall) -> ServiceResponse:
        manager = _manager(hass)
        user_id = await _resolve_user(hass, call, call.data.get("user_id"))
        policy = DeliveryPolicy(
            channels=tuple(call.data["channels"]),
            notify_targets=tuple(call.data.get("notify_targets", ())),
            mobile_app_services=tuple(call.data.get("mobile_app_services", ())),
            voice_targets=tuple(call.data.get("voice_targets", ())),
        )
        result = await manager.async_test_delivery(user_id=user_id, policy=policy)
        return {
            "succeeded_channels": list(result.succeeded),
            "failed_channels": list(result.failed_channels),
            "errors": list(result.errors),
        }

    handlers = (
        (SERVICE_CREATE, create, CREATE_SCHEMA, SupportsResponse.OPTIONAL),
        (
            SERVICE_CREATE_TRIGGERED,
            create_triggered,
            CREATE_TRIGGERED_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (
            SERVICE_FIRE_TRIGGER,
            fire_trigger,
            FIRE_TRIGGER_SCHEMA,
            SupportsResponse.ONLY,
        ),
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
            SERVICE_ACKNOWLEDGE,
            acknowledge,
            ACKNOWLEDGE_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (SERVICE_COMPLETE, complete, COMPLETE_SCHEMA, SupportsResponse.OPTIONAL),
        (
            SERVICE_EXTERNAL_ACTION,
            external_action,
            EXTERNAL_ACTION_SERVICE_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (
            SERVICE_TEST_DELIVERY,
            test_delivery,
            TEST_DELIVERY_SCHEMA,
            SupportsResponse.ONLY,
        ),
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
        except TriggerValidationError as err:
            raise ServiceValidationError(str(err)) from err

    return wrapped


async def _get_authorized(
    hass: HomeAssistant,
    manager: ReminderManager,
    call: ServiceCall,
    reminder_id: str,
) -> Reminder:
    return await auth_get_authorized(
        manager, await async_actor(hass, call.context.user_id), reminder_id
    )


async def _resolve_user(
    hass: HomeAssistant, call: ServiceCall, requested: str | None
) -> str:
    return await async_resolve_target_user(
        hass, await async_actor(hass, call.context.user_id), requested
    )


async def _resolve_list_user(
    hass: HomeAssistant, call: ServiceCall, requested: str | None
) -> str | None:
    actor = await async_actor(hass, call.context.user_id)
    return await auth_resolve_list_user(
        hass,
        actor,
        requested,
        all_users=actor is not None and actor.is_admin and requested is None,
    )


def _source_from_data(data: Any) -> dict[str, Any]:
    """Extract optional external-source metadata from a public request."""
    return {
        key: data[key]
        for key in (
            "source",
            "source_id",
            "source_event",
            "managed_externally",
            "external_actions",
        )
        if key in data
    }


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
        mobile_app_services=tuple(data.get("mobile_app_services", ())),
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
    monthly_fields = {
        "day_of_month",
        "monthly_mode",
        "monthly_weekday",
        "monthly_week",
    }
    if frequency is not RecurrenceFrequency.MONTHLY and monthly_fields.intersection(
        data
    ):
        raise RecurrenceError("Only monthly recurrence can define monthly patterns")

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
    monthly_mode = MonthlyMode.DAY_OF_MONTH
    monthly_weekday: Weekday | None = None
    monthly_week: int | None = None
    if frequency is RecurrenceFrequency.MONTHLY:
        monthly_mode = MonthlyMode(
            data.get(
                "monthly_mode",
                current.monthly_mode
                if current is not None and current.frequency is frequency
                else MonthlyMode.DAY_OF_MONTH,
            )
        )
        if monthly_mode is MonthlyMode.DAY_OF_MONTH and "day_of_month" in data:
            day_of_month = int(data["day_of_month"])
        elif (
            monthly_mode is MonthlyMode.DAY_OF_MONTH
            and current is not None
            and current.frequency is frequency
        ):
            day_of_month = current.day_of_month
        elif monthly_mode is MonthlyMode.DAY_OF_MONTH:
            day_of_month = anchor_local.day
        else:
            day_of_month = None
        if monthly_mode in {MonthlyMode.NTH_WEEKDAY, MonthlyMode.LAST_WEEKDAY}:
            if "monthly_weekday" in data:
                monthly_weekday = Weekday.from_name(data["monthly_weekday"])
            elif current is not None and current.frequency is frequency:
                monthly_weekday = current.monthly_weekday
            else:
                monthly_weekday = Weekday(anchor_local.weekday())
        if monthly_mode is MonthlyMode.NTH_WEEKDAY:
            monthly_week = int(
                data.get(
                    "monthly_week",
                    current.monthly_week
                    if current is not None and current.frequency is frequency
                    else (anchor_local.day - 1) // 7 + 1,
                )
            )
    else:
        day_of_month = None

    end_value = data.get("end_date", current.end_date if current is not None else None)
    end_date = (
        end_value
        if isinstance(end_value, date)
        else date.fromisoformat(str(end_value))
        if end_value
        else None
    )
    count_value = data.get(
        "occurrence_count", current.occurrence_count if current is not None else None
    )

    return RecurrenceRule(
        frequency=frequency,
        interval=interval,
        timezone=timezone,
        anchor_local=anchor_local,
        weekdays=weekdays,
        day_of_month=day_of_month,
        monthly_mode=monthly_mode,
        monthly_weekday=monthly_weekday,
        monthly_week=monthly_week,
        end_date=end_date,
        occurrence_count=int(count_value) if count_value is not None else None,
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
