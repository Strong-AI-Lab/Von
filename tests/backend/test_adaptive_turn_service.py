from __future__ import annotations

import json
import time
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import (
    InternalMCPTransport,
    internal_mcp_cancellation_requested,
)
from src.backend.languagemodels.llm_interface import ModelExecutionEligibilityError
from src.backend.languagemodels.structured_tool_calling.types import (
    LLMContinuation,
    LLMResponse,
    StructuredToolContextLimitError,
    StructuredToolProtocolError,
    ToolCall,
    ToolCallError,
    ToolResult,
)
from src.backend.security.access_control import (
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from src.backend.services.adaptive_turn_service import (
    _CONVERSATION_OBSERVATIONS_MAX_BYTES,
    _CONVERSATION_OBSERVATIONS_MAX_ITEMS,
    _CONVERSATION_SITUATION_MAX_CHARS,
    _bound_tool_results_for_model,
    _bounded_conversation_observation_projection,
    _capability_catalogue,
    _compact_context_after_limit,
    _compact_evidence_envelope,
    _compact_evidence_index,
    _effect_result_target_ids,
    _effect_subject_authorised,
    _effect_subject_authority_denial,
    _extract_conversation_situation_sidecar,
    _final_synthesis_context,
    _json_bytes,
    _scope_message,
    _trusted_tool_payload,
    execute_adaptive_turn,
    ordinary_turn_capability_delegation,
)
from src.backend.services.turn_evidence_store import TrustedTurnScope


@pytest.fixture(autouse=True)
def _acknowledge_effect_observation_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import turn_execution_record_service

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        lambda **kwargs: {
            "updated": True,
            "duplicate": False,
            "phase": kwargs.get("phase"),
        },
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


def test_effect_receipt_targets_require_explicit_generic_target_fields() -> None:
    assert _effect_result_target_ids(
        {
            "created_concept_ids": ["#V#created_a", "#V#created_b"],
            "result": {
                "concept_id": "#V#nested_result",
                "type_concept_id": "#V#context_type",
                "candidate_concept_ids": ["#V#candidate"],
                "missing_concept_ids": ["#V#missing"],
                "arguments": {
                    "invented_concept_id": "#V#echoed_argument_claim",
                },
            },
        }
    ) == [
        "#V#created_a",
        "#V#created_b",
        "#V#nested_result",
    ]


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


def test_scope_message_contains_boundaries_and_preserves_request_scope() -> None:
    message = _scope_message(
        TrustedTurnScope(
            user_concept_id="#V#person",
            organisation_concept_id="#V#org",
            namespace="#V#person@org",
        ),
        delegated_count=12,
        final_synthesis=False,
    )

    assert "Authenticated actor: #V#person" in message
    assert "Effect boundary:" in message
    assert "least costly" not in message
    assert "representedness" not in message
    assert "requested outcome and effect cardinality" in message
    assert "smallest bounded candidate set" in message
    assert "must not create, update, or otherwise act on more" in message
    assert "Before another search, read, or hydration" in message
    assert "what unresolved material decision" in message
    assert "Stop retrieving once current evidence supports" in message
    assert "do not broaden retrieval merely to avoid asking" in message
    assert "hydrate candidates sequentially" in message
    assert "Parallel fan-out is appropriate only" in message
    assert "Preserve result-set continuity" in message
    assert "uninspected candidate handles" in message
    assert "A non-match among earlier items" in message
    assert "Replace the result only when you can identify" in message
    assert "reuse existing representation as create-if-absent" in message
    assert "inspect and reuse any exact existing candidate" in message
    assert "stable source or component identifiers" in message
    assert "partial neighbourhood cannot establish absence" in message
    assert "Create only after that bounded reuse check" in message


def test_progressive_evidence_guidance_survives_post_read_continuation() -> None:
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    call_id="call-read-once",
                    tool_name="turn_invoke_capability",
                    payload={"name": "general_read", "arguments": {}},
                )
            ],
        ),
        LLMResponse(text_response="The first bounded read was sufficient."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Find the relevant item.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-progressive-evidence",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The first bounded read was sufficient."
    assert len(client.calls) == 2
    second_system_message = client.calls[1]["system_message"]
    assert "what unresolved material decision" in second_system_message
    assert "Stop retrieving once current evidence supports" in second_system_message
    assert "hydrate candidates sequentially" in second_system_message
    assert "Preserve result-set continuity" in second_system_message
    assert "reuse existing representation as create-if-absent" in second_system_message
    assert "inspect and reuse any exact existing candidate" in second_system_message


def test_scope_message_carries_brief_approval_and_exact_recovery_state() -> None:
    situation = (
        "Proposal: represent the booked journey using source Gmail message "
        "19febb3feda7b024. Durable workflow instance: workflow-trip-123. "
        "Created concept: #V#trip_gmail_19febb3feda7b024. "
        "Unmet outcome: add the three component relations."
    )

    message = _scope_message(
        TrustedTurnScope(
            user_concept_id="#V#person",
            organisation_concept_id="#V#org",
            namespace="#V#person@org",
        ),
        delegated_count=12,
        final_synthesis=False,
        conversation_id="conversation-trip",
        conversation_situation=situation,
    )

    assert situation in message
    assert "Interpret a brief follow-up" in message
    assert "most recent sufficiently concrete proposal" in message
    assert "Do not make the user repeat internal identifiers" in message
    assert "carry it out rather than merely restating it" in message
    assert "complete purpose index" in message
    assert "typed recovery affordance" in message
    assert "same requested semantic object and effect cardinality" in message
    assert "normally use it in the same turn" in message
    assert "complete only unmet postconditions" in message
    assert "never repeat a confirmed effect" in message
    assert "preserve its exact grounded candidate identifiers" in message
    assert "unresolved create-versus-reuse status" in message
    assert "Do not leave the only stable identity solely" in message
    assert "including when you made a proposal intended for later approval" in message


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


def _effect_gateway(
    handler: Any,
    *,
    write_timeout_sec: float = 1.0,
    effect_output_schema: Schema | None = None,
    effect_admission_window_sec: float | None = None,
    include_scoped_assertion: bool = False,
    hard_timeout_enabled: bool = False,
) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="general_read",
            handler=lambda **kwargs: handler("general_read", kwargs),
            input_schema=Schema(allow_unknown=True),
            category="read",
        )
    )
    for name, subject_argument in (
        ("create_concepts", None),
        ("upsert_text_relation", "concept_id"),
        ("add_relationship", "source_id"),
        *((("upsert_scoped_assertion", None),) if include_scoped_assertion else ()),
    ):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda _name=name, **kwargs: handler(_name, kwargs),
                input_schema=Schema(allow_unknown=True),
                output_schema=effect_output_schema,
                category="write",
                ordinary_turn_effect=True,
                hard_timeout_enabled=hard_timeout_enabled,
                effect_admission_window_sec=effect_admission_window_sec,
                ordinary_turn_mutation_subject_argument=subject_argument,
                ordinary_turn_trusted_argument_bindings=(
                    {
                        "namespace": "turn_namespace",
                        "created_by_concept_id": "actor_user_concept_id",
                    }
                    if name == "create_concepts"
                    else (
                        {"namespace": "turn_namespace"}
                        if name == "upsert_text_relation"
                        else (
                            {
                                "acting_user_concept_id": ("actor_user_concept_id"),
                                "organisation_concept_id": (
                                    "actor_organisation_concept_id"
                                ),
                                "namespace": "turn_namespace",
                            }
                            if name == "upsert_scoped_assertion"
                            else None
                        )
                    )
                ),
                ordinary_turn_fixed_arguments=(
                    {
                        "organisation_concept_id": None,
                        "org_id": None,
                        "scope_mode": "user_org_default",
                        "visibility_scope_mode": None,
                    }
                    if name == "create_concepts"
                    else (
                        {"provenance": None}
                        if name == "upsert_text_relation"
                        else (
                            {"canonical_publication": False}
                            if name == "upsert_scoped_assertion"
                            else None
                        )
                    )
                ),
            )
        )
    catalogue.register(
        MethodDefinition(
            name="other_write",
            handler=lambda **kwargs: handler("other_write", kwargs),
            input_schema=Schema(allow_unknown=True),
            category="write",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(
            read_timeout_sec=1.0,
            write_timeout_sec=write_timeout_sec,
        ),
        enabled=True,
    )


def _workflow_gateway(
    handler: Any,
    *,
    hard_timeout_enabled: bool = True,
    instance_handler: Any | None = None,
) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="general_read",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(allow_unknown=True),
            category="read",
            ordinary_turn_public=True,
        )
    )
    if instance_handler is not None:
        catalogue.register(
            MethodDefinition(
                name="workflow_get_instance",
                handler=instance_handler,
                input_schema=Schema(
                    required={"instance_id": str},
                    optional={
                        "await_terminal": bool,
                        "timeout_seconds": (int, float),
                        "poll_interval_seconds": (int, float),
                    },
                    allow_unknown=False,
                ),
                output_schema=Schema(
                    required={"success": bool},
                    allow_unknown=True,
                ),
                category="read",
            )
        )
    catalogue.register(
        MethodDefinition(
            name="workflow_execute",
            handler=handler,
            input_schema=Schema(
                required={"workflow_id": str},
                optional={
                    "user_id": str,
                    "org_id": (str, type(None)),
                    "namespace": str,
                    "inputs": dict,
                    "max_retries": int,
                    "await_terminal": bool,
                    "timeout_seconds": (int, float),
                    "poll_interval_seconds": (int, float),
                    "include_step_result_envelopes": bool,
                    "include_trace": bool,
                    "source_event_type": str,
                    "source_event_id": str,
                    "event_idempotency_key": str,
                },
                allow_unknown=False,
            ),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="write",
            timeout_sec=1.0,
            hard_timeout_enabled=hard_timeout_enabled,
            effect_admission_window_sec=0.01,
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(
            read_timeout_sec=1.0,
            write_timeout_sec=1.0,
        ),
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


class _TimedSequenceClient(_SequenceClient):
    def __init__(
        self,
        clock: _ManualClock,
        *timed_responses: tuple[float, Any],
    ) -> None:
        super().__init__(*(item[1] for item in timed_responses))
        self.clock = clock
        self.response_times = [item[0] for item in timed_responses]

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        self.clock.now = self.response_times[len(self.calls)]
        return super().generate_with_tools(prompt, available_tools, **kwargs)


class _HydrationAdvisoryClient:
    def __init__(
        self,
        clock: _ManualClock,
        *,
        native_continuation: bool,
        research_advisory_at: float,
    ) -> None:
        self.clock = clock
        self.native_continuation = native_continuation
        self.research_advisory_at = research_advisory_at
        self.calls: list[dict[str, Any]] = []
        self.received_evidence_outputs: list[dict[str, Any]] = []

    def _continuation(self, response_id: str) -> LLMContinuation | None:
        if not self.native_continuation:
            return None
        return LLMContinuation(
            provider="test",
            api_surface="responses",
            model="test-model",
            response_id=response_id,
        )

    def _latest_tool_output(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self.native_continuation:
            tool_results = kwargs.get("tool_results")
            assert isinstance(tool_results, list)
            assert len(tool_results) == 1
            assert isinstance(tool_results[0], ToolResult)
            assert isinstance(tool_results[0].output, dict)
            return dict(tool_results[0].output)

        context = kwargs.get("context")
        assert isinstance(context, list)
        tool_messages = [
            item
            for item in context
            if isinstance(item, dict) and item.get("role") == "tool"
        ]
        assert tool_messages
        output = json.loads(tool_messages[-1]["content"])
        assert isinstance(output, dict)
        return output

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        call_number = len(self.calls)
        self.calls.append(
            {
                "prompt": prompt,
                "available_tools": list(available_tools),
                **kwargs,
            }
        )
        if call_number == 0:
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="call-source-evidence",
                        payload={
                            "name": "general_read",
                            "arguments": {"query": "find the source"},
                        },
                    )
                ],
                continuation=self._continuation("response-source"),
            )
        if call_number == 1:
            envelope = self._latest_tool_output(kwargs)
            self.received_evidence_outputs.append(envelope)
            self.clock.now = self.research_advisory_at + 0.5
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_read_evidence",
                        call_id="call-hydrated-evidence",
                        payload={
                            "evidence_id": envelope["evidence_id"],
                            "json_pointer": "/record/z_summary",
                            "max_chars": 4_000,
                        },
                    )
                ],
                continuation=self._continuation("response-hydration"),
            )
        if call_number == 2:
            hydrated_slice = self._latest_tool_output(kwargs)
            self.received_evidence_outputs.append(hydrated_slice)
            return LLMResponse(
                text_response=(
                    "The final answer uses the explicitly hydrated evidence."
                )
            )
        raise AssertionError("unexpected extra model call")


