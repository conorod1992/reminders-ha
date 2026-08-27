"""Focused tests for the derived Needs attention view."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from custom_components.reminders.models import (
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.websocket_registration import _attention_reason


def _reminder(**changes: object) -> Reminder:
    now = datetime.now(UTC)
    reminder = Reminder(
        id="attention",
        user_id="user",
        title="Attention",
        due=now,
        created_at=now,
        updated_at=now,
    )
    return reminder.updated(**changes)


def test_failed_reminder_needs_attention() -> None:
    assert _attention_reason(_reminder(status=ReminderStatus.FAILED)) == "delivery_failed"


def test_recurring_series_keeps_recent_failed_occurrence_visible() -> None:
    assert (
        _attention_reason(
            _reminder(
                status=ReminderStatus.PENDING,
                last_occurrence_status=ReminderStatus.FAILED,
            )
        )
        == "recent_delivery_failed"
    )


def test_outstanding_acknowledgement_needs_attention_after_series_advances() -> None:
    now = datetime.now(UTC)
    occurrence = Occurrence(
        id="awaiting",
        due=now,
        scheduled_due=now,
        status=OccurrenceStatus.AWAITING_ACKNOWLEDGEMENT,
    )
    reminder = _reminder(
        status=ReminderStatus.PENDING,
        occurrence_history=(occurrence,),
    )
    assert _attention_reason(reminder) == "awaiting_acknowledgement"


def test_delivered_occurrence_only_needs_attention_when_actionable() -> None:
    now = datetime.now(UTC)
    occurrence = Occurrence(
        id="delivered",
        due=now,
        scheduled_due=now,
        status=OccurrenceStatus.DELIVERED,
    )
    passive = _reminder(
        status=ReminderStatus.PENDING,
        occurrence_history=(occurrence,),
    )
    actionable = passive.updated(allow_manual_completion=True)

    assert _attention_reason(passive) is None
    assert _attention_reason(actionable) == "action_available"


def test_attention_panel_is_additive_wrapper_over_native_panel() -> None:
    source = (
        Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "frontend"
        / "reminders-panel-attention.js"
    ).read_text(encoding="utf-8")

    assert 'import "./reminders-panel-native.js"' in source
    assert 'button.textContent = "Needs attention"' in source
    assert 'this._view = "attention"' in source
    assert 'Nothing needs your attention' in source
