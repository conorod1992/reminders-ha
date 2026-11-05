"""Keep Python and browser coverage aligned with the pinned Home Assistant."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements-test.txt"
MINIMUM_VERSION = "2026.7.0"
BASELINE_VERSION = "2026.9.4"


def versions(requirements: str) -> list[str]:
    """Include supported legacy backends and the exact dependency-update target."""
    pins = re.findall(r"^homeassistant==([0-9]+\.[0-9]+\.[0-9]+)$", requirements, re.M)
    if len(pins) != 1:
        raise ValueError("Expected exactly one stable homeassistant==version pin")
    return list(dict.fromkeys((MINIMUM_VERSION, BASELINE_VERSION, pins[0])))


def main() -> None:
    """Emit a shared matrix or requirements for one of its isolated environments."""
    requirements = REQUIREMENTS.read_text(encoding="utf-8")
    matrix_versions = versions(requirements)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", choices=matrix_versions)
    args = parser.parse_args()
    if args.requirements:
        print(
            re.sub(
                r"^homeassistant==[^\n]+",
                f"homeassistant=={args.requirements}",
                requirements,
                flags=re.M,
            ),
            end="",
        )
    else:
        print(json.dumps({"homeassistant": matrix_versions}))


if __name__ == "__main__":
    main()
