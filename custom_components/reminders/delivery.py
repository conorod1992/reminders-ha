"""Delivery providers for Reminders."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from homeassistant.core import HomeAssistant

from .const import (
    CHANNEL_PERSISTENT_NOTIFICATION,
    CHANNEL_PHONE,
    CHANNEL_VOICE,
)
from .models import DeliveryPolicy, Reminder

_LOGGER = logging.getLogger(__name__)


class DeliveryProvider(Protocol):
    """Protocol implemented by a logical delivery channel."""

    channel: str

    async def async_deliver(self, reminder: Reminder, policy: DeliveryPolicy) -> None:
        """Deliver a reminder."""


class PersistentNotificationProvider:
    """Deliver through Home Assistant persistent notifications."""

    channel = CHANNEL_PERSISTENT_NOTIFICATION

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_deliver(self, reminder: Reminder, policy: DeliveryPolicy) -> None:
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": reminder.title,
                "message": reminder.message or reminder.title,
                "notification_id": f"reminders_{reminder.id}",
            },
            blocking=True,
        )


class NotifyProvider:
    """Deliver through selected modern notify entities."""

    channel = CHANNEL_PHONE

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_deliver(self, reminder: Reminder, policy: DeliveryPolicy) -> None:
        if not policy.notify_targets:
            raise ValueError("Phone delivery has no configured notify targets")
        await self._hass.services.async_call(
            "notify",
            "send_message",
            {
                "title": reminder.title,
                "message": reminder.message or reminder.title,
            },
            target={"entity_id": list(policy.notify_targets)},
            blocking=True,
        )


class VoiceProvider:
    """Deliver through selected Assist satellites that support announcements."""

    channel = CHANNEL_VOICE

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_deliver(self, reminder: Reminder, policy: DeliveryPolicy) -> None:
        if not policy.voice_targets:
            raise ValueError(
                "Voice delivery has no configured Assist satellite targets"
            )
        await self._hass.services.async_call(
            "assist_satellite",
            "announce",
            {"message": reminder.message or reminder.title},
            target={"entity_id": list(policy.voice_targets)},
            blocking=True,
        )


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Aggregate delivery result."""

    succeeded: tuple[str, ...]
    errors: tuple[str, ...]
    failed: tuple[str, ...] = ()

    @property
    def failed_channels(self) -> tuple[str, ...]:
        """Return stable failed channel names, including legacy test doubles."""
        if self.failed:
            return self.failed
        return tuple(error.partition(":")[0] for error in self.errors)


class DeliveryDispatcher:
    """Resolve logical channels and isolate provider failures."""

    def __init__(self, providers: list[DeliveryProvider]) -> None:
        self._providers = {provider.channel: provider for provider in providers}

    async def async_deliver(
        self, reminder: Reminder, policy: DeliveryPolicy
    ) -> DeliveryResult:
        """Deliver through every configured channel."""
        succeeded: list[str] = []
        errors: list[str] = []
        failed: list[str] = []
        for channel in dict.fromkeys(policy.channels):
            provider = self._providers.get(channel)
            if provider is None:
                errors.append(f"{channel}: provider unavailable")
                failed.append(channel)
                continue
            try:
                await provider.async_deliver(reminder, policy)
            except Exception as err:
                _LOGGER.warning(
                    "Reminder %s delivery through %s failed: %s",
                    reminder.id,
                    channel,
                    err,
                )
                errors.append(f"{channel}: {type(err).__name__}")
                failed.append(channel)
            else:
                succeeded.append(channel)
        return DeliveryResult(tuple(succeeded), tuple(errors), tuple(failed))
