"""Regression tests for reminder robustness hardening."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from custom_components.reminders.delivery import PersistentNotificationProvider
from custom_components.reminders.models import DeliveryPolicy, Reminder
from custom_components.reminders.native_automation import NativeAutomationRuntime
from custom_components.reminders.persistent_cleanup import (
    async_register_persistent_cleanup,
)

ROOT = Path(__file__).parents[1]


def test_compact_websocket_registration_keeps_series_commands_and_source_filters() -> (
    None
):
    """The view-aware registrar must retain capabilities from the base API."""
    source = (
        ROOT / "custom_components" / "reminders" / "websocket_registration.py"
    ).read_text(encoding="utf-8")

    for command in ("websocket_pause", "websocket_resume", "websocket_skip_next"):
        assert source.count(command) >= 2
    assert 'vol.Optional("source")' in source
    assert 'vol.Optional("source_id")' in source
    assert 'source=msg.get("source")' in source
    assert 'source_id=msg.get("source_id")' in source


def test_frontend_disables_long_lived_module_cache_and_uses_robust_wrapper() -> None:
    source = (ROOT / "custom_components" / "reminders" / "frontend.py").read_text(
        encoding="utf-8"
    )

    assert "StaticPathConfig(STATIC_URL, str(frontend_dir), False)" in source
    assert 'module_url=f"{STATIC_URL}/reminders-panel-robust.js"' in source
    assert "?v=2.2.0" not in source


def test_panel_startup_wrapper_allows_retry_after_transient_failure() -> None:
    source = (
        ROOT
        / "custom_components"
        / "reminders"
        / "frontend"
        / "reminders-panel-robust.js"
    ).read_text(encoding="utf-8")

    assert "this._started = false" in source
    assert 'retry.textContent = "Retry"' in source
    assert 'import "./reminders-panel-attention.js"' in source


def test_native_create_rolls_back_if_rule_save_fails() -> None:
    source = (
        ROOT
        / "custom_components"
        / "reminders"
        / "frontend"
        / "reminders-panel-native.js"
    ).read_text(encoding="utf-8")

    assert (
        'await originalCall.call(this, "delete", { reminder_id: reminderId })' in source
    )
    assert "The incomplete reminder could not be removed automatically" in source


async def test_native_condition_runtime_fails_open_when_checker_raises() -> None:
    """A broken HA condition evaluator must not silently suppress a reminder."""

    async def callback(*_args: Any) -> None:
        return None

    class Checker:
        def async_check(self, variables: dict[str, Any]) -> bool:
            raise RuntimeError("condition engine unavailable")

        def async_unload(self) -> None:
            return None

    condition = {
        "condition": "state",
        "entity_id": "input_boolean.ready",
        "state": "on",
    }
    now = datetime.now(UTC)
    reminder = Reminder(
        id="condition-failure",
        user_id="user",
        title="Do not lose me",
        due=now,
        created_at=now,
        updated_at=now,
        delivery_conditions=(condition,),
    )
    runtime = NativeAutomationRuntime(  # type: ignore[arg-type]
        SimpleNamespace(), callback
    )
    runtime._condition_entries[reminder.id] = SimpleNamespace(
        config_key=(
            '{"condition":"state","entity_id":"input_boolean.ready","state":"on"}',
        ),
        checker=Checker(),
    )

    assert await runtime.async_conditions_match(reminder) is True


async def test_persistent_notifications_are_occurrence_scoped() -> None:
    class Services:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            **kwargs: Any,
        ) -> None:
            self.calls.append((domain, service, data, kwargs))

    services = Services()
    hass = SimpleNamespace(services=services, data={})
    provider = PersistentNotificationProvider(hass)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    reminder = Reminder(
        id="series",
        user_id="user",
        title="Recurring",
        due=now,
        created_at=now,
        updated_at=now,
        current_occurrence_id="occurrence-2",
    )

    await provider.async_deliver(reminder, DeliveryPolicy(("persistent_notification",)))

    assert services.calls[0][2]["notification_id"] == ("reminders_series_occurrence-2")


async def test_resolved_lifecycle_event_dismisses_persistent_notification() -> None:
    """Resolved reminders must not leave stale HA persistent notifications behind."""

    class Bus:
        def __init__(self) -> None:
            self.callback: Any = None

        def async_listen(self, _event_type: str, callback: Any) -> Any:
            self.callback = callback
            return lambda: None

    class Services:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any],
            **kwargs: Any,
        ) -> None:
            self.calls.append((domain, service, data, kwargs))

    bus = Bus()
    services = Services()
    tasks: list[asyncio.Task[Any]] = []

    def create_task(coroutine: Any, _name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    hass = SimpleNamespace(
        bus=bus,
        services=services,
        data={},
        async_create_task=create_task,
    )
    async_register_persistent_cleanup(hass)  # type: ignore[arg-type]
    bus.callback(
        SimpleNamespace(
            data={
                "action": "completed",
                "reminder_id": "abc-123",
                "occurrence_id": "occ-1",
            }
        )
    )
    await asyncio.gather(*tasks)

    dismissed = {call[2]["notification_id"] for call in services.calls}
    assert dismissed == {"reminders_abc-123_occ-1", "reminders_abc-123"}
