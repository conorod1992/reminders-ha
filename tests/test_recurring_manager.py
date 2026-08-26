"""Manager integration tests for recurring reminders and durability."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.reminders.delivery import DeliveryResult
from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    DeliveryPolicy,
    MissedOccurrencePolicy,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
    TriggerDurationWait,
    UserPreferences,
)
from custom_components.reminders.recurrence import (
    RecurrenceFrequency,
    RecurrenceRule,
    Weekday,
)
from custom_components.reminders.storage import serialize_storage
from custom_components.reminders.triggers.models import TriggerDefinition

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


async def test_pause_survives_restart_and_resume_retains_anchor_phase(
    fake_store: FakeStore,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": datetime(2027, 1, 1, 10, tzinfo=UTC)}
    monkeypatch.setattr(
        "custom_components.reminders.manager.dt_util.utcnow",
        lambda: clock["now"],
    )
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    anchor = datetime(2027, 1, 3, 10)
    reminder = await runtime.async_create_recurring(
        user_id="u1", title="Daily", recurrence=daily(anchor)
    )
    occurrence_id = reminder.current_occurrence_id
    paused = await runtime.async_pause(reminder.id)
    assert paused.status is ReminderStatus.PAUSED
    assert paused.due is None
    assert paused.recurrence == reminder.recurrence
    assert paused.current_occurrence_id == occurrence_id
    assert paused.occurrence_history[-1].status is OccurrenceStatus.SCHEDULED
    assert len(paused.occurrence_history) == 1

    restarted_dispatcher = FakeDispatcher()
    restarted = await manager(fake_store, restarted_dispatcher, scheduler)
    restored = await restarted.async_get(reminder.id)
    assert restored.paused is True
    assert restored.current_occurrence_id == occurrence_id
    assert not restarted_dispatcher.calls

    clock["now"] = datetime(2027, 1, 2, 10, tzinfo=UTC)
    resumed = await restarted.async_resume(reminder.id)
    assert resumed.paused is False
    assert resumed.recurrence == reminder.recurrence
    assert resumed.due == reminder.due
    assert resumed.current_occurrence_id == occurrence_id
    assert len(resumed.occurrence_history) == 1


async def test_resume_after_preserved_due_skips_to_next_future_anchor(
    fake_store: FakeStore,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": datetime(2027, 1, 1, 10, tzinfo=UTC)}
    monkeypatch.setattr(
        "custom_components.reminders.manager.dt_util.utcnow",
        lambda: clock["now"],
    )
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    reminder = await runtime.async_create_recurring(
        user_id="u1",
        title="Daily",
        recurrence=daily(datetime(2027, 1, 3, 10)),
    )
    original_id = reminder.current_occurrence_id
    await runtime.async_pause(reminder.id)

    clock["now"] = datetime(2027, 1, 3, 11, tzinfo=UTC)
    resumed = await runtime.async_resume(reminder.id)

    original = next(
        item for item in resumed.occurrence_history if item.id == original_id
    )
    assert original.status is OccurrenceStatus.SKIPPED
    assert original.completion_reason == "paused_occurrence_missed"
    assert resumed.due == datetime(2027, 1, 4, 10, tzinfo=UTC)
    assert resumed.current_occurrence_id != original_id
    assert len(resumed.occurrence_history) == 2


async def test_skip_next_records_one_occurrence_and_survives_restart(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    anchor = datetime.now().replace(tzinfo=None) + timedelta(days=2)
    reminder = await runtime.async_create_recurring(
        user_id="u1", title="Daily", recurrence=daily(anchor)
    )
    skipped_due = reminder.scheduled_due
    skipped = await runtime.async_skip_next(reminder.id)
    assert skipped.occurrence_history[-2].status is OccurrenceStatus.SKIPPED
    assert skipped.occurrence_history[-2].scheduled_due == skipped_due
    assert skipped.due == skipped_due + timedelta(days=1)  # type: ignore[operator]

    restarted = await manager(fake_store, FakeDispatcher(), scheduler)
    restored = await restarted.async_get(reminder.id)
    assert restored.due == skipped.due
    assert (
        sum(
            item.status is OccurrenceStatus.SKIPPED
            for item in restored.occurrence_history
        )
        == 1
    )


async def test_offline_skip_policy_advances_without_delivery(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    recurrence = daily((now - timedelta(days=5)).replace(tzinfo=None))
    due = now - timedelta(days=2)
    occurrence = Occurrence("missed", due, due)
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Daily",
        due=due,
        scheduled_due=due,
        created_at=now - timedelta(days=5),
        updated_at=now - timedelta(days=5),
        recurrence=recurrence,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        missed_occurrence_policy=MissedOccurrencePolicy.SKIP,
    )
    dispatcher = FakeDispatcher()
    runtime = await manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        dispatcher,
        scheduler,
    )
    restored = await runtime.async_get(reminder.id)
    assert not dispatcher.calls
    assert restored.occurrence_history[0].status is OccurrenceStatus.SKIPPED
    assert restored.due > now  # type: ignore[operator]


async def test_context_wait_expiry_is_durable_and_series_advances(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    recurrence = daily((now - timedelta(days=2)).replace(tzinfo=None))
    due = now - timedelta(minutes=2)
    occurrence = Occurrence("waiting", due, due)
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="At home",
        due=due,
        scheduled_due=due,
        created_at=now - timedelta(days=2),
        updated_at=due,
        recurrence=recurrence,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        deliver_when=TriggerDefinition.from_dict(
            {"type": "named", "trigger_id": "home"}
        ),
        expires_after_seconds=60,
    )
    dispatcher = FakeDispatcher()
    runtime = await manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        dispatcher,
        scheduler,
    )
    advanced = await runtime.async_get(reminder.id)
    assert not dispatcher.calls
    assert advanced.occurrence_history[0].status is OccurrenceStatus.EXPIRED
    assert advanced.last_occurrence_status is ReminderStatus.EXPIRED
    assert advanced.due > now  # type: ignore[operator]


async def test_context_can_activate_before_expiry(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    reminder = await runtime.async_create(
        user_id="u1",
        title="At home",
        due=datetime.now(UTC) - timedelta(seconds=1),
        deliver_when={"type": "named", "trigger_id": "home"},
        expires_after_seconds=3600,
    )
    waiting = await runtime.async_get(reminder.id)
    assert waiting.status is ReminderStatus.WAITING_FOR_CONTEXT
    assert waiting.occurrence_history[-1].expires_at is not None
    assert (
        await runtime.async_activate_delivery_context(
            reminder.id, cause="named", context={}
        )
        == "activated"
    )


async def test_expiry_window_edit_to_past_expires_waiting_occurrence(
    fake_store: FakeStore,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    events: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        runtime,
        "_fire_lifecycle_event",
        lambda _reminder, action, **data: events.append(
            (action, data.get("occurrence_id"))
        ),
    )
    due = datetime.now(UTC) - timedelta(minutes=2)
    reminder = await runtime.async_create(
        user_id="u1",
        title="At home",
        due=due,
        deliver_when={"type": "named", "trigger_id": "home"},
        expires_after_seconds=3600,
    )

    expired = await runtime.async_update(reminder.id, expires_after_seconds=60)

    occurrence = expired.occurrence_history[-1]
    assert expired.status is ReminderStatus.EXPIRED
    assert occurrence.status is OccurrenceStatus.EXPIRED
    assert occurrence.completion_reason == "context_wait_expired"
    assert occurrence.expires_at == due + timedelta(seconds=60)
    await asyncio.gather(
        runtime._async_process_occurrence_expiry(datetime.now(UTC)),
        runtime._async_process_occurrence_expiry(datetime.now(UTC)),
    )
    assert events == [("expired", occurrence.id)]


async def test_future_expiry_window_edit_reschedules_exact_deadline(
    fake_store: FakeStore, scheduler: Scheduler
) -> None:
    runtime = await manager(fake_store, FakeDispatcher(), scheduler)
    due = datetime.now(UTC) - timedelta(seconds=1)
    reminder = await runtime.async_create(
        user_id="u1",
        title="At home",
        due=due,
        deliver_when={"type": "named", "trigger_id": "home"},
        expires_after_seconds=3600,
    )

    waiting = await runtime.async_update(reminder.id, expires_after_seconds=7200)

    expected = due + timedelta(seconds=7200)
    assert waiting.status is ReminderStatus.WAITING_FOR_CONTEXT
    assert waiting.occurrence_history[-1].expires_at == expected
    assert scheduler.calls[-1] == expected


async def test_recurring_expiry_window_edit_advances_from_anchor(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    due = now - timedelta(minutes=2)
    occurrence = Occurrence(
        "waiting",
        due,
        due,
        status=OccurrenceStatus.WAITING_FOR_CONTEXT,
        expires_at=due + timedelta(hours=1),
    )
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="At home",
        due=due,
        scheduled_due=due,
        created_at=now - timedelta(days=2),
        updated_at=due,
        recurrence=daily(
            due.astimezone(ZoneInfo("Europe/Dublin")).replace(tzinfo=None)
        ),
        status=ReminderStatus.WAITING_FOR_CONTEXT,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        deliver_when=TriggerDefinition.from_dict(
            {"type": "named", "trigger_id": "home"}
        ),
        trigger_duration_waits=(TriggerDurationWait("deliver_when", due, "named", {}),),
        expires_after_seconds=3600,
    )
    runtime = await manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        FakeDispatcher(),
        scheduler,
    )

    advanced = await runtime.async_update(reminder.id, expires_after_seconds=60)

    assert advanced.occurrence_history[0].status is OccurrenceStatus.EXPIRED
    assert advanced.last_occurrence_due == due
    assert advanced.last_occurrence_status is ReminderStatus.EXPIRED
    assert advanced.due == due + timedelta(days=1)
    assert not advanced.trigger_duration_waits


async def test_pausing_context_wait_abandons_it_safely(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    due = now - timedelta(minutes=1)
    occurrence = Occurrence(
        "waiting",
        due,
        due,
        status=OccurrenceStatus.WAITING_FOR_CONTEXT,
        expires_at=now + timedelta(hours=1),
    )
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="At home",
        due=due,
        scheduled_due=due,
        created_at=now - timedelta(days=1),
        updated_at=now,
        recurrence=daily(
            due.astimezone(ZoneInfo("Europe/Dublin")).replace(tzinfo=None)
        ),
        status=ReminderStatus.WAITING_FOR_CONTEXT,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        deliver_when=TriggerDefinition.from_dict(
            {"type": "named", "trigger_id": "home"}
        ),
        expires_after_seconds=3600,
    )
    runtime = await manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        FakeDispatcher(),
        scheduler,
    )

    paused = await runtime.async_pause(reminder.id)

    assert paused.current_occurrence_id is None
    assert paused.occurrence_history[0].status is OccurrenceStatus.CANCELLED
    assert paused.occurrence_history[0].completion_reason == "series_paused"


async def test_pause_holds_independent_snoozed_retry_until_resume(
    scheduler: Scheduler,
) -> None:
    now = datetime.now(UTC)
    recurrence = daily((now + timedelta(days=2)).replace(tzinfo=None))
    retry = Occurrence(
        "retry",
        now - timedelta(hours=2),
        now - timedelta(hours=1),
        snoozed=True,
    )
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Daily",
        due=None,
        created_at=now - timedelta(days=1),
        updated_at=now,
        recurrence=recurrence,
        status=ReminderStatus.PAUSED,
        paused=True,
        paused_at=now,
        occurrence_history=(retry,),
    )
    dispatcher = FakeDispatcher()
    runtime = await manager(
        FakeStore(serialize_storage({reminder.id: reminder}, {})),
        dispatcher,
        scheduler,
    )
    assert not dispatcher.calls

    resumed = await runtime.async_resume(reminder.id)
    assert len(dispatcher.calls) == 1
    restored_retry = next(
        item for item in resumed.occurrence_history if item.id == retry.id
    )
    assert restored_retry.status is OccurrenceStatus.DELIVERED
