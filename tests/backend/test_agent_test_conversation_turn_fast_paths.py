from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.workflow_registry import (
    WorkflowRegistration,
    WorkflowRegistry,
)
from src.backend.workflows.definitions import (
    CHAT_NARRATION_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
)
from src.backend.workflows.durable.subworkflow_actions import (
    register_subworkflow_actions,
)
from src.backend.workflows.durable.turn_execution_runtime_support import (
    run_turn_execution_completion_gate,
)
from src.backend.workflows.llm_step_executor import (
    _request_uses_agent_test_local_model,
    execute_llm_step,
)
from src.backend.services.agent_test_replay_mode_service import (
    AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY,
    AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM,
)
from src.backend.workflows.workflow_selector import WorkflowSelectionPrompt
from src.backend.workflows.subworkflow_contracts import WORKFLOW_SUBWORKFLOW_ACTION_ID


class _ExplodingLLM:
    def generate(self, *_args: Any, **_kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("AgentTest fast path should not call the LLM")


class _CapturingLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt: str, context=None, model=None, **_kwargs: Any) -> str:
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        return self.response


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


def _workflow_definition(
    workflow_id: str,
    *,
    action_id: str,
    purpose: str = "Test workflow",
    routing_profile: dict[str, Any] | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=action_id),),
                terminal=True,
            )
        },
        termination_states=("start",),
        purpose=purpose,
        metadata=(
            {"routing_profile": routing_profile}
            if isinstance(routing_profile, dict)
            else {}
        ),
    )


def _expected_outcome_validation_policy() -> dict[str, Any]:
    defaults = {
        "answering_guidance": "default answering guidance",
        "expected_outcome_summary": "default summary",
        "grounding_requirement": "default grounding",
        "precision_policy": "default precision",
        "required_tools": [],
        "reasoning": "default reasoning",
        "selector_guidance": "default selector guidance",
    }
    return {
        "json_field_defaults": defaults,
        "output_format": "json_value",
        "required_json_fields": list(defaults.keys()),
    }


def test_agent_test_workflow_experience_prelude_returns_empty_guidance(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    registry = ActionRegistry()

    def _unexpected_definition_loader(_workflow_id: str) -> None:
        raise AssertionError("AgentTest prelude bypass should not load definitions")

    register_subworkflow_actions(
        registry,
        definition_loader=_unexpected_definition_loader,
    )
    context: dict[str, Any] = {
        "requested_model": "gemma4:e4b",
        "selected_workflow_id": CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    }

    result = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
            "workflow_experience_target_workflow_id": (
                CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
            ),
            "failure_mode": "capture",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
    )

    assert result.status == "success"
    payload = result.outputs["result"]
    assert payload["workflow_success_guidance_history"] == []
    assert payload["workflow_failure_avoidance_history"] == []
    assert payload["workflow_low_imposition_exploration_history"] == []
    assert payload["workflow_experience_profile_concept_id"].startswith("#V#")
    assert result.outputs["subworkflow_invocation"]["child_final_state"] == (
        "agent_test_skipped"
    )


def test_agent_test_postcondition_critic_subworkflow_uses_deterministic_record(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    registry = ActionRegistry()

    def _unexpected_definition_loader(_workflow_id: str) -> None:
        raise AssertionError("AgentTest critic bypass should not load definitions")

    register_subworkflow_actions(
        registry,
        definition_loader=_unexpected_definition_loader,
    )
    context: dict[str, Any] = {
        "prompt": "What text relations are used with the concept for Michael Witbrock?",
        "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
        "final_response": "For #V#michael_witbrock, the text-relation summary found hasName.",
        "requested_model": "gemma4:e4b",
        "required_prompt_tools": ["get_text_relations_summary"],
        "invocations": [
            {
                "tool": "get_text_relations_summary",
                "status": "ok",
                "payload": {
                    "concept_id": "#V#michael_witbrock",
                    "groups_found": 1,
                    "total_relations_scanned": 1,
                    "groups": [{"predicate": "hasName", "count": 1}],
                },
            }
        ],
        "aux_llm_calls": [],
    }

    result = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
            "failure_mode": "capture",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
    )

    assert result.status == "success"
    assert result.outputs["subworkflow_invocation"]["child_final_state"] == (
        "agent_test_deterministic_critic"
    )
    payload = result.outputs["result"]
    assert payload["agent_test_critic_skip_reason"] == "agent_test_instance"
    assert payload["completion_gate_requires_follow_up"] is False
    assert payload["turn_execution_record"]["execution"]["required_prompt_tools"] == [
        "get_text_relations_summary"
    ]


def test_agent_test_can_exercise_represented_postcondition_critic(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    monkeypatch.setenv("VON_AGENT_TEST_REAL_POSTCONDITION_CRITIC", "1")
    loaded_workflow_ids: list[str] = []

    def _definition_loader(workflow_id: str) -> WorkflowDefinition:
        loaded_workflow_ids.append(workflow_id)
        return WorkflowDefinition(
            workflow_id=workflow_id,
            initial_state="completed",
            states={
                "completed": WorkflowStateSpec(
                    state_id="completed",
                    terminal=True,
                )
            },
            termination_states=("completed",),
        )

    registry = ActionRegistry()
    register_subworkflow_actions(registry, definition_loader=_definition_loader)

    result = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
            "failure_mode": "capture",
        },
        context={
            "prompt": "Inspect the represented terminal outcome.",
            "final_response": "A candidate response.",
            "requested_model": "gemma4:e4b",
        },
        env=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
    )

    assert result.status == "success"
    assert loaded_workflow_ids == [KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID]
    assert result.outputs["subworkflow_invocation"]["child_final_state"] == "completed"
    assert "agent_test_critic_skip_reason" not in result.outputs["result"]


