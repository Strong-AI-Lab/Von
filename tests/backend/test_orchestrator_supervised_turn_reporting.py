from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.conversation_turn_llm_timeout import (
    DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC,
)
from src.backend.workflows import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowRegistration,
    WorkflowStateSpec,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from orchestrator_test_harness import build_db_independent_orchestrator


class _DummyLLM:
    def generate(self, prompt, context=None, model=None):  # pragma: no cover
        raise AssertionError("LLM should not be called in this regression test")


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


def _stub_base_system_prompt(monkeypatch, orchestrator: InternalMCPChatOrchestrator) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: ("Test base system prompt", "#V#test_base_prompt"),
    )


def test_supervised_turn_preserves_gate_reported_response_when_follow_up_is_required(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)
    expected_response = (
        "Execution status: mutation may have run but verification is inconclusive."
    )
    monkeypatch.setattr(
        orchestrator,
        "execute_workflow",
        lambda *args, **kwargs: SimpleNamespace(
            completed=False,
            final_state="failed",
            error="follow_up_required",
            data={
                "response_text": expected_response,
                "completion_gate_decision": "partial",
                "completion_gate_requires_follow_up": True,
                "completion_gate_safe_to_claim_completion": False,
                "completion_gate_evidence_payload": {
                    "terminal_outcome": "attempt_budget_exhausted"
                },
            },
        ),
    )

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Represent this paper.",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
    )

    assert result.response_text == expected_response
    assert result.completion_gate_verdict is not None
    assert result.completion_gate_verdict.get("decision") == "partial"
    assert result.completion_gate_verdict.get("requires_follow_up") is True


def test_supervised_turn_still_fails_closed_without_gate_reported_response(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)
    monkeypatch.setattr(
        orchestrator,
        "execute_workflow",
        lambda *args, **kwargs: SimpleNamespace(
            completed=False,
            final_state="failed",
            error="workflow_failed",
            data={},
        ),
    )

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Represent this paper.",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
    )

    assert (
        result.response_text
        == "I couldn't complete that request because the authoritative "
        "conversation-turn workflow failed."
    )


def test_supervised_turn_leaves_missing_discovery_unset_for_workflow_owned_routing(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)
    captured: dict[str, Any] = {}

    def _execute_workflow(*args, **kwargs):
        captured["data"] = kwargs.get("data")
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={"response_text": "Done.", "completion_report": {"completed": True}},
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="https://example.com/resource",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
    )

    workflow_data = captured.get("data")
    assert isinstance(workflow_data, dict)
    assert workflow_data.get("workflow_discovery_result") is None
    assert workflow_data.get("workflow_discovery") is None
    assert result.response_text == "Done."


def test_turn_execution_route_discovers_when_prefilled_payload_is_empty(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=False,
    )
    discovered_queries: list[str] = []

    def _discover_workflows_for_turn(
        user_input: str,
        *,
        namespace: str | None = None,
        workflow_registry: Any | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        discovered_queries.append(user_input)
        assert namespace == "#V#user"
        assert workflow_registry is orchestrator._workflow_registry
        return {
            "query": user_input,
            "requested_query": user_input,
            "search_sources": ["capability_index"],
            "candidate_count": 1,
            "match_count": 1,
            "matches": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "Default chat assistant route.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "candidate_source": "capability_index",
                }
            ],
            "candidates": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "Default chat assistant route.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "candidate_source": "capability_index",
                }
            ],
        }

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        _discover_workflows_for_turn,
    )

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "https://example.com/resource",
                "workflow_discovery_result": {},
                "workflow_discovery": None,
                "policy_state": None,
                "registry_snapshot": None,
                "llm_calls": [],
                "aux_llm_calls": [],
            },
            environment=SimpleNamespace(
                user_namespace="#V#user",
                llm_client=_DummyLLM(),
                model="test-model",
            ),
        )
    )

    assert discovered_queries == ["https://example.com/resource"]
    assert result.status == "success"
    assert result.outputs["workflow_discovery_result"]["requested_query"] == (
        "https://example.com/resource"
    )
    assert result.outputs["workflow_discovery_result"]["discovery_query_input"] == (
        "https://example.com/resource"
    )
    assert (
        CHAT_ASSISTANT_WORKFLOW_ID
        in result.outputs["selected_workflow_trace"]["selector_candidate_ids"]
    )


