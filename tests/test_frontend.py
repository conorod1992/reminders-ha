"""Panel and static-resource lifecycle tests."""

from __future__ import annotations

from pathlib import Path
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


def test_reminder_form_only_requires_active_trigger_fields() -> None:
    """Hidden trigger controls must not block time-reminder submission."""
    panel_source = (
        Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "frontend"
        / "reminders-panel.js"
    ).read_text(encoding="utf-8")

    for field in (
        "event_type",
        "trigger_id",
        "state_entity",
        "numeric_entity",
        "zone_entity",
        "zone_zone",
    ):
        assert f"form.elements.{field}.required = triggered &&" in panel_source


def test_reminder_form_displays_submission_errors_inside_dialog() -> None:
    """Custom trigger validation and API errors remain visible above the modal."""
    panel_source = (
        Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "frontend"
        / "reminders-panel.js"
    ).read_text(encoding="utf-8")

    assert 'class="form-error hidden" role="alert"' in panel_source
    assert 'errorHost.classList.remove("hidden")' in panel_source


def test_context_conditions_use_visual_trigger_editors() -> None:
    """Context conditions share typed controls rather than whole-trigger JSON."""
    panel_source = (
        Path(__file__).parents[1]
        / "custom_components"
        / "reminders"
        / "frontend"
        / "reminders-panel.js"
    ).read_text(encoding="utf-8")

    assert "deliver_when_json" not in panel_source
    assert "complete_when_json" not in panel_source
    assert '_contextTriggerEditor("deliver_when"' in panel_source
    assert '_contextTriggerEditor("complete_when"' in panel_source
    assert 'this._triggerData(form, "deliver_when")' in panel_source
    assert 'this._triggerData(form, "complete_when")' in panel_source
    assert "_setupEntityPicker(form, `${prefix}_zone_zone`" in panel_source


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