def test_agent_test_postcondition_critic_bounds_completed_selected_workflow(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    registry = ActionRegistry()

    def _unexpected_definition_loader(_workflow_id: str) -> None:
        raise AssertionError("Completed AgentTest critic should not load definitions")

    register_subworkflow_actions(
        registry,
        definition_loader=_unexpected_definition_loader,
    )
    completion_report = {
        "schema_version": "workflow_execution_summary.v1",
        "workflow_id": "#V#example_represent_artefact_workflow",
        "completed": True,
        "effective_completed": True,
        "reported_completed": True,
        "terminal_status": "completed",
        "final_state": "#V#workflow_step_example_represent_artefact_completed",
        "response_text": "The artefact has been represented.",
    }
    context: dict[str, Any] = {
        "prompt": "Represent this paper: https://arxiv.org/abs/2106.03245",
        "user_prompt": "Represent this paper: https://arxiv.org/abs/2106.03245",
        "response_text": "The artefact has been represented.",
        "final_response": "The artefact has been represented.",
        "requested_model": "gpt-oss:20b",
        "selected_model_provider": "openai",
        "workflow_routing": {
            "workflow_id": "#V#example_represent_artefact_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        "selected_workflow_trace": {
            "selected_workflow_id": "#V#example_represent_artefact_workflow",
            "child_workflow_completed": True,
            "child_workflow_final_state": (
                "#V#workflow_step_example_represent_artefact_completed"
            ),
            "completion_report_source": "child_completion_report",
            "workflow_execution_summary": completion_report,
        },
        "completion_report": completion_report,
        "invocations": [
            {
                "tool": "example.represent_artefact",
                "status": "ok",
                "payload": {"concept_id": "#V#represented_artefact"},
            }
        ],
        "aux_llm_calls": [],
    }

    result = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
            "failure_mode": "capture",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gpt-oss:20b"),
    )

    assert result.status == "success"
    assert result.outputs["subworkflow_invocation"]["child_final_state"] == (
        "agent_test_deterministic_critic"
    )
    assert result.outputs["subworkflow_result_envelope"]["agent_test_bypass"] is True
    payload = result.outputs["result"]
    assert payload["agent_test_critic_skip_reason"] == "agent_test_instance"
    assert payload["completion_gate_requires_follow_up"] is False
    assert payload["turn_execution_record"]["completion_report"] == completion_report


def test_agent_test_expected_outcome_fast_path_marks_relation_tools(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            "agent_test_replay_required_tools": ["get_text_relations_summary"],
        },
        llm_policy={"policy_stage": "planner"},
        validation_policy=_expected_outcome_validation_policy(),
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="expected_outcome_inference",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    payload = result.outputs["validated_json"]
    assert payload["required_tools"] == ["get_text_relations_summary"]
    assert "most specific represented workflow" in payload["selector_guidance"]
    assert "generic tool-calling workflow only" in payload["selector_guidance"]


def test_agent_test_expected_outcome_fast_path_does_not_infer_prompt_tools(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
        },
        llm_policy={"policy_stage": "planner"},
        validation_policy=_expected_outcome_validation_policy(),
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="expected_outcome_inference",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    payload = result.outputs["validated_json"]
    assert payload["required_tools"] == []
    assert "most specific represented workflow" not in payload["selector_guidance"]


