"""Shared authorization helpers for reminder actions and frontend APIs."""

from __future__ import annotations

from typing import Protocol, cast

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .manager import ReminderManager
from .models import Reminder


class Actor(Protocol):
    """Minimum authenticated-user surface needed by authorization."""

    @property
    def id(self) -> str:
        """Home Assistant user ID."""

    @property
    def is_admin(self) -> bool:
        """Whether this actor has administrator privileges."""


async def async_actor(hass: HomeAssistant, user_id: str | None) -> Actor | None:
    """Resolve an action context user; system contexts intentionally return None."""
    if user_id is None:
        return None
    return cast(Actor | None, await hass.auth.async_get_user(user_id))


async def async_validate_user(hass: HomeAssistant, user_id: str) -> None:
    """Reject unknown Home Assistant user IDs."""
    if await hass.auth.async_get_user(user_id) is None:
        raise HomeAssistantError("Unknown Home Assistant user")


async def async_resolve_target_user(
    hass: HomeAssistant,
    actor: Actor | None,
    requested_user_id: str | None,
) -> str:
    """Resolve a mutation/preferences owner without trusting client input."""
    if actor is None:
        if requested_user_id is None:
            raise HomeAssistantError("A user ID is required for a system action")
        await async_validate_user(hass, requested_user_id)
        return requested_user_id
    target = requested_user_id or actor.id
    if target != actor.id and not actor.is_admin:
        raise HomeAssistantError("You cannot manage another user's reminders")
    if target != actor.id:
        await async_validate_user(hass, target)
    return target


async def async_resolve_list_user(
    hass: HomeAssistant,
    actor: Actor | None,
    requested_user_id: str | None,
    *,
    all_users: bool = False,
) -> str | None:
    """Resolve an authorized backend list filter."""
    if actor is None:
        if requested_user_id is None:
            raise HomeAssistantError("A user ID is required for a system list action")
        if requested_user_id is not None:
            await async_validate_user(hass, requested_user_id)
        return requested_user_id
    if all_users:
        if not actor.is_admin:
            raise HomeAssistantError("Only administrators can list all reminders")
        return None
    return await async_resolve_target_user(hass, actor, requested_user_id)


async def async_get_authorized(
    manager: ReminderManager,
    actor: Actor | None,
    reminder_id: str,
) -> Reminder:
    """Return a reminder only when the actor may manage its owner."""
    reminder = await manager.async_get(reminder_id)
    if actor is None or actor.id == reminder.user_id or actor.is_admin:
        return reminder
    raise HomeAssistantError("You cannot access another user's reminder")
