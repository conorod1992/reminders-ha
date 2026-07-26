"""Shared test helpers."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.reminders.storage import empty_storage


class FakeStore:
    """In-memory Store test double."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.saved: list[dict[str, Any]] = []
        self.delayed = 0

    async def async_load(self) -> dict[str, Any] | None:
        return self.data

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = data
        self.saved.append(data)

    def async_delay_save(self, data_func: Any, delay: float) -> None:
        self.delayed += 1
        self.data = data_func()


class FakeDispatcher:
    """Delivery dispatcher test double."""

    def __init__(self, *, succeeds: bool = True) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.succeeds = succeeds

    async def async_deliver(self, reminder: Any, policy: Any) -> Any:
        from custom_components.reminders.delivery import DeliveryResult

        self.calls.append((reminder, policy))
        return DeliveryResult(
            (policy.channels[0],) if self.succeeds else (),
            () if self.succeeds else ("test: RuntimeError",),
        )


@pytest.fixture
def fake_store() -> FakeStore:
    return FakeStore(empty_storage())
