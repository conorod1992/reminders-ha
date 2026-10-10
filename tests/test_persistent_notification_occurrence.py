"""Persistent notification occurrence identity regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.reminders.delivery import _delivery_occurrence_id
from custom_components.reminders.models import Occurrence, OccurrenceStatus, Reminder


def test_snoozed_retry_uses_delivering_occurrence_not_series_pointer() -> None:
    now = datetime.now(UTC)
    retry = Occurrence(
        id="retry",
        scheduled_due=now - timedelta(hours=1),
        due=now,
        status=OccurrenceStatus.DELIVERING,
        snoozed=True,
    )
    current = Occurrence(
        id="current",
        scheduled_due=now + timedelta(days=1),
        due=now + timedelta(days=1),
        status=OccurrenceStatus.SCHEDULED,
    )
    reminder = Reminder(
        id="series",
        user_id="user",
        title="Series",
        due=current.due,
        scheduled_due=current.scheduled_due,
        created_at=now,
        updated_at=now,
        current_occurrence_id=current.id,
        occurrence_history=(retry, current),
    )

    assert _delivery_occurrence_id(reminder) == retry.id
