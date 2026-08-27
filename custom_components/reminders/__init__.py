"""Reminders integration."""

from __future__ import annotations

from contextlib import suppress

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .conversation import async_register_conversation_api
from .delivery import (
    DeliveryDispatcher,
    NotifyProvider,
    PersistentNotificationProvider,
    VoiceProvider,
)
from .frontend import async_register_frontend, async_unregister_panel
from .native_manager import NativeReminderManager
from .services import async_register_services
from .storage import ReminderStore
from .websocket_registration import async_register_websocket_api

RemindersConfigEntry = ConfigEntry[NativeReminderManager]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-wide resources."""
    async_register_services(hass)
    async_register_websocket_api(hass)
    async_register_conversation_api(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RemindersConfigEntry) -> bool:
    """Set up Reminders from a config entry."""
    dispatcher = DeliveryDispatcher(
        [
            PersistentNotificationProvider(hass),
            NotifyProvider(hass),
            VoiceProvider(hass),
        ]
    )
    manager = NativeReminderManager(hass, ReminderStore(hass), dispatcher)
    try:
        await manager.async_load()
        await async_register_frontend(hass)
    except BaseException:
        # async_load can install listeners and timers before later recovery work
        # fails. Always tear down that partial runtime and preserve the original
        # setup exception.
        with suppress(BaseException):
            await manager.async_unload()
        raise
    entry.runtime_data = manager
    hass.data[DOMAIN] = manager
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RemindersConfigEntry) -> bool:
    """Unload a Reminders config entry."""
    await entry.runtime_data.async_unload()
    async_unregister_panel(hass)
    hass.data.pop(DOMAIN, None)
    return True
