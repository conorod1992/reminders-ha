"""Typed, persistable trigger definitions and validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

MAX_TRIGGER_ID_LENGTH = 128
MAX_EVENT_DATA_KEYS = 32
MAX_EVENT_DATA_DEPTH = 4
MAX_EVENT_DATA_BYTES = 4096
MAX_FOR_SECONDS = 31_536_000
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class TriggerValidationError(ValueError):
    """Raised when a trigger definition is malformed."""


class TriggerType(StrEnum):
    """Supported integration-owned trigger types."""

    STATE = "state"
    NUMERIC_STATE = "numeric_state"
    ZONE = "zone"
    EVENT = "event"
    NAMED = "named"


class ZoneEvent(StrEnum):
    """Supported zone transitions."""

    ENTER = "enter"
    LEAVE = "leave"


@dataclass(frozen=True, slots=True)
class TriggerDefinition:
    """Canonical trigger data; fields are restricted by ``type``."""

    type: TriggerType
    entity_id: str | None = None
    from_value: str | None = None
    to_value: str | None = None
    attribute: str | None = None
    for_seconds: int = 0
    above: float | None = None
    below: float | None = None
    zone_entity_id: str | None = None
    event: ZoneEvent | None = None
    event_type: str | None = None
    event_data: dict[str, Any] | None = None
    trigger_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize only fields meaningful to this trigger type."""
        data: dict[str, Any] = {"type": self.type.value}
        if self.type is TriggerType.STATE:
            data["entity_id"] = self.entity_id
            if self.from_value is not None:
                data["from"] = self.from_value
            if self.to_value is not None:
                data["to"] = self.to_value
            if self.attribute is not None:
                data["attribute"] = self.attribute
            if self.for_seconds:
                data["for_seconds"] = self.for_seconds
        elif self.type is TriggerType.NUMERIC_STATE:
            data["entity_id"] = self.entity_id
            if self.above is not None:
                data["above"] = self.above
            if self.below is not None:
                data["below"] = self.below
            if self.attribute is not None:
                data["attribute"] = self.attribute
            if self.for_seconds:
                data["for_seconds"] = self.for_seconds
        elif self.type is TriggerType.ZONE:
            data.update(
                entity_id=self.entity_id,
                zone_entity_id=self.zone_entity_id,
                event=self.event.value if self.event else None,
            )
        elif self.type is TriggerType.EVENT:
            data["event_type"] = self.event_type
            if self.event_data:
                data["event_data"] = self.event_data
        elif self.type is TriggerType.NAMED:
            data["trigger_id"] = self.trigger_id
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Validate and canonicalise a stored/API trigger definition."""
        if not isinstance(raw, dict):
            raise TriggerValidationError("Trigger must be an object")
        try:
            trigger_type = TriggerType(str(raw.get("type", "")).strip().lower())
        except ValueError as err:
            raise TriggerValidationError("Unsupported trigger type") from err
        allowed = {
            TriggerType.STATE: {
                "type",
                "entity_id",
                "from",
                "to",
                "attribute",
                "for_seconds",
            },
            TriggerType.NUMERIC_STATE: {
                "type",
                "entity_id",
                "above",
                "below",
                "attribute",
                "for_seconds",
            },
            TriggerType.ZONE: {"type", "entity_id", "zone_entity_id", "event"},
            TriggerType.EVENT: {"type", "event_type", "event_data"},
            TriggerType.NAMED: {"type", "trigger_id"},
        }[trigger_type]
        unknown = set(raw) - allowed
        if unknown:
            raise TriggerValidationError(
                f"Unsupported trigger fields: {sorted(unknown)}"
            )

        if trigger_type is TriggerType.STATE:
            entity_id = _entity_id(raw.get("entity_id"), "entity_id")
            attribute = _optional_nonempty(raw.get("attribute"), "attribute")
            from_value = _optional_string(raw, "from")
            to_value = _optional_string(raw, "to")
            if from_value is None and to_value is None and attribute is None:
                raise TriggerValidationError(
                    "State trigger needs from, to, or an observed attribute"
                )
            return cls(
                trigger_type,
                entity_id=entity_id,
                from_value=from_value,
                to_value=to_value,
                attribute=attribute,
                for_seconds=_duration(raw.get("for_seconds", 0)),
            )
        if trigger_type is TriggerType.NUMERIC_STATE:
            above = _optional_float(raw.get("above"), "above")
            below = _optional_float(raw.get("below"), "below")
            if above is None and below is None:
                raise TriggerValidationError(
                    "Numeric-state trigger needs above or below"
                )
            if above is not None and below is not None and above >= below:
                raise TriggerValidationError("above must be less than below")
            return cls(
                trigger_type,
                entity_id=_entity_id(raw.get("entity_id"), "entity_id"),
                attribute=_optional_nonempty(raw.get("attribute"), "attribute"),
                for_seconds=_duration(raw.get("for_seconds", 0)),
                above=above,
                below=below,
            )
        if trigger_type is TriggerType.ZONE:
            try:
                event = ZoneEvent(str(raw.get("event", "")).strip().lower())
            except ValueError as err:
                raise TriggerValidationError(
                    "Zone event must be enter or leave"
                ) from err
            zone_entity_id = _entity_id(raw.get("zone_entity_id"), "zone_entity_id")
            if not zone_entity_id.startswith("zone."):
                raise TriggerValidationError("zone_entity_id must be a zone entity")
            entity_id = _entity_id(raw.get("entity_id"), "entity_id")
            if not entity_id.startswith(("person.", "device_tracker.")):
                raise TriggerValidationError(
                    "Zone trigger entity must be a person or device tracker"
                )
            return cls(
                trigger_type,
                entity_id=entity_id,
                zone_entity_id=zone_entity_id,
                event=event,
            )
        if trigger_type is TriggerType.EVENT:
            event_type = _identifier(raw.get("event_type"), "event_type")
            event_data = raw.get("event_data")
            if event_data is not None:
                if not isinstance(event_data, dict):
                    raise TriggerValidationError("event_data must be an object")
                _validate_json(event_data)
                event_data = _canonical_json_value(event_data)
            return cls(trigger_type, event_type=event_type, event_data=event_data)
        return cls(
            trigger_type,
            trigger_id=_identifier(raw.get("trigger_id"), "trigger_id"),
        )


def canonical_trigger_key(trigger: TriggerDefinition) -> str:
    """Return a deterministic key for equivalent trigger definitions."""
    return json.dumps(
        trigger.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def trigger_summary(
    trigger: TriggerDefinition, names: dict[str, str] | None = None
) -> str:
    """Generate a stable human-readable summary for APIs and history."""
    names = names or {}
    entity = names.get(trigger.entity_id or "", trigger.entity_id or "entity")
    if trigger.type is TriggerType.STATE:
        source = f"{entity} {trigger.attribute}" if trigger.attribute else entity
        if trigger.from_value is not None and trigger.to_value is not None:
            return (
                f"When {source} changes from {trigger.from_value} to {trigger.to_value}"
            )
        if trigger.to_value is not None:
            return f"When {source} changes to {trigger.to_value}"
        if trigger.from_value is not None:
            return f"When {source} changes from {trigger.from_value}"
        return f"When {source} changes"
    if trigger.type is TriggerType.NUMERIC_STATE:
        source = f"{entity} {trigger.attribute}" if trigger.attribute else entity
        if trigger.above is not None and trigger.below is not None:
            return f"When {source} is between {trigger.above:g} and {trigger.below:g}"
        if trigger.above is not None:
            return f"When {source} rises above {trigger.above:g}"
        return f"When {source} drops below {trigger.below:g}"
    if trigger.type is TriggerType.ZONE:
        zone = names.get(
            trigger.zone_entity_id or "", trigger.zone_entity_id or "the zone"
        )
        verb = "enters" if trigger.event is ZoneEvent.ENTER else "leaves"
        return f"When {entity} {verb} {zone}"
    if trigger.type is TriggerType.EVENT:
        suffix = ""
        if trigger.event_data:
            pairs = ", ".join(
                f"{key}: {value}" for key, value in trigger.event_data.items()
            )
            suffix = f" matches {pairs}"
        return f"When event {trigger.event_type}{suffix}"
    return f"When named trigger {trigger.trigger_id} fires"


def event_data_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Return whether actual event data recursively contains expected values."""
    for key, expected_value in expected.items():
        if key not in actual:
            return False
        actual_value = actual[key]
        if isinstance(expected_value, dict):
            if not isinstance(actual_value, dict) or not event_data_matches(
                expected_value, actual_value
            ):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _entity_id(value: Any, field_name: str) -> str:
    result = str(value or "").strip().lower()
    if not result or "." not in result or any(char.isspace() for char in result):
        raise TriggerValidationError(f"{field_name} must be a valid entity ID")
    return result


