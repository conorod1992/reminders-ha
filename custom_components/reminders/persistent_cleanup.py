"""Keep Home Assistant persistent notifications aligned with reminder state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN, LIFECYCLE_EVENT
from .models import OccurrenceStatus, Reminder

_LOGGER = logging.getLogger(__name__)
PERSISTENT_NOTIFICATION_DATA = f"{DOMAIN}_persistent_notification_ids"
PERSISTENT_CLEANUP_COORDINATOR_DATA = f"{DOMAIN}_persistent_cleanup_coordinator"
PERSISTENT_CLEANUP_STORE_KEY = f"{DOMAIN}.persistent_cleanup"

# External actions intentionally remain visible because selecting one does not
# necessarily resolve the occurrence. Delivery/failure events likewise keep the
# notification visible. These actions represent terminal or deferred resolution.
_DISMISS_ACTIONS = {
    "acknowledged",
    "automatically_completed",
    "cancelled",
    "completed",
    "deleted",
    "expired",
    "skipped",
    "snoozed",
}
_TERMINAL_OCCURRENCE_STATUSES = {
    OccurrenceStatus.ACKNOWLEDGED,
    OccurrenceStatus.COMPLETED,
    OccurrenceStatus.CANCELLED,
    OccurrenceStatus.EXPIRED,
    OccurrenceStatus.SKIPPED,
}


@dataclass(frozen=True, slots=True)
class _TrackedNotification:
    reminder_id: str
    occurrence_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "occurrence_id": self.occurrence_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> _TrackedNotification | None:
        if not isinstance(data, dict) or not isinstance(data.get("reminder_id"), str):
            return None
        occurrence_id = data.get("occurrence_id")
        return cls(
            data["reminder_id"],
            occurrence_id if isinstance(occurrence_id, str) else None,
        )


@dataclass(frozen=True, slots=True)
class _CleanupIntent:
    reminder_id: str
    occurrence_id: str | None
    delete: bool = False
    deferred: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "occurrence_id": self.occurrence_id,
            "delete": self.delete,
            "deferred": self.deferred,
        }

    @classmethod
    def from_dict(cls, data: Any) -> _CleanupIntent | None:
        if not isinstance(data, dict) or not isinstance(data.get("reminder_id"), str):
            return None
        occurrence_id = data.get("occurrence_id")
        return cls(
            reminder_id=data["reminder_id"],
            occurrence_id=occurrence_id if isinstance(occurrence_id, str) else None,
            delete=bool(data.get("delete", False)),
            deferred=bool(data.get("deferred", False)),
        )


class PersistentCleanupCoordinator:
    """Durably bridge reminder state commits and notification side effects."""

    def __init__(self, hass: HomeAssistant, store: Any | None = None) -> None:
        self._hass = hass
        self._store = store or Store[dict[str, Any]](
            hass,
            1,
            PERSISTENT_CLEANUP_STORE_KEY,
            atomic_writes=True,
        )
        self._lock = asyncio.Lock()
        self._tracked: dict[str, _TrackedNotification] = {}
        self._pending: dict[str, _CleanupIntent] = {}

    async def async_load(self) -> None:
        """Restore durable notification IDs and unfinished cleanup intents."""
        raw = await self._store.async_load()
        if not isinstance(raw, dict):
            return
        tracked = raw.get("tracked", {})
        pending = raw.get("pending", {})
        if isinstance(tracked, dict):
            for notification_id, value in tracked.items():
                tracked_item = _TrackedNotification.from_dict(value)
                if isinstance(notification_id, str) and tracked_item is not None:
                    self._tracked[notification_id] = tracked_item
        if isinstance(pending, dict):
            for notification_id, value in pending.items():
                pending_item = _CleanupIntent.from_dict(value)
                if isinstance(notification_id, str) and pending_item is not None:
                    self._pending[notification_id] = pending_item

    async def async_track(
        self,
        reminder_id: str,
        occurrence_id: str | None,
        notification_id: str,
    ) -> None:
        """Persist an ID before the external create side effect can happen."""
        async with self._lock:
            self._tracked[notification_id] = _TrackedNotification(
                reminder_id, occurrence_id
            )
            await self._async_save_locked()
        track_persistent_notification(self._hass, reminder_id, notification_id)

    async def async_abandon_failed_create(
        self, reminder_id: str, notification_id: str
    ) -> None:
        """Compensate an ambiguous create failure without forgetting failures."""
        succeeded = await _async_dismiss_ids(self._hass, reminder_id, {notification_id})
        if notification_id not in succeeded:
            return
        async with self._lock:
            self._tracked.pop(notification_id, None)
            self._pending.pop(notification_id, None)
            await self._async_save_locked()
        _untrack_volatile(self._hass, reminder_id, notification_id)

    async def async_prepare_transition(
        self,
        old_reminders: Mapping[str, Reminder],
        new_reminders: Mapping[str, Reminder],
    ) -> None:
        """Persist cleanup intent before the corresponding reminder-state write."""
        async with self._lock:
            additions = self._derive_intents(old_reminders, new_reminders)
            if not additions:
                return
            self._pending.update(additions)
            await self._async_save_locked()

    async def async_handle_lifecycle(self, data: Mapping[str, Any]) -> None:
        """Apply a committed lifecycle cleanup, retaining in-flight barriers."""
        action = data.get("action")
        reminder_id = data.get("reminder_id")
        occurrence_id = data.get("occurrence_id")
        occurrence_ids = data.get("occurrence_ids", ())
        if action not in _DISMISS_ACTIONS or not isinstance(reminder_id, str):
            return
        occurrence = occurrence_id if isinstance(occurrence_id, str) else None
        async with self._lock:
            selected = {
                notification_id: intent
                for notification_id, intent in self._pending.items()
                if intent.reminder_id == reminder_id
                and (
                    (action == "deleted" and intent.delete)
                    or (
                        action != "deleted"
                        and not intent.delete
                        and intent.occurrence_id == occurrence
                    )
                )
            }
            tracked_ids = {
                notification_id
                for notification_id, item in self._tracked.items()
                if item.reminder_id == reminder_id
                and (
                    action == "deleted"
                    or occurrence is None
                    or item.occurrence_id in {None, occurrence}
                )
            }
        known_occurrence_ids = (
            tuple(item for item in occurrence_ids if isinstance(item, str))
            if isinstance(occurrence_ids, (list, tuple))
            else ()
        )
        notification_ids = set(selected) | tracked_ids
        if action == "deleted":
            notification_ids.update(
                persistent_notification_id(reminder_id, item)
                for item in known_occurrence_ids
            )
        elif occurrence is not None:
            notification_ids.add(persistent_notification_id(reminder_id, occurrence))
        notification_ids.add(persistent_notification_id(reminder_id, None))
        succeeded = await _async_dismiss_ids(self._hass, reminder_id, notification_ids)
        if not succeeded:
            return
        async with self._lock:
            changed = False
            for notification_id in succeeded:
                intent = selected.get(notification_id)
                if intent is not None and intent.deferred:
                    continue
                if intent is not None and self._pending.get(notification_id) == intent:
                    self._pending.pop(notification_id, None)
                    changed = True
                if notification_id in tracked_ids and notification_id in self._tracked:
                    self._tracked.pop(notification_id, None)
                    changed = True
                    _untrack_volatile(self._hass, reminder_id, notification_id)
            if changed:
                await self._async_save_locked()

    async def async_finalize_delivery(
        self, reminder_id: str, occurrence_id: str | None
    ) -> None:
        """Dismiss again after an in-flight provider has definitely returned."""
        async with self._lock:
            notification_ids = {
                notification_id
                for notification_id, intent in self._pending.items()
                if intent.reminder_id == reminder_id
                and intent.occurrence_id in {None, occurrence_id}
            }
            notification_ids.update(
                notification_id
                for notification_id, item in self._tracked.items()
                if item.reminder_id == reminder_id
                and item.occurrence_id in {None, occurrence_id}
            )
        notification_ids.add(persistent_notification_id(reminder_id, occurrence_id))
        notification_ids.add(persistent_notification_id(reminder_id, None))
        succeeded = await _async_dismiss_ids(self._hass, reminder_id, notification_ids)
        if not succeeded:
            return
        async with self._lock:
            changed = False
            for notification_id in succeeded:
                intent = self._pending.get(notification_id)
                if intent is not None and intent.reminder_id == reminder_id:
                    self._pending.pop(notification_id, None)
                    changed = True
                tracked = self._tracked.get(notification_id)
                if tracked is not None and tracked.reminder_id == reminder_id:
                    self._tracked.pop(notification_id, None)
                    changed = True
                    _untrack_volatile(self._hass, reminder_id, notification_id)
            if changed:
                await self._async_save_locked()

    async def async_reconcile(self, reminders: Mapping[str, Reminder]) -> None:
        """Repair cleanup intents after restart and discard uncommitted intents."""
        async with self._lock:
            pending_snapshot = dict(self._pending)
            tracked_snapshot = dict(self._tracked)
        dismiss: dict[str, str] = {}
        stale: set[str] = set()
        for notification_id, intent in pending_snapshot.items():
            if _intent_committed(intent, reminders):
                dismiss[notification_id] = intent.reminder_id
            else:
                stale.add(notification_id)
        for notification_id, tracked in tracked_snapshot.items():
            reminder = reminders.get(tracked.reminder_id)
            if reminder is None or _tracked_notification_is_resolved(tracked, reminder):
                dismiss.setdefault(notification_id, tracked.reminder_id)
        succeeded: set[str] = set()
        for reminder_id in set(dismiss.values()):
            ids = {
                notification_id
                for notification_id, owner in dismiss.items()
                if owner == reminder_id
            }
            succeeded.update(await _async_dismiss_ids(self._hass, reminder_id, ids))
        async with self._lock:
            changed = False
            for notification_id in stale:
                if (
                    self._pending.get(notification_id)
                    == pending_snapshot[notification_id]
                ):
                    self._pending.pop(notification_id, None)
                    changed = True
            for notification_id in succeeded:
                if notification_id in self._pending:
                    self._pending.pop(notification_id, None)
                    changed = True
                tracked_item = self._tracked.get(notification_id)
                if tracked_item is not None:
                    self._tracked.pop(notification_id)
                    changed = True
                    _untrack_volatile(
                        self._hass, tracked_item.reminder_id, notification_id
                    )
            if changed:
                await self._async_save_locked()

    def _derive_intents(
        self,
        old_reminders: Mapping[str, Reminder],
        new_reminders: Mapping[str, Reminder],
    ) -> dict[str, _CleanupIntent]:
        additions: dict[str, _CleanupIntent] = {}
        for reminder_id, old in old_reminders.items():
            if reminder_id in new_reminders:
                continue
            notification_ids = {
                persistent_notification_id(reminder_id, occurrence.id)
                for occurrence in old.occurrence_history
            }
            notification_ids.add(persistent_notification_id(reminder_id, None))
            notification_ids.update(
                notification_id
                for notification_id, tracked in self._tracked.items()
                if tracked.reminder_id == reminder_id
            )
            for notification_id in notification_ids:
                additions[notification_id] = _CleanupIntent(
                    reminder_id, None, delete=True
                )

        for reminder_id, new in new_reminders.items():
            previous_reminder = old_reminders.get(reminder_id)
            if previous_reminder is None:
                continue
            old_occurrences = {
                item.id: item for item in previous_reminder.occurrence_history
            }
            for occurrence in new.occurrence_history:
                if occurrence.status not in _TERMINAL_OCCURRENCE_STATUSES:
                    continue
                previous = old_occurrences.get(occurrence.id)
                if (
                    previous is not None
                    and previous.status in _TERMINAL_OCCURRENCE_STATUSES
                ):
                    continue
                deferred = previous is not None and (
                    previous.status is OccurrenceStatus.DELIVERING
                    or any(attempt.in_flight for attempt in previous.escalation_history)
                )
                notification_ids = {
                    persistent_notification_id(reminder_id, occurrence.id),
                    persistent_notification_id(reminder_id, None),
                }
                notification_ids.update(
                    notification_id
                    for notification_id, tracked in self._tracked.items()
                    if tracked.reminder_id == reminder_id
                    and tracked.occurrence_id in {None, occurrence.id}
                )
                for notification_id in notification_ids:
                    current = additions.get(notification_id)
                    additions[notification_id] = _CleanupIntent(
                        reminder_id,
                        occurrence.id,
                        deferred=deferred or bool(current and current.deferred),
                    )
        return additions

    async def _async_save_locked(self) -> None:
        await self._store.async_save(
            {
                "tracked": {
                    notification_id: item.to_dict()
                    for notification_id, item in self._tracked.items()
                },
                "pending": {
                    notification_id: item.to_dict()
                    for notification_id, item in self._pending.items()
                },
            }
        )


def persistent_notification_id(reminder_id: str, occurrence_id: str | None) -> str:
    """Return a stable notification ID without conflating series occurrences."""
    if occurrence_id:
        return f"reminders_{reminder_id}_{occurrence_id}"
    return f"reminders_{reminder_id}"


@callback
def track_persistent_notification(
    hass: HomeAssistant, reminder_id: str, notification_id: str
) -> None:
    """Keep a volatile mirror for compatibility and same-process delete cleanup."""
    tracked = hass.data.setdefault(PERSISTENT_NOTIFICATION_DATA, {})
    tracked.setdefault(reminder_id, set()).add(notification_id)


async def async_track_persistent_notification(
    hass: HomeAssistant,
    reminder_id: str,
    occurrence_id: str | None,
    notification_id: str,
) -> None:
    """Durably record an ID before its create service call."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        track_persistent_notification(hass, reminder_id, notification_id)
        return
    await coordinator.async_track(reminder_id, occurrence_id, notification_id)


