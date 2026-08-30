"""Release-facing metadata and documentation contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from custom_components.reminders import const
from custom_components.reminders.models import WhileAwaitingAcknowledgement

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "reminders"


def _public_services() -> set[str]:
    return {
        value
        for name, value in vars(const).items()
        if name.startswith("SERVICE_") and isinstance(value, str)
    }


def _select_values(field: dict[str, Any]) -> set[str]:
    options = field["selector"]["select"]["options"]
    return {
        str(option["value"] if isinstance(option, dict) else option)
        for option in options
    }


def test_all_registered_services_have_action_metadata_and_icons() -> None:
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text(encoding="utf-8"))
    icons = json.loads((INTEGRATION / "icons.json").read_text(encoding="utf-8"))[
        "services"
    ]
    expected = _public_services()

    assert expected == set(services)
    assert expected == set(icons)


def test_triggered_acknowledgement_options_match_runtime_enum() -> None:
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text(encoding="utf-8"))
    expected = set(WhileAwaitingAcknowledgement)

    for service in ("create_triggered", "update"):
        field = services[service]["fields"]["while_awaiting_acknowledgement"]
        assert _select_values(field) == expected


def test_readme_names_current_storage_schema() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"Storage schema {const.STORAGE_VERSION}.{const.STORAGE_MINOR_VERSION}" in readme
