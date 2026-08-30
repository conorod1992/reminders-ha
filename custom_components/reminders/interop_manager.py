"""Interoperability-focused extensions for externally managed reminders."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

from homeassistant.util import dt as dt_util

from .const import LIFECYCLE_EVENT, LIFECYCLE_SCHEMA_VERSION
from .manager import ReminderValidationError, _validate_source_metadata
from .models import Reminder, ReminderStatus
from .native_manager import NativeReminderManager

_T = TypeVar("_T")


class InteropReminderManager(NativeReminderManager):
    """Native manager with stable external-source synchronization helpers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._external_mutation_lock = asyncio.Lock()

    async def async_upsert_external(
        self,
        *,
        user_id: str,
        source: str,
        source_id: str,
        create: Callable[[], Awaitable[_T]],
        update: Callable[[Reminder], Awaitable[_T]],
    ) -> tuple[bool, _T]:
        """Serialize one create-or-update operation by owner/source/source ID."""
        normalized_source, normalized_source_id, _ = _validate_source_metadata(
            source, source_id, None
        )
        assert normalized_source is not None
        assert normalized_source_id is not None
        async with self._external_mutation_lock:
            matches = await self.async_list(
                user_id=user_id,
                source=normalized_source,
                source_id=normalized_source_id,
                limit=2,
            )
            if len(matches) > 1:
                raise ReminderValidationError(
                    "Multiple reminders already use this external source key"
                )
            if matches:
                current = matches[0]
                if not current.managed_externally:
                    raise ReminderValidationError(
                        "External source key belongs to a reminder that is not "
                        "externally managed"
                    )
                return False, await update(current)
            return True, await create()

    async def async_reconcile_external_source(
        self,
        *,
        user_id: str,
        source: str,
        keep_source_ids: Iterable[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Atomically delete stale externally managed records for one source."""
        normalized_source, _, _ = _validate_source_metadata(source, None, None)
        assert normalized_source is not None
        keep: set[str] = set()
        for value in keep_source_ids:
            _, normalized, _ = _validate_source_metadata(None, value, None)
            assert normalized is not None
            keep.add(normalized)

        async with self._external_mutation_lock:
            async with self._lock:
                matches = [
                    reminder
                    for reminder in self._reminders.values()
                    if reminder.user_id == user_id
                    and reminder.source == normalized_source
                    and reminder.managed_externally
                    and reminder.source_id is not None
                    and reminder.source_id not in keep
                ]
                skipped = tuple(
                    reminder.id
                    for reminder in matches
                    if reminder.status is ReminderStatus.DELIVERING
                )
                deleted = tuple(
                    reminder
                    for reminder in matches
                    if reminder.status is not ReminderStatus.DELIVERING
                )
                if not deleted:
                    return (), skipped
                candidate = dict(self._reminders)
                for reminder in deleted:
                    candidate.pop(reminder.id, None)
                await self._async_persist_state(candidate, self._users)
                self._reschedule(force=True)

            for reminder in deleted:
                self._cancel_trigger_duration_timers(reminder.id)
            await self._trigger_registry.async_sync(self._reminders.values())
            await self._native_runtime.async_sync(self._reminders.values())
            self._notify_changed({user_id})
            for reminder in deleted:
                self._fire_lifecycle_event(reminder, "deleted")
            return tuple(reminder.id for reminder in deleted), skipped

    def _fire_lifecycle_event(
        self,
        reminder: Reminder,
        action: str,
        *,
        occurrence_id: str | None = None,
        external_action_id: str | None = None,
    ) -> None:
        """Publish the backwards-compatible versioned interoperability event."""
        bus = getattr(self._hass, "bus", None)
        fire = getattr(bus, "async_fire", None)
        if not callable(fire):
            return

        # Most lifecycle callers retain the pre-transition Reminder object while the
        # committed replacement is already in manager state. Prefer that committed
        # snapshot so enriched status fields describe the action that just happened,
        # not the state immediately before it. Deleted reminders are absent from the
        # manager, so they deliberately fall back to the retained pre-delete snapshot.
        current_reminders = getattr(self, "_reminders", {})
        event_reminder = current_reminders.get(reminder.id, reminder)
        occurrence = (
            next(
                (
                    item
                    for item in event_reminder.occurrence_history
                    if item.id == occurrence_id
                ),
                None,
            )
            if occurrence_id is not None
            else None
        )
        payload: dict[str, Any] = {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "reminder_id": event_reminder.id,
            "occurrence_id": occurrence_id,
            "user_id": event_reminder.user_id,
            "source": event_reminder.source,
            "source_id": event_reminder.source_id,
            "source_event": event_reminder.source_event,
            "action": action,
            "managed_externally": event_reminder.managed_externally,
            "activation_type": event_reminder.activation_type.value,
            "recurring": event_reminder.recurrence is not None,
            "reminder_status": event_reminder.status.value,
            "occurrence_status": (
                occurrence.status.value if occurrence is not None else None
            ),
            "event_time": dt_util.utcnow().isoformat(),
        }
        if action == "deleted":
            # Cleanup runs after the reminder has been removed from durable state.
            # Include retained occurrence IDs so persistent notifications can be
            # reconstructed even when the in-memory tracking cache was lost on restart.
            payload["occurrence_ids"] = [
                item.id for item in event_reminder.occurrence_history
            ]
        if external_action_id is not None:
            payload["external_action_id"] = external_action_id
        fire(LIFECYCLE_EVENT, payload)