class _RootAliasHydrationClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.hydrated_slice: dict[str, Any] | None = None

    @staticmethod
    def _latest_tool_output(kwargs: dict[str, Any]) -> dict[str, Any]:
        tool_results = kwargs.get("tool_results")
        assert isinstance(tool_results, list)
        assert len(tool_results) == 1
        assert isinstance(tool_results[0], ToolResult)
        assert isinstance(tool_results[0].output, dict)
        return dict(tool_results[0].output)

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: list[Any],
        **kwargs: Any,
    ) -> LLMResponse:
        call_number = len(self.calls)
        self.calls.append(
            {
                "prompt": prompt,
                "available_tools": list(available_tools),
                **kwargs,
            }
        )
        if call_number == 0:
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="root-alias-source",
                        payload={
                            "name": "general_read",
                            "arguments": {"query": "canonical record"},
                        },
                    )
                ],
                continuation=LLMContinuation(
                    provider="test",
                    api_surface="responses",
                    model="test-model",
                    response_id="root-source-response",
                ),
            )
        if call_number == 1:
            envelope = self._latest_tool_output(kwargs)
            return LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_read_evidence",
                        call_id="root-alias-hydration",
                        payload={
                            "evidence_id": envelope["evidence_id"],
                            "json_pointer": "/",
                            "max_chars": 4_000,
                        },
                    )
                ],
                continuation=LLMContinuation(
                    provider="test",
                    api_surface="responses",
                    model="test-model",
                    response_id="root-hydration-response",
                ),
            )
        if call_number == 2:
            self.hydrated_slice = self._latest_tool_output(kwargs)
            return LLMResponse(text_response="The canonical identifier was retained.")
        raise AssertionError("unexpected model call")


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
    assert "all other writes are unavailable" in system_message
    assert {tool.name for tool in client.calls[0]["available_tools"]} == {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    catalogue_tool = next(
        tool
        for tool in client.calls[0]["available_tools"]
        if tool.name == "turn_capabilities"
    )
    assert "complete unranked compact purpose index" in catalogue_tool.description
    assert "represented workflows are peers" in catalogue_tool.description
    assert "representedness does not rank a plan" in catalogue_tool.description
    assert "smallest plan that can produce" in catalogue_tool.description
    assert "all alternatives being compared together" in catalogue_tool.description
    assert set(catalogue_tool.input_schema["properties"]) == {
        "query",
        "names",
        "offset",
        "limit",
    }


def test_model_eligibility_denial_is_returned_in_ordinary_language() -> None:
    denial = ModelExecutionEligibilityError(
        "OpenAI model 'gpt-5.6-terra' is not enabled for the current user or "
        "organisation.",
        provider="openai",
        model="gpt-5.6-terra",
    )
    client = _SequenceClient(denial)

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Help with this.",
        context=[],
        llm_client=client,
        model="gpt-5.6-terra",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="model-not-enabled",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_not_enabled"
    assert result.response_text == str(denial)
    assert len(client.calls) == 1
    assert result.llm_calls[0]["call_id"] == "model-not-enabled:llm:1"
    assert result.llm_calls[0]["requested_model"] == "gpt-5.6-terra"
    assert result.llm_calls[0]["selected_model"] == "gpt-5.6-terra"
    assert result.llm_calls[0]["effective_model"] is None
    assert result.llm_calls[0]["model_identity_source"] is None
    assert result.llm_calls[0]["provider_request_sent"] is False


def test_terminal_answer_can_update_a_revisable_conversation_situation() -> None:
    current_situation = (
        "We are deciding how to represent an evolving conversation situation. "
        "The storage choice is still open."
    )
    revised_situation = (
        "We are implementing a lightweight, inspectable text description of the "
        "conversation situation. It remains provisional and may contain "
        "sub-situations. The next step is to validate same-call continuity."
    )
    client = _SequenceClient(
        LLMResponse(
            text_response=(
                "I have implemented the smallest same-call carrier seam.\n\n"
                "<von_conversation_situation>\n"
                f"{revised_situation}\n"
                "</von_conversation_situation>"
            )
        )
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Continue with the agreed first step.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-with-situation",
        conversation_id="conversation-123",
        conversation_situation=current_situation,
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == (
        "I have implemented the smallest same-call carrier seam."
    )
    assert "<von_conversation_situation>" not in result.response_text
    assert revised_situation not in result.response_text
    assert result.conversation_situation == revised_situation

    system_message = client.calls[0]["system_message"]
    assert "conversation-123" in system_message
    assert current_situation in system_message
    assert "provisional, revisable theory" in system_message
    assert "one focused question" in system_message
    assert "unavailable information is distinct from performative permission" in (
        system_message
    )
    assert "<von_conversation_situation>" in system_message


def test_material_unknown_is_elicited_then_resolved_through_shared_situation() -> None:
    outstanding_situation = (
        "Objective: prepare the corpus comparison. "
        "Material unknown: which corpus the user intends. "
        "Outstanding question: Which corpus should I use? "
        "Independent work can continue on the comparison structure."
    )
    first_client = _SequenceClient(
        LLMResponse(
            text_response=(
                "Which corpus should I use?\n"
                "<von_conversation_situation>\n"
                f"{outstanding_situation}\n"
                "</von_conversation_situation>"
            )
        )
    )

    first_turn = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Prepare the comparison.",
        context=[],
        llm_client=first_client,
        model="test-model",
        conversation_id="conversation-elicitation",
        conversation_situation=(
            "Objective: prepare the corpus comparison. "
            "The intended corpus is not yet known."
        ),
        turn_id="turn-elicitation-question",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert first_turn.response_text == "Which corpus should I use?"
    assert first_turn.response_text.count("?") == 1
    assert "permission" not in first_turn.response_text.lower()
    assert first_turn.conversation_situation == outstanding_situation

    resolved_situation = (
        "Objective: compare the British National Corpus with the existing "
        "baseline. The user selected the British National Corpus, resolving "
        "the prior material unknown. Next step: compute the comparison."
    )
    second_client = _SequenceClient(
        LLMResponse(
            text_response=(
                "I’ll use the British National Corpus and proceed with the "
                "comparison.\n"
                "<von_conversation_situation>\n"
                f"{resolved_situation}\n"
                "</von_conversation_situation>"
            )
        )
    )
    second_turn = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Use the British National Corpus.",
        context=[
            {"role": "assistant", "content": first_turn.response_text},
            {"role": "user", "content": "Use the British National Corpus."},
        ],
        llm_client=second_client,
        model="test-model",
        conversation_id="conversation-elicitation",
        conversation_situation=first_turn.conversation_situation,
        turn_id="turn-elicitation-answer",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert outstanding_situation in second_client.calls[0]["system_message"]
    assert second_turn.response_text == (
        "I’ll use the British National Corpus and proceed with the comparison."
    )
    assert "?" not in second_turn.response_text
    assert second_turn.conversation_situation == resolved_situation


def test_tool_resolvable_unknown_is_observed_without_questioning_the_user() -> None:
    invoked_queries: list[str] = []

    def _read(**kwargs: Any) -> dict[str, Any]:
        invoked_queries.append(str(kwargs.get("query")))
        return {"success": True, "current_corpus": "Lancaster-Oslo/Bergen"}

    revised_situation = (
        "Objective: continue the corpus comparison. The canonical configuration "
        "says the current corpus is Lancaster-Oslo/Bergen; no user answer was "
        "needed."
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-corpus",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "current configured corpus"},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The configured corpus is Lancaster-Oslo/Bergen, so I can "
                "continue.\n"
                "<von_conversation_situation>\n"
                f"{revised_situation}\n"
                "</von_conversation_situation>"
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(_read),
        prompt="Continue with the configured corpus.",
        context=[],
        llm_client=client,
        model="test-model",
        conversation_id="conversation-tool-resolvable",
        conversation_situation=(
            "Objective: continue the corpus comparison. "
            "The current configured corpus is unknown but available via tools."
        ),
        turn_id="turn-tool-resolvable",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked_queries == ["current configured corpus"]
    assert "?" not in result.response_text
    assert result.conversation_situation == revised_situation


def test_machine_observations_are_bounded_separate_and_not_redispatched() -> None:
    observations = [
        {
            "observation_id": observation_id,
            "effect_id": f"effect-{index}",
            "outcome": "succeeded",
            "canonical_terminal": True,
        }
        for index, observation_id in enumerate(
            [
                "omitted-old-a",
                "omitted-old-b",
                "retained-00",
                "retained-01",
                "retained-02",
                "retained-03",
                "retained-04",
                "retained-05",
                "retained-06",
                "retained-07",
            ]
        )
    ]
    projection = _bounded_conversation_observation_projection(observations)
    projection_with_prior_omissions = _bounded_conversation_observation_projection(
        observations,
        omitted_before=5,
    )

    assert projection is not None
    assert projection_with_prior_omissions is not None
    assert len(projection["observations"]) == _CONVERSATION_OBSERVATIONS_MAX_ITEMS
    assert projection["observations"] == observations[-8:]
    assert projection["omitted_count"] == 2
    assert projection_with_prior_omissions["omitted_count"] == 7
    assert (
        len(_json_bytes(projection["observations"]))
        <= _CONVERSATION_OBSERVATIONS_MAX_BYTES
    )

    client = _SequenceClient(
        LLMResponse(text_response="The recorded effect succeeded.")
    )
    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="What happened to the pending effect?",
        context=[],
        llm_client=client,
        model="test-model",
        conversation_id="conversation-with-observations",
        conversation_situation="The effect was pending when the previous turn ended.",
        conversation_observations=observations,
        conversation_observation_state={"omitted_count": 3},
        turn_id="turn-with-observations",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The recorded effect succeeded."
    system_message = client.calls[0]["system_message"]
    assert "omitted-old-a" not in system_message
    assert "omitted-old-b" not in system_message
    assert "retained-00" in system_message
    assert "retained-07" in system_message
    assert '"omitted_count": 5' in system_message
    assert "projected separately from the provisional plain-text situation" in (
        system_message
    )
    assert "Never redispatch an effect merely to learn an outcome already recorded" in (
        system_message
    )
    assert "reconcile that outcome into the visible answer" in system_message


def test_oversized_machine_observation_is_omitted_without_partial_projection() -> None:
    projection = _bounded_conversation_observation_projection(
        [
            {
                "observation_id": "too-large",
                "payload": "x" * (_CONVERSATION_OBSERVATIONS_MAX_BYTES + 1),
            }
        ]
    )

    assert projection is not None
    assert projection["observations"] == []
    assert projection["omitted_count"] == 1


def test_absent_situation_sidecar_preserves_the_current_situation() -> None:
    current_situation = "The user and Von are still choosing the next useful step."
    client = _SequenceClient(LLMResponse(text_response="Here is the answer."))

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Answer from the situation already established.",
        context=[],
        llm_client=client,
        model="test-model",
        conversation_id="conversation-unchanged",
        conversation_situation=current_situation,
        turn_id="turn-situation-unchanged",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "Here is the answer."
    assert result.conversation_situation == current_situation


def test_situation_sidecar_without_visible_answer_is_a_non_answer() -> None:
    current_situation = "The user still needs a visible answer."
    client = _SequenceClient(
        LLMResponse(
            text_response=(
                "<von_conversation_situation>\n"
                "Updated theory but no user work product.\n"
                "</von_conversation_situation>"
            )
        )
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Give me the answer.",
        context=[],
        llm_client=client,
        model="test-model",
        conversation_id="conversation-sidecar-only",
        conversation_situation=current_situation,
        turn_id="turn-sidecar-only",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_non_answer"
    assert result.response_text == (
        "The model returned a conversation-situation update but no "
        "user-visible answer."
    )
    assert result.conversation_situation == current_situation


@pytest.mark.parametrize(
    "raw_response",
    [
        (
            "Visible answer.\n"
            "<von_conversation_situation>\n"
            "Missing the terminal tag."
        ),
        "Visible answer.\n</von_conversation_situation>",
        (
            "Visible answer.\n"
            "<von_conversation_situation>Updated.</von_conversation_situation>\n"
            "Unexpected visible suffix."
        ),
        (
            "Visible answer.\n"
            "<von_conversation_situation>First.</von_conversation_situation>\n"
            "<von_conversation_situation>Second.</von_conversation_situation>"
        ),
        (
            "Visible answer.\n"
            "<von_conversation_situation >Protocol variant.</"
            "von_conversation_situation>"
        ),
        (
            "Visible answer.\n"
            "<VON_CONVERSATION_SITUATION>Protocol variant.</"
            "VON_CONVERSATION_SITUATION>"
        ),
        (
            "Visible answer.\n"
            "<von_conversation_situation>"
            + ("x" * (_CONVERSATION_SITUATION_MAX_CHARS + 1))
            + "</von_conversation_situation>"
        ),
    ],
)
def test_invalid_situation_sidecar_is_hidden_and_preserves_current_state(
    raw_response: str,
) -> None:
    visible_text, situation = _extract_conversation_situation_sidecar(
        raw_response,
        current_situation="Original situation.",
    )

    assert visible_text == "Visible answer."
    assert "von_conversation_situation" not in visible_text
    assert situation == "Original situation."


def test_model_slash_evidence_pointer_hydrates_the_whole_result() -> None:
    client = _RootAliasHydrationClient()

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "": "RFC slash would select this empty-key member.",
                "canonical_concept_id": "#V#stable_readback_id",
                "success": True,
            }
        ),
        prompt="Read the canonical record.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="root-alias-hydration",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == "The canonical identifier was retained."
    assert client.hydrated_slice is not None
    assert client.hydrated_slice["success"] is True
    assert client.hydrated_slice["selector"]["json_pointer"] is None
    assert (
        json.loads(client.hydrated_slice["content"])["canonical_concept_id"]
        == "#V#stable_readback_id"
    )
    read_tool = next(
        tool
        for tool in client.calls[0]["available_tools"]
        if tool.name == "turn_read_evidence"
    )
    assert "root alias" in read_tool.description
    assert "root alias" in (
        read_tool.input_schema["properties"]["json_pointer"]["description"]
    )


def test_ordinary_delegation_is_actor_capability_not_every_read_method() -> None:
    gateway = _delegation_gateway()

    without_mail = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
    )
    with_mail = ordinary_turn_capability_delegation(
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
        ordinary_turn_capability_delegation(
            gateway,
            user_concept_id="#V#person",
            trusted_argument_values={
                "gmail_profile": "represented-profile",
            },
        )
        == ()
    )

    gateway.enable()
    assert ordinary_turn_capability_delegation(
        gateway,
        user_concept_id=None,
    ) == ("search_arxiv",)


def test_server_bound_capability_argument_is_not_model_visible() -> None:
    gateway = _gmail_gateway(lambda **_kwargs: {"success": True})
    delegated = ordinary_turn_capability_delegation(
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


def test_capability_catalogue_rejects_placeholder_description_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import tool_metadata_service

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="grounded_direct_read",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(optional={"query": str}),
            description=(
                "Read grounded records for one anchor and return bounded "
                "content-bearing evidence."
            ),
            ordinary_turn_public=True,
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "_load_from_vontology",
        lambda: {
            "grounded_direct_read": tool_metadata_service.ToolMetadata(
                tool_name="grounded_direct_read",
                concept_id="#V#grounded_direct_read_tool",
                description=("Internal MCP metadata concept for grounded_direct_read."),
            )
        },
    )
    tool_metadata_service.invalidate_cache()
    try:
        capability = _capability_catalogue(
            gateway,
            ("grounded_direct_read",),
            {"names": ["grounded_direct_read"]},
        )["capabilities"][0]
    finally:
        tool_metadata_service.invalidate_cache()

    assert capability["description"] == (
        "Read grounded records for one anchor and return bounded "
        "content-bearing evidence."
    )


def test_effect_delegation_is_authenticated_and_exactly_metadata_marked() -> None:
    gateway = _effect_gateway(lambda _name, _arguments: {"success": True})
    trusted = {
        "turn_namespace": "#V#person@org",
        "actor_user_concept_id": "#V#person",
    }

    assert (
        ordinary_turn_capability_delegation(
            gateway,
            user_concept_id=None,
            trusted_argument_values=trusted,
        )
        == ()
    )
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values=trusted,
    )

    assert set(delegated) == {
        "general_read",
        "create_concepts",
        "upsert_text_relation",
        "add_relationship",
    }
    assert "other_write" not in delegated


def test_advisory_effect_does_not_project_inactive_admission_boundary() -> None:
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.5,
        effect_admission_window_sec=0.125,
    )
    trusted = {
        "turn_namespace": "#V#person@org",
        "actor_user_concept_id": "#V#person",
    }
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values=trusted,
    )

    catalogue_entry = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["create_concepts"]},
    )["capabilities"][0]

    assert gateway.get_method_effect_admission_window_sec("create_concepts") is None
    assert gateway.get_method_effect_admission_window_sec("general_read") is None
    assert catalogue_entry["semantic_effect"] is True
    assert "minimum_effect_window_seconds" not in catalogue_entry

    clock = _ManualClock(100.0)
    client = _SequenceClient(LLMResponse(text_response="A useful answer."))
    execute_adaptive_turn(
        gateway=gateway,
        prompt="Help with this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-window-scope",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert "Elapsed-time budgets are advisory" in client.calls[0]["system_message"]
    assert "Model and capability elapsed thresholds are advisory too" in (
        client.calls[0]["system_message"]
    )


def test_default_final_answer_reserve_is_nonzero_and_clamped() -> None:
    gateway = _effect_gateway(lambda _name, _arguments: {"success": True})
    clock = _ManualClock(100.0)
    client = _SequenceClient(LLMResponse(text_response="A useful answer."))

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Help with this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="default-answer-reserve",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        clock=clock,
    )

    assert "Elapsed-time budgets are advisory" in client.calls[0]["system_message"]
    allocation = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_budget_allocation"
    )
    assert allocation["effective_final_answer_reserve_seconds"] == 4.0
    assert allocation["final_answer_reserve_source"] == "environment_or_default"
    assert allocation["explicit_zero_override"] is False
    assert allocation["enforcement"] == "advisory"
    assert allocation["model_call_advisory_seconds"] == 120.0
    assert allocation["model_call_hard_timeout_seconds"] is None


def test_create_effect_scope_is_hidden_and_server_overrides_spoofed_values() -> None:
    seen: dict[str, Any] = {}

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "create_concepts"
        seen.update(arguments)
        return {"success": True, "effect_status": "succeeded", "changed": True}

    gateway = _effect_gateway(handler)
    delegated = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#person",
        trusted_argument_values={
            "turn_namespace": "#V#person@org",
            "actor_user_concept_id": "#V#person",
        },
    )
    capability = _capability_catalogue(
        gateway,
        delegated,
        {"names": ["create_concepts"]},
    )["capabilities"][0]
    assert {
        "namespace",
        "created_by_concept_id",
        "organisation_concept_id",
        "scope_mode",
        "visibility_scope_mode",
    }.isdisjoint(capability["input_schema"]["properties"])

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-1",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "namespace": "#V#spoof@other",
                            "created_by_concept_id": "#V#spoof",
                            "organisation_concept_id": "#V#other",
                            "org_id": "#V#other",
                            "scope_mode": "global_general",
                            "visibility_scope_mode": "global_general",
                            "concepts": [{"name": "Bounded representation"}],
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="Created and ready for canonical read-back."),
    )
    execute_adaptive_turn(
        gateway=gateway,
        prompt="Represent this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="create-scope",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert seen["namespace"] == "#V#person@org"
    assert seen["created_by_concept_id"] == "#V#person"
    assert seen["organisation_concept_id"] is None
    assert seen["org_id"] is None
    assert seen["scope_mode"] == "user_org_default"
    assert seen["visibility_scope_mode"] is None


def test_invalid_effect_arguments_are_returned_for_correction_without_poisoning_turn() -> (
    None
):
    invoked: list[dict[str, Any]] = []
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="record_source_processing_marker",
            handler=lambda **arguments: (
                invoked.append(dict(arguments))
                or {
                    "success": True,
                    "effect_status": "succeeded",
                    "changed": True,
                }
            ),
            input_schema=Schema(
                required={
                    "source_system": str,
                    "source_profile": str,
                    "source_item_id": str,
                },
                allow_unknown=False,
                description="Record one source-processing marker.",
            ),
            category="write",
            ordinary_turn_effect=True,
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(write_timeout_sec=1.0),
        enabled=True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="marker-without-profile",
                    payload={
                        "name": "record_source_processing_marker",
                        "arguments": {
                            "source_system": "gmail",
                            "source_item_id": "message-1",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="marker-with-profile",
                    payload={
                        "name": "record_source_processing_marker",
                        "arguments": {
                            "source_system": "gmail",
                            "source_profile": "personal-gmail",
                            "source_item_id": "message-1",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The corrected marker write succeeded."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Record the exact source marker.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="correct-invalid-effect-arguments",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == [
        {
            "source_system": "gmail",
            "source_profile": "personal-gmail",
            "source_item_id": "message-1",
        }
    ]
    validation_message = next(
        item
        for item in client.calls[1]["context"]
        if item.get("tool_call_id") == "marker-without-profile"
    )
    validation_result = json.loads(validation_message["content"])
    assert validation_result["status"] == "not_started"
    assert validation_result["changed"] is False
    assert validation_result["error_code"] == "capability_arguments_invalid"
    assert "source_profile" in validation_result["preview"]
    assert result.terminal_status == "completed"
    assert result.response_text == "The corrected marker write succeeded."
    assert result.tool_invocations[0]["effect_status"] == "not_started"
    assert result.tool_invocations[0]["turn_finality_required"] is False
    assert result.tool_invocations[1]["effect_status"] == "succeeded"


def test_effect_subject_authority_matches_actor_or_organisation_scope(
    monkeypatch,
) -> None:
    from src.backend.db import mongo_client

    class _Collection:
        relationships: dict[str, Any] = {}

        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": dict(self.relationships)}

    collection = _Collection()
    monkeypatch.setattr(mongo_client, "get_concepts_collection", lambda: collection)
    scope = TrustedTurnScope(
        user_concept_id="#V#person",
        organisation_concept_id="#V#org",
        namespace="#V#person@org",
    )

    collection.relationships = {
        "#V#specific_to_user": ["#V#other_person"],
        "#V#specific_to_organisation": ["#V#org"],
    }
    assert _effect_subject_authorised("#V#subject", scope) is True
    collection.relationships["#V#specific_to_user"] = ["#V#person"]
    assert _effect_subject_authorised("#V#subject", scope) is True
    collection.relationships = {
        "#V#specific_to_organisation": ["#V#org"],
    }
    assert _effect_subject_authorised("#V#subject", scope) is True
    collection.relationships = {
        "#V#specific_to_user": ["#V#other_person"],
        "#V#specific_to_organisation": ["#V#other_org"],
    }
    assert _effect_subject_authorised("#V#subject", scope) is False
    collection.relationships = {}
    assert _effect_subject_authorised("#V#subject", scope) is False
    assert _effect_subject_authorised("#V#person", scope) is True
    assert _effect_subject_authorised("#V#org", scope) is False


def test_ordinary_effect_authorises_actor_profile_without_visibility_lookup(
    monkeypatch,
) -> None:
    from src.backend.db import mongo_client

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: (_ for _ in ()).throw(
            AssertionError("actor identity should not require visibility lookup")
        ),
    )
    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="actor-profile",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#person",
                            "predicate": "#V#hasResearchInterest",
                            "target": "#V#topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The actor profile was updated."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Add this research interest to my profile.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="actor-profile",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == ["add_relationship"]
    assert result.tool_invocations[0]["effect_status"] == "succeeded"
    assert result.tool_invocations[0]["changed"] is True


def test_finality_fallback_rejects_pre_reconciliation_situation() -> None:
    current_situation = "The representation effect has not yet been observed."
    client = _SequenceClient(
        LLMResponse(
            text_response=(
                "I have not changed anything.\n"
                "<von_conversation_situation>\n"
                "The representation definitely did not change.\n"
                "</von_conversation_situation>"
            ),
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="effect-before-model-failure",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [{"name": "A real represented concept"}]
                        },
                    },
                )
            ],
        ),
        TimeoutError("final model call failed"),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            lambda _name, _arguments: {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
            }
        ),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-before-model-failure",
        conversation_id="conversation-with-effect-failure",
        conversation_situation=current_situation,
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_error"
    assert result.effect_finality_fallback is True
    assert "will not claim that nothing changed" in result.response_text
    assert "I have not changed anything" not in result.response_text
    assert "von_conversation_situation" not in result.response_text
    assert result.conversation_situation == current_situation
    assert result.tool_invocations[0]["effect_status"] == "succeeded"
    assert result.tool_invocations[0]["changed"] is True


def test_model_error_after_read_rejects_interim_situation_sidecar() -> None:
    current_situation = "The canonical corpus is not yet observed."
    client = _SequenceClient(
        LLMResponse(
            text_response=(
                "I am checking the canonical corpus.\n"
                "<von_conversation_situation>\n"
                "Stale interim theory before the read result.\n"
                "</von_conversation_situation>"
            ),
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-model-failure",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "canonical corpus"},
                    },
                )
            ],
        ),
        TimeoutError("final model call failed"),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "canonical_corpus": "Lancaster-Oslo/Bergen",
            }
        ),
        prompt="Which corpus is canonical?",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="read-before-model-failure",
        conversation_id="conversation-read-failure",
        conversation_situation=current_situation,
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "model_error"
    assert result.response_text == "I am checking the canonical corpus."
    assert "von_conversation_situation" not in result.response_text
    assert result.conversation_situation == current_situation


def test_post_handler_output_validation_failure_is_indeterminate() -> None:
    committed: list[str] = []

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        committed.append("#V#committed_before_invalid_receipt")
        return {"success": True}

    client = _SequenceClient(
        LLMResponse(
            text_response="I have not changed anything.",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invalid-receipt-after-commit",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [{"name": "Committed before invalid receipt"}]
                        },
                    },
                )
            ],
        ),
        TimeoutError("final model call failed"),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            effect_output_schema=Schema(
                required={"success": bool, "changed": bool},
                allow_unknown=True,
            ),
        ),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="invalid-receipt-after-commit",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert committed == ["#V#committed_before_invalid_receipt"]
    assert result.terminal_status == "model_error"
    assert result.effect_finality_fallback is True
    assert "will not claim that nothing changed" in result.response_text
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"
    assert result.tool_invocations[0]["changed"] is None


@pytest.mark.parametrize(
    ("answer_reserve", "effect_finished_at"),
    [
        (0.0, 8.1),
        (2.0, 8.5),
    ],
    ids=["evidence-capable-final", "answer-only-final"],
)
def test_effect_removes_false_draft_from_fresh_final_context(
    answer_reserve: float,
    effect_finished_at: float,
) -> None:
    started = time.monotonic()
    clock = _ManualClock(started)

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        clock.now = started + effect_finished_at
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    false_draft = "I have not changed anything."
    client = _SequenceClient(
        LLMResponse(
            text_response=false_draft,
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=f"effect-before-{answer_reserve}",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "A represented concept"}]},
                    },
                )
            ],
        ),
        LLMResponse(text_response="The represented concept was created."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"effect-draft-{answer_reserve}",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=answer_reserve,
        clock=clock,
    )

    assert result.response_text == "The represented concept was created."
    assert len(client.calls) == 2
    final_call = client.calls[1]
    assert {tool.name for tool in final_call["available_tools"]} == {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    assert not any(
        item.get("role") == "assistant" and item.get("content") == false_draft
        for item in final_call["context"]
    )
    assert false_draft not in json.dumps(final_call["context"])


def test_late_effect_completion_is_persisted_without_rewriting_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import turn_execution_record_service

    release_handler = Event()
    observation_persisted = Event()
    persisted: list[dict[str, Any]] = []

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        while not internal_mcp_cancellation_requested():
            time.sleep(0.001)
        assert release_handler.wait(timeout=10.0)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_ids": ["#V#late_real_concept"],
        }

    def persist_phase(**kwargs: Any) -> dict[str, Any]:
        persisted.append(dict(kwargs))
        if kwargs.get("phase") == "late_terminal":
            observation_persisted.set()
        return {"updated": True}

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        persist_phase,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="late-effect",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "A late real concept"}]},
                    },
                )
            ],
        ),
        TimeoutError("model failed after the effect timeout"),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            write_timeout_sec=0.02,
            hard_timeout_enabled=True,
        ),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="late-effect-request",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.effect_finality_fallback is True
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"
    assert "1 indeterminate" in result.response_text

    release_handler.set()
    assert observation_persisted.wait(timeout=1.0)
    late_phases = [item for item in persisted if item.get("phase") == "late_terminal"]
    assert len(late_phases) == 1
    durable = late_phases[0]
    assert durable["request_id"] == "late-effect-request"
    assert durable["effect_id"] == result.tool_invocations[0]["effect_id"]
    assert durable["observation"]["effect_status"] == "succeeded"
    assert durable["observation"]["changed"] is True
    # The already returned turn remains an honest point-in-time snapshot.
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"


