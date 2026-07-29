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
    _CONVERSATION_OBSERVATIONS_MAX_BYTES,
    _CONVERSATION_OBSERVATIONS_MAX_ITEMS,
    _CONVERSATION_SITUATION_MAX_CHARS,
    _bound_tool_results_for_model,
    _bounded_conversation_observation_projection,
    _capability_catalogue,
    _compact_context_after_limit,
    _compact_evidence_index,
    _effect_result_target_ids,
    _effect_subject_authorised,
    _effect_subject_authority_denial,
    _extract_conversation_situation_sidecar,
    _final_synthesis_context,
    _json_bytes,
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
        *(
            (("upsert_scoped_assertion", None),)
            if include_scoped_assertion
            else ()
        ),
    ):
        catalogue.register(
            MethodDefinition(
                name=name,
                handler=lambda _name=name, **kwargs: handler(_name, kwargs),
                input_schema=Schema(allow_unknown=True),
                output_schema=effect_output_schema,
                category="write",
                ordinary_turn_effect=True,
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
                                "acting_user_concept_id": (
                                    "actor_user_concept_id"
                                ),
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


def _workflow_gateway(handler: Any) -> InternalMCPGateway:
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


class _HydrationDeadlineClient:
    def __init__(
        self,
        clock: _ManualClock,
        *,
        native_continuation: bool,
        research_deadline: float,
    ) -> None:
        self.clock = clock
        self.native_continuation = native_continuation
        self.research_deadline = research_deadline
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
            self.clock.now = self.research_deadline + 0.5
            raise TimeoutError("research request ended after receiving hydration")
        return LLMResponse(
            text_response="The final answer uses the explicitly hydrated evidence."
        )


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
    assert {
        tool.name for tool in client.calls[0]["available_tools"]
    } == {
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
    assert set(catalogue_tool.input_schema["properties"]) == {
        "query",
        "names",
        "offset",
        "limit",
    }


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
    projection_with_prior_omissions = (
        _bounded_conversation_observation_projection(
            observations,
            omitted_before=5,
        )
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

    client = _SequenceClient(LLMResponse(text_response="The recorded effect succeeded."))
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


def test_effect_delegation_is_authenticated_and_exactly_metadata_marked() -> None:
    gateway = _effect_gateway(lambda _name, _arguments: {"success": True})
    trusted = {
        "turn_namespace": "#V#person@org",
        "actor_user_concept_id": "#V#person",
    }

    assert ordinary_turn_capability_delegation(
        gateway,
        user_concept_id=None,
        trusted_argument_values=trusted,
    ) == ()
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


def test_effect_catalogue_and_scope_project_resolved_remaining_windows() -> None:
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

    assert gateway.get_method_effect_admission_window_sec(
        "create_concepts"
    ) == pytest.approx(0.125)
    assert gateway.get_method_effect_admission_window_sec("general_read") is None
    assert catalogue_entry["semantic_effect"] is True
    assert catalogue_entry["minimum_effect_window_seconds"] == pytest.approx(0.125)

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

    assert (
        "Remaining effect-capable window before the protected final-answer "
        "reserve: 8.000 seconds." in client.calls[0]["system_message"]
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

    assert (
        "Remaining effect-capable window before the protected final-answer "
        "reserve: 6.000 seconds." in client.calls[0]["system_message"]
    )
    allocation = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_budget_allocation"
    )
    assert allocation["effective_final_answer_reserve_seconds"] == 4.0
    assert allocation["final_answer_reserve_source"] == "environment_or_default"
    assert allocation["explicit_zero_override"] is False


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
    ("answer_reserve", "effect_finished_at", "expected_tool_names"),
    [
        (
            0.0,
            8.1,
            {"turn_list_evidence", "turn_read_evidence"},
        ),
        (2.0, 8.5, set()),
    ],
    ids=["evidence-capable-final", "answer-only-final"],
)
def test_effect_removes_false_draft_from_fresh_final_context(
    answer_reserve: float,
    effect_finished_at: float,
    expected_tool_names: set[str],
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
                        "arguments": {
                            "concepts": [{"name": "A represented concept"}]
                        },
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
    assert {
        tool.name for tool in final_call["available_tools"]
    } == expected_tool_names
    assert not any(
        item.get("role") == "assistant" and item.get("content") == false_draft
        for item in final_call["context"]
    )
    assert false_draft not in json.dumps(final_call["context"])


def test_post_effect_draft_remains_available_to_answer_only_recovery() -> None:
    started = time.monotonic()
    clock = _ManualClock(started)
    useful_draft = "USEFUL POST-EFFECT DRAFT"

    def handler(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        clock.now = started + 8.1
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
        }

    client = _SequenceClient(
        LLMResponse(
            text_response="I have not changed anything.",
            tool_calls=[
                ToolCall(
                    tool_name="turn_invoke_capability",
                    call_id="effect-before-useful-draft",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [{"name": "A represented concept"}]
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=useful_draft,
            tool_calls=[
                ToolCall(
                    tool_name="turn_list_evidence",
                    call_id="list-after-effect",
                    payload={},
                )
            ],
        ),
        TimeoutError("evidence-capable synthesis failed"),
        LLMResponse(text_response="Recovered from the useful draft."),
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
        turn_id="post-effect-draft",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=1,
        clock=clock,
    )

    assert result.response_text == "Recovered from the useful draft."
    answer_only_call = client.calls[-1]
    assert answer_only_call["available_tools"] == []
    assert any(
        item.get("role") == "assistant" and item.get("content") == useful_draft
        for item in answer_only_call["context"]
    )


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
        assert release_handler.wait(timeout=1.0)
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
                        "arguments": {
                            "concepts": [{"name": "A late real concept"}]
                        },
                    },
                )
            ],
        ),
        TimeoutError("model failed after the effect timeout"),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, write_timeout_sec=0.02),
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
    late_phases = [
        item for item in persisted if item.get("phase") == "late_terminal"
    ]
    assert len(late_phases) == 1
    durable = late_phases[0]
    assert durable["request_id"] == "late-effect-request"
    assert durable["effect_id"] == result.tool_invocations[0]["effect_id"]
    assert durable["observation"]["effect_status"] == "succeeded"
    assert durable["observation"]["changed"] is True
    # The already returned turn remains an honest point-in-time snapshot.
    assert result.tool_invocations[0]["effect_status"] == "indeterminate"


def test_effect_is_not_started_without_its_minimum_admission_window() -> None:
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
                    call_id="effect-with-clipped-window",
                    payload={
                        "name": "create_concepts",
                        "arguments": {
                            "concepts": [{"name": "Must not start late"}]
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response=(
                "The write was not started because its full bounded window "
                "was no longer available."
            )
        ),
    )

    result = execute_adaptive_turn(
        gateway=_effect_gateway(handler, write_timeout_sec=0.2),
        prompt="Represent this concept.",
        context=[],
        llm_client=client,
        model="test-model",
        user_namespace="#V#person@org",
        user_concept_id="#V#person",
        org_concept_id="#V#org",
        turn_id="effect-window-admission",
        turn_budget_seconds=0.2,
        final_synthesis_reserve_seconds=0.15,
        final_answer_reserve_seconds=0.15,
    )

    assert invoked == []
    assert result.terminal_status == "effect_not_started"
    assert result.effect_finality_fallback is True
    assert "1 not_started" in result.response_text
    assert "was not dispatched and reports no change" in result.response_text
    assert "it can be retried in a new turn" in result.response_text
    assert "will not claim that nothing changed" not in result.response_text
    assert "The write was not started" not in result.response_text
    assert result.tool_invocations[0]["effect_status"] == "not_started"
    assert result.tool_invocations[0]["changed"] is False
    assert "insufficient_effect_window" in (
        result.tool_invocations[0]["evidence"]["preview"]
    )
    assert "not_started" in result.tool_invocations[0]["evidence"]["preview"]


def test_research_effect_uses_window_through_protected_answer_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ManualClock()
    observed_deadlines: list[float] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=3.0,
        effect_admission_window_sec=3.0,
    )

    def invoke(
        method_name: str,
        _arguments: dict[str, Any],
        *,
        deadline_monotonic: float,
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
    assert observed_deadlines == [pytest.approx(8.0)]
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
    observed: list[tuple[str, float]] = []
    gateway = _effect_gateway(
        lambda _name, _arguments: {"success": True},
        write_timeout_sec=0.05,
        effect_admission_window_sec=0.05,
    )

    def invoke(
        method_name: str,
        _arguments: dict[str, Any],
        *,
        deadline_monotonic: float,
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
    assert [item[1] for item in observed] == pytest.approx([0.8] * 5)


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

    assert result.terminal_status == "effect_failed"
    assert result.effect_finality_fallback is True
    assert "1 failed" in result.response_text
    assert "The valid effect completed." not in result.response_text
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
    assert result.tool_invocations[1]["result_target_ids"] == [
        "#V#created_if_present"
    ]
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
                        "arguments": {
                            "concepts": [{"name": "Must not be dispatched"}]
                        },
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
                            "subject_concept_id": (
                                "#V#globally_visible_subject"
                            ),
                            "predicate": "hasNote",
                            "target_text": "Actor-relative observation.",
                            "language": "en-NZ",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="The actor-scoped assertion was recorded."
        ),
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
                            "subject_concept_id": (
                                "#V#globally_visible_subject"
                            ),
                            "predicate": "#V#hasResearchInterest",
                            "target_concept_id": "#V#represented_topic",
                        },
                    },
                )
            ],
        ),
        LLMResponse(
            text_response="The actor-scoped relationship was recorded."
        ),
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
    assert recovered_arguments["subject_concept_id"] == (
        "#V#globally_visible_subject"
    )
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
    assert result.response_text == (
        "The actor-scoped relationship was recorded."
    )
    assert denial["recovery_status"] == "succeeded"
    assert denial["recovered_by_effect_id"] == recovery["effect_id"]


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
            "predicate": "#V#hasResearchInterest",
            "target": "Natural-language target",
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
                "value_kind": "text",
            },
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
    ],
)
def test_non_exact_relationship_denial_has_no_scoped_recovery(
    arguments: dict[str, Any],
) -> None:
    denial = _effect_subject_authority_denial(
        capability_name="add_relationship",
        arguments=arguments,
        scoped_assertion_available=True,
    )

    assert denial["error_code"] == "effect_subject_not_authorised"
    assert "recovery_affordances" not in denial


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
    assert all(
        item["query_match"] is False
        for item in unmatched_query["capabilities"]
    )

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
    public_web, internal = web_query["capabilities"]
    assert public_web["surface_family"] == "web"
    assert public_web["external_surface"] is True
    assert internal["surface_family"] == "represented_records"
    assert internal["external_surface"] is False
    assert internal["server_bound_arguments"] == ["actor_id"]
    assert "actor_id" not in internal["input_schema"]["properties"]


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
    assert result["capabilities"][0]["description"] == (
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
    direct = result["capabilities"][0]
    represented = result["capabilities"][1]
    assert direct["plan_profile"]["shape"] == "single_capability"
    assert represented["plan_profile"]["shape"] == "represented_workflow"
    assert represented["semantic_effect"] is False
    assert represented["effect_profile"]["operational_state_effect"] is True
    assert any(
        evidence["source"] == "declared_component_of_matched_workflow"
        for evidence in direct["selection"]["adequacy_evidence"]
    )
    assert result["frontier_total"] == 4
    assert result["dominated_total"] == 0


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
    generic_evidence = by_name[generic_names[0]]["selection"]["adequacy_evidence"]
    assert generic_evidence[0]["common_routing_term_count"] == 1
    assert generic_evidence[0]["informative_routing_term_count"] == 0


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
                        **{
                            f"field_{field}_{index}": str
                            for field in range(30)
                        },
                    },
                    allow_unknown=False,
                ),
                category="read",
                description=(
                    "Search research material. " + ("description " * 30)
                ),
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
    assert len(_json_bytes(first_raw_page)) > 24_000

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
    assert all(
        item.output["next_offset"] == len(item.output["capabilities"])
        for item in paired
    )
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
    schema_references: list[dict[str, Any]] = []
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
        schema_references.extend(
            item
            for item in entries
            if item.get("input_schema_omitted_for_model_context") is True
        )
        next_offset = bounded["next_offset"]
        if next_offset is not None:
            assert next_offset == offset + len(entries)
        offset = next_offset

    assert seen_names == capability_names
    reference = schema_references[0] if schema_references else seen_entries[0]
    assert reference["description"].startswith("Search research material.")
    assert reference["surface_family"] == "research"
    assert reference["evidence_surface_family"] == "research"
    assert reference["external_surface"] is False
    assert reference["server_bound_arguments"] == ["actor_id"]
    if schema_references:
        assert reference["input_schema_hydration"] == {
            "tool": "turn_capabilities",
            "arguments": {
                "names": [reference["name"]],
                "limit": 1,
            },
            "purpose": "dedicated_exact_name_schema_page",
        }
    else:
        assert "input_schema" in reference

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
                    **{
                        f"field_{index}": str
                        for index in range(2_000)
                    },
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
    assert bounded["capability_page_projection"]["returned"] == 1
    assert bounded["capability_page_projection"]["schema_reference_count"] == 1
    compact = bounded["capabilities"][0]
    assert compact["name"] == name
    assert compact["capability_metadata_omitted_for_model_context"] is True
    assert compact["input_schema_omitted_for_model_context"] is True
    assert compact["input_schema_hydration"] == {
        "tool": "turn_capabilities",
        "arguments": {
            "names": [name],
            "limit": 1,
        },
        "purpose": "dedicated_exact_name_schema_page",
    }
    assert compact["semantic_effect"] is False
    assert compact["plan_profile"]["shape"] == "single_capability"
    assert compact["selection"]["frontier_status"] == "candidate"

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
    assert exact_capability[
        "capability_metadata_omitted_for_model_context"
    ] is True
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
    assert included_slices[0]["selector"]["json_pointer"] == (
        "/records/0/summary"
    )
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
    assert "z" * 10_000 not in client.calls[1]["context"][-1]["content"]
    assert len(client.calls[1]["context"][-1]["content"]) < 10_000
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
    assert result.tool_invocations[1]["evidence"]["preview"].find(
        '"created":true'
    ) >= 0


