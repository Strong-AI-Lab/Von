import re
from unittest.mock import MagicMock

from representation_intent_regression_helpers import patch_representation_profile_loader
from src.backend.workflows.action_registry import (
    ActionSpec,
    ActionRegistry,
    WorkflowEnvironment,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from src.backend.workflows.definitions import (
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.durable.subworkflow_actions import (
    register_subworkflow_actions,
)
from src.backend.workflows.durable.turn_execution_actions import (
    register_turn_execution_actions,
)
from src.backend.workflows.engine import WORKFLOW_STEP_EXECUTION_MODE_LLM
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from workflow_test_support import (
    build_authoritative_test_workflow_definition,
    build_test_conversation_turn_registry,
)


def _build_stub_kb_postcondition_critic_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
        initial_state="evaluate",
        states={
            "evaluate": WorkflowStateSpec(
                state_id="evaluate",
                actions=(WorkflowActionInvocation(action_id="turn_execution.critic"),),
                terminal=True,
                metadata={
                    "writes_context_keys": [
                        "turn_execution_record",
                        "required_effects",
                        "postcondition_checks",
                        "critic_summary",
                        "critic_verdict",
                        "completion_gate_decision",
                        "completion_gate_requires_follow_up",
                        "completion_gate_safe_to_claim_completion",
                    ]
                },
            )
        },
        termination_states=("evaluate",),
    )


def test_tool_calling_workflow_includes_turn_execution_critic_and_gate() -> None:
    workflow = build_authoritative_test_workflow_definition(TOOL_CALLING_WORKFLOW_ID)
    assert workflow.initial_state == "preflight_requirements"
    assert "preflight_requirements" in workflow.states
    assert "plan" in workflow.states
    assert "validate" in workflow.states
    assert "repair" in workflow.states
    assert "execute" in workflow.states
    assert "backfill" in workflow.states
    assert "postcondition_critic" in workflow.states
    assert "completion_gate" in workflow.states

    preflight = workflow.states["preflight_requirements"]
    assert preflight.actions[0].action_id == "tool_calling.preflight_requirements"
    assert any(
        t.to_state == "plan" and t.reason == "requirements_preflight_completed"
        for t in preflight.transitions
    )

    plan = workflow.states["plan"]
    assert plan.actions[0].action_id == "tool_calling.plan"
    assert any(
        t.to_state == "validate" and t.reason == "tool_calls_planned"
        for t in plan.transitions
    )
    assert any(
        t.to_state == "postcondition_critic"
        and t.reason == "planning_completed_without_tool_calls"
        for t in plan.transitions
    )

    validate = workflow.states["validate"]
    assert validate.actions[0].action_id == "tool_calling.validate"
    assert any(
        t.to_state == "repair" and t.reason == "validation_error_repair_required"
        for t in validate.transitions
    )
    assert any(
        t.to_state == "execute" and t.reason == "tool_calls_validated"
        for t in validate.transitions
    )

    repair = workflow.states["repair"]
    assert repair.actions[0].action_id == "tool_calling.repair"
    assert repair.actions[0].prompt_contract is None
    assert any(
        t.to_state == "validate" and t.reason == "repair_attempt_completed"
        for t in repair.transitions
    )

    execute = workflow.states["execute"]
    assert execute.actions[0].action_id == "tool_calling.execute"
    assert execute.actions[0].execution_mode == "deterministic"
    assert any(
        t.to_state == "synthesiser_context_prep"
        and t.reason == "tool_execution_completed"
        for t in execute.transitions
    )

    synthesiser_context_prep = workflow.states["synthesiser_context_prep"]
    assert synthesiser_context_prep.actions[0].action_id == "synthesiser_context_prep"
    assert any(
        t.to_state == "backfill" and t.reason == "synthesiser_context_prepared"
        for t in synthesiser_context_prep.transitions
    )

    backfill = workflow.states["backfill"]
    assert backfill.actions[0].action_id == "tool_calling.backfill"
    assert any(
        t.to_state == "validate" and t.reason == "backfill_requested_more_tool_calls"
        for t in backfill.transitions
    )
    assert any(
        t.to_state == "postcondition_critic" and t.reason == "response_ready"
        for t in backfill.transitions
    )

    postcondition_critic = workflow.states["postcondition_critic"]
    assert postcondition_critic.actions[0].action_id == "workflow_invoke_subworkflow"
    assert (
        postcondition_critic.actions[0].subworkflow_id
        == KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
    )
    assert any(
        t.to_state == "completion_gate" for t in postcondition_critic.transitions
    )

    completion_gate = workflow.states["completion_gate"]
    assert completion_gate.actions[0].action_id == "turn_execution.completion_gate"
    assert any(
        t.to_state == "plan" and t.reason == "completion_gate_repeat_iteration"
        for t in completion_gate.transitions
    )
    assert any(t.to_state == "completed" for t in completion_gate.transitions)


def test_chat_assistant_workflow_executes_via_tool_calling_contract() -> None:
    workflow = build_authoritative_test_workflow_definition(CHAT_ASSISTANT_WORKFLOW_ID)
    assert workflow.initial_state == "respond"
    assert "respond" in workflow.states
    assert "completed" in workflow.states

    respond = workflow.states["respond"]
    assert respond.actions[0].action_id == "tool_calling.respond"
    assert any(t.to_state == "completed" for t in respond.transitions)


def test_turn_completion_gate_workflow_fails_when_follow_up_is_required() -> None:
    workflow = build_authoritative_test_workflow_definition(
        TURN_COMPLETION_GATE_WORKFLOW_ID
    )
    assert workflow.initial_state == "decide"
    assert "decide" in workflow.states
    assert "completed" in workflow.states
    assert "failed" in workflow.states

    decide = workflow.states["decide"]
    assert decide.actions[0].action_id == "turn_execution.completion_gate"
    assert any(
        t.to_state == "failed" and t.reason == "follow_up_required"
        for t in decide.transitions
    )
    assert any(
        t.to_state == "completed" and t.reason == "decided" for t in decide.transitions
    )


def test_conversation_turn_workflow_uses_authoritative_critic_subworkflow_and_gate() -> (
    None
):
    workflow = build_authoritative_test_workflow_definition(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert workflow.initial_state == "workflow_experience_context_prelude"
    prelude = workflow.states["workflow_experience_context_prelude"]
    assert prelude.actions[0].action_id == "workflow_invoke_subworkflow"
    assert prelude.actions[0].subworkflow_id == (
        WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID
    )
    assert any(t.to_state == "expected_outcome_inference" for t in prelude.transitions)
    assert "expected_outcome_inference" in workflow.states
    assert "selector_preparation" in workflow.states
    assert "selector_decision" in workflow.states
    assert "routing" in workflow.states
    assert "narration" in workflow.states
    assert "critic" in workflow.states
    assert "completion_gate" in workflow.states
    assert "recovery_decision" in workflow.states
    assert "apply_recovery_retry" in workflow.states
    assert "apply_recovery_tool_batch" in workflow.states
    assert "apply_recovery_answer" in workflow.states
    assert "apply_recovery_follow_up" in workflow.states
    assert "failed" in workflow.states

    expected_outcome = workflow.states["expected_outcome_inference"]
    expected_outcome_action = expected_outcome.actions[0]
    assert expected_outcome_action.action_id == "llm.action"
    assert expected_outcome_action.execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM
    expected_outcome_policy = expected_outcome_action.validation_policy or {}
    assert expected_outcome_policy.get("output_format") == "json_value"
    assert "expected_outcome_summary" in (
        expected_outcome_policy.get("json_field_defaults") or {}
    )
    assert "expected_outcome_summary" in (
        expected_outcome_policy.get("required_json_fields") or []
    )
    assert "required_tools" in (
        expected_outcome_policy.get("json_field_defaults") or {}
    )
    assert "required_tools" in (
        expected_outcome_policy.get("required_json_fields") or []
    )
    expected_outcome_prompt_contract = expected_outcome_action.prompt_contract
    assert isinstance(expected_outcome_prompt_contract, dict)
    assert expected_outcome_prompt_contract.get("requested_prompt_concept_ids") == [
        "#V#prompt_turn_execution_expected_outcome_inference"
    ]
    expected_outcome_mappings = (
        expected_outcome.metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_expected_outcome_summary"
        and mapping.get("tool_output_field")
        == "validated_json.expected_outcome_summary"
        for mapping in expected_outcome_mappings
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_answering_guidance"
        and mapping.get("tool_output_field") == "validated_json.answering_guidance"
        for mapping in expected_outcome_mappings
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_expected_required_tools"
        and mapping.get("tool_output_field") == "validated_json.required_tools"
        for mapping in expected_outcome_mappings
    )
    assert "turn_expected_required_tools" in (
        expected_outcome.metadata.get("writes_context_keys") or []
    )
    assert any(
        t.to_state == "selector_preparation" and t.reason == "expected_outcome_inferred"
        for t in expected_outcome.transitions
    )

    selector_preparation = workflow.states["selector_preparation"]
    assert (
        selector_preparation.actions[0].action_id
        == "turn_execution.prepare_selector_context"
    )
    assert any(
        t.to_state == "selector_decision" and t.reason == "selector_prompt_available"
        for t in selector_preparation.transitions
    )
    assert any(
        t.to_state == "routing" and t.reason == "selector_prompt_unavailable"
        for t in selector_preparation.transitions
    )

    selector_decision = workflow.states["selector_decision"]
    selector_decision_action = selector_decision.actions[0]
    assert selector_decision_action.action_id == "llm.action"
    assert selector_decision_action.execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM
    selector_decision_policy = selector_decision_action.llm_policy or {}
    assert selector_decision_policy.get("prompt_text_context_key") == (
        "selector_call_prompt_text"
    )
    assert selector_decision_policy.get("prompt_id_context_key") == "selector_prompt_id"
    assert selector_decision_policy.get("context_messages_context_key") == (
        "selector_context_messages"
    )
    assert selector_decision_policy.get("context_lineage_context_key") == (
        "selector_context_lineage"
    )
    selector_decision_mappings = (
        selector_decision.metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "selector_raw_response"
        and mapping.get("tool_output_field") == "final_response"
        for mapping in selector_decision_mappings
    )
    assert any(
        t.to_state == "routing" and t.reason == "selector_decided"
        for t in selector_decision.transitions
    )

    routing = workflow.states["routing"]
    assert routing.actions[0].action_id == "turn_execution.route"
    assert any(
        t.to_state == "execution" and t.reason == "routing_resolved"
        for t in routing.transitions
    )

    narration = workflow.states["narration"]
    assert narration.actions[0].execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM
    narration_context_fields = (narration.actions[0].llm_policy or {}).get(
        "context_fields"
    )
    assert isinstance(narration_context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_expected_outcome_summary"
        for field in narration_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_expected_grounding_requirement"
        for field in narration_context_fields
    )
    narration_mappings = narration.metadata.get("tool_output_context_mappings") or []
    assert any(
        mapping.get("tool_output_field") == "final_response"
        and mapping.get("context_key") == "response_text"
        for mapping in narration_mappings
        if isinstance(mapping, dict)
    )

    critic = workflow.states["critic"]
    assert critic.actions[0].action_id == "workflow_invoke_subworkflow"
    assert (
        critic.actions[0].subworkflow_id == KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
    )
    critic_inputs = critic.actions[0].inputs
    for input_key in (
        "prompt",
        "response_text",
        "final_response",
        "workflow_routing",
        "selected_workflow_trace",
        "completion_report",
        "invocations",
        "aux_llm_calls",
    ):
        assert critic_inputs.get(input_key, {}).get("$context_key") == input_key
    critic_contract = critic.metadata.get("subworkflow_contract") or {}
    critic_input_mappings = critic_contract.get("input_mappings") or []
    assert any(
        mapping.get("parent_context_key") == "completion_report"
        and mapping.get("child_input_key") == "completion_report"
        for mapping in critic_input_mappings
        if isinstance(mapping, dict)
    )
    assert any(
        mapping.get("parent_context_key") == "selected_workflow_trace"
        and mapping.get("child_input_key") == "selected_workflow_trace"
        for mapping in critic_input_mappings
        if isinstance(mapping, dict)
    )
    critic_mappings = critic.metadata.get("tool_output_context_mappings") or []
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_execution_record"
        and mapping.get("tool_output_field") == "result.turn_execution_record"
        for mapping in critic_mappings
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "critic_verdict"
        and mapping.get("tool_output_field") == "result.critic_verdict"
        for mapping in critic_mappings
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "completion_gate_decision"
        and mapping.get("tool_output_field") == "result.completion_gate_decision"
        for mapping in critic_mappings
    )
    assert any(t.to_state == "completion_gate" for t in critic.transitions)

    completion_gate = workflow.states["completion_gate"]
    assert completion_gate.actions[0].action_id == "turn_execution.completion_gate"
    completion_gate_mappings = (
        completion_gate.metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        mapping.get("context_key") == "completion_gate_evidence_payload"
        and mapping.get("tool_output_field") == "completion_gate_evidence_payload"
        for mapping in completion_gate_mappings
        if isinstance(mapping, dict)
    )
    assert "completion_gate_requires_follow_up" in (
        completion_gate.metadata.get("writes_context_keys") or []
    )
    assert any(
        t.to_state == "recovery_decision" and t.reason == "follow_up_required"
        for t in completion_gate.transitions
    )
    assert any(
        t.to_state == "completed" and t.reason == "completion_gate_decided"
        for t in completion_gate.transitions
    )

    recovery_decision = workflow.states["recovery_decision"]
    recovery_action = recovery_decision.actions[0]
    assert recovery_action.action_id == "llm.action"
    assert recovery_action.execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM
    assert recovery_action.validation_policy == {"output_format": "json_value"}
    prompt_contract = recovery_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("requested_prompt_concept_ids") == [
        "#V#prompt_turn_execution_recovery_decision"
    ]
    recovery_context_fields = (recovery_action.llm_policy or {}).get("context_fields")
    assert isinstance(recovery_context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_expected_outcome_summary"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "workflow_discovery_result"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "completion_gate_repeat_eligible"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_recovery_last_target_workflow_id"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_recovery_tool_batch_execution"
        for field in recovery_context_fields
    )

    recovery_mappings = (
        recovery_decision.metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_next_action_type"
        and mapping.get("tool_output_field")
        == "validated_json.turn_next_action.action_type"
        for mapping in recovery_mappings
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_recovery_decision"
        and mapping.get("tool_output_field")
        == "validated_json.turn_next_action.action_type"
        for mapping in recovery_mappings
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_next_action_tool_calls"
        and mapping.get("tool_output_field")
        == "validated_json.turn_next_action.tool_calls"
        for mapping in recovery_mappings
    )
    assert any(
        t.to_state == "apply_recovery_retry" and t.reason == "retry_execution"
        for t in recovery_decision.transitions
    )
    retry_transition = next(
        t for t in recovery_decision.transitions if t.reason == "retry_execution"
    )
    retry_conditions = retry_transition.condition_spec.get("conditions") or []
    assert {
        "kind": "context_flag",
        "key": "completion_gate_repeat_eligible",
        "expected": True,
    } in retry_conditions
    assert any(
        t.to_state == "apply_recovery_tool_batch" and t.reason == "execute_tool_batch"
        for t in recovery_decision.transitions
    )
    assert any(
        t.to_state == "apply_recovery_answer" and t.reason == "respond_with_answer"
        for t in recovery_decision.transitions
    )
    assert any(
        t.to_state == "apply_recovery_follow_up"
        and t.reason == "respond_with_follow_up"
        for t in recovery_decision.transitions
    )

    recovery_retry = workflow.states["apply_recovery_retry"]
    assert (
        recovery_retry.actions[0].action_id == "turn_execution.prepare_recovery_retry"
    )
    assert any(
        t.to_state == "execution" and t.reason == "recovery_retry_prepared"
        for t in recovery_retry.transitions
    )

    recovery_tool_batch = workflow.states["apply_recovery_tool_batch"]
    assert (
        recovery_tool_batch.actions[0].action_id == "turn_execution.execute_tool_batch"
    )
    tool_batch_inputs = recovery_tool_batch.actions[0].inputs
    assert (
        tool_batch_inputs.get("tool_calls_context_key") == "turn_next_action_tool_calls"
    )
    assert tool_batch_inputs.get("tool_batch_cap") == 4
    assert any(
        t.to_state == "critic" and t.reason == "recovery_tool_batch_executed"
        for t in recovery_tool_batch.transitions
    )

    recovery_answer = workflow.states["apply_recovery_answer"]
    assert recovery_answer.actions[0].action_id == "workflow_control.context_set"
    answer_inputs = recovery_answer.actions[0].inputs
    answer_assignments = answer_inputs.get("assignments")
    assert isinstance(answer_assignments, list)
    assert {
        "key": "response_text",
        "value_from_context": "turn_next_action_response_text",
    } in answer_assignments
    assert {
        "key": "selected_workflow_user_response",
        "value_from_context": "turn_next_action_response_text",
    } in answer_assignments
    assert any(
        t.to_state == "completed" and t.reason == "recovery_answer_ready"
        for t in recovery_answer.transitions
    )

    recovery_follow_up = workflow.states["apply_recovery_follow_up"]
    assert recovery_follow_up.actions[0].action_id == "workflow_control.context_set"
    follow_up_assignments = recovery_follow_up.actions[0].inputs.get("assignments")
    assert isinstance(follow_up_assignments, list)
    assert {
        "key": "response_text",
        "value_from_context": "turn_next_action_response_text",
    } in follow_up_assignments
    assert any(
        t.to_state == "failed" and t.reason == "recovery_follow_up_ready"
        for t in recovery_follow_up.transitions
    )


def test_conversation_turn_critic_subworkflow_receives_selected_workflow_context() -> (
    None
):
    source = build_authoritative_test_workflow_definition(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    completion_gate = source.states["completion_gate"]
    clipped_completion_gate = WorkflowStateSpec(
        state_id="completion_gate",
        actions=completion_gate.actions,
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda context: bool(
                    context.get("completion_gate_requires_follow_up")
                ),
                reason="follow_up_required",
            ),
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda _context: True,
                reason="decided",
            ),
        ),
        terminal=False,
        metadata=completion_gate.metadata,
    )
    workflow = WorkflowDefinition(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        initial_state="critic",
        states={
            "critic": source.states["critic"],
            "completion_gate": clipped_completion_gate,
            "completed": WorkflowStateSpec(state_id="completed", terminal=True),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
        termination_states=("completed", "failed"),
    )
    definitions = {
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID: workflow,
        KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID: (
            _build_stub_kb_postcondition_critic_definition()
        ),
    }
    registry = ActionRegistry()
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )
    register_turn_execution_actions(registry)

    failed_execution_summary = {
        "schema_version": "workflow_execution_summary.v1",
        "workflow_id": "#V#arxiv_paper_representation_workflow",
        "completed": False,
        "effective_completed": False,
        "terminal_status": "completed",
        "final_state": "#V#workflow_step_arxiv_paper_representation_workflow_failed",
        "terminal_success_contract": {
            "schema_version": "workflow_terminal_success_contract.v1",
            "success_statuses": ["completed"],
            "required_summary_fields": [
                "workflow_id",
                "terminal_status",
                "final_state",
                "completed",
            ],
            "require_terminal_status": True,
            "require_final_state": True,
            "require_completed_true": True,
        },
        "terminal_success_evaluation": {
            "schema_version": "workflow_terminal_success_evaluation.v1",
            "success": False,
            "terminal_status": "completed",
            "completed": False,
            "final_state": "#V#workflow_step_arxiv_paper_representation_workflow_failed",
            "failure_codes": ["contracted_workflow_completed_flag_false"],
            "decision_reason": (
                "Contracted workflow terminal-success contract requirements "
                "were not met."
            ),
        },
        "step_result_envelope_count": 10,
        "action_started_count": 10,
        "action_completed_count": 10,
        "action_success_count": 10,
        "action_failure_count": 0,
        "action_unknown_count": 0,
        "runtime_event_count": 1,
        "terminal_effect_count": 0,
        "terminal_effects": [],
    }

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        workflow,
        environment=WorkflowEnvironment(
            llm_client=object(),
            user_namespace="#V#user@org",
        ),
        data={
            "prompt": "https://arxiv.org/abs/2604.22937",
            "user_prompt": "https://arxiv.org/abs/2604.22937",
            "response_text": "Linked file copy.",
            "final_response": "Linked file copy.",
            "current_response": "Linked file copy.",
            "workflow_routing": {
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
                "source": "selector",
            },
            "selected_workflow_trace": {
                "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                "child_workflow_completed": False,
                "child_workflow_final_state": (
                    "#V#workflow_step_arxiv_paper_representation_workflow_failed"
                ),
                "completion_report_source": "child_completion_report",
                "workflow_execution_summary": failed_execution_summary,
            },
            "completion_report": failed_execution_summary,
            "invocations": [],
            "aux_llm_calls": [],
            "conversation_session_id": "session-critic-join",
            "turn_id": "req-critic-join",
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
        },
    )

    assert result.completed is True
    assert result.final_state == "failed"
    assert result.data["completion_gate_decision"] == "failed"
    assert result.data["completion_gate_requires_follow_up"] is True
    assert result.data["completion_gate_safe_to_claim_completion"] is False
    assert result.data["completion_gate_blocking_failure_codes"] == [
        "contracted_workflow_completed_flag_false"
    ]
    gate_evidence = result.data["completion_gate_evidence_payload"]
    assert gate_evidence["evaluation_basis"] == (
        "required_effects_postcondition_checks_and_execution_signals"
    )
    assert gate_evidence["execution_signal_blocker"]["source"] == (
        "workflow_terminal_success_contract"
    )
    custom_execution = result.data["turn_execution_record"]["execution"]["summary"][
        "custom_workflow_execution"
    ]
    assert custom_execution["terminal_success_evaluation"]["success"] is False


