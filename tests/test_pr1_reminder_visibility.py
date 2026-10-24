"""Regression tests for scheduled task actions and upcoming context waits."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders.models import (
    ActivationType,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders import websocket_registration as registration


def _timed_reminder(
    *,
    status: ReminderStatus = ReminderStatus.PENDING,
    occurrence_status: OccurrenceStatus = OccurrenceStatus.SCHEDULED,
    manual: bool = True,
    paused: bool = False,
) -> Reminder:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    occurrence = Occurrence(
        id="occurrence-1",
        due=now,
        scheduled_due=now,
        status=occurrence_status,
    )
    return Reminder(
        id="reminder-1",
        user_id="owner-1",
        title="Take the bins out",
        due=now,
        created_at=now,
        updated_at=now,
        activation_type=ActivationType.TIME,
        status=status,
        paused=paused,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        allow_manual_completion=manual,
    )


@pytest.mark.parametrize(
    ("status", "occurrence_status", "manual", "paused", "included"),
    [
        (ReminderStatus.PENDING, OccurrenceStatus.SCHEDULED, True, False, True),
        (ReminderStatus.PENDING, OccurrenceStatus.SCHEDULED, False, False, False),
        (ReminderStatus.PAUSED, OccurrenceStatus.SCHEDULED, True, True, False),
        (ReminderStatus.COMPLETED, OccurrenceStatus.COMPLETED, True, False, False),
        (
            ReminderStatus.AWAITING_ACKNOWLEDGEMENT,
            OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
            True,
            False,
            True,
        ),
    ],
)
def test_list_summary_exposes_only_actionable_occurrences(
    status: ReminderStatus,
    occurrence_status: OccurrenceStatus,
    manual: bool,
    paused: bool,
    included: bool,
) -> None:
    reminder = _timed_reminder(
        status=status,
        occurrence_status=occurrence_status,
        manual=manual,
        paused=paused,
    )
    summary = registration._serialize_summary(reminder, {})
    occurrences = summary["occurrence_history"]
    assert (len(occurrences) == 1) is included
    if included:
        assert occurrences[0]["id"] == reminder.current_occurrence_id


@pytest.mark.asyncio
async def test_upcoming_includes_reminders_waiting_for_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = _timed_reminder(
        status=ReminderStatus.WAITING_FOR_CONTEXT,
        occurrence_status=OccurrenceStatus.WAITING_FOR_CONTEXT,
    )
    observed_statuses: set[ReminderStatus] | None = None

    class StubManager:
        async def async_list_page(self, **kwargs: Any) -> tuple[list[Reminder], int]:
            nonlocal observed_statuses
            observed_statuses = kwargs["statuses"]
            matching = [waiting] if waiting.status in observed_statuses else []
            return matching, len(matching)

    monkeypatch.setattr(
        registration, "_manager", lambda _hass: StubManager()
    )
    page, total = await registration._upcoming_page(
        SimpleNamespace(),
        user_id="owner-1",
        query=None,
        due_after=None,
        due_before=None,
        source=None,
        source_id=None,
        limit=50,
        offset=0,
    )
    assert observed_statuses is not None
    assert ReminderStatus.WAITING_FOR_CONTEXT in observed_statuses
    assert page == [waiting]
    assert total == 1