def test_effect_evidence_preserves_success_partial_failure_and_unknown_timeout() -> None:
    seen_cases: list[str] = []
    progress_events: list[dict[str, Any]] = []

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
            time.sleep(0.1)
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
        gateway=_effect_gateway(handler, write_timeout_sec=0.02),
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
    assert all(item.get("evidence", {}).get("evidence_id") for item in result.tool_invocations)
    partial_progress = next(
        event
        for event in progress_events
        if event.get("status") == "tool_completed"
        and event.get("call_id") == "effect-partial"
    )
    assert partial_progress["success"] is False


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
        final_answer_reserve_seconds=0,
        clock=clock,
    )

    assert result.response_text == "A bounded final answer."
    assert len(client.calls) == 2
    assert client.calls[0]["llm_params"]["request_timeout_seconds"] == 8.0
    assert client.calls[1]["llm_params"]["request_timeout_seconds"] == 2.0
    assert {
        tool.name for tool in client.calls[1]["available_tools"]
    } == {"turn_list_evidence", "turn_read_evidence"}
    allocation = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_budget_allocation"
    )
    assert allocation["final_answer_reserve_source"] == "caller"
    assert allocation["explicit_zero_override"] is True
    assert result.llm_calls[1]["mode"] == "evidence_capable"
    recovery = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_research_deadline_recovery"
    )
    assert recovery["action"] == "fresh_final_synthesis_from_available_evidence"


