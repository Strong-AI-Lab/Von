"""Tests for coerce_payload_types in internal MCP schemas."""

from __future__ import annotations

import json
from typing import Any, cast
from types import SimpleNamespace

from src.backend.integrations.internal_mcp import build_default_catalogue
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
    normalise_payload_aliases,
    schema_to_json_schema,
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


def test_coerce_declared_scalar_from_object_payload() -> None:
    schema = Schema(
        required={},
        optional={"task_id": str},
        scalar_source_fields={
            "task_id": ("task_concept_id", "concept_id"),
        },
    )
    payload = {
        "task_id": {
            "success": True,
            "task_concept_id": "#V#task_contact_tsinghua",
        }
    }

    coerced, warnings = coerce_payload_types(schema, payload)

    assert coerced["task_id"] == "#V#task_contact_tsinghua"
    assert any("Extracted scalar field 'task_id'" in warning for warning in warnings)
    ok, errors = validate_payload(schema, coerced)
    assert ok is True
    assert errors == []


def test_schema_enum_values_are_validated() -> None:
    schema = Schema(
        required={"title": str},
        optional={"priority": str},
        enum_values={"priority": ("low", "medium", "high", "critical")},
    )

    ok, errors = validate_payload(
        schema,
        {"title": "Contact professors", "priority": "normal"},
    )

    assert ok is False
    assert errors == [
        "Optional field 'priority' expected one of 'low', 'medium', 'high', 'critical' but received 'normal'."
    ]


def test_gateway_invoke_extracts_declared_scalar_from_object_payload() -> None:
    observed: dict[str, object] = {}

    def _handler(*, task_id: str) -> dict[str, object]:
        observed["task_id"] = task_id
        return {"success": True, "task_id": task_id}

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="task_proxy",
            handler=_handler,
            input_schema=Schema(
                required={"task_id": str},
                scalar_source_fields={
                    "task_id": ("task_concept_id", "concept_id"),
                },
            ),
            output_schema=Schema(
                required={"success": bool, "task_id": str},
                allow_unknown=False,
            ),
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )
    caller_payload = {
        "task_id": {
            "success": True,
            "task_concept_id": "#V#task_contact_tsinghua",
        }
    }

    result = gateway.invoke("task_proxy", caller_payload)

    assert caller_payload["task_id"]["task_concept_id"] == "#V#task_contact_tsinghua"
    assert observed["task_id"] == "#V#task_contact_tsinghua"
    assert result.payload["task_id"] == "#V#task_contact_tsinghua"


def test_schema_aliases_are_normalised_without_mutating_unrelated_fields() -> None:
    schema = Schema(
        required={"profile": str},
        optional={"query": str},
        aliases={"identity": "profile", "q": "query"},
    )
    payload = {"identity": "zhan-gmail", "q": "in:inbox", "max_results": 10}

    normalised, warnings = normalise_payload_aliases(schema, payload)

    assert normalised is payload
    assert normalised == {
        "profile": "zhan-gmail",
        "query": "in:inbox",
        "max_results": 10,
    }
    assert len(warnings) == 2


def test_schema_metadata_round_trips_to_json_schema_extensions() -> None:
    schema = Schema(
        required={"profile": str},
        optional={"query": str},
        aliases={"identity": "profile"},
        batch_propagated_fields=("profile",),
        enum_values={"profile": ("zhan-gmail", "lab-gmail")},
        scalar_source_fields={"query": ("search_query",)},
    )

    json_schema = schema_to_json_schema(schema)

    assert json_schema["x-von-argument-aliases"] == {"identity": "profile"}
    assert json_schema["x-von-batch-propagated-fields"] == ["profile"]
    assert json_schema["x-von-scalar-source-fields"] == {"query": ["search_query"]}
    assert json_schema["properties"]["profile"]["enum"] == [
        "zhan-gmail",
        "lab-gmail",
    ]


