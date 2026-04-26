"""Support helpers for contract-bound tool-call preparation.

These helpers keep tool-call contract inspection, schema summarisation, and
validation diagnostics out of the orchestration monolith.  They do not decide
which tool should be used; they expose the selected tool contract and validation
state so workflow-authored recovery can reason over it.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping, Sequence


CONTRACT_ATTEMPT_SCHEMA_VERSION = "tool_contract_attempt.v1"
CONTRACT_VALIDATION_SCHEMA_VERSION = "tool_call_contract_validation.v1"


def stable_json_dumps(value: Any, *, max_chars: int | None = None) -> str:
    """Return deterministic JSON for telemetry and prompt context."""

    text = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    if isinstance(max_chars, int) and max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def schema_hash(schema: Mapping[str, Any] | None) -> str | None:
    """Return a short stable hash for a JSON-schema-like object."""

    if not isinstance(schema, Mapping):
        return None
    serialised = stable_json_dumps(schema)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()[:16]


def closed_object_schema(schema: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a provider-facing object schema with explicit unknown-field policy."""

    if not isinstance(schema, Mapping):
        return None
    payload = copy.deepcopy(dict(schema))
    if payload.get("type") is None:
        payload["type"] = "object"
    if payload.get("type") == "object" and "additionalProperties" not in payload:
        payload["additionalProperties"] = False
    return payload


def is_strict_tool_schema_compatible(schema: Mapping[str, Any] | None) -> bool:
    """Return whether a schema is safe for OpenAI-style strict tool definitions.

    The strict provider subset is most predictable when the schema is a closed
    object and every declared property is required.  Optional arguments still get
    closed-schema validation, but they are not marked strict here.
    """

    if not isinstance(schema, Mapping):
        return False
    if schema.get("type") != "object":
        return False
    if schema.get("additionalProperties") is not False:
        return False
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return False
    required = schema.get("required")
    if not isinstance(required, Sequence) or isinstance(
        required, (str, bytes, bytearray)
    ):
        return False
    property_names = {str(name) for name in properties.keys()}
    required_names = {str(name) for name in required}
    return property_names == required_names


def strip_internal_schema_extensions(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """Remove Von-only schema extension keys before provider submission."""

    if not isinstance(schema, Mapping):
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def _strip(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): _strip(nested)
                for key, nested in value.items()
                if not str(key).startswith("x-von-")
            }
        if isinstance(value, list):
            return [_strip(item) for item in value]
        return copy.deepcopy(value)

    return _strip(closed_object_schema(schema) or schema)


def tool_definition_contract_summary(
    *,
    name: str,
    description: str | None,
    input_schema: Mapping[str, Any] | None,
    output_schema: Mapping[str, Any] | None = None,
    category: str | None = None,
    include_schema: bool = True,
) -> dict[str, Any]:
    """Build a compact contract summary for telemetry or repair prompts."""

    input_contract = closed_object_schema(input_schema)
    output_contract = closed_object_schema(output_schema)
    summary: dict[str, Any] = {
        "name": name,
        "description": description,
        "category": category,
        "input_schema_hash": schema_hash(input_contract),
        "output_schema_hash": schema_hash(output_contract),
        "input_closed": (
            input_contract.get("additionalProperties") is False
            if isinstance(input_contract, Mapping)
            else None
        ),
        "strict_schema_compatible": is_strict_tool_schema_compatible(input_contract),
    }
    if include_schema:
        summary["input_schema"] = input_contract
        if output_contract is not None:
            summary["output_schema"] = output_contract
    return {key: value for key, value in summary.items() if value is not None}


def validation_diagnostic(
    *,
    tool: str | None,
    error_code: str,
    message: str,
    payload: Mapping[str, Any] | None = None,
    contract: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a standard validation diagnostic for failed contract fulfilment."""

    diagnostic: dict[str, Any] = {
        "schema_version": CONTRACT_VALIDATION_SCHEMA_VERSION,
        "status": "invalid",
        "tool": tool,
        "error_code": error_code,
        "message": message,
    }
    if isinstance(payload, Mapping):
        diagnostic["payload"] = dict(payload)
    if isinstance(contract, Mapping):
        diagnostic["contract"] = dict(contract)
    if warnings:
        diagnostic["warnings"] = [str(warning) for warning in warnings]
    return {key: value for key, value in diagnostic.items() if value is not None}


def contract_attempt(
    *,
    stage: str,
    tool_calls: Sequence[Mapping[str, Any]],
    contracts: Sequence[Mapping[str, Any]],
    validation_errors: Sequence[str],
    validation_warnings: Sequence[str],
    repair_attempted: bool = False,
    repair_succeeded: bool | None = None,
) -> dict[str, Any]:
    """Build a telemetry record for a contract validation attempt."""

    return {
        "schema_version": CONTRACT_ATTEMPT_SCHEMA_VERSION,
        "stage": stage,
        "tool_calls": [dict(call) for call in tool_calls if isinstance(call, Mapping)],
        "contracts": [dict(contract) for contract in contracts],
        "validation_error_count": len(validation_errors),
        "validation_errors": [str(error) for error in validation_errors],
        "validation_warnings": [str(warning) for warning in validation_warnings],
        "repair_attempted": bool(repair_attempted),
        "repair_succeeded": repair_succeeded,
    }
