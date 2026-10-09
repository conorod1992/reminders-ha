"""Provider-level tests for generic and Companion App notification calls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.reminders.delivery import (
    PERSISTENT_NOTIFICATION_MESSAGE,
    PERSISTENT_NOTIFICATION_TITLE,
    DeliveryDispatcher,
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
    assert data["data"]["url"] == "/reminders"
    assert data["data"]["tag"].startswith("reminders_reminder_")
    assert {action["title"] for action in data["data"]["actions"]} == {
        "Snooze 10 minutes",
        "Snooze 1 hour",
    }


async def test_repeated_mobile_delivery_uses_the_same_occurrence_tag() -> None:
    services = Services()
    provider = NotifyProvider(SimpleNamespace(services=services))  # type: ignore[arg-type]
    source = reminder()
    policy = DeliveryPolicy(
        channels=("phone",),
        mobile_app_services=("notify.mobile_app_conor",),
    )
    await provider.async_deliver(source, policy)
    await provider.async_deliver(source, policy)
    assert services.calls[0][2]["data"]["tag"] == services.calls[1][2]["data"]["tag"]
    assert services.calls[0][2]["data"]["url"] == "/reminders"


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


async def test_partial_phone_target_failure_preserves_actionable_success() -> None:
    class PartialServices(Services):
        async def async_call(
            self, domain: str, service: str, data: dict[str, Any], **kwargs: Any
        ) -> None:
            self.calls.append((domain, service, data, kwargs))
            if service == "mobile_app_tablet":
                raise RuntimeError("offline")

    provider = NotifyProvider(  # type: ignore[arg-type]
        SimpleNamespace(services=PartialServices())
    )
    dispatcher = DeliveryDispatcher([provider])
    result = await dispatcher.async_deliver(
        reminder(),
        DeliveryPolicy(
            channels=("phone",),
            mobile_app_services=(
                "notify.mobile_app_conor",
                "notify.mobile_app_tablet",
            ),
        ),
    )

    assert result.succeeded == ("phone",)
    assert result.failed_channels == ("phone",)
    assert result.errors == ("phone: notify.mobile_app_tablet: RuntimeError",)


async def test_dispatcher_times_out_hung_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HungProvider:
        channel = "phone"

        async def async_deliver(
            self, reminder: Reminder, policy: DeliveryPolicy
        ) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "custom_components.reminders.delivery.DELIVERY_TIMEOUT_SECONDS", 0.01
    )
    dispatcher = DeliveryDispatcher([HungProvider()])  # type: ignore[list-item]
    result = await dispatcher.async_deliver(
        reminder(), DeliveryPolicy(channels=("phone",))
    )

    assert result.succeeded == ()
    assert result.failed_channels == ("phone",)
    assert result.errors == ("phone: TimeoutError",)


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


async def test_mobile_actions_prioritise_done_dismiss_and_one_snooze() -> None:
    services = Services()
    provider = NotifyProvider(SimpleNamespace(services=services))  # type: ignore[arg-type]
    source = reminder().updated(
        notification_actions=(
            {"action": "token:SNOOZE_60", "title": "Snooze 1 hour"},
            {"action": "token:SNOOZE_10", "title": "Snooze 10 min"},
            {"action": "token:DISMISS", "title": "Dismiss"},
            {"action": "token:DONE", "title": "Done"},
            {"action": "token:EXTERNAL_other", "title": "Other"},
        ),
    )
    await provider.async_deliver(
        source,
        DeliveryPolicy(
            channels=("phone",), mobile_app_services=("notify.mobile_app_conor",)
        ),
    )
    actions = services.calls[0][2]["data"]["actions"]
    assert [item["title"] for item in actions] == ["Done", "Dismiss", "Snooze 10 min"]
    assert len(actions) == 3


async def test_voice_announcement_includes_title_and_notes() -> None:
    from custom_components.reminders.delivery import VoiceProvider

    services = Services()
    provider = VoiceProvider(SimpleNamespace(services=services))  # type: ignore[arg-type]
    source = reminder().updated(
        title="Take parcel to the post office", message="Label is in the kitchen drawer"
    )
    await provider.async_deliver(
        source,
        DeliveryPolicy(
            channels=("voice",), voice_targets=("assist_satellite.kitchen",)
        ),
    )
    assert (
        services.calls[0][2]["message"]
        == "Take parcel to the post office. Label is in the kitchen drawer"
    )


async def test_a_failing_generic_notify_entity_does_not_block_another() -> None:
    class SelectiveServices(Services):
        async def async_call(
            self, domain: str, service: str, data: dict[str, Any], **kwargs: Any
        ) -> None:
            self.calls.append((domain, service, data, kwargs))
            if kwargs.get("target") == {"entity_id": ["notify.offline"]}:
                raise RuntimeError("device unavailable")

    services = SelectiveServices()
    provider = NotifyProvider(SimpleNamespace(services=services))  # type: ignore[arg-type]
    outcome = await provider.async_deliver(
        reminder(),
        DeliveryPolicy(
            channels=("phone",),
            notify_targets=("notify.offline", "notify.healthy"),
        ),
    )
    assert outcome is not None
    assert outcome.errors == ("notify.offline: RuntimeError",)
    assert [item[3]["target"]["entity_id"][0] for item in services.calls] == [
        "notify.offline",
        "notify.healthy",
    ]


async def test_phone_channel_allows_each_target_its_own_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class SlowServices(Services):
        async def async_call(
            self, domain: str, service: str, data: dict[str, Any], **kwargs: Any
        ) -> None:
            calls.append(service)
            if service == "mobile_app_hung":
                await asyncio.sleep(0.08)

    monkeypatch.setattr(
        "custom_components.reminders.delivery.DELIVERY_TIMEOUT_SECONDS", 0.03
    )
    dispatcher = DeliveryDispatcher(
        [NotifyProvider(SimpleNamespace(services=SlowServices()))]  # type: ignore[list-item]
    )
    result = await dispatcher.async_deliver(
        reminder(),
        DeliveryPolicy(
            channels=("phone",),
            mobile_app_services=("notify.mobile_app_hung", "notify.mobile_app_healthy"),
        ),
    )
    assert calls == ["mobile_app_hung", "mobile_app_healthy"]
    assert result.failed_channels == ("phone",)
