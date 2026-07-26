from __future__ import annotations

import json
import time
from typing import Any

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.languagemodels.structured_tool_calling.types import (
    LLMContinuation,
    LLMResponse,
    StructuredToolContextLimitError,
    ToolCall,
    ToolResult,
)
from src.backend.security.access_control import (
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from src.backend.services.adaptive_turn_service import (
    _bound_tool_results_for_model,
    _capability_catalogue,
    _compact_context_after_limit,
    _compact_evidence_index,
    _json_bytes,
    _trusted_tool_payload,
    execute_adaptive_turn,
    ordinary_turn_read_delegation,
)


class _SequenceClient:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "available_tools": list(available_tools),
                **kwargs,
            }
        )
        next_response = self.responses.pop(0)
        if isinstance(next_response, BaseException):
            raise next_response
        return next_response


def _gateway(handler: Any) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="general_read",
            handler=handler,
            input_schema=Schema(
                optional={
                    "query": str,
                    "namespace": (str, type(None)),
                    "user_id": (str, type(None)),
                    "org_id": (str, type(None)),
                },
                allow_unknown=False,
                description="Read arbitrary general evidence.",
            ),
            category="read",
            ordinary_turn_public=True,
            description="Read arbitrary general evidence.",
        )
    )
    catalogue.register(
        MethodDefinition(
            name="general_write",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(allow_unknown=True),
            category="write",
            description="A write which must not be delegated.",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


def _gmail_gateway(handler: Any) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="gmail_list_messages",
            handler=handler,
            input_schema=Schema(
                required={"profile": str},
                optional={"query": str},
                aliases={
                    "profile_id": "profile",
                    "identity": "profile",
                    "user_id": "profile",
                },
                allow_unknown=False,
                description="List Gmail messages for a represented profile.",
            ),
            category="read",
            ordinary_turn_trusted_argument_bindings={
                "profile": "gmail_profile",
            },
            description="List Gmail messages for a represented profile.",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


def _actor_alias_gateway(handler: Any) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="shared_conversation_list_invites",
            handler=handler,
            input_schema=Schema(
                optional={
                    "invitee_user_id": (str, type(None)),
                    "user_concept_id": (str, type(None)),
                    "acting_user_concept_id": (str, type(None)),
                    "actor_user_id": (str, type(None)),
                    "on_behalf_of_user_concept_id": (str, type(None)),
                    "actor_concept_id": (str, type(None)),
                    "agent_concept_id": (str, type(None)),
                    "organisation_concept_id": (str, type(None)),
                    "org_id": (str, type(None)),
                    "namespace": (str, type(None)),
                },
                allow_unknown=True,
                description="List invites visible to the current actor.",
            ),
            category="read",
            description="List invites visible to the current actor.",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


def _delegation_gateway() -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    policies = {
        "concept_exists": {},
        "gmail_list_messages": {
            "ordinary_turn_trusted_argument_bindings": {
                "profile": "gmail_profile",
            },
        },
        "gmail_list_profiles": {
            "ordinary_turn_excluded_reason": "deployment_account_enumeration",
        },
        "list_recent_screenshots": {
            "ordinary_turn_excluded_reason": "host_local_data",
        },
        "search_arxiv": {"ordinary_turn_public": True},
    }
    for name, policy in policies.items():
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(allow_unknown=True),
                category="read",
                description=f"Read through {name}.",
                **policy,
            )
        )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )


class _ManualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _DeadlineClient(_SequenceClient):
    def __init__(self, clock: _ManualClock, *responses: Any) -> None:
        super().__init__(*responses)
        self.clock = clock

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        response_number = len(self.calls)
        if response_number == 0:
            self.clock.now = 8.0
        return super().generate_with_tools(prompt, available_tools, **kwargs)


