"""Interoperability-focused extensions for externally managed reminders."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

from homeassistant.util import dt as dt_util

from .const import LIFECYCLE_EVENT, LIFECYCLE_SCHEMA_VERSION
from .manager import ReminderValidationError, _validate_source_metadata
from .models import OccurrenceStatus, Reminder, ReminderStatus
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
        source, source_id, _ = _validate_source_metadata(source, source_id, None)
        assert source is not None
        assert source_id is not None
        async with self._external_mutation_lock:
            matches = await self.async_list(
                user_id=user_id,
                source=source,
                source_id=source_id,
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
        source, _, _ = _validate_source_metadata(source, None, None)
        assert source is not None
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
                    and reminder.source == source
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
        occurrence = (
            next(
                (
                    item
                    for item in reminder.occurrence_history
                    if item.id == occurrence_id
                ),
                None,
            )
            if occurrence_id is not None
            else None
        )
        payload: dict[str, Any] = {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "reminder_id": reminder.id,
            "occurrence_id": occurrence_id,
            "user_id": reminder.user_id,
            "source": reminder.source,
            "source_id": reminder.source_id,
            "source_event": reminder.source_event,
            "action": action,
            "managed_externally": reminder.managed_externally,
            "activation_type": reminder.activation_type.value,
            "recurring": reminder.recurrence is not None,
            "reminder_status": reminder.status.value,
            "occurrence_status": (
                occurrence.status.value if occurrence is not None else None
            ),
            "event_time": dt_util.utcnow().isoformat(),
        }
        if external_action_id is not None:
            payload["external_action_id"] = external_action_id
        fire(LIFECYCLE_EVENT, payload)


def occurrence_is_resolved(status: OccurrenceStatus) -> bool:
    """Return whether an occurrence status is terminal for API consumers."""
    return status not in {
        OccurrenceStatus.SCHEDULED,
        OccurrenceStatus.DELIVERING,
        OccurrenceStatus.WAITING_FOR_CONTEXT,
        OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
    }
