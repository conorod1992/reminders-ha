"""Timezone and DST behavior tests."""

from datetime import UTC, datetime
from types import SimpleNamespace

from custom_components.reminders.services import _parse_datetime


def test_local_datetime_uses_home_assistant_timezone_across_dst() -> None:
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Dublin"))
    winter = _parse_datetime(hass, "2026-01-12 20:00:00")
    summer = _parse_datetime(hass, "2026-07-27 20:00:00")
    assert winter == datetime(2026, 1, 12, 20, tzinfo=UTC)
    assert summer == datetime(2026, 7, 27, 19, tzinfo=UTC)


def test_aware_datetime_preserves_instant() -> None:
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Dublin"))
    assert _parse_datetime(hass, "2026-07-27T20:00:00+01:00") == datetime(
        2026, 7, 27, 19, tzinfo=UTC
    )