def test_effect_uses_its_method_liveness_window_not_a_turn_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ManualClock()
    observed_deadlines: list[float | None] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=3.0,
        effect_admission_window_sec=3.0,
    )

    def invoke(
        method_name: str,
        _arguments: dict[str, Any],
        *,
        deadline_monotonic: float | None,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        observed_deadlines.append(deadline_monotonic)
        return SimpleNamespace(
            payload={
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "mutation_outcome": "completed",
                "outcome_finality": "terminal_for_turn",
                "private_detail": "x" * 10_000,
            },
            execution_id=f"execution-{method_name}",
            timed_out=False,
            telemetry_metadata=lambda: {
                "schema_version": "internal_mcp_transport.v1",
                "outcome": "completed",
            },
        )

    monkeypatch.setattr(gateway, "invoke", invoke)
    client = _TimedSequenceClient(
        clock,
        (
            3.5,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="protected-window-effect",
                        payload={
                            "name": "create_concepts",
                            "arguments": {"concepts": [{"name": "Protected"}]},
                        },
                    )
                ],
            ),
        ),
        (3.6, LLMResponse(text_response="The effect completed.")),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Represent this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="protected-effect-window",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=6,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "The effect completed."
    assert observed_deadlines == [None]
    invocation = result.tool_invocations[0]
    assert invocation["effect_status"] == "succeeded"
    assert invocation["mutation_outcome"] == "completed"
    assert invocation["outcome_finality"] == "terminal_for_turn"
    assert "effective_payload" not in invocation
    assert "private_detail" not in invocation
    assert "x" * 10_000 not in json.dumps(invocation)


def test_mixed_effect_batch_preserves_order_with_independent_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ManualClock()
    observed: list[tuple[str, float | None]] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.05,
        effect_admission_window_sec=0.05,
    )

    def invoke(
        method_name: str,
        _arguments: dict[str, Any],
        *,
        deadline_monotonic: float | None,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        observed.append((method_name, deadline_monotonic))
        is_effect = method_name != "general_read"
        return SimpleNamespace(
            payload={
                "success": True,
                **(
                    {"effect_status": "succeeded", "changed": True}
                    if is_effect
                    else {"observed": True}
                ),
            },
            execution_id=f"execution-{len(observed)}",
            timed_out=False,
            telemetry_metadata=lambda: {
                "schema_version": "internal_mcp_transport.v1",
                "outcome": "completed",
            },
        )

    monkeypatch.setattr(gateway, "invoke", invoke)
    calls = [
        ToolCall(
            tool_name="turn_invoke_capability",
            call_id=f"mixed-{index}",
            payload={"name": name, "arguments": arguments},
        )
        for index, (name, arguments) in enumerate(
            (
                ("general_read", {"query": "before"}),
                ("create_concepts", {"concepts": [{"name": "One"}]}),
                ("general_read", {"query": "between"}),
                ("add_relationship", {"source_id": "#V#person"}),
                ("general_read", {"query": "after"}),
            )
        )
    ]
    client = _TimedSequenceClient(
        clock,
        (0.1, LLMResponse(text_response="", tool_calls=calls)),
        (0.15, LLMResponse(text_response="The ordered batch completed.")),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Apply and verify both effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="mixed-suffix-reservation",
        turn_budget_seconds=1.0,
        final_synthesis_reserve_seconds=0.8,
        final_answer_reserve_seconds=0.2,
        clock=clock,
    )

    assert result.response_text == "The ordered batch completed."
    assert [item[0] for item in observed] == [
        "general_read",
        "create_concepts",
        "general_read",
        "add_relationship",
        "general_read",
    ]
    assert [item[1] for item in observed] == [None] * 5


def test_invalid_effect_does_not_reserve_window_or_block_valid_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ManualClock()
    invoked: list[str] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.11,
        effect_admission_window_sec=0.11,
    )

    def invoke(method_name: str, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        invoked.append(method_name)
        return SimpleNamespace(
            payload={
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
            },
            execution_id=f"execution-{method_name}",
            timed_out=False,
            telemetry_metadata=lambda: {
                "schema_version": "internal_mcp_transport.v1",
                "outcome": "completed",
            },
        )

    monkeypatch.setattr(gateway, "invoke", invoke)
    client = _TimedSequenceClient(
        clock,
        (
            0.1,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="invalid-relationship",
                        payload={
                            "name": "add_relationship",
                            "arguments": {
                                "source_id": "#V#person",
                                "predicate": "#V#specific_to_user",
                                "target": "#V#other_actor",
                            },
                        },
                    ),
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="valid-create",
                        payload={
                            "name": "create_concepts",
                            "arguments": {"concepts": [{"name": "One"}]},
                        },
                    ),
                ],
            ),
        ),
        (0.15, LLMResponse(text_response="The valid effect completed.")),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Apply both effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="independent-effect-admission",
        turn_budget_seconds=1.0,
        final_synthesis_reserve_seconds=0.8,
        final_answer_reserve_seconds=0.7,
        clock=clock,
    )

    assert result.terminal_status == "effect_partially_completed"
    assert result.effect_finality_fallback is False
    assert "The valid effect completed." in result.response_text
    assert "1 succeeded and 1 failed or not started" in result.response_text
    assert any(
        call.get("type") == "adaptive_turn_mixed_effect_response_preserved"
        for call in result.aux_llm_calls
    )
    assert invoked == ["create_concepts"]
    assert len(result.tool_invocations) == 2
    assert len({item["effect_id"] for item in result.tool_invocations}) == 2
    assert result.tool_invocations[0]["status"] == "error"
    assert result.tool_invocations[0]["effect_status"] == "failed"
    assert result.tool_invocations[0]["changed"] is False
    assert "visibility_effect_not_delegated" in (
        result.tool_invocations[0]["evidence"]["preview"]
    )
    assert result.tool_invocations[1]["status"] == "ok"
    assert result.tool_invocations[1]["effect_status"] == "succeeded"


def test_indeterminate_effect_stops_later_effect_but_allows_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked: list[str] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.2,
        effect_admission_window_sec=0.05,
    )

    def invoke(method_name: str, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        invoked.append(method_name)
        if method_name == "create_concepts":
            return SimpleNamespace(
                payload={
                    "success": False,
                    "error_code": "tool_timeout_outcome_unknown",
                    "mutation_outcome": "unknown",
                    "outcome_finality": "terminal_for_turn",
                },
                execution_id="execution-indeterminate",
                timed_out=True,
                telemetry_metadata=lambda: {
                    "schema_version": "internal_mcp_transport.v1",
                    "outcome": "timed_out",
                    "timeout_phase": "handler",
                },
            )
        assert method_name == "general_read"
        return SimpleNamespace(
            payload={"success": True, "concept_id": "#V#created_if_present"},
            execution_id="execution-readback",
            timed_out=False,
            telemetry_metadata=lambda: {
                "schema_version": "internal_mcp_transport.v1",
                "outcome": "completed",
            },
        )

    monkeypatch.setattr(gateway, "invoke", invoke)
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="indeterminate-create",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Uncertain"}]},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-after-indeterminate",
                    payload={
                        "name": "general_read",
                        "arguments": {"concept_id": "#V#created_if_present"},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="blocked-later-effect",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {
                            "concept_id": "#V#person",
                            "predicate": "#V#has_note",
                            "text": "must not run",
                        },
                    },
                ),
            ],
        ),
        LLMResponse(text_response="The first effect needs canonical inspection."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create, inspect, then update.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="stop-after-indeterminate-effect",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=1,
    )

    assert invoked == ["create_concepts", "general_read"]
    assert result.terminal_status == "effect_outcome_indeterminate"
    assert result.effect_finality_fallback is True
    assert "indeterminate" in result.response_text
    assert "The first effect needs canonical inspection." not in result.response_text
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"
    assert result.tool_invocations[1]["result_target_ids"] == ["#V#created_if_present"]
    blocked = result.tool_invocations[2]
    assert blocked["status"] == "error"
    assert blocked["effect_status"] == "not_started"
    assert blocked["changed"] is False
    assert blocked["mutation_outcome"] == "not_started"
    assert blocked["error_code"] == "prior_effect_outcome_indeterminate"


def test_per_method_minimum_admits_sequential_effects_below_hard_cap() -> None:
    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        time.sleep(0.015)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="first-late-window-effect",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {"concept_id": "#V#person"},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="second-late-window-effect",
                    payload={
                        "name": "add_relationship",
                        "arguments": {"source_id": "#V#person"},
                    },
                ),
            ],
        ),
        LLMResponse(text_response="Both bounded effects completed."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            write_timeout_sec=0.2,
            effect_admission_window_sec=0.01,
        ),
        prompt="Apply both bounded effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="sequential-effect-admission",
        turn_budget_seconds=0.2,
        final_synthesis_reserve_seconds=0.12,
    )

    assert invoked == ["upsert_text_relation", "add_relationship"]
    assert [item["effect_status"] for item in result.tool_invocations] == [
        "succeeded",
        "succeeded",
    ]


def test_effect_is_not_dispatched_when_durable_intent_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import turn_execution_record_service

    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    monkeypatch.setattr(
        turn_execution_record_service,
        "record_effect_observation_phase",
        lambda **_kwargs: {
            "updated": False,
            "duplicate": False,
            "reason": "collection_unavailable",
        },
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="effect-without-durable-intent",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Must not be dispatched"}]},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The effect was not started because its durable intent could "
                "not be recorded."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-intent-persistence-failure",
    )

    assert invoked == []
    assert result.terminal_status == "effect_not_started"
    assert "1 not_started" in result.response_text
    assert "was not dispatched and reports no change" in result.response_text
    assert result.tool_invocations[0]["effect_status"] == "not_started"
    assert result.tool_invocations[0]["changed"] is False
    preview = result.tool_invocations[0]["evidence"]["preview"]
    assert "effect_observation_unavailable" in preview
    assert "not_started" in preview


@pytest.mark.parametrize(
    "relationships",
    [
        {
            "#V#specific_to_user": ["#V#other_person"],
            "#V#specific_to_organisation": ["#V#other_org"],
        },
        {},
    ],
    ids=["foreign-scoped", "global"],
)
def test_ordinary_effect_rejects_unscoped_subject_before_handler(
    monkeypatch,
    relationships: dict[str, Any],
) -> None:
    from src.backend.db import mongo_client

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": relationships}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        return {"success": True}

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="foreign-subject",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#foreign_subject",
                            "predicate": "#V#hasResearchInterest",
                            "target": "#V#topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The subject was outside delegated authority."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Update this represented concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="foreign-subject",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == []
    assert result.tool_invocations[0]["effect_status"] == "failed"
    assert "effect_subject_not_authorised" in (
        result.tool_invocations[0]["evidence"]["preview"]
    )


def test_canonical_text_denial_exposes_scoped_assertion_recovery(
    monkeypatch,
) -> None:
    from src.backend.db import mongo_client

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    invoked: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append((name, arguments))
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_recovered",
            "canonical_publication": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="canonical-text-denied",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {
                            "concept_id": "#V#globally_visible_subject",
                            "predicate": "hasNote",
                            "text": "Actor-relative observation.",
                            "language": "en-NZ",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="scoped-text-recovery",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": ("#V#globally_visible_subject"),
                            "predicate": "hasNote",
                            "target_text": "Actor-relative observation.",
                            "language": "en-NZ",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The actor-scoped assertion was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            include_scoped_assertion=True,
        ),
        prompt="Record this observation without changing shared publication.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="canonical-text-denied",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(invoked) == 1
    recovered_name, recovered_arguments = invoked[0]
    assert recovered_name == "upsert_scoped_assertion"
    assert recovered_arguments["acting_user_concept_id"] == "#V#person"
    assert recovered_arguments["organisation_concept_id"] == "#V#org"
    assert recovered_arguments["namespace"] == "#V#person@org"
    assert recovered_arguments["canonical_publication"] is False

    denial = result.tool_invocations[0]
    assert denial["effect_status"] == "failed"
    preview = denial["evidence"]["preview"]
    assert "effect_subject_not_authorised" in preview
    assert "assert_in_actor_scope" in preview
    assert "upsert_scoped_assertion" in preview
    assert "#V#globally_visible_subject" in preview
    assert "Actor-relative observation." in preview
    recovery = result.tool_invocations[1]
    assert recovery["effect_status"] == "succeeded"
    assert recovery["changed"] is True
    assert result.terminal_status == "completed"
    assert result.response_text == "The actor-scoped assertion was recorded."
    assert denial["recovery_status"] == "succeeded"
    assert denial["recovered_by_effect_id"] == recovery["effect_id"]


@pytest.mark.parametrize(
    "predicate_arguments",
    [
        {"predicate": "#V#hasResearchInterest"},
        {
            "predicate_ref": {
                "concept_id": "#V#hasResearchInterest",
                "value_kind": "concept",
            }
        },
    ],
)
def test_canonical_relationship_denial_recovers_as_scoped_assertion(
    monkeypatch: pytest.MonkeyPatch,
    predicate_arguments: dict[str, Any],
) -> None:
    from src.backend.db import mongo_client
    from src.backend.services import relationship_write_service

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "concept",
    )
    invoked: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append((name, arguments))
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_relationship_recovered",
            "canonical_publication": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="canonical-relationship-denied",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#globally_visible_subject",
                            **predicate_arguments,
                            "target": "#V#represented_topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="scoped-relationship-recovery",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": ("#V#globally_visible_subject"),
                            "predicate": "#V#hasResearchInterest",
                            "target_concept_id": "#V#represented_topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The actor-scoped relationship was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            include_scoped_assertion=True,
        ),
        prompt=(
            "Record this represented relationship without changing shared "
            "publication."
        ),
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="canonical-relationship-denied",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(invoked) == 1
    recovered_name, recovered_arguments = invoked[0]
    assert recovered_name == "upsert_scoped_assertion"
    assert recovered_arguments["subject_concept_id"] == ("#V#globally_visible_subject")
    assert recovered_arguments["predicate"] == "#V#hasResearchInterest"
    assert recovered_arguments["target_concept_id"] == "#V#represented_topic"
    assert recovered_arguments["acting_user_concept_id"] == "#V#person"
    assert recovered_arguments["organisation_concept_id"] == "#V#org"
    assert recovered_arguments["namespace"] == "#V#person@org"
    assert recovered_arguments["canonical_publication"] is False

    denial = result.tool_invocations[0]
    assert denial["effect_status"] == "failed"
    preview = denial["evidence"]["preview"]
    assert "effect_subject_not_authorised" in preview
    assert "assert_in_actor_scope" in preview
    assert "#V#globally_visible_subject" in preview
    assert "#V#hasResearchInterest" in preview
    assert "#V#represented_topic" in preview
    recovery = result.tool_invocations[1]
    assert recovery["effect_status"] == "succeeded"
    assert recovery["changed"] is True
    assert result.terminal_status == "completed"
    assert result.response_text == ("The actor-scoped relationship was recorded.")
    assert denial["recovery_status"] == "succeeded"
    assert denial["recovered_by_effect_id"] == recovery["effect_id"]


@pytest.mark.parametrize(
    "predicate_arguments",
    [
        {"predicate": "#V#has_email"},
        {
            "predicate_ref": {
                "concept_id": "#V#has_email",
                "value_kind": "text",
            }
        },
    ],
)
def test_canonical_literal_relationship_denial_recovers_in_chosen_scope(
    monkeypatch: pytest.MonkeyPatch,
    predicate_arguments: dict[str, Any],
) -> None:
    from src.backend.db import mongo_client
    from src.backend.services import relationship_write_service

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "text",
    )
    invoked: list[tuple[str, dict[str, Any]]] = []

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append((name, arguments))
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_literal_relationship_recovered",
            "canonical_publication": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="canonical-literal-relationship-denied",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#globally_visible_subject",
                            **predicate_arguments,
                            "target": "Actor-relative observation.",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="scoped-literal-relationship-recovery",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            "subject_concept_id": ("#V#globally_visible_subject"),
                            "predicate": "#V#has_email",
                            "target_text": "Actor-relative observation.",
                            "language": "en-NZ",
                            "scope_mode": "organisation",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The scoped observation was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt="Record this observation without changing shared publication.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="canonical-literal-relationship-denied",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(invoked) == 1
    recovered_name, recovered_arguments = invoked[0]
    assert recovered_name == "upsert_scoped_assertion"
    assert recovered_arguments["scope_mode"] == "organisation"
    assert recovered_arguments["target_text"] == "Actor-relative observation."
    denial, recovery = result.tool_invocations
    assert denial["effect_status"] == "failed"
    assert "target_text" in denial["evidence"]["preview"]
    assert recovery["effect_status"] == "succeeded"
    assert denial["recovery_status"] == "succeeded"
    assert denial["recovered_by_effect_id"] == recovery["effect_id"]
    assert result.terminal_status == "completed"
    assert result.response_text == "The scoped observation was recorded."


@pytest.mark.parametrize(
    "recovery_arguments",
    [
        {
            "subject_concept_id": "#V#different_subject",
            "predicate": "#V#has_email",
            "target_text": "Actor-relative observation.",
        },
        {
            "subject_concept_id": "#V#globally_visible_subject",
            "predicate": "hasDescription",
            "target_text": "Actor-relative observation.",
        },
        {
            "subject_concept_id": "#V#globally_visible_subject",
            "predicate": "#V#has_email",
            "target_text": "Different observation.",
        },
        {
            "subject_concept_id": "#V#globally_visible_subject",
            "predicate": "#V#has_email",
            "target_concept_id": "#V#actor_relative_observation",
        },
    ],
    ids=["subject", "predicate", "value", "target-kind"],
)
def test_canonical_literal_relationship_recovery_requires_same_object(
    monkeypatch: pytest.MonkeyPatch,
    recovery_arguments: dict[str, Any],
) -> None:
    from src.backend.db import mongo_client
    from src.backend.services import relationship_write_service

    class _Collection:
        def find_one(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"relationships": {}}

    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "text",
    )

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_unrelated_recovery",
            "canonical_publication": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="literal-relationship-denied",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#globally_visible_subject",
                            "predicate": "#V#has_email",
                            "target": "Actor-relative observation.",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="unrelated-scoped-assertion",
                    payload={
                        "name": "upsert_scoped_assertion",
                        "arguments": {
                            **recovery_arguments,
                            "scope_mode": "organisation",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The scoped assertion was recorded."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, include_scoped_assertion=True),
        prompt="Record this observation without changing shared publication.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="literal-relationship-denied",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    denial = result.tool_invocations[0]
    unrelated_recovery = result.tool_invocations[1]
    assert denial["effect_status"] == "failed"
    assert denial["recovery_status"] == "mismatched"
    assert denial["attempted_recovery_effect_id"] == unrelated_recovery["effect_id"]
    assert "recovered_by_effect_id" not in denial
    assert result.terminal_status == "effect_failed"
    assert result.response_text != "The scoped assertion was recorded."


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "source_id": "#V#subject",
            "predicate": "hasResearchInterest",
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate_ref": {"name": "hasResearchInterest"},
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate": "#V#hasResearchInterest",
            "predicate_ref": {"concept_id": "#V#otherPredicate"},
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate_ref": {
                "concept_id": "#V#hasResearchInterest",
                "on_missing": "create_typed_predicate",
            },
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate": "#V#hasResearchInterest",
            "predicate_if_missing": {"name": "hasResearchInterest"},
            "target": "#V#topic",
        },
        {
            "source_id": "#V#subject",
            "predicate_ref": {
                "concept_id": "#V#hasResearchInterest",
                "value_kind": "concept",
            },
            "target": "Natural-language target",
        },
        {
            "source_id": "#V#subject",
            "predicate": "#V#hasResearchInterest",
            "target": "#V#malformed target",
        },
    ],
)
def test_non_exact_relationship_denial_has_no_scoped_recovery(
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import relationship_write_service

    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: "concept",
    )
    denial = _effect_subject_authority_denial(
        capability_name="add_relationship",
        arguments=arguments,
        scoped_assertion_available=True,
    )

    assert denial["error_code"] == "effect_subject_not_authorised"
    assert "recovery_affordances" not in denial