def test_agent_test_expected_outcome_represented_selector_mode_uses_prompt_when_uncontracted(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    llm = _CapturingLLM(
        json.dumps(
            {
                "expected_outcome_summary": "List recent Gmail messages.",
                "grounding_requirement": "Use Gmail retrieval evidence.",
                "precision_policy": "Do not guess mailbox rows.",
                "selector_guidance": "Prefer the represented mail review workflow.",
                "answering_guidance": "Answer from retrieved Gmail rows only.",
                "reasoning": "The authored expected-outcome prompt owns tool requirements.",
                "required_tools": ["gmail_list_profiles", "gmail_list_messages"],
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm, model="gemma4:e4b"),
        data={
            "user_prompt": "List recent gmail messages with labels.",
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY: (
                AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM
            ),
        },
        prompt_contract={"prompt_text": "Return the expected-outcome JSON."},
        llm_policy={"policy_stage": "planner"},
        validation_policy=_expected_outcome_validation_policy(),
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="expected_outcome_inference",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert len(llm.calls) == 1
    payload = result.outputs["validated_json"]
    assert payload["required_tools"] == ["gmail_list_profiles", "gmail_list_messages"]
    aux_calls = result.outputs.get("aux_llm_calls") or []
    assert not any(
        call.get("reason_code") == "agent_test_explicit_local_replay_fast_path"
        for call in aux_calls
        if isinstance(call, dict)
    )


def test_agent_test_expected_outcome_fast_path_marks_gmail_arxiv_tools(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": (
                "Check recent messages for the zhanvonwitbrock@gmail.com zhan-gmail "
                "identity for arXiv links or PDF/file references. For each arXiv "
                "reference you find, retrieve the paper metadata/title where possible."
            ),
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            "agent_test_replay_required_tools": [
                "gmail_list_messages",
                "gmail_get_message",
                "get_paper_metadata",
                "search_arxiv",
            ],
        },
        llm_policy={"policy_stage": "planner"},
        validation_policy=_expected_outcome_validation_policy(),
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="expected_outcome_inference",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    payload = result.outputs["validated_json"]
    assert payload["required_tools"] == [
        "gmail_list_messages",
        "gmail_get_message",
        "get_paper_metadata",
        "search_arxiv",
    ]
    assert "most specific represented workflow" in payload["selector_guidance"]
    assert "generic tool-calling workflow only" in payload["selector_guidance"]


def test_agent_test_selector_fast_path_routes_relation_prompt_to_tools(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            "turn_expected_required_tools": ["get_text_relations"],
        },
        llm_policy={"policy_stage": "classifier"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="selector_decision",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    selector_payload = json.loads(result.outputs["final_response"])
    assert selector_payload["workflow_id"] == TOOL_CALLING_WORKFLOW_ID


def test_agent_test_selector_fast_path_routes_gmail_arxiv_prompt_to_tools(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": (
                "Check recent messages for zhanvonwitbrock@gmail.com and retrieve "
                "titles for any arXiv paper links."
            ),
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            "turn_expected_required_tools": [
                "gmail_list_messages",
                "gmail_get_message",
                "search_arxiv",
            ],
        },
        llm_policy={"policy_stage": "classifier"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="selector_decision",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    selector_payload = json.loads(result.outputs["final_response"])
    assert selector_payload["workflow_id"] == TOOL_CALLING_WORKFLOW_ID


def test_agent_test_selector_fast_path_reuses_prepared_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    represented_workflow_id = "#V#zhan_gmail_arxiv_ingestion_workflow"
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": (
                "Look in the last 20 email messages for arXiv papers and "
                "represent any papers you find in Vontology."
            ),
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            "selector_candidate_ids": [
                represented_workflow_id,
                TOOL_CALLING_WORKFLOW_ID,
            ],
        },
        llm_policy={"policy_stage": "classifier"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="selector_decision",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    selector_payload = json.loads(result.outputs["final_response"])
    assert selector_payload["workflow_id"] == represented_workflow_id
    assert "selector-prepared candidate" in selector_payload["reasoning"]


def test_agent_test_selector_represented_replay_mode_calls_selector_llm(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    represented_workflow_id = "#V#zhan_gmail_arxiv_ingestion_workflow"
    llm = _CapturingLLM(
        json.dumps(
            {
                "workflow_id": represented_workflow_id,
                "confidence": 0.91,
                "reasoning": "The represented selector prompt chose the candidate.",
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm, model="gemma4:e4b"),
        data={
            "user_prompt": (
                "Look in recent Gmail messages for arXiv papers and summarise "
                "the useful evidence."
            ),
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY: (
                AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM
            ),
            "selector_call_prompt_text": "Select workflow for this user request.",
            "selector_context_messages": [
                {
                    "role": "system",
                    "content": "Authoritative represented selector context.",
                }
            ],
        },
        llm_policy={
            "policy_stage": "classifier",
            "prompt_text_context_key": "selector_call_prompt_text",
            "context_messages_context_key": "selector_context_messages",
        },
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="selector_decision",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert llm.calls == [
        {
            "prompt": "Select workflow for this user request.",
            "context": [
                {
                    "role": "system",
                    "content": "Authoritative represented selector context.",
                }
            ],
            "model": "gemma4:e4b",
        }
    ]
    selector_payload = json.loads(result.outputs["final_response"])
    assert selector_payload["workflow_id"] == represented_workflow_id
    assert result.outputs["llm_step_envelope"]["selected_prompt_source"] == (
        "context.prompt_text"
    )


def test_agent_test_selector_fast_path_prefers_discovery_order_over_policy_order(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    represented_workflow_id = "#V#zhan_gmail_arxiv_ingestion_workflow"
    general_mail_workflow_id = "#V#general_mail_review_workflow"
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": (
                "Look in the last 20 email messages for arXiv papers and "
                "represent any papers you find in Vontology."
            ),
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            "turn_expected_required_tools": [
                "gmail_list_messages",
                "gmail_get_message",
                "search_arxiv",
            ],
            "workflow_discovery_result": {
                "matches": [
                    {
                        "concept_id": represented_workflow_id,
                        "candidate_source": "workflow_discovery",
                        "routing_eligible": True,
                        "is_executable": True,
                        "is_policy_safe": True,
                    },
                    {
                        "concept_id": general_mail_workflow_id,
                        "candidate_source": "workflow_discovery",
                        "routing_eligible": True,
                        "is_executable": True,
                        "is_policy_safe": True,
                    },
                ],
            },
            "selector_candidate_ids": [
                CHAT_NARRATION_WORKFLOW_ID,
                general_mail_workflow_id,
                represented_workflow_id,
                TOOL_CALLING_WORKFLOW_ID,
            ],
        },
        llm_policy={"policy_stage": "classifier"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="selector_decision",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    selector_payload = json.loads(result.outputs["final_response"])
    assert selector_payload["workflow_id"] == represented_workflow_id
    assert "workflow-discovery candidate" in selector_payload["reasoning"]


def test_agent_test_selector_preparation_uses_local_tool_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    calls: list[dict[str, Any]] = []

    def _empty_discovery(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "matches": [],
            "candidates": [],
            "routing_matches": [],
            "query": "relation prompt",
            "match_count": 0,
            "candidate_count": 0,
        }

    def _unexpected_prompt_render(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("AgentTest fallback should not render prompts")

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        _empty_discovery,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        _unexpected_prompt_render,
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
                "requested_model": "gemma4:e4b",
                "selected_model_provider": "ollama",
                "turn_expected_required_tools": ["get_text_relations"],
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gemma4:e4b",
                user_namespace="#V#michael_witbrock",
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    assert calls
    assert outputs["selector_prompt_available"] is True
    assert outputs["selector_candidate_ids"] == [TOOL_CALLING_WORKFLOW_ID]
    discovery = outputs["workflow_discovery_result"]
    assert discovery["agent_test_local_replay"] is True
    assert discovery["agent_test_selector_authority_source"] == (
        "turn_expected_required_tools"
    )
    assert discovery["local_replay_support_only"] is True
    assert discovery["not_production_acceptance_evidence"] is True
    assert outputs["selector_prompt_provenance"]["local_replay_support_only"] is True


def test_agent_test_selector_preparation_does_not_trigger_from_prompt_words(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())

    def _empty_discovery(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "matches": [],
            "candidates": [],
            "routing_matches": [],
            "query": "relation prompt",
            "match_count": 0,
            "candidate_count": 0,
        }

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        _empty_discovery,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda prompt: {"role": "user", "content": str(prompt)},
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
                "requested_model": "gemma4:e4b",
                "selected_model_provider": "ollama",
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gemma4:e4b",
                user_namespace="#V#michael_witbrock",
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    discovery = outputs["workflow_discovery_result"]
    assert discovery.get("agent_test_local_replay") is None
    assert discovery.get("agent_test_selector_authority_source") is None
    assert discovery["candidate_count"] == 0
    assert discovery["match_count"] == 0
    assert outputs["selector_prompt_id"] != "agent_test_local_replay_selector_prompt"


def test_selector_preparation_does_not_use_agent_test_authority_outside_agent_test(
    monkeypatch,
) -> None:
    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())

    def _empty_discovery(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "matches": [],
            "candidates": [],
            "routing_matches": [],
            "query": "relation prompt",
            "match_count": 0,
            "candidate_count": 0,
        }

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        _empty_discovery,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda prompt: {"role": "user", "content": str(prompt)},
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
                "requested_model": "gemma4:e4b",
                "selected_model_provider": "ollama",
                "turn_expected_required_tools": ["get_text_relations"],
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gemma4:e4b",
                user_namespace="#V#michael_witbrock",
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    assert outputs["workflow_discovery_result"].get("agent_test_local_replay") is None
    assert outputs["workflow_discovery_result"]["candidate_count"] == 0
    assert outputs["workflow_discovery_result"]["match_count"] == 0
    assert outputs["selector_prompt_id"] != "agent_test_local_replay_selector_prompt"


def test_agent_test_selector_preparation_exposes_required_tool_workflow_overlap(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    represented_workflow_id = "#V#zhan_gmail_arxiv_ingestion_workflow"
    required_action_id = "gmail_list_messages"
    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(
            workflow_id=represented_workflow_id,
            definition=_workflow_definition(
                represented_workflow_id,
                action_id=required_action_id,
                purpose="Scan Gmail messages for arXiv references.",
                routing_profile={
                    "schema_version": "workflow_routing_profile.v1",
                    "role": "execution",
                },
            ),
            purpose="Scan Gmail messages for arXiv references.",
            source="test",
        )
    )
    registry.register(
        WorkflowRegistration(
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            definition=_workflow_definition(
                TOOL_CALLING_WORKFLOW_ID,
                action_id="tool_calling.execute",
                purpose="Generic tool workflow",
            ),
            purpose="Generic tool workflow",
            source="test",
        )
    )
    orchestrator._workflow_registry = registry

    def _empty_discovery(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "matches": [],
            "candidates": [],
            "routing_matches": [],
            "query": "represented lookup prompt",
            "match_count": 0,
            "candidate_count": 0,
        }

    def _unexpected_prompt_render(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("AgentTest synthetic fallback should not render prompts")

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        _empty_discovery,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        _unexpected_prompt_render,
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": "Run the represented lookup with grounded evidence.",
                "requested_model": "gemma4:e4b",
                "selected_model_provider": "ollama",
                "turn_expected_required_tools": [required_action_id],
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gemma4:e4b",
                user_namespace="#V#michael_witbrock",
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    assert outputs["selector_authoritative_candidate_ids"] == [represented_workflow_id]
    assert outputs["selector_candidate_ids"] == [
        represented_workflow_id,
        TOOL_CALLING_WORKFLOW_ID,
    ]
    assert outputs["selector_policy_recommendation"]["recommended_workflow_id"] == (
        represented_workflow_id
    )
    selector_payload = json.loads(outputs["selector_call_prompt_text"])
    assert selector_payload["workflow_id"] == represented_workflow_id
    discovery = outputs["workflow_discovery_result"]
    assert discovery["discovery_payload_origin"] == (
        "agent_test_local_replay_synthetic_action_overlap"
    )
    assert discovery["agent_test_selector_authority_source"] == (
        "turn_expected_required_tools"
    )
    assert discovery["local_replay_support_only"] is True
    assert discovery["not_production_acceptance_evidence"] is True
    assert discovery["search_sources"] == [
        "agent_test_local_replay",
        "workflow_registry_action_overlap",
    ]
    assert discovery["candidate_count"] == 2
    assert discovery["match_count"] == 1
    assert discovery["candidates"][0]["concept_id"] == represented_workflow_id
    assert discovery["candidates"][0]["matched_required_tools"] == [required_action_id]
    assert discovery["candidates"][1]["concept_id"] == TOOL_CALLING_WORKFLOW_ID


def test_agent_test_selector_preparation_represented_mode_uses_selector_prompt(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    represented_workflow_id = "#V#zhan_gmail_arxiv_ingestion_workflow"
    required_action_id = "gmail_list_messages"
    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(
            workflow_id=represented_workflow_id,
            definition=_workflow_definition(
                represented_workflow_id,
                action_id=required_action_id,
                purpose="Scan Gmail messages for arXiv references.",
                routing_profile={
                    "schema_version": "workflow_routing_profile.v1",
                    "role": "execution",
                },
            ),
            purpose="Scan Gmail messages for arXiv references.",
            source="test",
        )
    )
    registry.register(
        WorkflowRegistration(
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            definition=_workflow_definition(
                TOOL_CALLING_WORKFLOW_ID,
                action_id="tool_calling.execute",
                purpose="Generic tool workflow",
            ),
            purpose="Generic tool workflow",
            source="test",
        )
    )
    orchestrator._workflow_registry = registry

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        lambda *_args, **_kwargs: {
            "matches": [],
            "candidates": [],
            "routing_matches": [],
            "query": "represented lookup prompt",
            "match_count": 0,
            "candidate_count": 0,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda prompt: {"role": "user", "content": str(prompt)},
    )
    captured_selector_candidates: list[str] = []

    def _prepare_selection_prompt(
        *,
        turn_text: str,
        discovered_workflows,
        continuation_routing_context_text=None,
    ) -> WorkflowSelectionPrompt:
        entries = tuple(dict(item) for item in discovered_workflows or ())
        captured_selector_candidates.extend(
            str(item.get("concept_id")).strip()
            for item in entries
            if isinstance(item.get("concept_id"), str)
        )
        return WorkflowSelectionPrompt(
            prompt_id="#V#chat_turn_classifier_prompt",
            prompt_text="Represented selector prompt with candidate list.",
            discovered_workflow_ids=tuple(captured_selector_candidates),
            candidate_entries=entries,
            candidate_list_text="\n".join(captured_selector_candidates),
            continuation_routing_context_text=continuation_routing_context_text,
            requested_prompt_ids=("#V#chat_turn_classifier_prompt",),
            prompt_provenance={
                "prompt_mode": "rag_first_candidate_selector",
                "render_variables": {
                    "candidate_list": "\n".join(captured_selector_candidates)
                },
            },
        )

    monkeypatch.setattr(
        orchestrator._workflow_selector,
        "prepare_selection_prompt",
        _prepare_selection_prompt,
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": (
                    "Look in recent Gmail messages for arXiv references and "
                    "retrieve paper evidence."
                ),
                "requested_model": "gemma4:e4b",
                "selected_model_provider": "ollama",
                AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY: (
                    AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM
                ),
                "turn_expected_required_tools": [required_action_id],
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gemma4:e4b",
                user_namespace="#V#michael_witbrock",
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    assert outputs["selector_prompt_id"] != "agent_test_local_replay_selector_prompt"
    assert outputs["selector_prompt_provenance"]["prompt_mode"] == (
        "rag_first_candidate_selector"
    )
    assert outputs["selector_call_prompt_text"].startswith(
        "Select workflow for this user request:"
    )
    assert represented_workflow_id in captured_selector_candidates
    assert outputs["selector_authoritative_candidate_ids"] == [represented_workflow_id]
    assert represented_workflow_id in outputs["selector_candidate_ids"]
    assert TOOL_CALLING_WORKFLOW_ID in outputs["selector_candidate_ids"]
    assert outputs["selector_context_lineage"]["stage"] == "selector_decision"
    discovery = outputs["workflow_discovery_result"]
    assert discovery["agent_test_local_replay"] is True
    assert discovery["discovery_payload_origin"] == (
        "agent_test_local_replay_synthetic_action_overlap"
    )


def test_agent_test_selector_preparation_prefers_represented_discovery_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    represented_workflow_id = "#V#zhan_gmail_arxiv_ingestion_workflow"

    def _represented_discovery(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "matches": [
                {
                    "concept_id": represented_workflow_id,
                    "name": "Zhan Gmail arXiv ingestion workflow",
                    "description": "Ingest Gmail messages that reference arXiv papers.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "candidates": [
                {
                    "concept_id": represented_workflow_id,
                    "name": "Zhan Gmail arXiv ingestion workflow",
                    "description": "Ingest Gmail messages that reference arXiv papers.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "routing_matches": [],
            "query": "gmail arxiv prompt",
            "match_count": 1,
            "candidate_count": 1,
        }

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        _represented_discovery,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_discovered_candidate_turn_launchability",
        lambda **_kwargs: {represented_workflow_id: {"launchable": True}},
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda prompt: {"role": "user", "content": str(prompt)},
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": (
                    "Look in the last 20 email messages for arXiv papers and "
                    "represent any papers you find in Vontology."
                ),
                "requested_model": "gemma4:e4b",
                "selected_model_provider": "ollama",
                "turn_expected_required_tools": [
                    "gmail_list_messages",
                    "gmail_get_message",
                    "search_arxiv",
                ],
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gemma4:e4b",
                user_namespace=None,
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    assert represented_workflow_id in outputs["selector_candidate_ids"]
    assert outputs["selector_authoritative_candidate_ids"] == [represented_workflow_id]
    assert outputs["selector_authoritative_candidate_source"] == (
        "workflow_discovery_pre_policy"
    )
    assert outputs["workflow_discovery_result"].get("agent_test_local_replay") is None


def test_agent_test_route_uses_represented_selector_decision_for_gmail_arxiv(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    target_workflow_id = "#V#zhan_gmail_arxiv_ingestion_workflow"
    policy_preferred_workflow_id = CHAT_NARRATION_WORKFLOW_ID

    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda prompt: {"role": "user", "content": str(prompt)},
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_discovered_candidate_turn_launchability",
        lambda **_kwargs: {
            target_workflow_id: {"launchable": True},
            policy_preferred_workflow_id: {"launchable": True},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        lambda *_args, **_kwargs: {
            "matches": [
                {
                    "concept_id": target_workflow_id,
                    "name": "Zhan Gmail arXiv ingestion workflow",
                    "description": (
                        "Scan recent Gmail messages, identify arXiv references, "
                        "and represent the papers in Vontology."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "candidate_source": "workflow_discovery",
                }
            ],
            "candidates": [
                {
                    "concept_id": target_workflow_id,
                    "name": "Zhan Gmail arXiv ingestion workflow",
                    "description": "Represent arXiv papers found in Gmail.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "candidate_source": "workflow_discovery",
                }
            ],
            "routing_matches": [],
            "query": "gmail arxiv prompt",
            "match_count": 1,
            "candidate_count": 1,
            "selector_fast_path_policy": {
                "candidate_scores": [
                    {
                        "workflow_id": policy_preferred_workflow_id,
                        "score": 1.0,
                    },
                    {
                        "workflow_id": target_workflow_id,
                        "score": 0.5,
                    },
                ],
                "recommended_workflow_id": policy_preferred_workflow_id,
                "guidance_mode": "agent_test_policy_order_probe",
            },
        },
    )
    prompt = (
        "Look in the last 20 email messages for arXiv papers and represent any "
        "papers you find in Vontology."
    )
    data: dict[str, Any] = {
        "prompt": prompt,
        "user_prompt": prompt,
        "requested_model": "gemma4:e4b",
        "selected_model_provider": "ollama",
        "gmail_profile": "zhan-gmail",
        "aux_llm_calls": [],
        "llm_calls": [],
    }
    env = WorkflowEnvironment(
        llm_client=_ExplodingLLM(),
        model="gemma4:e4b",
        user_namespace="#V#zhan@org",
    )
    prepare_request = type(
        "Request",
        (),
        {
            "data": data,
            "environment": env,
        },
    )()
    prepare_result = orchestrator._action_turn_execution_prepare_selector_context(
        prepare_request
    )
    data.update(prepare_result.outputs)

    selector_result = execute_llm_step(
        WorkflowActionRequest(
            action_id="llm.action",
            inputs={},
            environment=env,
            data=data,
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            workflow_state_id="selector_decision",
        )
    )
    data.update(selector_result.outputs)
    data["selector_raw_response"] = selector_result.outputs["final_response"]

    route_result = orchestrator._action_turn_execution_route(
        type(
            "Request",
            (),
            {
                "data": data,
                "environment": env,
            },
        )()
    )

    assert route_result.status == "success"
    selector_payload = json.loads(selector_result.outputs["final_response"])
    assert selector_payload["workflow_id"] == target_workflow_id
    assert "workflow-discovery candidate" in selector_payload["reasoning"]
    assert route_result.outputs["selector_authoritative_candidate_ids"] == [
        target_workflow_id
    ]
    assert route_result.outputs["selected_workflow_id"] == target_workflow_id
    assert route_result.outputs["workflow_routing"]["workflow_id"] == target_workflow_id


def test_agent_test_selector_preparation_bounds_local_discovery_timeout(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    monkeypatch.setenv("VON_AGENT_TEST_WORKFLOW_DISCOVERY_TIMEOUT_SECONDS", "0.75")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    calls: list[dict[str, Any]] = []

    def _capturing_discovery(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "matches": [],
            "candidates": [],
            "routing_matches": [],
            "query": "mail arxiv prompt",
            "match_count": 0,
            "candidate_count": 0,
        }

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        _capturing_discovery,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda prompt: {"role": "user", "content": str(prompt)},
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": (
                    "Check recent zhan-gmail messages for arXiv links and list "
                    "retrieved paper titles."
                ),
                "requested_model": "gemma4:e4b",
                "selected_model_provider": "ollama",
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gemma4:e4b",
                user_namespace=None,
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    assert calls
    assert calls[0]["timeout_seconds"] == "0.75"
    assert outputs["workflow_discovery_result"]["candidate_count"] == 0


def test_selector_preparation_bounds_live_discovery_timeout(monkeypatch) -> None:
    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)
    monkeypatch.setenv("VON_TURN_WORKFLOW_DISCOVERY_TIMEOUT_SECONDS", "0.42")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    calls: list[dict[str, Any]] = []

    def _capturing_discovery(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "matches": [],
            "candidates": [],
            "routing_matches": [],
            "query": "recent email messages",
            "match_count": 0,
            "candidate_count": 0,
        }

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_memo_service.discover_workflows_for_turn_memoized",
        _capturing_discovery,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        lambda prompt: {"role": "user", "content": str(prompt)},
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": "List my six most recent email messages.",
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gpt-5.4-mini",
                user_namespace=None,
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    assert calls
    assert calls[0]["timeout_seconds"] == "0.42"
    assert outputs["workflow_discovery_result"]["candidate_count"] == 0


def test_agent_test_narration_fast_path_reuses_selected_workflow_response(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "selected_workflow_user_response": "Michael Witbrock has relation evidence.",
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            "llm_calls": [
                {
                    "type": "llm.generate",
                    "stage": "classifier",
                    "model_name": "selector-model",
                }
            ],
            "aux_llm_calls": [
                {
                    "type": "workflow_selector_prompt",
                    "stage": "selector_preparation",
                    "prompt_id": "#V#chat_turn_classifier_prompt",
                },
                {
                    "type": "workflow_selector",
                    "stage": "selector_decision",
                    "workflow_id": "#V#chat_assistant_workflow",
                },
            ],
        },
        llm_policy={"policy_stage": "narration"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="narration",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["final_response"] == (
        "Michael Witbrock has relation evidence."
    )
    assert [entry["type"] for entry in result.outputs["llm_calls"]] == [
        "llm.generate",
        "llm.generate_skipped",
    ]
    assert [entry["type"] for entry in result.outputs["aux_llm_calls"]] == [
        "workflow_selector_prompt",
        "workflow_selector",
        "workflow_llm_step_skip",
    ]


def test_agent_test_does_not_assume_bare_hosted_model_is_local(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=_ExplodingLLM(), model="gpt-5.6-luna"
        ),
        data={"requested_model": "gpt-5.6-luna"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="narration",
    )

    assert _request_uses_agent_test_local_model(request) is False


def test_agent_test_infers_bare_ollama_model_is_local(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")

    class _OllamaTestClient(_ExplodingLLM):
        pass

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=_OllamaTestClient(), model="gemma4:e4b"
        ),
        data={"requested_model": "gemma4:e4b"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="narration",
    )

    assert _request_uses_agent_test_local_model(request) is True


def test_agent_test_locality_prefers_selected_over_requested_provider(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=_ExplodingLLM(), model="gpt-5.6-luna"
        ),
        data={
            "requested_model": "gpt-5.6-luna",
            "requested_client_type": "ollama",
            "selected_model_provider": "openai",
        },
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="narration",
    )

    assert _request_uses_agent_test_local_model(request) is False

    request.data["requested_client_type"] = "openai"
    request.data["selected_model_provider"] = "ollama"
    assert _request_uses_agent_test_local_model(request) is True


def test_agent_test_completion_gate_skips_episode_autotrigger(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")

    import src.backend.services.workflow_event_integration_service as event_integration

    monkeypatch.setattr(
        event_integration,
        "episode_evaluation_autotrigger_enabled",
        lambda: (_ for _ in ()).throw(
            AssertionError("AgentTest completion gate should not query autotrigger")
        ),
    )
    monkeypatch.setattr(
        event_integration,
        "maybe_launch_episode_evaluation_for_turn_completion_gate",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("AgentTest completion gate should not launch episodes")
        ),
    )
    request = WorkflowActionRequest(
        action_id="turn_execution.completion_gate",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "final_response": "Michael Witbrock has text relation evidence.",
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "completed",
                    "safe_to_claim_completion": True,
                    "requires_follow_up": False,
                },
                "required_effects": [],
                "critic": {"summary": {}},
            },
            "aux_llm_calls": [],
        },
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_agent_test_completion_gate",
        introspection_auto_apply_env="VON_TEST_AUTO_APPLY",
    )

    assert result.status == "success"
    assert result.outputs["workflow_introspection_autotrigger"]["reason"] == (
        "agent_test_instance"
    )


def test_completion_gate_removes_stale_ledger_suffix_when_completion_is_safe(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    stale_response = (
        "The artefact has been represented.\n\n"
        "Execution status: required tool execution was not completed. "
        "Required tool execution was not observed. "
        "Blocking effect IDs: effect_required_tool_obligations_1. "
        "Unresolved preconditions: Required tool obligations were not satisfied: "
        "scholarly_paper.verify_representation. "
        "Failure codes: required_tool_not_available_on_gateway."
    )
    request = WorkflowActionRequest(
        action_id="turn_execution.completion_gate",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "prompt": "Represent this paper: https://arxiv.org/abs/2106.03245",
            "selected_workflow_user_response": stale_response,
            "final_response": stale_response,
            "response_text": stale_response,
            "current_response": stale_response,
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "completed",
                    "safe_to_claim_completion": True,
                    "requires_follow_up": False,
                },
                "required_effects": [],
                "critic": {"summary": {}},
            },
            "aux_llm_calls": [],
        },
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate_removes_stale_ledger_suffix",
        introspection_auto_apply_env="VON_TEST_AUTO_APPLY",
    )

    assert result.status == "success"
    assert result.outputs["completion_gate_safe_to_claim_completion"] is True
    assert result.outputs["final_response"] == "The artefact has been represented."
    assert result.outputs["response_text"] == "The artefact has been represented."
    assert result.outputs["selected_workflow_user_response"] == (
        "The artefact has been represented."
    )


def test_completion_gate_blocks_workflow_llm_timeout_with_zero_required_effects(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM()),
        data={
            "final_response": (
                "workflow_llm_step_timeout:LLM call timed out after 45s "
                "(stage=planner, model=qwen3:8b)"
            ),
            "llm_step_envelope": {
                "completion_reason": "timeout",
                "timeout_stage": "tool_calling.plan",
                "timeout_detail": (
                    "LLM call timed out after 45s (stage=planner, model=qwen3:8b)"
                ),
            },
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "completed",
                    "safe_to_claim_completion": True,
                    "requires_follow_up": False,
                    "evidence_payload": {"required_effect_count": 0},
                },
                "required_effects": [],
                "critic": {"summary": {}},
            },
            "aux_llm_calls": [],
            "invocations": [],
        },
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate_blocks_workflow_llm_timeout",
        introspection_auto_apply_env="VON_TEST_AUTO_APPLY",
    )

    assert result.status == "success"
    assert result.outputs["completion_gate_safe_to_claim_completion"] is False
    assert result.outputs["completion_gate_requires_follow_up"] is True
    assert result.outputs["completion_gate_repeat_eligible"] is False
    assert (
        "workflow_llm_step_timeout"
        in result.outputs["completion_gate_blocking_failure_codes"]
    )
    assert result.outputs["completion_gate_unresolved_preconditions"] == [
        {
            "effect_id": "effect_workflow_llm_timeout_1",
            "effect_type": "workflow_execution",
            "status": "not_executed",
            "status_reason": (
                "A workflow LLM stage (tool_calling.plan) timed out before "
                "execution evidence could be verified."
            ),
            "failure_codes": ["workflow_llm_step_timeout"],
        }
    ]
    evidence = result.outputs["completion_gate_evidence_payload"]
    assert evidence["execution_signal_blocker"]["source"] == "workflow_llm_timeout"


def test_completion_gate_finds_timeout_inside_failed_subworkflow_outputs(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM()),
        data={
            "last_action_failed": True,
            "last_action_error": (
                "subworkflow_failed:#V#turn_context_adjudication_workflow:"
                "workflow_llm_step_timeout:context_adjudication"
            ),
            "last_failed_action_outputs": {
                "subworkflow_invocation": {
                    "child_completed": False,
                    "child_error": (
                        "workflow_llm_step_timeout:LLM call timed out after 45s"
                    ),
                }
            },
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "completed",
                    "safe_to_claim_completion": True,
                    "requires_follow_up": False,
                    "evidence_payload": {"required_effect_count": 0},
                },
                "required_effects": [],
                "critic": {"summary": {}},
            },
            "aux_llm_calls": [],
            "invocations": [],
        },
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_nested_subworkflow_timeout",
        introspection_auto_apply_env="VON_TEST_AUTO_APPLY",
    )

    assert result.status == "success"
    assert result.outputs["completion_gate_safe_to_claim_completion"] is False
    assert result.outputs["completion_gate_requires_follow_up"] is True
    assert result.outputs["completion_gate_blocking_failure_codes"] == [
        "workflow_llm_step_timeout"
    ]


