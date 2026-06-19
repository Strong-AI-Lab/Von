from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import src.backend.integrations.internal_mcp.orchestrator as orchestrator_module
from src.backend.integrations.internal_mcp.orchestrator import (
    CancellationRequested,
    InternalMCPChatOrchestrator,
    ProgressTracker,
    _WorkflowModelPolicyState,
)
from src.backend.workflows.conversation_turn_llm_timeout import (
    DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC,
)
from src.backend.workflows import (
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowRegistration,
    WorkflowStateSpec,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from orchestrator_test_harness import build_db_independent_orchestrator

_DISPATCH_POLICY_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "turn_contract_dispatch_policy.json"
)


class _DummyLLM:
    def generate(self, prompt, context=None, model=None):  # pragma: no cover
        raise AssertionError("LLM should not be called in this regression test")


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


def _stub_base_system_prompt(
    monkeypatch, orchestrator: InternalMCPChatOrchestrator
) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: (
            "Test base system prompt",
            "#V#test_base_prompt",
        ),
    )


def _stub_represented_dispatch_policy(monkeypatch) -> None:
    payload = json.loads(_DISPATCH_POLICY_FIXTURE_PATH.read_text(encoding="utf-8"))

    def _fixture_rows(concept_id, predicate=None, limit=1):
        if (
            concept_id == "#V#turn_contract_dispatch_policy"
            and predicate == "hasContent"
        ):
            return [{"text": json.dumps(payload)}]
        return []

    monkeypatch.setattr(
        "src.backend.services.turn_contract_dispatch_policy_service."
        "get_texts_for_concept",
        _fixture_rows,
    )


def test_generic_selected_workflow_modes_do_not_load_vontology_definitions(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=_DummyGateway(),
    )

    def fail_definition_load(_workflow_id):
        raise AssertionError("generic workflow mode should not load Vontology metadata")

    monkeypatch.setattr(
        orchestrator,
        "_resolve_workflow_registration_and_definition",
        fail_definition_load,
    )

    assert (
        orchestrator._selected_workflow_execution_mode(
            selected_workflow_id=TOOL_CALLING_WORKFLOW_ID
        )
        == "tool_pipeline"
    )
    assert (
        orchestrator._selected_workflow_execution_mode(
            selected_workflow_id=CHAT_ASSISTANT_WORKFLOW_ID
        )
        == "direct_response"
    )
    assert (
        orchestrator._selected_workflow_execution_mode(
            selected_workflow_id=CHAT_NARRATION_WORKFLOW_ID
        )
        == "direct_response"
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


def test_supervised_turn_prefers_gate_reported_response_when_completed_workflow_requires_follow_up(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)
    expected_response = (
        "Execution status: required grounded evidence was not retrieved."
    )
    monkeypatch.setattr(
        orchestrator,
        "execute_workflow",
        lambda *args, **kwargs: SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={
                "response_text": (
                    "You are Michael Witbrock. I could not find any papers explicitly linked to you."
                ),
                "final_response": expected_response,
                "completion_gate_decision": "escalation_required",
                "completion_gate_requires_follow_up": True,
                "completion_gate_safe_to_claim_completion": False,
                "completion_gate_evidence_payload": {
                    "terminal_outcome": "follow_up_required"
                },
            },
        ),
    )

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Tell me who I am and list my papers.",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
    )

    assert result.response_text == expected_response
    assert result.completion_gate_verdict is not None
    assert result.completion_gate_verdict.get("decision") == "escalation_required"
    assert result.completion_gate_verdict.get("requires_follow_up") is True


def test_sanitise_user_visible_action_output_strips_internal_status_suffix() -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    aux_log: list[dict[str, Any]] = []

    result = orchestrator._sanitise_user_visible_action_output(
        "Grounded answer text.\n\nExecution status: required tool execution was not completed.",
        aux_log=aux_log,
        source_stage="test",
    )

    assert result == "Grounded answer text."
    assert any(
        entry.get("reason") == "internal_status_suffix_stripped" for entry in aux_log
    )


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


def test_supervised_turn_check_cancellation_uses_progress_tracker(monkeypatch) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)
    observed: dict[str, Any] = {}

    def _execute_workflow(*args, **kwargs):
        data = kwargs.get("data")
        assert isinstance(data, dict)
        check_cancellation = data.get("check_cancellation")
        assert callable(check_cancellation)
        try:
            check_cancellation()
        except CancellationRequested as exc:
            observed["cancelled_task_id"] = exc.task_id
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={"response_text": "Done."},
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    tracker = ProgressTracker(
        callback=lambda _info: None,
        cancellation_checker=lambda: True,
        task_id="task-123",
    )
    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Cancel this background turn.",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
        progress_tracker=tracker,
    )

    assert result.response_text == "Done."
    assert observed["cancelled_task_id"] == "task-123"