class _LateResponseClient(_SequenceClient):
    def __init__(
        self,
        clock: _ManualClock,
        late_at: float,
        *responses: Any,
    ) -> None:
        super().__init__(*responses)
        self.clock = clock
        self.late_at = late_at

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        self.clock.now = self.late_at
        return super().generate_with_tools(prompt, available_tools, **kwargs)


def test_plain_answer_gets_trusted_scope_and_generic_read_doorway() -> None:
    client = _SequenceClient(LLMResponse(text_response="A useful answer."))
    gateway = _gateway(lambda **_kwargs: {"success": True})

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Help with this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-1",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "A useful answer."
    assert result.duration_ms is not None
    assert result.duration_ms >= 0
    system_message = client.calls[0]["system_message"]
    assert "#V#person" in system_message
    assert "#V#org" in system_message
    assert "no write or effect capability" in system_message
    assert {
        tool.name for tool in client.calls[0]["available_tools"]
    } == {
        "turn_read_capabilities",
        "turn_invoke_read_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    catalogue_tool = next(
        tool
        for tool in client.calls[0]["available_tools"]
        if tool.name == "turn_read_capabilities"
    )
    assert set(catalogue_tool.input_schema["properties"]) == {
        "query",
        "names",
        "offset",
        "limit",
    }


def test_ordinary_delegation_is_actor_capability_not_every_read_method() -> None:
    gateway = _delegation_gateway()

    without_mail = ordinary_turn_read_delegation(
        gateway,
        user_concept_id="#V#person",
    )
    with_mail = ordinary_turn_read_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": "represented-profile",
        },
    )

    assert without_mail == ("concept_exists", "search_arxiv")
    assert with_mail == (
        "concept_exists",
        "gmail_list_messages",
        "search_arxiv",
    )
    assert "gmail_list_profiles" not in with_mail
    assert "list_recent_screenshots" not in with_mail

    gateway.disable()
    assert (
        ordinary_turn_read_delegation(
            gateway,
            user_concept_id="#V#person",
            trusted_argument_values={
                "gmail_profile": "represented-profile",
            },
        )
        == ()
    )

    gateway.enable()
    assert ordinary_turn_read_delegation(
        gateway,
        user_concept_id=None,
    ) == ("search_arxiv",)


def test_server_bound_capability_argument_is_not_model_visible() -> None:
    gateway = _gmail_gateway(lambda **_kwargs: {"success": True})
    delegated = ordinary_turn_read_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": "represented-profile",
        },
    )

    catalogue = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["gmail_list_messages"]},
    )

    assert catalogue["total"] == 1
    input_schema = catalogue["capabilities"][0]["input_schema"]
    assert set(input_schema["properties"]) == {"query"}
    assert "profile" not in input_schema.get("required", [])
    assert "x-von-argument-aliases" not in input_schema


def test_fixed_ordinary_turn_arguments_narrow_only_unsafe_options() -> None:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="bounded_diagnostics",
            handler=lambda **kwargs: kwargs,
            input_schema=Schema(
                optional={
                    "query": str,
                    "reset": bool,
                    "bundle_path": (str, type(None)),
                },
                aliases={"path": "bundle_path"},
                allow_unknown=False,
            ),
            category="read",
            ordinary_turn_fixed_arguments={
                "reset": False,
                "bundle_path": None,
            },
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    delegated = ordinary_turn_read_delegation(
        gateway,
        user_concept_id="#V#person",
    )
    capability = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["bounded_diagnostics"]},
    )["capabilities"][0]
    assert set(capability["input_schema"]["properties"]) == {"query"}

    payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="bounded_diagnostics",
        model_payload={
            "query": "slow queries",
            "reset": True,
            "path": "/tmp/private.json",
        },
        trusted_argument_values=None,
    )
    assert payload == {
        "query": "slow queries",
        "reset": False,
        "bundle_path": None,
    }


