from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from unittest.mock import MagicMock

from src.backend.services import concept_service
from src.backend.services.concept_service import ConceptNotFoundError
from src.backend.services.testing_workflow_vontology_service import (
    ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
    CAPABILITY_TEST_EXECUTION_WORKFLOW_ID,
    CANONICAL_TESTING_WORKFLOW_IDS,
    EPHEMERAL_THEORY_GC_WORKFLOW_ID,
    MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
    MEETING_INVITATION_TESTING_WORKFLOW_ID,
    PROMOTION_GATE_WORKFLOW_ID,
    SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID,
    bootstrap_canonical_testing_workflows,
    ensure_testing_type_concept_support,
)
from src.backend.services.text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from src.backend.services.workflow_prompt_authority_service import DEFAULT_PROMPT_TYPE_ID
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.subworkflow_actions import register_subworkflow_actions
from src.backend.workflows.durable.testing_workflow_actions import (
    register_testing_workflow_actions,
)
from src.backend.workflows.durable.models import WorkflowInstanceStatus
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


def _relationship_targets(concept_doc: dict[str, Any] | None, predicate: str) -> list[str]:
    relationships = (concept_doc or {}).get("relationships") or {}
    raw_targets = relationships.get(predicate) or []
    if isinstance(raw_targets, str):
        return [raw_targets]
    if isinstance(raw_targets, list):
        return [str(item).strip() for item in raw_targets if str(item).strip()]
    return []


_MEETING_INVITATION_TEST_PROMPT_TEXTS: dict[str, str] = {
    "#V#meeting_invitation_structure_prompt": (
        "Return JSON with meeting_type, title, time, participants, "
        "location_signal, topic_purpose, and safe_downstream_action."
    ),
    "#V#meeting_invitation_observation_prompt": (
        "Return an array of exactly four observation objects describing the "
        "candidate meeting workflow result."
    ),
}


def _seed_meeting_invitation_prompt_content() -> None:
    for concept_id, text in _MEETING_INVITATION_TEST_PROMPT_TEXTS.items():
        try:
            concept_service.get_concept_by_concept_id(concept_id)
        except ConceptNotFoundError:
            concept_service.create_concept(
                name=concept_id.replace("#V#", "").replace("_", " ").title(),
                concept_id=concept_id,
                description="Test prompt concept for canonical testing workflow bootstraps.",
                parent_concept_ids=[DEFAULT_PROMPT_TYPE_ID],
                create_as_instance=True,
                visibility_scope_mode="global_general",
            )
        upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate="hasContent",
            text=text,
            lang="en-NZ",
            garbage_collect=True,
        )


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

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


