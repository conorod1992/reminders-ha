"""Regression tests for complete, filter-correct reminder pagination."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.reminders.manager import ReminderManager
from custom_components.reminders.models import Reminder, ReminderStatus
from custom_components.reminders.websocket_registration import _upcoming_page

from .conftest import FakeDispatcher, FakeStore


def _manager() -> ReminderManager:
    hass = SimpleNamespace(
        states={},
        bus=SimpleNamespace(),
        config=SimpleNamespace(time_zone="UTC"),
        create_task=lambda coroutine, _name=None: coroutine.close(),
    )
    return ReminderManager(
        hass,
        FakeStore(),
        FakeDispatcher(),  # type: ignore[arg-type]
    )


def _reminder(index: int, *, status: ReminderStatus, due: datetime) -> Reminder:
    return Reminder(
        id=f"reminder-{index:04d}",
        user_id="user",
        title=f"Reminder {index}",
        due=due,
        created_at=due,
        updated_at=due,
        status=status,
    )


async def test_list_page_reports_total_before_pagination() -> None:
    manager = _manager()
    now = datetime.now(UTC)
    manager._reminders = {
        reminder.id: reminder
        for index in range(1205)
        if (
            reminder := _reminder(
                index,
                status=ReminderStatus.PENDING,
                due=now + timedelta(seconds=index),
            )
        )
    }

    page, total = await manager.async_list_page(limit=100, offset=1100)

    assert total == 1205
    assert len(page) == 100
    assert page[0].id == "reminder-1100"
    assert page[-1].id == "reminder-1199"


async def test_upcoming_filters_before_paging_across_backend_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    now = datetime.now(UTC)
    reminders = [
        _reminder(
            index,
            status=ReminderStatus.DELIVERED,
            due=now + timedelta(seconds=index),
        )
        for index in range(1001)
    ]
    reminders.extend(
        [
            _reminder(
                1001,
                status=ReminderStatus.PENDING,
                due=now + timedelta(seconds=1001),
            ),
            _reminder(
                1002,
                status=ReminderStatus.PENDING,
                due=now + timedelta(seconds=1002),
            ),
        ]
    )
    manager._reminders = {item.id: item for item in reminders}
    monkeypatch.setattr(
        "custom_components.reminders.websocket_registration._manager",
        lambda _hass: manager,
    )

    page, total = await _upcoming_page(
        SimpleNamespace(),  # type: ignore[arg-type]
        user_id="user",
        query=None,
        due_after=None,
        due_before=None,
        source=None,
        source_id=None,
        limit=1,
        offset=0,
    )

    assert total == 2
    assert [item.id for item in page] == ["reminder-1001"]

    second, second_total = await _upcoming_page(
        SimpleNamespace(),  # type: ignore[arg-type]
        user_id="user",
        query=None,
        due_after=None,
        due_before=None,
        source=None,
        source_id=None,
        limit=1,
        offset=1,
    )
    assert second_total == 2
    assert [item.id for item in second] == ["reminder-1002"]
