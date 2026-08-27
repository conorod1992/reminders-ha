"""Regression tests for integration-global WebSocket registration."""

from __future__ import annotations

from typing import Any

from custom_components.reminders import websocket_registration


def test_trigger_commands_are_registered(monkeypatch: Any) -> None:
    """The module used by async_setup must expose every trigger command."""
    registered: list[str] = []

    def register(_hass: Any, handler: Any) -> None:
        registered.append(handler.__name__)

    monkeypatch.setattr(websocket_registration, "async_register_command", register)

    websocket_registration.async_register_websocket_api(object())

    assert "websocket_create_triggered" in registered
    assert "websocket_fire_trigger" in registered


def test_trigger_and_attention_views_are_accepted_by_registered_list_schema() -> None:
    """The live list handler must accept every panel view added outside core CRUD."""
    schema = websocket_registration.websocket_list._ws_schema

    for view in ("attention", "triggered", "waiting_for_trigger", "expired"):
        message = schema({"id": 1, "type": "reminders/list", "view": view})
        assert message["view"] == view
