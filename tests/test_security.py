"""Tests for action ownership enforcement."""

from types import SimpleNamespace

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.reminders.services import _resolve_user


class FakeAuth:
    def __init__(self, *, is_admin: bool) -> None:
        self.is_admin = is_admin

    async def async_get_user(self, user_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=user_id, is_admin=self.is_admin)


async def test_authenticated_user_defaults_to_self() -> None:
    hass = SimpleNamespace(auth=FakeAuth(is_admin=False))
    call = ServiceCall(hass, "reminders", "create", context=Context(user_id="u1"))
    assert await _resolve_user(hass, call, None) == "u1"


async def test_ordinary_user_cannot_select_another_user() -> None:
    hass = SimpleNamespace(auth=FakeAuth(is_admin=False))
    call = ServiceCall(hass, "reminders", "create", context=Context(user_id="u1"))
    with pytest.raises(HomeAssistantError):
        await _resolve_user(hass, call, "u2")


async def test_admin_can_select_another_user() -> None:
    hass = SimpleNamespace(auth=FakeAuth(is_admin=True))
    call = ServiceCall(hass, "reminders", "create", context=Context(user_id="admin"))
    assert await _resolve_user(hass, call, "u2") == "u2"
