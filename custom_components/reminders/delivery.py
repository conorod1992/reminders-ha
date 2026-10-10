"""Delivery providers for Reminders."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import (
    CHANNEL_PERSISTENT_NOTIFICATION,
    CHANNEL_PHONE,
    CHANNEL_VOICE,
    MOBILE_ACTION_PREFIX,
)
from .models import DeliveryPolicy, OccurrenceStatus, Reminder
from .persistent_cleanup import (
    async_abandon_failed_persistent_create,
    async_track_persistent_notification,
    persistent_notification_id,
)

_LOGGER = logging.getLogger(__name__)

PERSISTENT_NOTIFICATION_TITLE = "Reminder due"
PERSISTENT_NOTIFICATION_MESSAGE = "Open Reminders to view the reminder details."
DELIVERY_TIMEOUT_SECONDS = 30.0


class DeliveryProvider(Protocol):
    """Protocol implemented by a logical delivery channel."""

    channel: str

    async def async_deliver(
        self, reminder: Reminder, policy: DeliveryPolicy
    ) -> PartialDeliveryOutcome | None:
        """Deliver a reminder."""


async def _async_service_call(
    hass: HomeAssistant,
    domain: str,
    service: str,
    data: dict[str, Any],
    **kwargs: Any,
) -> None:
    """Call one delivery service without allowing it to stall indefinitely."""
    async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
        await hass.services.async_call(domain, service, data, **kwargs)


def _delivery_occurrence_id(reminder: Reminder) -> str | None:
    """Resolve the occurrence represented by this delivery payload."""
    # Escalations and actionable retries carry the occurrence's opaque action
    # token even when the recurring series has already advanced its current ID.
    tokens: set[str] = set()
    for item in reminder.notification_actions:
        action = item.get("action")
        if not isinstance(action, str) or not action.startswith(MOBILE_ACTION_PREFIX):
            continue
        token, separator, _operation = action.removeprefix(
            MOBILE_ACTION_PREFIX
        ).partition(":")
        if separator and token:
            tokens.add(token)
    if tokens:
        for occurrence in reminder.occurrence_history:
            if occurrence.notification_action_token in tokens:
                return occurrence.id

    # Non-actionable snoozed retries are still explicitly claimed as DELIVERING.
    delivering = [
        occurrence.id
        for occurrence in reminder.occurrence_history
        if occurrence.status is OccurrenceStatus.DELIVERING
    ]
    if len(delivering) == 1:
        return delivering[0]
    return reminder.current_occurrence_id


class PersistentNotificationProvider:
    """Deliver a privacy-safe signal through HA persistent notifications.

    Home Assistant exposes persistent notifications as one shared collection
    rather than filtering them by user. Never put reminder-owned content in
    this provider payload; the owner can read the details inside Reminders.
    """

    channel = CHANNEL_PERSISTENT_NOTIFICATION

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_deliver(self, reminder: Reminder, policy: DeliveryPolicy) -> None:
        occurrence_id = _delivery_occurrence_id(reminder)
        notification_id = persistent_notification_id(reminder.id, occurrence_id)
        await async_track_persistent_notification(
            self._hass, reminder.id, occurrence_id, notification_id
        )
        try:
            await _async_service_call(
                self._hass,
                "persistent_notification",
                "create",
                {
                    "title": PERSISTENT_NOTIFICATION_TITLE,
                    "message": PERSISTENT_NOTIFICATION_MESSAGE,
                    "notification_id": notification_id,
                },
                blocking=True,
            )
        except BaseException:
            await async_abandon_failed_persistent_create(
                self._hass, reminder.id, notification_id
            )
            raise


def _mobile_actions(actions: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Keep the three most useful actions on Companion App notifications.

    Android limits notification action buttons to three. Preserve explicit
    completion/dismissal first, then one snooze; any remaining actions are
    available through the reminder panel rather than invisible notification
    buttons.
    """
    priority = {"DONE": 0, "DISMISS": 1, "SNOOZE_10": 2, "SNOOZE_60": 3}

    def rank(action: dict[str, Any]) -> int:
        name = str(action.get("action", "")).rsplit(":", 1)[-1]
        return priority.get(name, 4)

    return sorted(actions, key=rank)[:3]