def test_turn_execution_route_refreshes_prefilled_discovery_when_effective_query_changes(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    refreshed_workflow_id = "#V#concept_search_instance_retrieval_workflow"
    discovered_queries: list[str] = []

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=refreshed_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=refreshed_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_custom"),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "retrieval",
                        "explicit_workflow_context_required": False,
                    }
                },
            ),
            purpose="Retrieve represented facts about a specific concept or instance.",
            source="test",
        )
    )

    def _discover_workflows_for_turn(
        user_input: str,
        *,
        namespace: str | None = None,
        workflow_registry: Any | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        discovered_queries.append(user_input)
        assert namespace == "#V#user"
        assert workflow_registry is orchestrator._workflow_registry
        assert user_input == (
            "What papers of mine do you know about?\n\n"
            "Turn-intent routing guidance:\n"
            "- Routing guidance: Treat this as concept/relation retrieval for the "
            "referenced entity rather than artefact creation or representation.\n"
            "- Grounding requirement: Ground authorship or ownership claims in "
            "represented concept relations.\n"
            "- Success target: Answer only with represented facts grounded to the "
            "referenced user concept."
        )
        return {
            "query": user_input,
            "requested_query": "stale query",
            "search_sources": ["capability_index"],
            "candidate_count": 1,
            "match_count": 1,
            "matches": [
                {
                    "concept_id": refreshed_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": (
                        "Retrieve represented facts about a specific concept or "
                        "instance from the Vontology."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "retrieval"},
                    "candidate_source": "capability_index",
                }
            ],
            "candidates": [
                {
                    "concept_id": refreshed_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": (
                        "Retrieve represented facts about a specific concept or "
                        "instance from the Vontology."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "retrieval"},
                    "candidate_source": "capability_index",
                }
            ],
        }

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        _discover_workflows_for_turn,
    )

    class _CapturingRouteLLM:
        def generate(self, prompt, context=None, model=None):
            if prompt == "Select workflow":
                return refreshed_workflow_id
            raise AssertionError(f"Unexpected selector prompt: {prompt!r}")

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "What papers of mine do you know about?",
                "workflow_discovery_result": {
                    "requested_query": "What papers of mine do you know about?",
                    "query": (
                        "What papers of mine do you know about?\n\n"
                        "Turn-intent routing guidance:\n"
                        "- Routing guidance: Treat this as concept/relation retrieval."
                    ),
                    "discovery_query_input": (
                        "What papers of mine do you know about?\n\n"
                        "Turn-intent routing guidance:\n"
                        "- Routing guidance: Treat this as concept/relation retrieval."
                    ),
                    "search_sources": ["capability_index"],
                    "candidate_count": 1,
                    "match_count": 1,
                    "matches": [
                        {
                            "concept_id": "#V#scholarly_paper_representation_workflow",
                            "name": "Scholarly Paper Representation Workflow",
                            "description": (
                                "Canonical durable workflow for representing scholarly "
                                "papers from file-copy artefacts, metadata, and "
                                "verification requirements."
                            ),
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "is_policy_safe": True,
                            "routing_eligible": True,
                            "routing_profile": {"role": "execution"},
                            "candidate_source": "capability_index",
                        }
                    ],
                    "candidates": [
                        {
                            "concept_id": "#V#scholarly_paper_representation_workflow",
                            "name": "Scholarly Paper Representation Workflow",
                            "description": (
                                "Canonical durable workflow for representing scholarly "
                                "papers from file-copy artefacts, metadata, and "
                                "verification requirements."
                            ),
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "is_policy_safe": True,
                            "routing_eligible": True,
                            "routing_profile": {"role": "execution"},
                            "candidate_source": "capability_index",
                        }
                    ],
                },
                "turn_expected_outcome_summary": (
                    "Answer only with represented facts grounded to the referenced "
                    "user concept."
                ),
                "turn_expected_grounding_requirement": (
                    "Ground authorship or ownership claims in represented concept "
                    "relations."
                ),
                "turn_expected_precision_policy": (
                    "Prefer explicit uncertainty over speculative recall."
                ),
                "turn_selector_guidance": (
                    "Treat this as concept/relation retrieval for the referenced "
                    "entity rather than artefact creation or representation."
                ),
                "turn_expected_outcome_reasoning": (
                    "The request asks what is already known about an entity and its "
                    "related artefacts, so concept/relation retrieval should outrank "
                    "representation workflows."
                ),
                "workflow_discovery": None,
                "policy_state": SimpleNamespace(
                    enabled=False,
                    policy=None,
                    policy_id=None,
                    predicate_id=None,
                    errors=(),
                ),
                "registry_snapshot": None,
                "llm_calls": [],
                "aux_llm_calls": [],
                "augmented_context": [
                    {
                        "role": "system",
                        "content": "CURRENT USER CONTEXT: Test User (#V#test_user)",
                    },
                    {
                        "role": "user",
                        "content": "What papers of mine do you know about?",
                    },
                ],
            },
            environment=SimpleNamespace(
                user_namespace="#V#user",
                llm_client=_CapturingRouteLLM(),
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert len(discovered_queries) == 1
    assert result.outputs["selected_workflow_id"] == refreshed_workflow_id
    refreshed_discovery = result.outputs["workflow_discovery_result"]
    assert refreshed_discovery["requested_query"] == (
        "What papers of mine do you know about?"
    )
    assert refreshed_discovery["discovery_query_input"] == (
        "What papers of mine do you know about?\n\n"
        "Turn-intent routing guidance:\n"
        "- Routing guidance: Treat this as concept/relation retrieval for the "
        "referenced entity rather than artefact creation or representation.\n"
        "- Grounding requirement: Ground authorship or ownership claims in "
        "represented concept relations.\n"
        "- Success target: Answer only with represented facts grounded to the "
        "referenced user concept."
    )
    assert refreshed_discovery["query"] == discovered_queries[0]
    assert refreshed_discovery.get("query_enrichment_applied") is True
    assert refreshed_discovery["discovery_refreshed"] is True
    assert refreshed_discovery["discovery_refresh_reason"] == (
        "effective_query_changed"
    )
    assert (
        refreshed_workflow_id
        in result.outputs["selected_workflow_trace"]["selector_candidate_ids"]
    )
    assert (
        "#V#scholarly_paper_representation_workflow"
        not in result.outputs["selected_workflow_trace"]["selector_candidate_ids"]
    )


def test_prepare_selector_context_emits_prompt_and_grounding_contract(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    aux_llm_calls: list[dict[str, Any]] = []

    result = orchestrator._action_turn_execution_prepare_selector_context(
        SimpleNamespace(
            data={
                "user_prompt": "What papers of mine do you know about?",
                "workflow_discovery_result": {
                    "matches": [
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default chat assistant route.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "routing_eligible": True,
                        }
                    ],
                    "candidates": [
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default chat assistant route.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "routing_eligible": True,
                        }
                    ],
                    "match_count": 1,
                },
                "turn_expected_outcome_summary": (
                    "Answer only with papers that can be grounded to the user."
                ),
                "turn_expected_grounding_requirement": (
                    "Only mention papers when authorship or ownership is grounded."
                ),
                "turn_expected_precision_policy": (
                    "Prefer omission or explicit uncertainty over speculative recall."
                ),
                "turn_selector_guidance": (
                    "Prefer retrieval or verification routes when the current context is insufficient."
                ),
                "augmented_context": [
                    {
                        "role": "system",
                        "content": "CURRENT USER CONTEXT: Test User (#V#test_user)",
                    },
                    {
                        "role": "user",
                        "content": "What papers of mine do you know about?",
                    },
                ],
                "aux_llm_calls": aux_llm_calls,
            },
            environment=SimpleNamespace(
                user_namespace="#V#user",
                llm_client=_DummyLLM(),
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert result.outputs["selector_prompt_available"] is True
    selector_context = result.outputs["selector_context_messages"]
    assert any(
        isinstance(message, dict)
        and "Expected answer contract for this turn"
        in str(message.get("content") or "")
        for message in selector_context
    )
    assert any(
        isinstance(message, dict)
        and "#V#chat_assistant_workflow" in str(message.get("content") or "")
        for message in selector_context
    )
    lineage = result.outputs["selector_context_lineage"]
    assert lineage["base_context_source"] == "augmented_context"
    assert lineage["stage_added_message_count"] == 3
    assert any(
        isinstance(message, dict)
        and "Current turn request to route" in str(message.get("content_preview") or "")
        for message in (lineage.get("stage_added_messages") or [])
        if isinstance(message, dict)
    )
    prepare_entry = next(
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_dispatch_prepare_step"
    )
    assert prepare_entry["step_id"] == "selector_candidate_preparation"
    prompt_entry = next(
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_prompt"
    )
    assert prompt_entry["stage"] == "selector_preparation"
    assert prompt_entry["prompt_id"] == "#V#chat_turn_classifier_prompt"
    assert "#V#chat_assistant_workflow" in (
        ((prompt_entry.get("candidate_list") or {}).get("text")) or ""
    )


def test_turn_execution_route_reuses_augmented_context_for_selector_and_tracks_lineage(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    llm_calls: list[dict[str, Any]] = []

    class _CapturingRouteLLM:
        def generate(self, prompt, context=None, model=None):
            llm_calls.append(
                {"prompt": prompt, "context": list(context or []), "model": model}
            )
            return CHAT_ASSISTANT_WORKFLOW_ID

    aux_llm_calls: list[dict[str, Any]] = []
    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "What is my name?",
                "workflow_discovery_result": {
                    "matches": [
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default chat assistant route.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "routing_eligible": True,
                        }
                    ],
                    "candidates": [
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default chat assistant route.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "routing_eligible": True,
                        }
                    ],
                    "match_count": 1,
                },
                "workflow_discovery": None,
                "policy_state": SimpleNamespace(
                    enabled=False,
                    policy=None,
                    policy_id=None,
                    predicate_id=None,
                    errors=(),
                ),
                "registry_snapshot": None,
                "llm_calls": [],
                "aux_llm_calls": aux_llm_calls,
                "augmented_context": [
                    {
                        "role": "system",
                        "content": "CURRENT USER CONTEXT: Test User (#V#test_user)",
                    },
                    {"role": "user", "content": "What is my name?"},
                ],
            },
            environment=SimpleNamespace(
                user_namespace="#V#user",
                llm_client=_CapturingRouteLLM(),
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert result.outputs["selected_workflow_id"] == CHAT_ASSISTANT_WORKFLOW_ID
    assert llm_calls
    selector_context = llm_calls[0]["context"]
    assert any(
        isinstance(message, dict)
        and message.get("content") == "CURRENT USER CONTEXT: Test User (#V#test_user)"
        for message in selector_context
    )
    assert any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and message.get("content") == "What is my name?"
        for message in selector_context
    )
    assert any(
        isinstance(message, dict)
        and "Current turn request to route" in str(message.get("content") or "")
        for message in selector_context
    )

    stage_summary = next(
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_model_policy_stage"
        and entry.get("stage") == "workflow_dispatch"
    )
    request = stage_summary["request"]
    assert request["context_lineage"]["base_context_source"] == "augmented_context"
    assert request["context_lineage"]["stage_added_message_count"] == 2
    assert request["context_summary"]["message_count"] == len(selector_context)
    assert (
        result.outputs["selected_workflow_trace"]["selector_context_lineage"][
            "base_context_source"
        ]
        == "augmented_context"
    )


def test_supervised_turn_propagates_completion_gate_retry_budget(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)
    orchestrator._completion_gate_loop_max_attempts = 3
    captured: dict[str, Any] = {}

    def _execute_workflow(*args, **kwargs):
        captured["data"] = kwargs.get("data")
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={"response_text": "Done.", "completion_report": {"completed": True}},
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Represent this paper.",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
    )

    workflow_data = captured.get("data")
    assert isinstance(workflow_data, dict)
    assert workflow_data.get("completion_gate_loop_max_attempts") == 3
    assert result.response_text == "Done."