async def async_abandon_failed_persistent_create(
    hass: HomeAssistant, reminder_id: str, notification_id: str
) -> None:
    """Compensate a provider create that raised after an ambiguous side effect."""
    coordinator = _coordinator(hass)
    if coordinator is None:
        return
    await coordinator.async_abandon_failed_create(reminder_id, notification_id)


async def async_prepare_persistent_cleanup(
    hass: HomeAssistant,
    old_reminders: Mapping[str, Reminder],
    new_reminders: Mapping[str, Reminder],
) -> None:
    """Persist cleanup intent before reminder state can make it necessary."""
    coordinator = _coordinator(hass)
    if coordinator is not None:
        await coordinator.async_prepare_transition(old_reminders, new_reminders)


async def async_finalize_persistent_delivery_cleanup(
    hass: HomeAssistant, reminder_id: str, occurrence_id: str | None
) -> None:
    """Compensate a provider that returned after its occurrence resolved."""
    coordinator = _coordinator(hass)
    if coordinator is not None:
        await coordinator.async_finalize_delivery(reminder_id, occurrence_id)
        return
    data = getattr(hass, "data", None)
    services = getattr(hass, "services", None)
    if not isinstance(data, dict) or services is None:
        return
    await _async_dismiss(
        hass,
        reminder_id,
        occurrence_id,
        dismiss_all=False,
    )


