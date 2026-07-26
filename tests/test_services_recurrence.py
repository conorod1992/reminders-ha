"""Tests for recurring-reminder action parsing."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.core import HomeAssistant

from custom_components.reminders.recurrence import RecurrenceError, Weekday
from custom_components.reminders.services import _recurrence_from_data


def _hass(timezone: str = "Europe/Dublin") -> HomeAssistant:
    return cast(
        HomeAssistant, SimpleNamespace(config=SimpleNamespace(time_zone=timezone))
    )


def test_weekly_action_defaults_to_anchor_weekday_and_ha_timezone() -> None:
    rule = _recurrence_from_data(
        _hass(),
        {
            "first_reminder": datetime(2026, 7, 27, 9),
            "frequency": "weekly",
            "interval": 3,
        },
    )

    assert rule.timezone == "Europe/Dublin"
    assert rule.weekdays == (Weekday.MONDAY,)
    assert rule.interval == 3


@pytest.mark.parametrize(
    "data",
    [
        {
            "first_reminder": datetime(2026, 7, 27, 9),
            "frequency": "daily",
            "weekdays": ["monday"],
        },
        {
            "first_reminder": datetime(2026, 7, 27, 9),
            "frequency": "weekly",
            "day_of_month": 27,
        },
        {
            "first_reminder": datetime(2026, 7, 27, 9),
            "frequency": "monthly",
            "weekdays": ["monday"],
        },
    ],
)
def test_action_rejects_fields_from_another_frequency(data: dict[str, Any]) -> None:
    with pytest.raises(RecurrenceError):
        _recurrence_from_data(_hass(), data)


def test_weekly_action_rejects_anchor_outside_selected_weekdays() -> None:
    with pytest.raises(RecurrenceError, match="First reminder weekday"):
        _recurrence_from_data(
            _hass(),
            {
                "first_reminder": datetime(2026, 7, 27, 9),
                "frequency": "weekly",
                "weekdays": ["friday"],
            },
        )