def test_kb_mutation_postcondition_critic_workflow_uses_prompt_backed_judgement() -> (
    None
):
    workflow = build_authoritative_test_workflow_definition(
        KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
    )

    assert workflow.initial_state == "build_evidence"
    assert "build_evidence" in workflow.states
    assert "evaluate_authoritative_prompt" in workflow.states
    assert "finalise_authoritative" in workflow.states
    assert "finalise_fallback" in workflow.states
    assert "completed" in workflow.states

    build_evidence = workflow.states["build_evidence"]
    build_evidence_action = build_evidence.actions[0]
    assert build_evidence_action.action_id == "turn_execution.critic"
    assert build_evidence_action.inputs.get("emit_default_critic_verdict") is False
    assert any(
        t.to_state == "evaluate_authoritative_prompt" and t.reason == "evidence_built"
        for t in build_evidence.transitions
    )

    evaluate_authoritative_prompt = workflow.states["evaluate_authoritative_prompt"]
    evaluate_action = evaluate_authoritative_prompt.actions[0]
    assert evaluate_action.action_id == "llm.action"
    assert evaluate_action.execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM
    assert evaluate_action.validation_policy == {"output_format": "json_value"}
    prompt_contract = evaluate_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("requested_prompt_concept_ids") == [
        "#V#prompt_turn_execution_postcondition_critic"
    ]
    context_fields = (evaluate_action.llm_policy or {}).get("context_fields")
    assert isinstance(context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_execution_critic_evidence_bundle"
        for field in context_fields
    )
    evaluate_mappings = (
        evaluate_authoritative_prompt.metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "critic_verdict"
        and mapping.get("tool_output_field") == "validated_json"
        for mapping in evaluate_mappings
    )
    assert any(
        t.to_state == "finalise_authoritative"
        and t.reason == "authoritative_critic_decided"
        for t in evaluate_authoritative_prompt.transitions
    )
    assert any(
        t.to_state == "finalise_fallback" and t.reason == "on_failure"
        for t in evaluate_authoritative_prompt.transitions
    )

    finalise_authoritative = workflow.states["finalise_authoritative"]
    assert finalise_authoritative.actions[0].action_id == "turn_execution.critic"

    finalise_fallback = workflow.states["finalise_fallback"]
    assert finalise_fallback.actions[0].action_id == "turn_execution.critic"


