"""Crash-recovery tests for durable escalation delivery claims."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    EscalationAttempt,
    EscalationPolicy,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.storage import deserialize_storage, serialize_storage

from .conftest import FakeDispatcher, FakeStore


class SimulatedCrash(BaseException):
    """Represent process interruption beyond normal exception handling."""


class CrashDispatcher(FakeDispatcher):
    async def async_deliver(self, reminder: Any, policy: Any) -> Any:
        self.calls.append((reminder, policy))
        raise SimulatedCrash


class FailResultStore(FakeStore):
    """Persist the claim, then simulate interruption while saving its result."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.save_calls = 0

    async def async_save(self, data: dict[str, Any]) -> None:
        self.save_calls += 1
        if self.save_calls == 2:
            raise SimulatedCrash
        await super().async_save(data)


def _hass() -> Any:
    return SimpleNamespace(
        states={},
        bus=SimpleNamespace(),
        config=SimpleNamespace(time_zone="UTC"),
    )


def _awaiting_reminder(now: datetime) -> Reminder:
    occurrence = Occurrence(
        id="occurrence",
        scheduled_due=now - timedelta(hours=1),
        due=now - timedelta(hours=1),
        status=OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
        delivered_at=now - timedelta(hours=1),
        acknowledgement_required=True,
        next_escalation_at=now - timedelta(minutes=1),
    )
    return Reminder(
        id="reminder",
        user_id="u1",
        title="Escalate me",
        due=None,
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
        status=ReminderStatus.AWAITING_ACKNOWLEDGEMENT,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        escalation=EscalationPolicy(1, 5, 2),
    )


@pytest.fixture
def no_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _when: lambda: None,
    )


def _raw_attempt(store: FakeStore) -> dict[str, Any]:
    assert store.data is not None
    return store.data["reminders"]["reminder"]["occurrence_history"][0][
        "escalation_history"
    ][0]


async def test_interrupted_escalation_before_provider_call_retries_on_restart(
    no_timer: None,
) -> None:
    now = datetime.now(UTC)
    reminder = _awaiting_reminder(now)
    store = FakeStore(serialize_storage({reminder.id: reminder}, {}))
    crashed = ReminderManager(_hass(), store, CrashDispatcher())  # type: ignore[arg-type]

    with pytest.raises(SimulatedCrash):
        await crashed.async_load()

    raw_attempt = _raw_attempt(store)
    assert raw_attempt["number"] == 1
    assert raw_attempt["in_flight"] is True
    assert raw_attempt["succeeded_channels"] == []

    dispatcher = FakeDispatcher()
    recovered = ReminderManager(_hass(), store, dispatcher)  # type: ignore[arg-type]
    await recovered.async_load()

    assert len(dispatcher.calls) == 1
    occurrence = (await recovered.async_get(reminder.id)).occurrence_history[0]
    assert occurrence.escalation_attempt_count == 1
    assert len(occurrence.escalation_history) == 1
    assert occurrence.escalation_history[0].number == 1
    assert occurrence.escalation_history[0].in_flight is False
    assert occurrence.escalation_history[0].succeeded_channels
    assert occurrence.next_escalation_at is not None
    assert occurrence.next_escalation_at > now


async def test_interrupted_escalation_result_persistence_retries_conservatively(
    no_timer: None,
) -> None:
    now = datetime.now(UTC)
    reminder = _awaiting_reminder(now)
    store = FailResultStore(serialize_storage({reminder.id: reminder}, {}))
    first_dispatcher = FakeDispatcher()
    crashed = ReminderManager(_hass(), store, first_dispatcher)  # type: ignore[arg-type]

    with pytest.raises(SimulatedCrash):
        await crashed.async_load()

    assert len(first_dispatcher.calls) == 1
    assert _raw_attempt(store)["in_flight"] is True

    dispatcher = FakeDispatcher()
    recovered_store = FakeStore(store.data)
    recovered = ReminderManager(  # type: ignore[arg-type]
        _hass(), recovered_store, dispatcher
    )
    await recovered.async_load()

    assert len(dispatcher.calls) == 1
    occurrence = (await recovered.async_get(reminder.id)).occurrence_history[0]
    assert occurrence.escalation_attempt_count == 1
    assert len(occurrence.escalation_history) == 1
    assert occurrence.escalation_history[0].number == 1
    assert occurrence.escalation_history[0].in_flight is False
    assert occurrence.escalation_history[0].succeeded_channels


def test_deserialize_rolls_back_only_unfinished_awaiting_claims() -> None:
    now = datetime.now(UTC)
    reminder = _awaiting_reminder(now)
    occurrence = reminder.occurrence_history[0].updated(
        escalation_attempt_count=2,
        escalation_history=(
            EscalationAttempt(
                1, now - timedelta(minutes=6), succeeded_channels=("phone",)
            ),
            EscalationAttempt(2, now - timedelta(minutes=1), in_flight=True),
        ),
        next_escalation_at=now - timedelta(minutes=1),
    )
    reminder = reminder.updated(occurrence_history=(occurrence,))

    restored, _ = deserialize_storage(serialize_storage({reminder.id: reminder}, {}))
    recovered = restored[reminder.id].occurrence_history[0]

    assert recovered.escalation_attempt_count == 1
    assert [attempt.number for attempt in recovered.escalation_history] == [1]
    assert recovered.next_escalation_at == now - timedelta(minutes=1)