def async_register_persistent_cleanup(hass: HomeAssistant) -> Callable[[], None]:
    """Dismiss persistent notifications when their reminder occurrence resolves."""

    @callback
    def lifecycle_event(event: Event[Any]) -> None:
        action = event.data.get("action")
        reminder_id = event.data.get("reminder_id")
        occurrence_id = event.data.get("occurrence_id")
        occurrence_ids = event.data.get("occurrence_ids", ())
        if action not in _DISMISS_ACTIONS or not isinstance(reminder_id, str):
            return
        coordinator = _coordinator(hass)
        if coordinator is not None:
            coroutine = coordinator.async_handle_lifecycle(event.data)
        else:
            known_occurrence_ids = (
                tuple(item for item in occurrence_ids if isinstance(item, str))
                if isinstance(occurrence_ids, (list, tuple))
                else ()
            )
            coroutine = _async_dismiss(
                hass,
                reminder_id,
                occurrence_id if isinstance(occurrence_id, str) else None,
                occurrence_ids=known_occurrence_ids,
                dismiss_all=action == "deleted",
            )
        hass.async_create_task(
            coroutine,
            f"reminders dismiss persistent notification {reminder_id}",
        )

    return hass.bus.async_listen(LIFECYCLE_EVENT, lifecycle_event)