def test_test_registry_includes_turn_execution_workflows() -> None:
    registry = build_test_conversation_turn_registry()

    assert registry.has(CHAT_BUTTONIFY_WORKFLOW_ID)
    assert registry.has(CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID)
    assert registry.has(KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID)
    assert registry.has(TURN_COMPLETION_GATE_WORKFLOW_ID)
    assert registry.has(CONVERSATION_TURN_EXECUTION_WORKFLOW_ID)


def test_concept_suggestion_preflight_workflow_has_single_guardrailed_step() -> None:
    workflow = build_authoritative_test_workflow_definition(
        CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID
    )
    assert workflow.initial_state == "suggest"
    assert "suggest" in workflow.states
    assert "completed" in workflow.states

    suggest = workflow.states["suggest"]
    assert suggest.actions[0].action_id == "preflight.specialised_suggest"
    assert any(t.to_state == "completed" for t in suggest.transitions)


def test_buttonify_workflow_exposes_transformation_states() -> None:
    workflow = build_authoritative_test_workflow_definition(CHAT_BUTTONIFY_WORKFLOW_ID)
    assert workflow.initial_state == "assess_input"
    assert "assess_input" in workflow.states
    assert "select_prompt" in workflow.states
    assert "extract_options" in workflow.states
    assert "completed" in workflow.states

    assess = workflow.states["assess_input"]
    assert assess.actions[0].action_id == "buttonify.assess_input"
    assert any(t.to_state == "select_prompt" for t in assess.transitions)