def test_bootstrap_materialises_testing_workflow_family(
    _reset_mock_db: Any,
) -> None:
    _seed_meeting_invitation_prompt_content()
    report = bootstrap_canonical_testing_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 7
    assert counts.get("errors") == 0
    prompt_support = report.get("prompt_support") or {}
    assert prompt_support.get("success") is True
    assert prompt_support.get("counts", {}).get("validated_prompts") == 2
    type_support = report.get("type_support") or {}
    assert type_support.get("success") is True
    assert type_support.get("counts", {}).get("errors") == 0
    assert type_support.get("counts", {}).get("description_upserts") == 8

    for workflow_id in CANONICAL_TESTING_WORKFLOW_IDS:
        definition = load_workflow_definition_from_vontology(workflow_id)
        assert definition is not None

    meeting_definition = load_workflow_definition_from_vontology(
        MEETING_INVITATION_TESTING_WORKFLOW_ID
    )
    assert meeting_definition is not None
    meeting_launch_contract = meeting_definition.metadata.get("launch_input_contract")
    assert isinstance(meeting_launch_contract, dict)
    assert meeting_launch_contract.get("schema_version") == (
        "workflow_launch_input_contract.v1"
    )
    assert meeting_launch_contract.get("required_inputs") == ["invitation_text"]
    input_mappings = meeting_launch_contract.get("input_mappings")
    assert isinstance(input_mappings, list)
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "invitation_text"
        and item.get("source_expression") == "inputs.prompt"
        and item.get("extractor") == "first_quoted_text"
        for item in input_mappings
    )
    assert all(
        not (
            isinstance(item, dict)
            and item.get("target_context_key") == "candidate_workflow_ids"
        )
        for item in input_mappings
    )

    candidate_definition = load_workflow_definition_from_vontology(
        MEETING_INVITATION_CANDIDATE_WORKFLOW_ID
    )
    assert candidate_definition is not None
    candidate_launch_contract = candidate_definition.metadata.get(
        "launch_input_contract"
    )
    assert isinstance(candidate_launch_contract, dict)
    assert candidate_launch_contract.get("required_inputs") == ["invitation_text"]
    candidate_step_id = authority_service._step_concept_id(
        workflow_id=MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
        state_id="derive_invitation_structure",
    )
    candidate_step = candidate_definition.states[candidate_step_id]
    assert len(candidate_step.actions) == 1
    candidate_action = candidate_step.actions[0]
    assert candidate_action.action_id == "llm.action"
    assert candidate_action.execution_mode == "llm"
    prompt_contract = candidate_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("resolved_prompt_concept_id") == (
        "#V#meeting_invitation_structure_prompt"
    )
    assert candidate_action.validation_policy == {"output_format": "json_value"}

    meeting_concept = concept_service.get_concept_by_concept_id(
        MEETING_INVITATION_TESTING_WORKFLOW_ID
    )
    assert meeting_concept is not None
    meeting_types = _relationship_targets(meeting_concept, "is_an_instance_of")
    assert "#V#testing_workflow" in meeting_types
    assert "#V#theory_slice_test_workflow" in meeting_types
    assert "#V#durable_workflow" in meeting_types

    candidate_concept = concept_service.get_concept_by_concept_id(
        MEETING_INVITATION_CANDIDATE_WORKFLOW_ID
    )
    assert candidate_concept is not None
    candidate_types = _relationship_targets(candidate_concept, "is_an_instance_of")
    assert "#V#ai_workflow" in candidate_types
    assert "#V#durable_workflow" in candidate_types
    assert "#V#testing_workflow" not in candidate_types

    arxiv_testing_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID
    )
    assert arxiv_testing_definition is not None
    arxiv_testing_launch_contract = arxiv_testing_definition.metadata.get(
        "launch_input_contract"
    )
    assert isinstance(arxiv_testing_launch_contract, dict)
    assert arxiv_testing_launch_contract.get("required_inputs") == ["prompt_text"]
    arxiv_input_mappings = arxiv_testing_launch_contract.get("input_mappings")
    assert isinstance(arxiv_input_mappings, list)
    assert any(
        isinstance(item, dict)
        and item.get("target_context_key") == "prompt_text"
        and item.get("source_expression") == "inputs.prompt"
        and item.get("extractor") == "identity"
        for item in arxiv_input_mappings
    )
    prepare_fixture_state = arxiv_testing_definition.states.get(
        authority_service._step_concept_id(
            workflow_id=ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
            state_id="prepare_fixture",
        )
    )
    assert prepare_fixture_state is not None
    prepare_fixture_action = prepare_fixture_state.actions[0]
    assert prepare_fixture_action.inputs.get("repair_existing_artifacts") is True
    execute_target_state = arxiv_testing_definition.states.get(
        authority_service._step_concept_id(
            workflow_id=ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
            state_id="execute_target_workflow",
        )
    )
    assert execute_target_state is not None
    execute_target_action = execute_target_state.actions[0]
    workflow_inputs = execute_target_action.inputs.get("workflow_inputs")
    assert isinstance(workflow_inputs, dict)
    assert workflow_inputs.get("prompt") == {"$context_key": "prompt_text"}
    assert workflow_inputs.get("original_filename") == {
        "$context_key": "original_filename"
    }
    assert workflow_inputs.get("verification_profile") == "arxiv"
    assert execute_target_action.inputs.get("await_terminal") is True
    assert execute_target_action.inputs.get("timeout_seconds") == 2400
    assert execute_target_action.inputs.get("poll_interval_seconds") == 5
    cleanup_state = arxiv_testing_definition.states.get(
        authority_service._step_concept_id(
            workflow_id=ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
            state_id="cleanup_artifacts",
        )
    )
    assert cleanup_state is not None
    cleanup_action = cleanup_state.actions[0]
    cleanup_file_copy_ids = cleanup_action.inputs.get("file_copy_concept_ids")
    assert isinstance(cleanup_file_copy_ids, dict)
    assert cleanup_file_copy_ids.get("$context_key") == "file_copy_concept_ids"
    arxiv_testing_concept = concept_service.get_concept_by_concept_id(
        ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID
    )
    assert arxiv_testing_concept is not None
    arxiv_testing_types = _relationship_targets(
        arxiv_testing_concept,
        "is_an_instance_of",
    )
    assert "#V#testing_workflow" in arxiv_testing_types
    assert "#V#theory_slice_test_workflow" in arxiv_testing_types

    synthetic_concept = concept_service.get_concept_by_concept_id(
        SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID
    )
    assert synthetic_concept is not None
    synthetic_types = _relationship_targets(synthetic_concept, "is_an_instance_of")
    assert "#V#testing_workflow" in synthetic_types
    assert "#V#clone_benchmark_test_workflow" in synthetic_types
    synthetic_definition = load_workflow_definition_from_vontology(
        SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID
    )
    assert synthetic_definition is not None

    capability_definition = load_workflow_definition_from_vontology(
        CAPABILITY_TEST_EXECUTION_WORKFLOW_ID
    )
    assert capability_definition is not None
    capability_concept = concept_service.get_concept_by_concept_id(
        CAPABILITY_TEST_EXECUTION_WORKFLOW_ID
    )
    assert capability_concept is not None
    capability_types = _relationship_targets(
        capability_concept,
        "is_an_instance_of",
    )
    assert "#V#testing_workflow" in capability_types
    assert "#V#theory_slice_test_workflow" in capability_types
    capability_validate_step_id = authority_service._step_concept_id(
        workflow_id=CAPABILITY_TEST_EXECUTION_WORKFLOW_ID,
        state_id="validate_candidate_workflow",
    )
    capability_validate_step = capability_definition.states[capability_validate_step_id]
    assert capability_validate_step.actions[0].action_id == (
        "testing.validate_candidate_workflow"
    )
    capability_execute_step_id = authority_service._step_concept_id(
        workflow_id=CAPABILITY_TEST_EXECUTION_WORKFLOW_ID,
        state_id="execute_target_workflow",
    )
    capability_execute_step = capability_definition.states[capability_execute_step_id]
    assert capability_execute_step.actions[0].action_id == (
        "experiment.execute_target_workflow"
    )
    assert any(
        transition.reason == "on_failure"
        and transition.to_state
        == authority_service._step_concept_id(
            workflow_id=CAPABILITY_TEST_EXECUTION_WORKFLOW_ID,
            state_id="compute_experiment_verdict",
        )
        for transition in capability_execute_step.transitions
    )

    promotion_concept = concept_service.get_concept_by_concept_id(
        PROMOTION_GATE_WORKFLOW_ID
    )
    assert promotion_concept is not None
    assert "#V#testing_workflow" in _relationship_targets(
        promotion_concept,
        "is_an_instance_of",
    )

    gc_concept = concept_service.get_concept_by_concept_id(
        EPHEMERAL_THEORY_GC_WORKFLOW_ID
    )
    assert gc_concept is not None
    assert "#V#testing_workflow" in _relationship_targets(
        gc_concept,
        "is_an_instance_of",
    )

    meeting_execute_step_id = authority_service._step_concept_id(
        workflow_id=MEETING_INVITATION_TESTING_WORKFLOW_ID,
        state_id="execute_candidate_workflow",
    )
    meeting_execute_step = concept_service.get_concept_by_concept_id(
        meeting_execute_step_id
    )
    assert meeting_execute_step is not None
    assert "#V#workflow_step" in _relationship_targets(
        meeting_execute_step,
        "is_an_instance_of",
    )

    suite_execute_step_id = authority_service._step_concept_id(
        workflow_id=SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID,
        state_id="execute_regression_suite",
    )
    suite_execute_step = concept_service.get_concept_by_concept_id(suite_execute_step_id)
    assert suite_execute_step is not None
    assert "#V#workflow_step" in _relationship_targets(
        suite_execute_step,
        "is_an_instance_of",
    )
    meeting_execute_action = meeting_definition.states[meeting_execute_step_id].actions[0]
    assert meeting_execute_action.action_id == "workflow_invoke_subworkflow"
    assert meeting_execute_action.inputs.get("workflow_id") == (
        MEETING_INVITATION_CANDIDATE_WORKFLOW_ID
    )
    meeting_prepare_step_id = authority_service._step_concept_id(
        workflow_id=MEETING_INVITATION_TESTING_WORKFLOW_ID,
        state_id="prepare_spec",
    )
    meeting_prepare_step = meeting_definition.states[meeting_prepare_step_id]
    meeting_prepare_action = meeting_prepare_step.actions[0]
    assert meeting_prepare_action.action_id == "testing.prepare_experiment_spec"
    assert meeting_prepare_step.metadata.get("reads_context_keys") == [
        "invitation_text"
    ]
    assert meeting_prepare_action.inputs.get("scenario_template") == {
        "schema_version": "testing_experiment_scenario_template.v1",
        "name": "Meeting invitation workflow derivation and validation",
        "description": "Testing spec for deriving, selecting, and validating a meeting-invitation workflow.",
        "fixture_payload": {
            "invitation_text": {"$input": "invitation_text"},
        },
        "expected_outcomes": [
            {
                "label": "meeting_type_classification",
                "expected": {
                    "$input": "expected_meeting_type",
                    "$default": "meeting_workflow_candidate",
                },
            },
            {
                "label": "structured_meeting_fields",
                "expected_fields": {
                    "$input": "expected_structure_fields",
                    "$default": ["title", "time", "participants"],
                },
            },
            {
                "label": "mutation_safety",
                "expected": "no_canonical_mutations_without_gate",
            },
            {
                "$if_input": "expected_downstream_actions",
                "then": {
                    "label": "downstream_actions",
                    "expected_actions": {"$input": "expected_downstream_actions"},
                },
            },
        ],
        "theory_setup": {
            "seed_claims": [
                {
                    "source_id": "#V#meeting_invitation_testing_workflow",
                    "predicate": "#V#has_hypothesis",
                    "target": {
                        "$input": "expected_meeting_type",
                        "$default": "meeting invitation should resolve to a safe workflow candidate",
                    },
                    "target_kind": "text",
                },
                {
                    "source_id": "#V#meeting_invitation_testing_workflow",
                    "predicate": "#V#has_assumption",
                    "target": "No canonical calendar or task mutation is permitted during testing.",
                    "target_kind": "text",
                },
            ]
        },
        "forbidden_side_effects": [
            "canonical_calendar_mutation",
            "canonical_task_mutation",
            "external_action_without_gate",
        ],
        "verdict_rules": {
            "require_all_expected_outcomes": True,
        },
        "replay_policy": {
            "retain_failing_cases": True,
            "retain_passing_cases": False,
        },
        "promotion_policy": {
            "requires_manual_gate": True,
        },
        "metadata": {
            "scenario": "meeting_invitation_testing",
        },
    }
    suite_execute_action = synthetic_definition.states[suite_execute_step_id].actions[0]
    assert suite_execute_action.inputs.get("suite_policy") == {
        "schema_version": "testing_regression_suite_policy.v1",
        "default_execution_tier": "tier1",
        "tiers": {
            "tier1": {"mode": "cases"},
            "tier2": {
                "mode": "benchmark",
                "output_root_default": "data/testing_workflows/benchmarks",
            },
            "benchmark": {"alias_for": "tier2"},
            "tier_2": {"alias_for": "tier2"},
        },
    }
    structure_prompt_rows = get_texts_for_concept(
        subject_concept_id="#V#meeting_invitation_structure_prompt",
        predicate="hasContent",
        limit=5,
    )
    assert any(
        "meeting_type, title, time, participants, location_signal" in str(
            row.get("text") or ""
        )
        for row in structure_prompt_rows
        if isinstance(row, dict)
    )
    observation_prompt_rows = get_texts_for_concept(
        subject_concept_id="#V#meeting_invitation_observation_prompt",
        predicate="hasContent",
        limit=5,
    )
    assert any(
        "array of exactly four observation objects" in str(row.get("text") or "")
        for row in observation_prompt_rows
        if isinstance(row, dict)
    )