async def _async_dismiss(
    hass: HomeAssistant,
    reminder_id: str,
    occurrence_id: str | None,
    *,
    occurrence_ids: tuple[str, ...] = (),
    dismiss_all: bool = False,
) -> None:
    """Dismiss matching persistent notifications without failing reminder state."""
    tracked = hass.data.setdefault(PERSISTENT_NOTIFICATION_DATA, {})
    known = tracked.get(reminder_id, set())
    if dismiss_all:
        notification_ids = set(known)
        notification_ids.update(
            persistent_notification_id(reminder_id, item) for item in occurrence_ids
        )
        notification_ids.add(persistent_notification_id(reminder_id, None))
    else:
        notification_ids = {persistent_notification_id(reminder_id, occurrence_id)}
        notification_ids.add(persistent_notification_id(reminder_id, None))

    succeeded = await _async_dismiss_ids(hass, reminder_id, notification_ids)
    for notification_id in succeeded:
        known.discard(notification_id)
    if dismiss_all or not known:
        tracked.pop(reminder_id, None)


async def _async_dismiss_ids(
    hass: HomeAssistant, reminder_id: str, notification_ids: set[str]
) -> set[str]:
    succeeded: set[str] = set()
    for notification_id in notification_ids:
        try:
            await hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": notification_id},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception(
                "Unable to dismiss persistent notification %s for reminder %s",
                notification_id,
                reminder_id,
            )
        else:
            succeeded.add(notification_id)
    return succeeded


