from __future__ import annotations

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.workflows.workflow_registry import WorkflowRegistration
from src.backend.workflows.workflow_registry import WorkflowRegistry


def _required_effects_contract() -> dict[str, object]:
    return {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "grounded_entity_information_retrieval_evidence",
        "required_effects": [
            {
                "effect_id": "grounded_entity_information_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": [
                    "get_predicate_incidence",
                    "find_relations_with_argument",
                ],
                "required_tools_match": "all",
            }
        ],
    }


def _workflow_definition(*, allowed_tools: list[str]) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#entity_information_retrieval_workflow",
        initial_state="lookup",
        states={
            "lookup": WorkflowStateSpec(
                state_id="lookup",
                actions=(
                    WorkflowActionInvocation(
                        action_id="entity_lookup_llm_step",
                        execution_mode="llm",
                        llm_policy={
                            "tool_mode": "allowed",
                            "allowed_tools": allowed_tools,
                        },
                    ),
                ),
                terminal=True,
            )
        },
        metadata={
            "required_effects_contract": _required_effects_contract(),
            "required_effects_contract_source": "definition_metadata",
        },
    )


def _orchestrator_for_definition(
    definition: WorkflowDefinition,
) -> InternalMCPChatOrchestrator:
    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(
            workflow_id=definition.workflow_id,
            definition=definition,
            source="vontology",
            purpose=definition.purpose,
        )
    )
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._workflow_registry = registry
    return orchestrator


def test_launchability_blocks_required_effect_tools_missing_from_llm_allowlist() -> (
    None
):
    orchestrator = _orchestrator_for_definition(
        _workflow_definition(allowed_tools=["search_knowledge_base"])
    )

    probe = orchestrator._probe_workflow_launchability_for_inputs(
        "#V#entity_information_retrieval_workflow",
        available_inputs={"prompt": "Who am I in this conversation?"},
    )

    assert probe["launchable"] is False
    assert probe["launch_input_resolution"]["status"] == (
        "workflow_required_effects_tools_unavailable"
    )
    policy = probe["required_effects_tool_policy"]
    assert policy["reason_code"] == "workflow_required_effect_tool_not_allowed"
    assert policy["unavailable_required_tools"] == [
        "get_predicate_incidence",
        "find_relations_with_argument",
    ]


def test_launchability_accepts_required_effect_tools_allowed_by_llm_step() -> None:
    orchestrator = _orchestrator_for_definition(
        _workflow_definition(
            allowed_tools=["get_predicate_incidence", "find_relations_with_argument"]
        )
    )

    probe = orchestrator._probe_workflow_launchability_for_inputs(
        "#V#entity_information_retrieval_workflow",
        available_inputs={"prompt": "Who am I in this conversation?"},
    )

    assert probe["launchable"] is True
    assert probe["required_effects_tool_policy"]["ok"] is True