@pytest.mark.parametrize(
    ("arguments", "canonical_kind", "expected_target_field"),
    [
        (
            {
                "source_id": "#V#subject",
                "predicate": "#V#is_an_instance_of",
                "target": "Professor",
            },
            "concept",
            None,
        ),
        (
            {
                "source_id": "#V#subject",
                "predicate_ref": {
                    "concept_id": "#V#has_email",
                    "value_kind": "concept",
                },
                "target": "#V#student@example.invalid",
            },
            "text",
            "target_text",
        ),
        (
            {
                "source_id": "#V#subject",
                "predicate": "#V#hasResearchInterest",
                "target": "#v#topic",
            },
            "concept",
            None,
        ),
    ],
    ids=["concept-literal", "text-represented-looking-literal", "lowercase-id"],
)
def test_relationship_recovery_uses_represented_predicate_kind(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    canonical_kind: str,
    expected_target_field: str | None,
) -> None:
    from src.backend.services import relationship_write_service

    monkeypatch.setattr(
        relationship_write_service,
        "resolve_existing_predicate_value_kind",
        lambda _predicate: canonical_kind,
    )

    denial = _effect_subject_authority_denial(
        capability_name="add_relationship",
        arguments=arguments,
        scoped_assertion_available=True,
    )

    affordances = denial.get("recovery_affordances") or []
    if expected_target_field is None:
        assert affordances == []
    else:
        assert len(affordances) == 1
        recovery_arguments = affordances[0]["arguments"]
        assert recovery_arguments[expected_target_field] == arguments["target"]
        assert "target_concept_id" not in recovery_arguments


def test_existing_predicate_value_kind_comes_from_canonical_typing() -> None:
    from src.backend.services.relationship_write_service import (
        resolve_existing_predicate_value_kind,
    )

    class _PredicateRepo:
        @staticmethod
        def find_one(query: dict[str, Any], *_args: Any) -> dict[str, Any] | None:
            concept_id = query.get("concept_id")
            if concept_id == "#V#has_email":
                return {
                    "concept_id": concept_id,
                    "relationships": {
                        "is_an_instance_of": ["#V#binary_text_predicate"]
                    },
                }
            if concept_id == "#V#hasResearchInterest":
                return {
                    "concept_id": concept_id,
                    "relationships": {"is_an_instance_of": ["#V#predicate"]},
                }
            return None

    assert (
        resolve_existing_predicate_value_kind("#V#has_email", _PredicateRepo) == "text"
    )
    assert (
        resolve_existing_predicate_value_kind(
            "#V#hasResearchInterest",
            _PredicateRepo,
        )
        == "concept"
    )
    assert (
        resolve_existing_predicate_value_kind("#V#hasDescription", _PredicateRepo)
        == "text"
    )
    assert (
        resolve_existing_predicate_value_kind("#V#is_an_instance_of", _PredicateRepo)
        == "concept"
    )


@pytest.mark.parametrize(
    "predicate",
    [
        "specific_to_user",
        "#V#specific_to_user",
        "specific_to_org",
        "specific_to_organisation",
        "#V#specific_to_org",
        "#V#specific_to_organisation",
    ],
)
def test_ordinary_relationship_effect_cannot_widen_visibility(
    monkeypatch,
    predicate: str,
) -> None:
    from src.backend.services import adaptive_turn_service

    invoked: list[str] = []

    def handler(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        invoked.append(name)
        return {"success": True}

    monkeypatch.setattr(
        adaptive_turn_service,
        "_effect_subject_authorised",
        lambda *_args: True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id=f"visibility-{predicate}",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": "#V#private_subject",
                            "predicate": predicate,
                            "target": "#V#other_actor",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="Visibility was not changed."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Share this represented concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"visibility-{predicate}",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert invoked == []
    assert result.tool_invocations[0]["effect_status"] == "failed"
    assert "visibility_effect_not_delegated" in (
        result.tool_invocations[0]["evidence"]["preview"]
    )


def test_capability_query_ranks_without_eliminating_the_delegated_set(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.tool_metadata_service import (
        ToolDispatchSurfaceMetadata,
    )

    catalogue = MethodCatalogue()
    for name, description, bound_arguments in (
        (
            "internal_record_search",
            "Search Von internal records.",
            {"actor_id": "actor"},
        ),
        (
            "public_web_search",
            "Search current public web sources.",
            None,
        ),
    ):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(
                    optional={
                        "query": str,
                        "actor_id": (str, type(None)),
                    },
                    allow_unknown=False,
                ),
                category="read",
                description=description,
                ordinary_turn_trusted_argument_bindings=bound_arguments,
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda name: ToolDispatchSurfaceMetadata(
            surface_family=(
                "web" if name == "public_web_search" else "represented_records"
            ),
            evidence_surface_family=(
                "web" if name == "public_web_search" else "represented_records"
            ),
            external_surface=name == "public_web_search",
        ),
    )

    unmatched_query = _capability_catalogue(
        gateway,
        ("internal_record_search", "public_web_search"),
        {"query": "unrepresented vocabulary"},
    )
    assert unmatched_query["total"] == 2
    assert unmatched_query["delegated_total"] == 2
    assert unmatched_query["matched_total"] == 0
    assert unmatched_query["catalogue_scope"] == "complete_delegated_capability_set"
    assert [item["name"] for item in unmatched_query["capabilities"]] == [
        "internal_record_search",
        "public_web_search",
    ]
    assert all(item["query_match"] is False for item in unmatched_query["capabilities"])

    web_query = _capability_catalogue(
        gateway,
        ("internal_record_search", "public_web_search"),
        {"query": "web"},
    )
    assert web_query["matched_total"] == 1
    assert [item["name"] for item in web_query["capabilities"]] == [
        "public_web_search",
        "internal_record_search",
    ]
    assert all(
        set(item) == {"name", "query_match", "selection"}
        for item in web_query["capabilities"]
    )
    exact = _capability_catalogue(
        gateway,
        ("internal_record_search", "public_web_search"),
        {"names": ["internal_record_search", "public_web_search"]},
    )
    exact_by_name = {item["name"]: item for item in exact["capabilities"]}
    public_web = exact_by_name["public_web_search"]
    internal = exact_by_name["internal_record_search"]
    assert public_web["surface_family"] == "web"
    assert public_web["external_surface"] is True
    assert internal["surface_family"] == "represented_records"
    assert internal["external_surface"] is False
    assert internal["server_bound_arguments"] == ["actor_id"]
    assert "actor_id" not in internal["input_schema"]["properties"]


def test_complete_purpose_index_keeps_zero_overlap_direct_tool_visible(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    target_name = "find_relations_with_argument"
    target_description = (
        "Return represented relations containing a supplied entity at a selected "
        "argument position, with bounded predicate, certainty, direction, preview, "
        "and pagination controls for direct evidence reads. A second sentence adds "
        "detail that the compact purpose index must not repeat."
    )
    families = (
        "knowledge_lookup",
        "mail_search",
        "project_read",
        "record_fetch",
        "task_query",
        "workflow_status",
        "web_research",
        "zotero_search",
    )
    filler_names = [
        f"{family}_{index:02d}" for family in families for index in range(12)
    ][:95]
    capability_names = (target_name, *filler_names)
    catalogue = MethodCatalogue()
    for name in capability_names:
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(
                    required={"query": str},
                    optional={f"bounded_field_{index}": str for index in range(8)},
                    allow_unknown=False,
                ),
                category="read",
                description=(
                    target_description
                    if name == target_name
                    else (
                        "Inspect a bounded source and return provenance-bearing "
                        "records for the requested research operation."
                    )
                ),
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda name, *, fallback_description=None: (
            target_description if name == target_name else fallback_description
        ),
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflows = tuple(
        WorkflowTurnCapability(
            name=f"represented_workflow_academic_profile_{index:02d}",
            workflow_id=f"#V#academic_profile_workflow_{index:02d}",
            display_name=f"Academic profile workflow {index:02d}",
            description=(
                "Compile and verify a multi-source academic mentorship dossier."
                if index == 0
                else "Compile and verify a bounded multi-source research dossier."
            ),
            relevance_score=0.97 - (index * 0.01),
            input_schema={"type": "object", "properties": {}},
            declared_step_count=5,
            semantic_effect=False,
            semantic_effect_source="declared_registered_read_components",
        )
        for index in range(10)
    )

    raw = _capability_catalogue(
        gateway,
        capability_names,
        {"query": "Which scholar mentors Michael?", "limit": 50},
        workflow_capabilities=workflows,
    )
    assert len(_json_bytes(raw)) <= 24_000
    assert len(raw["capabilities"]) == 50
    assert all(
        "description" not in item and "input_schema" not in item
        for item in raw["capabilities"]
    )

    target_detail = next(
        item for item in raw["capabilities"] if item["name"] == target_name
    )
    assert target_detail["query_match"] is False
    purpose_index = raw["purpose_index"]
    entries = purpose_index["entries"]
    assert purpose_index["complete"] is True
    assert purpose_index["ordering"] == "name_ascending_unranked"
    assert purpose_index["purpose_projection"] == "first_authored_sentence"
    assert len(entries) == len(capability_names) + len(workflows)
    assert [entry["name"] for entry in entries] == sorted(
        (*capability_names, *(workflow.name for workflow in workflows)),
        key=str.lower,
    )
    target_purpose = next(entry for entry in entries if entry["name"] == target_name)
    assert target_purpose["purpose"].endswith("...")
    assert len(target_purpose["purpose"]) <= 160
    assert "second sentence" not in target_purpose["purpose"].lower()
    assert set(target_purpose) == {"name", "purpose"}
    workflow_purpose = next(
        entry for entry in entries if entry["name"] == workflows[0].name
    )
    assert workflow_purpose["shape"] == "represented_workflow"
    assert "query_match" not in workflow_purpose
    assert "selection" not in workflow_purpose

    non_english = _capability_catalogue(
        gateway,
        capability_names,
        {"query": "Qui Michel encadre-t-il ?", "limit": 50},
        workflow_capabilities=workflows,
    )
    non_english_target = next(
        item for item in non_english["capabilities"] if item["name"] == target_name
    )
    assert non_english_target["query_match"] is False
    assert non_english["purpose_index"] == purpose_index

    bounded_results = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="realistic-purpose-index",
                tool_name="turn_capabilities",
                output=raw,
                status="ok",
            )
        ]
    )
    assert bounded_results is not None
    bounded = bounded_results[0].output
    bounded_size = len(_json_bytes(bounded))
    assert len(_json_bytes(purpose_index)) < 24_000
    assert bounded_size <= 24_000
    assert bounded["purpose_index"] == purpose_index

    exact = _capability_catalogue(
        gateway,
        capability_names,
        {"names": [target_name], "limit": 1},
        workflow_capabilities=workflows,
    )
    assert "purpose_index" not in exact
    assert exact["capabilities"][0]["name"] == target_name
    assert set(exact["capabilities"][0]["input_schema"]["properties"]) == {
        "query",
        *(f"bounded_field_{index}" for index in range(8)),
    }


