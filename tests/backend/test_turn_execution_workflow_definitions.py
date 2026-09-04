from __future__ import annotations

from workflow_test_support import (
    build_authoritative_test_workflow_definition,
    build_test_conversation_turn_registry,
)

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.workflow_concept_authority_service import (
    build_repo_seed_workflow_definitions,
)

_RETIRED_CONTROLLER_IDS = {
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
}
_RETIRED_PRELUDE_ID = "#V#workflow_experience_context_prelude"
_EXPLICIT_SUPPORT_IDS = {
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
}


def test_repo_seed_definitions_exclude_controllers_and_tombstone_the_prelude() -> None:
    definitions = build_repo_seed_workflow_definitions()

    assert _RETIRED_CONTROLLER_IDS.isdisjoint(definitions)
    assert _EXPLICIT_SUPPORT_IDS.issubset(definitions)
    tombstone = definitions[_RETIRED_PRELUDE_ID]
    assert tombstone.initial_state == "retired"
    assert tuple(tombstone.states) == ("retired",)
    assert tombstone.termination_states == ("retired",)
    assert tombstone.states["retired"].terminal is True
    assert tombstone.states["retired"].actions == ()
    assert tombstone.metadata["routing_profile"]["role"] == "maintenance"
    assert (
        tombstone.metadata["routing_profile"]["explicit_workflow_context_required"]
        is True
    )
    assert tombstone.metadata["routing_profile"]["routing_eligible"] is False

    result = WorkflowExecutor(registry=ActionRegistry()).run(
        tombstone,
        environment=WorkflowEnvironment(llm_client=None),
    )
    assert result.completed is True
    assert result.final_state == "retired"
    assert result.error is None


def test_explicit_support_workflows_remain_independently_callable() -> None:
    for workflow_id in sorted(_EXPLICIT_SUPPORT_IDS):
        workflow = build_authoritative_test_workflow_definition(workflow_id)
        assert workflow.workflow_id == workflow_id
        assert workflow.initial_state in workflow.states
        assert workflow.termination_states


def test_test_registry_keeps_support_and_excludes_retired_workflows() -> None:
    registry = build_test_conversation_turn_registry()

    for workflow_id in _EXPLICIT_SUPPORT_IDS:
        assert registry.has(workflow_id)
    for workflow_id in _RETIRED_CONTROLLER_IDS:
        assert not registry.has(workflow_id)
    assert not registry.has(_RETIRED_PRELUDE_ID)


def test_turn_execution_registry_contains_only_explicit_verification_actions() -> None:
    from src.backend.workflows.action_registry import ActionRegistry
    from src.backend.workflows.durable.turn_execution_actions import (
        TURN_EXECUTION_CRITIC_ACTION_ID,
        register_turn_execution_actions,
    )

    registry = ActionRegistry()
    register_turn_execution_actions(registry)

    assert set(registry.all_action_ids()) == {TURN_EXECUTION_CRITIC_ACTION_ID}
