from src.backend.workflows.definitions import (
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
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
    assert any(t.to_state == "completed" for t in completion_gate.transitions)


def test_register_default_workflows_includes_turn_execution_workflows() -> None:
    registry = WorkflowRegistry()
    register_default_workflows(registry)

    assert registry.get(KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID) is not None
    assert registry.get(TURN_COMPLETION_GATE_WORKFLOW_ID) is not None
    assert registry.get(CONVERSATION_TURN_EXECUTION_WORKFLOW_ID) is not None
