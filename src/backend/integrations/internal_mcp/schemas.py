"""Lightweight schema helpers used by the internal MCP gateway.

The design deliberately avoids taking a dependency on `jsonschema` so that we
can iterate quickly inside the monolith. The helper verifies a narrow subset of
JSON schema semantics (required keys, simple type checking, optional unknown
fields) which is sufficient for the initial gateway scaffolding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Tuple, Union
from types import NoneType

JsonCompatibleType = Union[type, Tuple[type, ...]]


@dataclass(frozen=True)
class Schema:
    """Very small payload schema representation.

    Attributes:
        required: mapping of keys to acceptable Python types.
        optional: mapping of optional keys to acceptable Python types.
        allow_unknown: whether keys outside required/optional should be allowed.
        description: human readable context for diagnostics / errors.
    """

    required: Mapping[str, JsonCompatibleType] = field(default_factory=dict)
    optional: Mapping[str, JsonCompatibleType] = field(default_factory=dict)
    allow_unknown: bool = False
    description: str | None = None

    def expect(self, key: str) -> JsonCompatibleType | None:
        if key in self.required:
            return self.required[key]
        return self.optional.get(key)


class SchemaValidationError(Exception):
    """Raised when payload validation fails."""


def _normalise_expected(expected: JsonCompatibleType) -> Tuple[type, ...]:
    if isinstance(expected, tuple):
        candidates = expected
    else:
        candidates = (expected,)
    normalised: list[type] = []
    for candidate in candidates:
        if candidate is None:  # pragma: no cover - defensive path
            normalised.append(NoneType)
        elif candidate is NoneType:
            normalised.append(NoneType)
        else:
            normalised.append(candidate)
    return tuple(normalised)


def _matches(value: Any, expected: JsonCompatibleType) -> bool:
    allowed = _normalise_expected(expected)
    if not allowed:
        return True
    return any(isinstance(value, typ) for typ in allowed)


def validate_payload(
    schema: Schema, payload: Mapping[str, Any]
) -> Tuple[bool, list[str]]:
    """Validate payload against schema returning success flag and error list."""

    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return False, ["Payload must be a mapping."]

    for key, expected in schema.required.items():
        if key not in payload:
            label = schema.description or "payload"
            errors.append(f"Missing required field '{key}' for {label}.")
            continue
        value = payload[key]
        if not _matches(value, expected):
            typ_names = ", ".join(
                sorted({t.__name__ for t in _normalise_expected(expected)})
            )
            errors.append(
                f"Field '{key}' expected type {typ_names} but received {type(value).__name__}."
            )

    if not schema.allow_unknown:
        known_keys = set(schema.required.keys()) | set(schema.optional.keys())
        for key in payload.keys():
            if key not in known_keys:
                errors.append(f"Unexpected field '{key}'.")

    for key, expected in schema.optional.items():
        if key not in payload:
            continue
        value = payload[key]
        allowed = _normalise_expected(expected)
        if value is None and NoneType not in allowed:
            typ_names = ", ".join(sorted({t.__name__ for t in allowed}))
            errors.append(
                f"Optional field '{key}' expected type {typ_names} but received NoneType."
            )
            continue
        if not _matches(value, expected):
            typ_names = ", ".join(sorted({t.__name__ for t in allowed}))
            errors.append(
                f"Optional field '{key}' expected type {typ_names} but received {type(value).__name__}."
            )

    return not errors, errors


def coerce_payload(
    schema: Schema, payload: MutableMapping[str, Any]
) -> Tuple[MutableMapping[str, Any], list[str]]:
    """Validate and return payload with errors for convenience.

    The helper mirrors :func:`validate_payload` but keeps the mutable payload so
    callers can chain transformations without additional dict allocations.
    """

    ok, errors = validate_payload(schema, payload)
    return payload, errors


def coerce_payload_types(
    schema: Schema, payload: MutableMapping[str, Any]
) -> Tuple[MutableMapping[str, Any], list[str]]:
    """Coerce payload fields to expected primitive types when safe.

    This intentionally applies only narrow, predictable coercions to avoid
    unexpected behaviour changes. It is used as a pre-validation step to
    recover from common LLM output mismatches (e.g., numeric strings).
    """

    warnings: list[str] = []

    def _coerce_value(key: str, value: Any, expected: JsonCompatibleType) -> Any:
        allowed = _normalise_expected(expected)

        if value is None:
            return value

        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return value

            if int in allowed and raw.isdigit():
                warnings.append(f"Coerced field '{key}' from string to int.")
                return int(raw)

            if float in allowed:
                try:
                    coerced = float(raw)
                except ValueError:
                    coerced = None
                if coerced is not None:
                    warnings.append(f"Coerced field '{key}' from string to float.")
                    return coerced

            if bool in allowed:
                lowered = raw.lower()
                if lowered in {"true", "false"}:
                    return lowered == "true"

            if list in allowed:
                lowered = raw.lower()
                if lowered in {"none", "null"}:
                    if NoneType in allowed:
                        warnings.append(f"Coerced field '{key}' from string to None.")
                        return None
                    return value
                warnings.append(f"Coerced field '{key}' from string to list.")
                return [raw]

            # JSON string to dict coercion (common LLM output pattern)
            if dict in allowed and raw.startswith("{") and raw.endswith("}"):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        warnings.append(
                            f"Coerced field '{key}' from JSON string to dict."
                        )
                        return parsed
                except json.JSONDecodeError:
                    pass  # Fall through to return original value

        return value

    for key, expected in schema.required.items():
        if key not in payload:
            continue
        payload[key] = _coerce_value(key, payload[key], expected)

    for key, expected in schema.optional.items():
        if key not in payload:
            continue
        payload[key] = _coerce_value(key, payload[key], expected)

    return payload, warnings
