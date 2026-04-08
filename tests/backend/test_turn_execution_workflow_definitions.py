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
        t.to_state == "failed" and t.reason == "follow_up_required"
        for t in completion_gate.transitions
    )
    assert any(
        t.to_state == "completed" and t.reason == "completion_gate_decided"
        for t in completion_gate.transitions
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
