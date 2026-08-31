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

_TRIGGER_RETRY_DELAYS = (5.0, 30.0, 120.0, 300.0)


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


@dataclass(slots=True)
class _TriggerFailure:
    """Retry state for a native trigger subscription that failed to attach."""

    config_key: tuple[str, ...]
    attempts: int


class NativeAutomationRuntime:
    """Attach and evaluate automation-native trigger/condition configurations."""

    def __init__(self, hass: HomeAssistant, callback: NativeTriggerCallback) -> None:
        self._hass = hass
        self._callback = callback
        self._trigger_entries: dict[tuple[str, str], _TriggerEntry] = {}
        self._condition_entries: dict[str, _ConditionEntry] = {}
        self._trigger_failures: dict[tuple[str, str], _TriggerFailure] = {}
        self._trigger_retry_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._sync_lock = asyncio.Lock()
        self._unloaded = False

    @property
    def listener_count(self) -> int:
        """Return the number of role-level native trigger subscriptions."""
        return len(self._trigger_entries)

    @property
    def failed_listener_count(self) -> int:
        """Return native trigger subscriptions waiting for a retry."""
        return len(self._trigger_failures)

    @property
    def failed_listener_roles(self) -> dict[str, int]:
        """Return privacy-safe failed native trigger counts by lifecycle role."""
        return {
            role: sum(reference[1] == role for reference in self._trigger_failures)
            for role in ("activation", "delivery", "completion")
        }

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

            desired_references = set(desired)
            for reference in (
                set(self._trigger_entries) | set(self._trigger_failures)
            ) - desired_references:
                self._remove_trigger(reference)
                self._clear_trigger_failure(reference)
            for reminder_id in set(self._condition_entries) - set(conditions):
                self._remove_conditions(reminder_id)

            for reference, configs in desired.items():
                failure = self._trigger_failures.get(reference)
                if failure is not None and failure.config_key != _config_key(configs):
                    self._clear_trigger_failure(reference)
                await self._async_ensure_trigger_entry(reference, configs)
            for reminder_id, configs in conditions.items():
                await self._async_ensure_condition_entry(reminder_id, configs)

    async def async_conditions_match(self, reminder: Reminder) -> bool:
        """Evaluate native conditions, preferring an extra reminder to a lost one."""
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
            # A valid stored reminder must not be permanently skipped because HA
            # could not compile its condition at runtime (for example while a
            # dependency is temporarily unavailable after restart). Reminders are
            # safety-biased: fail open and log rather than silently lose delivery.
            _LOGGER.error(
                "Native delivery conditions are unavailable for reminder %s; "
                "delivering rather than silently skipping the reminder",
                reminder.id,
            )
            return True
        try:
            return bool(entry.checker.async_check(variables={}))
        except Exception:
            _LOGGER.exception(
                "Error evaluating native delivery conditions for reminder %s; "
                "delivering rather than silently skipping the reminder",
                reminder.id,
            )
            return True

    async def async_unload(self) -> None:
        """Detach native trigger listeners, retries, and condition resources."""
        retry_tasks: tuple[asyncio.Task[None], ...]
        async with self._sync_lock:
            self._unloaded = True
            for reference in tuple(self._trigger_entries):
                self._remove_trigger(reference)
            retry_tasks = tuple(self._trigger_retry_tasks.values())
            for task in retry_tasks:
                task.cancel()
            self._trigger_retry_tasks.clear()
            self._trigger_failures.clear()
            for reminder_id in tuple(self._condition_entries):
                self._remove_conditions(reminder_id)
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)

    async def _async_ensure_trigger_entry(
        self,
        reference: tuple[str, str],
        configs: tuple[dict[str, Any], ...],
    ) -> None:
        key = _config_key(configs)
        current = self._trigger_entries.get(reference)
        if current is not None and current.config_key == key:
            self._clear_trigger_failure(reference)
            return
        failure = self._trigger_failures.get(reference)
        retry_task = self._trigger_retry_tasks.get(reference)
        if (
            failure is not None
            and failure.config_key == key
            and retry_task is not None
            and not retry_task.done()
        ):
            return
        if failure is not None and failure.config_key != key:
            self._clear_trigger_failure(reference)
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
            if unsubscribe is None:
                raise RuntimeError("Home Assistant returned no trigger subscription")
        except Exception:
            failure = self._record_trigger_failure(reference, key)
            self._schedule_trigger_retry(reference, configs, key, failure.attempts)
            _LOGGER.exception(
                "Unable to attach native %s triggers for reminder %s; "
                "retry %s scheduled",
                reference[1],
                reference[0],
                failure.attempts,
            )
            return
        self._clear_trigger_failure(reference)
        self._trigger_entries[reference] = _TriggerEntry(key, unsubscribe)

    def _record_trigger_failure(
        self, reference: tuple[str, str], config_key: tuple[str, ...]
    ) -> _TriggerFailure:
        current = self._trigger_failures.get(reference)
        attempts = (
            current.attempts + 1
            if current is not None and current.config_key == config_key
            else 1
        )
        failure = _TriggerFailure(config_key=config_key, attempts=attempts)
        self._trigger_failures[reference] = failure
        return failure

    def _schedule_trigger_retry(
        self,
        reference: tuple[str, str],
        configs: tuple[dict[str, Any], ...],
        config_key: tuple[str, ...],
        attempts: int,
    ) -> None:
        current = self._trigger_retry_tasks.get(reference)
        if current is not None and not current.done():
            return
        delay = _TRIGGER_RETRY_DELAYS[min(attempts - 1, len(_TRIGGER_RETRY_DELAYS) - 1)]
        retry_configs = tuple(dict(item) for item in configs)
        self._trigger_retry_tasks[reference] = asyncio.create_task(
            self._async_retry_trigger(reference, retry_configs, config_key, delay),
            name=f"reminders native trigger retry {reference[1]}",
        )

    async def _async_retry_trigger(
        self,
        reference: tuple[str, str],
        configs: tuple[dict[str, Any], ...],
        config_key: tuple[str, ...],
        delay: float,
    ) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(delay)
            async with self._sync_lock:
                if self._unloaded:
                    return
                failure = self._trigger_failures.get(reference)
                if failure is None or failure.config_key != config_key:
                    return
                if self._trigger_retry_tasks.get(reference) is current_task:
                    self._trigger_retry_tasks.pop(reference, None)
                await self._async_ensure_trigger_entry(reference, configs)
        except asyncio.CancelledError:
            raise
        finally:
            if self._trigger_retry_tasks.get(reference) is current_task:
                self._trigger_retry_tasks.pop(reference, None)

    def _clear_trigger_failure(self, reference: tuple[str, str]) -> None:
        self._trigger_failures.pop(reference, None)
        task = self._trigger_retry_tasks.pop(reference, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

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
