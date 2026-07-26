"""Reminders integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .delivery import (
    DeliveryDispatcher,
    NotifyProvider,
    PersistentNotificationProvider,
    VoiceProvider,
)
from .manager import ReminderManager
from .services import async_register_services
from .storage import ReminderStore

RemindersConfigEntry = ConfigEntry[ReminderManager]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-wide resources."""
    async_register_services(hass)
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
    manager = ReminderManager(hass, ReminderStore(hass), dispatcher)
    await manager.async_load()
    entry.runtime_data = manager
    hass.data[DOMAIN] = manager
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RemindersConfigEntry) -> bool:
    """Unload a Reminders config entry."""
    await entry.runtime_data.async_unload()
    hass.data.pop(DOMAIN, None)
    return True