@dataclass(frozen=True, slots=True)
class PartialDeliveryOutcome:
    """A channel reached at least one destination but not all of them."""

    errors: tuple[str, ...]


class NotifyProvider:
    """Deliver through generic entities and explicit Companion App services."""

    channel = CHANNEL_PHONE

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_deliver(
        self, reminder: Reminder, policy: DeliveryPolicy
    ) -> PartialDeliveryOutcome | None:
        if not policy.notify_targets and not policy.mobile_app_services:
            raise ValueError("Phone delivery has no configured notify targets")
        ordinary_data: dict[str, Any] = {
            "title": reminder.title,
            "message": reminder.message or reminder.title,
        }
        succeeded = 0
        errors: list[str] = []
        for notify_target in policy.notify_targets:
            try:
                # Dispatch individually so one unavailable entity does not
                # block independent, healthy notification targets.
                await _async_service_call(
                    self._hass,
                    "notify",
                    "send_message",
                    ordinary_data,
                    target={"entity_id": [notify_target]},
                    blocking=True,
                    context=Context(user_id=reminder.user_id),
                )
            except Exception as err:
                errors.append(f"{notify_target}: {type(err).__name__}")
            else:
                succeeded += 1
        for action_name in policy.mobile_app_services:
            _domain, service = action_name.split(".", 1)
            actionable_data = dict(ordinary_data)
            # A stable occurrence tag replaces repeated announcements instead
            # of accumulating independent notifications on the same device.
            occurrence_id = _delivery_occurrence_id(reminder)
            actionable_data["data"] = {
                "tag": f"reminders_{reminder.id}_{occurrence_id or 'current'}",
                "url": "/reminders",
            }
            if reminder.notification_actions:
                actionable_data["data"]["actions"] = _mobile_actions(
                    reminder.notification_actions
                )
            try:
                await _async_service_call(
                    self._hass,
                    "notify",
                    service,
                    actionable_data,
                    blocking=True,
                    context=Context(user_id=reminder.user_id),
                )
            except ServiceValidationError as action_err:
                # A registered target may stop supporting action metadata. Keep
                # the reminder deliverable by retrying once without buttons.
                try:
                    await _async_service_call(
                        self._hass,
                        "notify",
                        service,
                        ordinary_data,
                        blocking=True,
                        context=Context(user_id=reminder.user_id),
                    )
                except Exception as fallback_err:
                    errors.append(
                        f"{action_name}: {type(action_err).__name__}/"
                        f"{type(fallback_err).__name__}"
                    )
                    continue
            except Exception as err:
                errors.append(f"{action_name}: {type(err).__name__}")
                continue
            succeeded += 1
        if errors:
            if succeeded:
                # Preserve actionable success for a phone which did receive the
                # reminder while retaining failure details for other targets.
                return PartialDeliveryOutcome(tuple(errors))
            raise RuntimeError(f"Phone delivery failed: {'; '.join(errors)}")
        return None


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
        await _async_service_call(
            self._hass,
            "assist_satellite",
            "announce",
            {
                "message": f"{reminder.title}. {reminder.message}"
                if reminder.message and reminder.message != reminder.title
                else reminder.title
            },
            target={"entity_id": list(policy.voice_targets)},
            blocking=True,
            context=Context(user_id=reminder.user_id),
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
                # Each phone destination has its own bounded service deadline.
                # The channel envelope must not cancel later targets merely
                # because the first destination consumed its own timeout.
                destination_count = (
                    len(policy.notify_targets) + len(policy.mobile_app_services)
                    if channel == CHANNEL_PHONE
                    else 1
                )
                async with asyncio.timeout(
                    min(180.0, DELIVERY_TIMEOUT_SECONDS * max(1, destination_count * 2))
                    if channel == CHANNEL_PHONE
                    else DELIVERY_TIMEOUT_SECONDS
                ):
                    outcome = await provider.async_deliver(reminder, policy)
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
                if isinstance(outcome, PartialDeliveryOutcome):
                    failed.append(channel)
                    errors.extend(f"{channel}: {error}" for error in outcome.errors)
        return DeliveryResult(tuple(succeeded), tuple(errors), tuple(failed))
