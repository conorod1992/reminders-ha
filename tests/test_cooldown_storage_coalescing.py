"""Tests for cooldown-only storage write coalescing."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.helpers.storage import Store

from custom_components.reminders.const import SAVE_DELAY
from custom_components.reminders.storage import (
    ReminderStore,
    StoredData,
    _is_cooldown_counter_only_update,
)


def _data(
    count: int,
    *,
    updated_at: str = "2026-09-03T02:00:00+00:00",
    status: str = "waiting_for_trigger",
    trigger_duration_waits: list[dict[str, Any]] | None = None,
) -> StoredData:
    return {
        "reminders": {
            "reminder": {
                "id": "reminder",
                "status": status,
                "cooldown_skip_count": count,
                "updated_at": updated_at,
                "trigger_duration_waits": trigger_duration_waits or [],
            }
        },
        "users": {},
    }


def test_only_cooldown_counter_and_timestamp_can_be_coalesced() -> None:
    previous = _data(10)
    current = _data(11, updated_at="2026-09-03T02:00:01+00:00")

    assert _is_cooldown_counter_only_update(previous, current) is True


def test_real_reminder_changes_are_never_coalesced() -> None:
    previous = _data(10)

    assert (
        _is_cooldown_counter_only_update(
            previous,
            _data(
                11,
                updated_at="2026-09-03T02:00:01+00:00",
                trigger_duration_waits=[{"role": "activation"}],
            ),
        )
        is False
    )
    assert (
        _is_cooldown_counter_only_update(previous, _data(11, status="expired"))
        is False
    )
    assert (
        _is_cooldown_counter_only_update(
            previous,
            _data(10, updated_at="2026-09-03T02:00:01+00:00"),
        )
        is False
    )


async def test_cooldown_only_save_is_debounced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _data(4)
    current = _data(5, updated_at="2026-09-03T02:00:01+00:00")
    store = object.__new__(ReminderStore)
    store._last_requested_data = previous
    delayed: list[tuple[Any, float]] = []
    immediate: list[StoredData] = []

    def delay_save(data_func: Any, delay: float) -> None:
        delayed.append((data_func, delay))

    async def immediate_save(_self: Any, data: StoredData) -> None:
        immediate.append(data)

    store.async_delay_save = delay_save  # type: ignore[method-assign]
    monkeypatch.setattr(Store, "async_save", immediate_save)

    await store.async_save(current)

    assert immediate == []
    assert len(delayed) == 1
    data_func, delay = delayed[0]
    assert delay == SAVE_DELAY
    assert data_func() is current


async def test_meaningful_save_supersedes_pending_cooldown_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _data(4)
    cooldown = _data(5, updated_at="2026-09-03T02:00:01+00:00")
    meaningful = _data(
        5,
        updated_at="2026-09-03T02:00:02+00:00",
        status="expired",
    )
    store = object.__new__(ReminderStore)
    store._last_requested_data = previous
    delayed: list[Any] = []
    immediate: list[StoredData] = []

    def delay_save(data_func: Any, _delay: float) -> None:
        delayed.append(data_func)

    async def immediate_save(_self: Any, data: StoredData) -> None:
        immediate.append(data)

    store.async_delay_save = delay_save  # type: ignore[method-assign]
    monkeypatch.setattr(Store, "async_save", immediate_save)

    await store.async_save(cooldown)
    await store.async_save(meaningful)

    assert immediate == [meaningful]
    assert delayed[0]() is meaningful
