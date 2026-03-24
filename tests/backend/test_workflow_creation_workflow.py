"""Integration tests for workflow-creation workflow execution and trigger routing."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest

from src.backend.db.mongo_client import get_db
from src.backend.services import concept_service
from src.backend.services.text_value_service import (
    get_texts_for_concept,
    upsert_text_for_concept,
)
from src.backend.services.workflow_discovery_service import (
    EXECUTABILITY_EXECUTABLE_NOW,
    WORKFLOW_CREATION_WORKFLOW_ID as DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID,
    WorkflowDiscoveryResult,
    WorkflowMatch,
    discover_workflows,
    discover_workflows_for_turn,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows.durable.registry_factory import build_durable_action_registry
from src.backend.workflows.durable.workflow_creation_workflow import (
    WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY,
    WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION,
    WORKFLOW_CONTEXT_KEY_VALIDATED_TYPE_NAME,
    WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
    WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_ACTION_EMIT_MARKER,
    WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_ACTION_FINALISE,
    WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
    WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT,
    WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS,
    WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE,
    WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_STEP_SEQUENCE,
    WORKFLOW_CREATION_WORKFLOW_ID,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    build_workflow_process_graph,
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
    resolve_workflow_publication_lifecycle,
)
from src.backend.workflows.workflow_creation_contracts import (
    WORKFLOW_AUTHORING_ACTION_CONCEPT_ENSURE_WORKFLOW_IDENTITY,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_PUBLISH_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_CONCEPT_VALIDATE_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_PROMPT_REPAIR_OR_CREATE_DECISION,
    WORKFLOW_AUTHORING_PROMPT_REPAIR_SPEC,
    WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID,
    WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID,
    WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_ACTION_CONCEPT_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_ACTION_CONCEPT_EMIT_MARKER,
    WORKFLOW_CREATION_ACTION_CONCEPT_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_ACTION_CONCEPT_FINALISE,
    WORKFLOW_CREATION_ACTION_CONCEPT_IDENTIFY_NEED,
    WORKFLOW_CREATION_ACTION_CONCEPT_RESOLVE_SCHOLARLY_AUTHORS,
    WORKFLOW_CREATION_ACTION_CONCEPT_VERIFY_DISCOVERABILITY,
)
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

PHD_STUDENT_WORKFLOW_REQUEST_PROMPT = (
    "Create a workflow from this description request: represent a PhD student from "
    "text description in Vontology. Include candidate concept resolution/reuse, "
    "core person/student/research relationship assertions, text grounding with "
    "provenance, and fail-closed ambiguity diagnostics."
)


class _QueuedLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None, llm_params=None) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "context": context,
                "model": model,
            }
        )
        if not self._responses:
            raise AssertionError("queued_llm_exhausted")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    def _stub_capability_search(query: str, *args: Any, **kwargs: Any):
        text = str(query or "").lower()
        if "create a workflow from this description request" in text:
            return [
                WorkflowMatch(
                    concept_id=DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID,
                    name="Von Workflow Creation Workflow",
                    description=(
                        "Create and verify executable workflows from a workflow "
                        "description/request."
                    ),
                    relevance_score=1.0,
                    match_source="capability_index",
                )
            ]
        return []

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities",
        _stub_capability_search,
    )
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


def _seed_workflow_authoring_prompts() -> None:
    _ensure_type("#V#prompt_for_llm", "Prompt For LLM")
    prompt_specs = (
        (
            WORKFLOW_AUTHORING_PROMPT_REPAIR_OR_CREATE_DECISION,
            "Workflow authoring repair-or-create decision prompt",
            (
                "Decide whether the request should reuse an existing workflow, "
                "repair one, or create a new one. Return JSON only."
            ),
        ),
        (
            WORKFLOW_AUTHORING_PROMPT_REPAIR_SPEC,
            "Workflow authoring repair spec prompt",
            (
                "Produce a repaired declarative workflow spec for the target "
                "workflow. Return JSON only."
            ),
        ),
    )
    for prompt_concept_id, name, text in prompt_specs:
        try:
            concept_service.create_concept(
                name=name,
                concept_id=prompt_concept_id,
                parent_concept_ids=["#V#prompt_for_llm"],
                create_as_instance=True,
                visibility_scope_mode="global_general",
            )
        except Exception:
            pass
        upsert_text_for_concept(
            subject_concept_id=prompt_concept_id,
            predicate="hasContent",
            text=text,
            lang="en-NZ",
        )


def _publish_workflow_authoring_governance_workflows() -> None:
    _seed_workflow_authoring_prompts()
    authority_service.publish_canonical_chat_workflow_graphs(
        target_workflow_ids=[
            WORKFLOW_CREATION_WORKFLOW_ID,
            WORKFLOW_AUTHORING_REPAIR_WORKFLOW_ID,
            WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID,
        ]
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
    assert set(report.get("created_action_concept_ids") or []) == {
        WORKFLOW_CREATION_ACTION_CONCEPT_IDENTIFY_NEED,
        WORKFLOW_CREATION_ACTION_CONCEPT_DESIGN_STRUCTURE,
        WORKFLOW_AUTHORING_ACTION_CONCEPT_ENSURE_WORKFLOW_IDENTITY,
        WORKFLOW_AUTHORING_ACTION_CONCEPT_MATERIALISE_WORKFLOW_DEFINITION,
        WORKFLOW_AUTHORING_ACTION_CONCEPT_VALIDATE_WORKFLOW_DEFINITION,
        WORKFLOW_AUTHORING_ACTION_CONCEPT_PUBLISH_WORKFLOW_DEFINITION,
    }

    repaired = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert repaired is not None
    action_ids = set(collect_workflow_action_ids(repaired))
    assert WORKFLOW_CREATION_ACTION_IDENTIFY_NEED in action_ids
    assert WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE in action_ids
    assert WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY in action_ids
    assert WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION in action_ids
    assert WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION in action_ids
    assert WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION in action_ids

    graph, warnings = build_workflow_process_graph(WORKFLOW_CREATION_WORKFLOW_ID)
    assert graph is not None
    assert warnings == []
    step_targets = {
        str(step.get("step_id")): str(step.get("invokes_action_target") or "")
        for step in (graph.get("steps") or [])
        if isinstance(step, dict)
    }
    assert (
        step_targets.get("#V#workflow_creation_step_identify_need")
        == WORKFLOW_CREATION_ACTION_CONCEPT_IDENTIFY_NEED
    )
    assert (
        step_targets.get("#V#workflow_creation_step_design_structure")
        == WORKFLOW_CREATION_ACTION_CONCEPT_DESIGN_STRUCTURE
    )
    assert (
        step_targets.get("#V#workflow_creation_step_create_workflow_type")
        == WORKFLOW_AUTHORING_ACTION_CONCEPT_ENSURE_WORKFLOW_IDENTITY
    )
    assert (
        step_targets.get("#V#workflow_creation_step_materialise_workflow_definition")
        == WORKFLOW_AUTHORING_ACTION_CONCEPT_MATERIALISE_WORKFLOW_DEFINITION
    )
    assert (
        step_targets.get("#V#workflow_creation_step_verify_discoverability")
        == WORKFLOW_AUTHORING_ACTION_CONCEPT_VALIDATE_WORKFLOW_DEFINITION
    )
    assert (
        step_targets.get("#V#workflow_creation_step_document_in_jira")
        == WORKFLOW_AUTHORING_ACTION_CONCEPT_PUBLISH_WORKFLOW_DEFINITION
    )


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

    pre_publication_actions = (
        WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
        WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
        WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY,
        WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION,
        WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION,
    )
    for action_id in pre_publication_actions:
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
    lifecycle_before_publish, lifecycle_source_before_publish = (
        resolve_workflow_publication_lifecycle(target_workflow_id)
    )
    assert lifecycle_before_publish is not None
    assert lifecycle_before_publish.get("phase") == "validated"
    assert lifecycle_before_publish.get("published") is False
    assert lifecycle_source_before_publish in {"concept_data", "text_relation:#V#hasWorkflowLifecycleJson"}
    assert target_workflow_id not in set(discover_workflow_ids())

    finalise_result = action_registry.execute(
        WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION,
        inputs={},
        context=context,
        env=env,
    )
    assert finalise_result.outcome == "success", finalise_result.error
    context.update(finalise_result.outputs)
    lifecycle_after_publish, _lifecycle_source_after_publish = (
        resolve_workflow_publication_lifecycle(target_workflow_id)
    )
    assert lifecycle_after_publish is not None
    assert lifecycle_after_publish.get("phase") == "published"
    assert lifecycle_after_publish.get("published") is True
    assert target_workflow_id in set(discover_workflow_ids())

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
        patch(
            "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
            return_value=True,
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
        patch(
            "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
            return_value=True,
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
    assert WORKFLOW_CREATION_ACTION_EMIT_MARKER in action_ids
    assert WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS in action_ids

    generated_graph, generated_warnings = build_workflow_process_graph(
        "#V#integration_scholarly_paper_representation_workflow"
    )
    assert generated_graph is not None
    assert generated_warnings == []
    generated_step_targets = {
        str(step.get("invokes_action")): str(step.get("invokes_action_target") or "")
        for step in (generated_graph.get("steps") or [])
        if isinstance(step, dict) and isinstance(step.get("invokes_action"), str)
    }
    assert (
        generated_step_targets.get(WORKFLOW_CREATION_ACTION_EMIT_MARKER)
        == WORKFLOW_CREATION_ACTION_CONCEPT_EMIT_MARKER
    )
    assert (
        generated_step_targets.get(WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS)
        == WORKFLOW_CREATION_ACTION_CONCEPT_RESOLVE_SCHOLARLY_AUTHORS
    )

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


def test_text_only_phd_student_request_routes_create_execute_end_to_end() -> None:
    _seed_workflow_creation_graph_without_actions()

    pre = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert pre is not None
    registry_for_publish = WorkflowRegistry()
    registry_for_publish.register(
        WorkflowRegistration(
            workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
            definition=pre,
            purpose="text-only phd student synthesis",
            source="vontology",
        )
    )
    authority_service.publish_canonical_chat_workflow_graphs(registry=registry_for_publish)
    _seed_workflow_creation_synthesis_policy()

    query = PHD_STUDENT_WORKFLOW_REQUEST_PROMPT
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
        patch(
            "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
            return_value=True,
        ),
    ):
        wrapped = discover_workflows_for_turn(query, max_results=5)
        assert wrapped is not None
        routing_ids = [match.get("concept_id") for match in wrapped.get("matches", [])]
        assert DISCOVERY_WORKFLOW_CREATION_WORKFLOW_ID in routing_ids

    action_registry = build_durable_action_registry()
    env = WorkflowEnvironment(llm_client=None, user_namespace="#V#test_user")
    workflow_creation_definition = load_workflow_definition_from_vontology(
        WORKFLOW_CREATION_WORKFLOW_ID
    )
    assert workflow_creation_definition is not None

    generated_workflow_id = "#V#integration_phd_student_representation_workflow"
    run_result = WorkflowExecutor(registry=action_registry, max_transitions=40).run(
        workflow_creation_definition,
        environment=env,
        data={
            "prompt": query,
            "target_workflow_id": generated_workflow_id,
        },
    )
    assert run_result.completed is True
    assert run_result.data.get("postconditions_verified") is True
    assert run_result.data.get("required_effects_declared") is True

    generated_definition = load_workflow_definition_from_vontology(generated_workflow_id)
    assert generated_definition is not None
    action_ids = set(collect_workflow_action_ids(generated_definition))
    assert "request_user_input" not in action_ids
    assert WORKFLOW_CREATION_ACTION_EMIT_MARKER in action_ids
    assert WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE in action_ids
    assert WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS in action_ids
    assert WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT in action_ids

    _ensure_type("#V#person", "Person")
    concept_service.create_concept(
        name="Grace Hopper",
        concept_id="#V#person_grace_hopper_existing",
        parent_concept_ids=["#V#person"],
        create_as_instance=True,
    )

    student_description = (
        "Student Name: Alex Example\n"
        "Supervisors: Grace Hopper\n"
        "Research Topic: Neuro-Symbolic Systems\n"
        "Institution: University of Auckland"
    )
    generated_run = WorkflowExecutor(registry=action_registry, max_transitions=40).run(
        generated_definition,
        environment=env,
        data={"phd_student_description": student_description},
    )
    assert generated_run.completed is True
    assert generated_run.data.get("phd_student_candidate_resolved") is True
    assert generated_run.data.get("phd_student_relationships_asserted") is True
    assert generated_run.data.get("phd_student_text_grounded") is True
    assert generated_run.data.get("phd_student_representation_verified") is True

    student_concept_id = str(generated_run.data.get("phd_student_concept_id") or "")
    assert student_concept_id
    student_doc = concept_service.get_concept_by_concept_id(student_concept_id)
    assert student_doc is not None
    student_types = ((student_doc.get("relationships") or {}).get("is_an_instance_of") or [])
    assert "#V#person" in student_types
    assert "#V#student" in student_types
    assert "#V#phd_student" in student_types

    supervised_by_ids = ((student_doc.get("relationships") or {}).get("#V#supervised_by") or [])
    assert "#V#person_grace_hopper_existing" in supervised_by_ids
    research_topic_ids = ((student_doc.get("relationships") or {}).get("#V#researches") or [])
    assert len(research_topic_ids) == 1

    description_rows = get_texts_for_concept(
        subject_concept_id=student_concept_id,
        predicate="hasDescription",
        limit=50,
    )
    description_values = [str(row.get("text") or "") for row in description_rows]
    assert student_description in description_values

    note_rows = get_texts_for_concept(
        subject_concept_id=student_concept_id,
        predicate="hasNote",
        limit=50,
    )
    note_values = [str(row.get("text") or "") for row in note_rows]
    assert any("text_driven_workflow_creation" in value for value in note_values)


def test_generated_phd_student_workflow_fails_closed_for_ambiguous_student() -> None:
    _seed_workflow_creation_graph_without_actions()

    pre = load_workflow_definition_from_vontology(WORKFLOW_CREATION_WORKFLOW_ID)
    assert pre is not None
    registry_for_publish = WorkflowRegistry()
    registry_for_publish.register(
        WorkflowRegistration(
            workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
            definition=pre,
            purpose="text-only phd student ambiguity",
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

    generated_workflow_id = "#V#integration_phd_student_ambiguity_workflow"
    creation_run = WorkflowExecutor(registry=action_registry, max_transitions=40).run(
        workflow_creation_definition,
        environment=env,
        data={
            "prompt": PHD_STUDENT_WORKFLOW_REQUEST_PROMPT,
            "target_workflow_id": generated_workflow_id,
        },
    )
    assert creation_run.completed is True
    generated_definition = load_workflow_definition_from_vontology(generated_workflow_id)
    assert generated_definition is not None

    _ensure_type("#V#person", "Person")
    concept_service.create_concept(
        name="Pat Lee",
        concept_id="#V#person_pat_lee_a",
        parent_concept_ids=["#V#person"],
        create_as_instance=True,
    )
    concept_service.create_concept(
        name="Pat Lee",
        concept_id="#V#person_pat_lee_b",
        parent_concept_ids=["#V#person"],
        create_as_instance=True,
    )

    generated_run = WorkflowExecutor(registry=action_registry, max_transitions=40).run(
        generated_definition,
        environment=env,
        data={
            "phd_student_description": (
                "Student Name: Pat Lee\n"
                "Research Topic: Symbolic Learning Systems"
            )
        },
    )
    assert generated_run.completed is True
    assert "failed" in str(generated_run.final_state or "").lower()
    failure_error = str(generated_run.data.get("last_action_error") or "")
    assert "phd_student_candidate_ambiguous" in failure_error
    assert "Pat Lee" in failure_error
    assert "#V#person_pat_lee_a" in failure_error
    assert "#V#person_pat_lee_b" in failure_error


def test_workflow_authoring_preflight_create_routes_through_wrapper_workflow() -> None:
    _publish_workflow_authoring_governance_workflows()

    action_registry = build_durable_action_registry()
    llm = _QueuedLLM(
        responses=[
            (
                '{"decision":"create","target_workflow_id":"'
                '#V#wrapper_created_workflow",'
                '"target_workflow_name":"Wrapper Created Workflow",'
                '"reasoning":"No close existing workflow matches the request.",'
                '"evidence":[{"kind":"candidate_count","value":0}],'
                '"response_text":"Creating a new workflow."}'
            )
        ]
    )
    env = WorkflowEnvironment(llm_client=llm, user_namespace="#V#test_user")
    wrapper_definition = load_workflow_definition_from_vontology(
        WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID
    )
    assert wrapper_definition is not None

    with patch(
        "src.backend.workflows.durable.workflow_creation_workflow.discover_workflows",
        return_value=WorkflowDiscoveryResult(
            matches=[],
            routing_matches=[],
            query="Create a workflow from this description request for wrapper create.",
            requested_query=(
                "Create a workflow from this description request for wrapper create."
            ),
            allow_non_executable=True,
        ),
    ):
        run_result = WorkflowExecutor(registry=action_registry, max_transitions=30).run(
            wrapper_definition,
            environment=env,
            data={
                "prompt": (
                    "Create a workflow from this description request for wrapper "
                    "create."
                )
            },
        )

    assert run_result.completed is True
    assert run_result.data.get("workflow_authoring_preflight_decision") == "create"
    assert run_result.data.get("workflow_concept_id") == "#V#wrapper_created_workflow"
    assert run_result.data.get("workflow_discoverable") is True

    generated_definition = load_workflow_definition_from_vontology(
        "#V#wrapper_created_workflow"
    )
    assert generated_definition is not None
    generated_run = WorkflowExecutor(registry=action_registry, max_transitions=20).run(
        generated_definition,
        environment=WorkflowEnvironment(llm_client=None, user_namespace="#V#test_user"),
    )
    assert generated_run.completed is True
    assert generated_run.data.get("workflow_request_summary")


def test_workflow_authoring_preflight_repair_updates_existing_workflow() -> None:
    _publish_workflow_authoring_governance_workflows()

    action_registry = build_durable_action_registry()
    base_env = WorkflowEnvironment(llm_client=None, user_namespace="#V#test_user")
    creation_definition = load_workflow_definition_from_vontology(
        WORKFLOW_CREATION_WORKFLOW_ID
    )
    assert creation_definition is not None

    existing_workflow_id = "#V#existing_marker_workflow"
    create_existing = WorkflowExecutor(registry=action_registry, max_transitions=30).run(
        creation_definition,
        environment=base_env,
        data={
            "prompt": "Create a workflow from this description request.",
            "workflow_spec": _workflow_spec(
                existing_workflow_id,
                "repair_marker",
                "before_repair",
            ),
        },
    )
    assert create_existing.completed is True

    wrapper_definition = load_workflow_definition_from_vontology(
        WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID
    )
    assert wrapper_definition is not None
    llm = _QueuedLLM(
        responses=[
            (
                '{"decision":"repair","target_workflow_id":"'
                '#V#existing_marker_workflow",'
                '"target_workflow_name":"Existing Marker Workflow",'
                '"reasoning":"The request closely matches the existing workflow but '
                'needs an update.",'
                '"evidence":[{"kind":"candidate","concept_id":"'
                '#V#existing_marker_workflow"}],'
                '"response_text":"Repairing the existing workflow."}'
            ),
            (
                '{"target_workflow_id":"#V#existing_marker_workflow",'
                '"repair_summary":"Update the emitted marker value.",'
                '"repaired_workflow_spec":'
                '{"workflow_id":"#V#existing_marker_workflow",'
                '"name":"Generated Marker Workflow",'
                '"description":"Generated by workflow creation integration test.",'
                '"parent_type_id":"#V#ai_workflow",'
                '"required_effects":["context:repair_marker=after_repair"],'
                '"postcondition_probe":{"repair_marker":"after_repair"},'
                '"steps":['
                '{"state_id":"emit","action_id":"workflow_authoring.emit_marker",'
                '"inputs":{"marker_key":"repair_marker","marker_value":"after_repair"},'
                '"next_state":"completed"},'
                '{"state_id":"completed","terminal":true}'
                "]}}"
            ),
        ]
    )
    repair_env = WorkflowEnvironment(llm_client=llm, user_namespace="#V#test_user")

    discovery_result = WorkflowDiscoveryResult(
        matches=[
            WorkflowMatch(
                concept_id=existing_workflow_id,
                name="Existing Marker Workflow",
                description="Existing workflow candidate for repair.",
                relevance_score=0.97,
                match_source="test",
            )
        ],
        routing_matches=[
            WorkflowMatch(
                concept_id=existing_workflow_id,
                name="Existing Marker Workflow",
                description="Existing workflow candidate for repair.",
                relevance_score=0.97,
                match_source="test",
            )
        ],
        query="Repair the existing marker workflow.",
        requested_query="Repair the existing marker workflow.",
        allow_non_executable=True,
    )
    with patch(
        "src.backend.workflows.durable.workflow_creation_workflow.discover_workflows",
        return_value=discovery_result,
    ):
        repair_run = WorkflowExecutor(registry=action_registry, max_transitions=40).run(
            wrapper_definition,
            environment=repair_env,
            data={"prompt": "Repair the existing marker workflow."},
        )

    assert repair_run.completed is True
    assert repair_run.data.get("workflow_authoring_preflight_decision") == "repair"
    assert repair_run.data.get("workflow_concept_id") == existing_workflow_id
    assert repair_run.data.get("workflow_authoring_repaired_workflow_id") == (
        existing_workflow_id
    )
    assert repair_run.data.get("workflow_discoverable") is True

    repaired_definition = load_workflow_definition_from_vontology(existing_workflow_id)
    assert repaired_definition is not None
    repaired_run = WorkflowExecutor(registry=action_registry, max_transitions=20).run(
        repaired_definition,
        environment=base_env,
    )
    assert repaired_run.completed is True
    assert repaired_run.data.get("repair_marker") == "after_repair"


def test_workflow_authoring_preflight_reuse_returns_existing_workflow_id() -> None:
    _publish_workflow_authoring_governance_workflows()

    wrapper_definition = load_workflow_definition_from_vontology(
        WORKFLOW_AUTHORING_REPAIR_OR_CREATE_WORKFLOW_ID
    )
    assert wrapper_definition is not None
    llm = _QueuedLLM(
        responses=[
            (
                '{"decision":"reuse","target_workflow_id":"'
                '#V#existing_reusable_workflow",'
                '"target_workflow_name":"Existing Reusable Workflow",'
                '"reasoning":"The existing workflow already satisfies the request.",'
                '"evidence":[{"kind":"candidate","concept_id":"'
                '#V#existing_reusable_workflow"}],'
                '"response_text":"Reusing the existing workflow."}'
            )
        ]
    )
    env = WorkflowEnvironment(llm_client=llm, user_namespace="#V#test_user")
    discovery_result = WorkflowDiscoveryResult(
        matches=[
            WorkflowMatch(
                concept_id="#V#existing_reusable_workflow",
                name="Existing Reusable Workflow",
                description="Existing workflow candidate for reuse.",
                relevance_score=0.99,
                match_source="test",
            )
        ],
        routing_matches=[
            WorkflowMatch(
                concept_id="#V#existing_reusable_workflow",
                name="Existing Reusable Workflow",
                description="Existing workflow candidate for reuse.",
                relevance_score=0.99,
                match_source="test",
            )
        ],
        query="Reuse the existing workflow.",
        requested_query="Reuse the existing workflow.",
        allow_non_executable=True,
    )
    with patch(
        "src.backend.workflows.durable.workflow_creation_workflow.discover_workflows",
        return_value=discovery_result,
    ):
        reuse_run = WorkflowExecutor(registry=build_durable_action_registry(), max_transitions=20).run(
            wrapper_definition,
            environment=env,
            data={"prompt": "Reuse the existing workflow."},
        )

    assert reuse_run.completed is True
    assert reuse_run.data.get("workflow_authoring_preflight_decision") == "reuse"
    assert reuse_run.data.get("workflow_concept_id") == "#V#existing_reusable_workflow"
    assert reuse_run.data.get("response_text") == "Reusing the existing workflow."