def test_bootstrap_skips_republication_when_testing_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    _seed_meeting_invitation_prompt_content()
    first_report = bootstrap_canonical_testing_workflows()
    first_counts = (first_report.get("publication") or {}).get("counts") or {}
    assert first_counts.get("workflows_published") == 7

    second_report = bootstrap_canonical_testing_workflows()
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0
    assert second_report.get("typed_workflow_ids") == []
    assert second_report.get("typed_step_ids") == []


def test_bootstrap_can_force_republish_when_testing_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    _seed_meeting_invitation_prompt_content()
    bootstrap_canonical_testing_workflows()

    forced_report = bootstrap_canonical_testing_workflows(force_republish=True)
    publication = forced_report.get("publication") or {}
    counts = publication.get("counts") or {}

    assert publication.get("skipped") is not True
    assert publication.get("forced_republish") is True
    assert counts.get("workflows_published") == 7
    assert counts.get("errors") == 0


def test_bootstrap_preserves_authoritative_vontology_mapping_edits_when_family_remains_valid(
    _reset_mock_db: Any,
) -> None:
    _seed_meeting_invitation_prompt_content()
    bootstrap_canonical_testing_workflows()

    mapping_concept_id = authority_service._runtime_context_input_mapping_concept_id(
        workflow_id=MEETING_INVITATION_TESTING_WORKFLOW_ID,
        state_id="prepare_spec",
        tool_param="expected_meeting_type",
        context_key="expected_meeting_type",
    )
    concept_service.update_concept(
        mapping_concept_id,
        {"concept_data.workflow_mapping_spec.required": True},
    )

    follow_up_report = bootstrap_canonical_testing_workflows()
    publication = follow_up_report.get("publication") or {}

    assert publication.get("skipped") is True
    assert publication.get("skip_reason") == "existing_materialisation_valid"

    meeting_definition = load_workflow_definition_from_vontology(
        MEETING_INVITATION_TESTING_WORKFLOW_ID
    )
    assert meeting_definition is not None
    prepare_step_id = authority_service._step_concept_id(
        workflow_id=MEETING_INVITATION_TESTING_WORKFLOW_ID,
        state_id="prepare_spec",
    )
    prepare_step = meeting_definition.states[prepare_step_id]
    assert set(prepare_step.metadata.get("reads_context_keys") or []) == {
        "expected_meeting_type",
        "invitation_text",
    }


