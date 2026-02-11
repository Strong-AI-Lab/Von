"""Tests for coerce_payload_types in internal MCP schemas."""

from __future__ import annotations

import json

from src.backend.integrations.internal_mcp.schemas import Schema, coerce_payload_types


def test_coerce_json_array_string_to_list() -> None:
    """When an LLM sends a JSON-encoded array as a string, parse it into a real list."""
    schema = Schema(required={"concepts": list}, optional={})
    concepts = [
        {"name": "has_supervisee", "kind": "predicate"},
        {"name": "has_supervisor", "kind": "predicate"},
    ]
    payload: dict = {"concepts": json.dumps(concepts)}

    coerced, warnings = coerce_payload_types(schema, payload)

    assert isinstance(coerced["concepts"], list)
    assert len(coerced["concepts"]) == 2
    assert coerced["concepts"][0]["name"] == "has_supervisee"
    assert coerced["concepts"][1]["name"] == "has_supervisor"
    assert any("JSON string to list" in w for w in warnings)


def test_coerce_plain_string_wraps_in_list() -> None:
    """A plain non-JSON string should still be wrapped in a single-element list."""
    schema = Schema(required={"tags": list}, optional={})
    payload: dict = {"tags": "some-tag"}

    coerced, warnings = coerce_payload_types(schema, payload)

    assert coerced["tags"] == ["some-tag"]
    assert any("string to list" in w for w in warnings)


def test_coerce_invalid_json_string_wraps_in_list() -> None:
    """Malformed JSON starting with [ should fall back to wrapping."""
    schema = Schema(required={"items": list}, optional={})
    payload: dict = {"items": "[not valid json"}

    coerced, warnings = coerce_payload_types(schema, payload)

    assert coerced["items"] == ["[not valid json"]
    assert any("string to list" in w for w in warnings)


def test_coerce_json_object_string_not_treated_as_list() -> None:
    """A JSON object string should NOT be parsed as a list (it's not an array)."""
    schema = Schema(required={"data": list}, optional={})
    payload: dict = {"data": '{"key": "value"}'}

    coerced, warnings = coerce_payload_types(schema, payload)

    # Should wrap the string, not parse it as an array
    assert coerced["data"] == ['{"key": "value"}']


def test_coerce_null_string_to_none_when_allowed() -> None:
    """String 'null' should coerce to None when NoneType is allowed."""
    schema = Schema(required={"concepts": (list, type(None))}, optional={})
    payload: dict = {"concepts": "null"}

    coerced, warnings = coerce_payload_types(schema, payload)

    assert coerced["concepts"] is None
    assert any("string to None" in w for w in warnings)
