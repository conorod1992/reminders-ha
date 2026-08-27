"""Home Assistant-native trigger and condition runtime for Reminders."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant
from homeassistant.helpers import condition as condition_helper
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import trigger as trigger_helper
from homeassistant.helpers.typing import ConfigType

from .models import (
    ActivationType,
    Reminder,
    ReminderStatus,
    TriggerRepeatPolicy,
)

_LOGGER = logging.getLogger(__name__)

NativeTriggerCallback = Callable[
    [str, str, dict[str, Any], Context | None], Awaitable[None]
]


@dataclass(slots=True)
class _TriggerEntry:
    """One attached HA trigger-list subscription."""

    config_key: tuple[str, ...]
    unsubscribe: CALLBACK_TYPE


@dataclass(slots=True)
class _ConditionEntry:
    """One compiled HA condition-list checker."""

    config_key: tuple[str, ...]
    checker: Any


class NativeAutomationRuntime:
    """Attach and evaluate automation-native trigger/condition configurations."""

    def __init__(self, hass: HomeAssistant, callback: NativeTriggerCallback) -> None:
        self._hass = hass
        self._callback = callback
        self._trigger_entries: dict[tuple[str, str], _TriggerEntry] = {}
        self._condition_entries: dict[str, _ConditionEntry] = {}
        self._sync_lock = asyncio.Lock()
        self._unloaded = False

    @property
    def listener_count(self) -> int:
        """Return the number of role-level native trigger subscriptions."""
        return len(self._trigger_entries)

    async def async_sync(self, reminders: Iterable[Reminder]) -> None:
        """Reconcile native trigger listeners and compiled condition checkers."""
        if self._unloaded:
            return
        async with self._sync_lock:
            if self._unloaded:
                return
            reminder_list = list(reminders)
            desired: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
            conditions: dict[str, tuple[dict[str, Any], ...]] = {}
            for reminder in reminder_list:
                if _native_activation_armed(reminder):
                    desired[(reminder.id, "activation")] = reminder.activation_triggers
                if (
                    reminder.delivery_triggers
                    and reminder.status is ReminderStatus.WAITING_FOR_CONTEXT
                ):
                    desired[(reminder.id, "delivery")] = reminder.delivery_triggers
                if reminder.completion_triggers and _native_completion_armed(reminder):
                    desired[(reminder.id, "completion")] = reminder.completion_triggers
                if reminder.delivery_conditions:
                    conditions[reminder.id] = reminder.delivery_conditions

            for reference in set(self._trigger_entries) - set(desired):
                self._remove_trigger(reference)
            for reminder_id in set(self._condition_entries) - set(conditions):
                self._remove_conditions(reminder_id)

            for reference, configs in desired.items():
                await self._async_ensure_trigger_entry(reference, configs)
            for reminder_id, configs in conditions.items():
                await self._async_ensure_condition_entry(reminder_id, configs)

    async def async_conditions_match(self, reminder: Reminder) -> bool:
        """Evaluate a reminder's native delivery conditions using HA semantics."""
        if not reminder.delivery_conditions:
            return True
        key = _config_key(reminder.delivery_conditions)
        entry = self._condition_entries.get(reminder.id)
        if entry is None or entry.config_key != key:
            async with self._sync_lock:
                if self._unloaded:
                    return False
                await self._async_ensure_condition_entry(
                    reminder.id, reminder.delivery_conditions
                )
                entry = self._condition_entries.get(reminder.id)
        if entry is None:
            return False
        try:
            return bool(entry.checker.async_check(variables={}))
        except Exception:
            _LOGGER.exception(
                "Error evaluating native delivery conditions for reminder %s",
                reminder.id,
            )
            return False

    async def async_unload(self) -> None:
        """Detach native trigger listeners and condition resources."""
        async with self._sync_lock:
            self._unloaded = True
            for reference in tuple(self._trigger_entries):
                self._remove_trigger(reference)
            for reminder_id in tuple(self._condition_entries):
                self._remove_conditions(reminder_id)

    async def _async_ensure_trigger_entry(
        self,
        reference: tuple[str, str],
        configs: tuple[dict[str, Any], ...],
    ) -> None:
        key = _config_key(configs)
        current = self._trigger_entries.get(reference)
        if current is not None and current.config_key == key:
            return
        self._remove_trigger(reference)
        try:
            validated = await async_validate_native_triggers(self._hass, configs)
            unsubscribe = await trigger_helper.async_initialize_triggers(
                self._hass,
                validated,
                self._action(reference),
                "reminders",
                f"reminder {reference[0]} {reference[1]}",
                self._log_callback,
            )
        except Exception:
            _LOGGER.exception(
                "Unable to attach native %s triggers for reminder %s",
                reference[1],
                reference[0],
            )
            return
        if unsubscribe is not None:
            self._trigger_entries[reference] = _TriggerEntry(key, unsubscribe)

    async def _async_ensure_condition_entry(
        self,
        reminder_id: str,
        configs: tuple[dict[str, Any], ...],
    ) -> None:
        key = _config_key(configs)
        current = self._condition_entries.get(reminder_id)
        if current is not None and current.config_key == key:
            return
        self._remove_conditions(reminder_id)
        try:
            validated = await async_validate_native_conditions(self._hass, configs)
            checker = await condition_helper.async_conditions_from_config(
                self._hass,
                validated,
                _LOGGER,
                f"reminder {reminder_id} delivery conditions",
            )
        except Exception:
            _LOGGER.exception(
                "Unable to prepare native delivery conditions for reminder %s",
                reminder_id,
            )
            return
        self._condition_entries[reminder_id] = _ConditionEntry(key, checker)

    def _action(
        self, reference: tuple[str, str]
    ) -> Callable[[dict[str, Any], Context | None], Awaitable[None]]:
        reminder_id, role = reference

        async def action(
            run_variables: dict[str, Any], context: Context | None = None
        ) -> None:
            if self._unloaded:
                return
            await self._callback(reminder_id, role, run_variables, context)

        return action

    def _remove_trigger(self, reference: tuple[str, str]) -> None:
        entry = self._trigger_entries.pop(reference, None)
        if entry is not None:
            entry.unsubscribe()

    def _remove_conditions(self, reminder_id: str) -> None:
        entry = self._condition_entries.pop(reminder_id, None)
        if entry is not None:
            entry.checker.async_unload()

    @staticmethod
    def _log_callback(level: int, message: str, **kwargs: Any) -> None:
        _LOGGER.log(level, "%s", message, **kwargs)


