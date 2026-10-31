"""Structured Reminders API for Home Assistant conversation/LLM agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from .authorization import (
    Actor,
    async_actor,
    async_get_authorized,
    async_resolve_list_user,
    async_resolve_target_user,
)
from .const import DOMAIN, SUPPORTED_CHANNELS
from .manager import ReminderManager, ReminderValidationError
from .models import (
    AcknowledgementPolicy,
    ActivationType,
    QuietHoursPolicy,
    Reminder,
    ReminderStatus,
    TriggerRepeatPolicy,
    WhileAwaitingAcknowledgement,
)
from .recurrence import RecurrenceFrequency
from .services import _parse_datetime, _policy_from_data, _recurrence_from_data
from .triggers.models import TriggerDefinition

ToolHandler = Callable[
    [HomeAssistant, dict[str, Any], llm.LLMContext], Awaitable[dict[str, Any]]
]

TARGET_SCHEMA: dict[Any, Any] = {
    vol.Optional("reminder_id"): cv.string,
    vol.Optional("title"): cv.string,
    vol.Optional("user_id"): cv.string,
}


class RemindersTool(llm.Tool):
    """Small declarative tool wrapper around shared manager operations."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: vol.Schema,
        handler: ToolHandler,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self._handler = handler

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> dict[str, Any]:
        """Validate identity and invoke one shared domain operation."""
        try:
            args = self.parameters(tool_input.tool_args)
            return await self._handler(hass, args, llm_context)
        except (vol.Invalid, ValueError, KeyError) as err:
            raise HomeAssistantError(str(err)) from err


class RemindersAPI(llm.API):
    """Expose reminders as an opt-in LLM API."""

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return tools bound to the request's authenticated context."""
        return llm.APIInstance(
            api=self,
            api_prompt=(
                "Use Reminders tools for reminder requests. Never guess which reminder "
                "to change when a tool returns needs_disambiguation. Times are "
                "interpreted "
                "in Home Assistant's configured timezone unless they include an offset."
            ),
            llm_context=llm_context,
            tools=_tools(),
        )


def async_register_conversation_api(hass: HomeAssistant) -> None:
    """Register the integration-owned opt-in conversation API."""
    llm.async_register_api(hass, RemindersAPI(hass=hass, id=DOMAIN, name="Reminders"))