def test_conversation_turn_recovery_can_complete_with_direct_answer() -> None:
    workflow = build_authoritative_test_workflow_definition(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    recovery_definition = WorkflowDefinition(
        workflow_id=workflow.workflow_id,
        initial_state="recovery_decision",
        states={
            "recovery_decision": WorkflowStateSpec(
                state_id="recovery_decision",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        inputs=workflow.states["recovery_decision"].actions[0].inputs,
                        execution_mode=WORKFLOW_STEP_EXECUTION_MODE_LLM,
                        prompt_contract={
                            "prompt_text": "Return JSON only with a turn_next_action."
                        },
                        llm_policy=workflow.states["recovery_decision"]
                        .actions[0]
                        .llm_policy,
                        validation_policy=workflow.states["recovery_decision"]
                        .actions[0]
                        .validation_policy,
                    ),
                ),
                transitions=workflow.states["recovery_decision"].transitions,
                terminal=workflow.states["recovery_decision"].terminal,
                metadata=workflow.states["recovery_decision"].metadata,
            ),
            **{
                state_id: workflow.states[state_id]
                for state_id in (
                    "apply_recovery_retry",
                    "apply_recovery_answer",
                    "apply_recovery_follow_up",
                    "completed",
                    "failed",
                )
            },
        },
        termination_states=workflow.termination_states,
        purpose=workflow.purpose,
        metadata=workflow.metadata,
    )

    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _wid: None)
    llm_client = MagicMock()
    llm_client.generate.return_value = (
        '{"turn_next_action":{"action_type":"respond_with_answer",'
        '"target_workflow_id":null,'
        '"response_text":"You are Michael Witbrock.",'
        '"tool_calls":null},'
        '"reasoning":"Authenticated actor context already grounds the answer."}'
    )

    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        recovery_definition,
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "completed"
    assert result.data["turn_next_action_type"] == "respond_with_answer"
    assert result.data["turn_recovery_last_decision"] == "respond_with_answer"
    assert result.data["response_text"] == "You are Michael Witbrock."
    assert result.data["final_response"] == "You are Michael Witbrock."
    assert result.data["selected_workflow_user_response"] == "You are Michael Witbrock."