def test_schema_discovery_metadata_is_retrievable_without_list_word_trigger(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="vontology_concept_search",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(
                required={"query": str},
                optional={"filter_kind": list},
                allow_unknown=False,
            ),
            category="read",
            description="Namespaced concept-search alias.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(tool_metadata_service, "_load_from_vontology", dict)
    tool_metadata_service.invalidate_cache()
    try:
        positive = _capability_catalogue(
            gateway,
            ("vontology_concept_search",),
            {"query": "schema discovery for represented relationships"},
        )
        negative = _capability_catalogue(
            gateway,
            ("vontology_concept_search",),
            {"query": "list more concise alternatives"},
        )

        capability = positive["capabilities"][0]
        assert capability["query_match"] is True
        exact = _capability_catalogue(
            gateway,
            ("vontology_concept_search",),
            {"names": ["vontology_concept_search"]},
        )
        planner_hint = exact["capabilities"][0]["planner_hint"].lower()
        assert "unknown" in planner_hint
        assert "relation-bearing read" in planner_hint
        assert "what is possible, not what is actually used" in planner_hint
        assert negative["capabilities"][0]["query_match"] is False
    finally:
        tool_metadata_service.invalidate_cache()


def test_possible_duplicate_review_intent_discovers_uncertain_assertion_lifecycle(
    monkeypatch,
) -> None:
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.services import tool_metadata_service

    catalogue = build_default_catalogue()
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(tool_metadata_service, "_load_from_vontology", dict)
    tool_metadata_service.invalidate_cache()
    try:
        delegated = ordinary_turn_capability_delegation(
            gateway,
            user_concept_id="#V#ordinary_actor",
        )
        assert "list_uncertain_relationship_assertions" in delegated
        assert "upsert_uncertain_relationship_assertion" in delegated

        discovered = _capability_catalogue(
            gateway,
            delegated,
            {"query": "mark as a possible duplicate for later review", "limit": 50},
        )
        discovered_by_name = {
            item["name"]: item for item in discovered["capabilities"]
        }
        assert discovered_by_name[
            "upsert_uncertain_relationship_assertion"
        ]["query_match"] is True
        assert discovered_by_name[
            "list_uncertain_relationship_assertions"
        ]["query_match"] is True

        purpose_by_name = {
            item["name"]: item["purpose"]
            for item in discovered["purpose_index"]["entries"]
        }
        assert "possible duplicate" in purpose_by_name[
            "upsert_uncertain_relationship_assertion"
        ]
        assert "later review" in purpose_by_name[
            "list_uncertain_relationship_assertions"
        ]

        exact = _capability_catalogue(
            gateway,
            delegated,
            {
                "names": [
                    "list_uncertain_relationship_assertions",
                    "upsert_uncertain_relationship_assertion",
                ]
            },
        )
        exact_by_name = {
            item["name"]: item for item in exact["capabilities"]
        }
        upsert = exact_by_name["upsert_uncertain_relationship_assertion"]
        listed = exact_by_name["list_uncertain_relationship_assertions"]
        assert upsert["semantic_effect"] is True
        assert upsert["plan_profile"]["shape"] == "single_capability"
        assert "reuse an existing represented predicate" in upsert[
            "planner_hint"
        ].lower()
        assert "do not mint a predicate" in upsert["planner_hint"].lower()
        assert "upsert_uncertain_relationship_assertion" in listed[
            "planner_hint"
        ]
    finally:
        tool_metadata_service.invalidate_cache()


def test_capability_metadata_failure_does_not_remove_delegated_reads(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service

    gateway = _gateway(lambda **_kwargs: {"success": True})

    def fail_metadata(*_args, **_kwargs):
        raise RuntimeError("represented metadata unavailable")

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        fail_metadata,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        fail_metadata,
    )

    result = _capability_catalogue(
        gateway,
        ("general_read",),
        {"query": "unmatched vocabulary"},
    )

    assert result["total"] == 1
    assert result["matched_total"] == 0
    assert result["capabilities"][0]["name"] == "general_read"
    assert result["purpose_index"]["entries"][0]["purpose"] == (
        "Read arbitrary general evidence."
    )
    exact = _capability_catalogue(
        gateway,
        ("general_read",),
        {"names": ["general_read"]},
    )
    assert exact["capabilities"][0]["description"] == (
        "Read arbitrary general evidence."
    )


def test_capability_frontier_promotes_direct_components_without_hiding_workflow(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    for name in (
        "fetch_concept",
        "find_relations_with_argument",
        "students_index",
    ):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(
                    optional={"concept_id": str},
                    allow_unknown=False,
                ),
                category="read",
                description=f"Use {name}.",
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=2.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_entity_lookup",
        workflow_id="#V#entity_lookup_workflow",
        display_name="Entity lookup workflow",
        description="Retrieve represented relationships for an entity.",
        relevance_score=0.93,
        input_schema={"type": "object", "properties": {}},
        component_capability_names=(
            "fetch_concept",
            "find_relations_with_argument",
        ),
        declared_component_count=2,
        declared_step_count=4,
        semantic_effect=False,
        semantic_effect_source="declared_registered_read_components",
    )

    result = _capability_catalogue(
        gateway,
        ("fetch_concept", "find_relations_with_argument", "students_index"),
        {"query": "students supervised by me", "limit": 10},
        workflow_capabilities=(workflow,),
    )

    assert result["ranking"] == "minimum_adequate_cost_sensitive_frontier"
    assert result["selection_policy"]["representedness_priority"] is False
    assert [item["name"] for item in result["capabilities"]] == [
        "fetch_concept",
        "represented_workflow_entity_lookup",
        "find_relations_with_argument",
        "students_index",
    ]
    direct_reference = result["capabilities"][0]
    exact = _capability_catalogue(
        gateway,
        ("fetch_concept", "find_relations_with_argument", "students_index"),
        {
            "names": [
                "fetch_concept",
                "represented_workflow_entity_lookup",
            ]
        },
        workflow_capabilities=(workflow,),
    )
    exact_by_name = {item["name"]: item for item in exact["capabilities"]}
    direct = exact_by_name["fetch_concept"]
    represented = exact_by_name["represented_workflow_entity_lookup"]
    assert direct["plan_profile"]["shape"] == "single_capability"
    assert represented["plan_profile"]["shape"] == "represented_workflow"
    assert represented["semantic_effect"] is False
    assert represented["effect_profile"]["operational_state_effect"] is True
    assert "declared_component_of_matched_workflow" in (
        direct_reference["selection"]["adequacy_sources"]
    )
    assert result["frontier_total"] == 4
    assert result["dominated_total"] == 0


def test_workflow_components_remain_visible_ahead_of_unrelated_tool_matches(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    component_names = (
        "fetch_concept",
        "find_relations_with_argument",
        "get_predicate_incidence",
    )
    catalogue = MethodCatalogue()
    for name in (*component_names, "workflow_list_instances"):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(optional={}, allow_unknown=True),
                category="read",
                description=f"Use {name}.",
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=2.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_entity_lookup",
        workflow_id="#V#entity_lookup_workflow",
        display_name="Entity lookup workflow",
        description="Retrieve represented relationships for an entity.",
        relevance_score=0.96,
        input_schema={"type": "object", "properties": {}},
        component_capability_names=component_names,
        declared_component_count=3,
        declared_step_count=3,
        semantic_effect=False,
        semantic_effect_source="declared_registered_read_components",
    )

    result = _capability_catalogue(
        gateway,
        (*component_names, "workflow_list_instances"),
        {"query": "who is supervised by this person", "limit": 4},
        workflow_capabilities=(workflow,),
    )

    assert [item["name"] for item in result["capabilities"]] == [
        "fetch_concept",
        "represented_workflow_entity_lookup",
        "find_relations_with_argument",
        "get_predicate_incidence",
    ]
    assert all(
        "declared_component_of_matched_workflow"
        in item["selection"]["adequacy_sources"]
        for item in (
            result["capabilities"][0],
            result["capabilities"][2],
            result["capabilities"][3],
        )
    )


def test_descriptions_put_relevant_direct_plan_in_visible_frontier(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    descriptions = {
        "generic_read": "Inspect an unrelated operational record.",
        "search_concepts": (
            "Search represented concept names, predicates, and types for "
            "relationship schema discovery."
        ),
        "find_relations_with_argument": (
            "Find represented relationships, including people supervised by "
            "a concept, where it appears as subject or target."
        ),
    }
    for name, description in descriptions.items():
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(optional={"query": str}, allow_unknown=True),
                category="read",
                description=description,
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=2.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_entity_lookup",
        workflow_id="#V#entity_lookup_workflow",
        display_name="Entity lookup workflow",
        description="Retrieve represented relationships for an entity.",
        relevance_score=0.96,
        input_schema={"type": "object", "properties": {}},
        declared_component_count=2,
        unresolved_component_count=2,
        declared_step_count=3,
        semantic_effect=None,
    )

    result = _capability_catalogue(
        gateway,
        ("generic_read", "search_concepts", "find_relations_with_argument"),
        {"query": "who is supervised by this person", "limit": 6},
        workflow_capabilities=(workflow,),
    )

    capability_names = [item["name"] for item in result["capabilities"]]
    assert capability_names[:2] == [
        "find_relations_with_argument",
        "represented_workflow_entity_lookup",
    ]
    assert set(capability_names[2:]) == {"search_concepts", "generic_read"}
    exact = _capability_catalogue(
        gateway,
        ("generic_read", "search_concepts", "find_relations_with_argument"),
        {"names": ["find_relations_with_argument"]},
        workflow_capabilities=(workflow,),
    )
    direct_candidate = exact["capabilities"][0]
    assert direct_candidate["plan_profile"]["cost_profile"]["durable_runtime"] is False
    assert result["selection_policy"]["semantic_adequacy_owner"] == "adaptive_model"


def test_capability_frontier_does_not_prefer_unrelated_direct_tool(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="unrelated_direct_read",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(optional={"record_id": str}, allow_unknown=False),
            category="read",
            description=(
                "Inspect an unrelated verified record. The shared adjective is "
                "incidental rather than routing evidence."
            ),
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_multi_step",
        workflow_id="#V#multi_step_workflow",
        display_name="Multi-step workflow",
        description="Produce a verified multi-step research work product.",
        relevance_score=0.95,
        input_schema={"type": "object", "properties": {}},
        declared_step_count=6,
        semantic_effect=False,
        semantic_effect_source="declared_registered_read_components",
    )

    result = _capability_catalogue(
        gateway,
        ("unrelated_direct_read",),
        {"query": "verified multi-step research work product", "limit": 10},
        workflow_capabilities=(workflow,),
    )

    assert [item["name"] for item in result["capabilities"]] == [
        "represented_workflow_multi_step",
        "unrelated_direct_read",
    ]
    assert result["frontier_total"] == 1


def test_capability_frontier_uses_catalogue_rarity_not_generic_routing_words(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service

    catalogue = MethodCatalogue()
    generic_names = tuple(f"list_inventory_{index}" for index in range(5))
    for name in (*generic_names, "students_relations"):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(optional={}, allow_unknown=False),
                category="read",
                description="Inspect a bounded represented record.",
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )

    result = _capability_catalogue(
        gateway,
        (*generic_names, "students_relations"),
        {"query": "list students", "limit": 10},
    )

    assert result["frontier_total"] == 1
    assert result["matched_total"] == 1
    assert result["capabilities"][0]["name"] == "students_relations"
    by_name = {item["name"]: item for item in result["capabilities"]}
    assert all(
        by_name[name]["selection"]["frontier_status"] == "outside_query_frontier"
        for name in generic_names
    )
    assert by_name[generic_names[0]]["selection"]["adequacy_sources"] == [
        "literal_query_terms"
    ]


def test_declared_read_only_direct_equivalence_prunes_only_frontier(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="direct_lookup",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(optional={"query": str}, allow_unknown=False),
            category="read",
            description="Look up the requested record.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_planner_hint",
        lambda _name: None,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_degenerate_lookup",
        workflow_id="#V#degenerate_lookup_workflow",
        display_name="Degenerate lookup workflow",
        description="Look up the requested record through a durable workflow.",
        relevance_score=0.9,
        input_schema={"type": "object", "properties": {}},
        component_capability_names=("direct_lookup",),
        declared_component_count=1,
        declared_step_count=1,
        direct_equivalent_capability_names=("direct_lookup",),
        semantic_effect=False,
        semantic_effect_source="declared_registered_read_components",
    )

    result = _capability_catalogue(
        gateway,
        ("direct_lookup",),
        {"query": "look up the requested record", "limit": 10},
        workflow_capabilities=(workflow,),
    )

    assert [item["name"] for item in result["capabilities"]] == [
        "direct_lookup",
        "represented_workflow_degenerate_lookup",
    ]
    assert result["total"] == 2
    assert result["frontier_total"] == 1
    assert result["dominated_total"] == 1
    assert result["dominated_capabilities"] == [
        {
            "name": "represented_workflow_degenerate_lookup",
            "dominated_by": "direct_lookup",
            "reason": (
                "declared_direct_equivalence_with_lower_declared_orchestration_cost"
            ),
            "equivalence_source": "represented_workflow_routing_profile",
        }
    ]
    assert result["capabilities"][1]["selection"]["frontier_status"] == (
        "declared_dominated"
    )


def test_declared_direct_equivalence_does_not_prune_effectful_workflow(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="direct_lookup",
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(optional={"query": str}, allow_unknown=False),
            category="read",
            description="Look up the requested record.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(tool_metadata_service, "get_tool_planner_hint", lambda _: None)
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _: None,
    )
    workflow = WorkflowTurnCapability(
        name="represented_workflow_effectful_lookup",
        workflow_id="#V#effectful_lookup_workflow",
        display_name="Effectful lookup workflow",
        description="Look up the record and publish a represented result.",
        relevance_score=0.9,
        input_schema={"type": "object", "properties": {}},
        component_capability_names=("direct_lookup",),
        declared_component_count=1,
        declared_step_count=2,
        direct_equivalent_capability_names=("direct_lookup",),
        semantic_effect=True,
        semantic_effect_source="represented_workflow_declaration",
    )

    result = _capability_catalogue(
        gateway,
        ("direct_lookup",),
        {"query": "look up and publish the requested record", "limit": 10},
        workflow_capabilities=(workflow,),
    )

    assert result["dominated_total"] == 0
    assert result["dominated_capabilities"] == []
    assert {item["name"] for item in result["capabilities"]} == {
        "direct_lookup",
        "represented_workflow_effectful_lookup",
    }
    purpose = next(
        item
        for item in result["purpose_index"]["entries"]
        if item["name"] == "represented_workflow_effectful_lookup"
    )
    assert purpose["semantic_effect"] is True


def test_capability_page_budget_preserves_every_alternative_and_cursor(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.tool_metadata_service import (
        ToolDispatchSurfaceMetadata,
    )

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: ToolDispatchSurfaceMetadata(
            surface_family="research",
            evidence_surface_family="research",
            external_surface=False,
        ),
    )
    catalogue = MethodCatalogue()
    capability_names = [f"research_search_{index:02d}" for index in range(20)]
    for index, name in enumerate(capability_names):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda **_kwargs: {"success": True},
                input_schema=Schema(
                    optional={
                        "actor_id": (str, type(None)),
                        **{f"field_{field}_{index}": str for field in range(30)},
                    },
                    allow_unknown=False,
                ),
                category="read",
                description=("Search research material. " + ("description " * 30)),
                ordinary_turn_trusted_argument_bindings={
                    "actor_id": "actor",
                },
            )
        )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    first_raw_page = _capability_catalogue(
        gateway,
        capability_names,
        {"query": "research", "offset": 0, "limit": 20},
    )
    assert len(_json_bytes(first_raw_page)) <= 24_000
    assert len(first_raw_page["capabilities"]) == 20

    paired = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id=f"catalogue-{index}",
                tool_name="turn_capabilities",
                output=first_raw_page,
                status="ok",
            )
            for index in range(2)
        ]
    )
    assert paired is not None
    assert all(item.output.get("capabilities") for item in paired)
    assert all(item.output["next_offset"] is None for item in paired)
    assert all(len(item.output["capabilities"]) == 20 for item in paired)
    assert (
        len(
            _json_bytes(
                [
                    {
                        "call_id": item.call_id,
                        "tool_name": item.tool_name,
                        "status": item.status,
                        "output": item.output,
                    }
                    for item in paired
                ]
            )
        )
        <= 24_000
    )

    seen_names: list[str] = []
    seen_entries: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        raw_page = _capability_catalogue(
            gateway,
            capability_names,
            {"query": "research", "offset": offset, "limit": 20},
        )
        bounded_results = _bound_tool_results_for_model(
            [
                ToolResult(
                    call_id=f"catalogue-page-{offset}",
                    tool_name="turn_capabilities",
                    output=raw_page,
                    status="ok",
                )
            ]
        )
        assert bounded_results is not None
        bounded = bounded_results[0].output
        assert len(_json_bytes(bounded)) <= 24_000
        entries = bounded["capabilities"]
        assert entries
        assert [item["name"] for item in entries] == capability_names[
            offset : offset + len(entries)
        ]
        seen_names.extend(item["name"] for item in entries)
        seen_entries.extend(entries)
        next_offset = bounded["next_offset"]
        if next_offset is not None:
            assert next_offset == offset + len(entries)
        offset = next_offset

    assert seen_names == capability_names
    reference = seen_entries[0]
    assert reference["query_match"] is True
    assert reference["selection"]["frontier_status"] == "candidate"
    assert "description" not in reference
    assert "input_schema" not in reference
    purpose_entry = next(
        item
        for item in bounded["purpose_index"]["entries"]
        if item["name"] == reference["name"]
    )
    assert purpose_entry["purpose"].startswith("Search research material.")
    assert purpose_entry["purpose"].endswith("...")
    assert len(purpose_entry["purpose"]) <= 160
    assert bounded["purpose_index"]["exact_schema_hydration"] == {
        "tool": "turn_capabilities",
        "guidance": "Request all alternatives being compared in one names array.",
        "arguments": {"names": ["<capability names>"], "limit": 50},
    }

    exact_page = _capability_catalogue(
        gateway,
        capability_names,
        {"names": [reference["name"]], "limit": 1},
    )
    exact_bounded = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="exact-schema",
                tool_name="turn_capabilities",
                output=exact_page,
                status="ok",
            )
        ]
    )
    assert exact_bounded is not None
    assert "input_schema" in exact_bounded[0].output["capabilities"][0]


def test_bounded_catalogue_compacts_purposes_without_erasing_candidates() -> None:
    from src.backend.services.adaptive_turn_service import (
        _bounded_capability_catalogue_output,
        _json_bytes,
    )

    entries = [
        {
            "name": f"general_capability_{index:03d}",
            "purpose": (
                "Perform an ordinary bounded task using represented evidence and "
                "return the useful work product while preserving provenance, "
                "recoverability, and a concise account of the observed result."
            ),
        }
        for index in range(200)
    ]
    raw = {
        "schema_version": "adaptive_turn_capability_catalogue.v1",
        "success": True,
        "delegation": "bounded_capabilities",
        "total": len(entries),
        "delegated_total": len(entries),
        "catalogue_scope": "query_frontier",
        "offset": 0,
        "next_offset": None,
        "purpose_index": {
            "schema_version": "adaptive_turn_capability_purpose_index.v1",
            "complete": True,
            "ordering": "name_ascending_unranked",
            "purpose_projection": "first_authored_sentence",
            "purpose_max_chars": 160,
            "exact_schema_hydration": {
                "tool": "turn_capabilities",
                "guidance": (
                    "Request all alternatives being compared in one names array."
                ),
                "arguments": {"names": ["<capability names>"], "limit": 50},
            },
            "entries": entries,
        },
        "capabilities": [
            {
                "name": entry["name"],
                "query_match": True,
                "selection": {"frontier_status": "candidate"},
            }
            for entry in entries[:50]
        ],
    }

    bounded = _bounded_capability_catalogue_output(raw, max_bytes=24_000)

    assert bounded
    assert len(_json_bytes(bounded)) <= 24_000
    purpose_index = bounded["purpose_index"]
    assert purpose_index["complete"] is True
    assert len(purpose_index["entries"]) == len(entries)
    assert purpose_index["purpose_text_compacted_for_model_context"] is True
    assert purpose_index["purpose_max_chars"] < 160
    assert [item["name"] for item in purpose_index["entries"]] == [
        item["name"] for item in entries
    ]


def test_single_oversized_capability_remains_visible_as_schema_reference(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.tool_metadata_service import (
        ToolDispatchSurfaceMetadata,
    )

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: ToolDispatchSurfaceMetadata(
            surface_family="knowledge_base",
            evidence_surface_family="knowledge_base",
            external_surface=False,
        ),
    )
    name = "oversized_general_read"
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name=name,
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(
                optional={
                    "actor_id": (str, type(None)),
                    **{f"field_{index}": str for index in range(2_000)},
                },
                allow_unknown=False,
            ),
            category="read",
            description="Read a broad represented record.",
            ordinary_turn_trusted_argument_bindings={"actor_id": "actor"},
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    raw = _capability_catalogue(
        gateway,
        (name,),
        {"names": [name], "limit": 1},
    )
    assert len(_json_bytes(raw)) > 24_000
    bounded_results = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="oversized-schema",
                tool_name="turn_capabilities",
                output=raw,
                status="ok",
            )
        ]
    )

    assert bounded_results is not None
    bounded = bounded_results[0].output
    assert len(_json_bytes(bounded)) <= 24_000
    assert bounded["next_offset"] is None
    assert bounded["capability_page_projection"] == {
        "schema_version": "adaptive_turn_capability_page_projection.v1",
        "returned": 1,
        "full_schema_count": 0,
        "schema_reference_count": 1,
        "omitted_page_entry_count": 0,
        "reason": "model_context_budget",
    }
    assert len(bounded["capabilities"]) == 1
    compact = bounded["capabilities"][0]
    assert compact["name"] == name
    assert compact["description"] == "Read a broad represented record."
    assert compact["surface_family"] == "knowledge_base"
    assert compact["evidence_surface_family"] == "knowledge_base"
    assert compact["external_surface"] is False
    assert compact["server_bound_arguments"] == ["actor_id"]
    assert "input_schema" not in compact
    assert compact["input_schema_omitted_for_model_context"] is True
    assert "input_schema_hydration" not in compact
    assert compact["input_schema_unavailable_reason"] == (
        "schema_exceeds_model_context_budget"
    )
    assert compact["direct_invocation"] == {
        "tool": "turn_invoke_capability",
        "available_if_arguments_known": True,
    }


def test_bulky_capability_metadata_falls_back_without_hiding_the_schema(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service
    from src.backend.services.tool_metadata_service import (
        ToolDispatchSurfaceMetadata,
    )

    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_description",
        lambda _name, *, fallback_description=None: fallback_description,
    )
    monkeypatch.setattr(
        tool_metadata_service,
        "get_tool_dispatch_surface_metadata",
        lambda _name: ToolDispatchSurfaceMetadata(
            surface_family="knowledge_base",
            evidence_surface_family="knowledge_base",
            external_surface=False,
        ),
    )
    name = "metadata_heavy_read"
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name=name,
            handler=lambda **_kwargs: {"success": True},
            input_schema=Schema(
                optional={
                    "actor_id": (str, type(None)),
                    "query": str,
                },
                allow_unknown=False,
            ),
            category="read",
            description="Read represented records. " + ("metadata " * 4_000),
            ordinary_turn_trusted_argument_bindings={"actor_id": "actor"},
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )

    raw = _capability_catalogue(
        gateway,
        (name,),
        {"limit": 20},
    )
    bounded_results = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="bulky-metadata",
                tool_name="turn_capabilities",
                output=raw,
                status="ok",
            )
        ]
    )

    assert bounded_results is not None
    bounded = bounded_results[0].output
    assert len(_json_bytes(bounded)) <= 24_000
    assert bounded["next_offset"] is None
    assert "capability_page_projection" not in bounded
    assert len(bounded["capabilities"]) == 1
    compact = bounded["capabilities"][0]
    assert compact["name"] == name
    assert compact["selection"]["frontier_status"] == "candidate"
    assert "description" not in compact
    assert "input_schema" not in compact
    purpose = bounded["purpose_index"]["entries"][0]
    assert purpose["name"] == name
    assert purpose["purpose"].startswith("Read represented records.")
    assert purpose["purpose"].endswith("...")
    assert len(purpose["purpose"]) <= 160

    exact = _capability_catalogue(
        gateway,
        (name,),
        {"names": [name], "limit": 1},
    )
    exact_results = _bound_tool_results_for_model(
        [
            ToolResult(
                call_id="bulky-metadata-exact",
                tool_name="turn_capabilities",
                output=exact,
                status="ok",
            )
        ]
    )
    assert exact_results is not None
    exact_capability = exact_results[0].output["capabilities"][0]
    assert exact_capability["name"] == name
    assert exact_capability["capability_metadata_omitted_for_model_context"] is True
    assert exact_capability["server_bound_arguments"] == ["actor_id"]
    assert set(exact_capability["input_schema"]["properties"]) == {"query"}
    assert "input_schema_hydration" not in exact_capability


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

    delegated = ordinary_turn_capability_delegation(
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


def test_compact_evidence_envelope_keeps_source_diagnostics_before_preview() -> None:
    source_diagnostics = {
        "coverage_complete": False,
        "counts_are_lower_bounds": True,
        "has_more": True,
        "next_offset": 20,
        "offset": 0,
        "limit": 20,
        "total": 200,
        "total_hits_is_lower_bound": True,
    }
    compact = _compact_evidence_envelope(
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": "ev-source-diagnostics",
            "source_diagnostics": source_diagnostics,
            "tool_name": "bounded_canonical_read",
            "preview": "x" * 100_000,
            "unrelated_large_source_field": "y" * 100_000,
        },
        max_bytes=400,
    )

    assert len(_json_bytes(compact)) <= 400
    assert compact["evidence_id"] == "ev-source-diagnostics"
    assert compact["source_diagnostics"] == source_diagnostics
    assert "unrelated_large_source_field" not in compact
    assert len(compact.get("preview", "")) < 100_000


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


def test_fresh_evidence_projection_prioritises_hydrated_slices_mechanically() -> None:
    evidence_index = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": f"ev-{index}",
            "tool_name": "general_read",
            "call_id": f"call-{index}",
            "status": "ok",
            "sha256": f"{index:064x}",
            "provenance": {"source": f"source-{index}"},
            "preview": f"preview-{index}-" + ("p" * 1_100),
            "preview_truncated": True,
        }
        for index in range(16)
    ]
    envelope_views = [dict(item) for item in evidence_index]
    hydrated_slices = [
        {
            "schema_version": "turn_evidence_slice.v1",
            "success": True,
            "evidence_id": f"ev-{12 + index}",
            "tool_name": "general_read",
            "call_id": f"call-{12 + index}",
            "trust_boundary": "untrusted_tool_output",
            "source_sha256": f"{12 + index:064x}",
            "provenance": {"source": f"source-{12 + index}"},
            "selector": {
                "json_pointer": f"/records/{index}/summary",
                "offset": 0,
                "max_chars": 5_200,
            },
            "content": f"hydrated-{index}-" + ("h" * 5_000),
            "content_format": "text",
            "returned_chars": 5_011,
            "has_more": False,
        }
        for index in range(4)
    ]

    context = _final_synthesis_context(
        [],
        evidence_index,
        [
            *envelope_views,
            hydrated_slices[0],
            hydrated_slices[1],
            dict(hydrated_slices[0]),
            hydrated_slices[2],
            hydrated_slices[3],
        ],
    )

    assert len(context) == 1
    assert len(_json_bytes(context[0])) <= 24_000
    payload = json.loads(context[0]["content"])
    included_views = payload["evidence_views"]
    included_slices = [
        item
        for item in included_views
        if item["schema_version"] == "turn_evidence_slice.v1"
    ]
    assert included_slices == hydrated_slices
    first_envelope = next(
        (
            index
            for index, item in enumerate(included_views)
            if item["schema_version"] == "turn_evidence_envelope.v1"
        ),
        len(included_views),
    )
    assert all(
        item["schema_version"] == "turn_evidence_slice.v1"
        for item in included_views[:first_envelope]
    )
    assert included_slices[0]["selector"]["json_pointer"] == ("/records/0/summary")
    assert included_slices[0]["source_sha256"] == f"{12:064x}"
    assert included_slices[0]["provenance"] == {"source": "source-12"}

    projection = payload["evidence_view_projection"]
    assert projection["order"] == (
        "content_bearing_evidence_slices_first_stable_within_class"
    )
    assert projection["total_count"] == len(envelope_views) + len(hydrated_slices)
    assert projection["included_count"] == len(included_views)
    assert projection["omitted_count"] > 0
    assert projection["reason"] == "model_context_budget"
    assert "next_offset" not in projection
    assert payload["evidence"]
    assert projection["read_tool"] == "turn_read_evidence"
    assert projection["list_tool"] == "turn_list_evidence"