def test_agent_test_explicit_local_model_preserves_workflow_model_policy(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    monkeypatch.setenv("VON_WORKFLOW_MODEL_POLICY_ENABLE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)

    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "mail_review_response_rendering": {
                    "primary": "active_llm",
                    "fallback": ["ollama:granite3.3:2b"],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )

    def _unexpected_remote_lookup(*_args, **_kwargs):
        raise AssertionError("remote AgentTest setup lookup should be skipped")

    monkeypatch.setattr(
        orchestrator,
        "_load_workflow_model_policy",
        lambda *_args, **_kwargs: (
            policy_state,
            {
                "type": "workflow_model_policy",
                "enabled": True,
                "loaded": True,
                "policy_source": "test_graph",
            },
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        _unexpected_remote_lookup,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "resolve_model_execution_budget_policy",
        _unexpected_remote_lookup,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "get_model_llm_timeout",
        _unexpected_remote_lookup,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "build_turn_memory_context_state",
        _unexpected_remote_lookup,
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        _unexpected_remote_lookup,
    )
    captured: dict[str, Any] = {}

    def _execute_workflow(*args, **kwargs):
        captured["data"] = kwargs.get("data")
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={"response_text": "Done."},
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)
    progress_events: list[dict[str, Any]] = []
    tracker = ProgressTracker(callback=lambda info: progress_events.append(dict(info)))

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="What text relations are used with the concept for Michael Witbrock?",
        context=None,
        llm_client=_DummyLLM(),
        model="gemma4:e4b",
        progress_tracker=tracker,
    )

    workflow_data = captured.get("data")
    assert isinstance(workflow_data, dict)
    assert result.response_text == "Done."
    assert workflow_data["policy_state"] is policy_state
    assert workflow_data["policy_state"].enabled is True
    assert workflow_data["policy_state"].policy["stages"][
        "mail_review_response_rendering"
    ]["fallback"] == ["ollama:granite3.3:2b"]
    assert workflow_data["registry_snapshot"]["source"] == (
        "explicit_local_model_override"
    )
    assert workflow_data["model_execution_budget_policy"] is None
    assert workflow_data["turn_memory_context_state"]["status"] == "none"
    assert "AgentTest" in workflow_data["augmented_context"][0]["content"]
    subtasks = {
        event.get("subtask")
        for event in progress_events
        if isinstance(event.get("subtask"), str)
    }
    assert "Load routing model policy for explicit local model" in subtasks
    assert "Use explicit local model registry snapshot" in subtasks
    assert "Use no turn memory context" in subtasks
    assert "Execute supervised workflow" in subtasks


def test_agent_test_execute_workflow_skips_durable_persistence(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=False,
    )
    workflow_id = "#V#agent_test_persistence_skip_workflow"
    definition = WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="done",
        states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
        termination_states=("done",),
        purpose="AgentTest persistence skip regression workflow.",
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=definition,
            purpose=definition.purpose,
            source="test",
        )
    )
    aux_log: list[dict[str, Any]] = []

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("AgentTest execute_workflow should skip persistence")

    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
        _unexpected,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_episode_service.start_workflow_use_episode",
        _unexpected,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_episode_service.finalise_workflow_use_episode",
        _unexpected,
    )

    result = orchestrator.execute_workflow(
        workflow_id,
        data={
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#university_of_auckland_strong_ai_lab",
            "conversation_session_id": "session-1",
            "turn_id": "turn-1",
            "workflow_episode_source": "conversation_turn_supervised",
            "workflow_episode_stage": "conversation_turn",
            "aux_llm_calls": aux_log,
        },
        llm_client=_DummyLLM(),
        model="gemma4:e4b",
        user_namespace="#V#michael_witbrock",
    )

    assert result is not None
    assert result.completed is True
    submission_event = next(
        item for item in aux_log if item.get("type") == "workflow_instance_submission"
    )
    assert submission_event["status"] == "submission_skipped"
    assert submission_event["reason_code"] == "agent_test_instance"


def test_conversation_turn_supervised_execute_workflow_skips_duplicate_durable_submission(
    monkeypatch,
) -> None:
    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=False,
    )
    definition = WorkflowDefinition(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        initial_state="done",
        states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
        termination_states=("done",),
        purpose="Conversation turn duplicate durable submission skip regression.",
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            definition=definition,
            purpose=definition.purpose,
            source="test",
        )
    )
    aux_log: list[dict[str, Any]] = []

    def _unexpected_submit(*_args, **_kwargs):
        raise AssertionError(
            "conversation_turn_supervised should not block on duplicate durable submission"
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
        _unexpected_submit,
    )

    result = orchestrator.execute_workflow(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        data={
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#university_of_auckland_strong_ai_lab",
            "conversation_session_id": "session-1",
            "turn_id": "turn-1",
            "workflow_episode_source": "conversation_turn_supervised",
            "workflow_episode_stage": "conversation_turn",
            "aux_llm_calls": aux_log,
        },
        llm_client=_DummyLLM(),
        model="gemma4:e4b",
        user_namespace="#V#michael_witbrock",
    )

    assert result is not None
    assert result.completed is True
    submission_event = next(
        item for item in aux_log if item.get("type") == "workflow_instance_submission"
    )
    assert submission_event["status"] == "submission_skipped"
    assert (
        submission_event["reason_code"] == "conversation_turn_supervised_in_process"
    )


