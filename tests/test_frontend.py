"""Panel and static-resource lifecycle tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from custom_components.reminders.frontend import (
    FRONTEND_DATA,
    PANEL_ELEMENT,
    PANEL_PATH,
    async_register_frontend,
    async_unregister_panel,
)


async def test_panel_registers_once_and_reloads_cleanly(
    monkeypatch: Any,
) -> None:
    register_static = AsyncMock()
    register_panel = AsyncMock()
    remove_panel = Mock()
    exists = False

    def panel_exists(hass: Any, path: str) -> bool:
        return exists

    monkeypatch.setattr(
        "custom_components.reminders.frontend.frontend.async_panel_exists",
        panel_exists,
    )
    monkeypatch.setattr(
        "custom_components.reminders.frontend.panel_custom.async_register_panel",
        register_panel,
    )
    monkeypatch.setattr(
        "custom_components.reminders.frontend.frontend.async_remove_panel",
        remove_panel,
    )
    hass = SimpleNamespace(
        data={}, http=SimpleNamespace(async_register_static_paths=register_static)
    )

    await async_register_frontend(hass)
    assert hass.data[FRONTEND_DATA] is True
    register_static.assert_awaited_once()
    register_panel.assert_awaited_once()
    assert register_panel.await_args.kwargs["frontend_url_path"] == PANEL_PATH
    assert register_panel.await_args.kwargs["webcomponent_name"] == PANEL_ELEMENT
    assert register_panel.await_args.kwargs["config_panel_domain"] == "reminders"

    exists = True
    await async_register_frontend(hass)
    register_static.assert_awaited_once()
    register_panel.assert_awaited_once()

    async_unregister_panel(hass)
    remove_panel.assert_called_once_with(hass, PANEL_PATH, warn_if_unknown=False)
