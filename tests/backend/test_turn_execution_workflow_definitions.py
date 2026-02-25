from src.backend.workflows.definitions import (
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    build_chat_buttonify_workflow,
    build_concept_suggestion_preflight_workflow,
    build_tool_calling_workflow,
    register_default_workflows,
)
from src.backend.workflows.workflow_registry import WorkflowRegistry


def test_tool_calling_workflow_includes_turn_execution_critic_and_gate() -> None:
    workflow = build_tool_calling_workflow()

    assert "postcondition_critic" in workflow.states
    assert "completion_gate" in workflow.states

    plan = workflow.states["plan"]
    assert any(
        t.to_state == "postcondition_critic" and t.reason == "direct_response"
        for t in plan.transitions
    )

    validate = workflow.states["validate"]
    assert any(
        t.to_state == "postcondition_critic" and t.reason == "validation_error"
        for t in validate.transitions
    )

    backfill = workflow.states["backfill"]
    assert any(t.to_state == "postcondition_critic" for t in backfill.transitions)

    postcondition_critic = workflow.states["postcondition_critic"]
    assert postcondition_critic.actions[0].action_id == "turn_execution.critic"
    assert any(t.to_state == "completion_gate" for t in postcondition_critic.transitions)

    completion_gate = workflow.states["completion_gate"]
    assert completion_gate.actions[0].action_id == "turn_execution.completion_gate"
    assert any(
        t.to_state == "plan" and t.reason == "completion_gate_repeat_iteration"
        for t in completion_gate.transitions
    )
    assert any(t.to_state == "completed" for t in completion_gate.transitions)


def test_register_default_workflows_includes_turn_execution_workflows() -> None:
    registry = WorkflowRegistry()
    register_default_workflows(registry)

    assert registry.get(CHAT_BUTTONIFY_WORKFLOW_ID) is not None
    assert registry.get(CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID) is not None
    assert registry.get(KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID) is not None
    assert registry.get(TURN_COMPLETION_GATE_WORKFLOW_ID) is not None
    assert registry.get(CONVERSATION_TURN_EXECUTION_WORKFLOW_ID) is not None


def test_concept_suggestion_preflight_workflow_has_single_guardrailed_step() -> None:
    workflow = build_concept_suggestion_preflight_workflow()

    assert workflow.initial_state == "suggest"
    assert "suggest" in workflow.states
    assert "completed" in workflow.states

    suggest = workflow.states["suggest"]
    assert suggest.actions[0].action_id == "preflight.specialised_suggest"
    assert any(t.to_state == "completed" for t in suggest.transitions)


def test_buttonify_workflow_exposes_transformation_states() -> None:
    workflow = build_chat_buttonify_workflow()

    assert workflow.initial_state == "assess_input"
    assert "assess_input" in workflow.states
    assert "select_prompt" in workflow.states
    assert "extract_options" in workflow.states
    assert "completed" in workflow.states

    assess = workflow.states["assess_input"]
    assert assess.actions[0].action_id == "buttonify.assess_input"
    assert any(t.to_state == "select_prompt" for t in assess.transitions)