def test_meeting_invitation_testing_workflow_executes_end_to_end_via_vontology(
    _reset_mock_db: Any,
) -> None:
    _seed_meeting_invitation_prompt_content()
    bootstrap_canonical_testing_workflows()
    meeting_definition = load_workflow_definition_from_vontology(
        MEETING_INVITATION_TESTING_WORKFLOW_ID
    )
    assert meeting_definition is not None

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    register_subworkflow_actions(
        registry,
        definition_loader=load_workflow_definition_from_vontology,
    )

    llm_client = MagicMock()
    llm_client.generate.side_effect = [
        (
            '{"meeting_type":"project_meeting","title":"Roadmap sync",'
            '"time":"Monday 10am","participants":"team",'
            '"location_signal":"Room 4","topic_purpose":"Discuss roadmap",'
            '"safe_downstream_action":"draft_calendar_entry"}'
        ),
        (
            '[{"label":"meeting_type_classification","verdict":"pass",'
            '"expected_outcome":"project_meeting",'
            '"observed_outcome":"project_meeting"},'
            '{"label":"structured_meeting_fields","verdict":"pass",'
            '"expected_outcome":"title,time,participants,location_signal,topic_purpose",'
            '"observed_outcome":"all expected fields present"},'
            '{"label":"mutation_safety","verdict":"pass",'
            '"expected_outcome":"no_canonical_mutations_without_gate",'
            '"observed_outcome":"no canonical mutations attempted"},'
            '{"label":"downstream_actions","verdict":"pass",'
            '"expected_outcome":"draft_calendar_entry",'
            '"observed_outcome":"draft_calendar_entry"}]'
        ),
    ]

    result = WorkflowExecutor(registry=registry, max_transitions=30).run(
        meeting_definition,
        environment=WorkflowEnvironment(
            llm_client=llm_client,
            user_namespace="#V#user@org",
        ),
        data={
            "invitation_text": (
                "Please meet on Monday at 10am in Room 4 to discuss the roadmap."
            ),
            "expected_meeting_type": "project_meeting",
            "expected_structure_fields": [
                "title",
                "time",
                "participants",
                "location_signal",
                "topic_purpose",
            ],
            "expected_downstream_actions": ["draft_calendar_entry"],
        },
    )

    assert result.completed is True
    assert result.error is None
    assert result.final_state == authority_service._step_concept_id(
        workflow_id=MEETING_INVITATION_TESTING_WORKFLOW_ID,
        state_id="complete",
    )
    assert result.data["candidate_meeting_type"] == "project_meeting"
    assert result.data["candidate_safe_downstream_action"] == "draft_calendar_entry"
    assert result.data["verdict"] == "pass"
    assert len(result.data["meeting_candidate_observations"]) == 4


