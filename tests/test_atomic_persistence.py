"""Failure-path tests for reminder runtime/storage atomicity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import Reminder, ReminderStatus
from custom_components.reminders.recurrence import RecurrenceFrequency, RecurrenceRule
from custom_components.reminders.storage import serialize_storage

from .conftest import FakeDispatcher, FakeStore


class SaveError(RuntimeError):
    """Raised by the controlled failing Store."""


class FailingStore(FakeStore):
    """Store that raises before changing its durable test state."""

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        *,
        fail_on_calls: set[int] | None = None,
    ) -> None:
        super().__init__(data)
        self.calls = 0
        self.fail_on_calls = fail_on_calls or set()

    async def async_save(self, data: dict[str, Any]) -> None:
        self.calls += 1
        if self.calls in self.fail_on_calls:
            raise SaveError("controlled Store failure")
        await super().async_save(data)


class Scheduler:
    """Capture scheduled timestamps and cancellations."""

    def __init__(self) -> None:
        self.calls: list[datetime] = []
        self.cancelled = 0

    def schedule(self, hass: Any, callback: Any, due: datetime) -> Any:
        self.calls.append(due)

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
    runtime = ReminderManager(  # type: ignore[arg-type]
        SimpleNamespace(), store, dispatcher
    )
    await runtime.async_load()
    return runtime


def _daily(anchor: datetime, interval: int = 1) -> RecurrenceRule:
    return RecurrenceRule(
        RecurrenceFrequency.DAILY,
        interval,
        "Europe/Dublin",
        anchor,
    )


def _reminder(*, recurring: bool = False) -> Reminder:
    now = datetime.now(UTC)
    due = now + timedelta(days=2)
    recurrence = _daily(due.astimezone().replace(tzinfo=None)) if recurring else None
    return Reminder(
        id="existing",
        user_id="u1",
        title="Original",
        due=due,
        scheduled_due=due if recurring else None,
        created_at=now,
        updated_at=now,
        recurrence=recurrence,
    )


async def test_create_save_failure_changes_neither_runtime_nor_scheduler(
    scheduler: Scheduler,
) -> None:
    store = FailingStore({"reminders": {}, "users": {}}, fail_on_calls={1})
    runtime = await _manager(store, FakeDispatcher(), scheduler)

    with pytest.raises(SaveError):
        await runtime.async_create(
            user_id="u1",
            title="Never committed",
            due=datetime.now(UTC) + timedelta(days=1),
        )

    assert await runtime.async_list() == []
    assert store.data == {"reminders": {}, "users": {}}
    assert runtime.scheduled_for is None
    assert scheduler.calls == []


async def test_recurring_create_save_failure_is_not_committed(
    scheduler: Scheduler,
) -> None:
    store = FailingStore({"reminders": {}, "users": {}}, fail_on_calls={1})
    runtime = await _manager(store, FakeDispatcher(), scheduler)

    with pytest.raises(SaveError):
        await runtime.async_create_recurring(
            user_id="u1",
            title="Never committed",
            recurrence=_daily(datetime(2027, 8, 2, 20)),
        )

    assert await runtime.async_list() == []
    assert runtime.scheduled_for is None
    assert scheduler.calls == []


async def test_update_save_failure_preserves_original_and_scheduler(
    scheduler: Scheduler,
) -> None:
    original = _reminder()
    store = FailingStore(
        serialize_storage({original.id: original}, {}), fail_on_calls={1}
    )
    runtime = await _manager(store, FakeDispatcher(), scheduler)

    with pytest.raises(SaveError):
        await runtime.async_update(
            original.id,
            title="Changed",
            due=original.due + timedelta(hours=3),
        )

    assert await runtime.async_get(original.id) == original
    assert runtime.scheduled_for == original.due
    assert scheduler.calls == [original.due]
    assert scheduler.cancelled == 0


async def test_recurrence_update_save_failure_preserves_phase(
    scheduler: Scheduler,
) -> None:
    original = _reminder(recurring=True)
    store = FailingStore(
        serialize_storage({original.id: original}, {}), fail_on_calls={1}
    )
    runtime = await _manager(store, FakeDispatcher(), scheduler)
    changed = _daily(datetime(2027, 8, 3, 9), interval=3)

    with pytest.raises(SaveError):
        await runtime.async_update(original.id, recurrence=changed)

    current = await runtime.async_get(original.id)
    assert current == original
    assert current.recurrence == original.recurrence
    assert current.due == original.due
    assert current.scheduled_due == original.scheduled_due
    assert runtime.scheduled_for == original.due
    assert scheduler.calls == [original.due]
    assert scheduler.cancelled == 0


async def test_delete_save_failure_preserves_reminder_and_scheduler(
    scheduler: Scheduler,
) -> None:
    original = _reminder()
    store = FailingStore(
        serialize_storage({original.id: original}, {}), fail_on_calls={1}
    )
    runtime = await _manager(store, FakeDispatcher(), scheduler)

    with pytest.raises(SaveError):
        await runtime.async_delete(original.id)

    assert await runtime.async_get(original.id) == original
    assert runtime.scheduled_for == original.due
    assert scheduler.calls == [original.due]
    assert scheduler.cancelled == 0


@pytest.mark.parametrize("recurring", [False, True])
async def test_snooze_save_failure_preserves_due_and_recurrence_phase(
    scheduler: Scheduler, recurring: bool
) -> None:
    original = _reminder(recurring=recurring)
    store = FailingStore(
        serialize_storage({original.id: original}, {}), fail_on_calls={1}
    )
    runtime = await _manager(store, FakeDispatcher(), scheduler)

    with pytest.raises(SaveError):
        await runtime.async_snooze(original.id, due=original.due + timedelta(hours=1))

    current = await runtime.async_get(original.id)
    assert current == original
    assert current.due == original.due
    assert current.scheduled_due == original.scheduled_due
    assert current.recurrence == original.recurrence
    assert runtime.scheduled_for == original.due
    assert scheduler.calls == [original.due]
    assert scheduler.cancelled == 0


async def test_delivery_claim_save_failure_does_not_call_provider(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    reminder = Reminder(
        id="due",
        user_id="u1",
        title="Due",
        due=now - timedelta(minutes=1),
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=1),
    )
    store = FailingStore(
        serialize_storage({reminder.id: reminder}, {}), fail_on_calls={1}
    )
    dispatcher = FakeDispatcher()
    runtime = ReminderManager(  # type: ignore[arg-type]
        SimpleNamespace(), store, dispatcher
    )

    with pytest.raises(SaveError):
        await runtime.async_load()

    assert dispatcher.calls == []
    assert await runtime.async_get(reminder.id) == reminder
    assert store.data["reminders"][reminder.id]["status"] == "pending"
    assert runtime.scheduled_for == reminder.due


async def test_recurring_result_save_failure_keeps_persisted_claim_recoverable(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    due = now - timedelta(minutes=1)
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Daily",
        due=due,
        scheduled_due=due,
        created_at=now - timedelta(days=3),
        updated_at=now - timedelta(days=3),
        recurrence=_daily((now - timedelta(days=2)).replace(tzinfo=None)),
    )
    store = FailingStore(
        serialize_storage({reminder.id: reminder}, {}), fail_on_calls={2}
    )
    dispatcher = FakeDispatcher()
    runtime = ReminderManager(  # type: ignore[arg-type]
        SimpleNamespace(), store, dispatcher
    )

    with pytest.raises(SaveError):
        await runtime.async_load()

    current = await runtime.async_get(reminder.id)
    assert len(dispatcher.calls) == 1
    assert current.status is ReminderStatus.DELIVERING
    assert current.due == reminder.due
    assert current.scheduled_due == reminder.scheduled_due
    assert store.data["reminders"][reminder.id]["status"] == "delivering"
    assert store.data["reminders"][reminder.id]["due"] == reminder.to_dict()["due"]

    recovery_dispatcher = FakeDispatcher()
    recovered = await _manager(store, recovery_dispatcher, scheduler)
    advanced = await recovered.async_get(reminder.id)
    # The first provider call succeeded but its result was not durable. Recovery
    # must retry because the same stored DELIVERING claim is also produced when a
    # crash happens before the provider runs; without provider idempotency, this
    # possible duplicate is the unavoidable conservative ambiguity.
    assert len(recovery_dispatcher.calls) == 1
    assert advanced.status is ReminderStatus.PENDING
    assert advanced.due > now