def test_oversized_early_slice_does_not_suppress_a_later_fitting_slice() -> None:
    evidence_index = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": f"ev-{index}",
            "tool_name": "general_read",
            "call_id": f"call-{index}",
            "status": "ok",
        }
        for index in range(2)
    ]
    oversized_slice = {
        "schema_version": "turn_evidence_slice.v1",
        "success": True,
        "evidence_id": "ev-0",
        "source_sha256": "a" * 64,
        "selector": {"json_pointer": "/large"},
        "content": "x" * 23_500,
    }
    later_slice = {
        "schema_version": "turn_evidence_slice.v1",
        "success": True,
        "evidence_id": "ev-1",
        "source_sha256": "b" * 64,
        "selector": {"json_pointer": "/corrective"},
        "content": "A later corrective slice.",
    }

    context = _final_synthesis_context(
        [],
        evidence_index,
        [oversized_slice, later_slice],
    )

    payload = json.loads(context[0]["content"])
    assert len(_json_bytes(context[0])) <= 24_000
    assert payload["evidence_views"] == [later_slice]
    projection = payload["evidence_view_projection"]
    assert projection["total_count"] == 2
    assert projection["included_count"] == 1
    assert projection["omitted_count"] == 1
    assert projection["reason"] == "model_context_budget"


def test_tool_result_correlation_shell_overflow_has_no_oversized_fallback() -> None:
    results = [
        ToolResult(
            call_id=f"call-{index}",
            tool_name="turn_invoke_capability",
            status="ok",
            output={"evidence_id": f"ev-{index}", "preview": "x" * 1_000},
        )
        for index in range(500)
    ]

    assert _bound_tool_results_for_model(results) is None


def test_tool_result_batch_preserves_full_outputs_when_aggregate_fits() -> None:
    results = [
        ToolResult(
            call_id=f"capability-{index}",
            tool_name="turn_capabilities",
            status="ok",
            output={
                "name": f"capability-{index}",
                "description": character * size,
                "planner_hint": f"planner-{index}",
            },
        )
        for index, (character, size) in enumerate(
            (("a", 6_700), ("b", 5_100), ("c", 5_400), ("d", 3_300))
        )
    ]
    complete_batch = [
        {
            "call_id": result.call_id,
            "tool_name": result.tool_name,
            "status": result.status,
            "output": result.output,
        }
        for result in results
    ]
    assert len(_json_bytes(complete_batch)) < 24_000

    bounded = _bound_tool_results_for_model(results)

    assert bounded is not None
    assert [result.output for result in bounded] == [
        result.output for result in results
    ]
    assert bounded[0].output["planner_hint"] == "planner-0"


def test_bounded_effect_result_preserves_exact_partial_receipt() -> None:
    result = ToolResult(
        call_id="partial-effect",
        tool_name="turn_invoke_capability",
        status="error",
        output={
            "evidence_id": "ev-partial",
            "status": "partial",
            "effect_status": "partial",
            "changed": True,
            "error_code": "derived_identity_write_failed",
            "mutation_outcome": "partial",
            "outcome_finality": "terminal_for_turn",
            "preview": "x" * 20_000,
        },
    )

    bounded = _bound_tool_results_for_model([result], max_bytes=450)

    assert bounded is not None
    assert bounded[0].status == "error"
    assert bounded[0].output["evidence_id"] == "ev-partial"
    assert bounded[0].output["effect_status"] == "partial"
    assert bounded[0].output["changed"] is True
    assert bounded[0].output["error_code"] == "derived_identity_write_failed"
    assert bounded[0].output["mutation_outcome"] == "partial"
    assert bounded[0].output["outcome_finality"] == "terminal_for_turn"


def test_model_can_invoke_any_delegated_read_without_a_prompt_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_tail = "z" * 50_000
    seen_actor: dict[str, Any] = {}

    monkeypatch.setattr(
        "src.backend.services.tool_evidence_projection_service."
        "project_tool_payload_for_llm",
        lambda tool_name, payload: (
            {
                "_llm_view": "test_projection.v1",
                "relationships": {
                    "has_file": ["#V#existing_file_copy"],
                },
            }
            if tool_name == "general_read" and payload.get("answer") == "found"
            else None
        ),
    )

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
                    tool_name="turn_invoke_capability",
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
    model_evidence_text = client.calls[1]["context"][-1]["content"]
    model_evidence = json.loads(model_evidence_text)
    assert model_evidence["projected_payload"]["relationships"]["has_file"] == [
        "#V#existing_file_copy"
    ]
    assert model_evidence["preview_truncated"] is True
    assert model_evidence["evidence_id"] == envelope["evidence_id"]
    assert "json_pointer" in model_evidence["available_selectors"]
    assert "z" * 10_000 not in model_evidence_text
    assert len(model_evidence_text) < 10_000
    assert "projected_payload" not in envelope
    assert "effective_payload" not in result.tool_invocations[0]
    assert raw_tail not in json.dumps(result.tool_invocations)
    assert all(
        invocation.get("tool") != "general_write"
        for invocation in result.tool_invocations
    )


def test_effect_batch_preserves_order_and_read_can_observe_prior_effect(
    monkeypatch,
) -> None:
    from src.backend.services import adaptive_turn_service

    seen: list[tuple[str, dict[str, Any]]] = []
    state = {"created": False}

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append((name, dict(arguments)))
        if name == "create_concepts":
            state["created"] = True
            return {"success": True, "effect_status": "succeeded", "changed": True}
        if name == "general_read":
            return {"success": True, "created": state["created"]}
        return {"success": True, "effect_status": "succeeded", "changed": True}

    monkeypatch.setattr(
        adaptive_turn_service,
        "_effect_subject_authorised",
        lambda *_args: True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="ordered-create",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Ordered"}]},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="ordered-read",
                    payload={
                        "name": "general_read",
                        "arguments": {"concept_id": "#V#ordered"},
                    },
                ),
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="ordered-text",
                    payload={
                        "name": "upsert_text_relation",
                        "arguments": {
                            "concept_id": "#V#ordered",
                            "predicate": "hasDescription",
                            "text": "Observed after creation.",
                            "provenance": {"source": "model-spoof"},
                        },
                    },
                ),
            ],
        ),
        LLMResponse(text_response="Canonical state was available to inspect."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler),
        prompt="Represent and inspect this.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="ordered-effects",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert [name for name, _arguments in seen] == [
        "create_concepts",
        "general_read",
        "upsert_text_relation",
    ]
    assert seen[2][1]["provenance"] is None
    assert result.tool_invocations[1]["evidence"]["preview"].find('"created":true') >= 0


def test_relation_progress_emits_one_human_start_and_terminal_summary(
    monkeypatch,
) -> None:
    from src.backend.services import adaptive_turn_service

    progress_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        adaptive_turn_service,
        "_effect_subject_authorised",
        lambda *_args, **_kwargs: True,
    )

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="robert-amor-supervision",
                    payload={
                        "name": "add_relationship",
                        "arguments": {
                            "source_id": (
                                "#V#nathan_young_doctoral_candidature_situation"
                            ),
                            "predicate": "#V#has_doctoral_supervisor",
                            "target": "#V#robert_amor",
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="That supervision relationship was already known."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            lambda _name, _arguments: {
                "success": True,
                "effect_status": "succeeded",
                "changed": False,
            }
        ),
        prompt="Re-assert the existing Robert Amor supervision relationship.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="semantic-relation-progress",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event)),
            check_cancellation=lambda: None,
        ),
    )

    relation_events = [
        event
        for event in progress_events
        if event.get("call_id") == "robert-amor-supervision"
    ]
    assert [event["event_kind"] for event in relation_events] == [
        "tool_call_start",
        "tool_call_end",
    ]
    assert [event["subtask"] for event in relation_events] == [
        "Add Relationship",
        "Add Relationship",
    ]
    assert relation_events[0]["result_summary"] == (
        "Add Relationship: Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor (in progress)."
    )
    assert relation_events[1]["success"] is True
    assert relation_events[1]["result_summary"] == (
        "Add Relationship reported that no change was needed: "
        "Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor."
    )
    assert relation_events[0]["semantic_operation"]["lifecycle_status"] == "running"
    assert relation_events[0]["semantic_operation"]["verification"] == {
        "status": "unknown",
        "canonical_read_back_present": False,
        "source": "none",
    }
    assert relation_events[1]["semantic_operation"]["outcome"]["changed"] is False
    assert relation_events[1]["semantic_operation"]["verification"] == {
        "status": "receipt_only",
        "canonical_read_back_present": False,
        "source": "effect_receipt",
    }
    assert result.tool_invocations[0]["changed"] is False


def test_concept_search_progress_emits_query_and_bounded_results() -> None:
    progress_events: list[dict[str, Any]] = []
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="search_concepts",
            handler=lambda **_kwargs: {
                "results": [
                    {
                        "concept_id": "#V#university_of_auckland",
                        "name": "University of Auckland",
                    },
                    {"concept_id": "#V#university", "name": "University"},
                ],
                "total_count": 4,
                "match_types_used": ["substring"],
                "query_info": {"query": "University of Auckland"},
            },
            input_schema=Schema(
                required={"query": str},
                allow_unknown=False,
                description="Search represented concepts.",
            ),
            category="read",
            ordinary_turn_public=True,
            description="Search represented concepts.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=1.0),
        enabled=True,
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="search-auckland",
                    payload={
                        "name": "search_concepts",
                        "arguments": {"query": "University of Auckland"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="I found the represented university concepts."),
    )

    execute_adaptive_turn(
        gateway=gateway,
        prompt="Find the represented University of Auckland concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="semantic-search-progress",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event)),
            check_cancellation=lambda: None,
        ),
    )

    search_events = [
        event for event in progress_events if event.get("call_id") == "search-auckland"
    ]
    assert [event["event_kind"] for event in search_events] == [
        "tool_call_start",
        "tool_call_end",
    ]
    assert [event["subtask"] for event in search_events] == [
        "Search Concepts",
        "Search Concepts",
    ]
    assert search_events[0]["result_summary"] == (
        "Search Concepts: Query: “University of Auckland” (in progress)."
    )
    assert search_events[1]["result_summary"] == (
        "Search Concepts returned 4 concept matches: University of Auckland, "
        "University and 2 others for Query: “University of Auckland”."
    )
    assert search_events[1]["semantic_operation"]["observation"]["items"][0] == {
        "name": "University of Auckland",
        "identifier": "#V#university_of_auckland",
    }


def test_effect_evidence_preserves_success_partial_failure_and_unknown_timeout() -> (
    None
):
    seen_cases: list[str] = []
    progress_events: list[dict[str, Any]] = []
    release_timeout_handler = Event()

    def handler(_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        case = str(arguments["case"])
        seen_cases.append(case)
        if case == "partial":
            return {
                "success": False,
                "effect_status": "partial",
                "changed": True,
                "partial_failures": [{"stage": "derived_inverse", "error": "late"}],
            }
        if case == "failed":
            return {
                "success": False,
                "effect_status": "failed",
                "changed": False,
                "error_code": "rejected",
            }
        if case == "timeout":
            while not internal_mcp_cancellation_requested():
                time.sleep(0.001)
            assert release_timeout_handler.wait(timeout=1.0)
        return {"success": True, "effect_status": "succeeded", "changed": True}

    calls = [
        ToolCall(
            tool_name="turn_invoke_capability",
            call_id=f"effect-{case}",
            payload={
                "name": "create_concepts",
                "arguments": {"case": case},
            },
        )
        for case in ("succeeded", "partial", "failed", "timeout")
    ]
    client = _SequenceClient(
        LLMResponse(text_response="", tool_calls=calls),
        LLMResponse(text_response="The bounded effects were reported truthfully."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            handler,
            write_timeout_sec=0.02,
            hard_timeout_enabled=True,
        ),
        prompt="Exercise bounded effects.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-statuses",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event)),
            check_cancellation=lambda: None,
        ),
    )
    release_timeout_handler.set()

    assert seen_cases == ["succeeded", "partial", "failed", "timeout"]
    assert result.terminal_status == "effect_outcome_indeterminate"
    assert result.effect_finality_fallback is True
    assert "The bounded effects were reported truthfully." not in result.response_text
    assert [item["effect_status"] for item in result.tool_invocations] == [
        "succeeded",
        "partial",
        "failed",
        "indeterminate",
    ]
    assert [item["status"] for item in result.tool_invocations] == [
        "ok",
        "error",
        "error",
        "error",
    ]
    assert len({item["effect_id"] for item in result.tool_invocations}) == 4
    assert result.tool_invocations[0]["changed"] is True
    assert result.tool_invocations[1]["changed"] is True
    assert "partial_failures" in result.tool_invocations[1]["evidence"]["preview"]
    assert result.tool_invocations[2]["changed"] is False
    assert result.tool_invocations[3]["changed"] is None
    assert all(
        item.get("evidence", {}).get("evidence_id") for item in result.tool_invocations
    )
    partial_progress = next(
        event
        for event in progress_events
        if event.get("status") == "tool_completed"
        and event.get("call_id") == "effect-partial"
    )
    assert partial_progress["success"] is False
    assert not partial_progress["result_summary"].startswith("Finished ")


def test_partial_effect_downgrades_nominal_model_completion() -> None:
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="partial-effect",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Only partly created"}]},
                    },
                )
            ],
        ),
        LLMResponse(text_response="Everything was created."),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(
            lambda _name, _arguments: {
                "success": False,
                "effect_status": "partial",
                "changed": True,
                "error_code": "derived_relation_failed",
            }
        ),
        prompt="Create the represented concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="partial-effect-terminal-status",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "effect_partially_completed"
    assert result.effect_finality_fallback is True
    assert "1 partial" in result.response_text
    assert "Everything was created." not in result.response_text


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
                    tool_name="turn_invoke_capability",
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


def test_context_limit_recovery_keeps_exact_hydrated_evidence_view() -> None:
    evidence_index = [
        {
            "schema_version": "turn_evidence_envelope.v1",
            "evidence_id": "ev-hydrated",
            "tool_name": "general_read",
            "call_id": "call-hydrated",
            "status": "ok",
            "sha256": "a" * 64,
            "provenance": {"source": "represented-document"},
            "preview": "preview-only",
        }
    ]
    hydrated_slice = {
        "schema_version": "turn_evidence_slice.v1",
        "success": True,
        "evidence_id": "ev-hydrated",
        "tool_name": "general_read",
        "call_id": "call-hydrated",
        "trust_boundary": "untrusted_tool_output",
        "source_sha256": "a" * 64,
        "source_size_bytes": 50_000,
        "provenance": {"source": "represented-document"},
        "selector": {
            "json_pointer": "/project/summary",
            "offset": 0,
            "max_chars": 4_000,
        },
        "content": "The explicitly selected project summary.",
        "content_format": "text",
        "returned_chars": 40,
        "has_more": False,
    }

    compact = _compact_context_after_limit(
        prompt="Answer from the selected evidence.",
        context=[
            {
                "role": "tool",
                "content": "an obsolete provider-specific tool transcript",
            }
        ],
        evidence_index=evidence_index,
        evidence_views=[hydrated_slice],
        before_size=60_000,
    )

    assert compact is not None
    assert len(compact) == 1
    assert compact[0]["role"] == "user"
    assert len(_json_bytes(compact[0])) <= 24_000
    payload = json.loads(compact[0]["content"])
    assert payload["schema_version"] == "adaptive_turn_context_recovery.v1"
    assert payload["evidence_views"] == [hydrated_slice]
    assert payload["evidence"][0]["evidence_id"] == "ev-hydrated"
    assert payload["evidence_views"][0]["source_sha256"] == "a" * 64
    assert payload["evidence_views"][0]["selector"] == {
        "json_pointer": "/project/summary",
        "offset": 0,
        "max_chars": 4_000,
    }
    assert payload["evidence_views"][0]["provenance"] == {
        "source": "represented-document"
    }


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


def _wrapped_model_timeout(message: str = "OpenAI call failed") -> ToolCallError:
    try:
        raise TimeoutError("provider request deadline exhausted")
    except TimeoutError as cause:
        try:
            raise ToolCallError(message) from cause
        except ToolCallError as wrapped:
            return wrapped


def test_model_timeout_retries_once_with_smaller_faithful_context() -> None:
    client = _SequenceClient(
        _wrapped_model_timeout(),
        LLMResponse(text_response="Recovered after the request timeout."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(lambda **_kwargs: {"success": True}),
        prompt="Keep the actual task.",
        context=[
            {"role": "system", "content": "trusted instruction"},
            {"role": "assistant", "content": "old context " * 10_000},
        ],
        llm_client=client,
        model="test-model",
        turn_id="turn-model-timeout-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Recovered after the request timeout."
    assert len(client.calls) == 2
    assert len(_json_bytes(client.calls[1]["context"])) < len(
        _json_bytes(client.calls[0]["context"])
    )
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_liveness_recovery"
    )
    assert recovery["changed"] is True
    assert recovery["reason"] == "fresh_smaller_faithful_context"
    assert recovery["after_bytes"] < recovery["before_bytes"]
    assert result.llm_calls[0]["status"] == "failed"


def test_model_timeout_recovery_retains_existing_tool_evidence() -> None:
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-before-timeout",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "canonical record"},
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="response-before-timeout",
            ),
        ),
        _wrapped_model_timeout(),
        LLMResponse(text_response="Recovered from the retained evidence."),
    )

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "record": {
                    "summary": "The exact canonical record.",
                    "raw": "z" * 50_000,
                },
            }
        ),
        prompt="Read the canonical record and answer.",
        context=[{"role": "assistant", "content": "old context " * 10_000}],
        llm_client=client,
        model="test-model",
        turn_id="turn-model-timeout-evidence-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Recovered from the retained evidence."
    assert len(client.calls) == 3
    assert client.calls[1]["continuation"].response_id == "response-before-timeout"
    assert "continuation" not in client.calls[-1]
    assert "tool_results" not in client.calls[-1]
    recovery_context = client.calls[-1]["context"]
    evidence_messages = [
        item
        for item in recovery_context
        if item.get("role") == "user"
        and isinstance(item.get("content"), str)
        and item["content"].startswith("{")
    ]
    assert len(evidence_messages) == 1
    evidence_payload = json.loads(evidence_messages[0]["content"])
    assert evidence_payload["schema_version"] == "adaptive_turn_context_recovery.v1"
    assert evidence_payload["evidence"][0]["call_id"] == "read-before-timeout"
    assert "z" * 5_000 not in evidence_messages[0]["content"]


