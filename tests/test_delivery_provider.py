"""Provider-level tests for generic and Companion App notification calls."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.reminders.delivery import (
    PERSISTENT_NOTIFICATION_MESSAGE,
    PERSISTENT_NOTIFICATION_TITLE,
    NotifyProvider,
    PersistentNotificationProvider,
)
from custom_components.reminders.models import DeliveryPolicy, Reminder


class Services:
    """Capture real provider call construction and optionally reject actions."""

    def __init__(self, *, reject_actions: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        self.reject_actions = reject_actions or set()

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], **kwargs: Any
    ) -> None:
        self.calls.append((domain, service, data, kwargs))
        if service in self.reject_actions and "data" in data:
            raise ServiceValidationError("actions unsupported")


def reminder(*, include_done: bool = True) -> Reminder:
    now = datetime.now(UTC)
    actions = [
        {"action": "token:S10", "title": "Snooze 10 minutes"},
        {"action": "token:S60", "title": "Snooze 1 hour"},
    ]
    if include_done:
        actions.append({"action": "token:DONE", "title": "Done"})
    return Reminder(
        id="reminder",
        user_id="user",
        title="Take medicine",
        message="Now",
        due=now,
        created_at=now,
        updated_at=now,
        notification_actions=tuple(actions),
    )


async def test_generic_notify_entity_never_receives_actions() -> None:
    services = Services()
    provider = NotifyProvider(SimpleNamespace(services=services))  # type: ignore[arg-type]
    await provider.async_deliver(
        reminder(), DeliveryPolicy(("phone",), ("notify.kitchen",))
    )

    assert len(services.calls) == 1
    domain, service, data, kwargs = services.calls[0]
    assert (domain, service, data) == (
        "notify",
        "send_message",
        {"title": "Take medicine", "message": "Now"},
    )
    assert kwargs["target"] == {"entity_id": ["notify.kitchen"]}
    assert kwargs["blocking"] is True
    assert kwargs["context"].user_id == "user"


async def test_mobile_service_receives_actions_without_done_when_not_required() -> None:
    services = Services()
    provider = NotifyProvider(SimpleNamespace(services=services))  # type: ignore[arg-type]
    await provider.async_deliver(
        reminder(include_done=False),
        DeliveryPolicy(
            channels=("phone",),
            mobile_app_services=("notify.mobile_app_conor",),
        ),
    )

    data = services.calls[0][2]
    assert services.calls[0][1] == "mobile_app_conor"
    assert {action["title"] for action in data["data"]["actions"]} == {
        "Snooze 10 minutes",
        "Snooze 1 hour",
    }


async def test_mobile_action_rejection_falls_back_to_ordinary_notification() -> None:
    services = Services(reject_actions={"mobile_app_conor"})
    provider = NotifyProvider(SimpleNamespace(services=services))  # type: ignore[arg-type]
    await provider.async_deliver(
        reminder(),
        DeliveryPolicy(
            channels=("phone",),
            mobile_app_services=("notify.mobile_app_conor",),
        ),
    )

    assert len(services.calls) == 2
    assert "data" in services.calls[0][2]
    assert services.calls[1][2] == {"title": "Take medicine", "message": "Now"}


async def test_mixed_generic_and_mobile_targets_use_their_supported_shapes() -> None:
    services = Services(reject_actions={"mobile_app_tablet"})
    provider = NotifyProvider(SimpleNamespace(services=services))  # type: ignore[arg-type]
    await provider.async_deliver(
        reminder(),
        DeliveryPolicy(
            channels=("phone",),
            notify_targets=("notify.hall",),
            mobile_app_services=(
                "notify.mobile_app_conor",
                "notify.mobile_app_tablet",
            ),
        ),
    )

    assert [call[1] for call in services.calls] == [
        "send_message",
        "mobile_app_conor",
        "mobile_app_tablet",
        "mobile_app_tablet",
    ]
    assert "data" not in services.calls[0][2]
    assert "data" in services.calls[1][2]
    assert "data" not in services.calls[-1][2]


async def test_all_phone_targets_failing_raises() -> None:
    class FailingServices(Services):
        async def async_call(
            self, domain: str, service: str, data: dict[str, Any], **kwargs: Any
        ) -> None:
            raise RuntimeError("offline")

    provider = NotifyProvider(  # type: ignore[arg-type]
        SimpleNamespace(services=FailingServices())
    )
    with pytest.raises(RuntimeError, match="mobile_app_conor"):
        await provider.async_deliver(
            reminder(),
            DeliveryPolicy(
                channels=("phone",),
                mobile_app_services=("notify.mobile_app_conor",),
            ),
        )


async def test_persistent_notification_omits_private_reminder_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = Services()

    async def track(*_args: object) -> None:
        return None

    monkeypatch.setattr(
        "custom_components.reminders.delivery.async_track_persistent_notification",
        track,
    )
    provider = PersistentNotificationProvider(  # type: ignore[arg-type]
        SimpleNamespace(services=services)
    )
    private = reminder().updated(
        title="Private medical appointment",
        message="Sensitive details that other HA users must not see",
    )

    await provider.async_deliver(
        private,
        DeliveryPolicy(("persistent_notification",)),
    )

    assert len(services.calls) == 1
    domain, service, data, kwargs = services.calls[0]
    assert (domain, service) == ("persistent_notification", "create")
    assert data["title"] == PERSISTENT_NOTIFICATION_TITLE
    assert data["message"] == PERSISTENT_NOTIFICATION_MESSAGE
    assert private.title not in data["title"]
    assert private.title not in data["message"]
    assert private.message not in data["title"]
    assert private.message not in data["message"]
    assert data["notification_id"].startswith("reminders_")
    assert kwargs == {"blocking": True}