def test_agent_test_tool_calling_plan_uses_relation_summary(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        action_id="tool_calling.plan",
        data={
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_concept_id": "#V#michael_witbrock",
            "turn_expected_required_tools": ["get_text_relations_summary"],
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "model_for_stage": lambda _stage: "gemma4:e4b",
            "record_llm_call": lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("AgentTest relation plan should not call the LLM")
            ),
            "aux_llm_calls": [],
            "llm_calls": [],
        },
    )

    result = orchestrator._action_tool_calling_plan(request)

    assert result.status == "success"
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#michael_witbrock"},
        }
    ]
    assert result.outputs["required_prompt_tools"] == ["get_text_relations_summary"]
    assert result.outputs["agent_test_local_replay_support_only"] is True
    assert result.outputs["not_production_acceptance_evidence"] is True
    assert result.outputs["agent_test_local_relation_authority_source"] == (
        "turn_expected_required_tools"
    )
    assert request.data["aux_llm_calls"] == [
        {
            "type": "agent_test_local_relation_tool_plan",
            "stage": "tool_calling.plan",
            "tool": "get_text_relations_summary",
            "concept_id": "#V#michael_witbrock",
            "authority_source": "turn_expected_required_tools",
            "required_tools": ["get_text_relations_summary"],
            "reason_code": "agent_test_explicit_required_tool_authority",
            "local_replay_support_only": True,
            "not_production_acceptance_evidence": True,
        }
    ]