def test_supervised_turn_seeds_conversation_turn_llm_timeout_override(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)
    captured: dict[str, Any] = {}

    def _execute_workflow(*args, **kwargs):
        captured["data"] = kwargs.get("data")
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={"response_text": "Done.", "completion_report": {"completed": True}},
        )

    monkeypatch.delenv("VON_CONVERSATION_TURN_LLM_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Represent this paper.",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
    )

    workflow_data = captured.get("data")
    assert isinstance(workflow_data, dict)
    assert (
        workflow_data.get("conversation_turn_llm_timeout_override_sec")
        == DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC
    )
    assert result.response_text == "Done."


def test_supervised_turn_seeds_requested_model_for_workflow_llm_steps(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)
    captured: dict[str, Any] = {}

    def _execute_workflow(*args, **kwargs):
        captured["data"] = kwargs.get("data")
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={"response_text": "Done.", "completion_report": {"completed": True}},
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Find recent open-source projects.",
        context=None,
        llm_client=_DummyLLM(),
        model="gemma4:26b",
    )

    workflow_data = captured.get("data")
    assert isinstance(workflow_data, dict)
    assert workflow_data.get("requested_model") == "gemma4:26b"
    assert result.response_text == "Done."


def test_supervised_turn_surfaces_workflow_tool_outputs(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)

    def _execute_workflow(*args, **kwargs):
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={
                "response_text": "Done.",
                "tool_messages": [
                    {"role": "tool", "content": "KB lookup completed."},
                ],
                "invocations": [
                    {
                        "tool": "search_knowledge_base",
                        "arguments": {"query": "research themes"},
                    }
                ],
                "completion_report": {"completed": True},
            },
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Find relevant open-source projects.",
        context=None,
        llm_client=_DummyLLM(),
        model="gemma4:26b",
    )

    assert result.response_text == "Done."
    assert list(result.tool_invocations) == [
        {
            "tool": "search_knowledge_base",
            "arguments": {"query": "research themes"},
        }
    ]
    assert list(result.extra_messages) == [
        {"role": "tool", "content": "KB lookup completed."}
    ]