def test_model_timeout_does_not_retry_without_changed_context() -> None:
    client = _SequenceClient(
        ToolCallError("OpenAI call failed: Request timed out."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="A short request.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-model-timeout-unchanged",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 1
    assert result.terminal_status == "model_error"
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_liveness_recovery"
    )
    assert recovery["changed"] is False
    assert recovery["reason"] == "no_smaller_faithful_context"


def test_model_timeout_allows_only_one_changed_context_retry() -> None:
    client = _SequenceClient(
        _wrapped_model_timeout(),
        _wrapped_model_timeout(),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Keep the task while shedding old context.",
        context=[{"role": "assistant", "content": "old context " * 10_000}],
        llm_client=client,
        model="test-model",
        turn_id="turn-single-model-timeout-recovery",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 2
    assert result.terminal_status == "model_error"
    recoveries = [
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_model_liveness_recovery"
    ]
    assert [item["changed"] for item in recoveries] == [True, False]
    assert recoveries[-1]["reason"] == "single_fresh_context_retry_already_used"


@pytest.mark.parametrize(
    "error",
    [
        ToolCallError("OpenAI call failed: authentication rejected."),
        ValueError("request_timeout_seconds must be positive"),
        StructuredToolProtocolError(
            "Provider call lineage was unavailable after a deadline."
        ),
        ToolCallError("OpenAI call failed: provider returned 500."),
    ],
    ids=["authentication", "configuration", "protocol", "generic-provider"],
)
def test_non_liveness_model_failure_is_not_retried_after_compaction(
    error: Exception,
) -> None:
    client = _SequenceClient(
        error,
        LLMResponse(text_response="This response must not be requested."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Do not retry an authentication failure.",
        context=[{"role": "assistant", "content": "old context " * 10_000}],
        llm_client=client,
        model="test-model",
        turn_id="turn-non-liveness-model-failure",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
    )

    assert len(client.calls) == 1
    assert result.terminal_status == "model_error"
    assert not any(
        item.get("type") == "adaptive_turn_model_liveness_recovery"
        for item in result.aux_llm_calls
    )


def test_elapsed_thresholds_are_one_time_model_advisories_without_removing_tools() -> (
    None
):
    clock = _ManualClock()
    progress_events: list[dict[str, Any]] = []
    all_tool_names = {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    client = _TimedSequenceClient(
        clock,
        (
            6.1,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="after-research-advisory",
                        payload={},
                    )
                ],
            ),
        ),
        (
            8.1,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="after-answer-advisory",
                        payload={},
                    )
                ],
            ),
        ),
        (
            10.1,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="after-turn-advisory",
                        payload={},
                    )
                ],
            ),
        ),
        (10.2, LLMResponse(text_response="Completed after the advisory budget.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Keep working while progress is useful.",
        context=[],
        llm_client=client,
        model="test-model",
        model_parameters={"request_timeout_seconds": 7.0},
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event))
        ),
        turn_id="turn-elapsed-advisories",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Completed after the advisory budget."
    assert len(client.calls) == 4
    assert all(
        {tool.name for tool in call["available_tools"]} == all_tool_names
        for call in client.calls
    )
    assert all(
        "request_timeout_seconds" not in call["llm_params"] for call in client.calls
    )
    assert all("timeout_seconds" not in call["llm_params"] for call in client.calls)
    assert all(call["request_timeout_seconds"] is None for call in result.llm_calls)
    assert all(call["request_advisory_seconds"] >= 7.0 for call in result.llm_calls)
    advisory_events = [
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_elapsed_time_advisory"
    ]
    assert [item["advisory_kind"] for item in advisory_events] == [
        "research_interval",
        "answer_reserve",
        "turn_budget",
    ]
    assert all(item["capabilities_removed"] is False for item in advisory_events)
    system_messages = [call["system_message"] for call in client.calls]
    for notice in (
        "The planned research interval has elapsed.",
        "The planned final-answer reserve has begun.",
        "The planned turn budget has elapsed.",
    ):
        assert sum(notice in message for message in system_messages) == 1
    progress_advisories = [
        event
        for event in progress_events
        if event.get("stage") == "elapsed_time_advisory"
    ]
    assert len(progress_advisories) == 3
    assert [item["call_id"] for item in result.tool_invocations] == [
        "after-research-advisory",
        "after-answer-advisory",
        "after-turn-advisory",
    ]


def test_model_result_returned_after_turn_advisory_remains_usable() -> None:
    clock = _ManualClock()
    progress_events: list[dict[str, Any]] = []
    client = _LateResponseClient(
        clock,
        0.2,
        LLMResponse(text_response="A useful answer after the advisory."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer when ready.",
        context=[],
        llm_client=client,
        model="test-model",
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event))
        ),
        turn_id="turn-late-model-result",
        turn_budget_seconds=0.05,
        final_synthesis_reserve_seconds=0.01,
        clock=clock,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "A useful answer after the advisory."
    assert result.llm_calls[0]["status"] == "completed"
    allocation = next(
        event
        for event in result.aux_llm_calls
        if event.get("type") == "adaptive_turn_budget_allocation"
    )
    assert allocation["enforcement"] == "advisory"
    advisories = [
        event
        for event in result.aux_llm_calls
        if event.get("type") == "adaptive_turn_elapsed_time_advisory"
    ]
    assert [event["advisory_kind"] for event in advisories] == [
        "research_interval",
        "answer_reserve",
        "turn_budget",
    ]
    assert (
        len(
            [
                event
                for event in progress_events
                if event.get("stage") == "elapsed_time_advisory"
            ]
        )
        == 3
    )


def test_model_call_duration_is_advisory_and_late_result_is_retained() -> None:
    clock = _ManualClock()
    client = _LateResponseClient(
        clock,
        2.0,
        LLMResponse(text_response="Useful result after the model advisory."),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer when the model has finished.",
        context=[],
        llm_client=client,
        model="test-model",
        model_parameters={"request_timeout_seconds": 1.0},
        turn_id="turn-model-call-advisory",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        clock=clock,
    )

    assert result.terminal_status == "completed"
    assert result.response_text == "Useful result after the model advisory."
    assert "request_timeout_seconds" not in client.calls[0]["llm_params"]
    assert result.llm_calls[0]["request_timeout_seconds"] is None
    assert result.llm_calls[0]["request_advisory_seconds"] == 1.0
    assert result.llm_calls[0]["advisory_budget_exceeded"] is True
    advisory = next(
        event
        for event in result.aux_llm_calls
        if event.get("type") == "adaptive_turn_model_call_advisory"
    )
    assert advisory["result_retained"] is True


def test_elapsed_advisory_preserves_native_continuation_and_bounded_evidence() -> None:
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
                    tool_name="turn_invoke_capability",
                    call_id="call-advisory-evidence",
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
        turn_id="turn-advisory-evidence",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=0,
        clock=clock,
    )

    assert result.response_text == "The final answer uses usable evidence."
    assert len(client.calls) == 2
    final_call = client.calls[1]
    assert {tool.name for tool in final_call["available_tools"]} == {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    assert final_call["continuation"].response_id == "response-1"
    assert len(final_call["tool_results"]) == 1
    tool_output = final_call["tool_results"][0].output
    assert tool_output["call_id"] == "call-advisory-evidence"
    assert "usable evidence" in tool_output["preview"]
    assert raw_tail not in json.dumps(tool_output)
    assert "The planned research interval has elapsed." in (
        final_call["system_message"]
    )


@pytest.mark.parametrize(
    "native_continuation",
    [True, False],
    ids=["native-continuation", "stateless-context"],
)
def test_hydrated_evidence_survives_research_advisory_for_provider_styles(
    native_continuation: bool,
) -> None:
    started = time.monotonic()
    research_advisory_at = started + 8.0
    clock = _ManualClock(started)
    client = _HydrationAdvisoryClient(
        clock,
        native_continuation=native_continuation,
        research_advisory_at=research_advisory_at,
    )
    raw_tail = "z" * 50_000

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "record": {
                    "a_raw": raw_tail,
                    "z_summary": (
                        "The specifically hydrated result remains available."
                    ),
                },
            }
        ),
        prompt="Research the record and answer.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"turn-hydration-{'native' if native_continuation else 'stateless'}",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=0,
        clock=clock,
    )

    assert result.response_text == (
        "The final answer uses the explicitly hydrated evidence."
    )
    assert len(client.calls) == 3
    assert len(client.received_evidence_outputs) == 2
    delivered_envelope, delivered_slice = client.received_evidence_outputs
    assert delivered_envelope["schema_version"] == "turn_evidence_envelope.v1"
    assert delivered_slice["schema_version"] == "turn_evidence_slice.v1"
    assert delivered_slice["content"] == (
        "The specifically hydrated result remains available."
    )

    final_call = client.calls[-1]
    assert {tool.name for tool in final_call["available_tools"]} == {
        "turn_capabilities",
        "turn_invoke_capability",
        "turn_list_evidence",
        "turn_read_evidence",
    }
    assert "The planned research interval has elapsed." in (
        final_call["system_message"]
    )
    if native_continuation:
        assert final_call["continuation"].response_id == "response-hydration"
        assert final_call["tool_results"][0].output == delivered_slice
    else:
        assert "continuation" not in final_call
        tool_messages = [
            item for item in final_call["context"] if item.get("role") == "tool"
        ]
        assert json.loads(tool_messages[-1]["content"]) == delivered_slice
    assert raw_tail not in json.dumps(final_call, default=str)


def test_many_read_results_share_one_model_context_budget() -> None:
    calls = [
        ToolCall(
            tool_name="turn_invoke_capability",
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
                    tool_name="turn_capabilities",
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
    assert {tool.name for tool in client.calls[1]["available_tools"]} == {
        "turn_list_evidence",
        "turn_read_evidence",
    }
    overflow = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_tool_result_batch_overflow"
    )
    assert overflow["tool_call_count"] == 500
    assert overflow["action"] == "fresh_final_synthesis_with_pageable_evidence_index"


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
                    tool_name="turn_invoke_capability",
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
                    tool_name="turn_invoke_capability",
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
        == "capability_not_delegated"
    )


def test_represented_workflow_is_discovered_and_invoked_as_bound_capability(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    seen_arguments: dict[str, Any] = {}
    progress_events: list[dict[str, Any]] = []
    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_turn_test",
        workflow_id="#V#represented_test_workflow",
        display_name="Represented test workflow",
        description="Produce the represented test work product.",
        relevance_score=0.94,
        semantic_effect=True,
        semantic_effect_source="represented_workflow_declaration",
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {
                    "type": "object",
                    "properties": {"record_id": {"type": "string"}},
                }
            },
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )

    def _execute_workflow(**kwargs: Any) -> dict[str, Any]:
        seen_arguments.update(kwargs)
        return {
            "success": True,
            "instance_id": "workflow-instance-1",
            "created_new": True,
            "final_status": "completed",
            "workflow_execution": {
                "final_status": "completed",
                "current_state": "record_description",
                "latest_step_result_envelope": {
                    "schema_version": "workflow_step_result_envelope.v1",
                    "workflow_id": "#V#represented_test_workflow",
                    "state_id": "record_description",
                    "action_id": "upsert_research_description",
                    "action_status": "success",
                    "action_outcome": "success",
                    "state_attempt": 1,
                    "diagnostics": {"duration_ms": 125},
                    "progress_facts": [
                        {
                            "schema_version": "workflow_progress_projection.v1",
                            "fact_id": "student_name",
                            "label": "Student",
                            "status": "available",
                            "present": True,
                            "visibility": "default",
                            "value": "Nathan Doe",
                            "source_path": "context.student_name",
                        },
                        {
                            "schema_version": "workflow_progress_projection.v1",
                            "fact_id": "description_date",
                            "label": "Description date",
                            "status": "available",
                            "present": True,
                            "visibility": "default",
                            "value": "2026-08-05",
                            "source_path": "context.description_date",
                        },
                    ],
                },
            },
        }

    gateway = _workflow_gateway(
        _execute_workflow,
        hard_timeout_enabled=False,
    )
    assert "workflow_execute" not in ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#real_user",
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-workflow",
                    payload={"query": "produce the represented work product"},
                )
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="workflow-discovery-response",
            ),
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {
                            "workflow_id": "#V#spoofed_workflow",
                            "user_id": "#V#spoofed_user",
                            "inputs": {
                                "record_id": "#V#record",
                                "user_concept_id": "#V#spoofed_user",
                            },
                            "timeout_seconds": 180.0,
                        },
                    },
                )
            ],
            continuation=LLMContinuation(
                provider="test",
                api_surface="responses",
                model="test-model",
                response_id="workflow-invocation-response",
            ),
        ),
        LLMResponse(text_response="The represented work product was completed."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Produce the represented work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        workflow_launch_inputs={"authorised_record": "#V#request_record"},
        turn_id="turn-represented-workflow",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event)),
            check_cancellation=lambda: None,
        ),
    )

    catalogue_result = client.calls[1]["tool_results"][0].output
    assert catalogue_result["represented_workflow_total"] == 1
    workflow_purpose = next(
        item
        for item in catalogue_result["purpose_index"]["entries"]
        if item["name"] == workflow_capability.name
    )
    assert workflow_purpose["shape"] == "represented_workflow"
    assert (
        catalogue_result["selection_policy"]["semantic_adequacy_owner"]
        == "adaptive_model"
    )
    assert seen_arguments["workflow_id"] == "#V#represented_test_workflow"
    assert seen_arguments["user_id"] == "#V#real_user"
    assert seen_arguments["org_id"] == "#V#real_org"
    assert seen_arguments["namespace"] == "#V#real_user@real_org"
    assert seen_arguments["inputs"]["user_concept_id"] == "#V#real_user"
    assert seen_arguments["inputs"]["record_id"] == "#V#record"
    assert seen_arguments["inputs"]["authorised_record"] == "#V#request_record"
    assert seen_arguments["source_event_type"] == "conversation_turn"
    assert seen_arguments["source_event_id"] == "turn-represented-workflow"
    assert seen_arguments["timeout_seconds"] == 90.0
    assert seen_arguments["event_idempotency_key"].startswith(
        "conversation_turn_workflow:"
    )
    invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert invocation["tool"] == workflow_capability.name
    assert invocation["execution_method"] == "workflow_execute"
    assert invocation["capability_kind"] == "represented_workflow"
    assert invocation["capability_display_name"] == "Represented test workflow"
    assert invocation["represented_workflow_id"] == ("#V#represented_test_workflow")
    assert invocation["effect_status"] == "succeeded"
    assert invocation["changed"] is True
    assert invocation["instance_id"] == "workflow-instance-1"
    assert invocation["workflow_id"] == "#V#represented_test_workflow"
    assert invocation["plan_profile"]["shape"] == "represented_workflow"
    assert invocation["workflow_progress_evidence"]["facts"] == [
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "student_name",
            "label": "Student",
            "status": "available",
            "present": True,
            "redacted": False,
            "truncated": False,
            "source_path": "context.student_name",
            "payload_source_path": (
                "workflow_execution.latest_step_result_envelope.progress_facts"
            ),
            "visibility": "default",
            "value": "Nathan Doe",
        },
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "description_date",
            "label": "Description date",
            "status": "available",
            "present": True,
            "redacted": False,
            "truncated": False,
            "source_path": "context.description_date",
            "payload_source_path": (
                "workflow_execution.latest_step_result_envelope.progress_facts"
            ),
            "visibility": "default",
            "value": "2026-08-05",
        },
    ]
    selection_trace = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_capability_selection"
    )
    assert selection_trace["capability_name"] == workflow_capability.name
    assert selection_trace["selection_policy"]["representedness_priority"] is False
    assert selection_trace["plan_profile"]["shape"] == ("represented_workflow")
    workflow_events = [
        event for event in progress_events if event.get("call_id") == "invoke-workflow"
    ]
    assert [event["event_kind"] for event in workflow_events] == [
        "tool_call_start",
        "tool_call_end",
    ]
    assert [event["subtask"] for event in workflow_events] == [
        "Represented test workflow",
        "Represented test workflow",
    ]
    assert workflow_events[0]["result_summary"] == ("Using Represented test workflow.")
    assert workflow_events[1]["result_summary"] == (
        "Finished Represented test workflow."
    )
    for event in workflow_events:
        semantic_operation = event["semantic_operation"]
        assert semantic_operation["capability"]["id"] == workflow_capability.name
        assert semantic_operation["capability"]["label"] == (
            "Represented test workflow"
        )
        assert semantic_operation["arguments"][0]["value"] == (
            "#V#represented_test_workflow"
        )
    start_execution = workflow_events[0]["selected_workflow_execution_event"]
    assert start_execution == {
        "schema_version": "selected_workflow_execution_event.v1",
        "status": "workflow_execution_start",
        "event_kind": "workflow_execution_start",
        "workflow_id": "#V#represented_test_workflow",
        "selected_workflow_id": "#V#represented_test_workflow",
        "selected_workflow_name": "Represented test workflow",
        "selected_execution_mode": "adaptive_turn_capability",
    }
    completed_execution = workflow_events[1]["selected_workflow_execution_event"]
    assert completed_execution["status"] == "workflow_execution_complete"
    assert completed_execution["state_id"] == "record_description"
    assert completed_execution["action_id"] == "upsert_research_description"
    assert completed_execution["action_outcome"] == "success"
    assert completed_execution["effect_status"] == "succeeded"
    assert completed_execution["semantic_effect"] is True
    assert [fact["label"] for fact in completed_execution["progress_facts"]] == [
        "Student",
        "Description date",
    ]
    assert workflow_events[1]["progress_facts"] == completed_execution["progress_facts"]
    assert result.response_text == "The represented work product was completed."


def test_represented_workflow_failure_progress_is_actionable() -> None:
    from src.backend.services.adaptive_turn_service import (
        _build_represented_workflow_execution_event,
    )

    event, progress_evidence = _build_represented_workflow_execution_event(
        payload={
            "final_status": "failed",
            "effect_status": "failed",
            "semantic_effect": True,
            "changed": False,
            "mutation_outcome": "partial",
            "outcome_finality": "terminal_for_turn",
            "recovery_affordances": [{"action_type": "inspect_workflow_instance"}],
            "workflow_execution": {
                "current_state": "extract_attachment",
                "error": "Attachment text extraction failed.",
                "latest_step_result_envelope": {
                    "state_id": "extract_attachment",
                    "action_id": "extract_pdf_text",
                    "action_status": "failed",
                    "action_outcome": "failure",
                    "diagnostics": {
                        "error": "The attached PDF could not be read.",
                        "duration_ms": 430,
                    },
                    "progress_facts": [
                        {
                            "schema_version": "workflow_progress_projection.v1",
                            "fact_id": "attachment_name",
                            "label": "Attachment",
                            "status": "available",
                            "present": True,
                            "visibility": "default",
                            "value": "research-description.pdf",
                            "source_path": "context.attachment_name",
                        }
                    ],
                },
            },
        },
        workflow_id="#V#student_research_description_workflow",
        workflow_name="Student research description workflow",
    )

    assert event["status"] == "workflow_execution_failed"
    assert event["state_id"] == "extract_attachment"
    assert event["action_id"] == "extract_pdf_text"
    assert event["action_outcome"] == "failure"
    assert event["error"] == "The attached PDF could not be read."
    assert event["effect_status"] == "failed"
    assert event["semantic_effect"] is True
    assert event["changed"] is False
    assert event["next_action"] == "Inspect workflow instance"
    assert event["progress_facts"][0]["label"] == "Attachment"
    assert progress_evidence is not None
    assert progress_evidence["facts"][0]["value"] == ("research-description.pdf")


def test_represented_workflow_nonfinite_wait_is_typed_not_started_feedback(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_invalid_wait_test",
        workflow_id="#V#represented_invalid_wait_workflow",
        display_name="Represented invalid-wait workflow",
        description="Produce a represented work product.",
        relevance_score=0.94,
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )

    def _unexpected_execution(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("invalid wait arguments must not submit a workflow")

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-invalid-wait-workflow",
                    payload={"query": "produce the represented work product"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-invalid-wait-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {"timeout_seconds": float("inf")},
                    },
                )
            ],
        ),
        LLMResponse(text_response="I corrected the invalid wait without an effect."),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(
            _unexpected_execution,
            hard_timeout_enabled=False,
        ),
        prompt="Produce the represented work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-represented-workflow-invalid-wait",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert invocation["error_code"] == ("invalid_workflow_capability_arguments")
    assert invocation["effect_status"] == "not_started"
    assert invocation["changed"] is False
    assert invocation["mutation_outcome"] == "not_started"
    assert result.terminal_status == "completed"