def test_conversation_turn_recovery_can_execute_direct_tool_batch() -> None:
    workflow = build_authoritative_test_workflow_definition(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    recovery_definition = WorkflowDefinition(
        workflow_id=workflow.workflow_id,
        initial_state="recovery_decision",
        states={
            "recovery_decision": WorkflowStateSpec(
                state_id="recovery_decision",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        inputs=workflow.states["recovery_decision"].actions[0].inputs,
                        execution_mode=WORKFLOW_STEP_EXECUTION_MODE_LLM,
                        prompt_contract={
                            "prompt_text": "Return JSON only with a turn_next_action."
                        },
                        llm_policy=workflow.states["recovery_decision"]
                        .actions[0]
                        .llm_policy,
                        validation_policy=workflow.states["recovery_decision"]
                        .actions[0]
                        .validation_policy,
                    ),
                ),
                transitions=workflow.states["recovery_decision"].transitions,
                terminal=workflow.states["recovery_decision"].terminal,
                metadata=workflow.states["recovery_decision"].metadata,
            ),
            **{
                state_id: workflow.states[state_id]
                for state_id in (
                    "apply_recovery_tool_batch",
                    "narration",
                    "critic",
                    "completion_gate",
                    "completed",
                    "failed",
                )
            },
        },
        termination_states=workflow.termination_states,
        purpose=workflow.purpose,
        metadata=workflow.metadata,
    )

    captured_payloads: list[dict[str, object]] = []

    def _handle_lookup_current_user(
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        captured_payloads.append(dict(request.inputs))
        return WorkflowActionResult(
            outputs={
                "result": {
                    "response_text": "The current authenticated user is Michael Witbrock."
                }
            }
        )

    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _wid: None)
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _build_stub_kb_postcondition_critic_definition()
            if workflow_id == KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
            else None
        ),
    )
    register_turn_execution_actions(registry)
    registry.register_if_absent(
        ActionSpec(
            action_id="test.lookup_current_user",
            handler=_handle_lookup_current_user,
            description="Return the current authenticated user for testing.",
        )
    )

    llm_client = MagicMock()
    llm_client.generate.side_effect = [
        (
            '{"turn_next_action":{"action_type":"execute_tool_batch",'
            '"target_workflow_id":null,'
            '"response_text":null,'
            '"tool_calls":[{"tool":"test.lookup_current_user","arguments":{}}]},'
            '"reasoning":"A direct identity lookup is enough."}'
        ),
        "The current authenticated user is Michael Witbrock.",
    ]

    result = WorkflowExecutor(registry=registry, max_transitions=12).run(
        recovery_definition,
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={"user_prompt": "tell me about the current user"},
    )

    assert result.completed is True
    assert result.final_state == "completed"
    assert captured_payloads == [{}]
    assert result.data["turn_next_action_type"] == "execute_tool_batch"
    assert result.data["turn_recovery_last_decision"] == "execute_tool_batch"
    assert result.data["selected_workflow_user_response"] == (
        "The current authenticated user is Michael Witbrock."
    )
    assert result.data["response_text"] == (
        "The current authenticated user is Michael Witbrock."
    )
    assert result.data["final_response"] == (
        "The current authenticated user is Michael Witbrock."
    )
    completion_report = result.data["completion_report"]
    assert completion_report["action_type"] == "execute_tool_batch"
    assert completion_report["executed_tool_call_count"] == 1
    assert result.data["invocations"][0]["tool"] == "test.lookup_current_user"