def test_turn_execution_route_uses_prepared_selector_response_without_extra_llm_call(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )

    def _raise_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("selector LLM should not be called on prepared route data")

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "Who am I?",
                "workflow_discovery_result": {
                    "matches": [
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default chat assistant route.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "routing_eligible": True,
                        }
                    ],
                    "candidates": [
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default chat assistant route.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "routing_eligible": True,
                        }
                    ],
                    "match_count": 1,
                },
                "selector_prompt_available": True,
                "selector_prompt_id": "#V#chat_turn_classifier_prompt",
                "selector_prompt_text": "Selector system prompt",
                "selector_call_prompt_text": "Select workflow",
                "selector_requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                "selector_prompt_provenance": {
                    "render_variables": {
                        "candidate_list": "- #V#chat_assistant_workflow: Chat Assistant Workflow"
                    }
                },
                "selector_candidate_entries": [
                    {
                        "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                        "name": "Chat Assistant Workflow",
                        "description": "Default chat assistant route.",
                        "candidate_source": "workflow_discovery",
                    }
                ],
                "selector_candidate_ids": [CHAT_ASSISTANT_WORKFLOW_ID],
                "selector_excluded_candidate_entries": [],
                "selector_excluded_candidate_ids": [],
                "selector_discovered_workflow_ids": [CHAT_ASSISTANT_WORKFLOW_ID],
                "selector_context_messages": [
                    {"role": "system", "content": "Selector system prompt"},
                    {"role": "user", "content": "Who am I?"},
                ],
                "selector_context_lineage": {
                    "stage": "selector_decision",
                    "base_context_source": "augmented_context",
                    "stage_added_message_count": 1,
                },
                "selector_raw_response": CHAT_ASSISTANT_WORKFLOW_ID,
                "policy_state": None,
                "registry_snapshot": None,
                "llm_calls": [],
                "aux_llm_calls": [],
                "augmented_context": [
                    {"role": "user", "content": "Who am I?"},
                ],
            },
            environment=SimpleNamespace(
                user_namespace="#V#user",
                llm_client=SimpleNamespace(generate=_raise_if_called),
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert result.outputs["selected_workflow_id"] == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.outputs["selected_workflow_trace"]["selector_prompt_id"] == (
        "#V#chat_turn_classifier_prompt"
    )


def test_turn_execution_route_passes_conversation_turn_timeout_override_to_selector_llm(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    captured: dict[str, Any] = {}

    def _capture_timeout_override(**kwargs: Any):
        captured["timeout_override_sec"] = kwargs.get("timeout_override_sec")
        return CHAT_ASSISTANT_WORKFLOW_ID, "test-model", None

    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_fallbacks",
        _capture_timeout_override,
    )

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "Who am I?",
                "workflow_discovery_result": {
                    "matches": [
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default chat assistant route.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "routing_eligible": True,
                        }
                    ],
                    "candidates": [
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default chat assistant route.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "routing_eligible": True,
                        }
                    ],
                    "match_count": 1,
                },
                "workflow_discovery": None,
                "conversation_turn_llm_timeout_override_sec": 29,
                "policy_state": SimpleNamespace(
                    enabled=False,
                    policy=None,
                    policy_id=None,
                    predicate_id=None,
                    errors=(),
                ),
                "registry_snapshot": None,
                "llm_calls": [],
                "aux_llm_calls": [],
                "augmented_context": [
                    {"role": "user", "content": "Who am I?"},
                ],
            },
            environment=SimpleNamespace(
                user_namespace="#V#user",
                llm_client=_DummyLLM(),
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert captured["timeout_override_sec"] == 29.0


def test_turn_execution_route_preserves_non_default_selector_intent_with_safe_general_fallback(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    excluded_workflow_id = "#V#specialised_vontology_search_workflow"
    aux_llm_calls: list[dict[str, Any]] = []

    llm = SimpleNamespace(
        generate=lambda prompt, context=None, model=None: (
            '{"workflow_id":"#V#specialised_vontology_search_workflow",'
            '"confidence":0.93,'
            '"reasoning":"The specialised workflow best matches this request."}'
        )
    )

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "Look up the represented student concept and inspect its relations.",
                "workflow_discovery_result": {
                    "matches": [
                        {
                            "concept_id": excluded_workflow_id,
                            "name": "Specialised Vontology Search Workflow",
                            "description": "Inspect represented concepts and relations.",
                            "routing_eligible": False,
                            "routing_exclusion_reason": "missing_authoritative_purpose",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                        }
                    ],
                    "candidates": [
                        {
                            "concept_id": excluded_workflow_id,
                            "name": "Specialised Vontology Search Workflow",
                            "description": "Inspect represented concepts and relations.",
                            "routing_eligible": False,
                            "routing_exclusion_reason": "missing_authoritative_purpose",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                        }
                    ],
                    "match_count": 1,
                },
                "workflow_discovery": None,
                "policy_state": SimpleNamespace(
                    enabled=False,
                    policy=None,
                    policy_id=None,
                    predicate_id=None,
                    errors=(),
                ),
                "registry_snapshot": None,
                "llm_calls": [],
                "aux_llm_calls": aux_llm_calls,
            },
            environment=SimpleNamespace(
                user_namespace="#V#user",
                llm_client=llm,
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert result.outputs["selected_workflow_id"] != CHAT_ASSISTANT_WORKFLOW_ID
    workflow_routing = result.outputs["workflow_routing"]
    assert workflow_routing["workflow_id"] == result.outputs["selected_workflow_id"]
    assert workflow_routing["verdict"] in {"tool_contract_override", "rag_selected"}
    assert workflow_routing["source"] in {"selector_override", "selector"}
    selection_metadata = result.outputs["selected_workflow_trace"][
        "selector_selection_metadata"
    ]
    unmatched_candidate_workflow_id = selection_metadata.get(
        "unmatched_candidate_workflow_id"
    )
    if unmatched_candidate_workflow_id is not None:
        assert unmatched_candidate_workflow_id == excluded_workflow_id

    override_entries = [
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selector_unmatched_candidate_requires_safe_general_fallback"
    ]
    if override_entries:
        override_entry = override_entries[0]
        assert (
            override_entry["selected_workflow_id"]
            == result.outputs["selected_workflow_id"]
        )
        assert override_entry["requested_candidate_workflow_id"] == excluded_workflow_id


def test_turn_execution_route_recovers_single_discovered_execution_workflow_after_selector_default(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"
    aux_llm_calls: list[dict[str, Any]] = []

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_custom"),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "execution",
                        "explicit_workflow_context_required": False,
                    }
                },
            ),
            purpose="Represent an arXiv paper from a bare URL or arXiv identifier.",
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=testing_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=testing_workflow_id,
                initial_state="prepare_fixture",
                states={
                    "prepare_fixture": WorkflowStateSpec(
                        state_id="prepare_fixture",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "explicit_workflow_context_required": True,
                    }
                },
            ),
            purpose="Run the arXiv ingestion workflow as a testing/verification task.",
            source="test",
        )
    )

    llm = SimpleNamespace(
        generate=lambda prompt, context=None, model=None: (
            "I'm not sure which workflow you'd like me to select from the "
            "provided candidates."
        )
    )

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "https://arxiv.org/abs/2501.00663",
                "workflow_discovery_result": {
                    "requested_query": "https://arxiv.org/abs/2501.00663",
                    "query": "https://arxiv.org/abs/2501.00663",
                    "discovery_query_input": "https://arxiv.org/abs/2501.00663",
                    "matches": [
                        {
                            "concept_id": selected_workflow_id,
                            "name": "Arxiv Paper Representation Workflow",
                            "description": (
                                "Represent an arXiv paper from a bare URL or "
                                "arXiv identifier."
                            ),
                            "routing_eligible": True,
                            "is_executable": True,
                            "is_policy_safe": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "execution"},
                        },
                        {
                            "concept_id": testing_workflow_id,
                            "name": "Arxiv Paper Ingestion Testing Workflow",
                            "description": "Run the arXiv ingestion workflow as a testing task.",
                            "routing_eligible": True,
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "testing"},
                        },
                    ],
                    "candidates": [
                        {
                            "concept_id": selected_workflow_id,
                            "name": "Arxiv Paper Representation Workflow",
                            "description": (
                                "Represent an arXiv paper from a bare URL or "
                                "arXiv identifier."
                            ),
                            "routing_eligible": True,
                            "is_executable": True,
                            "is_policy_safe": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "execution"},
                        },
                        {
                            "concept_id": testing_workflow_id,
                            "name": "Arxiv Paper Ingestion Testing Workflow",
                            "description": "Run the arXiv ingestion workflow as a testing task.",
                            "routing_eligible": True,
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "testing"},
                        },
                    ],
                    "match_count": 2,
                },
                "workflow_discovery": None,
                "policy_state": SimpleNamespace(
                    enabled=False,
                    policy=None,
                    policy_id=None,
                    predicate_id=None,
                    errors=(),
                ),
                "registry_snapshot": None,
                "llm_calls": [],
                "aux_llm_calls": aux_llm_calls,
            },
            environment=SimpleNamespace(
                user_namespace="#V#user",
                llm_client=llm,
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert result.outputs["selected_workflow_id"] == selected_workflow_id
    assert result.outputs["workflow_routing"]["workflow_id"] == selected_workflow_id
    assert result.outputs["workflow_routing"]["verdict"] == "rag_selected"
    assert result.outputs["workflow_routing"]["source"] == "selector"
    assert (
        result.outputs["selected_workflow_trace"]["selector_selection_metadata"][
            "selection_resolution"
        ]
        == "single_specialised_candidate_recovery_from_selector_fallback"
    )
    assert (
        result.outputs["selected_workflow_trace"]["selector_selection_metadata"][
            "selection_resolution_prior"
        ]
        == "default_workflow_fallback"
    )
    assert (
        result.outputs["selected_workflow_trace"]["selector_selection_metadata"][
            "recovered_candidate_workflow_id"
        ]
        == selected_workflow_id
    )
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
        for entry in aux_llm_calls
    )


