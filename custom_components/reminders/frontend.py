"""Serve and register the integration-owned Reminders panel."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

PANEL_PATH = "reminders"
PANEL_ELEMENT = "reminders-management-panel"
STATIC_URL = "/reminders_static"
FRONTEND_DATA = f"{DOMAIN}_frontend_registered"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the static route once and the reloadable sidebar panel."""
    if not hass.data.get(FRONTEND_DATA):
        frontend_dir = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(frontend_dir), True)]
        )
        hass.data[FRONTEND_DATA] = True

    if frontend.async_panel_exists(hass, PANEL_PATH):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title="Reminders",
        sidebar_icon="mdi:bell",
        module_url=f"{STATIC_URL}/reminders-panel.js?v=2.0.3",
        config={"api_prefix": f"{DOMAIN}/"},
        require_admin=False,
        config_panel_domain=DOMAIN,
    )


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove only the reloadable panel; HTTP routes cannot be unregistered."""
    frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
