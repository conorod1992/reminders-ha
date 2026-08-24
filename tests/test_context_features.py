"""Context, escalation, and mobile-action lifecycle tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import State

from custom_components.reminders.const import MOBILE_ACTION_PREFIX
from custom_components.reminders.delivery import DeliveryResult
from custom_components.reminders.manager import (
    ReminderManager,
    _automatic_completion_occurrence,
)
from custom_components.reminders.models import (
    AcknowledgementPolicy,
    DeliveryPolicy,
    EscalationPolicy,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.recurrence import RecurrenceFrequency, RecurrenceRule

from .conftest import FakeDispatcher, FakeStore


class Bus:
    """Small event-bus listener test double."""

    def __init__(self) -> None:
        self.listeners: dict[str, Any] = {}

    def async_listen(self, event_type: str, callback: Any) -> Any:
        self.listeners[event_type] = callback
        return lambda: self.listeners.pop(event_type, None)


@pytest.fixture
def no_runtime_listeners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.triggers.registry.async_track_state_change_event",
        lambda _hass, _entities, _callback: lambda: None,
    )
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _due: lambda: None,
    )


async def _manager(
    store: FakeStore,
    dispatcher: FakeDispatcher,
    states: dict[str, State] | None = None,
) -> ReminderManager:
    hass = SimpleNamespace(
        states=states or {},
        bus=Bus(),
        config=SimpleNamespace(time_zone="UTC"),
        create_task=lambda coroutine, _name=None: asyncio.create_task(coroutine),
    )
    manager = ReminderManager(hass, store, dispatcher)  # type: ignore[arg-type]
    await manager.async_load()
    return manager


async def _recurring_snoozed_retry(
    store: FakeStore, dispatcher: FakeDispatcher
) -> tuple[ReminderManager, str, str, tuple[Any, ...]]:
    """Create A as a scheduled retry while B remains the current occurrence."""
    manager = await _manager(store, dispatcher)
    anchor = (datetime.now(UTC) + timedelta(minutes=1)).replace(tzinfo=None)
    reminder = await manager.async_create_recurring(
        user_id="u1",
        title="Complete the retry",
        recurrence=RecurrenceRule(RecurrenceFrequency.DAILY, 1, "UTC", anchor),
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        escalation=EscalationPolicy(5, 5, 2),
        complete_when={"type": "named", "trigger_id": "task_detected"},
    )
    await manager._async_process_due(reminder.due)  # type: ignore[arg-type]
    snooze_action = next(
        action["action"]
        for action in dispatcher.calls[-1][0].notification_actions
        if action["title"] == "Snooze 1 hour"
    )
    await manager._async_handle_mobile_action(snooze_action)
    snoozed = await manager.async_get(reminder.id)
    retry = next(
        occurrence
        for occurrence in snoozed.occurrence_history
        if occurrence.id != snoozed.current_occurrence_id
        and occurrence.snoozed
        and occurrence.status is OccurrenceStatus.SCHEDULED
    )
    return manager, reminder.id, retry.id, _recurrence_snapshot(snoozed)


def _recurrence_snapshot(reminder: Reminder) -> tuple[Any, ...]:
    current = next(
        occurrence
        for occurrence in reminder.occurrence_history
        if occurrence.id == reminder.current_occurrence_id
    )
    return (
        reminder.current_occurrence_id,
        reminder.due,
        reminder.scheduled_due,
        reminder.status,
        reminder.current_occurrence_number,
        current.status,
        current.next_escalation_at,
        current.context_eligible_at,
        tuple(occurrence.id for occurrence in reminder.occurrence_history),
        reminder.last_occurrence_due,
        reminder.last_occurrence_status,
        reminder.recurrence,
    )


async def test_due_context_current_match_delivers_immediately(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(
        fake_store,
        dispatcher,
        {"person.conor": State("person.conor", "home")},
    )
    reminder = await manager.async_create(
        user_id="u1",
        title="Parcel",
        due=datetime.now(UTC) - timedelta(seconds=1),
        deliver_when={"type": "state", "entity_id": "person.conor", "to": "home"},
    )
    assert (await manager.async_get(reminder.id)).status is ReminderStatus.DELIVERED
    assert len(dispatcher.calls) == 1


async def test_context_waits_then_delivers_once(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(
        fake_store,
        dispatcher,
        {"person.conor": State("person.conor", "not_home")},
    )
    reminder = await manager.async_create(
        user_id="u1",
        title="Parcel",
        due=datetime.now(UTC) - timedelta(seconds=1),
        deliver_when={"type": "state", "entity_id": "person.conor", "to": "home"},
    )
    waiting = await manager.async_get(reminder.id)
    assert waiting.status is ReminderStatus.WAITING_FOR_CONTEXT
    occurrence = waiting.occurrence_history[-1]
    assert occurrence.status is OccurrenceStatus.WAITING_FOR_CONTEXT
    assert occurrence.context_eligible_at is not None
    assert not dispatcher.calls

    assert (
        await manager.async_activate_delivery_context(
            reminder.id, cause="future_transition", context={"to": "home"}
        )
        == "activated"
    )
    assert (
        await manager.async_activate_delivery_context(
            reminder.id, cause="duplicate", context={}
        )
        == "inactive"
    )
    assert len(dispatcher.calls) == 1


async def test_completion_before_due_and_while_waiting(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    future = datetime.now(UTC) + timedelta(hours=1)
    reminder = await manager.async_create(
        user_id="u1",
        title="Brush teeth",
        due=future,
        complete_when={"type": "event", "event_type": "brushing_started"},
    )
    assert (
        await manager.async_complete_automatically(
            reminder.id, cause="home_assistant_event", context={}
        )
        == "completed"
    )
    completed = await manager.async_get(reminder.id)
    occurrence = completed.occurrence_history[-1]
    assert completed.status is ReminderStatus.COMPLETED
    assert occurrence.status is OccurrenceStatus.CANCELLED
    assert occurrence.completion_source == "automatic"
    await manager._async_process_due(future)
    assert not dispatcher.calls

    waiting = await manager.async_create(
        user_id="u1",
        title="Second task",
        due=datetime.now(UTC) - timedelta(seconds=1),
        deliver_when={"type": "event", "event_type": "arrived"},
        complete_when={"type": "event", "event_type": "finished"},
    )
    assert (
        await manager.async_get(waiting.id)
    ).status is ReminderStatus.WAITING_FOR_CONTEXT
    await manager.async_complete_automatically(
        waiting.id, cause="home_assistant_event", context={}
    )
    assert (await manager.async_get(waiting.id)).status is ReminderStatus.COMPLETED
    assert not dispatcher.calls


async def test_escalation_attempts_stop_on_done(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    reminder = await manager.async_create(
        user_id="u1",
        title="Door",
        due=datetime.now(UTC) - timedelta(seconds=1),
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        escalation=EscalationPolicy(1, 2, 2),
        source="expiry_tracker",
        source_id="front-door",
        managed_externally=True,
    )
    delivered = await manager.async_get(reminder.id)
    occurrence = delivered.occurrence_history[-1]
    assert occurrence.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
    assert occurrence.next_escalation_at is not None

    await manager._async_process_due(occurrence.next_escalation_at)
    escalated = await manager.async_get(reminder.id)
    occurrence = escalated.occurrence_history[-1]
    assert occurrence.escalation_attempt_count == 1
    assert occurrence.escalation_history[0].succeeded_channels
    assert len(dispatcher.calls) == 2
    assert escalated.managed_externally is True
    assert escalated.source == "expiry_tracker"

    await manager.async_acknowledge(reminder.id, occurrence_id=occurrence.id)
    acknowledged = await manager.async_get(reminder.id)
    occurrence = acknowledged.occurrence_history[-1]
    assert occurrence.next_escalation_at is None
    await manager._async_process_due(datetime.now(UTC) + timedelta(days=1))
    assert len(dispatcher.calls) == 2


async def test_mobile_done_snooze_and_forged_actions_are_occurrence_scoped(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    policy = DeliveryPolicy(("phone",), ("notify.conor",))
    reminder = await manager.async_create(
        user_id="u1",
        title="Action",
        due=datetime.now(UTC) - timedelta(seconds=1),
        delivery_policy=policy,
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        source="expiry_tracker",
        source_id="snooze-item",
        managed_externally=True,
    )
    delivered_payload = dispatcher.calls[-1][0]
    done = next(
        item["action"]
        for item in delivered_payload.notification_actions
        if item["title"] == "Done"
    )
    assert reminder.id not in done
    assert "u1" not in done
    await manager._async_handle_mobile_action("REMINDERS_forged:DONE")
    await manager._async_handle_mobile_action(done)
    await manager._async_handle_mobile_action(done)
    acknowledged = await manager.async_get(reminder.id)
    assert acknowledged.occurrence_history[-1].status is OccurrenceStatus.ACKNOWLEDGED
    assert acknowledged.occurrence_history[-1].completion_source == "mobile_action"

    snooze_reminder = await manager.async_create(
        user_id="u2",
        title="Snooze",
        due=datetime.now(UTC) - timedelta(seconds=1),
        delivery_policy=policy,
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        source="expiry_tracker",
        source_id="snooze-item",
        managed_externally=True,
    )
    payload = dispatcher.calls[-1][0]
    snooze = next(
        item["action"]
        for item in payload.notification_actions
        if item["title"] == "Snooze 10 minutes"
    )
    await manager._async_handle_mobile_action(snooze)
    updated = await manager.async_get(snooze_reminder.id)
    assert updated.status is ReminderStatus.PENDING
    assert updated.occurrence_history[-1].snoozed
    assert updated.user_id == "u2"
    assert updated.managed_externally is True


async def test_event_context_before_due_is_ignored_and_waiting_survives_restart(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    due = datetime.now(UTC) + timedelta(minutes=5)
    reminder = await manager.async_create(
        user_id="u1",
        title="After work",
        due=due,
        deliver_when={"type": "event", "event_type": "work_finished"},
    )
    assert (
        await manager.async_activate_delivery_context(
            reminder.id, cause="home_assistant_event", context={}
        )
        == "inactive"
    )
    await manager._async_process_due(due)
    assert (
        await manager.async_get(reminder.id)
    ).status is ReminderStatus.WAITING_FOR_CONTEXT

    restarted_dispatcher = FakeDispatcher()
    restarted = await _manager(fake_store, restarted_dispatcher)
    restored = await restarted.async_get(reminder.id)
    assert restored.status is ReminderStatus.WAITING_FOR_CONTEXT
    assert not restarted_dispatcher.calls
    await restarted.async_activate_delivery_context(
        reminder.id, cause="home_assistant_event", context={}
    )
    assert len(restarted_dispatcher.calls) == 1


async def test_equivalent_contexts_share_listener_and_delete_releases_it(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    manager = await _manager(fake_store, FakeDispatcher())
    for title in ("One", "Two"):
        await manager.async_create(
            user_id="u1",
            title=title,
            due=datetime.now(UTC) - timedelta(seconds=1),
            deliver_when={"type": "event", "event_type": "arrived_home"},
        )
    reminders = await manager.async_list()
    assert manager.trigger_listener_count == 1
    await manager.async_delete(reminders[0].id)
    assert manager.trigger_listener_count == 1
    await manager.async_delete(reminders[1].id)
    assert manager.trigger_listener_count == 0


async def test_completion_after_delivery_cancels_escalation_without_user_claim(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    reminder = await manager.async_create(
        user_id="u1",
        title="Detected",
        due=datetime.now(UTC) - timedelta(seconds=1),
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        escalation=EscalationPolicy(5, 5, 3),
        complete_when={"type": "named", "trigger_id": "detected_done"},
    )
    await manager.async_complete_automatically(
        reminder.id, cause="named_trigger_service", context={}
    )
    completed = await manager.async_get(reminder.id)
    occurrence = completed.occurrence_history[-1]
    assert occurrence.status is OccurrenceStatus.ACKNOWLEDGED
    assert occurrence.acknowledged_by is None
    assert occurrence.completion_source == "automatic"
    assert occurrence.next_escalation_at is None


async def test_escalation_max_attempts_and_restart_claim_are_bounded(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher(succeeds=False)
    manager = await _manager(fake_store, dispatcher)
    # The initial delivery must succeed before escalation can begin.
    dispatcher.succeeds = True
    reminder = await manager.async_create(
        user_id="u1",
        title="Escalate",
        due=datetime.now(UTC) - timedelta(seconds=1),
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        escalation=EscalationPolicy(1, 1, 2),
    )
    dispatcher.succeeds = False
    occurrence = (await manager.async_get(reminder.id)).occurrence_history[-1]
    assert occurrence.next_escalation_at is not None
    await manager._async_process_due(occurrence.next_escalation_at)
    occurrence = (await manager.async_get(reminder.id)).occurrence_history[-1]
    assert occurrence.next_escalation_at is not None

    restarted_dispatcher = FakeDispatcher(succeeds=False)
    restarted = await _manager(fake_store, restarted_dispatcher)
    occurrence = (await restarted.async_get(reminder.id)).occurrence_history[-1]
    await restarted._async_process_due(occurrence.next_escalation_at)  # type: ignore[arg-type]
    occurrence = (await restarted.async_get(reminder.id)).occurrence_history[-1]
    assert occurrence.escalation_attempt_count == 2
    assert occurrence.next_escalation_at is None
    assert len(occurrence.escalation_history) == 2
    await restarted._async_process_due(datetime.now(UTC) + timedelta(days=1))
    assert len(restarted_dispatcher.calls) == 1


async def test_non_acknowledged_delivery_omits_done_but_keeps_snooze(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    await manager.async_create(
        user_id="u1",
        title="No done",
        due=datetime.now(UTC) - timedelta(seconds=1),
        delivery_policy=DeliveryPolicy(("phone",), ("notify.phone",)),
        acknowledgement_policy=AcknowledgementPolicy.NOT_REQUIRED,
    )
    titles = {item["title"] for item in dispatcher.calls[-1][0].notification_actions}
    assert "Done" not in titles
    assert titles == {"Snooze 10 minutes", "Snooze 1 hour"}


async def test_recurring_automatic_completion_acknowledges_delivered_occurrence_only(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    anchor = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None)
    reminder = await manager.async_create_recurring(
        user_id="u1",
        title="Daily task",
        recurrence=RecurrenceRule(RecurrenceFrequency.DAILY, 1, "UTC", anchor),
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        complete_when={"type": "named", "trigger_id": "task_detected"},
    )
    assert reminder.due is not None
    await manager._async_process_due(reminder.due)
    advanced = await manager.async_get(reminder.id)
    next_occurrence_id = advanced.current_occurrence_id
    awaiting = next(
        item
        for item in advanced.occurrence_history
        if item.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
    )

    await manager.async_complete_automatically(
        reminder.id, cause="named_trigger_service", context={}
    )
    completed = await manager.async_get(reminder.id)
    assert completed.current_occurrence_id == next_occurrence_id
    assert completed.status is ReminderStatus.PENDING
    completed_old = next(
        item for item in completed.occurrence_history if item.id == awaiting.id
    )
    assert completed_old.status is OccurrenceStatus.ACKNOWLEDGED
    assert completed_old.completion_source == "automatic"


async def test_completion_cancels_scheduled_snoozed_retry_without_advancing_series(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    manager, reminder_id, retry_id, series_snapshot = await _recurring_snoozed_retry(
        fake_store, FakeDispatcher()
    )
    scheduled = next(
        item
        for item in (await manager.async_get(reminder_id)).occurrence_history
        if item.id == retry_id
    )
    assert scheduled.notification_action_token is not None

    assert (
        await manager.async_complete_automatically(
            reminder_id, cause="named_trigger_service", context={}
        )
        == "completed"
    )
    completed = await manager.async_get(reminder_id)
    retry = next(item for item in completed.occurrence_history if item.id == retry_id)
    assert retry.status is OccurrenceStatus.CANCELLED
    assert retry.completion_source == "automatic"
    assert retry.next_escalation_at is None
    assert _recurrence_snapshot(completed) == series_snapshot
    await manager._async_handle_mobile_action(
        f"{MOBILE_ACTION_PREFIX}{scheduled.notification_action_token}:SNOOZE_10"
    )
    assert await manager.async_get(reminder_id) == completed


async def test_completion_wins_snoozed_retry_delivery_race(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    class GateDispatcher(FakeDispatcher):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def async_deliver(self, reminder: Any, policy: Any) -> DeliveryResult:
            self.calls.append((reminder, policy))
            if len(self.calls) > 1:
                self.started.set()
                await self.release.wait()
            return DeliveryResult((policy.channels[0],), ())

    dispatcher = GateDispatcher()
    manager, reminder_id, retry_id, series_snapshot = await _recurring_snoozed_retry(
        fake_store, dispatcher
    )
    retry = next(
        item
        for item in (await manager.async_get(reminder_id)).occurrence_history
        if item.id == retry_id
    )
    delivery = asyncio.create_task(manager._async_process_due(retry.due))
    await dispatcher.started.wait()
    claimed = await manager.async_get(reminder_id)
    assert (
        next(item for item in claimed.occurrence_history if item.id == retry_id).status
        is OccurrenceStatus.DELIVERING
    )

    await manager.async_complete_automatically(
        reminder_id, cause="named_trigger_service", context={}
    )
    dispatcher.release.set()
    await delivery

    completed = await manager.async_get(reminder_id)
    retry = next(item for item in completed.occurrence_history if item.id == retry_id)
    assert retry.status is OccurrenceStatus.CANCELLED
    assert retry.completion_source == "automatic"
    assert _recurrence_snapshot(completed) == series_snapshot


async def test_completion_acknowledges_redelivered_snoozed_retry_only(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    dispatcher = FakeDispatcher()
    manager, reminder_id, retry_id, series_snapshot = await _recurring_snoozed_retry(
        fake_store, dispatcher
    )
    retry = next(
        item
        for item in (await manager.async_get(reminder_id)).occurrence_history
        if item.id == retry_id
    )
    await manager._async_process_due(retry.due)
    awaiting = next(
        item
        for item in (await manager.async_get(reminder_id)).occurrence_history
        if item.id == retry_id
    )
    assert awaiting.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
    assert awaiting.next_escalation_at is not None

    await manager.async_complete_automatically(
        reminder_id, cause="named_trigger_service", context={}
    )
    completed = await manager.async_get(reminder_id)
    retry = next(item for item in completed.occurrence_history if item.id == retry_id)
    assert retry.status is OccurrenceStatus.ACKNOWLEDGED
    assert retry.completion_source == "automatic"
    assert retry.next_escalation_at is None
    assert _recurrence_snapshot(completed) == series_snapshot


async def test_completion_after_restart_resolves_snoozed_retry_only(
    fake_store: FakeStore, no_runtime_listeners: None
) -> None:
    (
        _manager_instance,
        reminder_id,
        retry_id,
        series_snapshot,
    ) = await _recurring_snoozed_retry(fake_store, FakeDispatcher())
    restarted = await _manager(fake_store, FakeDispatcher())

    await restarted.async_complete_automatically(
        reminder_id, cause="named_trigger_service", context={}
    )
    completed = await restarted.async_get(reminder_id)
    retry = next(item for item in completed.occurrence_history if item.id == retry_id)
    assert retry.status is OccurrenceStatus.CANCELLED
    assert retry.completion_source == "automatic"
    assert _recurrence_snapshot(completed) == series_snapshot


def test_multiple_snoozed_retries_select_earliest_due_not_history_order() -> None:
    now = datetime.now(UTC)
    current = Occurrence("current", now + timedelta(days=1), now + timedelta(days=1))
    later = Occurrence(
        "later",
        now - timedelta(days=2),
        now + timedelta(hours=2),
        snoozed=True,
    )
    earlier = Occurrence(
        "earlier",
        now - timedelta(days=1),
        now + timedelta(hours=1),
        snoozed=True,
    )
    reminder = Reminder(
        id="series",
        user_id="u1",
        title="Ordered retries",
        due=current.due,
        created_at=now,
        updated_at=now,
        recurrence=RecurrenceRule(
            RecurrenceFrequency.DAILY,
            1,
            "UTC",
            now.replace(tzinfo=None),
        ),
        current_occurrence_id=current.id,
        occurrence_history=(later, current, earlier),
    )

    assert _automatic_completion_occurrence(reminder) == earlier


@pytest.mark.parametrize(
    ("snooze_title", "expected_delay"),
    [
        ("Snooze 10 minutes", timedelta(minutes=10)),
        ("Snooze 1 hour", timedelta(hours=1)),
    ],
)
async def test_recurring_mobile_snooze_keeps_next_occurrence_and_retries_exactly(
    fake_store: FakeStore,
    no_runtime_listeners: None,
    snooze_title: str,
    expected_delay: timedelta,
) -> None:
    dispatcher = FakeDispatcher()
    manager = await _manager(fake_store, dispatcher)
    anchor = (datetime.now(UTC) + timedelta(minutes=1)).replace(tzinfo=None)
    reminder = await manager.async_create_recurring(
        user_id="u1",
        title="Recurring action",
        recurrence=RecurrenceRule(RecurrenceFrequency.DAILY, 1, "UTC", anchor),
        acknowledgement_policy=AcknowledgementPolicy.REQUIRED,
        escalation=EscalationPolicy(5, 5, 2),
    )
    await manager._async_process_due(reminder.due)  # type: ignore[arg-type]
    advanced = await manager.async_get(reminder.id)
    next_snapshot = (
        advanced.current_occurrence_id,
        advanced.due,
        advanced.scheduled_due,
        advanced.status,
    )
    original_payload = dispatcher.calls[-1][0]
    snooze_action = next(
        action["action"]
        for action in original_payload.notification_actions
        if action["title"] == snooze_title
    )

    before_snooze = datetime.now(UTC)
    await manager._async_handle_mobile_action(snooze_action)
    snoozed = await manager.async_get(reminder.id)
    assert (
        snoozed.current_occurrence_id,
        snoozed.due,
        snoozed.scheduled_due,
        snoozed.status,
    ) == next_snapshot
    retry = next(
        occurrence
        for occurrence in snoozed.occurrence_history
        if occurrence.id != snoozed.current_occurrence_id
        and occurrence.status is OccurrenceStatus.SCHEDULED
    )
    assert before_snooze + expected_delay <= retry.due
    assert retry.due <= datetime.now(UTC) + expected_delay + timedelta(seconds=1)
    assert retry.redelivery_count == 1

    # The first notification token was replaced, so a repeated/stale action is inert.
    await manager._async_handle_mobile_action(snooze_action)
    assert (await manager.async_get(reminder.id)) == snoozed

    restarted_dispatcher = FakeDispatcher()
    restarted = await _manager(fake_store, restarted_dispatcher)
    restored = await restarted.async_get(reminder.id)
    assert restored.current_occurrence_id == next_snapshot[0]
    assert not restarted_dispatcher.calls

    await restarted._async_process_due(retry.due)
    redelivered = await restarted.async_get(reminder.id)
    assert (
        redelivered.current_occurrence_id,
        redelivered.due,
        redelivered.scheduled_due,
        redelivered.status,
    ) == next_snapshot
    retried = next(
        occurrence
        for occurrence in redelivered.occurrence_history
        if occurrence.id == retry.id
    )
    assert retried.status is OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT
    assert retried.next_escalation_at is not None
    assert len(restarted_dispatcher.calls) == 1

    done = next(
        action["action"]
        for action in restarted_dispatcher.calls[-1][0].notification_actions
        if action["title"] == "Done"
    )
    await restarted._async_handle_mobile_action(done)
    finished = await restarted.async_get(reminder.id)
    retried = next(
        occurrence
        for occurrence in finished.occurrence_history
        if occurrence.id == retry.id
    )
    assert retried.status is OccurrenceStatus.ACKNOWLEDGED
    assert retried.next_escalation_at is None
    assert finished.current_occurrence_id == next_snapshot[0]
