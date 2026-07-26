"""Manager integration tests for recurring reminders and durability."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.delivery import DeliveryResult
from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    DeliveryPolicy,
    Reminder,
    ReminderStatus,
    UserPreferences,
)
from custom_components.reminders.recurrence import (
    RecurrenceFrequency,
    RecurrenceRule,
    Weekday,
)
from custom_components.reminders.storage import serialize_storage

from .conftest import FakeDispatcher, FakeStore


class Scheduler:
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


def daily(anchor: datetime, interval: int = 1) -> RecurrenceRule:
    return RecurrenceRule(
        RecurrenceFrequency.DAILY,
        interval,
        "Europe/Dublin",
        anchor,
    )


async def manager(
    store: FakeStore, dispatcher: FakeDispatcher, scheduler: Scheduler
) -> ReminderManager:
    result = ReminderManager(SimpleNamespace(), store, dispatcher)  # type: ignore[arg-type]
    await result.async_load()
    return result


async def test_recurring_creation_is_persisted_before_return(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    reminder = await runtime.async_create_recurring(
        user_id="u1",
        title="Daily",
        recurrence=daily(datetime.now().replace(tzinfo=None) + timedelta(days=2)),
    )
    assert fake_store.saved[-1]["reminders"][reminder.id]["recurring"] is True
    assert (
        fake_store.saved[-1]["reminders"][reminder.id]["due"]
        == reminder.to_dict()["due"]
    )


async def test_create_waits_for_persistence(scheduler: Scheduler) -> None:
    class GateStore(FakeStore):
        def __init__(self) -> None:
            super().__init__({"reminders": {}, "users": {}})
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def async_save(self, data: dict[str, Any]) -> None:
            self.started.set()
            await self.release.wait()
            await super().async_save(data)

    store = GateStore()
    runtime = await manager(store, FakeDispatcher(), scheduler)
    task = asyncio.create_task(
        runtime.async_create(
            user_id="u1",
            title="Durable",
            due=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await store.started.wait()
    assert not task.done()
    store.release.set()
    reminder = await task
    assert reminder.id in store.saved[-1]["reminders"]


async def test_record_mutations_are_durable_before_return(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    due = datetime.now(UTC) + timedelta(days=2)
    reminder = await runtime.async_create(user_id="u1", title="One", due=due)
    assert reminder.id in fake_store.saved[-1]["reminders"]

    new_due = due + timedelta(hours=1)
    await runtime.async_update(reminder.id, due=new_due)
    assert fake_store.saved[-1]["reminders"][reminder.id]["due"] == new_due.isoformat()

    snoozed_due = new_due + timedelta(hours=1)
    await runtime.async_snooze(reminder.id, due=snoozed_due)
    assert (
        fake_store.saved[-1]["reminders"][reminder.id]["due"] == snoozed_due.isoformat()
    )

    await runtime.async_delete(reminder.id)
    assert reminder.id not in fake_store.saved[-1]["reminders"]


async def test_successful_occurrence_advances_and_persists(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    recurrence = daily((now - timedelta(days=2)).replace(tzinfo=None))
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Daily",
        due=now - timedelta(minutes=5),
        scheduled_due=now - timedelta(minutes=5),
        created_at=now - timedelta(days=5),
        updated_at=now - timedelta(days=5),
        recurrence=recurrence,
    )
    store = FakeStore(serialize_storage({reminder.id: reminder}, {}))
    runtime = await manager(store, FakeDispatcher(), scheduler)
    advanced = await runtime.async_get(reminder.id)
    assert advanced.status is ReminderStatus.PENDING
    assert advanced.last_occurrence_status is ReminderStatus.DELIVERED
    assert advanced.due > now
    assert store.saved[-1]["reminders"][reminder.id]["status"] == "pending"
    assert store.saved[-1]["reminders"][reminder.id]["due"] == advanced.to_dict()["due"]


async def test_failed_occurrence_does_not_stop_series(scheduler: Scheduler) -> None:
    now = datetime.now(UTC)
    recurrence = daily((now - timedelta(days=1)).replace(tzinfo=None))
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Daily",
        due=now - timedelta(minutes=1),
        scheduled_due=now - timedelta(minutes=1),
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
        recurrence=recurrence,
    )
    runtime = await manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        FakeDispatcher(succeeds=False),
        scheduler,
    )
    advanced = await runtime.async_get(reminder.id)
    assert advanced.status is ReminderStatus.PENDING
    assert advanced.last_occurrence_status is ReminderStatus.FAILED
    assert advanced.delivery_errors
    assert advanced.due > now


async def test_partial_provider_failure_advances_as_success(
    scheduler: Scheduler,
) -> None:
    class PartialDispatcher:
        async def async_deliver(self, reminder: Any, policy: Any) -> DeliveryResult:
            return DeliveryResult(
                ("persistent_notification",), ("phone: RuntimeError",)
            )

    now = datetime.now(UTC)
    recurrence = daily((now - timedelta(days=1)).replace(tzinfo=None))
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Daily",
        due=now - timedelta(minutes=1),
        scheduled_due=now - timedelta(minutes=1),
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
        recurrence=recurrence,
    )
    runtime = ReminderManager(  # type: ignore[arg-type]
        SimpleNamespace(),
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        PartialDispatcher(),  # type: ignore[arg-type]
    )
    await runtime.async_load()
    advanced = await runtime.async_get(reminder.id)
    assert advanced.status is ReminderStatus.PENDING
    assert advanced.last_occurrence_status is ReminderStatus.DELIVERED
    assert advanced.delivery_errors == ("phone: RuntimeError",)


async def test_long_outage_delivers_once_and_keeps_anchor_phase(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    recurrence = daily((now - timedelta(days=20)).replace(tzinfo=None))
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Daily",
        due=now - timedelta(days=10),
        scheduled_due=now - timedelta(days=10),
        created_at=now - timedelta(days=20),
        updated_at=now - timedelta(days=20),
        recurrence=recurrence,
    )
    dispatcher = FakeDispatcher()
    runtime = await manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        dispatcher,
        scheduler,
    )
    assert len(dispatcher.calls) == 1
    advanced = await runtime.async_get(reminder.id)
    assert advanced.due > now
    assert advanced.recurrence == recurrence


async def test_interrupted_delivery_recovers_without_losing_series(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    recurrence = daily((now - timedelta(days=2)).replace(tzinfo=None))
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Daily",
        due=now - timedelta(minutes=1),
        scheduled_due=now - timedelta(minutes=1),
        created_at=now - timedelta(days=3),
        updated_at=now - timedelta(minutes=1),
        recurrence=recurrence,
        status=ReminderStatus.DELIVERING,
    )
    dispatcher = FakeDispatcher()
    runtime = await manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        dispatcher,
        scheduler,
    )
    assert len(dispatcher.calls) == 1
    assert (await runtime.async_get(reminder.id)).status is ReminderStatus.PENDING


async def test_recurring_snooze_preserves_regular_phase(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    anchor = datetime.now().replace(tzinfo=None) + timedelta(days=2)
    reminder = await runtime.async_create_recurring(
        user_id="u1", title="Daily", recurrence=daily(anchor)
    )
    original_scheduled = reminder.scheduled_due
    snoozed = await runtime.async_snooze(
        reminder.id, due=reminder.due + timedelta(hours=1)
    )
    assert snoozed.scheduled_due == original_scheduled
    assert snoozed.due == reminder.due + timedelta(hours=1)
    assert fake_store.saved[-1]["reminders"][reminder.id]["scheduled_due"] == (
        original_scheduled.isoformat() if original_scheduled else None
    )
    snoozed_again = await runtime.async_snooze(
        reminder.id, due=snoozed.due + timedelta(hours=1)
    )
    assert snoozed_again.scheduled_due == original_scheduled


async def test_current_user_default_is_resolved_each_occurrence(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    dispatcher = FakeDispatcher()
    runtime = await manager(fake_store, dispatcher, scheduler)
    anchor = datetime.now().replace(tzinfo=None) + timedelta(days=1)
    reminder = await runtime.async_create_recurring(
        user_id="u1", title="Daily", recurrence=daily(anchor)
    )
    phone = DeliveryPolicy(("phone",), ("notify.phone",))
    await runtime.async_set_user_preferences("u1", phone)
    await runtime._async_process_due(reminder.due)
    assert dispatcher.calls[-1][1] == phone

    persistent = DeliveryPolicy(("persistent_notification",))
    await runtime.async_set_user_preferences("u1", persistent)
    next_reminder = await runtime.async_get(reminder.id)
    await runtime._async_process_due(next_reminder.due)
    assert dispatcher.calls[-1][1] == persistent


async def test_one_shot_and_recurring_share_single_earliest_scheduler(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    recurring_anchor = datetime.now().replace(tzinfo=None) + timedelta(days=3)
    recurring = await runtime.async_create_recurring(
        user_id="u1", title="Recurring", recurrence=daily(recurring_anchor)
    )
    one_shot = await runtime.async_create(
        user_id="u1",
        title="One shot",
        due=datetime.now(UTC) + timedelta(days=1),
    )
    assert runtime.scheduled_for == one_shot.due
    assert runtime.scheduled_for < recurring.due


async def test_simultaneous_one_shot_and_recurring_are_claimed_together(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    due = now - timedelta(minutes=1)
    recurrence = daily((now - timedelta(days=2)).replace(tzinfo=None))
    recurring = Reminder(
        id="recurring",
        user_id="u1",
        title="Recurring",
        due=due,
        scheduled_due=due,
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
        recurrence=recurrence,
    )
    one_shot = Reminder(
        id="one-shot",
        user_id="u1",
        title="One shot",
        due=due,
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=1),
    )
    store = FakeStore(
        serialize_storage(
            {recurring.id: recurring, one_shot.id: one_shot},
            {"u1": UserPreferences()},
        )
    )
    dispatcher = FakeDispatcher()
    runtime = await manager(store, dispatcher, scheduler)
    assert len(dispatcher.calls) == 2
    assert {raw["status"] for raw in store.saved[0]["reminders"].values()} == {
        "delivering"
    }
    assert (await runtime.async_get(recurring.id)).status is ReminderStatus.PENDING
    assert (await runtime.async_get(one_shot.id)).status is ReminderStatus.DELIVERED


async def test_recurrence_edit_reanchors_and_persists(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    reminder = await runtime.async_create_recurring(
        user_id="u1",
        title="Weekly",
        recurrence=RecurrenceRule(
            RecurrenceFrequency.WEEKLY,
            1,
            "Europe/Dublin",
            datetime(2027, 8, 2, 20),
            (Weekday.MONDAY,),
        ),
    )
    changed = RecurrenceRule(
        RecurrenceFrequency.WEEKLY,
        2,
        "Europe/Dublin",
        datetime(2027, 8, 3, 9),
        (Weekday.TUESDAY, Weekday.THURSDAY),
    )
    updated = await runtime.async_update(reminder.id, recurrence=changed)
    assert updated.recurrence == changed
    assert updated.scheduled_due == updated.due
    assert fake_store.saved[-1]["reminders"][reminder.id]["recurrence"] == (
        changed.to_dict()
    )