def test_agent_test_relation_shortcut_stands_down_for_type_incidence_contract(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "prompt": "What are key predicates for scientific papers in Vontology?",
            "user_prompt": (
                "What are key predicates for scientific papers in Vontology?"
            ),
            "user_concept_id": "#V#michael_witbrock",
            "required_prompt_tools": [
                "get_text_relations_summary",
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "turn_expected_outcome_contract": {
                "required_tools": [
                    "get_predicate_incidence",
                    "find_relations_with_argument",
                ],
                "target_type_ids": [
                    "#V#scholarly_article",
                    "#V#paper_on_arxiv",
                ],
            },
            "aux_llm_calls": [],
        },
    )

    result = orchestrator._agent_test_local_relation_plan_result(
        request=request,
        data=request.data,
        missing_tool_call_retry_attempts=0,
        missing_tool_call_retry_budget=1,
        missing_tool_call_retry_suppressed=False,
        missing_tool_call_retry_stop_reason=None,
        missing_tool_call_recovery_outcome=None,
    )

    assert result is None
    assert request.data["aux_llm_calls"] == [
        {
            "type": "agent_test_local_relation_shortcut_skipped",
            "stage": "tool_calling.plan",
            "reason_code": "type_targeted_predicate_incidence_contract",
            "target_type_ids": [
                "#V#scholarly_article",
                "#V#paper_on_arxiv",
            ],
            "required_tools": [
                "find_relations_with_argument",
                "get_predicate_incidence",
                "get_text_relations_summary",
            ],
            "local_replay_support_only": True,
            "not_production_acceptance_evidence": True,
        }
    ]


