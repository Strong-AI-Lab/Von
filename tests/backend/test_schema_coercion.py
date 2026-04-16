"""Tests for coerce_payload_types in internal MCP schemas."""

from __future__ import annotations

import json
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import (
    Schema,
    coerce_payload_types,
    validate_payload,
)
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


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


def test_gateway_invoke_coerces_numeric_strings_without_mutating_caller_payload() -> (
    None
):
    """Gateway invoke should share the same primitive coercion support as orchestrator preflight."""

    observed: dict[str, object] = {}

    def _handler(*, query: str, top_k: int | None = None) -> dict[str, object]:
        observed["query"] = query
        observed["top_k"] = top_k
        observed["top_k_type"] = type(top_k).__name__
        return {"success": True, "query": query, "top_k": top_k}

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="search_proxy",
            handler=_handler,
            input_schema=Schema(
                required={"query": str},
                optional={"top_k": int},
                allow_unknown=False,
                description="Search proxy input.",
            ),
            output_schema=Schema(
                required={"success": bool, "query": str},
                optional={"top_k": (int, type(None))},
                allow_unknown=False,
                description="Search proxy output.",
            ),
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )
    caller_payload = {"query": "papers of mine", "top_k": "10"}

    result = gateway.invoke("search_proxy", caller_payload)

    assert caller_payload["top_k"] == "10"
    assert observed["query"] == "papers of mine"
    assert observed["top_k"] == 10
    assert observed["top_k_type"] == "int"
    assert result.payload["success"] is True
    assert result.payload["top_k"] == 10


def test_tool_schema_lookup_accepts_json_schema_metadata() -> None:
    """Tool-call preflight should understand JSON Schema-shaped metadata too."""

    class _JsonSchemaGateway:
        enabled = True

        def describe_methods(self) -> dict[str, object]:
            return {
                "test.lookup_current_user_papers": {
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "user_concept_id": {"type": "string"},
                            "top_k": {"type": "integer"},
                        },
                        "required": ["user_concept_id"],
                        "description": "Grounded current-user paper lookup.",
                    }
                }
            }

    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _JsonSchemaGateway())
    )
    schema = orchestrator._tool_schema_for_name(
        "test.lookup_current_user_papers",
        orchestrator._gateway.describe_methods(),
    )

    assert schema is not None
    assert schema.required["user_concept_id"] is str
    assert schema.optional["top_k"] is int
    ok, errors = validate_payload(
        schema,
        {"user_concept_id": "#V#test_user", "top_k": 3},
    )
    assert ok is True
    assert errors == []
