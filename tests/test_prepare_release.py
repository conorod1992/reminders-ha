"""Release workflow version preparation contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.prepare_release import parse_version, prepare_release


@pytest.mark.parametrize(
    "version",
    ["1", "v2.8.2", "2.8", "2.8.2rc1", "02.8.2", "2.08.2", "2.8.02", "1.2.3\n"],
)
def test_release_rejects_nonstable_versions(version: str) -> None:
    with pytest.raises(ValueError, match=r"stable X\.Y\.Z"):
        parse_version(version)


def test_release_bumps_manifest_preserving_other_fields(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        '{"domain": "reminders", "version": "2.8.1", "name": "Reminders"}\n'
    )
    prepare_release("2.8.2", manifest_path=path)
    assert json.loads(path.read_text()) == {
        "domain": "reminders",
        "version": "2.8.2",
        "name": "Reminders",
    }
    assert path.read_text().endswith("\n")


@pytest.mark.parametrize("version", ["2.8.1", "2.8.0", "2.7.99", "1.9.99"])
def test_release_never_reuses_or_downgrades_version(
    tmp_path: Path, version: str
) -> None:
    path = tmp_path / "manifest.json"
    original = '{"version": "2.8.1"}\n'
    path.write_text(original)
    with pytest.raises(ValueError, match="newer"):
        prepare_release(version, manifest_path=path)
    assert path.read_text() == original


def test_release_rejects_ambiguous_version_field(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"version": "2.8.1", "nested": {"version": "2.8.1"}}\n')
    with pytest.raises(ValueError, match="exactly one"):
        prepare_release("2.8.2", manifest_path=path)
