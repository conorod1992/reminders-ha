"""Authorization and ambiguity tests for structured conversation tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from custom_components.reminders.const import DOMAIN
from custom_components.reminders.conversation import (
    _list,
    _pause_series,
    _resolve_reminder,
    _resume_series,
    _skip_next_series,
    _tools,
    _update,
)
from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.recurrence import RecurrenceFrequency, RecurrenceRule

from .conftest import FakeDispatcher, FakeStore


class Scheduler:
    def schedule(self, hass: Any, callback: Any, due: datetime) -> Any:
        return lambda: None


class Auth:
    def __init__(self) -> None:
        self.users = {
            "u1": SimpleNamespace(id="u1", is_admin=False),
            "u2": SimpleNamespace(id="u2", is_admin=False),
            "admin": SimpleNamespace(id="admin", is_admin=True),
        }

    async def async_get_user(self, user_id: str) -> Any:
        return self.users.get(user_id)


def context(user_id: str) -> llm.LLMContext:
    return llm.LLMContext(
        platform="test",
        context=Context(user_id=user_id),
        language="en",
        assistant=None,
        device_id=None,
    )


@pytest.fixture
async def runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ReminderManager]:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        Scheduler().schedule,
    )
    hass = SimpleNamespace(
        data={},
        auth=Auth(),
        config=SimpleNamespace(time_zone="UTC"),
    )
    manager = ReminderManager(  # type: ignore[arg-type]
        hass, FakeStore({"reminders": {}, "users": {}}), FakeDispatcher()
    )
    hass.data[DOMAIN] = manager
    await manager.async_load()
    due = datetime.now(UTC) + timedelta(days=1)
    await manager.async_create(user_id="u1", title="Bins", due=due)
    await manager.async_create(user_id="u1", title="Bins", due=due + timedelta(hours=1))
    await manager.async_create(user_id="u2", title="Private", due=due)
    return hass, manager


async def test_ambiguous_title_returns_candidates_without_mutation(
    runtime: tuple[Any, ReminderManager],
) -> None:
    hass, manager = runtime
    response = await _update(
        hass, {"title": "Bins", "new_title": "Changed"}, context("u1")
    )
    assert response["needs_disambiguation"] is True
    assert len(response["candidates"]) == 2
    assert all(item.title == "Bins" for item in await manager.async_list(user_id="u1"))


async def test_ordinary_user_cannot_list_another_users_reminders(
    runtime: tuple[Any, ReminderManager],
) -> None:
    hass, _ = runtime
    with pytest.raises(HomeAssistantError, match="another user"):
        await _list(hass, {"user_id": "u2", "limit": 20}, context("u1"))


async def test_admin_targeting_is_explicit(
    runtime: tuple[Any, ReminderManager],
) -> None:
    hass, _ = runtime
    own = await _list(hass, {"limit": 20}, context("admin"))
    other = await _list(hass, {"user_id": "u2", "limit": 20}, context("admin"))
    assert own["reminders"] == []
    assert [item["title"] for item in other["reminders"]] == ["Private"]
    assert other["total"] == 1
    assert other["has_more"] is False


def test_recurring_voice_controls_are_exposed() -> None:
    names = {tool.name for tool in _tools()}
    assert {
        "pause_recurring_reminder",
        "resume_recurring_reminder",
        "skip_next_reminder",
    } <= names


async def test_recurring_voice_handlers_reuse_owner_checked_manager_operations(
    runtime: tuple[Any, ReminderManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, manager = runtime
    item = (await manager.async_list(user_id="u1"))[0]
    expected = [
        ("pause", item.id, "u1"),
        ("resume", item.id, "u1"),
        ("skip_next", item.id, "u1"),
    ]
    calls: list[tuple[str, str, str | None]] = []

    for name, handler in (
        ("pause", _pause_series),
        ("resume", _resume_series),
        ("skip_next", _skip_next_series),
    ):

        async def operation(
            reminder_id: str,
            *,
            expected_user_id: str | None = None,
            action: str = name,
        ) -> Any:
            calls.append((action, reminder_id, expected_user_id))
            return item

        monkeypatch.setattr(manager, f"async_{name}", operation)
        result = await handler(hass, {"reminder_id": item.id}, context("u1"))
        assert result["reminder"]["id"] == item.id

    assert calls == expected


async def test_exact_title_disambiguation_searches_past_first_page(
    runtime: tuple[Any, ReminderManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, manager = runtime
    found = (await manager.async_list(user_id="u1"))[0]
    duplicate = found.updated(id="another-exact-title")
    calls: list[int] = []

    async def pages(**kwargs: Any) -> tuple[list[Any], int]:
        offset = kwargs["offset"]
        calls.append(offset)
        if offset == 0:
            return [found], 2
        return [duplicate], 2

    monkeypatch.setattr(manager, "async_list_page", pages)
    _, response, _ = await _resolve_reminder(
        hass, {"title": found.title}, context("u1")
    )
    assert calls == [0, 1]
    assert response is not None
    assert response["needs_disambiguation"] is True
    assert len(response["candidates"]) == 2


async def test_voice_list_discloses_when_results_are_truncated(
    runtime: tuple[Any, ReminderManager],
) -> None:
    hass, _ = runtime
    result = await _list(hass, {"limit": 1}, context("u1"))
    assert len(result["reminders"]) == 1
    assert result["total"] == 2
    assert result["has_more"] is True


async def test_voice_list_can_retrieve_following_page(
    runtime: tuple[Any, ReminderManager],
) -> None:
    hass, _ = runtime
    first = await _list(hass, {"limit": 1, "offset": 0}, context("u1"))
    second = await _list(hass, {"limit": 1, "offset": 1}, context("u1"))
    assert first["total"] == second["total"] == 2
    assert first["next_offset"] == 1
    assert first["has_more"] is True
    assert second["offset"] == 1
    assert second["next_offset"] == 2
    assert second["has_more"] is False
    assert first["reminders"][0]["id"] != second["reminders"][0]["id"]


async def test_voice_can_edit_existing_recurring_pattern(
    runtime: tuple[Any, ReminderManager],
) -> None:
    hass, manager = runtime
    start = (datetime.now(UTC) + timedelta(days=2)).replace(tzinfo=None)
    start += timedelta(days=(0 - start.weekday()) % 7)
    series = await manager.async_create_recurring(
        user_id="u1",
        title="Scheduled chore",
        recurrence=RecurrenceRule(RecurrenceFrequency.DAILY, 1, "UTC", start),
    )
    response = await _update(
        hass,
        {
            "reminder_id": series.id,
            "frequency": "weekly",
            "weekdays": ["monday", "wednesday"],
        },
        context("u1"),
    )
    assert response["reminder"]["recurrence"]["frequency"] == "weekly"
    assert set(response["reminder"]["recurrence"]["weekdays"]) == {
        "monday",
        "wednesday",
    }


async def test_voice_cannot_add_recurrence_to_one_time_reminder(
    runtime: tuple[Any, ReminderManager],
) -> None:
    from custom_components.reminders.manager import ReminderValidationError

    hass, manager = runtime
    item = (await manager.async_list(user_id="u1"))[0]
    with pytest.raises(ReminderValidationError, match="recurring"):
        await _update(
            hass, {"reminder_id": item.id, "frequency": "daily"}, context("u1")
        )


async def test_voice_updates_and_resets_reminder_delivery_channels(
    runtime: tuple[Any, ReminderManager],
) -> None:
    hass, manager = runtime
    reminder = (await manager.async_list(user_id="u1"))[0]
    custom = await _update(
        hass,
        {
            "reminder_id": reminder.id,
            "delivery_mode": "custom",
            "channels": ["persistent_notification"],
        },
        context("u1"),
    )
    assert custom["reminder"]["delivery_policy"]["channels"] == [
        "persistent_notification"
    ]
    reset = await _update(
        hass,
        {"reminder_id": reminder.id, "delivery_mode": "default"},
        context("u1"),
    )
    assert reset["reminder"]["delivery_policy"] is None


async def test_tool_dispatch_applies_defaults_and_rejects_invalid_arguments(runtime):
    hass, _manager = runtime
    tools = {tool.name: tool for tool in _tools()}
    result = await tools["create_reminder"].async_call(
        hass,
        llm.ToolInput(
            tool_name="create_reminder",
            tool_args={"title": "Minimal", "delay_minutes": 10},
        ),
        context("u1"),
    )
    assert result
    listed = await tools["list_reminders"].async_call(
        hass, llm.ToolInput(tool_name="list_reminders", tool_args={}), context("u1")
    )
    assert listed
    with pytest.raises(HomeAssistantError):
        await tools["create_reminder"].async_call(
            hass,
            llm.ToolInput(
                tool_name="create_reminder",
                tool_args={"title": "Invalid", "delay_minutes": -1},
            ),
            context("u1"),
        )