def test_orchestrator_schema_conversion_preserves_tool_argument_metadata() -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, object()))

    json_schema = orchestrator._mcp_schema_to_json_schema(
        {
            "required": {"profile": str},
            "optional": {"query": str},
            "description": "List records.",
            "aliases": {"identity": "profile"},
            "batch_propagated_fields": ["profile"],
            "enum_values": {"profile": ["zhan-gmail", "lab-gmail"]},
            "scalar_source_fields": {"query": ["search_query"]},
        }
    )

    assert json_schema["description"] == "List records."
    assert json_schema["x-von-argument-aliases"] == {"identity": "profile"}
    assert json_schema["x-von-batch-propagated-fields"] == ["profile"]
    assert json_schema["x-von-scalar-source-fields"] == {"query": ["search_query"]}
    assert json_schema["properties"]["profile"]["enum"] == [
        "zhan-gmail",
        "lab-gmail",
    ]


def test_preflight_emits_contract_validation_diagnostics() -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, object()))
    catalogue = {
        "gmail_list_messages": {
            "name": "gmail_list_messages",
            "description": "List Gmail messages.",
            "input_schema": {
                "required": {"profile": str},
                "optional": {"max_results": int},
                "allow_unknown": False,
                "description": "Gmail list arguments",
            },
            "output_schema": None,
            "category": "read",
        }
    }

    preflight = orchestrator._preflight_tool_calls(
        [
            {
                "action": "call_tool",
                "tool": "gmail_list_messages",
                "payload": {"max_results": 10},
            }
        ],
        catalogue,
        allowed_tool_names=None,
        user_namespace="#V#test_user",
        selected_gmail_profile=None,
    )

    assert preflight.errors == [
        "gmail_list_messages: Missing required field 'profile' for Gmail list arguments."
    ]
    assert preflight.diagnostics
    diagnostic = preflight.diagnostics[0]
    assert diagnostic["schema_version"] == "tool_call_contract_validation.v1"
    assert diagnostic["error_code"] == "schema_validation_failed"
    assert diagnostic["tool"] == "gmail_list_messages"
    assert diagnostic["contract"]["input_schema"]["required"] == ["profile"]
    assert diagnostic["contract"]["input_schema"]["additionalProperties"] is False


def test_tool_call_repair_prompt_receives_selected_contract() -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, object()))
    captured: dict[str, str] = {}

    class _RepairLLM:
        def generate(self, prompt: str, context=None, model=None) -> str:
            captured["prompt"] = prompt
            return (
                '{"action":"call_tool","tool":"gmail_list_messages",'
                '"payload":{"profile":"zhan-gmail","max_results":10}}'
            )

    orchestrator._render_authoritative_prompt = cast(  # type: ignore[method-assign]
        Any,
        lambda *args, **kwargs: SimpleNamespace(
            text="Repair the tool call using this contract:\n{tool_contracts}"
        ),
    )
    catalogue = {
        "gmail_list_messages": {
            "name": "gmail_list_messages",
            "description": "List Gmail messages.",
            "input_schema": {
                "required": {"profile": str},
                "optional": {"max_results": int},
                "allow_unknown": False,
                "description": "Gmail list arguments",
            },
            "output_schema": None,
            "category": "read",
        }
    }

    repaired = orchestrator._attempt_tool_call_repair(
        current_response=(
            '[{"action":"call_tool","tool":"gmail_list_messages","payload":{}}]'
        ),
        errors=["gmail_list_messages: Missing required field 'profile'."],
        tool_list=["gmail_list_messages"],
        tool_calls=[
            {
                "action": "call_tool",
                "tool": "gmail_list_messages",
                "payload": {},
            }
        ],
        method_catalogue=catalogue,
        llm_client=_RepairLLM(),
        policy_state=cast(Any, None),
        default_model="test-model",
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        aux_llm_calls=[],
        llm_calls_log=[],
        record_llm_call=None,
    )

    assert repaired == [
        {
            "action": "call_tool",
            "tool": "gmail_list_messages",
            "payload": {"profile": "zhan-gmail", "max_results": 10},
        }
    ]
    assert '"name": "gmail_list_messages"' in captured["prompt"]
    assert '"profile"' in captured["prompt"]
    assert '"additionalProperties": false' in captured["prompt"]


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
                        "x-von-argument-aliases": {"user_id": "user_concept_id"},
                        "x-von-batch-propagated-fields": ["user_concept_id"],
                        "x-von-scalar-source-fields": {
                            "user_concept_id": ["concept_id"]
                        },
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
    assert schema.aliases == {"user_id": "user_concept_id"}
    assert tuple(schema.batch_propagated_fields) == ("user_concept_id",)
    assert tuple(schema.scalar_source_fields["user_concept_id"]) == ("concept_id",)
    ok, errors = validate_payload(
        schema,
        {"user_concept_id": "#V#test_user", "top_k": 3},
    )
    assert ok is True
    assert errors == []


