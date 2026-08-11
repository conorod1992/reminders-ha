"""Context, escalation, and mobile-action lifecycle tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import State

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    AcknowledgementPolicy,
    DeliveryPolicy,
    EscalationPolicy,
    OccurrenceStatus,
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
