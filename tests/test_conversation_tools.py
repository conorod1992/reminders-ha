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
from custom_components.reminders.conversation import _list, _update
from custom_components.reminders.manager import ReminderManager

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