def test_execute_selected_promotes_child_result_snapshot_into_completion_report(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    monkeypatch.setattr(
        orchestrator,
        "execute_workflow",
        lambda *args, **kwargs: SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={
                "workflow_execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#child_workflow",
                    "completed": True,
                    "final_state": "completed",
                    "durable_side_effect_count": 0,
                    "durable_side_effects": [],
                },
                "paper_concept_id": "#V#paper_123",
                "file_copy_concept_id": "#V#file_copy_456",
            },
        ),
    )

    result = orchestrator._action_turn_execution_execute_selected(
        SimpleNamespace(
            data={
                "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                "selected_workflow_trace": {},
                "conversation_session_id": "session-1",
                "turn_id": "turn-1",
            },
            environment=SimpleNamespace(
                llm_client=_DummyLLM(),
                model="test-model",
                user_namespace="#V#user",
                auxiliary_system_prompt=None,
            ),
            trace=None,
        )
    )

    report = result.outputs["completion_report"]
    assert report["paper_concept_id"] == "#V#paper_123"
    assert report["file_copy_concept_id"] == "#V#file_copy_456"
    assert report["result_snapshot"]["paper_concept_id"] == "#V#paper_123"
    assert report["result_snapshot"]["file_copy_concept_id"] == "#V#file_copy_456"
    assert (
        "Created paper concept: #V#paper_123."
        in result.outputs["selected_workflow_user_response"]
    )
    assert "Created paper concept: #V#paper_123." in report["response_text"]
    assert "Linked file copy: #V#file_copy_456." in report["response_text"]
    assert "Created paper concept: #V#paper_123." in result.outputs["response_text"]
    assert "Linked file copy: #V#file_copy_456." in result.outputs["response_text"]


