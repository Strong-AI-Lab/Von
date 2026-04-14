from src.backend.workflows.definitions import (
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
)
from src.backend.workflows.engine import WORKFLOW_STEP_EXECUTION_MODE_LLM
from workflow_test_support import (
    build_authoritative_test_workflow_definition,
    build_test_conversation_turn_registry,
)


def test_tool_calling_workflow_includes_turn_execution_critic_and_gate() -> None:
    workflow = build_authoritative_test_workflow_definition(TOOL_CALLING_WORKFLOW_ID)
    assert workflow.initial_state == "preflight_requirements"
    assert "preflight_requirements" in workflow.states
    assert "respond" in workflow.states
    assert "postcondition_critic" in workflow.states
    assert "completion_gate" in workflow.states

    preflight = workflow.states["preflight_requirements"]
    assert preflight.actions[0].action_id == "tool_calling.preflight_requirements"
    assert any(
        t.to_state == "respond"
        and t.reason == "requirements_preflight_completed"
        for t in preflight.transitions
    )

    respond = workflow.states["respond"]
    assert respond.actions[0].action_id == "tool_calling.respond"
    assert respond.actions[0].execution_mode == "deterministic"
    assert any(
        t.to_state == "postcondition_critic" and t.reason == "response_ready"
        for t in respond.transitions
    )

    postcondition_critic = workflow.states["postcondition_critic"]
    assert postcondition_critic.actions[0].action_id == "turn_execution.critic"
    assert any(t.to_state == "completion_gate" for t in postcondition_critic.transitions)

    completion_gate = workflow.states["completion_gate"]
    assert completion_gate.actions[0].action_id == "turn_execution.completion_gate"
    assert any(
        t.to_state == "respond"
        and t.reason == "completion_gate_repeat_iteration"
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
    workflow = build_authoritative_test_workflow_definition(TURN_COMPLETION_GATE_WORKFLOW_ID)
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
    assert any(t.to_state == "completed" and t.reason == "decided" for t in decide.transitions)


def test_conversation_turn_workflow_uses_deterministic_critic_and_gate() -> None:
    workflow = build_authoritative_test_workflow_definition(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert workflow.initial_state == "routing"
    assert "narration" in workflow.states
    assert "critic" in workflow.states
    assert "completion_gate" in workflow.states
    assert "recovery_decision" in workflow.states
    assert "apply_recovery_retry" in workflow.states
    assert "apply_recovery_follow_up" in workflow.states
    assert "failed" in workflow.states

    narration = workflow.states["narration"]
    assert narration.actions[0].execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM
    narration_mappings = narration.metadata.get("tool_output_context_mappings") or []
    assert any(
        mapping.get("tool_output_field") == "final_response"
        and mapping.get("context_key") == "response_text"
        for mapping in narration_mappings
        if isinstance(mapping, dict)
    )

    critic = workflow.states["critic"]
    assert critic.actions[0].action_id == "turn_execution.critic"
    assert any(t.to_state == "completion_gate" for t in critic.transitions)

    completion_gate = workflow.states["completion_gate"]
    assert completion_gate.actions[0].action_id == "turn_execution.completion_gate"
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
        t.to_state == "apply_recovery_retry" and t.reason == "retry_execution"
        for t in recovery_decision.transitions
    )
    assert any(
        t.to_state == "apply_recovery_follow_up"
        and t.reason == "respond_with_follow_up"
        for t in recovery_decision.transitions
    )

    recovery_retry = workflow.states["apply_recovery_retry"]
    assert recovery_retry.actions[0].action_id == "workflow_control.context_set"
    retry_inputs = recovery_retry.actions[0].inputs
    retry_assignments = retry_inputs.get("assignments")
    assert isinstance(retry_assignments, list)
    assert {
        "key": "selected_workflow_id",
        "value_from_context": "turn_recovery_target_workflow_id",
    } in retry_assignments
    assert {"key": "response_text", "value": ""} in retry_assignments
    assert {"key": "final_response", "value": ""} in retry_assignments
    assert {"key": "current_response", "value": ""} in retry_assignments
    assert {"key": "selected_workflow_user_response", "value": ""} in retry_assignments
    assert any(
        t.to_state == "execution" and t.reason == "recovery_retry_prepared"
        for t in recovery_retry.transitions
    )

    recovery_follow_up = workflow.states["apply_recovery_follow_up"]
    assert recovery_follow_up.actions[0].action_id == "workflow_control.context_set"
    assert any(
        t.to_state == "failed" and t.reason == "recovery_follow_up_ready"
        for t in recovery_follow_up.transitions
    )


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
