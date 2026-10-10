"""Validate the custom integration version against a GitHub release/tag.

Run for every CI event; compare with the tag on tag pushes and releases.
For pull requests and main pushes, also check that the manifest version is valid.
"""

import json
import os
import re
from pathlib import Path

manifest = json.loads(
    Path("custom_components/reminders/manifest.json").read_text(encoding="utf-8")
)
version = manifest["version"]
if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
    raise SystemExit(f"Invalid manifest version: {version!r}; expected X.Y.Z")

event = os.environ.get("EVENT_NAME", "")
tag = (
    os.environ.get("RELEASE_TAG", "")
    if event == "release"
    else os.environ.get("REF_NAME", "")
    if os.environ.get("REF_TYPE") == "tag"
    else ""
)
if event == "release" and not tag:
    raise SystemExit("Release event did not supply a tag")
if tag:
    if re.fullmatch(r"v\d+\.\d+\.\d+", tag) is None:
        raise SystemExit(f"Invalid release tag: {tag!r}; expected vX.Y.Z")
    if version != tag[1:]:
        raise SystemExit(
            f"Release tag {tag} disagrees with integration manifest version {version}"
        )
print(f"Manifest version {version}" + (f" matches {tag}" if tag else " is valid"))
