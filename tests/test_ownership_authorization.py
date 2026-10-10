"""Regression coverage for ownership checks at mutation commit time."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.reminders.manager import (
    ReminderManager,
    ReminderValidationError,
)

from .conftest import FakeDispatcher, FakeStore


async def test_stale_owner_cannot_update_or_delete_after_transfer(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "custom_components.reminders.manager.async_track_point_in_utc_time",
        lambda _hass, _callback, _when: lambda: None,
    )
    manager = ReminderManager(
        SimpleNamespace(),
        fake_store,
        FakeDispatcher(),  # type: ignore[arg-type]
    )
    await manager.async_load()
    reminder = await manager.async_create(
        user_id="u1",
        title="Owned by one",
        due=datetime.now(UTC) + timedelta(hours=1),
    )

    transferred = await manager.async_update(reminder.id, user_id="u2")
    assert transferred.user_id == "u2"

    with pytest.raises(ReminderValidationError, match="ownership changed"):
        await manager.async_update(
            reminder.id, expected_user_id="u1", title="stale update"
        )
    with pytest.raises(ReminderValidationError, match="ownership changed"):
        await manager.async_delete(reminder.id, expected_user_id="u1")

    current = await manager.async_get(reminder.id)
    assert current.user_id == "u2"
    assert current.title == "Owned by one"

    updated = await manager.async_update(
        reminder.id, expected_user_id="u2", title="current owner update"
    )
    assert updated.title == "current owner update"


def _guarded_calls(path: str, methods: set[str]) -> list[ast.Call]:
    tree = ast.parse(Path(path).read_text())
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else node.func.id
            if isinstance(node.func, ast.Name)
            else None
        )
        if name not in methods:
            continue
        positional = [ast.unparse(value) for value in node.args]
        if "reminder.id" not in positional[:2]:
            continue
        calls.append(node)
    return calls


def test_authenticated_mutation_routes_pass_expected_owner() -> None:
    targets = {
        "custom_components/reminders/services.py": {
            "async_update",
            "async_delete",
            "async_wait_for_next_trigger",
            "async_snooze",
            "async_acknowledge",
            "async_complete",
            "async_select_external_action",
        },
        "custom_components/reminders/interop_services.py": {
            "async_pause",
            "async_resume",
            "async_skip_next",
            "async_set_native_rules",
        },
        "custom_components/reminders/websocket_api.py": {
            "async_update",
            "async_delete",
            "async_pause",
            "async_resume",
            "async_skip_next",
            "async_wait_for_next_trigger",
            "async_snooze",
            "async_acknowledge",
            "async_complete",
            "async_select_external_action",
        },
        "custom_components/reminders/conversation.py": {
            "async_update",
            "async_delete",
            "async_wait_for_next_trigger",
            "async_snooze",
            "async_acknowledge",
            "async_complete",
        },
        "custom_components/reminders/native_websocket.py": {
            "async_set_native_rules",
            "async_update_native_triggered",
            "async_update_native_scheduled",
        },
    }
    for path, methods in targets.items():
        calls = _guarded_calls(path, methods)
        assert calls, path
        for call in calls:
            expected = next(
                (
                    item.value
                    for item in call.keywords
                    if item.arg == "expected_user_id"
                ),
                None,
            )
            assert expected is not None, (
                f"missing owner guard in {path}: {ast.unparse(call)}"
            )
            assert ast.unparse(expected) == "reminder.user_id"


def test_mobile_capability_path_captures_owner_before_unlock() -> None:
    source = Path("custom_components/reminders/manager.py").read_text()
    assert "match = (reminder.id, occurrence.id, reminder.user_id)" in source
    assert "reminder_id, occurrence_id, expected_user_id = match" in source
    assert source.count("expected_user_id=expected_user_id") >= 4