def test_conversation_turn_recovery_retry_progresses_across_multiple_prompt_targets(
    monkeypatch,
) -> None:
    patch_representation_profile_loader(monkeypatch)
    workflow = build_authoritative_test_workflow_definition(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )

    def _fake_selected_workflow_definition() -> WorkflowDefinition:
        return WorkflowDefinition(
            workflow_id="#V#fake_multi_target_paper_workflow",
            initial_state="materialise",
            states={
                "materialise": WorkflowStateSpec(
                    state_id="materialise",
                    actions=(
                        WorkflowActionInvocation(action_id="test.materialise_target"),
                    ),
                    terminal=True,
                )
            },
            termination_states=("materialise",),
        )

    definitions = {
        "#V#fake_multi_target_paper_workflow": _fake_selected_workflow_definition(),
        KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID: _build_stub_kb_postcondition_critic_definition(),
    }
    execution_to_narration = next(
        transition
        for transition in workflow.states["execution"].transitions
        if transition.to_state == "narration"
    )
    recovery_definition = WorkflowDefinition(
        workflow_id=workflow.workflow_id,
        initial_state="execution",
        states={
            "execution": WorkflowStateSpec(
                state_id="execution",
                actions=workflow.states["execution"].actions,
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="critic",
                        condition=execution_to_narration.condition,
                        condition_spec=execution_to_narration.condition_spec,
                        description=execution_to_narration.description,
                        reason=execution_to_narration.reason,
                    ),
                ),
                terminal=False,
                metadata=workflow.states["execution"].metadata,
            ),
            "critic": workflow.states["critic"],
            "completion_gate": WorkflowStateSpec(
                state_id="completion_gate",
                actions=(
                    WorkflowActionInvocation(
                        action_id="test.stub_retry_completion_gate"
                    ),
                ),
                transitions=workflow.states["completion_gate"].transitions,
                terminal=False,
                metadata=workflow.states["completion_gate"].metadata,
            ),
            "recovery_decision": WorkflowStateSpec(
                state_id="recovery_decision",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        inputs=workflow.states["recovery_decision"].actions[0].inputs,
                        execution_mode=WORKFLOW_STEP_EXECUTION_MODE_LLM,
                        prompt_contract={
                            "prompt_text": "Return JSON only with a turn_next_action."
                        },
                        llm_policy=workflow.states["recovery_decision"]
                        .actions[0]
                        .llm_policy,
                        validation_policy=workflow.states["recovery_decision"]
                        .actions[0]
                        .validation_policy,
                    ),
                ),
                transitions=workflow.states["recovery_decision"].transitions,
                terminal=workflow.states["recovery_decision"].terminal,
                metadata=workflow.states["recovery_decision"].metadata,
            ),
            "apply_recovery_retry": workflow.states["apply_recovery_retry"],
            "apply_recovery_answer": workflow.states["apply_recovery_answer"],
            "apply_recovery_follow_up": workflow.states["apply_recovery_follow_up"],
            "completed": workflow.states["completed"],
            "failed": workflow.states["failed"],
        },
        termination_states=workflow.termination_states,
        purpose=workflow.purpose,
        metadata=workflow.metadata,
    )

    processed_targets: list[str] = []

    def _handle_retry_completion_gate(
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        from src.backend.services.arxiv_paper_link_service import (
            extract_arxiv_id_candidates,
        )

        prompt_text = str(request.data.get("prompt") or "")
        target_tokens = re.findall(r"https?://\S+", prompt_text)
        required_effects = []
        unresolved = False
        for index, target_token in enumerate(target_tokens, start=1):
            arxiv_ids = extract_arxiv_id_candidates(target_token)
            target_arxiv_id = arxiv_ids[0] if arxiv_ids else target_token
            satisfied = target_arxiv_id in processed_targets
            if not satisfied:
                unresolved = True
            required_effects.append(
                {
                    "effect_id": f"effect_prompt_target_{index}",
                    "effect_type": "scholarly_representation",
                    "status": "satisfied" if satisfied else "not_satisfied",
                    "targets": [target_token],
                }
            )

        return WorkflowActionResult(
            outputs={
                "final_response": request.data.get("final_response") or "",
                "required_effects": required_effects,
                "completion_gate_decision": (
                    "escalation_required" if unresolved else "completed"
                ),
                "completion_gate_decision_reason": (
                    "Synthetic prompt targets remain unresolved."
                    if unresolved
                    else "All synthetic prompt targets were satisfied."
                ),
                "completion_gate_blocking_effect_ids": [
                    effect["effect_id"]
                    for effect in required_effects
                    if effect["status"] != "satisfied"
                ],
                "completion_gate_blocking_failure_codes": (
                    ["required_effects_unresolved"] if unresolved else []
                ),
                "completion_gate_requires_follow_up": unresolved,
                "completion_gate_safe_to_claim_completion": not unresolved,
                "completion_gate_repeat_eligible": unresolved,
                "completion_gate_unresolved_preconditions": [
                    effect
                    for effect in required_effects
                    if effect["status"] != "satisfied"
                ],
                "completion_gate_evidence_payload": {
                    "required_effects": required_effects,
                    "synthetic_gate": True,
                },
                "completion_gate_terminal_outcome": (
                    "retrying" if unresolved else "completed"
                ),
                "completion_gate_repeat_iteration": unresolved,
                "completion_gate_loop_retry_reason": (
                    "Synthetic prompt targets remain unresolved."
                    if unresolved
                    else None
                ),
                "completion_gate_loop_stop_reason": None,
                "completion_gate_loop_attempts": (
                    int(request.data.get("completion_gate_loop_attempts") or 0) + 1
                    if unresolved
                    else int(request.data.get("completion_gate_loop_attempts") or 0)
                ),
                "completion_gate_loop_max_attempts": int(
                    request.data.get("completion_gate_loop_max_attempts") or 1
                ),
                "completion_gate_escalation_signal": False,
                "completion_gate_escalation_reason": None,
            }
        )

    def _handle_materialise_target(
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        from src.backend.services.arxiv_paper_link_service import (
            extract_arxiv_id_candidates,
        )

        raw_target = request.data.get("arxiv_id") or request.data.get("source_uri")
        if not isinstance(raw_target, str) or not raw_target.strip():
            prompt_candidates = extract_arxiv_id_candidates(request.data.get("prompt"))
            raw_target = prompt_candidates[0] if prompt_candidates else ""
        target = str(raw_target).strip()
        processed_targets.append(target)
        return WorkflowActionResult(
            outputs={
                "response_text": f"Handled {target}",
                "completion_report": {
                    "schema_version": "selected_workflow_result.v1",
                    "workflow_id": "#V#fake_multi_target_paper_workflow",
                    "response_text": f"Handled {target}",
                },
                "invocations": [
                    {
                        "tool": "download_paper",
                        "arguments": {"arxiv_id": target},
                        "payload": {"success": True, "arxiv_id": target},
                    },
                    {
                        "tool": "materialise_scholarly_representation_for_file_copy",
                        "arguments": {"arxiv_id": target},
                        "payload": {
                            "success": True,
                            "arxiv_id": target,
                            "scholarly_representation": {
                                "attempted": True,
                                "verified": True,
                            },
                        },
                    },
                ],
                "tool_messages": [],
            }
        )

    registry = ActionRegistry()
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )
    register_turn_execution_actions(registry)
    registry.register_if_absent(
        ActionSpec(
            action_id="test.materialise_target",
            handler=_handle_materialise_target,
            description="Materialise one synthetic paper target for recovery-loop tests.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id="test.stub_retry_completion_gate",
            handler=_handle_retry_completion_gate,
            description="Return recovery-loop gate signals for unresolved prompt targets.",
        )
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        lambda: registry,
    )

    llm_client = MagicMock()
    llm_client.generate.return_value = (
        '{"turn_next_action":{"action_type":"retry_execution",'
        '"target_workflow_id":"#V#fake_multi_target_paper_workflow",'
        '"response_text":null,'
        '"tool_calls":null},'
        '"reasoning":"Another unresolved paper target remains, so retry the same workflow on the next target."}'
    )

    result = WorkflowExecutor(registry=registry, max_transitions=16).run(
        recovery_definition,
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={
            "prompt": (
                "eprint version: https://arxiv.org/abs/2310.03714\n"
                "arXiv preprint version: https://arxiv.org/abs/2308.03688"
            ),
            "user_prompt": (
                "eprint version: https://arxiv.org/abs/2310.03714\n"
                "arXiv preprint version: https://arxiv.org/abs/2308.03688"
            ),
            "selected_workflow_id": "#V#fake_multi_target_paper_workflow",
        },
    )

    assert result.completed is True
    assert result.final_state == "completed"
    assert processed_targets == ["2310.03714", "2308.03688"]
    assert result.data["turn_recovery_last_target_token"] == (
        "https://arxiv.org/abs/2308.03688"
    )
    assert result.data["selected_workflow_id"] == "#V#fake_multi_target_paper_workflow"
    assert len(result.data["invocations"]) == 4
    assert [
        invocation["arguments"]["arxiv_id"] for invocation in result.data["invocations"]
    ] == [
        "2310.03714",
        "2310.03714",
        "2308.03688",
        "2308.03688",
    ]
    required_effects = result.data["required_effects"]
    assert [effect["status"] for effect in required_effects] == [
        "satisfied",
        "satisfied",
    ]
    assert result.data["completion_gate_requires_follow_up"] is False