def test_evidence_index_omission_is_bounded_and_pageable() -> None:
    compact = _compact_evidence_index(
        [
            {
                "schema_version": "turn_evidence_envelope.v1",
                "evidence_id": f"ev-{index}",
                "tool_name": "general_read",
                "call_id": f"call-{index}",
                "status": "ok",
                "preview": "x" * 240,
                "preview_truncated": True,
            }
            for index in range(1_000)
        ]
    )

    assert (
        len(
            json.dumps(
                compact,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= 24_000
    )
    omission = compact[-1]
    assert omission["schema_version"] == "adaptive_turn_evidence_index_omission.v1"
    assert omission["omitted_count"] > 0
    assert omission["total_count"] == 1_000
    assert omission["next_offset"] > 0
    assert omission["list_tool"] == "turn_list_evidence"


def test_evidence_page_resumes_at_first_globally_omitted_handle() -> None:
    full_page = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": f"ev-{index}-{'e' * 512}",
            "tool_name": "general_read",
            "call_id": f"call-{index}-{'c' * 512}",
            "status": "ok",
            "trust_boundary": "untrusted_tool_output",
            "sha256": "f" * 64,
            "size_bytes": 10_000,
            "char_count": 10_000,
            "content_type": "application/json",
            "value_kind": "object",
            "preview_format": "json",
            "preview": "x" * 1_000,
            "preview_truncated": True,
            "available_selectors": [f"/items/{item}" for item in range(20)],
            "turn_id": "turn-pageable",
        }
        for index in range(250, 300)
    ]

    compact = _compact_evidence_index(
        full_page,
        max_bytes=12_000,
        base_offset=250,
        total_count=400,
    )

    omission = compact[-1]
    assert omission["schema_version"] == "adaptive_turn_evidence_index_omission.v1"
    next_offset = omission["next_offset"]
    assert 250 < next_offset < 300
    assert omission["total_count"] == 400
    assert omission["omitted_count"] == 400 - next_offset
    emitted_ids = [
        item["evidence_id"]
        for item in compact
        if isinstance(item.get("evidence_id"), str)
    ]
    assert emitted_ids == [
        f"ev-{index}-{'e' * 512}" for index in range(250, next_offset)
    ]
    resumed = _compact_evidence_index(
        full_page[next_offset - 250 :],
        max_bytes=12_000,
        base_offset=next_offset,
        total_count=400,
    )
    assert resumed[0]["evidence_id"] == f"ev-{next_offset}-{'e' * 512}"


def test_tool_result_correlation_shell_overflow_has_no_oversized_fallback() -> None:
    results = [
        ToolResult(
            call_id=f"call-{index}",
            tool_name="turn_invoke_read_capability",
            status="ok",
            output={"evidence_id": f"ev-{index}", "preview": "x" * 1_000},
        )
        for index in range(500)
    ]

    assert _bound_tool_results_for_model(results) is None