def test_tool_call_preflight_applies_generic_aliases_and_batch_hints() -> None:
    class _AliasGateway:
        enabled = True

        def describe_methods(self) -> dict[str, object]:
            return {
                "lookup_documents": {
                    "input_schema": {
                        "required": ["workspace_id", "query"],
                        "optional": {"limit": int},
                        "aliases": {"space": "workspace_id", "q": "query"},
                        "batch_propagated_fields": ["workspace_id"],
                    }
                }
            }

    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _AliasGateway()))
    tool_calls = orchestrator._extract_tool_calls(
        (
            '{"action":"call_tool","tool":"lookup_documents",'
            '"payload":{"space":"lab","q":"workflow"}}\n'
            '{"action":"call_tool","tool":"lookup_documents",'
            '"payload":{"q":"telemetry","limit":5}}'
        )
    )

    preflight = orchestrator._preflight_tool_calls(
        tool_calls or [],
        orchestrator._gateway.describe_methods(),
        allowed_tool_names=None,
        user_namespace=None,
        selected_gmail_profile=None,
    )

    assert preflight.errors == []
    assert tool_calls is not None
    assert [dict(call["payload"]) for call in tool_calls] == [
        {"workspace_id": "lab", "query": "workflow"},
        {"workspace_id": "lab", "query": "telemetry", "limit": 5},
    ]


def test_task_contracts_expose_priority_enum_and_task_id_scalar_projection() -> None:
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))
    catalogue = gateway.describe_methods()

    task_create_schema = orchestrator._tool_schema_for_name("task_create", catalogue)
    task_assign_schema = orchestrator._tool_schema_for_name("task_assign", catalogue)

    assert task_create_schema is not None
    assert tuple(task_create_schema.enum_values["priority"]) == (
        "low",
        "medium",
        "high",
        "critical",
    )
    assert task_assign_schema is not None
    assert "task_concept_id" in task_assign_schema.scalar_source_fields["task_id"]


def test_tool_call_preflight_extracts_task_id_from_prior_task_payload() -> None:
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))
    tool_calls = [
        {
            "action": "call_tool",
            "tool": "task_assign",
            "payload": {
                "task_id": {
                    "success": True,
                    "task_concept_id": "#V#task_contact_tsinghua",
                },
                "assignee_concept_id": "#V#michael_witbrock",
            },
        }
    ]

    preflight = orchestrator._preflight_tool_calls(
        cast(Any, tool_calls),
        gateway.describe_methods(),
        allowed_tool_names=None,
        user_namespace="#V#lu_yunli@the_lu_witbrock_household",
        selected_gmail_profile=None,
    )

    assert preflight.errors == []
    assert tool_calls[0]["payload"]["task_id"] == "#V#task_contact_tsinghua"


