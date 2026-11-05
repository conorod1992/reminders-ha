"""Exercise the schema boundary through real Home Assistant registries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import llm

from custom_components.reminders import interop_services, services
from custom_components.reminders.const import DOMAIN, SERVICE_LIST, SERVICE_UPSERT
from custom_components.reminders.conversation import RemindersTool
from custom_components.reminders.validation import Schema, vol
from custom_components.reminders.websocket_registration import (
    async_register_websocket_api,
)
from scripts.ha_test_matrix import versions


@pytest.fixture
async def registered_hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    services.async_register_services(hass)
    interop_services.async_register_interop_services(hass)
    async_register_websocket_api(hass)
    yield hass
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


async def test_service_registry_applies_defaults_and_rejects_invalid_inputs(
    registered_hass, monkeypatch
):
    manager = SimpleNamespace(async_list_page=AsyncMock(return_value=([], 0)))
    monkeypatch.setattr(services, "_manager", lambda _hass: manager)
    monkeypatch.setattr(services, "_resolve_list_user", AsyncMock(return_value="u1"))
    result = await registered_hass.services.async_call(
        DOMAIN, SERVICE_LIST, {}, blocking=True, return_response=True
    )
    assert result == {"reminders": [], "total": 0, "limit": 500, "offset": 0}
    assert manager.async_list_page.call_args.kwargs["pending_only"] is False

    for data in ({"limit": 0}, {"pending_only": "not-a-boolean"}):
        with pytest.raises(vol.Invalid):
            await registered_hass.services.async_call(
                DOMAIN, SERVICE_LIST, data, blocking=True, return_response=True
            )
    manager.async_list_page.assert_awaited_once()


async def test_interop_service_uses_native_schema_and_error_translation(
    registered_hass,
):
    schema = registered_hass.services.async_services()[DOMAIN][SERVICE_UPSERT].schema
    assert isinstance(schema, type(cv.PLATFORM_SCHEMA))
    with pytest.raises(vol.Invalid):
        schema({"source": "peer", "source_id": "one", "kind": "invalid", "data": {}})

    @interop_services._translate_errors
    async def invalid(_call):
        cv.boolean("not-a-boolean")

    with pytest.raises(ServiceValidationError):
        await invalid(SimpleNamespace())


async def test_registered_websocket_defaults_and_native_rule_validation(
    registered_hass,
):
    commands = registered_hass.data["websocket_api"]
    _, listing = commands["reminders/list"]
    result = listing({"id": 1, "type": "reminders/list"})
    assert result["scope"] == "mine"
    assert result["limit"] == 500
    with pytest.raises(vol.Invalid):
        listing({"id": 1, "type": "reminders/list", "limit": -1})

    _, rules = commands["reminders/set_native_rules"]
    payload = {"id": 2, "type": "reminders/set_native_rules", "reminder_id": "one"}
    with pytest.raises(vol.Invalid):
        rules(payload)
    assert rules({**payload, "delivery_conditions": []})["delivery_conditions"] == []


def test_schema_and_errors_match_home_assistant_validators():
    assert Schema is type(cv.PLATFORM_SCHEMA)
    assert vol.Schema is Schema
    with pytest.raises(vol.Invalid):
        Schema({"enabled": cv.boolean})({"enabled": "not-a-boolean"})


async def test_conversation_translates_native_validator_errors_before_dispatch():
    handler = AsyncMock(return_value={"ok": True})
    tool = RemindersTool(
        "test", "Test native validation", Schema({"enabled": cv.boolean}), handler
    )
    tool_input = llm.ToolInput(tool_name="test", tool_args={"enabled": "invalid"})
    with pytest.raises(HomeAssistantError):
        await tool.async_call(SimpleNamespace(), tool_input, SimpleNamespace())
    handler.assert_not_awaited()
    tool_input = llm.ToolInput(tool_name="test", tool_args={"enabled": "true"})
    assert await tool.async_call(SimpleNamespace(), tool_input, SimpleNamespace()) == {
        "ok": True
    }
    assert handler.call_args.args[1] == {"enabled": True}


def test_matrix_follows_dependency_target_and_deduplicates_baseline():
    assert versions("homeassistant==2026.10.4\npytest==9.2.0\n") == [
        "2026.7.0",
        "2026.9.4",
        "2026.10.4",
    ]
    assert versions("homeassistant==2026.11.1\n")[-1] == "2026.11.1"
    assert versions("homeassistant==2026.9.4\n") == ["2026.7.0", "2026.9.4"]


@pytest.mark.parametrize(
    "requirements",
    [
        "homeassistant>=2026.9.4\n",
        "homeassistant==2026.9.4\nhomeassistant==2026.10.4\n",
    ],
)
def test_matrix_rejects_ambiguous_or_unpinned_targets(requirements):
    with pytest.raises(ValueError, match="exactly one"):
        versions(requirements)