def test_meeting_invitation_testing_workflow_executes_with_optional_expectations_omitted(
    _reset_mock_db: Any,
) -> None:
    _seed_meeting_invitation_prompt_content()
    bootstrap_canonical_testing_workflows()
    meeting_definition = load_workflow_definition_from_vontology(
        MEETING_INVITATION_TESTING_WORKFLOW_ID
    )
    assert meeting_definition is not None

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    register_subworkflow_actions(
        registry,
        definition_loader=load_workflow_definition_from_vontology,
    )

    llm_client = MagicMock()
    llm_client.generate.side_effect = [
        (
            '{"meeting_type":"meeting_workflow_candidate","title":"Roadmap sync",'
            '"time":"Tuesday 2:00pm","participants":"team",'
            '"location_signal":"Room 4","topic_purpose":"Project planning",'
            '"safe_downstream_action":"draft_calendar_entry"}'
        ),
        (
            '[{"label":"meeting_type_classification","verdict":"pass",'
            '"expected_outcome":"meeting_workflow_candidate",'
            '"observed_outcome":"meeting_workflow_candidate"},'
            '{"label":"structured_meeting_fields","verdict":"pass",'
            '"expected_outcome":"title,time,participants",'
            '"observed_outcome":"all expected fields present"},'
            '{"label":"mutation_safety","verdict":"pass",'
            '"expected_outcome":"no_canonical_mutations_without_gate",'
            '"observed_outcome":"no canonical mutations attempted"}]'
        ),
    ]

    result = WorkflowExecutor(registry=registry, max_transitions=30).run(
        meeting_definition,
        environment=WorkflowEnvironment(
            llm_client=llm_client,
            user_namespace="#V#user@org",
        ),
        data={
            "invitation_text": (
                "Please join us on Tuesday at 2:00pm in Room 4 for project planning."
            )
        },
    )

    assert result.completed is True
    assert result.error is None
    assert result.final_state == authority_service._step_concept_id(
        workflow_id=MEETING_INVITATION_TESTING_WORKFLOW_ID,
        state_id="complete",
    )
    assert result.data["candidate_meeting_type"] == "meeting_workflow_candidate"
    assert result.data["verdict"] == "pass"
    assert len(result.data["meeting_candidate_observations"]) == 3


