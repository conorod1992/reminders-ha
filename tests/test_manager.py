"""Behavior tests for ReminderManager."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.manager import (
    ReminderManager,
    ReminderNotFoundError,
)
from custom_components.reminders.models import (
    DeliveryPolicy,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.storage import serialize_storage

from .conftest import FakeDispatcher, FakeStore


class Scheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, datetime]] = []
        self.cancelled = 0

    def schedule(self, hass: Any, callback: Any, due: datetime) -> Any:
        self.calls.append((callback, due))

        def cancel() -> None:
            self.cancelled += 1

        return cancel


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> Scheduler:
    result = Scheduler()
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        result.schedule,
    )
    return result


async def _manager(
    store: FakeStore, dispatcher: FakeDispatcher, scheduler: Scheduler
) -> ReminderManager:
    manager = ReminderManager(SimpleNamespace(), store, dispatcher)  # type: ignore[arg-type]
    await manager.async_load()
    return manager


async def test_crud_and_user_isolation(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    manager = await _manager(fake_store, FakeDispatcher(), scheduler)
    due = datetime.now(UTC) + timedelta(hours=2)
    first = await manager.async_create(user_id="u1", title="One", due=due)
    second = await manager.async_create(
        user_id="u2", title="Two", due=due + timedelta(hours=1)
    )
    assert await manager.async_get(first.id) == first
    assert await manager.async_list(user_id="u1") == [first]
    changed = await manager.async_update(first.id, title="Updated")
    assert changed.title == "Updated"
    await manager.async_delete(second.id)
    with pytest.raises(ReminderNotFoundError):
        await manager.async_get(second.id)


async def test_earliest_only_and_intelligent_rescheduling(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    manager = await _manager(fake_store, FakeDispatcher(), scheduler)
    base = datetime.now(UTC) + timedelta(hours=2)
    late = await manager.async_create(user_id="u1", title="Late", due=base)
    assert manager.scheduled_for == base
    assert len(scheduler.calls) == 1

    await manager.async_create(
        user_id="u1", title="Later", due=base + timedelta(hours=1)
    )
    assert len(scheduler.calls) == 1

    await manager.async_create(user_id="u1", title="Same", due=base)
    assert len(scheduler.calls) == 1

    early = await manager.async_create(
        user_id="u1", title="Early", due=base - timedelta(hours=1)
    )
    assert manager.scheduled_for == early.due
    assert scheduler.cancelled == 1
    assert len(scheduler.calls) == 2

    await manager.async_delete(early.id)
    assert manager.scheduled_for == late.due
    assert len(scheduler.calls) == 3


async def test_same_due_batch_and_restart_recovery(scheduler: Scheduler) -> None:
    now = datetime.now(UTC)
    reminders = {
        key: Reminder(
            id=key,
            user_id="u1",
            title=key,
            due=now - timedelta(minutes=1),
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1),
        )
        for key in ("one", "two")
    }
    store = FakeStore(serialize_storage(reminders, {}))
    dispatcher = FakeDispatcher()
    manager = await _manager(store, dispatcher, scheduler)
    assert len(dispatcher.calls) == 2
    assert {item.status for item in await manager.async_list()} == {
        ReminderStatus.DELIVERED
    }
    assert all(
        raw["status"] == ReminderStatus.DELIVERING
        for raw in store.saved[0]["reminders"].values()
    )
    assert manager.scheduled_for is None


async def test_future_reminder_survives_restart(scheduler: Scheduler) -> None:
    now = datetime.now(UTC)
    reminder = Reminder(
        id="future",
        user_id="u1",
        title="Future",
        due=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    manager = await _manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        FakeDispatcher(),
        scheduler,
    )
    assert manager.scheduled_for == reminder.due
    assert len(scheduler.calls) == 1


async def test_live_default_and_custom_override(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher, scheduler)
    due = datetime.now(UTC) + timedelta(hours=1)
    default_reminder = await manager.async_create(
        user_id="u1", title="Default", due=due
    )
    custom = DeliveryPolicy(("persistent_notification",))
    custom_reminder = await manager.async_create(
        user_id="u1", title="Custom", due=due, delivery_policy=custom
    )
    changed_default = DeliveryPolicy(("phone",), ("notify.phone",))
    await manager.async_set_user_preferences("u1", changed_default)

    await manager._async_process_due(due)
    policies = {reminder.id: policy for reminder, policy in dispatcher.calls}
    assert policies[default_reminder.id] == changed_default
    assert policies[custom_reminder.id] == custom


async def test_provider_failure_is_stored_without_reschedule_loop(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    manager = await _manager(fake_store, FakeDispatcher(succeeds=False), scheduler)
    reminder = await manager.async_create(
        user_id="u1",
        title="Fail",
        due=datetime.now(UTC) - timedelta(seconds=1),
    )
    stored = await manager.async_get(reminder.id)
    assert stored.status is ReminderStatus.FAILED
    assert stored.delivery_errors
    assert manager.scheduled_for is None


async def test_unload_cancels_callback(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    manager = await _manager(fake_store, FakeDispatcher(), scheduler)
    await manager.async_create(
        user_id="u1",
        title="Future",
        due=datetime.now(UTC) + timedelta(hours=1),
    )
    await manager.async_unload()
    assert scheduler.cancelled == 1
    assert manager.scheduled_for is None