def test_model_can_invoke_any_delegated_read_without_a_prompt_classifier() -> None:
    raw_tail = "z" * 50_000
    seen_actor: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_actor.update(
            {
                "user": get_effective_user_concept_id(),
                "org": get_effective_organisation_concept_id(),
                "arguments": kwargs,
            }
        )
        return {"success": True, "answer": "found", "raw_tail": raw_tail}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_read_capability",
                    call_id="call-1",
                    payload={
                        "name": "general_read",
                        "arguments": {
                            "query": "anything",
                            "namespace": "#V#spoof@other",
                            "user_id": "#V#spoof",
                            "org_id": "#V#other",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The evidence says found."),
    )
    gateway = _gateway(handler)

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Use whatever read is useful.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-2",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The evidence says found."
    assert seen_actor["user"] == "#V#person"
    assert seen_actor["org"] == "#V#org"
    assert seen_actor["arguments"] == {
        "query": "anything",
        "namespace": "#V#spoof@other",
        "user_id": "#V#spoof",
        "org_id": "#V#other",
    }
    assert len(result.evidence_index) == 1
    envelope = result.evidence_index[0]
    assert envelope["preview_truncated"] is True
    assert envelope["sha256"]
    assert "z" * 10_000 not in client.calls[1]["context"][-1]["content"]
    assert len(client.calls[1]["context"][-1]["content"]) < 10_000
    assert "effective_payload" not in result.tool_invocations[0]
    assert raw_tail not in json.dumps(result.tool_invocations)
    assert all(
        invocation.get("tool") != "general_write"
        for invocation in result.tool_invocations
    )


def test_identity_shaped_targets_are_not_globally_rewritten() -> None:
    captured: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"success": True, "invites": []}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_read_capability",
                    call_id="call-actor-aliases",
                    payload={
                        "name": "shared_conversation_list_invites",
                        "arguments": {
                            "invitee_user_id": "#V#legitimate_target",
                            "user_concept_id": "#V#spoof",
                            "acting_user_concept_id": "#V#spoof",
                            "actor_user_id": "#V#spoof",
                            "on_behalf_of_user_concept_id": "#V#spoof",
                            "actor_concept_id": "#V#spoof_agent",
                            "agent_concept_id": "#V#spoof_agent",
                            "organisation_concept_id": "#V#other_org",
                            "org_id": "#V#other_org",
                            "namespace": "#V#spoof@other_org",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="No current invites were found."),
    )

    result = execute_adaptive_turn(
        gateway=_actor_alias_gateway(handler),
        prompt="List my current shared-conversation invites.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-actor-aliases",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "No current invites were found."
    assert captured["invitee_user_id"] == "#V#legitimate_target"
    for preserved_field in (
        "user_concept_id",
        "acting_user_concept_id",
        "actor_user_id",
        "on_behalf_of_user_concept_id",
        "actor_concept_id",
        "agent_concept_id",
        "organisation_concept_id",
        "org_id",
        "namespace",
    ):
        assert captured[preserved_field].startswith("#V#")


def test_context_limit_retries_only_after_a_smaller_different_request() -> None:
    context_error = StructuredToolContextLimitError(
        "context too large",
        decision={"provider_error_code": "context_length_exceeded"},
    )
    client = _SequenceClient(
        context_error,
        LLMResponse(text_response="Recovered from a smaller context."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Keep the task itself.",
        context=[
            {"role": "system", "content": "trusted instruction"},
            {"role": "assistant", "content": "old context " * 10_000},
        ],
        llm_client=client,
        model="test-model",
        turn_id="turn-3",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "Recovered from a smaller context."
    assert len(client.calls) == 2
    before_size = len(str(client.calls[0]["context"]))
    after_size = len(str(client.calls[1]["context"]))
    assert after_size < before_size
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_context_limit_recovery"
    )
    assert recovery["changed"] is True
    assert recovery["after_bytes"] < recovery["before_bytes"]


def test_context_limit_recovery_keeps_a_pageable_evidence_reference() -> None:
    evidence_index = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": f"ev-{index}",
            "tool_name": "general_read",
            "call_id": f"call-{index}",
            "status": "ok",
            "preview": "evidence " * 80,
            "preview_truncated": True,
        }
        for index in range(100)
    ]

    compact = _compact_context_after_limit(
        prompt="Answer from evidence.",
        context=[],
        evidence_index=evidence_index,
        before_size=30_000,
    )

    assert compact is not None
    assert compact
    assert len(_json_bytes(compact)) < 15_000
    payload = json.loads(compact[-1]["content"])
    assert payload["type"] == "prior_tool_evidence_index"
    assert payload["evidence"]
    assert any(
        item.get("list_tool") == "turn_list_evidence"
        and item.get("total_count") == len(evidence_index)
        for item in payload["evidence"]
    )


def test_context_limit_recovery_refuses_to_drop_existing_evidence() -> None:
    compact = _compact_context_after_limit(
        prompt="x" * 20_000,
        context=[],
        evidence_index=[
            {
                "schema_version": "turn_evidence_envelope.v1",
                "evidence_id": "ev-1",
                "tool_name": "general_read",
                "call_id": "call-1",
                "status": "ok",
            }
        ],
        before_size=20_100,
    )

    assert compact is None