@dataclass
class _StubWorkflowInstance:
    instance_id: str
    workflow_id: str
    status: WorkflowInstanceStatus
    current_state: str = "complete"
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    error: str | None = None
    error_step: str | None = None
    user_id: str = "#V#user"
    org_id: str = "#V#org"
    namespace: str = "#V#user@org"

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "current_state": self.current_state,
            "error": self.error,
            "error_step": self.error_step,
        }


@dataclass
class _StubWorkflowManager:
    last_call: dict[str, Any] | None = None
    instances_by_id: dict[str, Any] | None = None

    def create_instance(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        inputs: dict[str, Any] | None = None,
        max_retries: int = 3,
        **_kwargs: Any,
    ) -> str:
        self.last_call = {
            "workflow_id": workflow_id,
            "user_id": user_id,
            "org_id": org_id,
            "namespace": namespace,
            "inputs": dict(inputs or {}),
            "max_retries": max_retries,
        }
        return "#V#wf_instance_arxiv_ingestion_test"

    def get_instance(self, instance_id: str) -> Any | None:
        return (self.instances_by_id or {}).get(instance_id)


def test_arxiv_paper_ingestion_testing_workflow_executes_end_to_end_via_vontology(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import testing_workflow_actions as actions_module
    from src.backend.services.arxiv_paper_link_service import (
        predict_arxiv_paper_concept_id,
    )
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        WorkflowInstanceSubmissionResult,
    )

    _seed_meeting_invitation_prompt_content()
    bootstrap_canonical_testing_workflows()
    definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID
    )
    assert definition is not None
    paper_concept_id = predict_arxiv_paper_concept_id(arxiv_id="2603.21702")

    monkeypatch.setattr(
        actions_module,
        "prepare_arxiv_paper_ingestion_test_fixture",
        lambda **_kwargs: {
            "success": True,
            "prompt_text": "Run the arXiv ingestion test on https://arxiv.org/abs/2603.21702",
            "arxiv_id": "2603.21702",
            "source_uri": "https://arxiv.org/abs/2603.21702",
            "original_filename": "2603.21702v1.pdf",
            "expected_title": "A very obscure paper",
            "expected_summary": "An abstract about an obscure but useful result.",
            "expected_publication_date": "2026-03-25",
            "expected_author_names": ["Author One", "Author Two"],
            "expected_author_concept_ids": ["#V#author_one", "#V#author_two"],
            "expected_topic_labels": ["cs.AI"],
            "expected_topic_concept_ids": ["#V#topic_cs_ai"],
            "preexisting_author_concept_ids": [],
            "preexisting_topic_concept_ids": [],
            "paper_concept_id": paper_concept_id,
        },
    )
    monkeypatch.setattr(
        actions_module,
        "verify_arxiv_paper_ingestion_test_result",
        lambda **_kwargs: {
            "success": True,
            "verification_passed": True,
            "paper_concept_id": paper_concept_id,
            "file_copy_concept_id": "#V#file_copy_2603_21702",
            "file_copy_concept_ids": [
                "#V#file_copy_2603_21702",
                "#V#markdown_file_copy_2603_21702",
            ],
            "author_concept_ids": ["#V#author_one", "#V#author_two"],
            "topic_concept_ids": ["#V#topic_cs_ai"],
            "metadata_verification": {
                "title_matched": True,
                "summary_matched": True,
                "publication_date_matched": True,
                "author_ids_matched": True,
                "author_names_matched": True,
                "topic_ids_matched": True,
                "arxiv_identifier_preserved": True,
                "source_uri_preserved": True,
                "file_copy_link_preserved": True,
            },
            "observations": [
                {
                    "label": "metadata_representation",
                    "verdict": "pass",
                    "expected_outcome": "title, authors, abstract, and publication date represented",
                    "observed_outcome": "target workflow completed, title matched, abstract matched, publication date matched",
                },
                {
                    "label": "author_links",
                    "verdict": "pass",
                    "expected_outcome": "all expected authors and topics linked to the paper",
                    "observed_outcome": "all expected author links present, author names preserved, topic links present",
                },
                {
                    "label": "provenance_preserved",
                    "verdict": "pass",
                    "expected_outcome": "arXiv identifiers, source URL, and file copy link preserved",
                    "observed_outcome": "arXiv identifier preserved, source URI preserved, file copy linked",
                },
            ],
        },
    )
    monkeypatch.setattr(
        actions_module,
        "cleanup_arxiv_paper_ingestion_test_artifacts",
        lambda **_kwargs: {
            "success": True,
            "cleanup_passed": True,
            "cleanup_summary": {
                "cleanup_passed": True,
                "deleted_concept_ids": [
                    paper_concept_id,
                    "#V#file_copy_2603_21702",
                    "#V#markdown_file_copy_2603_21702",
                ],
                "failed_deletions": [],
            },
            "observations": [
                {
                    "label": "cleanup_completed",
                    "verdict": "pass",
                    "expected_outcome": "transient ingestion artefacts removed",
                    "observed_outcome": "deleted 3 transient concepts",
                }
            ],
        },
    )

    manager = _StubWorkflowManager(
        instances_by_id={
            "#V#wf_instance_arxiv_ingestion_test": _StubWorkflowInstance(
                instance_id="#V#wf_instance_arxiv_ingestion_test",
                workflow_id="#V#arxiv_paper_representation_workflow",
                status=WorkflowInstanceStatus.COMPLETED,
                outputs={
                    "paper_concept_id": paper_concept_id,
                    "file_copy_concept_id": "#V#file_copy_2603_21702",
                },
            )
        }
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    cleanup_docs = {
        paper_concept_id: {
            "relationships": {
                "#V#specific_to_user": ["#V#user"],
                "#V#propositional_information_thing_has_computer_file": [
                    "#V#file_copy_2603_21702",
                    "#V#markdown_file_copy_2603_21702",
                ],
            }
        },
        "#V#file_copy_2603_21702": {
            "relationships": {"#V#specific_to_user": ["#V#user"]}
        },
        "#V#markdown_file_copy_2603_21702": {
            "relationships": {"#V#specific_to_user": ["#V#user"]}
        },
        "#V#author_one": {
            "relationships": {"#V#specific_to_user": ["#V#user"]}
        },
        "#V#author_two": {
            "relationships": {"#V#specific_to_user": ["#V#user"]}
        },
        "#V#topic_cs_ai": {
            "relationships": {"#V#specific_to_user": ["#V#user"]}
        },
    }
    monkeypatch.setattr(
        actions_module,
        "_load_cleanup_concept_for_authority",
        lambda concept_id: cleanup_docs.get(concept_id),
    )

    def _fake_submit_verified_workflow_instance(**kwargs: Any) -> WorkflowInstanceSubmissionResult:
        workflow_id = str(kwargs.get("workflow_id") or "").strip()
        instance_id = manager.create_instance(
            workflow_id,
            user_id=str(kwargs.get("user_id") or "anonymous").strip() or "anonymous",
            org_id=str(kwargs.get("org_id") or "default").strip() or "default",
            namespace=str(kwargs.get("namespace") or "#V#anonymous@default").strip()
            or "#V#anonymous@default",
            inputs=dict(kwargs.get("inputs") or {}),
            max_retries=int(kwargs.get("max_retries", 3) or 3),
        )
        return WorkflowInstanceSubmissionResult(
            success=True,
            workflow_id=workflow_id,
            status="pending",
            instance_id=instance_id,
            verification={
                "preflight_passed": True,
                "postflight_passed": True,
                "runnable_verification_success": True,
                "preflight": {"errors": []},
                "postflight": {"errors": []},
            },
            created_new=True,
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
        _fake_submit_verified_workflow_instance,
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)

    result = WorkflowExecutor(registry=registry, max_transitions=40).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            user_namespace="#V#user@org",
        ),
        data={
            "prompt_text": "Run the arXiv paper ingestion testing workflow on https://arxiv.org/abs/2603.21702",
        },
    )

    assert result.completed is True
    assert result.error is None
    assert result.final_state == authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
        state_id="complete",
    )
    assert result.data["run_id"].startswith("#V#experiment_run_")
    assert result.data["verdict"] == "pass"
    assert result.data["workflow_execution"]["final_status"] == "completed"
    assert result.data["metadata_verification"]["publication_date_matched"] is True
    assert result.data["cleanup_summary"]["cleanup_passed"] is True
    assert len(result.data["observations"]) == 5


def test_ensure_testing_type_concept_support_materialises_testing_types(
    _reset_mock_db: Any,
) -> None:
    concept_service.create_concept(
        name="Thing",
        concept_id="#V#thing",
        description="Minimal root type for testing bootstrap.",
        parent_concept_ids=[],
        create_as_instance=False,
        visibility_scope_mode="global_general",
    )
    concept_service.create_concept(
        name="Information",
        concept_id="#V#information",
        description="Minimal information type for testing bootstrap.",
        parent_concept_ids=["#V#thing"],
        create_as_instance=False,
        visibility_scope_mode="global_general",
    )
    concept_service.create_concept(
        name="Event",
        concept_id="#V#event",
        description="Minimal event type for testing bootstrap.",
        parent_concept_ids=["#V#thing"],
        create_as_instance=False,
        visibility_scope_mode="global_general",
    )

    report = ensure_testing_type_concept_support()

    assert report["success"] is True
    assert report["counts"]["created"] == 8
    for concept_id in report["type_concept_ids"]:
        concept = concept_service.get_concept_by_concept_id(concept_id)
        assert concept is not None
        assert concept.get("concept_id") == concept_id
