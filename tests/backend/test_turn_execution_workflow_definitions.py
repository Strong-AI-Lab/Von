from __future__ import annotations

from workflow_test_support import (
    build_authoritative_test_workflow_definition,
    build_test_conversation_turn_registry,
)

from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
)
from src.backend.workflows.workflow_concept_authority_service import (
    build_repo_seed_workflow_definitions,
)

_RETIRED_OUTER_CONTROLLER_IDS = {
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
}
_RETAINED_REPO_SEED_IDS = {
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
}
_EXPLICIT_SUPPORT_IDS = {
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
}


def test_repo_seed_definitions_retire_the_universal_outer_controller() -> None:
    definitions = build_repo_seed_workflow_definitions()

    assert _RETIRED_OUTER_CONTROLLER_IDS.isdisjoint(definitions)
    assert _RETAINED_REPO_SEED_IDS.issubset(definitions)
    assert _EXPLICIT_SUPPORT_IDS.issubset(definitions)


def test_explicit_support_workflows_remain_independently_callable() -> None:
    for workflow_id in sorted(_EXPLICIT_SUPPORT_IDS):
        workflow = build_authoritative_test_workflow_definition(workflow_id)
        assert workflow.workflow_id == workflow_id
        assert workflow.initial_state in workflow.states
        assert workflow.termination_states


def test_test_registry_keeps_support_and_excludes_retired_controller() -> None:
    registry = build_test_conversation_turn_registry()

    for workflow_id in _EXPLICIT_SUPPORT_IDS:
        assert registry.has(workflow_id)
    for workflow_id in _RETIRED_OUTER_CONTROLLER_IDS:
        assert not registry.has(workflow_id)


def test_turn_execution_registry_contains_only_explicit_verification_actions() -> None:
    from src.backend.workflows.action_registry import ActionRegistry
    from src.backend.workflows.durable.turn_execution_actions import (
        TURN_EXECUTION_CRITIC_ACTION_ID,
        register_turn_execution_actions,
    )

    registry = ActionRegistry()
    register_turn_execution_actions(registry)

    assert set(registry.all_action_ids()) == {TURN_EXECUTION_CRITIC_ACTION_ID}