def test_tool_call_preflight_rejects_invalid_task_priority_before_invoke() -> None:
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))

    preflight = orchestrator._preflight_tool_calls(
        [
            {
                "action": "call_tool",
                "tool": "task_create",
                "payload": {
                    "title": "Contact professors",
                    "description": "Contact Tsinghua professors.",
                    "priority": "normal",
                },
            }
        ],
        gateway.describe_methods(),
        allowed_tool_names=None,
        user_namespace="#V#lu_yunli@the_lu_witbrock_household",
        selected_gmail_profile=None,
    )

    assert preflight.errors == [
        "task_create: Optional field 'priority' expected one of 'low', 'medium', 'high', 'critical' but received 'normal'."
    ]


def test_tool_call_preflight_allows_declared_follow_up_tools() -> None:
    class _FollowUpGateway:
        enabled = True

        def describe_methods(self) -> dict[str, object]:
            return {
                "list_documents": {
                    "input_schema": {
                        "required": ["workspace_id"],
                        "optional": {},
                    }
                },
                "get_document": {
                    "input_schema": {
                        "required": ["workspace_id", "document_id"],
                        "optional": {},
                    }
                },
            }

    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _FollowUpGateway()))
    tool_calls = orchestrator._extract_tool_calls(
        (
            '{"action":"call_tool","tool":"get_document",'
            '"payload":{"workspace_id":"lab","document_id":"doc-1"}}'
        )
    )

    preflight = orchestrator._preflight_tool_calls(
        tool_calls or [],
        orchestrator._gateway.describe_methods(),
        allowed_tool_names={"list_documents"},
        user_namespace=None,
        selected_gmail_profile=None,
        tool_invocations=[
            {
                "tool": "list_documents",
                "status": "ok",
                "effective_payload": {
                    "documents": [{"document_id": "doc-1"}],
                    "_tool_follow_up": {
                        "schema_version": "mcp_tool_follow_up.v1",
                        "item_array_field": "documents",
                        "follow_up_tools": [
                            {
                                "tool": "get_document",
                                "input_bindings": {
                                    "workspace_id": {
                                        "source": "request",
                                        "field": "workspace_id",
                                    },
                                    "document_id": {
                                        "source": "item",
                                        "field": "document_id",
                                    },
                                },
                            }
                        ],
                    },
                },
            }
        ],
    )

    assert preflight.errors == []


def test_tool_result_formatting_keeps_top_level_fields_over_raw_nested_payload() -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, object()))
    payload = {
        "id": "msg-1",
        "message_id": "msg-1",
        "sender": "sender@example.test",
        "subject": "Subject line",
        "date": "Sat, 25 Apr 2026 09:00:00 +0000",
        "snippet": "Short snippet",
        "payload": {
            "headers": [
                {
                    "name": "Large-Raw-Header",
                    "value": "x" * 50_000,
                }
            ]
        },
    }

    formatted = orchestrator._format_tool_result(
        "detail_tool",
        payload,
        duration_ms=1.0,
        status="ok",
    )
    result = json.loads(formatted)

    assert result["payload"]["_llm_view"] == (
        "generic_tool_payload_top_level_fields.v1"
    )
    assert result["payload"]["message_id"] == "msg-1"
    assert result["payload"]["subject"] == "Subject line"
    assert result["payload"]["_omitted_nested_payload_keys"] == ["payload"]
    assert "Large-Raw-Header" not in formatted


def test_follow_up_context_keeps_many_recent_tool_messages() -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, object()))
    messages: list[dict[str, str]] = [{"role": "user", "content": "Need records."}]
    for index in range(10):
        messages.append(
            {
                "role": "assistant",
                "content": f"Tool call batch {index}",
            }
        )
        messages.append(
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "tool": "detail_tool",
                        "payload": {"message_id": f"msg-{index}"},
                    }
                ),
            }
        )

    compacted = orchestrator._build_follow_up_llm_context(
        messages,
        max_chars=40_000,
    )

    retained_tool_ids = [
        json.loads(str(message["content"]))["payload"]["message_id"]
        for message in compacted
        if message.get("role") == "tool"
    ]
    assert retained_tool_ids == [f"msg-{index}" for index in range(10)]
