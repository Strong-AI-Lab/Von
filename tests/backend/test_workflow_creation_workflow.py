"""Integration tests for workflow-creation workflow execution and trigger routing."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest

from src.backend.db.mongo_client import get_db
from src.backend.services import concept_service
from src.backend.services.text_value_service import upsert_text_for_concept
from src.backend.services.workflow_discovery_service import (
    EXECUTABILITY_EXECUTABLE_NOW,
    WORKFLOW_CREATION_WORKFLOW_ID as DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID,
    discover_workflows,
    discover_workflows_for_turn,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows.durable.registry_factory import build_durable_action_registry
from src.backend.workflows.durable.workflow_creation_workflow import (
    WORKFLOW_CONTEXT_KEY_VALIDATED_TYPE_NAME,
    WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_ACTION_EMIT_MARKER,
    WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_ACTION_FINALISE,
    WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
    WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_STEP_SEQUENCE,
    WORKFLOW_CREATION_WORKFLOW_ID,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology
from src.backend.workflows.workflow_definition_identity_service import (
    collect_workflow_action_ids,
)
from src.backend.workflows.workflow_registry import WorkflowRegistration, WorkflowRegistry

SCHOLARLY_WORKFLOW_REQUEST_PROMPT = (
    "Create a workflow from this description request: represent scholarly works "
    "from uploaded PDFs in Vontology. Focus on scholarly-work properties and "
    "canonical assertions, including type #V#scholarly_article, file-linking via "
    "#V#propositional_information_thing_has_content, and stable naming/description "
    "predicates. Include upload eligibility, interpretation, assertion, and "
    "representation verification steps. Run autonomously by default and only ask "
    "the user when vital required information cannot be obtained from tools, "
    "existing ontology context, or uploaded content."
)


@pytest.fixture(autouse=True)
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.services.workflow_discovery_service import (
        invalidate_workflow_discovery_executability_caches,
    )

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    invalidate_workflow_discovery_executability_caches()
    yield
    invalidate_workflow_discovery_executability_caches()
    authority_service.clear_workflow_type_resolution_cache()


def _ensure_type(concept_id: str, name: str) -> None:
    try:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            create_as_instance=False,
        )
    except Exception:
        pass


def _seed_workflow_creation_graph_without_actions() -> None:
    _ensure_type("#V#ai_workflow", "AI Workflow")
    _ensure_type("#V#workflow_step", "Workflow Step")

    concept_service.create_concept(
        name="Von Workflow Creation Workflow",
        concept_id=WORKFLOW_CREATION_WORKFLOW_ID,
        parent_concept_ids=["#V#ai_workflow"],
        create_as_instance=True,
    )

    for step_id in WORKFLOW_CREATION_STEP_SEQUENCE:
        concept_service.create_concept(
            name=step_id.replace("#V#", "").replace("_", " ").title(),
            concept_id=step_id,
            parent_concept_ids=["#V#workflow_step"],
            create_as_instance=True,
        )

    concept_service.update_concept(
        WORKFLOW_CREATION_WORKFLOW_ID,
        {
            "relationships": {
                "is_an_instance_of": ["#V#ai_workflow"],
                "hasInitialStep": [WORKFLOW_CREATION_STEP_SEQUENCE[0]],
                "hasStep": list(WORKFLOW_CREATION_STEP_SEQUENCE),
            }
        },
    )

    for index, step_id in enumerate(WORKFLOW_CREATION_STEP_SEQUENCE):
        relationships: Dict[str, Any] = {}
        if index + 1 < len(WORKFLOW_CREATION_STEP_SEQUENCE):
            relationships["nextStep"] = [WORKFLOW_CREATION_STEP_SEQUENCE[index + 1]]
        concept_service.update_concept(step_id, {"relationships": relationships})


def _workflow_spec(workflow_id: str, marker_key: str, marker_value: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "name": "Generated Marker Workflow",
        "description": "Generated by workflow creation integration test.",
        "parent_type_id": "#V#ai_workflow",
        "required_effects": [f"context:{marker_key}={marker_value}"],
        "postcondition_probe": {marker_key: marker_value},
        "steps": [
            {
                "state_id": "emit",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {"marker_key": marker_key, "marker_value": marker_value},
                "next_state": "completed",
            },
            {"state_id": "completed", "terminal": True},
        ],
    }


def _seed_workflow_creation_synthesis_policy() -> None:
    upsert_text_for_concept(
        subject_concept_id=WORKFLOW_CREATION_WORKFLOW_ID,
        predicate="hasContent",
        text=(
            "When synthesising workflows from text-only requests, produce executable "
            "workflow steps and impose autonomy by default. User affirmation is only "
            "allowed when vital information cannot be obtained by tools."
        ),
        lang="en-NZ",
    )


def test_publish_repair_adds_executable_actions_to_workflow_creation_workflow() -> None:
    _seed_workflow_creation_graph_without_actions()

    pre = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert pre is not None
    assert collect_workflow_action_ids(pre) == ()

    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(
            workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
            definition=pre,
            purpose="repair test",
            source="vontology",
        )
    )

    report = authority_service.publish_canonical_chat_workflow_graphs(registry=registry)
    assert WORKFLOW_CREATION_WORKFLOW_ID in (
        report.get("published_workflow_ids") or []
    )

    repaired = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert repaired is not None
    action_ids = set(collect_workflow_action_ids(repaired))
    assert WORKFLOW_CREATION_ACTION_IDENTIFY_NEED in action_ids
    assert WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE in action_ids
    assert WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE in action_ids
    assert WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS in action_ids
    assert WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS in action_ids
    assert WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY in action_ids
    assert WORKFLOW_CREATION_ACTION_FINALISE in action_ids


def test_workflow_creation_actions_individual_then_end_to_end() -> None:
    _seed_workflow_creation_graph_without_actions()

    pre = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert pre is not None
    registry_for_publish = WorkflowRegistry()
    registry_for_publish.register(
        WorkflowRegistration(
            workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
            definition=pre,
            purpose="execution test",
            source="vontology",
        )
    )
    authority_service.publish_canonical_chat_workflow_graphs(registry=registry_for_publish)

    action_registry = build_durable_action_registry()
    env = WorkflowEnvironment(llm_client=None, user_namespace="#V#test_user")

    target_workflow_id = "#V#integration_created_marker_workflow"
    marker_key = "paper_marker"
    marker_value = "created"
    context: dict[str, Any] = {
        "prompt": "Create a workflow from this description request.",
        "workflow_spec": _workflow_spec(target_workflow_id, marker_key, marker_value),
    }

    individual_actions = (
        WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
        WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
        WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
        WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
        WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
        WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
        WORKFLOW_CREATION_ACTION_FINALISE,
    )
    for action_id in individual_actions:
        result = action_registry.execute(
            action_id,
            inputs={},
            context=context,
            env=env,
        )
        assert result.outcome == "success", f"{action_id}: {result.error}"
        context.update(result.outputs)
        assert WORKFLOW_CONTEXT_KEY_VALIDATED_TYPE_NAME in context

    assert context.get("required_effects_declared") is True
    assert context.get("structural_validation_passed") is True
    assert context.get("postconditions_verified") is True

    workflow_creation_definition = load_workflow_definition_from_vontology(
        WORKFLOW_CREATION_WORKFLOW_ID
    )
    assert workflow_creation_definition is not None
    workflow_creation_result = WorkflowExecutor(
        registry=action_registry,
        max_transitions=30,
    ).run(
        workflow_creation_definition,
        environment=env,
        data={
            "prompt": "Create a workflow from this description request.",
            "workflow_spec": _workflow_spec(
                "#V#integration_created_marker_workflow_full_run",
                "paper_marker_full_run",
                "created_full_run",
            ),
        },
    )
    assert workflow_creation_result.completed is True
    assert workflow_creation_result.data.get("postconditions_verified") is True

    generated_definition = load_workflow_definition_from_vontology(target_workflow_id)
    assert generated_definition is not None
    generated_run = WorkflowExecutor(registry=action_registry, max_transitions=20).run(
        generated_definition,
        environment=env,
    )
    assert generated_run.completed is True
    assert generated_run.data.get(marker_key) == marker_value


def test_workflow_creation_request_is_discoverable_for_turn_routing() -> None:
    with (
        patch(
            "src.backend.services.workflow_discovery_service._search_workflows_semantic",
            return_value=[],
        ),
        patch(
            "src.backend.services.workflow_discovery_service._search_workflows_vontology",
            return_value=[],
        ),
        patch(
            "src.backend.services.workflow_discovery_service._enrich_workflow_matches",
            side_effect=lambda matches: matches,
        ),
        patch(
            "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability",
            side_effect=lambda concept_id: (
                (True, EXECUTABILITY_EXECUTABLE_NOW, None)
                if concept_id == DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID
                else (False, "graph_incomplete", "not_used")
            ),
        ),
    ):
        query = (
            "Please create a workflow from this description request for scholarly "
            "paper representation."
        )
        result = discover_workflows(query, max_results=5)
        routing_ids = [
            match.concept_id for match in (result.routing_matches or []) if match.concept_id
        ]
        assert DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID in routing_ids

        wrapped = discover_workflows_for_turn(query, max_results=5)
        assert wrapped is not None
        wrapped_ids = [match.get("concept_id") for match in wrapped.get("matches", [])]
        assert DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID in wrapped_ids


def test_workflow_creation_request_routes_and_executes_end_to_end() -> None:
    _seed_workflow_creation_graph_without_actions()

    pre = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert pre is not None
    registry_for_publish = WorkflowRegistry()
    registry_for_publish.register(
        WorkflowRegistration(
            workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
            definition=pre,
            purpose="routing + execution test",
            source="vontology",
        )
    )
    authority_service.publish_canonical_chat_workflow_graphs(registry=registry_for_publish)

    query = (
        "Create a workflow from this description request so scholarly paper upload "
        "representation can run end to end."
    )
    with (
        patch(
            "src.backend.services.workflow_discovery_service._search_workflows_semantic",
            return_value=[],
        ),
        patch(
            "src.backend.services.workflow_discovery_service._search_workflows_vontology",
            return_value=[],
        ),
        patch(
            "src.backend.services.workflow_discovery_service._enrich_workflow_matches",
            side_effect=lambda matches: matches,
        ),
        patch(
            "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability",
            side_effect=lambda concept_id: (
                (True, EXECUTABILITY_EXECUTABLE_NOW, None)
                if concept_id == DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID
                else (False, "graph_incomplete", "not_used")
            ),
        ),
    ):
        wrapped = discover_workflows_for_turn(query, max_results=5)
        assert wrapped is not None
        routing_ids = [match.get("concept_id") for match in wrapped.get("matches", [])]
        assert DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID in routing_ids

    action_registry = build_durable_action_registry()
    env = WorkflowEnvironment(llm_client=None, user_namespace="#V#test_user")
    generated_workflow_id = "#V#integration_routed_created_workflow"
    marker_key = "routed_marker"
    marker_value = "routed_created"

    workflow_creation_definition = load_workflow_definition_from_vontology(
        WORKFLOW_CREATION_WORKFLOW_ID
    )
    assert workflow_creation_definition is not None
    run_result = WorkflowExecutor(registry=action_registry, max_transitions=30).run(
        workflow_creation_definition,
        environment=env,
        data={
            "prompt": query,
            "workflow_spec": _workflow_spec(
                generated_workflow_id,
                marker_key,
                marker_value,
            ),
        },
    )
    assert run_result.completed is True
    assert run_result.data.get("postconditions_verified") is True

    generated_definition = load_workflow_definition_from_vontology(generated_workflow_id)
    assert generated_definition is not None
    generated_run = WorkflowExecutor(registry=action_registry, max_transitions=20).run(
        generated_definition,
        environment=env,
    )
    assert generated_run.completed is True
    assert generated_run.data.get(marker_key) == marker_value


def test_text_only_scholarly_request_fails_closed_without_vontology_policy() -> None:
    _seed_workflow_creation_graph_without_actions()

    pre = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert pre is not None
    registry_for_publish = WorkflowRegistry()
    registry_for_publish.register(
        WorkflowRegistration(
            workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
            definition=pre,
            purpose="text-only scholarly synthesis guardrail",
            source="vontology",
        )
    )
    authority_service.publish_canonical_chat_workflow_graphs(registry=registry_for_publish)

    action_registry = build_durable_action_registry()
    env = WorkflowEnvironment(llm_client=None, user_namespace="#V#test_user")
    workflow_creation_definition = load_workflow_definition_from_vontology(
        WORKFLOW_CREATION_WORKFLOW_ID
    )
    assert workflow_creation_definition is not None

    prompt = SCHOLARLY_WORKFLOW_REQUEST_PROMPT
    run_result = WorkflowExecutor(registry=action_registry, max_transitions=30).run(
        workflow_creation_definition,
        environment=env,
        data={"prompt": prompt},
    )
    assert run_result.completed is False
    assert "workflow_creation_synthesis_policy_missing" in str(run_result.error)


def test_text_only_scholarly_request_synthesises_non_blocking_workflow() -> None:
    _seed_workflow_creation_graph_without_actions()

    pre = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert pre is not None
    registry_for_publish = WorkflowRegistry()
    registry_for_publish.register(
        WorkflowRegistration(
            workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
            definition=pre,
            purpose="text-only scholarly synthesis",
            source="vontology",
        )
    )
    authority_service.publish_canonical_chat_workflow_graphs(registry=registry_for_publish)
    _seed_workflow_creation_synthesis_policy()

    action_registry = build_durable_action_registry()
    env = WorkflowEnvironment(llm_client=None, user_namespace="#V#test_user")
    workflow_creation_definition = load_workflow_definition_from_vontology(
        WORKFLOW_CREATION_WORKFLOW_ID
    )
    assert workflow_creation_definition is not None

    prompt = SCHOLARLY_WORKFLOW_REQUEST_PROMPT
    run_result = WorkflowExecutor(registry=action_registry, max_transitions=30).run(
        workflow_creation_definition,
        environment=env,
        data={
            "prompt": prompt,
            "target_workflow_id": "#V#integration_scholarly_paper_representation_workflow",
        },
    )
    assert run_result.completed is True
    assert run_result.data.get("postconditions_verified") is True
    assert run_result.data.get("required_effects_declared") is True
    assert run_result.data.get("workflow_concept_id") == (
        "#V#integration_scholarly_paper_representation_workflow"
    )

    generated_definition = load_workflow_definition_from_vontology(
        "#V#integration_scholarly_paper_representation_workflow"
    )
    assert generated_definition is not None
    action_ids = set(collect_workflow_action_ids(generated_definition))
    assert "request_user_input" not in action_ids
    assert "workflow_creation.emit_marker" in action_ids
    assert "workflow_creation.resolve_scholarly_authors" in action_ids

    _ensure_type("#V#scholarly_article", "Scholarly Article")
    _ensure_type("#V#person", "Person")
    concept_service.create_concept(
        name="Integration Scholarly Paper",
        concept_id="#V#paper_test",
        parent_concept_ids=["#V#scholarly_article"],
        create_as_instance=True,
    )
    concept_service.create_concept(
        name="Jane Doe",
        concept_id="#V#person_jane_doe_existing",
        parent_concept_ids=["#V#person"],
        create_as_instance=True,
    )

    generated_run = WorkflowExecutor(registry=action_registry, max_transitions=40).run(
        generated_definition,
        environment=env,
        data={
            "file_copy_concept_id": "#V#uploaded_file_copy_test",
            "paper_concept_id": "#V#paper_test",
            "author_names": ["Jane Doe", "Alan Turing"],
        },
    )
    assert generated_run.completed is True
    assert generated_run.data.get("requires_user_affirmation") is False
    assert generated_run.data.get("user_affirmation_policy") == (
        "only_when_vital_info_missing"
    )
    assert generated_run.data.get("scholarly_representation_verified") is True
    assert generated_run.data.get("author_resolution_completed") is True
    reused_ids = generated_run.data.get("reused_author_concept_ids") or []
    created_ids = generated_run.data.get("created_author_concept_ids") or []
    resolved_ids = generated_run.data.get("resolved_author_concept_ids") or []
    assert "#V#person_jane_doe_existing" in reused_ids
    assert "#V#person_jane_doe_existing" in resolved_ids
    assert len(created_ids) == 1

    created_author_id = str(created_ids[0])
    created_author = concept_service.get_concept_by_concept_id(created_author_id)
    assert created_author is not None
    created_types = (
        (created_author.get("relationships") or {}).get("is_an_instance_of") or []
    )
    assert "#V#person" in created_types

    paper_doc = concept_service.get_concept_by_concept_id("#V#paper_test")
    assert paper_doc is not None
    authored_by_ids = ((paper_doc.get("relationships") or {}).get("#V#authored_by") or [])
    assert "#V#person_jane_doe_existing" in authored_by_ids
    assert created_author_id in authored_by_ids