def _tools() -> list[llm.Tool]:
    create_common: dict[Any, Any] = {
        vol.Required("title"): cv.string,
        vol.Optional("message"): cv.string,
        vol.Optional("user_id"): cv.string,
        vol.Optional("acknowledgement_policy", default="default"): vol.In(
            tuple(AcknowledgementPolicy)
        ),
        vol.Optional("quiet_hours_policy", default="respect"): vol.In(
            tuple(QuietHoursPolicy)
        ),
        vol.Optional("allow_manual_completion", default=False): cv.boolean,
    }
    scheduled_advanced: dict[Any, Any] = {
        vol.Optional("deliver_when"): dict,
        vol.Optional("complete_when"): dict,
        vol.Optional("escalation"): dict,
    }
    return [
        RemindersTool(
            "create_reminder",
            "Create one reminder. Provide due or delay_minutes; deliver_when waits "
            "for bounded context after that time, complete_when can resolve it, and "
            "escalation can redeliver until Done.",
            vol.Schema(
                vol.All(
                    vol.Schema(
                        {
                            **create_common,
                            **scheduled_advanced,
                            vol.Exclusive("due", "when"): cv.string,
                            vol.Exclusive("delay_minutes", "when"): vol.All(
                                vol.Coerce(int), vol.Range(min=1)
                            ),
                        }
                    ),
                    cv.has_at_least_one_key("due", "delay_minutes"),
                )
            ),
            _create,
        ),
        RemindersTool(
            "create_recurring_reminder",
            "Create an anchored daily, weekly, monthly, or yearly reminder series.",
            vol.Schema(
                {
                    **create_common,
                    **scheduled_advanced,
                    vol.Required("first_reminder"): cv.string,
                    vol.Required("frequency"): vol.In(tuple(RecurrenceFrequency)),
                    vol.Optional("interval", default=1): vol.All(
                        vol.Coerce(int), vol.Range(min=1)
                    ),
                    vol.Optional("weekdays"): vol.All(cv.ensure_list, [cv.string]),
                    vol.Optional("day_of_month"): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=31)
                    ),
                    vol.Optional("monthly_mode"): cv.string,
                    vol.Optional("monthly_weekday"): cv.string,
                    vol.Optional("monthly_week"): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=5)
                    ),
                    vol.Optional("end_date"): cv.string,
                    vol.Optional("occurrence_count"): vol.All(
                        vol.Coerce(int), vol.Range(min=1)
                    ),
                }
            ),
            _create_recurring,
        ),
        RemindersTool(
            "create_triggered_reminder",
            "Create a reminder activated by a supported state, numeric_state, zone, "
            "event, or named trigger. Use exact Home Assistant entity IDs.",
            vol.Schema(
                {
                    **create_common,
                    vol.Required("trigger"): dict,
                    vol.Optional("repeat_policy", default="once"): vol.In(
                        tuple(TriggerRepeatPolicy)
                    ),
                    vol.Optional("fire_if_already_matching", default=False): cv.boolean,
                    vol.Optional(
                        "while_awaiting_acknowledgement", default="skip"
                    ): vol.In(tuple(WhileAwaitingAcknowledgement)),
                    vol.Optional("cooldown_seconds", default=0): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=31_536_000)
                    ),
                    vol.Optional("available_from"): cv.string,
                    vol.Optional("expires_at"): cv.string,
                    vol.Optional("trigger_description"): cv.string,
                    vol.Optional("complete_when"): dict,
                    vol.Optional("escalation"): dict,
                }
            ),
            _create_triggered,
        ),
        RemindersTool(
            "list_reminders",
            "List upcoming reminders, optionally searching title/message or a "
            "due range.",
            vol.Schema(
                {
                    vol.Optional("user_id"): cv.string,
                    vol.Optional("query"): cv.string,
                    vol.Optional("due_after"): cv.string,
                    vol.Optional("due_before"): cv.string,
                    vol.Optional("recurring"): cv.boolean,
                    vol.Optional("activation_type"): vol.In(tuple(ActivationType)),
                    vol.Optional("limit", default=20): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=100)
                    ),
                    vol.Optional("offset", default=0): vol.All(
                        vol.Coerce(int), vol.Range(min=0)
                    ),
                }
            ),
            _list,
        ),
        RemindersTool(
            "get_reminder",
            "Get reminder details by ID or an unambiguous exact title.",
            vol.Schema(
                vol.All(
                    vol.Schema(TARGET_SCHEMA),
                    cv.has_at_least_one_key("reminder_id", "title"),
                )
            ),
            _get,
        ),
        RemindersTool(
            "update_reminder",
            "Update an unambiguous reminder's details, policies, or recurrence rule.",
            vol.Schema(
                vol.All(
                    vol.Schema(
                        {
                            **TARGET_SCHEMA,
                            vol.Optional("new_title"): cv.string,
                            vol.Optional("message"): vol.Any(None, cv.string),
                            vol.Optional("due"): cv.string,
                            vol.Optional("acknowledgement_policy"): vol.In(
                                tuple(AcknowledgementPolicy)
                            ),
                            vol.Optional("quiet_hours_policy"): vol.In(
                                tuple(QuietHoursPolicy)
                            ),
                            vol.Optional("activation_type"): vol.In(
                                tuple(ActivationType)
                            ),
                            vol.Optional("trigger"): dict,
                            vol.Optional("repeat_policy"): vol.In(
                                tuple(TriggerRepeatPolicy)
                            ),
                            vol.Optional("fire_if_already_matching"): cv.boolean,
                            vol.Optional("cooldown_seconds"): vol.All(
                                vol.Coerce(int), vol.Range(min=0, max=31_536_000)
                            ),
                            vol.Optional("available_from"): vol.Any(None, cv.string),
                            vol.Optional("expires_at"): vol.Any(None, cv.string),
                            vol.Optional("deliver_when"): vol.Any(None, dict),
                            vol.Optional("complete_when"): vol.Any(None, dict),
                            vol.Optional("escalation"): vol.Any(None, dict),
                            vol.Optional("allow_manual_completion"): cv.boolean,
                            vol.Optional("delivery_mode"): vol.In(
                                ("default", "custom")
                            ),
                            vol.Optional("channels"): vol.All(
                                cv.ensure_list, [vol.In(SUPPORTED_CHANNELS)]
                            ),
                            vol.Optional("notify_targets"): vol.All(
                                cv.ensure_list, [cv.entity_id]
                            ),
                            vol.Optional("mobile_app_services"): vol.All(
                                cv.ensure_list, [cv.string]
                            ),
                            vol.Optional("voice_targets"): vol.All(
                                cv.ensure_list, [cv.entity_id]
                            ),
                            vol.Optional("first_reminder"): cv.string,
                            vol.Optional("frequency"): vol.In(
                                tuple(RecurrenceFrequency)
                            ),
                            vol.Optional("interval"): vol.All(
                                vol.Coerce(int), vol.Range(min=1)
                            ),
                            vol.Optional("weekdays"): vol.All(
                                cv.ensure_list, [cv.string]
                            ),
                            vol.Optional("day_of_month"): vol.All(
                                vol.Coerce(int), vol.Range(min=1, max=31)
                            ),
                            vol.Optional("monthly_mode"): cv.string,
                            vol.Optional("monthly_weekday"): cv.string,
                            vol.Optional("monthly_week"): vol.All(
                                vol.Coerce(int), vol.Range(min=1, max=5)
                            ),
                            vol.Optional("end_date"): cv.string,
                            vol.Optional("occurrence_count"): vol.All(
                                vol.Coerce(int), vol.Range(min=1)
                            ),
                            vol.Optional("timezone"): cv.string,
                        }
                    ),
                    cv.has_at_least_one_key("reminder_id", "title"),
                )
            ),
            _update,
        ),
        RemindersTool(
            "delete_reminder",
            "Cancel/delete an unambiguous reminder or recurring series.",
            vol.Schema(
                vol.All(
                    vol.Schema(TARGET_SCHEMA),
                    cv.has_at_least_one_key("reminder_id", "title"),
                )
            ),
            _delete,
        ),
        RemindersTool(
            "pause_recurring_reminder",
            "Pause an anchored recurring reminder series until explicitly resumed.",
            vol.Schema(
                vol.All(
                    vol.Schema(TARGET_SCHEMA),
                    cv.has_at_least_one_key("reminder_id", "title"),
                )
            ),
            _pause_series,
        ),
        RemindersTool(
            "resume_recurring_reminder",
            "Resume a paused recurring series on its existing anchored schedule.",
            vol.Schema(
                vol.All(
                    vol.Schema(TARGET_SCHEMA),
                    cv.has_at_least_one_key("reminder_id", "title"),
                )
            ),
            _resume_series,
        ),
        RemindersTool(
            "skip_next_reminder",
            "Skip the next occurrence without cancelling the recurring series.",
            vol.Schema(
                vol.All(
                    vol.Schema(TARGET_SCHEMA),
                    cv.has_at_least_one_key("reminder_id", "title"),
                )
            ),
            _skip_next_series,
        ),
        RemindersTool(
            "snooze_reminder",
            "Snooze only the active occurrence by due time or minutes.",
            vol.Schema(
                vol.All(
                    vol.Schema(
                        {
                            **TARGET_SCHEMA,
                            vol.Exclusive("due", "when"): cv.string,
                            vol.Exclusive("minutes", "when"): vol.All(
                                vol.Coerce(int), vol.Range(min=1)
                            ),
                            vol.Exclusive("wait_for_next_trigger", "when"): cv.boolean,
                        }
                    ),
                    cv.has_at_least_one_key("reminder_id", "title"),
                    cv.has_at_least_one_key("due", "minutes", "wait_for_next_trigger"),
                )
            ),
            _snooze,
        ),
        RemindersTool(
            "acknowledge_reminder",
            "Dismiss one delivered occurrence; occurrence_id disambiguates "
            "series history.",
            vol.Schema(
                vol.All(
                    vol.Schema(
                        {**TARGET_SCHEMA, vol.Optional("occurrence_id"): cv.string}
                    ),
                    cv.has_at_least_one_key("reminder_id", "title"),
                )
            ),
            _acknowledge,
        ),
        RemindersTool(
            "complete_reminder",
            "Mark the underlying task completed when manual completion is enabled.",
            vol.Schema(
                vol.All(
                    vol.Schema(
                        {**TARGET_SCHEMA, vol.Optional("occurrence_id"): cv.string}
                    ),
                    cv.has_at_least_one_key("reminder_id", "title"),
                )
            ),
            _complete,
        ),
        RemindersTool(
            "fire_named_reminder_trigger",
            "Fire a named trigger for the authenticated user's reminders only.",
            vol.Schema({vol.Required("trigger_id"): cv.string}),
            _fire_named_trigger,
        ),
        RemindersTool(
            "query_reminder_history",
            "Query bounded delivered/failed/acknowledged reminder occurrence history.",
            vol.Schema(
                {
                    vol.Optional("user_id"): cv.string,
                    vol.Optional("query"): cv.string,
                    vol.Optional("due_after"): cv.string,
                    vol.Optional("due_before"): cv.string,
                    vol.Optional("offset", default=0): vol.All(
                        vol.Coerce(int), vol.Range(min=0)
                    ),
                    vol.Optional("limit", default=20): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=100)
                    ),
                }
            ),
            _history,
        ),
    ]


