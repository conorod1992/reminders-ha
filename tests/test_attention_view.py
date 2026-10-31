"""Focused tests for the derived Needs attention view."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.reminders import websocket_registration as registration
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
    assert (
        _attention_reason(_reminder(status=ReminderStatus.FAILED)) == "delivery_failed"
    )


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
    polish = (
        Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "frontend"
        / "reminders-panel-polish.js"
    ).read_text(encoding="utf-8")

    assert '["attention", "Needs attention"]' in polish
    assert "proto._renderFilters = function ()" not in source
    panel = (
        Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "frontend"
        / "reminders-panel.js"
    ).read_text(encoding="utf-8")
    assert "Nothing needs your attention" in panel
    assert "proto._renderList = function ()" not in source


async def test_native_listener_failure_appears_in_needs_attention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reminder with an unavailable automation must be discoverable."""
    broken = _reminder(
        id="broken",
        status=ReminderStatus.WAITING_FOR_TRIGGER,
    )

    class FakeManager:
        def native_failure_roles(self, reminder_id: str) -> tuple[str, ...]:
            return ("activation",) if reminder_id == broken.id else ()

        async def async_list_page(self, **kwargs: Any) -> tuple[list[Reminder], int]:
            assert kwargs["user_id"] == "user"
            return [broken], 1

    monkeypatch.setattr(registration, "_manager", lambda _hass: FakeManager())
    matches, total = await registration._attention_page(
        SimpleNamespace(),
        user_id="user",
        query=None,
        due_after=None,
        due_before=None,
        source=None,
        source_id=None,
        limit=50,
        offset=0,
    )
    assert matches == [broken]
    assert total == 1