def _intent_committed(
    intent: _CleanupIntent, reminders: Mapping[str, Reminder]
) -> bool:
    reminder = reminders.get(intent.reminder_id)
    if intent.delete:
        return reminder is None
    if reminder is None:
        return True
    if intent.occurrence_id is None:
        return False
    occurrence = next(
        (
            item
            for item in reminder.occurrence_history
            if item.id == intent.occurrence_id
        ),
        None,
    )
    return occurrence is None or occurrence.status in _TERMINAL_OCCURRENCE_STATUSES


def _tracked_notification_is_resolved(
    tracked: _TrackedNotification, reminder: Reminder
) -> bool:
    if tracked.occurrence_id is None:
        return False
    occurrence = next(
        (
            item
            for item in reminder.occurrence_history
            if item.id == tracked.occurrence_id
        ),
        None,
    )
    return occurrence is None or occurrence.status in _TERMINAL_OCCURRENCE_STATUSES


def _untrack_volatile(
    hass: HomeAssistant, reminder_id: str, notification_id: str
) -> None:
    tracked = hass.data.setdefault(PERSISTENT_NOTIFICATION_DATA, {})
    known = tracked.get(reminder_id)
    if not isinstance(known, set):
        return
    known.discard(notification_id)
    if not known:
        tracked.pop(reminder_id, None)


def _coordinator(hass: HomeAssistant) -> PersistentCleanupCoordinator | None:
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        return None
    value = data.get(PERSISTENT_CLEANUP_COORDINATOR_DATA)
    return value if isinstance(value, PersistentCleanupCoordinator) else None
