"""Crash-safety tests for persistent-notification cleanup."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from custom_components.reminders.delivery import DeliveryResult
from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    DeliveryPolicy,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.persistent_cleanup import (
    PERSISTENT_CLEANUP_COORDINATOR_DATA,
    PersistentCleanupCoordinator,
    persistent_notification_id,
)

from .conftest import FakeStore


class CleanupStore:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.saved: list[dict[str, Any]] = []

    async def async_load(self) -> dict[str, Any] | None:
        return self.data

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = data
        self.saved.append(data)


class Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], **kwargs: Any
    ) -> None:
        self.calls.append((domain, service, data, kwargs))


def _hass(services: Services | None = None) -> SimpleNamespace:
    return SimpleNamespace(data={}, services=services or Services())


def _reminder(
    reminder_id: str, occurrence: Occurrence, *, status: ReminderStatus
) -> Reminder:
    now = datetime.now(UTC)
    return Reminder(
        id=reminder_id,
        user_id="user",
        title="Durable cleanup",
        due=occurrence.due,
        created_at=now,
        updated_at=now,
        status=status,
        delivery_policy=DeliveryPolicy(("persistent_notification",)),
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
    )


async def test_delete_cleanup_intent_survives_restart_after_state_commit() -> None:
    services = Services()
    hass = _hass(services)
    store = CleanupStore()
    coordinator = PersistentCleanupCoordinator(hass, store)  # type: ignore[arg-type]
    await coordinator.async_load()
    now = datetime.now(UTC)
    occurrence = Occurrence("occ-1", now, now)
    reminder = _reminder("delete-me", occurrence, status=ReminderStatus.PENDING)
    old_pruned_id = persistent_notification_id(reminder.id, "old-pruned")
    await coordinator.async_track(reminder.id, "old-pruned", old_pruned_id)
    await coordinator.async_prepare_transition({reminder.id: reminder}, {})

    restarted_hass = _hass(services)
    restarted = PersistentCleanupCoordinator(
        restarted_hass,
        CleanupStore(store.data),  # type: ignore[arg-type]
    )
    await restarted.async_load()
    await restarted.async_reconcile({})

    dismissed = {call[2]["notification_id"] for call in services.calls}
    assert old_pruned_id in dismissed
    assert persistent_notification_id(reminder.id, occurrence.id) in dismissed
    assert persistent_notification_id(reminder.id, None) in dismissed


async def test_uncommitted_delete_intent_is_discarded_after_restart() -> None:
    services = Services()
    hass = _hass(services)
    store = CleanupStore()
    coordinator = PersistentCleanupCoordinator(hass, store)  # type: ignore[arg-type]
    await coordinator.async_load()
    now = datetime.now(UTC)
    occurrence = Occurrence("occ-1", now, now)
    reminder = _reminder("still-here", occurrence, status=ReminderStatus.PENDING)
    await coordinator.async_prepare_transition({reminder.id: reminder}, {})

    restarted = PersistentCleanupCoordinator(
        hass,
        CleanupStore(store.data),  # type: ignore[arg-type]
    )
    await restarted.async_load()
    await restarted.async_reconcile({reminder.id: reminder})

    assert services.calls == []
    assert restarted._pending == {}


async def test_delivering_completion_keeps_cleanup_intent_until_provider_returns() -> (
    None
):
    services = Services()
    hass = _hass(services)
    store = CleanupStore()
    coordinator = PersistentCleanupCoordinator(hass, store)  # type: ignore[arg-type]
    await coordinator.async_load()
    now = datetime.now(UTC)
    delivering = Occurrence("occ-1", now, now, status=OccurrenceStatus.DELIVERING)
    old = _reminder("race", delivering, status=ReminderStatus.DELIVERING)
    completed_occurrence = delivering.updated(status=OccurrenceStatus.COMPLETED)
    new = old.updated(
        status=ReminderStatus.COMPLETED,
        occurrence_history=(completed_occurrence,),
    )
    notification_id = persistent_notification_id(old.id, delivering.id)
    await coordinator.async_track(old.id, delivering.id, notification_id)
    await coordinator.async_prepare_transition({old.id: old}, {new.id: new})

    await coordinator.async_handle_lifecycle(
        {
            "action": "completed",
            "reminder_id": old.id,
            "occurrence_id": delivering.id,
        }
    )
    assert notification_id in coordinator._pending

    await coordinator.async_finalize_delivery(old.id, delivering.id)

    assert notification_id not in coordinator._pending
    dismissed = [
        call[2]["notification_id"]
        for call in services.calls
        if call[2]["notification_id"] == notification_id
    ]
    assert len(dismissed) == 2


async def test_stale_delete_intent_is_not_consumed_by_other_lifecycle_event() -> None:
    services = Services()
    hass = _hass(services)
    store = CleanupStore()
    coordinator = PersistentCleanupCoordinator(hass, store)  # type: ignore[arg-type]
    await coordinator.async_load()
    now = datetime.now(UTC)
    occurrence = Occurrence("occ-1", now, now)
    reminder = _reminder("same-process", occurrence, status=ReminderStatus.PENDING)
    await coordinator.async_prepare_transition({reminder.id: reminder}, {})
    pending_before = dict(coordinator._pending)

    await coordinator.async_handle_lifecycle(
        {
            "action": "completed",
            "reminder_id": reminder.id,
            "occurrence_id": occurrence.id,
        }
    )

    assert coordinator._pending == pending_before


async def test_reconcile_cleans_tracked_notification_after_history_pruning() -> None:
    services = Services()
    hass = _hass(services)
    store = CleanupStore()
    coordinator = PersistentCleanupCoordinator(hass, store)  # type: ignore[arg-type]
    await coordinator.async_load()
    now = datetime.now(UTC)
    occurrence = Occurrence("current", now, now)
    reminder = _reminder("pruned", occurrence, status=ReminderStatus.PENDING)
    stale_id = persistent_notification_id(reminder.id, "old-pruned")
    await coordinator.async_track(reminder.id, "old-pruned", stale_id)

    await coordinator.async_reconcile({reminder.id: reminder})

    assert stale_id not in coordinator._tracked
    assert any(call[2]["notification_id"] == stale_id for call in services.calls)


async def test_manager_compensates_delivery_that_finishes_after_completion() -> None:
    services = Services()
    hass = _hass(services)
    cleanup = PersistentCleanupCoordinator(
        hass,
        CleanupStore(),  # type: ignore[arg-type]
    )
    await cleanup.async_load()
    hass.data[PERSISTENT_CLEANUP_COORDINATOR_DATA] = cleanup
    now = datetime.now(UTC)
    occurrence = Occurrence("occ-race", now, now, status=OccurrenceStatus.DELIVERING)
    reminder = _reminder("manager-race", occurrence, status=ReminderStatus.DELIVERING)

    class RacingDispatcher:
        async def async_deliver(
            self, delivery_reminder: Reminder, policy: DeliveryPolicy
        ) -> DeliveryResult:
            notification_id = persistent_notification_id(
                delivery_reminder.id, occurrence.id
            )
            await cleanup.async_track(
                delivery_reminder.id, occurrence.id, notification_id
            )
            current = manager._reminders[delivery_reminder.id]
            completed = occurrence.updated(status=OccurrenceStatus.COMPLETED)
            candidate = dict(manager._reminders)
            candidate[current.id] = current.updated(
                status=ReminderStatus.COMPLETED,
                occurrence_history=(completed,),
            )
            await manager._async_persist_state(candidate, manager._users)
            return DeliveryResult(("persistent_notification",), ())

    manager = ReminderManager(
        hass,
        FakeStore(),
        RacingDispatcher(),  # type: ignore[arg-type]
    )
    manager._loaded = True
    manager._reminders = {reminder.id: reminder}

    await manager._async_deliver_claimed(reminder.id, now)

    notification_id = persistent_notification_id(reminder.id, occurrence.id)
    assert notification_id not in cleanup._pending
    assert any(call[2]["notification_id"] == notification_id for call in services.calls)


def test_setup_registers_cleanup_before_manager_recovery() -> None:
    source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "__init__.py"
    ).read_text(encoding="utf-8")
    assert source.index("async_register_persistent_cleanup(hass)") < source.index(
        "await manager.async_load()"
    )