@pytest.mark.parametrize(
    (
        "read_workflow_id",
        "read_status",
        "expected_effect_status",
        "expected_terminal_status",
        "expected_fallback",
    ),
    [
        (
            "#V#represented_readback_workflow",
            "completed",
            "succeeded",
            "completed",
            False,
        ),
        (
            "#V#represented_readback_workflow",
            "running",
            "partial",
            "effect_partially_completed",
            True,
        ),
        (
            "#V#represented_readback_workflow",
            "failed",
            "failed",
            "effect_failed",
            True,
        ),
        (
            "#V#different_workflow",
            "completed",
            "partial",
            "effect_partially_completed",
            True,
        ),
    ],
    ids=["completed", "non-terminal", "failed", "workflow-mismatch"],
)
def test_workflow_instance_readback_reconciles_only_exact_terminal_effect(
    monkeypatch,
    read_workflow_id: str,
    read_status: str,
    expected_effect_status: str,
    expected_terminal_status: str,
    expected_fallback: bool,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_readback_test",
        workflow_id="#V#represented_readback_workflow",
        display_name="Represented read-back workflow",
        description="Produce a durable work product and verify its terminal state.",
        relevance_score=0.95,
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {"type": "object", "additionalProperties": True},
                "timeout_seconds": {"type": "number"},
            },
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )
    instance_reads: list[dict[str, Any]] = []

    def _execute_workflow(**_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": "workflow-instance-readback-1",
            "workflow_id": workflow_capability.workflow_id,
            "created_new": True,
            "final_status": "running",
            "timed_out": True,
            "workflow_execution": {
                "timeout_seconds": 90.0,
                "poll_interval_seconds": 0.5,
            },
        }

    def _read_instance(**kwargs: Any) -> dict[str, Any]:
        instance_reads.append(dict(kwargs))
        return {
            "success": True,
            "instance_id": "workflow-instance-readback-1",
            "workflow_id": read_workflow_id,
            "status": read_status,
            "outputs": {"verified": True},
            "timed_out": False,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-readback-workflow",
                    payload={"query": "produce and verify the durable work product"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-readback-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {"timeout_seconds": 90.0},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="await-readback-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {
                            "instance_id": "workflow-instance-readback-1",
                            "await_terminal": True,
                            "timeout_seconds": 90.0,
                            "poll_interval_seconds": 0.5,
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="repeat-readback-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {
                            "instance_id": "workflow-instance-readback-1",
                            "await_terminal": True,
                            "timeout_seconds": 90.0,
                            "poll_interval_seconds": 0.5,
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response="The durable work product was verified."),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(
            _execute_workflow,
            hard_timeout_enabled=False,
            instance_handler=_read_instance,
        ),
        prompt="Produce and verify the durable work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-represented-workflow-readback",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    expected_instance_read = {
        "instance_id": "workflow-instance-readback-1",
        "await_terminal": True,
        "timeout_seconds": 90.0,
        "poll_interval_seconds": 0.5,
    }
    assert instance_reads == [
        expected_instance_read,
        expected_instance_read,
    ]
    workflow_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert workflow_invocation["effect_status"] == expected_effect_status
    if expected_effect_status in {"succeeded", "failed"}:
        canonical_readback = workflow_invocation["canonical_readback"]
        assert {
            key: canonical_readback.get(key)
            for key in ("capability", "instance_id", "workflow_id", "status")
        } == {
            "capability": "workflow_get_instance",
            "instance_id": "workflow-instance-readback-1",
            "workflow_id": workflow_capability.workflow_id,
            "status": read_status,
        }
        assert isinstance(canonical_readback.get("evidence_id"), str)
    else:
        assert "canonical_readback" not in workflow_invocation
    assert result.terminal_status == expected_terminal_status
    assert result.effect_finality_fallback is expected_fallback
    if expected_fallback:
        assert result.response_text != "The durable work product was verified."
    else:
        assert result.response_text == "The durable work product was verified."


@pytest.mark.parametrize(
    ("read_back_trip", "expected_status", "expected_fallback"),
    [
        (True, "effect_partially_completed", False),
        (False, "effect_failed", True),
    ],
    ids=["exact-direct-readback", "unverified-direct-success"],
)
def test_failed_workflow_and_later_direct_trip_effects_preserve_only_verified_answer(
    monkeypatch: pytest.MonkeyPatch,
    read_back_trip: bool,
    expected_status: str,
    expected_fallback: bool,
) -> None:
    """Regress request 95c16c12: a failed route must not erase verified recovery."""

    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_trip_creation_test",
        workflow_id="#V#represented_trip_creation_workflow",
        display_name="Represented trip creation workflow",
        description="Create one trip and connect its existing flight components.",
        relevance_score=0.99,
        semantic_effect=True,
        semantic_effect_source="represented_workflow_declaration",
        input_schema={"type": "object", "properties": {}},
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )

    instance_id = "workflow-trip-failed-before-domain-mutation"
    trip_id = "#V#american_airlines_confirmation_trip_gmail_derived"
    leg_ids = [
        f"#V#flight_trip_component_gmail_19febb3feda7b024_leg_0{index}"
        for index in (1, 2, 3)
    ]

    def execute_workflow(**_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": instance_id,
            "workflow_id": workflow_capability.workflow_id,
            "created_new": True,
            "final_status": "failed",
            "workflow_execution": {
                "final_status": "failed",
                "current_state": "initialise_from_item_request",
                "error": "metadata validation failed before domain mutation",
            },
        }

    def read_instance(**_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": instance_id,
            "workflow_id": workflow_capability.workflow_id,
            "status": "failed",
        }

    gateway = _workflow_gateway(
        execute_workflow,
        hard_timeout_enabled=False,
        instance_handler=read_instance,
    )

    def direct_effect(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "create_concepts":
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "created_concept_ids": [trip_id],
            }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "source_id": arguments["source_id"],
            "target": arguments["target"],
        }

    for capability_name in ("create_concepts", "add_relationship"):
        gateway._catalogue.register(
            MethodDefinition(
                name=capability_name,
                handler=lambda _name=capability_name, **kwargs: direct_effect(
                    _name,
                    kwargs,
                ),
                input_schema=Schema(allow_unknown=True),
                output_schema=Schema(required={"success": bool}, allow_unknown=True),
                category="write",
                ordinary_turn_effect=True,
            )
        )
        gateway.register_metrics_if_missing(capability_name)
    gateway._catalogue.register(
        MethodDefinition(
            name="fetch_concept",
            handler=lambda concept_id: {
                "success": True,
                "concept_id": concept_id,
                "relations": [
                    {
                        "source_id": trip_id,
                        "predicate": "#V#has_trip_component",
                        "target_id": leg_id,
                    }
                    for leg_id in leg_ids
                ],
            },
            input_schema=Schema(required={"concept_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway.register_metrics_if_missing("fetch_concept")

    responses = [
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-trip-workflow",
                    payload={"query": "create and connect the trip"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-trip-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-failed-trip-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {"instance_id": instance_id},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="create-trip-directly",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"concept_id": trip_id}]},
                    },
                ),
                *[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id=f"link-trip-leg-{index}",
                        payload={
                            "name": "add_relationship",
                            "arguments": {
                                "source_id": trip_id,
                                "predicate": "#V#has_trip_component",
                                "target": leg_id,
                            },
                        },
                    )
                    for index, leg_id in enumerate(leg_ids, start=1)
                ],
            ],
        ),
    ]
    if read_back_trip:
        responses.append(
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_invoke_capability",
                        call_id="read-trip-direct-result",
                        payload={
                            "name": "fetch_concept",
                            "arguments": {"concept_id": trip_id},
                        },
                    )
                ],
            )
        )
    useful_answer = (
        f"The neutral trip {trip_id} and its three component links persist; "
        f"the earlier workflow instance {instance_id} failed."
    )
    responses.append(LLMResponse(text_response=useful_answer))

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Create the agreed trip and tell me what actually persisted.",
        context=[],
        llm_client=_SequenceClient(*responses),
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id=f"trip-mixed-finality-{read_back_trip}",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert result.terminal_status == expected_status
    assert result.effect_finality_fallback is expected_fallback
    workflow_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert workflow_invocation["effect_status"] == "failed"
    assert workflow_invocation["changed"] is True
    assert workflow_invocation["canonical_readback"]["status"] == "failed"
    if read_back_trip:
        assert useful_answer in result.response_text
        preservation = next(
            item
            for item in result.aux_llm_calls
            if item.get("type") == "adaptive_turn_mixed_effect_response_preserved"
        )
        assert preservation["preservation_basis"] == (
            "failed_workflow_and_material_successes_exactly_read_back"
        )
        assert preservation["canonically_verified_succeeded_count"] == 4
        assert preservation["failed_workflow_count"] == 1
        assert preservation["known_no_change_count"] == 0
    else:
        assert useful_answer not in result.response_text


def test_pending_durable_response_with_exact_handle_is_preserved(monkeypatch) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_pending_test",
        workflow_id="#V#represented_pending_workflow",
        display_name="Represented pending workflow",
        description="Produce a durable work product that may continue in background.",
        relevance_score=0.95,
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {"type": "object", "additionalProperties": True},
                "timeout_seconds": {"type": "number"},
            },
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )
    instance_id = "workflow-instance-pending-1"
    instance_reads: list[dict[str, Any]] = []

    def _execute_workflow(**_kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "instance_id": instance_id,
            "workflow_id": workflow_capability.workflow_id,
            "created_new": True,
            "final_status": "running",
            "timed_out": True,
            "workflow_execution": {
                "timeout_seconds": 90.0,
                "poll_interval_seconds": 0.5,
            },
        }

    def _read_instance(**kwargs: Any) -> dict[str, Any]:
        instance_reads.append(dict(kwargs))
        return {
            "success": True,
            "instance_id": instance_id,
            "workflow_id": workflow_capability.workflow_id,
            "status": "running",
            "timed_out": False,
        }

    final_text = (
        "The durable work is still running. Its exact workflow instance is "
        f"{instance_id}."
    )
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-pending-workflow",
                    payload={"query": "produce the durable work product"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-pending-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {"timeout_seconds": 90.0},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="inspect-pending-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {
                            "instance_id": instance_id,
                            "await_terminal": False,
                        },
                    },
                )
            ],
        ),
        LLMResponse(text_response=final_text),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(
            _execute_workflow,
            hard_timeout_enabled=False,
            instance_handler=_read_instance,
        ),
        prompt="Produce the durable work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-represented-workflow-pending",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert instance_reads == [{"instance_id": instance_id, "await_terminal": False}]
    assert result.terminal_status == "effect_partially_completed"
    assert result.effect_finality_fallback is False
    assert result.response_text == final_text
    assert any(
        call.get("schema_version")
        == "adaptive_turn_pending_effect_response_preserved.v1"
        for call in result.aux_llm_calls
    )


def test_workflow_instance_readback_does_not_reconcile_unrelated_effect() -> None:
    instance_id = "shared-looking-instance-id"
    workflow_id = "#V#shared-looking-workflow-id"

    def _effect_handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": False,
            "effect_status": "partial",
            "changed": True,
            "instance_id": instance_id,
            "workflow_id": workflow_id,
        }

    gateway = _effect_gateway(_effect_handler)
    gateway._catalogue.register(
        MethodDefinition(
            name="workflow_get_instance",
            handler=lambda **_kwargs: {
                "success": True,
                "instance_id": instance_id,
                "workflow_id": workflow_id,
                "status": "completed",
            },
            input_schema=Schema(required={"instance_id": str}),
            output_schema=Schema(required={"success": bool}, allow_unknown=True),
            category="read",
        )
    )
    gateway.register_metrics_if_missing("workflow_get_instance")
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-unrelated-effect",
                    payload={
                        "name": "create_concepts",
                        "arguments": {"concepts": [{"name": "Partial result"}]},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="read-lookalike-workflow",
                    payload={
                        "name": "workflow_get_instance",
                        "arguments": {"instance_id": instance_id},
                    },
                )
            ],
        ),
        LLMResponse(text_response="Everything completed."),
    )

    result = execute_adaptive_turn(
        gateway=gateway,
        prompt="Complete the bounded effect.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="turn-unrelated-workflow-lookalike",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    effect_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == "create_concepts"
    )
    assert effect_invocation["capability_kind"] == "registered_tool"
    assert effect_invocation["effect_status"] == "partial"
    assert "canonical_readback" not in effect_invocation
    assert result.terminal_status == "effect_partially_completed"
    assert result.effect_finality_fallback is True
    assert result.response_text != "Everything completed."


def test_read_only_workflow_not_started_preserves_successful_direct_recovery(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_mail_review_test",
        workflow_id="#V#mail_review_test_workflow",
        display_name="Mail review test workflow",
        description="Review recent messages through bounded read capabilities.",
        relevance_score=0.96,
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {"type": "object", "properties": {}},
            },
            "additionalProperties": False,
        },
        component_capability_names=("general_read",),
        declared_component_count=1,
        declared_step_count=3,
        semantic_effect=None,
        semantic_effect_source="insufficient_declared_component_evidence",
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )

    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-mail-workflow",
                    payload={"query": "summarise recent messages"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-mail-workflow",
                    payload={
                        "name": workflow_capability.name,
                        "arguments": {"inputs": {}},
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="recover-with-direct-read",
                    payload={
                        "name": "general_read",
                        "arguments": {"query": "recent messages"},
                    },
                )
            ],
        ),
        LLMResponse(text_response="The recent messages were summarised successfully."),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(
            lambda **_kwargs: {
                "success": False,
                "error_code": "workflow_not_runnable",
                "status": "rejected_preflight",
            }
        ),
        prompt="Summarise my recent messages.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-read-only-workflow-direct-recovery",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert result.response_text == ("The recent messages were summarised successfully.")
    assert result.terminal_status == "completed"
    assert result.effect_finality_fallback is False
    workflow_invocation = next(
        item
        for item in result.tool_invocations
        if item.get("tool") == workflow_capability.name
    )
    assert workflow_invocation["effect_status"] == "not_started"
    assert workflow_invocation["changed"] is False
    assert workflow_invocation["semantic_effect"] is None
    assert workflow_invocation["turn_finality_required"] is False
    assert "instance_id" not in workflow_invocation
    assert workflow_invocation["evidence"]["semantic_effect"] is None
    assert workflow_invocation["evidence"]["turn_finality_required"] is False
    direct_invocation = next(
        item for item in result.tool_invocations if item.get("tool") == "general_read"
    )
    assert direct_invocation["status"] == "ok"


def test_represented_workflow_retry_reuses_same_turn_idempotency_key(
    monkeypatch,
) -> None:
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_retry_test",
        workflow_id="#V#represented_retry_workflow",
        display_name="Represented retry workflow",
        description="Produce one durable work product.",
        relevance_score=0.95,
        input_schema={
            "type": "object",
            "properties": {
                "inputs": {
                    "type": "object",
                    "properties": {"record_id": {"type": "string"}},
                }
            },
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_turn_capability_service."
        "discover_turn_workflow_capabilities",
        lambda *_args, **_kwargs: (
            [workflow_capability],
            {
                "schema_version": "workflow_turn_capability_discovery.v1",
                "status": "completed",
                "match_count": 1,
            },
        ),
    )
    idempotency_keys: list[str] = []

    def _execute_workflow(**kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["event_idempotency_key"])
        idempotency_keys.append(key)
        return {
            "success": True,
            "instance_id": "workflow-instance-reused",
            "created_new": len(idempotency_keys) == 1,
            "final_status": "running",
            "timed_out": True,
        }

    repeated_call = {
        "name": workflow_capability.name,
        "arguments": {"inputs": {"record_id": "#V#record"}},
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_capabilities",
                    call_id="discover-retry-workflow",
                    payload={"query": "produce the durable work product"},
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-retry-workflow-1",
                    payload=repeated_call,
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invoke-retry-workflow-2",
                    payload=repeated_call,
                )
            ],
        ),
        LLMResponse(
            text_response="The durable workflow is still running.",
        ),
    )

    result = execute_adaptive_turn(
        gateway=_workflow_gateway(_execute_workflow),
        prompt="Produce the durable work product.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#real_user@real_org",
        user_concept_id="#V#real_user",
        org_concept_id="#V#real_org",
        turn_id="turn-represented-workflow-retry",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert len(idempotency_keys) == 2
    assert idempotency_keys[0] == idempotency_keys[1]
    workflow_invocations = [
        invocation
        for invocation in result.tool_invocations
        if invocation.get("tool") == workflow_capability.name
    ]
    assert [item["instance_id"] for item in workflow_invocations] == [
        "workflow-instance-reused",
        "workflow-instance-reused",
    ]
    assert [item["changed"] for item in workflow_invocations] == [True, False]
    assert [item["effect_status"] for item in workflow_invocations] == [
        "partial",
        "partial",
    ]
    assert len(client.calls) == 4
    assert all(
        "requested outcome and effect cardinality" in call["system_message"]
        and "must not create, update, or otherwise act on more"
        in call["system_message"]
        for call in client.calls
    )


def test_unchanged_terminally_failed_effect_is_not_dispatched_twice(
    monkeypatch,
) -> None:
    handler_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.services.adaptive_turn_service." "_effect_subject_authorised",
        lambda *_args, **_kwargs: True,
    )

    def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "add_relationship"
        handler_calls.append(arguments)
        return {
            "success": False,
            "effect_status": "failed",
            "changed": False,
            "error_code": "invalid_predicate_format",
            "retryable": False,
        }

    effect_arguments = {
        "source_id": "#V#source",
        "predicate": "unresolved predicate",
        "target": "#V#target",
    }
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invalid-effect-1",
                    payload={
                        "name": "add_relationship",
                        "arguments": effect_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="invalid-effect-2",
                    payload={
                        "name": "add_relationship",
                        "arguments": effect_arguments,
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The relationship was not changed; a different typed predicate "
                "reference is required."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, effect_admission_window_sec=0.01),
        prompt="Add this relationship.",
        context=[],
        llm_client=client,
        model="test-model",
        user_concept_id="#V#user",
        turn_id="turn-repeat-terminal-failure",
        turn_budget_seconds=20,
        final_synthesis_reserve_seconds=2,
    )

    assert handler_calls == [effect_arguments]
    assert len(result.tool_invocations) == 2
    assert result.tool_invocations[0]["error_code"] == ("invalid_predicate_format")
    assert result.tool_invocations[1]["error_code"] == (
        "effect_request_unchanged_after_terminal_failure"
    )
    assert result.tool_invocations[1]["effect_status"] == "not_started"
    assert result.tool_invocations[1]["changed"] is False


def test_model_call_progress_reports_cumulative_usage_cost_and_exact_identity() -> None:
    progress_events: list[dict[str, Any]] = []
    client = _SequenceClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="turn_list_evidence",
                    call_id="list-evidence",
                    payload={},
                )
            ],
            model="gpt-test-effective",
            usage={"prompt_tokens": 100, "completion_tokens": 10},
            transport_metadata={
                "effective_service_tier": "default",
                "effective_connection_id": "#V#openai_provider",
            },
        ),
        LLMResponse(
            text_response="Done.",
            model="gpt-test-effective",
            usage={"prompt_tokens": 50, "completion_tokens": 5},
            transport_metadata={
                "effective_service_tier": "default",
                "effective_connection_id": "#V#openai_provider",
            },
        ),
    )
    client.config = SimpleNamespace(provider="openai")
    registry = {
        "models": [
            {
                "provider": "openai",
                "model_id": "gpt-test-effective",
                "pricing": {
                    "schema_version": "llm_model_pricing.v1",
                    "version": "test-v1",
                    "source": "test",
                    "effective_at_utc": "2026-08-05T00:00:00Z",
                    "model_id": "gpt-test-effective",
                    "currency": "USD",
                    "unit_tokens": 1_000,
                    "rates": {
                        "input_tokens": 1.0,
                        "output_tokens": 2.0,
                    },
                },
            }
        ]
    }

    execute_adaptive_turn(
        gateway=None,
        prompt="Use evidence if useful, then answer.",
        context=[],
        llm_client=client,
        model="gpt-test-requested",
        progress_tracker=SimpleNamespace(
            emit=lambda event: progress_events.append(dict(event))
        ),
        turn_id="usage-cost-progress",
        model_registry_snapshot=registry,
    )

    model_end_events = [
        event
        for event in progress_events
        if event.get("event_kind") == "llm_call_end"
        and str(event.get("call_id") or "").startswith("usage-cost-progress:llm:")
    ]
    assert [event["call_id"] for event in model_end_events] == [
        "usage-cost-progress:llm:1",
        "usage-cost-progress:llm:2",
    ]
    assert model_end_events[0]["requested_model"] == "gpt-test-requested"
    assert model_end_events[0]["selected_model"] == "gpt-test-requested"
    assert model_end_events[0]["effective_model"] == "gpt-test-effective"
    assert model_end_events[0]["model_identity_source"] == "provider_response"
    first_summary = model_end_events[0]["llm_usage_cost_summary"]
    second_summary = model_end_events[1]["llm_usage_cost_summary"]
    assert first_summary["usage"]["total_tokens"] == 110
    assert first_summary["estimated_cost"]["amount"] == 0.12
    assert second_summary["usage"]["total_tokens"] == 165
    assert second_summary["estimated_cost"]["amount"] == 0.18
    assert second_summary["model_identities"] == [
        {
            "provider": "openai",
            "requested_model": "gpt-test-requested",
            "selected_model": "gpt-test-requested",
            "effective_model": "gpt-test-effective",
            "model_identity_source": "provider_response",
            "provider_request_sent": True,
            "effective_service_tier": "default",
            "connection_id": "#V#openai_provider",
            "call_count": 2,
        }
    ]