async def async_validate_native_triggers(
    hass: HomeAssistant, configs: Iterable[dict[str, Any]]
) -> list[ConfigType]:
    """Validate frontend-native trigger dictionaries through HA's own pipeline."""
    raw = [dict(item) for item in configs]
    base = cv.TRIGGER_SCHEMA(raw)
    return await trigger_helper.async_validate_trigger_config(hass, base)


async def async_validate_native_conditions(
    hass: HomeAssistant, configs: Iterable[dict[str, Any]]
) -> list[ConfigType]:
    """Validate frontend-native condition dictionaries through HA's own pipeline."""
    raw = [dict(item) for item in configs]
    base = cv.CONDITIONS_SCHEMA(raw)
    validated = await condition_helper.async_validate_conditions_config(hass, base)
    result: list[ConfigType] = []
    for item in validated:
        if not isinstance(item, dict):
            raise ValueError(
                "Reminder conditions must use structured Home Assistant rules"
            )
        result.append(dict(item))
    return result


def _native_activation_armed(reminder: Reminder) -> bool:
    return (
        reminder.activation_type is ActivationType.TRIGGER
        and bool(reminder.activation_triggers)
        and not (
            reminder.repeat_policy is TriggerRepeatPolicy.ONCE
            and reminder.last_triggered_at is not None
        )
        and reminder.status
        not in {
            ReminderStatus.EXPIRED,
            ReminderStatus.COMPLETED,
            ReminderStatus.CANCELLED,
            ReminderStatus.DELIVERED,
            ReminderStatus.ACKNOWLEDGED,
            ReminderStatus.FAILED,
            ReminderStatus.DELIVERING,
        }
    )


def _native_completion_armed(reminder: Reminder) -> bool:
    return reminder.status in {
        ReminderStatus.PENDING,
        ReminderStatus.WAITING_FOR_CONTEXT,
        ReminderStatus.DELIVERING,
        ReminderStatus.AWAITING_ACKNOWLEDGEMENT,
    }


def _config_key(configs: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Create a stable comparison key without interpreting HA configuration."""
    return tuple(
        json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
        for item in configs
    )