def test_selected_workflow_execute_workflow_skips_in_process_persistence(
    monkeypatch,
) -> None:
    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=False,
    )
    definition = WorkflowDefinition(
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        initial_state="done",
        states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
        termination_states=("done",),
        purpose="Selected workflow in-process persistence skip regression.",
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            definition=definition,
            purpose=definition.purpose,
            source="test",
        )
    )
    aux_log: list[dict[str, Any]] = []

    def _unexpected_submit(*_args, **_kwargs):
        raise AssertionError(
            "selected in-process workflow should not block on durable submission"
        )

    def _unexpected_episode_start(*_args, **_kwargs):
        raise AssertionError(
            "selected in-process workflow should not block on episode start"
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
        _unexpected_submit,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_episode_service.start_workflow_use_episode",
        _unexpected_episode_start,
    )

    result = orchestrator.execute_workflow(
        TOOL_CALLING_WORKFLOW_ID,
        data={
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#university_of_auckland_strong_ai_lab",
            "conversation_session_id": "session-1",
            "turn_id": "turn-1",
            "workflow_episode_source": "conversation_turn_selected_workflow",
            "workflow_episode_stage": "selected_workflow_execution",
            "aux_llm_calls": aux_log,
        },
        llm_client=_DummyLLM(),
        model="gemma4:e4b",
        user_namespace="#V#michael_witbrock",
    )

    assert result is not None
    assert result.completed is True
    submission_event = next(
        item for item in aux_log if item.get("type") == "workflow_instance_submission"
    )
    assert submission_event["status"] == "submission_skipped"
    assert (
        submission_event["reason_code"]
        == "conversation_turn_selected_workflow_in_process"
    )


def test_supervised_turn_times_out_workflow_model_policy_load(monkeypatch) -> None:
    import time

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    monkeypatch.setenv("VON_WORKFLOW_MODEL_POLICY_TIMEOUT_SECONDS", "0.01")
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)

    def _slow_policy_load(_preferred_language=None):
        time.sleep(0.2)
        return (
            _WorkflowModelPolicyState(
                enabled=True,
                policy={"unexpected": True},
                policy_id="#V#slow_policy",
                predicate_id="#V#has_model_policy_json",
                errors=(),
            ),
            {"type": "workflow_model_policy", "loaded": True},
        )

    monkeypatch.setattr(
        orchestrator,
        "_load_workflow_model_policy",
        _slow_policy_load,
    )

    def _execute_workflow(*_args, **kwargs):
        data = kwargs.get("data")
        aux_calls = data.get("aux_llm_calls") if isinstance(data, dict) else []
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={
                "response_text": "ok",
                "aux_llm_calls": (
                    [dict(item) for item in aux_calls if isinstance(item, dict)]
                    if isinstance(aux_calls, list)
                    else []
                ),
            },
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="List my recent mail.",
        context=None,
        llm_client=_DummyLLM(),
        model="gemma4:e4b",
    )

    assert result.response_text == "ok"
    timeout_event = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "workflow_dispatch_prepare_step"
    )
    assert timeout_event["step_label"].startswith("Load routing model policy")
    assert timeout_event["status"] == "timed_out"
    policy_event = next(
        item
        for item in result.aux_llm_calls
        if item.get("type") == "workflow_model_policy"
    )
    assert policy_event["policy_source"] == "timeout"
    assert policy_event["loaded"] is False
    assert policy_event["errors"] == ["workflow_model_policy_timeout"]


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


