from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.testing_workflow_vontology_service import (
    CANONICAL_TESTING_WORKFLOW_IDS,
    EPHEMERAL_THEORY_GC_WORKFLOW_ID,
    MEETING_INVITATION_TESTING_WORKFLOW_ID,
    PROMOTION_GATE_WORKFLOW_ID,
    SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID,
    bootstrap_canonical_testing_workflows,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


def _relationship_targets(concept_doc: dict[str, Any] | None, predicate: str) -> list[str]:
    relationships = (concept_doc or {}).get("relationships") or {}
    raw_targets = relationships.get(predicate) or []
    if isinstance(raw_targets, str):
        return [raw_targets]
    if isinstance(raw_targets, list):
        return [str(item).strip() for item in raw_targets if str(item).strip()]
    return []


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
    report = bootstrap_canonical_testing_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 4
    assert counts.get("errors") == 0

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

    meeting_concept = concept_service.get_concept_by_concept_id(
        MEETING_INVITATION_TESTING_WORKFLOW_ID
    )
    assert meeting_concept is not None
    meeting_types = _relationship_targets(meeting_concept, "is_an_instance_of")
    assert "#V#testing_workflow" in meeting_types
    assert "#V#theory_slice_test_workflow" in meeting_types

    synthetic_concept = concept_service.get_concept_by_concept_id(
        SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID
    )
    assert synthetic_concept is not None
    synthetic_types = _relationship_targets(synthetic_concept, "is_an_instance_of")
    assert "#V#testing_workflow" in synthetic_types
    assert "#V#clone_benchmark_test_workflow" in synthetic_types

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

    meeting_note_rows = get_texts_for_concept(
        subject_concept_id=meeting_execute_step_id,
        predicate="hasNote",
        limit=10,
    )
    assert any(
        "experiment.execute_target_workflow" in str(row.get("text") or "")
        for row in meeting_note_rows
        if isinstance(row, dict)
    )

    synthetic_note_rows = get_texts_for_concept(
        subject_concept_id=suite_execute_step_id,
        predicate="hasNote",
        limit=10,
    )
    assert any(
        "experiment.execute_regression_suite" in str(row.get("text") or "")
        for row in synthetic_note_rows
        if isinstance(row, dict)
    )


def test_bootstrap_skips_republication_when_testing_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_testing_workflows()
    first_counts = (first_report.get("publication") or {}).get("counts") or {}
    assert first_counts.get("workflows_published") == 4

    second_report = bootstrap_canonical_testing_workflows()
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0
    assert second_report.get("typed_workflow_ids") == []
    assert second_report.get("typed_step_ids") == []
