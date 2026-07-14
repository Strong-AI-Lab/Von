"""Lightweight schema helpers used by the internal MCP gateway.

The design deliberately avoids taking a dependency on `jsonschema` so that we
can iterate quickly inside the monolith. The helper verifies a narrow subset of
JSON schema semantics (required keys, simple type checking, optional unknown
fields) which is sufficient for the initial gateway scaffolding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from types import NoneType

JsonCompatibleType = Union[type, Tuple[type, ...]]


# ---------------------------------------------------------------------------
# Standardised MCP Error Response (JVNAUTOSCI-1053)
# ---------------------------------------------------------------------------


@dataclass
class MCPErrorResponse:
    """Standardised error response for MCP tool handlers.

    This dataclass ensures all MCP tool errors follow a consistent structure,
    making it easier for LLM agents to interpret failures and take corrective
    action.

    Attributes:
        error_code: A machine-readable error identifier (e.g., 'missing_parameter',
            'concept_not_found', 'validation_failed'). Should be snake_case.
        message: A human-readable description of the error.
        details: Additional structured context about the error (e.g., which
            parameters were missing, what validation failed).
        suggestions: A list of actionable suggestions for the LLM/user to
            recover from the error.
        related_concept_ids: Concept IDs that are relevant to the error context,
            useful for the LLM to reference in subsequent operations.
    """

    error_code: str
    message: str
    details: Optional[Dict[str, Any]] = None
    suggestions: Optional[List[str]] = None
    related_concept_ids: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a dictionary suitable for JSON serialisation.

        Returns a response dict with 'success': False and the error fields.
        Omits None values for cleaner output.
        """
        result: Dict[str, Any] = {
            "success": False,
            "error": self.message,  # Backward compatibility: top-level 'error' string
            "error_code": self.error_code,
        }
        if self.details is not None:
            result["error_details"] = self.details
        if self.suggestions is not None:
            result["suggestions"] = self.suggestions
        if self.related_concept_ids is not None:
            result["related_concept_ids"] = self.related_concept_ids
        return result


