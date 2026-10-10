"""Regression tests for complete public listing and diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

from custom_components.reminders import services as services_module
from custom_components.reminders.const import SERVICE_LIST
from custom_components.reminders.diagnostics import async_get_config_entry_diagnostics
from custom_components.reminders.models import (
    ActivationType,
    Reminder,
    ReminderStatus,
)
from custom_components.reminders.triggers.models import TriggerDefinition


class PagingManager:
    """Small manager surface used by list-service and diagnostics tests."""

    def __init__(self, reminders: list[Reminder]) -> None:
        self.reminders = reminders
        self.calls: list[dict[str, Any]] = []
        self.trigger_listener_count = 3
        self.native_trigger_listener_count = 4
        self.native_trigger_failure_count = 2
        self.native_trigger_failures_by_role = {"activation": 2}
        self.scheduled_for = None

    async def async_list_page(self, **kwargs: Any) -> tuple[list[Reminder], int]:
        self.calls.append(dict(kwargs))
        limit = int(kwargs.get("limit", 500))
        offset = int(kwargs.get("offset", 0))
        return self.reminders[offset : offset + limit], len(self.reminders)


class RegisteredServices:
    """Capture Home Assistant service registrations."""

    def __init__(self) -> None:
        self.handlers: dict[str, tuple[Any, Any]] = {}

    def has_service(self, _domain: str, _name: str) -> bool:
        return False

    def async_register(
        self,
        _domain: str,
        name: str,
        handler: Any,
        schema: Any,
        *,
        supports_response: Any,
    ) -> None:
        del supports_response
        self.handlers[name] = (handler, schema)


def _reminder(index: int, **changes: Any) -> Reminder:
    now = datetime.now(UTC) + timedelta(seconds=index)
    reminder = Reminder(
        id=f"reminder-{index:04d}",
        user_id="u1",
        title=f"Reminder {index}",
        due=now,
        created_at=now,
        updated_at=now,
    )
    return reminder.updated(**changes) if changes else reminder


async def test_list_service_exposes_stable_pagination_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PagingManager([_reminder(index) for index in range(1205)])
    registered = RegisteredServices()
    hass = SimpleNamespace(services=registered)
    monkeypatch.setattr(services_module, "_manager", lambda _hass: manager)
    monkeypatch.setattr(
        services_module, "_resolve_list_user", AsyncMock(return_value="u1")
    )

    services_module.async_register_services(hass)  # type: ignore[arg-type]
    handler, schema = registered.handlers[SERVICE_LIST]
    data = schema({"limit": 100, "offset": 1100})
    response = await handler(
        SimpleNamespace(
            data=data,
            return_response=True,
            context=SimpleNamespace(user_id="u1"),
        )
    )

    assert response["total"] == 1205
    assert response["limit"] == 100
    assert response["offset"] == 1100
    assert len(response["reminders"]) == 100
    assert response["reminders"][0]["id"] == "reminder-1100"
    assert manager.calls[-1]["limit"] == 100
    assert manager.calls[-1]["offset"] == 1100

    with pytest.raises(vol.Invalid):
        schema({"limit": 0})
    with pytest.raises(vol.Invalid):
        schema({"limit": 1001})
    with pytest.raises(vol.Invalid):
        schema({"offset": -1})


async def test_diagnostics_pages_all_reminders_and_counts_native_rules() -> None:
    state_trigger = TriggerDefinition.from_dict(
        {"type": "state", "entity_id": "sensor.ready", "to": "on"}
    )
    reminders = [_reminder(index) for index in range(1200)]
    reminders.extend(
        [
            _reminder(
                1200,
                deliver_when=state_trigger,
                deliver_when_summary="Ready",
            ),
            _reminder(
                1201,
                activation_type=ActivationType.TRIGGER,
                status=ReminderStatus.WAITING_FOR_TRIGGER,
                due=None,
                activation_triggers=({"trigger": "state"},),
                delivery_triggers=({"trigger": "state"},),
            ),
            _reminder(
                1202,
                delivery_conditions=({"condition": "state"},),
            ),
            _reminder(
                1203,
                complete_when=state_trigger,
                complete_when_summary="Ready",
            ),
            _reminder(
                1204,
                completion_triggers=({"trigger": "state"},),
            ),
        ]
    )
    manager = PagingManager(reminders)
    entry = SimpleNamespace(runtime_data=manager)

    diagnostics = await async_get_config_entry_diagnostics(
        SimpleNamespace(),  # type: ignore[arg-type]
        entry,  # type: ignore[arg-type]
    )

    assert diagnostics["reminder_count"] == 1205
    assert diagnostics["triggered_count"] == 1
    assert diagnostics["contextual_delivery_count"] == 3
    assert diagnostics["automatic_completion_count"] == 2
    assert diagnostics["native_trigger_failure_count"] == 2
    assert [call["offset"] for call in manager.calls] == [0, 1000]
    assert all(call["limit"] == 1000 for call in manager.calls)