def test_agent_test_tool_calling_plan_does_not_trigger_from_prompt_words(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_concept_id": "#V#michael_witbrock",
            "aux_llm_calls": [],
        },
    )

    result = orchestrator._agent_test_local_relation_plan_result(
        request=request,
        data=request.data,
        missing_tool_call_retry_attempts=0,
        missing_tool_call_retry_budget=1,
        missing_tool_call_retry_suppressed=False,
        missing_tool_call_retry_stop_reason=None,
        missing_tool_call_recovery_outcome=None,
    )

    assert result is None
    assert request.data["aux_llm_calls"] == []


def test_agent_test_tool_calling_plan_ignores_explicit_authority_outside_agent_test(
    monkeypatch,
) -> None:
    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_concept_id": "#V#michael_witbrock",
            "turn_expected_required_tools": ["get_text_relations_summary"],
            "aux_llm_calls": [],
        },
    )

    result = orchestrator._agent_test_local_relation_plan_result(
        request=request,
        data=request.data,
        missing_tool_call_retry_attempts=0,
        missing_tool_call_retry_budget=1,
        missing_tool_call_retry_suppressed=False,
        missing_tool_call_retry_stop_reason=None,
        missing_tool_call_recovery_outcome=None,
    )

    assert result is None
    assert request.data["aux_llm_calls"] == []


