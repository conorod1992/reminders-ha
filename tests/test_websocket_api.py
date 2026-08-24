"""Behavior and privacy tests for the management WebSocket API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.websocket_api import (
    websocket_create,
    websocket_create_recurring,
    websocket_delete,
    websocket_get,
    websocket_get_preferences,
    websocket_list,
    websocket_set_preferences,
    websocket_snooze,
    websocket_subscribe,
    websocket_update,
)

from .conftest import FakeDispatcher, FakeStore


class Auth:
    def __init__(self, users: list[Any]) -> None:
        self.users = {user.id: user for user in users}

    async def async_get_user(self, user_id: str) -> Any | None:
        return self.users.get(user_id)

    async def async_get_users(self) -> list[Any]:
        return list(self.users.values())


class Connection:
    def __init__(self, user: Any) -> None:
        self.user = user
        self.results: list[Any] = []
        self.events: list[Any] = []
        self.subscriptions: dict[int, Any] = {}

    def send_result(self, msg_id: int, result: Any = None) -> None:
        self.results.append((msg_id, result))

    def send_event(self, msg_id: int, event: Any = None) -> None:
        self.events.append((msg_id, event))


def user(user_id: str, *, admin: bool = False) -> Any:
    return SimpleNamespace(
        id=user_id,
        name=user_id.title(),
        is_admin=admin,
        is_active=True,
        system_generated=False,
    )


async def invoke(
    handler: Any, hass: Any, connection: Connection, **message: Any
) -> Any:
    raw = {"id": len(connection.results) + 1, **message}
    validated = handler._ws_schema(raw)
    return await handler.__wrapped__(hass, connection, validated)


@pytest.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ReminderManager]:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda hass, callback, due: lambda: None,
    )
    normal = user("u1")
    other = user("u2")
    admin = user("admin", admin=True)
    hass = SimpleNamespace(
        auth=Auth([normal, other, admin]),
        config=SimpleNamespace(time_zone="Europe/Dublin"),
        data={},
    )
    manager = ReminderManager(hass, FakeStore(), FakeDispatcher())  # type: ignore[arg-type]
    await manager.async_load()
    hass.data["reminders"] = manager
    return hass, manager


async def test_normal_list_and_get_are_owner_scoped(
    api: tuple[Any, ReminderManager],
) -> None:
    hass, manager = api
    mine = await manager.async_create(
        user_id="u1", title="Mine", due=datetime.now(UTC) + timedelta(days=1)
    )
    other = await manager.async_create(
        user_id="u2", title="Private", due=datetime.now(UTC) + timedelta(days=1)
    )
    connection = Connection(user("u1"))
    await invoke(websocket_list, hass, connection, type="reminders/list")
    assert [item["id"] for item in connection.results[-1][1]["reminders"]] == [mine.id]
    await invoke(
        websocket_get,
        hass,
        connection,
        type="reminders/get",
        reminder_id=mine.id,
    )
    with pytest.raises(HomeAssistantError):
        await invoke(
            websocket_get,
            hass,
            connection,
            type="reminders/get",
            reminder_id=other.id,
        )


async def test_source_filter_round_trips_without_expanding_user_access(
    api: tuple[Any, ReminderManager],
) -> None:
    hass, manager = api
    connection = Connection(user("u1"))
    await invoke(
        websocket_create,
        hass,
        connection,
        type="reminders/create",
        title="Mine",
        due=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        source="expiry_tracker",
        source_id="milk",
        managed_externally=True,
    )
    mine = connection.results[-1][1]["reminder"]
    await manager.async_create(
        user_id="u2",
        title="Private",
        due=datetime.now(UTC) + timedelta(days=1),
        source="expiry_tracker",
        source_id="milk",
    )
    await invoke(
        websocket_list,
        hass,
        connection,
        type="reminders/list",
        source="expiry_tracker",
        source_id="milk",
    )
    reminders = connection.results[-1][1]["reminders"]
    assert [item["id"] for item in reminders] == [mine["id"]]
    assert reminders[0]["managed_externally"] is True
    with pytest.raises(HomeAssistantError):
        await invoke(
            websocket_list,
            hass,
            connection,
            type="reminders/list",
            scope="all",
        )


async def test_normal_crud_and_snooze_cannot_cross_users(
    api: tuple[Any, ReminderManager],
) -> None:
    hass, manager = api
    connection = Connection(user("u1"))
    await invoke(
        websocket_create,
        hass,
        connection,
        type="reminders/create",
        title="Created",
        due=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    created_id = connection.results[-1][1]["reminder"]["id"]
    assert (await manager.async_get(created_id)).user_id == "u1"
    with pytest.raises(HomeAssistantError):
        await invoke(
            websocket_create,
            hass,
            connection,
            type="reminders/create",
            user_id="u2",
            title="Forbidden",
            due=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        )
    await invoke(
        websocket_update,
        hass,
        connection,
        type="reminders/update",
        reminder_id=created_id,
        title="Updated",
    )
    await invoke(
        websocket_snooze,
        hass,
        connection,
        type="reminders/snooze",
        reminder_id=created_id,
        duration_seconds=600,
    )
    private = await manager.async_create(
        user_id="u2", title="Private", due=datetime.now(UTC) + timedelta(days=1)
    )
    for handler, command in (
        (websocket_update, "update"),
        (websocket_snooze, "snooze"),
        (websocket_delete, "delete"),
    ):
        payload: dict[str, Any] = {
            "type": f"reminders/{command}",
            "reminder_id": private.id,
        }
        if command == "update":
            payload["title"] = "No"
        if command == "snooze":
            payload["duration_seconds"] = 60
        with pytest.raises(HomeAssistantError):
            await invoke(handler, hass, connection, **payload)
    await invoke(
        websocket_delete,
        hass,
        connection,
        type="reminders/delete",
        reminder_id=created_id,
    )


async def test_admin_defaults_to_self_and_can_manage_other_users(
    api: tuple[Any, ReminderManager],
) -> None:
    hass, manager = api
    await manager.async_create(
        user_id="u1", title="Mine", due=datetime.now(UTC) + timedelta(days=1)
    )
    admin_reminder = await manager.async_create(
        user_id="admin", title="Admin", due=datetime.now(UTC) + timedelta(days=1)
    )
    connection = Connection(user("admin", admin=True))
    await invoke(websocket_list, hass, connection, type="reminders/list")
    assert [item["id"] for item in connection.results[-1][1]["reminders"]] == [
        admin_reminder.id
    ]
    await invoke(
        websocket_list,
        hass,
        connection,
        type="reminders/list",
        scope="all",
    )
    assert len(connection.results[-1][1]["reminders"]) == 2
    assert all("owner_name" in item for item in connection.results[-1][1]["reminders"])
    await invoke(
        websocket_create,
        hass,
        connection,
        type="reminders/create",
        user_id="u2",
        title="Delegated",
        due=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    delegated = connection.results[-1][1]["reminder"]
    assert delegated["user_id"] == "u2"
    await invoke(
        websocket_list,
        hass,
        connection,
        type="reminders/list",
        scope="user",
        user_id="u2",
    )
    assert [item["user_id"] for item in connection.results[-1][1]["reminders"]] == [
        "u2"
    ]
    await invoke(
        websocket_update,
        hass,
        connection,
        type="reminders/update",
        reminder_id=delegated["id"],
        title="Admin updated",
    )
    await invoke(
        websocket_snooze,
        hass,
        connection,
        type="reminders/snooze",
        reminder_id=delegated["id"],
        duration_seconds=30,
    )
    await invoke(
        websocket_delete,
        hass,
        connection,
        type="reminders/delete",
        reminder_id=delegated["id"],
    )


async def test_preferences_are_owner_scoped_but_admin_manageable(
    api: tuple[Any, ReminderManager],
) -> None:
    hass, _ = api
    normal = Connection(user("u1"))
    with pytest.raises(HomeAssistantError):
        await invoke(
            websocket_get_preferences,
            hass,
            normal,
            type="reminders/get_preferences",
            user_id="u2",
        )
    admin = Connection(user("admin", admin=True))
    await invoke(
        websocket_set_preferences,
        hass,
        admin,
        type="reminders/set_preferences",
        user_id="u2",
        channels=["persistent_notification"],
    )
    assert admin.results[-1][1]["user_id"] == "u2"


async def test_recurrence_ids_users_and_targets_are_validated(
    api: tuple[Any, ReminderManager],
) -> None:
    hass, _ = api
    connection = Connection(user("u1"))
    with pytest.raises(HomeAssistantError):
        await invoke(
            websocket_create_recurring,
            hass,
            connection,
            type="reminders/create_recurring",
            title="Invalid",
            first_reminder="2026-08-04T09:00:00",
            frequency="daily",
            weekdays=["tuesday"],
        )
    with pytest.raises(HomeAssistantError):
        await invoke(
            websocket_get,
            hass,
            connection,
            type="reminders/get",
            reminder_id="missing",
        )
    with pytest.raises(HomeAssistantError):
        await invoke(
            websocket_create,
            hass,
            Connection(user("admin", admin=True)),
            type="reminders/create",
            user_id="unknown",
            title="Unknown owner",
            due=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        )
    with pytest.raises(HomeAssistantError):
        await invoke(
            websocket_create,
            hass,
            connection,
            type="reminders/create",
            title="Wrong target",
            due=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            delivery_mode="custom",
            channels=["phone"],
            notify_targets=["assist_satellite.kitchen"],
        )


async def test_subscription_is_invalidation_only_and_owner_filtered(
    api: tuple[Any, ReminderManager],
) -> None:
    hass, manager = api
    connection = Connection(user("u1"))
    websocket_subscribe(
        hass,
        connection,
        {"id": 42, "type": "reminders/subscribe"},
    )
    await manager.async_create(
        user_id="u2", title="Secret title", due=datetime.now(UTC) + timedelta(days=1)
    )
    assert connection.events == []
    visible = await manager.async_create(
        user_id="u1", title="Visible", due=datetime.now(UTC) + timedelta(days=1)
    )
    assert connection.events == [(42, {"changed": True})]
    await manager.async_update(visible.id, title="Changed")
    await manager.async_snooze(visible.id, duration=timedelta(minutes=5))
    await manager.async_delete(visible.id)
    assert connection.events == [(42, {"changed": True})] * 4
    assert "Visible" not in str(connection.events)
    assert "Changed" not in str(connection.events)