def _identifier(value: Any, field_name: str) -> str:
    result = str(value or "").strip().lower()
    if (
        not result
        or len(result) > MAX_TRIGGER_ID_LENGTH
        or not _IDENTIFIER.fullmatch(result)
    ):
        raise TriggerValidationError(
            f"{field_name} must use lowercase letters, digits, underscores, "
            "dots, or hyphens"
        )
    return result


def _optional_string(raw: dict[str, Any], key: str) -> str | None:
    if key not in raw or raw[key] is None:
        return None
    return str(raw[key])


def _optional_nonempty(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        raise TriggerValidationError(f"{field_name} must not be empty")
    return result


def _optional_float(value: Any, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as err:
        raise TriggerValidationError(f"{field_name} must be numeric") from err
    if result != result or result in (float("inf"), float("-inf")):
        raise TriggerValidationError(f"{field_name} must be finite")
    return result


def _duration(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as err:
        raise TriggerValidationError("for_seconds must be an integer") from err
    if result < 0 or result > MAX_FOR_SECONDS:
        raise TriggerValidationError(
            f"for_seconds must be between 0 and {MAX_FOR_SECONDS}"
        )
    return result


def _validate_json(value: dict[str, Any]) -> None:
    if len(value) > MAX_EVENT_DATA_KEYS:
        raise TriggerValidationError("event_data has too many keys")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as err:
        raise TriggerValidationError("event_data must be JSON-serialisable") from err
    if len(encoded.encode()) > MAX_EVENT_DATA_BYTES:
        raise TriggerValidationError("event_data is too large")

    def visit(item: Any, depth: int) -> None:
        if depth > MAX_EVENT_DATA_DEPTH:
            raise TriggerValidationError("event_data is nested too deeply")
        if isinstance(item, dict):
            if len(item) > MAX_EVENT_DATA_KEYS or any(
                not isinstance(key, str) for key in item
            ):
                raise TriggerValidationError("event_data keys must be strings")
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value, 1)


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    return value
