"""Release-facing metadata and documentation contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from custom_components.reminders import const

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "reminders"


def _public_services() -> set[str]:
    return {
        value
        for name, value in vars(const).items()
        if name.startswith("SERVICE_") and isinstance(value, str)
    }


def test_all_registered_services_have_action_metadata_and_icons() -> None:
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text(encoding="utf-8"))
    icons = json.loads((INTEGRATION / "icons.json").read_text(encoding="utf-8"))[
        "services"
    ]
    expected = _public_services()

    assert expected <= set(services)
    assert expected <= set(icons)


def test_readme_names_current_storage_schema() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"Storage schema {const.STORAGE_VERSION}.{const.STORAGE_MINOR_VERSION}" in readme
