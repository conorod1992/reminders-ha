"""Regression tests for Home Assistant user-boundary security."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import (
    ActivationType,
    DeliveryPolicy,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.permissions import (
    ReminderPermissionError,
    async_filter_delivery_policy_permissions,
    async_validate_delivery_policy_permission,
    async_validate_trigger_permission,
)
from custom_components.reminders.triggers.models import TriggerDefinition

from .conftest import FakeDispatcher, FakeStore


class FakePermissions:
    def __init__(
        self,
        *,
        read: set[str] | None = None,
        control: set[str] | None = None,
    ) -> None:
        self.read = read or set()
        self.control = control or set()

    def check_entity(self, entity_id: str, policy: str) -> bool:
        if policy == "read":
            return entity_id in self.read
        if policy == "control":
            return entity_id in self.control
        return False


class FakeAuth:
    def __init__(self, users: dict[str, object]) -> None:
        self.users = users

    async def async_get_user(self, user_id: str) -> object | None:
        return self.users.get(user_id)


def user(
    user_id: str,
    *,
    admin: bool = False,
    read: set[str] | None = None,
    control: set[str] | None = None,
) -> object:
    return SimpleNamespace(
        id=user_id,
        is_admin=admin,
        permissions=FakePermissions(read=read, control=control),
    )


def hass_with(user_obj: object) -> object:
    return SimpleNamespace(auth=FakeAuth({user_obj.id: user_obj}))


async def test_classic_trigger_requires_read_permission() -> None:
    trigger = TriggerDefinition.from_dict(
        {"type": "state", "entity_id": "sensor.private", "to": "on"}
    )
    hass = hass_with(user("u1"))
    with pytest.raises(ReminderPermissionError, match="cannot read"):
        await async_validate_trigger_permission(hass, "u1", trigger)  # type: ignore[arg-type]


async def test_event_trigger_requires_admin_owner() -> None:
    trigger = TriggerDefinition.from_dict(
        {"type": "event", "event_type": "sensitive_event"}
    )
    hass = hass_with(user("u1"))
    with pytest.raises(ReminderPermissionError, match="administrator"):
        await async_validate_trigger_permission(hass, "u1", trigger)  # type: ignore[arg-type]


async def test_delivery_targets_require_control_and_mobile_service_requires_admin() -> (
    None
):
    hass = hass_with(user("u1", control={"notify.allowed"}))
    await async_validate_delivery_policy_permission(
        hass,  # type: ignore[arg-type]
        "u1",
        DeliveryPolicy(("phone",), notify_targets=("notify.allowed",)),
    )
    with pytest.raises(ReminderPermissionError, match="cannot control"):
        await async_validate_delivery_policy_permission(
            hass,  # type: ignore[arg-type]
            "u1",
            DeliveryPolicy(("voice",), voice_targets=("assist_satellite.private",)),
        )
    with pytest.raises(ReminderPermissionError, match="administrator"):
        await async_validate_delivery_policy_permission(
            hass,  # type: ignore[arg-type]
            "u1",
            DeliveryPolicy(
                ("phone",), mobile_app_services=("notify.mobile_app_private",)
            ),
        )


async def test_revoked_delivery_permission_fails_without_calling_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _when: lambda: None,
    )
    owner = user("u1", control=set())
    hass = SimpleNamespace(
        auth=FakeAuth({"u1": owner}),
        bus=SimpleNamespace(),
        states=SimpleNamespace(get=lambda _entity_id: None),
        config=SimpleNamespace(time_zone="UTC"),
    )
    dispatcher = FakeDispatcher()
    manager = ReminderManager(
        hass,  # type: ignore[arg-type]
        FakeStore(),
        dispatcher,  # type: ignore[arg-type]
    )
    manager._loaded = True
    due = datetime.now(UTC)
    occurrence = Occurrence(
        "restricted-occurrence",
        due,
        due,
        status=OccurrenceStatus.DELIVERING,
    )
    reminder = Reminder(
        id="restricted-delivery",
        user_id="u1",
        title="Private target",
        due=due,
        created_at=due,
        updated_at=due,
        status=ReminderStatus.DELIVERING,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        delivery_policy=DeliveryPolicy(("phone",), notify_targets=("notify.private",)),
    )
    manager._reminders = {reminder.id: reminder}

    await manager._async_deliver_claimed(reminder.id, due)

    assert dispatcher.calls == []
    assert manager._reminders[reminder.id].status is ReminderStatus.FAILED


async def test_legacy_unauthorized_trigger_cannot_activate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _when: lambda: None,
    )
    owner = user("u1", read=set())
    hass = SimpleNamespace(
        auth=FakeAuth({"u1": owner}),
        bus=SimpleNamespace(),
        states=SimpleNamespace(get=lambda _entity_id: None),
        config=SimpleNamespace(time_zone="UTC"),
    )
    dispatcher = FakeDispatcher()
    manager = ReminderManager(
        hass,  # type: ignore[arg-type]
        FakeStore(),
        dispatcher,  # type: ignore[arg-type]
    )
    manager._loaded = True
    now = datetime.now(UTC)
    trigger = TriggerDefinition.from_dict(
        {"type": "state", "entity_id": "sensor.private", "to": "on"}
    )
    reminder = Reminder(
        id="restricted-trigger",
        user_id="u1",
        title="Private trigger",
        due=None,
        created_at=now,
        updated_at=now,
        status=ReminderStatus.WAITING_FOR_TRIGGER,
        activation_type=ActivationType.TRIGGER,
        trigger=trigger,
    )
    manager._reminders = {reminder.id: reminder}

    result = await manager.async_activate_trigger(
        reminder.id, cause="future_transition", context={"to": "on"}
    )

    assert result == "inactive"
    assert dispatcher.calls == []
    assert manager._reminders[reminder.id].status is ReminderStatus.WAITING_FOR_TRIGGER


async def test_secondary_delivery_rechecks_revoked_owner_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _when: lambda: None,
    )
    hass = hass_with(user("u1", control=set()))
    dispatcher = FakeDispatcher()
    runtime = ReminderManager(
        hass,  # type: ignore[arg-type]
        FakeStore(),
        dispatcher,  # type: ignore[arg-type]
    )
    now = datetime.now(UTC)
    reminder = Reminder(
        id="follow-up",
        user_id="u1",
        title="Reminder",
        due=now,
        created_at=now,
        updated_at=now,
    )
    result = await runtime._async_deliver_with_current_permissions(
        reminder,
        DeliveryPolicy(("phone",), notify_targets=("notify.revoked",)),
    )
    assert result.succeeded == ()
    assert result.failed_channels == ("phone",)
    assert dispatcher.calls == []


async def test_due_delivery_condition_rechecks_revoked_read_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed state condition cannot reveal an entity after permission loss."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _when: lambda: None,
    )
    owner = user("u1", read=set())
    hass = SimpleNamespace(
        auth=FakeAuth({"u1": owner}),
        bus=SimpleNamespace(),
        states=SimpleNamespace(get=lambda _entity_id: None),
        config=SimpleNamespace(time_zone="UTC"),
    )
    dispatcher = FakeDispatcher()
    manager = ReminderManager(
        hass,  # type: ignore[arg-type]
        FakeStore(),
        dispatcher,  # type: ignore[arg-type]
    )
    manager._loaded = True
    manager._trigger_registry.async_sync = AsyncMock()  # type: ignore[method-assign]
    due = datetime.now(UTC)
    condition = TriggerDefinition.from_dict(
        {"type": "state", "entity_id": "sensor.private", "to": "on"}
    )
    occurrence = Occurrence("due-private", due, due)
    reminder = Reminder(
        id="due-private",
        user_id="u1",
        title="Private condition",
        due=due,
        created_at=due,
        updated_at=due,
        status=ReminderStatus.PENDING,
        current_occurrence_id=occurrence.id,
        occurrence_history=(occurrence,),
        deliver_when=condition,
    )
    manager._reminders = {reminder.id: reminder}
    await manager._async_process_due(due)
    assert dispatcher.calls == []
    assert manager._reminders[reminder.id].status is ReminderStatus.SKIPPED
    assert (
        manager._reminders[reminder.id].occurrence_history[-1].completion_reason
        == "permission_denied"
    )
    # A denied occurrence is resolved, not silently delivered after access returns.
    owner.permissions.read.add("sensor.private")
    await manager._async_process_due(due)
    assert dispatcher.calls == []
    assert manager._reminders[reminder.id].status is ReminderStatus.SKIPPED


async def test_revoked_one_of_multiple_targets_preserves_healthy_destination() -> None:
    hass = hass_with(user("u1", control={"notify.allowed", "assist_satellite.allowed"}))
    permitted, denied = await async_filter_delivery_policy_permissions(
        hass,  # type: ignore[arg-type]
        "u1",
        DeliveryPolicy(
            ("phone", "voice"),
            notify_targets=("notify.allowed", "notify.revoked"),
            voice_targets=("assist_satellite.allowed", "assist_satellite.revoked"),
        ),
    )
    assert permitted.channels == ("phone", "voice")
    assert permitted.notify_targets == ("notify.allowed",)
    assert permitted.voice_targets == ("assist_satellite.allowed",)
    assert denied == ("phone", "voice")


async def test_revoked_entire_target_group_removes_only_that_channel() -> None:
    hass = hass_with(user("u1", control={"notify.allowed"}))
    permitted, denied = await async_filter_delivery_policy_permissions(
        hass,  # type: ignore[arg-type]
        "u1",
        DeliveryPolicy(
            ("phone", "voice", "persistent_notification"),
            notify_targets=("notify.allowed",),
            voice_targets=("assist_satellite.revoked",),
        ),
    )
    assert permitted.channels == ("phone", "persistent_notification")
    assert denied == ("voice",)
