"""Home Assistant permission checks for reminder observation and delivery."""

from __future__ import annotations

from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.core import HomeAssistant

from .models import DeliveryPolicy, Reminder
from .triggers.models import TriggerDefinition, TriggerType


class ReminderPermissionError(ValueError):
    """Raised when a reminder would exceed its owner's HA permissions."""


async def _async_user(hass: HomeAssistant, user_id: str) -> tuple[Any | None, bool]:
    """Return the owner and whether HA auth is available in this runtime."""
    auth = getattr(hass, "auth", None)
    getter = getattr(auth, "async_get_user", None)
    if not callable(getter):
        # Lightweight unit-test harnesses do not construct HA's auth manager.
        # A real Home Assistant runtime always does.
        return None, False
    return await getter(user_id), True


def _can(user: Any, entity_id: str, policy: str) -> bool:
    permissions = getattr(user, "permissions", None)
    check = getattr(permissions, "check_entity", None)
    return bool(callable(check) and check(entity_id, policy))


async def async_user_is_admin(hass: HomeAssistant, user_id: str) -> bool:
    """Return whether the owner is currently an administrator."""
    user, enforced = await _async_user(hass, user_id)
    if not enforced:
        return True
    return bool(user is not None and user.is_admin)


async def async_validate_trigger_permission(
    hass: HomeAssistant, user_id: str, trigger: TriggerDefinition
) -> None:
    """Require current read permission for observable classic triggers."""
    user, enforced = await _async_user(hass, user_id)
    if not enforced:
        return
    if user is None:
        raise ReminderPermissionError("Reminder owner no longer exists")
    if user.is_admin or trigger.type is TriggerType.NAMED:
        return
    if trigger.type is TriggerType.EVENT:
        raise ReminderPermissionError(
            "Home Assistant event triggers require an administrator owner"
        )
    for entity_id in (trigger.entity_id, trigger.zone_entity_id):
        if entity_id and not _can(user, entity_id, POLICY_READ):
            raise ReminderPermissionError(
                f"Reminder owner cannot read trigger entity {entity_id}"
            )


async def async_trigger_permitted(
    hass: HomeAssistant, user_id: str, trigger: TriggerDefinition
) -> bool:
    """Return whether a persisted classic trigger may currently act."""
    try:
        await async_validate_trigger_permission(hass, user_id, trigger)
    except ReminderPermissionError:
        return False
    return True


async def async_validate_delivery_policy_permission(
    hass: HomeAssistant, user_id: str, policy: DeliveryPolicy | None
) -> None:
    """Require current control permission for configured delivery targets."""
    if policy is None:
        return
    restricted = bool(
        policy.notify_targets or policy.mobile_app_services or policy.voice_targets
    )
    if not restricted:
        return
    user, enforced = await _async_user(hass, user_id)
    if not enforced:
        return
    if user is None:
        raise ReminderPermissionError("Reminder owner no longer exists")
    if user.is_admin:
        return
    if policy.mobile_app_services:
        raise ReminderPermissionError(
            "Companion App service targets require an administrator owner"
        )
    for entity_id in policy.notify_targets:
        if not _can(user, entity_id, POLICY_CONTROL):
            raise ReminderPermissionError(
                f"Reminder owner cannot control notify target {entity_id}"
            )
    for entity_id in policy.voice_targets:
        if not _can(user, entity_id, POLICY_CONTROL):
            raise ReminderPermissionError(
                f"Reminder owner cannot control voice target {entity_id}"
            )


async def async_validate_reminder_permissions(
    hass: HomeAssistant, reminder: Reminder
) -> None:
    """Validate every security-sensitive reference stored by one reminder."""
    await async_validate_delivery_policy_permission(
        hass, reminder.user_id, reminder.delivery_policy
    )
    for trigger in (reminder.trigger, reminder.deliver_when, reminder.complete_when):
        if trigger is not None:
            await async_validate_trigger_permission(hass, reminder.user_id, trigger)
    if any(
        (
            reminder.activation_triggers,
            reminder.delivery_triggers,
            reminder.delivery_conditions,
            reminder.completion_triggers,
        )
    ) and not await async_user_is_admin(hass, reminder.user_id):
        raise ReminderPermissionError(
            "Home Assistant-native reminder rules require an administrator owner"
        )


async def async_filter_delivery_policy_permissions(
    hass: HomeAssistant, user_id: str, policy: DeliveryPolicy
) -> tuple[DeliveryPolicy, tuple[str, ...]]:
    """Remove channels whose targets the owner may no longer control."""
    relevant = {channel for channel in policy.channels if channel in {"phone", "voice"}}
    if not relevant:
        return policy, ()
    user, enforced = await _async_user(hass, user_id)
    if not enforced:
        return policy, ()
    denied: set[str] = set()
    if user is None:
        denied.update(relevant)
    elif not user.is_admin:
        if "phone" in relevant and (
            policy.mobile_app_services
            or any(
                not _can(user, item, POLICY_CONTROL) for item in policy.notify_targets
            )
        ):
            denied.add("phone")
        if "voice" in relevant and any(
            not _can(user, item, POLICY_CONTROL) for item in policy.voice_targets
        ):
            denied.add("voice")
    if not denied:
        return policy, ()
    return (
        DeliveryPolicy(
            channels=tuple(
                channel for channel in policy.channels if channel not in denied
            ),
            notify_targets=policy.notify_targets,
            mobile_app_services=policy.mobile_app_services,
            voice_targets=policy.voice_targets,
        ),
        tuple(channel for channel in policy.channels if channel in denied),
    )