def make_error_response(
    error_code: str,
    message: str,
    *,
    details: Optional[Dict[str, Any]] = None,
    suggestions: Optional[List[str]] = None,
    related_concept_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a standardised MCP error response dictionary.

    This is a convenience function for creating error responses without
    explicitly instantiating MCPErrorResponse.

    Args:
        error_code: A machine-readable error identifier (snake_case).
        message: A human-readable description of the error.
        details: Additional structured context about the error.
        suggestions: Actionable suggestions for recovery.
        related_concept_ids: Relevant concept IDs for context.

    Returns:
        A dictionary with standardised error response structure, including
        'success': False for response consistency.

    Example:
        >>> make_error_response(
        ...     "concept_not_found",
        ...     "Concept '#V#example' does not exist",
        ...     details={"concept_id": "#V#example"},
        ...     suggestions=["Create the concept first using create_concepts"]
        ... )
        {
            'success': False,
            'error': "Concept '#V#example' does not exist",
            'error_code': 'concept_not_found',
            'error_details': {'concept_id': '#V#example'},
            'suggestions': ['Create the concept first using create_concepts']
        }
    """
    return MCPErrorResponse(
        error_code=error_code,
        message=message,
        details=details,
        suggestions=suggestions,
        related_concept_ids=related_concept_ids,
    ).to_dict()


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
    aliases: Mapping[str, str] = field(default_factory=dict)
    batch_propagated_fields: Sequence[str] = field(default_factory=tuple)
    enum_values: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    scalar_source_fields: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def expect(self, key: str) -> JsonCompatibleType | None:
        if key in self.required:
            return self.required[key]
        return self.optional.get(key)


class SchemaValidationError(Exception):
    """Raised when a gateway input or output payload violates its schema."""

    def __init__(self, message: str, *, stage: str = "unknown") -> None:
        super().__init__(message)
        self.stage = str(stage or "unknown").strip() or "unknown"


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


def python_type_to_json_schema_type(python_type: Any) -> str:
    if python_type is type(None):
        return "null"
    mapping = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    return mapping.get(python_type, "string")


def expected_to_json_schema(expected: JsonCompatibleType) -> Dict[str, Any]:
    json_types: list[str] = []
    for candidate in _normalise_expected(expected):
        json_type = python_type_to_json_schema_type(candidate)
        if json_type not in json_types:
            json_types.append(json_type)

    payload: Dict[str, Any] = {
        "type": json_types[0] if len(json_types) == 1 else json_types or "string"
    }

    # Some MCP clients reject array schemas without an explicit items schema.
    if "array" in json_types:
        payload["items"] = {}
    if "object" in json_types:
        payload["additionalProperties"] = True

    return payload


def schema_to_json_schema(schema: "Schema") -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required_names: list[str] = []

    for field_name, expected in schema.required.items():
        properties[field_name] = expected_to_json_schema(expected)
        enum_values = schema.enum_values.get(field_name)
        if enum_values:
            properties[field_name]["enum"] = list(enum_values)
        required_names.append(field_name)

    for field_name, expected in schema.optional.items():
        if field_name not in properties:
            properties[field_name] = expected_to_json_schema(expected)
        enum_values = schema.enum_values.get(field_name)
        if enum_values:
            properties[field_name]["enum"] = list(enum_values)

    payload: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required_names,
        "additionalProperties": bool(schema.allow_unknown),
    }
    if isinstance(schema.description, str) and schema.description.strip():
        payload["description"] = schema.description.strip()
    if schema.aliases:
        payload["x-von-argument-aliases"] = dict(schema.aliases)
    if schema.batch_propagated_fields:
        payload["x-von-batch-propagated-fields"] = [
            field_name
            for field_name in schema.batch_propagated_fields
            if isinstance(field_name, str) and field_name.strip()
        ]
    if schema.scalar_source_fields:
        payload["x-von-scalar-source-fields"] = {
            field_name: [
                source_field
                for source_field in source_fields
                if isinstance(source_field, str) and source_field.strip()
            ]
            for field_name, source_fields in schema.scalar_source_fields.items()
            if isinstance(field_name, str) and field_name.strip()
        }
    return payload


def normalise_payload_aliases(
    schema: Schema, payload: MutableMapping[str, Any]
) -> Tuple[MutableMapping[str, Any], list[str]]:
    """Map declared schema aliases onto canonical argument names."""

    warnings: list[str] = []
    if not schema.aliases:
        return payload, warnings

    for alias_name, canonical_name in schema.aliases.items():
        if not isinstance(alias_name, str) or not alias_name.strip():
            continue
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            continue
        alias_key = alias_name.strip()
        canonical_key = canonical_name.strip()
        if alias_key not in payload:
            continue
        alias_value = payload.pop(alias_key)
        canonical_value = payload.get(canonical_key)
        canonical_missing = (
            canonical_key not in payload
            or canonical_value is None
            or (
                isinstance(canonical_value, str)
                and not canonical_value.strip()
            )
        )
        if canonical_missing:
            payload[canonical_key] = alias_value
            warnings.append(
                f"Mapped alias field '{alias_key}' to canonical field '{canonical_key}'."
            )
        else:
            warnings.append(
                f"Dropped alias field '{alias_key}' because canonical field '{canonical_key}' was already present."
            )

    return payload, warnings


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
            continue
        enum_values = schema.enum_values.get(key)
        if enum_values and value is not None and value not in enum_values:
            allowed_values = ", ".join(repr(item) for item in enum_values)
            errors.append(
                f"Field '{key}' expected one of {allowed_values} but received {value!r}."
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
            continue
        enum_values = schema.enum_values.get(key)
        if enum_values and value is not None and value not in enum_values:
            allowed_values = ", ".join(repr(item) for item in enum_values)
            errors.append(
                f"Optional field '{key}' expected one of {allowed_values} but received {value!r}."
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

        if isinstance(value, Mapping):
            source_fields = schema.scalar_source_fields.get(key) or ()
            if str in allowed and source_fields:
                for source_field in source_fields:
                    if not isinstance(source_field, str) or not source_field.strip():
                        continue
                    source_value = value.get(source_field.strip())
                    if isinstance(source_value, str) and source_value.strip():
                        warnings.append(
                            f"Extracted scalar field '{key}' from object field '{source_field.strip()}'."
                        )
                        return source_value.strip()

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
                # JSON array string → list (common LLM output pattern)
                if raw.startswith("[") and raw.endswith("]"):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, list):
                            warnings.append(
                                f"Coerced field '{key}' from JSON string to list."
                            )
                            return parsed
                    except json.JSONDecodeError:
                        pass  # Fall through to wrap-in-list fallback
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
