from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
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


def test_supervised_turn_preserves_gate_reported_response_when_follow_up_is_required(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
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
    assert CHAT_ASSISTANT_WORKFLOW_ID in result.outputs["selected_workflow_trace"][
        "selector_candidate_ids"
    ]


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
    assert result.outputs["selected_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert result.outputs["workflow_routing"]["workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert result.outputs["workflow_routing"]["verdict"] == "tool_contract_override"
    assert result.outputs["workflow_routing"]["source"] == "selector_override"
    assert (
        result.outputs["selected_workflow_trace"]["selector_selection_metadata"][
            "unmatched_candidate_workflow_id"
        ]
        == excluded_workflow_id
    )
    override_entry = next(
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selector_unmatched_candidate_requires_safe_general_fallback"
    )
    assert override_entry["selected_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert override_entry["requested_candidate_workflow_id"] == excluded_workflow_id


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
    report = result.outputs["completion_report"]
    assert report["workflow_id"] == "#V#specialised_route"
    assert report["completed"] is False
    assert report["final_state"] == "failed"
    assert report["error"] == "selected route failed closed"


def test_execute_selected_surfaces_missing_selected_workflow_as_recoverable_context() -> None:
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
    assert result.outputs["selected_workflow_error"] == "turn_execution_no_workflow_selected"