def test_execute_selected_captures_child_failure_for_recovery_path(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    monkeypatch.setattr(
        orchestrator,
        "execute_workflow",
        lambda *args, **kwargs: SimpleNamespace(
            completed=False,
            final_state="failed",
            error="selected route failed closed",
            data={},
        ),
    )

    result = orchestrator._action_turn_execution_execute_selected(
        SimpleNamespace(
            data={
                "selected_workflow_id": "#V#specialised_route",
                "selected_workflow_trace": {},
                "conversation_session_id": "session-1",
                "turn_id": "turn-1",
            },
            environment=SimpleNamespace(
                llm_client=_DummyLLM(),
                model="test-model",
                user_namespace="#V#user",
                auxiliary_system_prompt=None,
            ),
            trace=None,
        )
    )

    assert result.status == "success"
    assert result.outputs["selected_workflow_completed"] is False
    assert result.outputs["selected_workflow_child_failed"] is True
    assert result.outputs["selected_workflow_final_state"] == "failed"
    assert result.outputs["selected_workflow_error"] == "selected route failed closed"
    assert (
        result.outputs["selected_workflow_user_response"]
        == "selected route failed closed"
    )
    assert result.outputs["response_text"] == "selected route failed closed"
    report = result.outputs["completion_report"]
    assert report["workflow_id"] == "#V#specialised_route"
    assert report["completed"] is False
    assert report["final_state"] == "failed"
    assert report["error"] == "selected route failed closed"
    assert report["response_text"] == "selected route failed closed"


def test_execute_selected_routes_chat_assistant_via_direct_response(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))

    class _DirectAnswerLLM:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def generate(self, prompt, context=None, model=None):
            self.calls.append(
                {"prompt": prompt, "context": list(context or []), "model": model}
            )
            return "You are Test User."

    llm = _DirectAnswerLLM()

    monkeypatch.setattr(
        orchestrator,
        "execute_workflow",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "chat_assistant direct response should not execute as a child workflow"
            )
        ),
    )

    result = orchestrator._action_turn_execution_execute_selected(
        SimpleNamespace(
            data={
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selected_workflow_trace": {
                    "selector_context_lineage": {
                        "base_context_source": "augmented_context",
                        "stage_added_message_count": 1,
                    }
                },
                "workflow_routing": {
                    "workflow_id": "#V#chat_assistant_workflow",
                    "verdict": "rag_selected",
                    "source": "selector",
                },
                "conversation_session_id": "session-1",
                "turn_id": "turn-1",
                "user_prompt": "Who am I?",
                "turn_expected_outcome_summary": (
                    "Answer from grounded authenticated user context only."
                ),
                "turn_expected_grounding_requirement": (
                    "Only state identity details that are grounded in the current turn context."
                ),
                "turn_expected_precision_policy": (
                    "Prefer explicit uncertainty over speculation."
                ),
                "turn_answering_guidance": (
                    "If no grounded identity evidence is available, say that clearly instead of guessing."
                ),
                "augmented_context": [
                    {
                        "role": "system",
                        "content": "CURRENT USER CONTEXT: Test User (#V#test_user)",
                    },
                    {"role": "user", "content": "Who am I?"},
                ],
                "aux_llm_calls": [],
                "llm_calls": [],
                "policy_state": SimpleNamespace(
                    enabled=False,
                    policy=None,
                    policy_id=None,
                    predicate_id=None,
                    errors=(),
                ),
            },
            environment=SimpleNamespace(
                llm_client=llm,
                model="test-model",
                user_namespace="#V#user",
                auxiliary_system_prompt=None,
            ),
            trace=None,
        )
    )

    assert result.status == "success"
    assert llm.calls
    direct_context = llm.calls[0]["context"]
    assert any(
        isinstance(message, dict)
        and "Expected answer contract for this turn"
        in str(message.get("content") or "")
        for message in direct_context
    )
    assert result.outputs.get("selected_workflow_user_response") == "You are Test User."
    assert result.outputs.get("response_text") == "You are Test User."
    report = result.outputs["completion_report"]
    assert report.get("response_text") == "You are Test User."
    assert report.get("selected_execution_mode") == "direct_response"
    workflow_routing = result.outputs["workflow_routing"]
    assert workflow_routing["dispatch"]["selected_execution_mode"] == "direct_response"
    assert workflow_routing["dispatch"]["dispatch_workflow_id"] == (
        "#V#chat_assistant_workflow"
    )


def test_execute_selected_surfaces_missing_selected_workflow_as_recoverable_context() -> (
    None
):
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))

    result = orchestrator._action_turn_execution_execute_selected(
        SimpleNamespace(
            data={
                "selected_workflow_trace": {},
                "conversation_session_id": "session-1",
                "turn_id": "turn-1",
            },
            environment=SimpleNamespace(
                llm_client=_DummyLLM(),
                model="test-model",
                user_namespace="#V#user",
                auxiliary_system_prompt=None,
            ),
            trace=None,
        )
    )

    assert result.status == "success"
    assert result.outputs["selected_workflow_completed"] is False
    assert result.outputs["selected_workflow_child_failed"] is True
    assert result.outputs["selected_workflow_final_state"] == "no_selected_workflow"
    assert (
        result.outputs["selected_workflow_error"]
        == "turn_execution_no_workflow_selected"
    )
