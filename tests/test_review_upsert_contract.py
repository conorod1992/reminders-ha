from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.reminders import manager as manager_module
from custom_components.reminders.interop_manager import InteropReminderManager
from custom_components.reminders.interop_services import async_register_interop_services
from custom_components.reminders.models import ReminderStatus
from custom_components.reminders.services import async_register_services
from tests.conftest import FakeDispatcher, FakeStore


class Services:
    def __init__(self):
        self.handlers = {}

    def has_service(self, d, s):
        return (d, s) in self.handlers

    def async_register(self, d, s, h, schema, **kw):
        self.handlers[d, s] = (h, schema)

    async def async_call(self, d, s, data, context=None, return_response=True, **kw):
        h, schema = self.handlers[d, s]
        return await h(
            SimpleNamespace(
                data=schema(data),
                context=context or SimpleNamespace(user_id="u1"),
                return_response=return_response,
            )
        )


async def noop(*a):
    pass


async def test_upsert_replacement_and_idempotence(monkeypatch):
    monkeypatch.setattr(
        manager_module, "async_track_point_in_utc_time", lambda *a: lambda: None
    )
    auth = SimpleNamespace(async_get_user=lambda _: get_user())
    hass = SimpleNamespace(
        data={},
        auth=auth,
        services=Services(),
        bus=SimpleNamespace(async_fire=lambda *a: None),
        states=SimpleNamespace(get=lambda _: None),
        config=SimpleNamespace(time_zone="UTC"),
        create_task=lambda c, *a: c.close(),
    )
    manager = InteropReminderManager(hass, FakeStore(), FakeDispatcher())
    manager._trigger_registry.async_sync = noop
    manager._native_runtime.async_sync = noop
    manager._async_evaluate_immediate = noop
    manager._async_restore_trigger_durations = noop
    hass.data["reminders"] = manager
    async_register_services(hass)
    async_register_interop_services(hass)
    base = {
        "source": "peer",
        "source_id": "first",
        "kind": "one_time",
        "data": {
            "title": "Example",
            "due": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "message": "old message",
            "external_actions": [{"id": "old", "label": "Old"}],
        },
    }
    a = await hass.services.async_call("reminders", "upsert", base)
    second = {**base, "data": {"title": "Example", "due": base["data"]["due"]}}
    b = await hass.services.async_call("reminders", "upsert", second)
    assert b["reminder"]["message"] is None
    assert b["reminder"]["external_actions"] == []
    triggered = {
        "source": "peer",
        "source_id": "trigger",
        "kind": "triggered",
        "data": {
            "title": "Run once",
            "trigger": {"type": "named", "trigger_id": "foo"},
        },
    }
    a = await hass.services.async_call("reminders", "upsert", triggered)
    rid = a["reminder"]["id"]
    current = manager._reminders[rid]
    manager._reminders[rid] = current.updated(status=ReminderStatus.COMPLETED)
    b = await hass.services.async_call("reminders", "upsert", triggered)
    assert b["reminder"]["status"] == "completed"
    recurring = {
        "source": "peer",
        "source_id": "repeat",
        "kind": "recurring",
        "data": {
            "title": "Daily",
            "frequency": "daily",
            "first_reminder": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    }
    a = await hass.services.async_call("reminders", "upsert", recurring)
    rid = a["reminder"]["id"]
    await manager.async_skip_next(rid)
    skipped = manager._reminders[rid]
    b = await hass.services.async_call("reminders", "upsert", recurring)
    assert b["reminder"]["due"] == skipped.due.isoformat()


async def get_user():
    return SimpleNamespace(id="u1", is_admin=True)
