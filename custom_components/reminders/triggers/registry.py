"""Central, deduplicated Home Assistant trigger listener registry."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.zone import in_zone
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from ..models import (
    ActivationType,
    Reminder,
    ReminderStatus,
    TriggerRepeatPolicy,
)
from .models import (
    TriggerDefinition,
    TriggerType,
    ZoneEvent,
    canonical_trigger_key,
    event_data_matches,
)

_LOGGER = logging.getLogger(__name__)

TriggerCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class _Entry:
    definition: TriggerDefinition
    reminder_ids: set[str] = field(default_factory=set)
    unsubscribe: Callable[[], None] | None = None
    cancel_timer: Callable[[], None] | None = None
    timer_value: Any = None


class TriggerRegistry:
    """Group equivalent triggers and attach one listener per canonical definition."""

    def __init__(self, hass: HomeAssistant, callback: TriggerCallback) -> None:
        self._hass = hass
        self._callback = callback
        self._entries: dict[str, _Entry] = {}
        self._named: dict[str, set[str]] = {}
        self._unloaded = False

    @property
    def listener_count(self) -> int:
        """Return registered HA listeners (named triggers need no listener)."""
        return sum(entry.unsubscribe is not None for entry in self._entries.values())

    def named_ids(self, trigger_id: str) -> frozenset[str]:
        """Return reminder IDs indexed for a canonical named trigger ID."""
        return frozenset(
            value for value in self._named.get(trigger_id, ()) if "::" not in value
        )

    def named_references(self, trigger_id: str) -> frozenset[str]:
        """Return activation and contextual subscriptions for a named trigger."""
        return frozenset(self._named.get(trigger_id, ()))

    async def async_sync(self, reminders: Iterable[Reminder]) -> None:
        """Atomically reconcile runtime listeners with persisted reminders."""
        desired: dict[str, tuple[TriggerDefinition, set[str]]] = {}
        for reminder in reminders:
            for reference, definition in _subscriptions(reminder):
                key = canonical_trigger_key(definition)
                _definition, ids = desired.setdefault(key, (definition, set()))
                ids.add(reference)
        for key in set(self._entries) - set(desired):
            self._remove(key)
        for key, (definition, ids) in desired.items():
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(definition=definition, reminder_ids=set(ids))
                self._entries[key] = entry
                self._attach(key, entry)
            else:
                entry.reminder_ids = set(ids)
        self._named = {}
        for entry in self._entries.values():
            if entry.definition.type is TriggerType.NAMED:
                self._named.setdefault(entry.definition.trigger_id or "", set()).update(
                    entry.reminder_ids
                )

    async def async_unload(self) -> None:
        """Remove every listener and pending duration timer."""
        self._unloaded = True
        for key in tuple(self._entries):
            self._remove(key)
        self._named.clear()

    def condition_is_currently_matching(self, trigger: TriggerDefinition) -> bool:
        """Evaluate durable state/numeric/zone conditions without firing."""
        if trigger.type is TriggerType.STATE:
            state = self._hass.states.get(trigger.entity_id or "")
            if state is None:
                return False
            value = _observed(state, trigger.attribute)
            if trigger.to_value is not None:
                return bool(value == trigger.to_value)
            if trigger.from_value is not None:
                return bool(value != trigger.from_value)
            return False
        if trigger.type is TriggerType.NUMERIC_STATE:
            state = self._hass.states.get(trigger.entity_id or "")
            return _numeric_matches(_number(state, trigger.attribute), trigger)
        if trigger.type is TriggerType.ZONE:
            state = self._hass.states.get(trigger.entity_id or "")
            zone_state = self._hass.states.get(trigger.zone_entity_id or "")
            if state is None or zone_state is None:
                return False
            inside = self._zone_match(trigger, state)
            return inside if trigger.event is ZoneEvent.ENTER else not inside
        return False

    def _attach(self, key: str, entry: _Entry) -> None:
        trigger = entry.definition
        if trigger.type in {
            TriggerType.STATE,
            TriggerType.NUMERIC_STATE,
            TriggerType.ZONE,
        }:

            def state_listener(event: Event[Any]) -> None:
                self._hass.create_task(
                    self._async_state_changed(key, event),
                    f"reminders trigger {trigger.type.value}",
                )

            entry.unsubscribe = async_track_state_change_event(
                self._hass, [trigger.entity_id or ""], state_listener
            )
        elif trigger.type is TriggerType.EVENT:

            def event_listener(event: Event[Any]) -> None:
                self._hass.create_task(
                    self._async_event_fired(key, event),
                    "reminders event trigger",
                )

            entry.unsubscribe = self._hass.bus.async_listen(
                trigger.event_type or "", event_listener
            )

    async def _async_state_changed(self, key: str, event: Event[Any]) -> None:
        entry = self._entries.get(key)
        if entry is None or self._unloaded:
            return
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if not isinstance(old_state, State) or not isinstance(new_state, State):
            self._cancel_duration(entry)
            return
        trigger = entry.definition
        matched = False
        remains_matching = False
        timer_value: Any = None
        if trigger.type is TriggerType.STATE:
            old_value = _observed(old_state, trigger.attribute)
            new_value = _observed(new_state, trigger.attribute)
            if old_value == new_value:
                return
            matched = (
                trigger.from_value is None or old_value == trigger.from_value
            ) and (trigger.to_value is None or new_value == trigger.to_value)
            remains_matching = _state_current_matches(trigger, new_value)
            timer_value = new_value
        elif trigger.type is TriggerType.NUMERIC_STATE:
            old_number = _number(old_state, trigger.attribute)
            new_number = _number(new_state, trigger.attribute)
            matched = not _numeric_matches(old_number, trigger) and _numeric_matches(
                new_number, trigger
            )
            remains_matching = _numeric_matches(new_number, trigger)
        elif trigger.type is TriggerType.ZONE:
            old_inside = self._zone_match(trigger, old_state)
            new_inside = self._zone_match(trigger, new_state)
            matched = (
                trigger.event is ZoneEvent.ENTER and not old_inside and new_inside
            ) or (trigger.event is ZoneEvent.LEAVE and old_inside and not new_inside)
            remains_matching = (
                new_inside if trigger.event is ZoneEvent.ENTER else not new_inside
            )
        if not remains_matching:
            self._cancel_duration(entry)
        if not matched:
            return
        context = {
            "entity_id": trigger.entity_id,
            "from": _safe_state_value(old_state, trigger.attribute),
            "to": _safe_state_value(new_state, trigger.attribute),
        }
        if trigger.for_seconds:
            self._start_duration(key, entry, timer_value, context)
            return
        await self._activate(entry, "future_transition", context)

    async def _async_event_fired(self, key: str, event: Event[Any]) -> None:
        entry = self._entries.get(key)
        if entry is None or self._unloaded:
            return
        expected = entry.definition.event_data or {}
        data = dict(event.data)
        if not event_data_matches(expected, data):
            return
        await self._activate(
            entry,
            "home_assistant_event",
            {"event_type": entry.definition.event_type, "matched_data": expected},
        )

    def _start_duration(
        self, key: str, entry: _Entry, timer_value: Any, context: dict[str, Any]
    ) -> None:
        self._cancel_duration(entry)
        entry.timer_value = timer_value

        async def elapsed(_now: Any) -> None:
            current = self._entries.get(key)
            if current is not entry or not self._duration_still_matches(entry):
                return
            entry.cancel_timer = None
            await self._activate(entry, "future_transition", context)

        entry.cancel_timer = async_call_later(
            self._hass, entry.definition.for_seconds, elapsed
        )

    def _duration_still_matches(self, entry: _Entry) -> bool:
        trigger = entry.definition
        state = self._hass.states.get(trigger.entity_id or "")
        if state is None:
            return False
        if trigger.type is TriggerType.STATE:
            value = _observed(state, trigger.attribute)
            if trigger.to_value is None and trigger.from_value is None:
                return bool(value == entry.timer_value)
            return _state_current_matches(trigger, value)
        if trigger.type is TriggerType.NUMERIC_STATE:
            return _numeric_matches(_number(state, trigger.attribute), trigger)
        return self._zone_match(trigger, state) == (trigger.event is ZoneEvent.ENTER)

    async def _activate(
        self, entry: _Entry, cause: str, context: dict[str, Any]
    ) -> None:
        for reminder_id in tuple(entry.reminder_ids):
            try:
                await self._callback(reminder_id, cause, context)
            except Exception:
                _LOGGER.exception("Error activating triggered reminder")

    def _zone_match(self, trigger: TriggerDefinition, state: State | None) -> bool:
        zone_state = self._hass.states.get(trigger.zone_entity_id or "")
        if state is None or zone_state is None:
            return False
        try:
            latitude = float(state.attributes["latitude"])
            longitude = float(state.attributes["longitude"])
            gps_accuracy = float(state.attributes.get("gps_accuracy", 0))
        except KeyError, TypeError, ValueError:
            return False
        try:
            return in_zone(zone_state, latitude, longitude, gps_accuracy)
        except KeyError, TypeError, ValueError:
            return False

    def _cancel_duration(self, entry: _Entry) -> None:
        if entry.cancel_timer is not None:
            entry.cancel_timer()
            entry.cancel_timer = None
            entry.timer_value = None

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        self._cancel_duration(entry)
        if entry.unsubscribe is not None:
            entry.unsubscribe()


def _should_listen(reminder: Reminder) -> bool:
    """Return whether the legacy activation trigger remains armed."""
    return (
        reminder.activation_type is ActivationType.TRIGGER
        and reminder.trigger is not None
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


def _subscriptions(reminder: Reminder) -> list[tuple[str, TriggerDefinition]]:
    """Build role-tagged subscriptions while preserving legacy activation IDs."""
    result: list[tuple[str, TriggerDefinition]] = []
    if _should_listen(reminder) and reminder.trigger is not None:
        result.append((reminder.id, reminder.trigger))
    if (
        reminder.deliver_when is not None
        and reminder.status is ReminderStatus.WAITING_FOR_CONTEXT
    ):
        result.append((f"{reminder.id}::deliver_when", reminder.deliver_when))
    if reminder.complete_when is not None and reminder.status in {
        ReminderStatus.PENDING,
        ReminderStatus.WAITING_FOR_CONTEXT,
        ReminderStatus.DELIVERING,
        ReminderStatus.AWAITING_ACKNOWLEDGEMENT,
    }:
        result.append((f"{reminder.id}::complete_when", reminder.complete_when))
    return result


def _observed(state: State, attribute: str | None) -> Any:
    return state.attributes.get(attribute) if attribute else state.state


def _state_current_matches(trigger: TriggerDefinition, value: Any) -> bool:
    if trigger.to_value is not None:
        return bool(value == trigger.to_value)
    if trigger.from_value is not None:
        return bool(value != trigger.from_value)
    return True


def _number(state: State | None, attribute: str | None) -> float | None:
    if state is None:
        return None
    value = _observed(state, attribute)
    if value in (None, "unknown", "unavailable"):
        return None
    try:
        result = float(value)
    except TypeError, ValueError:
        return None
    return (
        result
        if result == result and result not in (float("inf"), float("-inf"))
        else None
    )


def _numeric_matches(value: float | None, trigger: TriggerDefinition) -> bool:
    return (
        value is not None
        and (trigger.above is None or value > trigger.above)
        and (trigger.below is None or value < trigger.below)
    )


def _safe_state_value(state: State, attribute: str | None) -> Any:
    value = _observed(state, attribute)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:256]
