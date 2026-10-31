"""Validate and stage a HACS-compatible Reminders release version."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

MANIFEST = Path("custom_components/reminders/manifest.json")
VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def parse_version(value: str) -> tuple[int, int, int]:
    """Accept stable X.Y.Z versions only (no prefixes or leading zeros)."""
    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"Expected a stable X.Y.Z version, got {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def prepare_release(version: str, *, manifest_path: Path = MANIFEST) -> None:
    """Advance only the HA manifest version; retain other metadata and formatting."""
    target = parse_version(version)
    content = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(content)
    current = manifest["version"]
    if target <= parse_version(current):
        raise ValueError(f"Release {version} must be newer than manifest {current}")
    # Preserve the existing JSON formatting/field order; refuse ambiguous inputs.
    pattern = r'("version"\s*:\s*")' + re.escape(current) + r'(")'
    revised, replacements = re.subn(pattern, lambda m: m[1] + version + m[2], content)
    if replacements != 1:
        raise ValueError("Expected exactly one matching manifest version entry")
    if json.loads(revised)["version"] != version:
        raise ValueError("Failed to update manifest version")
    manifest_path.write_text(revised, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="New stable X.Y.Z version (without v)")
    args = parser.parse_args()
    prepare_release(args.version)
    print(f"Prepared Reminders v{args.version}")


if __name__ == "__main__":
    main()