def test_agent_test_tool_calling_backfill_uses_relation_summary(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "turn_expected_required_tools": ["get_text_relations_summary"],
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "model_for_stage": lambda _stage: (_ for _ in ()).throw(
                AssertionError("AgentTest relation backfill should not call the LLM")
            ),
            "record_llm_call": lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("AgentTest relation backfill should not call the LLM")
            ),
            "aux_llm_calls": [],
            "llm_calls": [],
            "invocations": [
                {
                    "tool": "get_text_relations_summary",
                    "status": "ok",
                    "payload": {
                        "concept_id": "#V#michael_witbrock",
                    },
                    "effective_payload": {
                        "concept_id": "#V#michael_witbrock",
                        "groups_found": 2,
                        "total_relations_scanned": 3,
                        "predicates": ["hasName", "#V#has_email"],
                        "groups": [
                            {"predicate": "hasName", "language": "en-NZ", "count": 2},
                            {"predicate": "#V#has_email", "language": "en", "count": 1},
                        ],
                    },
                }
            ],
        },
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.status == "success"
    assert result.outputs["more_tool_calls"] is False
    assert result.outputs["required_prompt_tools"] == ["get_text_relations_summary"]
    assert result.outputs["agent_test_local_replay_support_only"] is True
    assert result.outputs["not_production_acceptance_evidence"] is True
    assert result.outputs["agent_test_local_relation_authority_source"] == (
        "turn_expected_required_tools"
    )
    assert "hasName" in result.outputs["final_response"]
    assert "#V#has_email" in result.outputs["final_response"]
    assert "did not expose predicate names" not in result.outputs["final_response"]


def test_agent_test_tool_calling_backfill_does_not_trigger_from_prompt_words(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "aux_llm_calls": [],
            "invocations": [
                {
                    "tool": "get_text_relations_summary",
                    "status": "ok",
                    "payload": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {
                        "concept_id": "#V#michael_witbrock",
                        "predicates": ["hasName"],
                    },
                }
            ],
        },
    )

    result = orchestrator._agent_test_local_relation_backfill_result(
        request=request,
        data=request.data,
        missing_tool_call_retry_attempts=0,
        missing_tool_call_retry_budget=1,
        missing_tool_call_retry_suppressed=False,
        missing_tool_call_retry_stop_reason=None,
        missing_tool_call_recovery_outcome=None,
    )

    assert result is None
    assert request.data["aux_llm_calls"] == []