def test_turn_execution_route_preflight_overrides_direct_response_with_required_tools(
    monkeypatch,
) -> None:
    _stub_represented_dispatch_policy(monkeypatch)
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    prompt_text = "What students do I supervise"
    aux_llm_calls: list[dict[str, Any]] = []

    class _DirectResponseSelectorLLM:
        def generate(self, prompt, context=None, model=None):
            if isinstance(prompt, str) and prompt.startswith("Select workflow"):
                assert prompt_text in prompt
                return (
                    '{"workflow_id":"#V#chat_assistant_workflow",'
                    '"confidence":0.91,'
                    '"reasoning":"The request can be answered directly."}'
                )
            raise AssertionError(f"Unexpected selector prompt: {prompt!r}")

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": prompt_text,
                "turn_expected_outcome_contract": {
                    "summary": "Answer from grounded represented relationships.",
                    "selector_guidance": (
                        "Use the represented retrieval tools before answering."
                    ),
                    "required_tools": [
                        "find_relations_with_argument",
                        "get_text_relations_summary",
                        "search_concepts",
                    ],
                },
                "workflow_discovery_result": {
                    "requested_query": prompt_text,
                    "query": prompt_text,
                    "discovery_query_input": prompt_text,
                    "search_sources": ["capability_index"],
                    "candidate_count": 2,
                    "match_count": 2,
                    "matches": [
                        {
                            "concept_id": "#V#entity_information_retrieval_workflow",
                            "name": "Entity Information Retrieval Workflow",
                            "description": (
                                "Grounded retrieval of information of a requested "
                                "kind about a resolved entity."
                            ),
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "is_policy_safe": True,
                            "routing_eligible": True,
                            "routing_profile": {"role": "retrieval"},
                            "candidate_source": "workflow_discovery",
                        },
                        {
                            "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                            "name": "Chat Assistant Workflow",
                            "description": "Default direct response workflow.",
                            "is_executable": True,
                            "executability_reason": "executable_now",
                            "is_policy_safe": True,
                            "routing_eligible": True,
                            "candidate_source": "selector_default",
                        },
                    ],
                    "candidates": [
                        {"concept_id": "#V#entity_information_retrieval_workflow"},
                        {"concept_id": CHAT_ASSISTANT_WORKFLOW_ID},
                    ],
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
                llm_client=_DirectResponseSelectorLLM(),
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert result.outputs["selected_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert result.outputs["workflow_routing"]["workflow_id"] == (
        TOOL_CALLING_WORKFLOW_ID
    )
    assert result.outputs["workflow_routing"]["verdict"] == "tool_contract_override"
    assert result.outputs["workflow_routing"]["source"] == "selector_override"
    selector_override = result.outputs["selected_workflow_trace"]["selector_override"]
    assert selector_override["reason"] == (
        "direct_response_route_cannot_satisfy_required_turn_tools"
    )
    assert selector_override["prior_selected_workflow_id"] == (
        CHAT_ASSISTANT_WORKFLOW_ID
    )
    assert selector_override["turn_contract_required_tools"] == [
        "find_relations_with_argument",
        "get_text_relations_summary",
        "search_concepts",
    ]
    contract_check_entry = next(
        (
            entry
            for entry in aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_turn_contract_check"
        ),
        None,
    )
    assert contract_check_entry is not None
    assert contract_check_entry.get("status") == (
        "direct_response_route_requires_tool_pipeline"
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
            if isinstance(prompt, str) and prompt.startswith("Select workflow"):
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
    contract_state = result.outputs["turn_expected_outcome_contract_state"]
    assert contract_state["schema_version"] == "turn_expected_outcome_contract.v1"
    assert contract_state["fields"]["summary"] == (
        "Answer only with papers that can be grounded to the user."
    )
    assert contract_state["fields"]["grounding_requirement"] == (
        "Only mention papers when authorship or ownership is grounded."
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
    progress_events: list[dict[str, Any]] = []

    class _CapturingRouteLLM:
        def generate(self, prompt, context=None, model=None):
            llm_calls.append(
                {"prompt": prompt, "context": list(context or []), "model": model}
            )
            return CHAT_ASSISTANT_WORKFLOW_ID

    original_prepare_selection_prompt = (
        orchestrator._workflow_selector.prepare_selection_prompt
    )

    def _capturing_prepare_selection_prompt(*args, **kwargs):
        progress_events.append({"marker": "prepare_selection_prompt"})
        return original_prepare_selection_prompt(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator._workflow_selector,
        "prepare_selection_prompt",
        _capturing_prepare_selection_prompt,
    )

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
                "emit_progress": progress_events.append,
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
    progress_markers = [
        str(event.get("subtask") or event.get("marker") or "")
        for event in progress_events
        if isinstance(event, dict)
    ]
    assert "selector candidate summary" in progress_markers
    assert "prepare_selection_prompt" in progress_markers
    assert progress_markers.index(
        "selector candidate summary"
    ) < progress_markers.index("prepare_selection_prompt")
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


def test_prepare_selector_context_compacts_bloated_augmented_context(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    orchestrator._selector_context_chars = 2_000
    old_debug_payload = "old diagnostic payload " + ("x" * 8_000)
    recent_user_turn = "Please route this concise request."

    result = orchestrator._action_turn_execution_prepare_selector_context(
        SimpleNamespace(
            data={
                "user_prompt": recent_user_turn,
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
                "augmented_context": [
                    {"role": "system", "content": "CURRENT USER CONTEXT: Test User"},
                    {"role": "assistant", "content": old_debug_payload},
                    {"role": "tool", "content": "tool payload " + ("y" * 7_000)},
                    {"role": "user", "content": recent_user_turn},
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
    selector_context = result.outputs["selector_context_messages"]
    joined_context = "\n".join(
        str(message.get("content") or "")
        for message in selector_context
        if isinstance(message, dict)
    )
    assert "Current turn request to route" in joined_context
    assert recent_user_turn in joined_context
    assert old_debug_payload not in joined_context

    lineage = result.outputs["selector_context_lineage"]
    assert lineage["base_context_source"] == "selector_compact_augmented_context"
    capsule = lineage["selector_context_capsule"]
    assert capsule["schema_version"] == "selector_context_capsule.v1"
    assert capsule["compacted"] is True
    assert capsule["original_content_chars"] > capsule["included_content_chars"]
    assert capsule["estimated_included_tokens"] < capsule["estimated_original_tokens"]
    assert capsule["omitted_message_count"] >= 1


def test_turn_execution_route_uses_represented_selector_fast_path_without_llm(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    selected_workflow_id = "#V#represented_fast_path_candidate_workflow"
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(),
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
            purpose="A represented workflow candidate for fast-path tests.",
            source="test",
        )
    )

    class _FailIfCalledLLM:
        def generate(self, prompt, context=None, model=None):  # pragma: no cover
            raise AssertionError(
                "selector LLM should be skipped by represented fast path"
            )

    aux_llm_calls: list[dict[str, Any]] = []
    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "Run the represented candidate.",
                "workflow_discovery_result": {
                    "requested_query": "Run the represented candidate.",
                    "query": "Run the represented candidate.",
                    "selector_fast_path_policy": {
                        "enabled": True,
                        "rule": "unique_candidate_covers_contract",
                        "authority_concept_id": (
                            "#V#selector_fast_path_policy_unique_contract"
                        ),
                        "require_contract_coverage": True,
                    },
                    "matches": [
                        {
                            "concept_id": selected_workflow_id,
                            "name": "Represented Fast Path Candidate",
                            "description": "Synthetic represented workflow candidate.",
                            "routing_eligible": True,
                            "is_executable": True,
                            "is_policy_safe": True,
                            "turn_launchable": True,
                            "covers_expected_tool_set": True,
                            "covers_success_contract": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "execution"},
                            "selector_fast_path_policy": {
                                "enabled": True,
                                "rule": "unique_candidate_covers_contract",
                                "authority_concept_id": (
                                    "#V#selector_fast_path_policy_unique_contract"
                                ),
                                "require_contract_coverage": True,
                            },
                        }
                    ],
                    "candidates": [
                        {
                            "concept_id": selected_workflow_id,
                            "name": "Represented Fast Path Candidate",
                            "description": "Synthetic represented workflow candidate.",
                            "routing_eligible": True,
                            "is_executable": True,
                            "is_policy_safe": True,
                            "turn_launchable": True,
                            "covers_expected_tool_set": True,
                            "covers_success_contract": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "execution"},
                            "selector_fast_path_policy": {
                                "enabled": True,
                                "rule": "unique_candidate_covers_contract",
                                "authority_concept_id": (
                                    "#V#selector_fast_path_policy_unique_contract"
                                ),
                                "require_contract_coverage": True,
                            },
                        }
                    ],
                    "match_count": 1,
                },
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
                llm_client=_FailIfCalledLLM(),
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert result.outputs["selected_workflow_id"] == selected_workflow_id
    assert result.outputs["workflow_routing"]["source"] == "represented_fast_path"
    fast_path = result.outputs["selected_workflow_trace"][
        "selector_represented_fast_path"
    ]
    assert fast_path["applied"] is True
    assert (
        fast_path["authority_source"] == "#V#selector_fast_path_policy_unique_contract"
    )
    assert any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_represented_fast_path"
        and entry.get("applied") is True
        for entry in aux_llm_calls
    )


def test_turn_execution_route_falls_back_to_selector_llm_when_fast_path_ambiguous(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    first_workflow_id = "#V#represented_fast_path_first_workflow"
    second_workflow_id = "#V#represented_fast_path_second_workflow"
    for workflow_id in (first_workflow_id, second_workflow_id):
        orchestrator._workflow_registry.register_or_replace(
            WorkflowRegistration(
                workflow_id=workflow_id,
                definition=WorkflowDefinition(
                    workflow_id=workflow_id,
                    initial_state="complete",
                    states={
                        "complete": WorkflowStateSpec(
                            state_id="complete",
                            actions=(),
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
                purpose=f"Synthetic represented workflow {workflow_id}.",
                source="test",
            )
        )

    llm_calls: list[dict[str, Any]] = []

    class _CapturingLLM:
        def generate(self, prompt, context=None, model=None):
            llm_calls.append({"prompt": prompt, "context": list(context or [])})
            return first_workflow_id

    candidate_base = {
        "description": "Synthetic represented workflow candidate.",
        "routing_eligible": True,
        "is_executable": True,
        "is_policy_safe": True,
        "turn_launchable": True,
        "covers_expected_tool_set": True,
        "covers_success_contract": True,
        "executability_reason": "executable_now",
        "candidate_source": "workflow_discovery",
        "routing_profile": {"role": "execution"},
        "selector_fast_path_policy": {
            "enabled": True,
            "rule": "unique_candidate_covers_contract",
            "authority_concept_id": "#V#selector_fast_path_policy_unique_contract",
            "require_contract_coverage": True,
        },
    }
    aux_llm_calls: list[dict[str, Any]] = []
    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": "Run one represented candidate.",
                "workflow_discovery_result": {
                    "requested_query": "Run one represented candidate.",
                    "query": "Run one represented candidate.",
                    "selector_fast_path_policy": {
                        "enabled": True,
                        "rule": "unique_candidate_covers_contract",
                        "authority_concept_id": (
                            "#V#selector_fast_path_policy_unique_contract"
                        ),
                        "require_contract_coverage": True,
                    },
                    "matches": [
                        {
                            **candidate_base,
                            "concept_id": first_workflow_id,
                            "name": "First Represented Fast Path Candidate",
                        },
                        {
                            **candidate_base,
                            "concept_id": second_workflow_id,
                            "name": "Second Represented Fast Path Candidate",
                        },
                    ],
                    "candidates": [
                        {
                            **candidate_base,
                            "concept_id": first_workflow_id,
                            "name": "First Represented Fast Path Candidate",
                        },
                        {
                            **candidate_base,
                            "concept_id": second_workflow_id,
                            "name": "Second Represented Fast Path Candidate",
                        },
                    ],
                    "match_count": 2,
                },
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
                llm_client=_CapturingLLM(),
                model="test-model",
            ),
        )
    )

    assert result.status == "success"
    assert llm_calls
    fast_path_entry = next(
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_represented_fast_path"
    )
    assert fast_path_entry["applied"] is False
    assert fast_path_entry["reason"] == "ambiguous_represented_fast_path_candidates"


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


def test_turn_execution_route_recovers_launchable_requested_workflow_after_discovery_timeout(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    selected_workflow_id = "#V#entity_information_retrieval_workflow"
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
            ),
            purpose=(
                "Canonical grounded retrieval workflow for authenticated "
                "self-relative entity questions."
            ),
            source="test",
        )
    )

    llm = SimpleNamespace(
        generate=lambda prompt, context=None, model=None: (
            '{"workflow_id":"#V#entity_information_retrieval_workflow",'
            '"confidence":0.98,'
            '"reasoning":"This is an authenticated self-relative entity '
            "information request, so the entity-information retrieval workflow "
            'is the best fit."}'
        )
    )

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": (
                    "What research interests of mine are explicitly represented here?"
                ),
                "workflow_discovery_result": {
                    "requested_query": (
                        "What research interests of mine are explicitly represented "
                        "here?"
                    ),
                    "query": (
                        "What research interests of mine are explicitly represented "
                        "here?"
                    ),
                    "discovery_query_input": (
                        "What research interests of mine are explicitly represented "
                        "here?"
                    ),
                    "search_sources": [],
                    "matches": [],
                    "candidates": [],
                    "routing_matches": [],
                    "match_count": 0,
                    "candidate_count": 0,
                    "excluded_candidate_ids": [],
                    "excluded_candidates": [],
                    "budget_exhausted": True,
                    "match_absence_reason": "workflow_discovery_budget_exhausted",
                    "budget_exhaustion_stage": "workflow_discovery_for_turn",
                    "budget_exhaustion_detail": (
                        "workflow_discovery_for_turn timed out after 10.000s "
                        "during workflow discovery"
                    ),
                    "errors": [
                        "workflow_discovery_budget_exhausted: "
                        "workflow_discovery_for_turn timed out after 10.000s "
                        "during workflow discovery"
                    ],
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
    workflow_routing = result.outputs["workflow_routing"]
    assert workflow_routing["workflow_id"] == selected_workflow_id
    assert workflow_routing["verdict"] == "rag_selected"
    assert workflow_routing["source"] == "selector_override"

    recovery_entry = next(
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == (
            "selector_unmatched_candidate_budget_timeout_recovered_to_requested_workflow"
        )
    )
    assert recovery_entry["selected_workflow_id"] == selected_workflow_id
    assert recovery_entry["requested_candidate_workflow_id"] == selected_workflow_id
    assert recovery_entry["workflow_discovery_budget_exhausted"] is True
    assert recovery_entry["function"] == "_promote_selected_workflow_to_custom_dispatch"

    recovery_prepare_step = next(
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_dispatch_prepare_step"
        and entry.get("step_id") == "selector_unmatched_candidate_recovery"
    )
    assert recovery_prepare_step["workflow_id"] == selected_workflow_id


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


def test_turn_execution_route_recovers_workflow_execute_contract_from_generic_selection(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    selected_workflow_id = "#V#grounded_artifact_representation_workflow"
    aux_llm_calls: list[dict[str, Any]] = []
    prompt_text = "Represent the most recent grounded artefact."
    discovery_query_input = (
        "Represent the most recent grounded artefact.\n\n"
        "Turn-intent routing guidance:\n"
        "- Required tools: gmail_get_message, workflow_execute"
    )

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
            purpose=(
                "Represent a grounded artefact by running the appropriate "
                "represented workflow."
            ),
            source="test",
        )
    )

    llm = SimpleNamespace(
        generate=lambda prompt, context=None, model=None: (
            '{"workflow_id":"#V#tool_calling_workflow",'
            '"confidence":0.9,'
            '"reasoning":"This needs tools, so choose the generic tool route."}'
        )
    )

    result = orchestrator._action_turn_execution_route(
        SimpleNamespace(
            data={
                "user_prompt": prompt_text,
                "turn_expected_outcome_contract": {
                    "required_tools": ["gmail_get_message", "workflow_execute"],
                    "success_target": (
                        "Run the represented artefact workflow and read back "
                        "confirmation."
                    ),
                },
                "workflow_discovery_result": {
                    "requested_query": prompt_text,
                    "query": discovery_query_input,
                    "discovery_query_input": discovery_query_input,
                    "query_enrichment_applied": True,
                    "query_enrichment_source": "turn_expected_outcome_contract",
                    "matches": [
                        {
                            "concept_id": selected_workflow_id,
                            "name": "Grounded Artefact Representation Workflow",
                            "description": (
                                "Represent a grounded artefact through represented "
                                "workflow authority."
                            ),
                            "routing_eligible": True,
                            "is_executable": True,
                            "is_policy_safe": True,
                            "turn_launchable": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "execution"},
                        }
                    ],
                    "candidates": [
                        {
                            "concept_id": selected_workflow_id,
                            "name": "Grounded Artefact Representation Workflow",
                            "description": (
                                "Represent a grounded artefact through represented "
                                "workflow authority."
                            ),
                            "routing_eligible": True,
                            "is_executable": True,
                            "is_policy_safe": True,
                            "turn_launchable": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "execution"},
                        }
                    ],
                    "routing_matches": [
                        {
                            "concept_id": selected_workflow_id,
                            "name": "Grounded Artefact Representation Workflow",
                            "description": (
                                "Represent a grounded artefact through represented "
                                "workflow authority."
                            ),
                            "routing_eligible": True,
                            "is_executable": True,
                            "is_policy_safe": True,
                            "turn_launchable": True,
                            "executability_reason": "executable_now",
                            "candidate_source": "workflow_discovery",
                            "routing_profile": {"role": "execution"},
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
    assert result.outputs["selected_workflow_id"] == selected_workflow_id
    assert result.outputs["workflow_routing"]["workflow_id"] == selected_workflow_id
    assert result.outputs["workflow_routing"]["source"] == "selector_override"
    selector_override = result.outputs["selected_workflow_trace"]["selector_override"]
    assert (
        selector_override["reason"]
        == "workflow_execute_contract_recovered_to_single_discovered_workflow"
    )
    assert selector_override["prior_selected_workflow_id"] == "#V#tool_calling_workflow"
    workflow_execute_review = result.outputs["selected_workflow_trace"][
        "workflow_execute_candidate_review"
    ]
    assert workflow_execute_review["status"] == "single_candidate"
    assert workflow_execute_review["eligible_workflow_execute_candidate_ids"] == [
        selected_workflow_id
    ]
    assert any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_execute_candidate_review"
        and entry.get("status") == "single_candidate"
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
        "Paper concept: #V#paper_123."
        in result.outputs["selected_workflow_user_response"]
    )
    assert "Paper concept: #V#paper_123." in report["response_text"]
    assert "File copy concept: #V#file_copy_456." in report["response_text"]
    assert "Paper concept: #V#paper_123." in result.outputs["response_text"]
    assert "File copy concept: #V#file_copy_456." in result.outputs["response_text"]


def test_execute_selected_progress_includes_selector_route_evidence(
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
            data={},
        ),
    )
    progress_events: list[dict[str, Any]] = []

    result = orchestrator._action_turn_execution_execute_selected(
        SimpleNamespace(
            data={
                "selected_workflow_id": "#V#specialised_route",
                "selected_workflow_trace": {
                    "selector_candidate_ids": [
                        "#V#specialised_route",
                        "#V#tool_calling_workflow",
                    ],
                    "excluded_candidate_ids": ["#V#excluded_route"],
                },
                "selector_discovered_workflow_ids": ["#V#specialised_route"],
                "conversation_session_id": "session-1",
                "turn_id": "turn-1",
                "emit_progress": progress_events.append,
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
    execution_events = [
        event
        for event in progress_events
        if isinstance(event.get("selected_workflow_execution_event"), dict)
    ]
    assert execution_events
    first_event = execution_events[0]
    assert first_event["selector_candidate_ids"] == [
        "#V#specialised_route",
        "#V#tool_calling_workflow",
    ]
    assert first_event["selector_discovered_workflow_ids"] == ["#V#specialised_route"]
    assert first_event["selector_excluded_candidate_ids"] == ["#V#excluded_route"]
    nested_event = first_event["selected_workflow_execution_event"]
    assert nested_event["selector_candidate_ids"] == [
        "#V#specialised_route",
        "#V#tool_calling_workflow",
    ]


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


def test_execute_selected_strips_internal_execution_status_from_user_response(
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
                "response_text": (
                    "I found grounded represented evidence for the current user.\n\n"
                    "Execution status: requested mutation was not executed. "
                    "Blocking effect IDs: effect_prompt_required_evidence_search_web_1."
                )
            },
        ),
    )

    result = orchestrator._action_turn_execution_execute_selected(
        SimpleNamespace(
            data={
                "selected_workflow_id": "#V#tool_calling_workflow",
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
    assert (
        result.outputs["selected_workflow_user_response"]
        == "I found grounded represented evidence for the current user."
    )
    assert "Execution status:" not in result.outputs["response_text"]
    assert (
        result.outputs["completion_report"]["response_text"]
        == "I found grounded represented evidence for the current user."
    )


def test_execute_selected_ignores_count_only_response_and_uses_tool_result_fallback(
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
                "response_text": "5 results",
                "invocations": [
                    {
                        "tool": "search_concepts",
                        "status": "ok",
                        "result_preview": {
                            "response_text": (
                                "I found grounded represented evidence linking "
                                "Example Record to the current user."
                            )
                        },
                    }
                ],
            },
        ),
    )

    result = orchestrator._action_turn_execution_execute_selected(
        SimpleNamespace(
            data={
                "selected_workflow_id": "#V#tool_calling_workflow",
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
    assert (
        result.outputs["selected_workflow_user_response"]
        == "I found grounded represented evidence linking Example Record to the current user."
    )
    assert result.outputs["response_text"] == (
        "I found grounded represented evidence linking Example Record to the current user."
    )


def test_execute_selected_routes_chat_assistant_via_direct_response(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)

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
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda _turn_text: {
            "role": "system",
            "content": "CURRENT REQUEST: Who am I?",
        },
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


def test_execute_selected_direct_response_surfaces_selected_workflow_policy_memory(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)

    class _DirectAnswerLLM:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def generate(self, prompt, context=None, model=None):
            self.calls.append(
                {"prompt": prompt, "context": list(context or []), "model": model}
            )
            return "Here is the grounded answer."

    llm = _DirectAnswerLLM()
    monkeypatch.setattr(
        orchestrator_module,
        "build_selected_workflow_policy_memory_state",
        lambda **_kwargs: {
            "schema_version": "selected_workflow_policy_memory.v1",
            "status": "available",
            "selected_workflow_id": "#V#chat_assistant_workflow",
            "suggestion_count": 1,
            "suggestions": [
                {
                    "memory_id": "#V#policy_memory_1",
                    "suggestion_id": "#V#workflow_suggestion_1",
                    "priority": "high",
                    "category": "grounding",
                    "title": "Prefer grounded identity evidence before answering",
                    "rationale": "Recent evaluated turns regressed into stale identity claims.",
                }
            ],
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda _turn_text: {
            "role": "system",
            "content": "CURRENT REQUEST: Who am I?",
        },
    )

    aux_llm_calls: list[dict[str, Any]] = []
    result = orchestrator._action_turn_execution_execute_selected(
        SimpleNamespace(
            data={
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selected_workflow_trace": {},
                "workflow_routing": {
                    "workflow_id": "#V#chat_assistant_workflow",
                    "verdict": "rag_selected",
                    "source": "selector",
                },
                "conversation_session_id": "session-1",
                "turn_id": "turn-1",
                "user_prompt": "Who am I?",
                "turn_memory_context_state": {
                    "status": "available",
                    "fail_closed": False,
                    "subject_contexts": [
                        {
                            "status": "available",
                            "subject_role": "user",
                            "context_dossier_id": "#V#user_turn_dossier",
                            "workspace_fingerprint": "workspace-fp-1",
                        }
                    ],
                },
                "augmented_context": [
                    {
                        "role": "system",
                        "content": "AUTHORITATIVE TURN MEMORY CONTEXT (User):\n- Subject: concept #V#test_user",
                    },
                    {"role": "user", "content": "Who am I?"},
                ],
                "aux_llm_calls": aux_llm_calls,
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
        and "RECENT POLICY MEMORY FOR #V#chat_assistant_workflow:"
        in str(message.get("content") or "")
        for message in direct_context
    )
    context_lineage = result.outputs["selected_workflow_trace"][
        "direct_response_context_lineage"
    ]
    assert context_lineage["selected_workflow_policy_memory"] == {
        "status": "available",
        "selected_workflow_id": "#V#chat_assistant_workflow",
        "suggestion_count": 1,
        "memory_ids": ["#V#policy_memory_1"],
        "suggestion_ids": ["#V#workflow_suggestion_1"],
    }
    assert context_lineage["turn_memory_context"] == {
        "status": "available",
        "fail_closed": False,
        "subject_count": 1,
        "available_subject_count": 1,
        "context_dossier_ids": ["#V#user_turn_dossier"],
        "workspace_fingerprints": ["workspace-fp-1"],
    }
    assert any(
        entry.get("type") == "selected_workflow_policy_memory"
        for entry in aux_llm_calls
    )


def test_supervised_turn_seeds_turn_memory_context_into_workflow_inputs(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))
    _stub_base_system_prompt(monkeypatch, orchestrator)

    monkeypatch.setattr(
        orchestrator_module,
        "build_turn_memory_context_state",
        lambda **_kwargs: {
            "schema_version": "conversation_turn_memory_context.v1",
            "status": "available",
            "fail_closed": False,
            "subject_contexts": [
                {
                    "status": "available",
                    "subject_role": "user",
                    "context_dossier_id": "#V#user_turn_dossier",
                    "workspace_fingerprint": "workspace-fp-1",
                }
            ],
        },
    )
    monkeypatch.setattr(
        orchestrator_module,
        "render_turn_memory_context_messages",
        lambda _state: [
            {
                "role": "system",
                "content": "AUTHORITATIVE TURN MEMORY CONTEXT (User):\n- Subject: concept #V#test_user",
            }
        ],
    )

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
        prompt="Who am I?",
        context=[{"role": "user", "content": "Earlier identity question"}],
        llm_client=_DummyLLM(),
        model="test-model",
        user_namespace="#V#user",
        user_concept_id="#V#test_user",
        turn_memory_context={"subject_id": "#V#test_user", "subject_kind": "concept"},
    )

    workflow_data = captured["data"]
    assert workflow_data["turn_memory_context_state"]["status"] == "available"
    assert any(
        isinstance(message, dict)
        and "AUTHORITATIVE TURN MEMORY CONTEXT (User):"
        in str(message.get("content") or "")
        for message in workflow_data["augmented_context"]
    )
    assert any(
        entry.get("type") == "turn_memory_context" for entry in result.aux_llm_calls
    )
    assert result.response_text == "Done."


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