def _manager(hass: HomeAssistant) -> ReminderManager:
    manager = hass.data.get(DOMAIN)
    if not isinstance(manager, ReminderManager):
        raise HomeAssistantError("The Reminders integration is not loaded")
    return manager


def _compact_reminder(reminder: Reminder) -> dict[str, Any]:
    """Serialize list results without embedding bounded historical occurrences."""
    result = reminder.to_dict()
    result["occurrence_history"] = [
        item
        for item in result.get("occurrence_history", [])
        if item.get("status") == "awaiting_acknowledgement"
    ]
    return result


async def _identity(hass: HomeAssistant, context: llm.LLMContext) -> tuple[Actor, str]:
    user_id = context.context.user_id if context.context is not None else None
    actor = await async_actor(hass, user_id)
    if actor is None:
        raise HomeAssistantError(
            "Reminders conversation tools require an authenticated Home Assistant user"
        )
    return actor, actor.id


async def _create(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    actor, _ = await _identity(hass, context)
    user_id = await async_resolve_target_user(hass, actor, args.get("user_id"))
    due = (
        _parse_datetime(hass, args["due"])
        if "due" in args
        else dt_util.utcnow() + timedelta(minutes=args["delay_minutes"])
    )
    reminder = await _manager(hass).async_create(
        user_id=user_id,
        title=args["title"],
        message=args.get("message"),
        due=due,
        acknowledgement_policy=AcknowledgementPolicy(args["acknowledgement_policy"]),
        quiet_hours_policy=QuietHoursPolicy(args["quiet_hours_policy"]),
        deliver_when=args.get("deliver_when"),
        complete_when=args.get("complete_when"),
        escalation=args.get("escalation"),
        allow_manual_completion=args["allow_manual_completion"],
    )
    return {"reminder": reminder.to_dict()}


async def _create_recurring(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    actor, _ = await _identity(hass, context)
    user_id = await async_resolve_target_user(hass, actor, args.get("user_id"))
    reminder = await _manager(hass).async_create_recurring(
        user_id=user_id,
        title=args["title"],
        message=args.get("message"),
        recurrence=_recurrence_from_data(hass, args),
        acknowledgement_policy=AcknowledgementPolicy(args["acknowledgement_policy"]),
        quiet_hours_policy=QuietHoursPolicy(args["quiet_hours_policy"]),
        deliver_when=args.get("deliver_when"),
        complete_when=args.get("complete_when"),
        escalation=args.get("escalation"),
        allow_manual_completion=args["allow_manual_completion"],
    )
    return {"reminder": reminder.to_dict()}


async def _create_triggered(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    actor, _ = await _identity(hass, context)
    user_id = await async_resolve_target_user(hass, actor, args.get("user_id"))
    reminder = await _manager(hass).async_create_triggered(
        user_id=user_id,
        title=args["title"],
        message=args.get("message"),
        trigger=TriggerDefinition.from_dict(args["trigger"]),
        acknowledgement_policy=AcknowledgementPolicy(args["acknowledgement_policy"]),
        quiet_hours_policy=QuietHoursPolicy(args["quiet_hours_policy"]),
        repeat_policy=TriggerRepeatPolicy(args["repeat_policy"]),
        fire_if_already_matching=args["fire_if_already_matching"],
        while_awaiting_acknowledgement=WhileAwaitingAcknowledgement(
            args["while_awaiting_acknowledgement"]
        ),
        cooldown_seconds=args["cooldown_seconds"],
        available_from=(
            _parse_datetime(hass, args["available_from"])
            if "available_from" in args
            else None
        ),
        expires_at=(
            _parse_datetime(hass, args["expires_at"]) if "expires_at" in args else None
        ),
        trigger_description=args.get("trigger_description"),
        complete_when=args.get("complete_when"),
        escalation=args.get("escalation"),
        allow_manual_completion=args["allow_manual_completion"],
    )
    return {"reminder": reminder.to_dict()}


async def _list(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    actor, _ = await _identity(hass, context)
    user_id = await async_resolve_list_user(hass, actor, args.get("user_id"))
    statuses = None
    if "activation_type" not in args:
        # A reminder waiting for a trigger, availability window, or delivery
        # condition is active even though it is not yet scheduled for delivery.
        statuses = {
            ReminderStatus.PENDING,
            ReminderStatus.AWAITING_ACKNOWLEDGEMENT,
            ReminderStatus.WAITING_FOR_CONTEXT,
            ReminderStatus.WAITING_FOR_TRIGGER,
            ReminderStatus.INACTIVE_BEFORE_AVAILABLE_FROM,
        }
    reminders, total = await _manager(hass).async_list_page(
        user_id=user_id,
        statuses=statuses,
        query=args.get("query"),
        due_after=_parse_datetime(hass, args["due_after"])
        if "due_after" in args
        else None,
        due_before=_parse_datetime(hass, args["due_before"])
        if "due_before" in args
        else None,
        recurring=args.get("recurring"),
        activation_type=(
            ActivationType(args["activation_type"])
            if "activation_type" in args
            else None
        ),
        limit=args["limit"],
        offset=args.get("offset", 0),
    )
    return {
        "reminders": [_compact_reminder(item) for item in reminders],
        "total": total,
        "offset": args.get("offset", 0),
        "next_offset": args.get("offset", 0) + len(reminders),
        "has_more": args.get("offset", 0) + len(reminders) < total,
    }


async def _resolve_reminder(
    hass: HomeAssistant,
    args: dict[str, Any],
    context: llm.LLMContext,
) -> tuple[Reminder | None, dict[str, Any] | None, Actor]:
    actor, _ = await _identity(hass, context)
    manager = _manager(hass)
    if reminder_id := args.get("reminder_id"):
        return await async_get_authorized(manager, actor, reminder_id), None, actor
    user_id = await async_resolve_list_user(hass, actor, args.get("user_id"))
    # Check the full bounded search rather than its first 500 rows: an
    # exact duplicate beyond that page must never be silently overlooked.
    matches: list[Reminder] = []
    offset = 0
    page_size = 1000
    search_exhausted = False
    while offset < 20_000 and len(matches) < 2:
        page, total = await manager.async_list_page(
            user_id=user_id, query=args["title"], limit=page_size, offset=offset
        )
        matches.extend(
            item
            for item in page
            if item.title.casefold() == args["title"].strip().casefold()
        )
        offset += len(page)
        if offset >= total or not page:
            search_exhausted = True
            break
    if len(matches) == 1 and search_exhausted:
        return matches[0], None, actor
    return (
        None,
        {
            "needs_disambiguation": True,
            "message": (
                "Too many possible matches to identify a reminder safely"
                if not search_exhausted
                else "No exact match"
                if not matches
                else "Several reminders match"
            ),
            "candidates": [
                {
                    "id": item.id,
                    "title": item.title,
                    "due": item.due.isoformat() if item.due else None,
                    "trigger_summary": item.trigger_summary,
                    "recurring": item.recurrence is not None,
                }
                for item in matches[:10]
            ],
        },
        actor,
    )


async def _get(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, _ = await _resolve_reminder(hass, args, context)
    return response or {"reminder": reminder.to_dict()}  # type: ignore[union-attr]


async def _update(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, _ = await _resolve_reminder(hass, args, context)
    if response is not None:
        return response
    assert reminder is not None
    changes: dict[str, Any] = {}
    if "new_title" in args:
        changes["title"] = args["new_title"]
    if "message" in args:
        changes["message"] = args["message"]
    if "due" in args:
        changes["due"] = _parse_datetime(hass, args["due"])
    if "acknowledgement_policy" in args:
        changes["acknowledgement_policy"] = AcknowledgementPolicy(
            args["acknowledgement_policy"]
        )
    if "quiet_hours_policy" in args:
        changes["quiet_hours_policy"] = QuietHoursPolicy(args["quiet_hours_policy"])
    if "activation_type" in args:
        changes["activation_type"] = ActivationType(args["activation_type"])
    if "trigger" in args:
        changes["trigger"] = TriggerDefinition.from_dict(args["trigger"])
    if "repeat_policy" in args:
        changes["repeat_policy"] = TriggerRepeatPolicy(args["repeat_policy"])
    for key in (
        "fire_if_already_matching",
        "cooldown_seconds",
        "deliver_when",
        "complete_when",
        "escalation",
        "allow_manual_completion",
    ):
        if key in args:
            changes[key] = args[key]
    for key in ("available_from", "expires_at"):
        if key in args:
            changes[key] = (
                _parse_datetime(hass, args[key]) if args[key] is not None else None
            )
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
    if recurrence_fields.intersection(args):
        if reminder.recurrence is None:
            raise ReminderValidationError(
                "Recurrence fields can only update a recurring reminder"
            )
        changes["recurrence"] = _recurrence_from_data(hass, args, reminder.recurrence)
    delivery_fields = {
        "channels",
        "notify_targets",
        "mobile_app_services",
        "voice_targets",
    }
    if "delivery_mode" in args or delivery_fields.intersection(args):
        changes["delivery_policy"] = _policy_from_data(args)
    if not changes:
        raise ReminderValidationError("No reminder changes were provided")
    updated = await _manager(hass).async_update(
        reminder.id, expected_user_id=reminder.user_id, **changes
    )
    return {"reminder": updated.to_dict()}


async def _delete(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, _ = await _resolve_reminder(hass, args, context)
    if response is not None:
        return response
    assert reminder is not None
    await _manager(hass).async_delete(reminder.id, expected_user_id=reminder.user_id)
    return {"deleted": True, "reminder_id": reminder.id}


async def _pause_series(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, _ = await _resolve_reminder(hass, args, context)
    if response is not None:
        return response
    assert reminder is not None
    updated = await _manager(hass).async_pause(
        reminder.id, expected_user_id=reminder.user_id
    )
    return {"reminder": updated.to_dict()}


async def _resume_series(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, _ = await _resolve_reminder(hass, args, context)
    if response is not None:
        return response
    assert reminder is not None
    updated = await _manager(hass).async_resume(
        reminder.id, expected_user_id=reminder.user_id
    )
    return {"reminder": updated.to_dict()}


async def _skip_next_series(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, _ = await _resolve_reminder(hass, args, context)
    if response is not None:
        return response
    assert reminder is not None
    updated = await _manager(hass).async_skip_next(
        reminder.id, expected_user_id=reminder.user_id
    )
    return {"reminder": updated.to_dict()}


async def _snooze(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, _ = await _resolve_reminder(hass, args, context)
    if response is not None:
        return response
    assert reminder is not None
    if args.get("wait_for_next_trigger"):
        updated = await _manager(hass).async_wait_for_next_trigger(
            reminder.id, expected_user_id=reminder.user_id
        )
    else:
        updated = await _manager(hass).async_snooze(
            reminder.id,
            expected_user_id=reminder.user_id,
            due=_parse_datetime(hass, args["due"]) if "due" in args else None,
            duration=timedelta(minutes=args["minutes"]) if "minutes" in args else None,
        )
    return {"reminder": updated.to_dict()}


async def _acknowledge(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, actor = await _resolve_reminder(hass, args, context)
    if response is not None:
        return response
    assert reminder is not None
    occurrence = await _manager(hass).async_acknowledge(
        reminder.id,
        expected_user_id=reminder.user_id,
        occurrence_id=args.get("occurrence_id"),
        acknowledged_by=actor.id,
    )
    return {"occurrence": occurrence.to_dict()}


async def _complete(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    reminder, response, actor = await _resolve_reminder(hass, args, context)
    if response is not None:
        return response
    assert reminder is not None
    occurrence = await _manager(hass).async_complete(
        reminder.id,
        expected_user_id=reminder.user_id,
        occurrence_id=args.get("occurrence_id"),
        completed_by=actor.id,
    )
    return {"occurrence": occurrence.to_dict()}


async def _fire_named_trigger(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    actor, user_id = await _identity(hass, context)
    del actor
    return await _manager(hass).async_fire_named_trigger(
        args["trigger_id"], user_id=user_id
    )


async def _history(
    hass: HomeAssistant, args: dict[str, Any], context: llm.LLMContext
) -> dict[str, Any]:
    actor, _ = await _identity(hass, context)
    user_id = await async_resolve_list_user(hass, actor, args.get("user_id"))
    rows, total = await _manager(hass).async_history(
        user_id=user_id,
        query=args.get("query"),
        due_after=_parse_datetime(hass, args["due_after"])
        if "due_after" in args
        else None,
        due_before=_parse_datetime(hass, args["due_before"])
        if "due_before" in args
        else None,
        limit=args["limit"],
        offset=args.get("offset", 0),
    )
    next_offset = args.get("offset", 0) + len(rows)
    return {
        "history": rows,
        "total": total,
        "next_offset": next_offset,
        "has_more": next_offset < total,
    }