def test_context_limit_does_not_retry_an_unchanged_request() -> None:
    client = _SequenceClient(
        StructuredToolContextLimitError(
            "context too large",
            decision={"provider_error_code": "context_length_exceeded"},
        )
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="x" * 20_000,
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-4",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 1
    assert result.terminal_status == "context_limit_unrecoverable"
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_context_limit_recovery"
    )
    assert recovery["changed"] is False


def test_research_deadline_failure_preserves_final_synthesis_reserve() -> None:
    clock = _ManualClock()
    client = _DeadlineClient(
        clock,
        TimeoutError("research model deadline"),
        LLMResponse(text_response="A bounded final answer."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Use the available time sensibly.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-research-deadline",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "A bounded final answer."
    assert len(client.calls) == 2
    assert client.calls[0]["llm_params"]["request_timeout_seconds"] == 8.0
    assert client.calls[1]["llm_params"]["request_timeout_seconds"] == 2.0
    assert {
        tool.name for tool in client.calls[1]["available_tools"]
    } == {"turn_list_evidence", "turn_read_evidence"}
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_research_deadline_recovery"
    )
    assert recovery["action"] == "fresh_final_synthesis_from_available_evidence"


def test_model_result_returned_after_turn_deadline_is_discarded() -> None:
    clock = _ManualClock()
    client = _LateResponseClient(
        clock,
        0.2,
        LLMResponse(text_response="A late answer that must not become terminal."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer within the bounded turn.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-late-model-result",
        turn_budget_seconds=0.05,
        final_synthesis_reserve_seconds=0.01,
        clock=clock,
    )

    assert result.terminal_status == "turn_deadline_exceeded"
    assert "late answer" not in result.response_text
    assert result.llm_calls[0]["status"] == "late_result_discarded"
    late_event = next(
        event
        for event in result.aux_llm_calls
        if event.get("type") == "adaptive_turn_late_model_result"
    )
    assert late_event["late_result_policy"] == "discard_from_terminal_result"


def test_final_synthesis_receives_bounded_evidence_after_native_continuation() -> None:
    started = time.monotonic()
    clock = _ManualClock(started)
    raw_tail = "z" * 50_000

    def handler(**_kwargs: Any) -> dict[str, Any]:
        clock.now = started + 8.5
        return {
            "success": True,
            "answer": "usable evidence",
            "raw_tail": raw_tail,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_read_capability",
                    call_id="call-final-evidence",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "the useful thing"},
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="response-1",
            ),
        ),
        LLMResponse(text_response="The final answer uses usable evidence."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(handler),
        prompt="Research this and answer.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-final-evidence",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "The final answer uses usable evidence."
    assert len(client.calls) == 2
    final_call = client.calls[1]
    assert {
        tool.name for tool in final_call["available_tools"]
    } == {"turn_list_evidence", "turn_read_evidence"}
    assert "continuation" not in final_call
    assert "tool_results" not in final_call
    assert len(final_call["context"]) == 1
    evidence_context = final_call["context"][0]
    assert evidence_context["role"] == "user"
    evidence_payload = json.loads(evidence_context["content"])
    assert (
        evidence_payload["schema_version"]
        == "adaptive_turn_final_synthesis_evidence.v1"
    )
    assert evidence_payload["trust_boundary"] == "untrusted_tool_output"
    assert evidence_payload["evidence"][0]["call_id"] == "call-final-evidence"
    assert "usable evidence" in evidence_payload["evidence"][0]["preview"]
    assert raw_tail not in evidence_context["content"]
    assert len(evidence_context["content"]) < 10_000


def test_many_read_results_share_one_model_context_budget() -> None:
    calls = [
        ToolCall(
            tool_name="turn_invoke_read_capability",
            call_id=f"call-{index}",
            payload={
                "name": "general_read",
                "arguments": {"query": f"query-{index}"},
            },
        )
        for index in range(100)
    ]
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=calls,
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="response-many",
            ),
        ),
        LLMResponse(text_response="Synthesised from the bounded evidence handles."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **kwargs: {
                "success": True,
                "query": kwargs.get("query"),
                "raw": "x" * 10_000,
            }
        ),
        prompt="Use all of these independent reads.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-many-results",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "Synthesised from the bounded evidence handles."
    model_results = client.calls[1]["tool_results"]
    serialised_results = json.dumps(
        [
            {
                "call_id": item.call_id,
                "tool_name": item.tool_name,
                "status": item.status,
                "output": item.output,
            }
            for item in model_results
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(serialised_results) <= 24_000
    assert len(model_results) == 100
    assert all(
        isinstance(item.output, dict) and item.output.get("evidence_id")
        for item in model_results
    )
    assert (
        len(
            json.dumps(
                list(result.evidence_index),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= 24_000
    )


def test_correlation_shell_overflow_switches_to_bounded_final_synthesis() -> None:
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_read_capabilities",
                    call_id=f"catalogue-call-{index}",
                    payload={"offset": index},
                )
                for index in range(500)
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="response-overflow",
            ),
        ),
        LLMResponse(
            text_response=(
                "I stopped the oversized continuation and answered from the "
                "bounded evidence context."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Exercise a mechanically oversized tool-call batch.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-correlation-shell-overflow",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert len(client.calls) == 2
    assert "continuation" not in client.calls[1]
    assert "tool_results" not in client.calls[1]
    assert {
        tool.name for tool in client.calls[1]["available_tools"]
    } == {"turn_list_evidence", "turn_read_evidence"}
    overflow = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_tool_result_batch_overflow"
    )
    assert overflow["tool_call_count"] == 500
    assert (
        overflow["action"]
        == "fresh_final_synthesis_with_pageable_evidence_index"
    )


def test_trusted_gmail_profile_overrides_model_profile_and_aliases() -> None:
    seen_arguments: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.update(kwargs)
        return {"success": True, "messages": []}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_read_capability",
                    call_id="call-gmail",
                    payload={
                        "name": "gmail_list_messages",
                        "arguments": {
                            "profile": "model-chosen-profile",
                            "profile_id": "model-chosen-alias",
                            "identity": "another-model-alias",
                            "query": "newer_than:7d",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="There are no matching messages."),
    )

    result = execute_adaptive_turn(
        gateway=_gmail_gateway(handler),
        prompt="Check my recent mail.",
        context=[],
        llm_client=client,
        model="test-model",
        user_concept_id="#V#person",
        trusted_argument_values={
            "gmail_profile": "trusted-represented-profile",
        },
        turn_id="turn-gmail-profile",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "There are no matching messages."
    assert seen_arguments == {
        "profile": "trusted-represented-profile",
        "query": "newer_than:7d",
    }
    invocation = result.tool_invocations[0]
    assert invocation["payload"]["arguments"]["profile"] == "model-chosen-profile"
    assert invocation["effective_arguments"] == seen_arguments


def test_model_cannot_select_gmail_profile_without_a_trusted_binding() -> None:
    seen_arguments: dict[str, Any] = {}

    def handler(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.update(kwargs)
        return {"success": True, "messages": []}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_read_capability",
                    call_id="call-model-gmail",
                    payload={
                        "name": "gmail_list_messages",
                        "arguments": {
                            "user_id": "model-selected-profile",
                            "query": "newer_than:1d",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="No authorised Gmail profile was available."),
    )

    result = execute_adaptive_turn(
        gateway=_gmail_gateway(handler),
        prompt="Check the selected mail profile.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-model-gmail-profile",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "No authorised Gmail profile was available."
    assert seen_arguments == {}
    assert result.tool_invocations[0]["status"] == "error"
    assert (
        result.tool_invocations[0]["effective_payload"]["error_code"]
        == "read_capability_not_delegated"
    )