def test_final_answer_checkpoint_preempts_repeated_evidence_cycles() -> None:
    clock = _ManualClock()
    client = _TimedSequenceClient(
        clock,
        (6.0, TimeoutError("research deadline")),
        (
            6.4,
            LLMResponse(
                text_response="A useful draft based on the evidence page.",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="list-before-checkpoint",
                        payload={"offset": 0, "limit": 20},
                    )
                ]
            ),
        ),
        (
            8.0,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="list-at-checkpoint",
                        payload={"offset": 0, "limit": 20},
                    )
                ]
            ),
        ),
        (8.2, LLMResponse(text_response="The protected final answer.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Research, then answer.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-answer-checkpoint",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "The protected final answer."
    assert [call["mode"] for call in result.llm_calls] == [
        "research",
        "evidence_capable",
        "evidence_capable",
        "answer_only",
    ]
    assert [tool.name for tool in client.calls[1]["available_tools"]] == [
        "turn_read_evidence",
        "turn_list_evidence",
    ]
    assert client.calls[-1]["available_tools"] == []
    assert client.calls[-1]["llm_params"]["request_timeout_seconds"] == 2.0
    final_context = client.calls[-1]["context"]
    assert final_context[0] == {
        "role": "assistant",
        "content": "A useful draft based on the evidence page.",
    }
    evidence_contexts = [
        json.loads(item["content"])
        for item in final_context
        if item.get("role") == "user"
        and isinstance(item.get("content"), str)
        and item["content"].startswith("{")
    ]
    assert len(evidence_contexts) == 1
    assert evidence_contexts[0]["evidence_views"][0]["schema_version"] == (
        "adaptive_turn_evidence_index_page.v1"
    )
    assert not any(
        item.get("call_id") == "list-at-checkpoint"
        for item in result.tool_invocations
    )
    checkpoint = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_final_answer_reserve_entered"
    )
    assert checkpoint["reason"] == "late_evidence_result"
    assert checkpoint["effective_answer_reserve_seconds"] == 2.0
    assert checkpoint["reserve_clamped"] is False


def test_final_evidence_failure_recovers_to_answer_only() -> None:
    clock = _ManualClock()
    client = _TimedSequenceClient(
        clock,
        (6.0, TimeoutError("research deadline")),
        (6.5, TimeoutError("evidence-capable final call failed")),
        (6.6, LLMResponse(text_response="Answered from retained evidence.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer despite a final evidence-call failure.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-final-evidence-failure",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "Answered from retained evidence."
    assert client.calls[1]["available_tools"]
    assert client.calls[2]["available_tools"] == []
    assert result.llm_calls[1]["status"] == "failed"
    checkpoint = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_final_answer_reserve_entered"
    )
    assert checkpoint["reason"] == "evidence_call_failed"
    assert checkpoint["remaining_ms"] == 3_500.0


def test_final_evidence_overrun_records_actual_remaining_answer_time() -> None:
    clock = _ManualClock()
    client = _TimedSequenceClient(
        clock,
        (6.0, TimeoutError("research deadline")),
        (
            9.5,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="late-list",
                        payload={"offset": 0, "limit": 20},
                    )
                ],
            ),
        ),
        (9.6, LLMResponse(text_response="Answered in the actual time left.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Finish from the available evidence.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-final-evidence-overrun",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "Answered in the actual time left."
    assert client.calls[-1]["llm_params"]["request_timeout_seconds"] == 0.5
    checkpoint = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_final_answer_reserve_entered"
    )
    assert checkpoint["reason"] == "late_evidence_result"
    assert checkpoint["remaining_ms"] == 500.0
    assert result.llm_calls[1]["status"] == "late_result_discarded"


def test_small_final_reserve_is_a_recorded_answer_only_checkpoint() -> None:
    clock = _ManualClock()
    client = _TimedSequenceClient(
        clock,
        (8.0, TimeoutError("research model deadline")),
        (8.5, LLMResponse(text_response="A bounded answer.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Answer within the caller's small reserve.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-small-final-reserve",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        final_answer_reserve_seconds=20,
        clock=clock,
    )

    assert result.response_text == "A bounded answer."
    assert len(client.calls) == 2
    assert client.calls[1]["available_tools"] == []
    assert client.calls[1]["llm_params"]["request_timeout_seconds"] == 2.0
    checkpoint = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_final_answer_reserve_entered"
    )
    assert checkpoint["reason"] == "direct_final_entry"
    assert checkpoint["requested_answer_reserve_seconds"] == 20.0
    assert checkpoint["effective_answer_reserve_seconds"] == 2.0
    assert checkpoint["evidence_capable_reserve_seconds"] == 0.0
    assert checkpoint["reserve_clamped"] is True


def test_answer_checkpoint_can_be_enabled_from_candidate_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VON_ADAPTIVE_TURN_FINAL_ANSWER_RESERVE_SEC",
        "1.5",
    )
    clock = _ManualClock()
    client = _TimedSequenceClient(
        clock,
        (8.0, TimeoutError("research model deadline")),
        (8.5, LLMResponse(text_response="A late evidence-capable draft.")),
        (8.6, LLMResponse(text_response="The candidate answer.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Exercise the candidate allocation seam.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-env-answer-checkpoint",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "The candidate answer."
    assert client.calls[1]["llm_params"]["request_timeout_seconds"] == 0.5
    assert {
        tool.name for tool in client.calls[1]["available_tools"]
    } == {"turn_list_evidence", "turn_read_evidence"}
    assert client.calls[2]["available_tools"] == []
    checkpoint = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_final_answer_reserve_entered"
    )
    assert checkpoint["effective_answer_reserve_seconds"] == 1.5
    assert checkpoint["reserve_clamped"] is False


def test_final_evidence_tools_remain_available_before_answer_checkpoint() -> None:
    clock = _ManualClock()
    client = _TimedSequenceClient(
        clock,
        (6.0, TimeoutError("research deadline")),
        (
            6.5,
            LLMResponse(
                text_response="",
                tool_calls=[
                    ToolCall(
                        tool_name="turn_list_evidence",
                        call_id="list-final-evidence",
                        payload={"offset": 0, "limit": 20},
                    )
                ]
            ),
        ),
        (6.6, LLMResponse(text_response="Answered before the checkpoint.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Use final evidence only if it helps.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-final-evidence-window",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=2,
        clock=clock,
    )

    assert result.response_text == "Answered before the checkpoint."
    assert {
        tool.name for tool in client.calls[1]["available_tools"]
    } == {"turn_list_evidence", "turn_read_evidence"}
    assert result.tool_invocations[0]["call_id"] == "list-final-evidence"
    assert not any(
        item.get("type") == "adaptive_turn_final_answer_reserve_entered"
        for item in result.aux_llm_calls
    )


def test_answer_checkpoint_can_be_disabled_for_an_adaptive_caller() -> None:
    clock = _ManualClock()
    client = _TimedSequenceClient(
        clock,
        (6.0, TimeoutError("research deadline")),
        (9.0, LLMResponse(text_response="The caller retained adaptive time.")),
    )

    result = execute_adaptive_turn(
        gateway=None,
        prompt="Use the final interval adaptively.",
        context=[],
        llm_client=client,
        model="test-model",
        turn_id="turn-disabled-answer-checkpoint",
        turn_budget_seconds=10,
        final_synthesis_reserve_seconds=4,
        final_answer_reserve_seconds=0,
        clock=clock,
    )

    assert result.response_text == "The caller retained adaptive time."
    assert {
        tool.name for tool in client.calls[1]["available_tools"]
    } == {"turn_list_evidence", "turn_read_evidence"}
    assert client.calls[1]["llm_params"]["request_timeout_seconds"] == 4.0
    assert not any(
        item.get("type") == "adaptive_turn_final_answer_reserve_entered"
        for item in result.aux_llm_calls
    )


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
                    tool_name="turn_invoke_capability",
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
        final_answer_reserve_seconds=0,
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
    assert evidence_payload["evidence_views"][0]["call_id"] == (
        "call-final-evidence"
    )
    assert "usable evidence" in evidence_payload["evidence_views"][0]["preview"]
    assert evidence_payload["evidence_view_projection"] == {
        "schema_version": "adaptive_turn_evidence_view_projection.v1",
        "order": "content_bearing_evidence_slices_first_stable_within_class",
        "deduplication": "exact_canonical_output",
        "total_count": 1,
        "included_count": 1,
        "omitted_count": 0,
        "read_tool": "turn_read_evidence",
        "list_tool": "turn_list_evidence",
    }
    assert raw_tail not in evidence_context["content"]
    assert len(evidence_context["content"]) < 10_000


@pytest.mark.parametrize(
    "native_continuation",
    [True, False],
    ids=["native-continuation", "stateless-context"],
)
def test_final_reset_carries_exact_hydrated_evidence_for_provider_styles(
    native_continuation: bool,
) -> None:
    started = time.monotonic()
    research_deadline = started + 8.0
    clock = _ManualClock(started)
    client = _HydrationDeadlineClient(
        clock,
        native_continuation=native_continuation,
        research_deadline=research_deadline,
    )
    raw_tail = "z" * 50_000

    result = execute_adaptive_turn(
        gateway=_gateway(
            lambda **_kwargs: {
                "success": True,
                "record": {
                    "a_raw": raw_tail,
                    "z_summary": (
                        "The specifically hydrated result survives a fresh request."
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
    assert len(client.calls) == 4
    assert len(client.received_evidence_outputs) == 2
    delivered_envelope, delivered_slice = client.received_evidence_outputs
    assert delivered_envelope["schema_version"] == "turn_evidence_envelope.v1"
    assert delivered_slice["schema_version"] == "turn_evidence_slice.v1"
    assert delivered_slice["content"] == (
        "The specifically hydrated result survives a fresh request."
    )

    final_call = client.calls[-1]
    assert "continuation" not in final_call
    assert "tool_results" not in final_call
    assert {
        tool.name for tool in final_call["available_tools"]
    } == {"turn_list_evidence", "turn_read_evidence"}
    assert len(final_call["context"]) == 1
    evidence_message = final_call["context"][0]
    assert evidence_message["role"] == "user"
    assert "z" * 5_000 not in evidence_message["content"]
    assert len(_json_bytes(evidence_message)) <= 24_000
    payload = json.loads(evidence_message["content"])
    assert payload["evidence_views"] == [
        delivered_slice,
        delivered_envelope,
    ]
    retained_slice = payload["evidence_views"][0]
    assert retained_slice["selector"] == {
        "json_pointer": "/record/z_summary",
        "offset": 0,
        "max_chars": 4_000,
    }
    assert retained_slice["source_sha256"] == delivered_envelope["sha256"]
    assert retained_slice["provenance"] == delivered_envelope["provenance"]
    assert payload["evidence_view_projection"]["total_count"] == 2
    assert payload["evidence_view_projection"]["omitted_count"] == 0


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
    workflow_capability = WorkflowTurnCapability(
        name="represented_workflow_turn_test",
        workflow_id="#V#represented_test_workflow",
        display_name="Represented test workflow",
        description="Produce the represented test work product.",
        relevance_score=0.94,
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
        }

    gateway = _workflow_gateway(_execute_workflow)
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
    )

    catalogue_result = client.calls[1]["tool_results"][0].output
    assert catalogue_result["represented_workflow_total"] == 1
    assert catalogue_result["capabilities"][0]["kind"] == ("represented_workflow")
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
    assert invocation["represented_workflow_id"] == (
        "#V#represented_test_workflow"
    )
    assert invocation["effect_status"] == "succeeded"
    assert invocation["changed"] is True
    assert invocation["instance_id"] == "workflow-instance-1"
    assert invocation["workflow_id"] == "#V#represented_test_workflow"
    assert invocation["plan_profile"]["shape"] == "represented_workflow"
    selection_trace = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "adaptive_turn_capability_selection"
    )
    assert selection_trace["capability_name"] == workflow_capability.name
    assert selection_trace["selection_policy"]["representedness_priority"] is False
    assert selection_trace["plan_profile"]["shape"] == ("represented_workflow")
    assert result.response_text == "The represented work product was completed."


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


def test_unchanged_terminally_failed_effect_is_not_dispatched_twice(
    monkeypatch,
) -> None:
    handler_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.services.adaptive_turn_service."
        "_effect_subject_authorised",
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
    assert result.tool_invocations[0]["error_code"] == (
        "invalid_predicate_format"
    )
    assert result.tool_invocations[1]["error_code"] == (
        "effect_request_unchanged_after_terminal_failure"
    )
    assert result.tool_invocations[1]["effect_status"] == "not_started"
    assert result.tool_invocations[1]["changed"] is False
