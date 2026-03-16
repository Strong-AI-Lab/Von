from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.talk_representation_workflow_vontology_service import (
    ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID,
    TALK_REPRESENTATION_WORKFLOW_ID,
    TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_talk_representation_workflows,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows.durable import registry_factory
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


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()
    registry_factory._resolve_subworkflow_definition.cache_clear()

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
    registry_factory._resolve_subworkflow_definition.cache_clear()


def test_bootstrap_materialises_talk_representation_workflow_family(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_talk_representation_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 3
    assert counts.get("errors") == 0

    for workflow_id in (
        TALK_REPRESENTATION_WORKFLOW_ID,
        TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID,
        ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID,
    ):
        definition = load_workflow_definition_from_vontology(workflow_id)
        assert definition is not None

    academic_workflow_concept = concept_service.get_concept_by_concept_id(
        ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID
    )
    assert academic_workflow_concept is not None

    academic_instance_types = _relationship_targets(
        academic_workflow_concept,
        "is_an_instance_of",
    )
    assert "#V#ai_workflow" in academic_instance_types
    assert "#V#durable_workflow" in academic_instance_types

    academic_type_parents = _relationship_targets(
        academic_workflow_concept,
        "is_a_type_of",
    )
    assert "#V#durable_workflow" not in academic_type_parents

    delegate_step_concept_id = authority_service._step_concept_id(
        workflow_id=ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID,
        state_id="delegate_to_talk_representation",
    )
    delegate_step_concept = concept_service.get_concept_by_concept_id(
        delegate_step_concept_id
    )
    assert delegate_step_concept is not None
    assert "#V#workflow_step" in _relationship_targets(
        delegate_step_concept,
        "is_an_instance_of",
    )


def test_bootstrap_skips_republication_when_talk_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_talk_representation_workflows()
    first_counts = (first_report.get("publication") or {}).get("counts") or {}
    assert first_counts.get("workflows_published") == 3

    second_report = bootstrap_canonical_talk_representation_workflows()
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0
    assert second_report.get("typed_workflow_ids") == []
    assert second_report.get("typed_step_ids") == []


def test_technical_scientific_talk_workflow_materialises_and_verifies_representation(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_talk_representation_workflows()
    registry_factory._resolve_subworkflow_definition.cache_clear()

    definition = load_workflow_definition_from_vontology(
        TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    action_registry = registry_factory.build_durable_action_registry()
    result = WorkflowExecutor(registry=action_registry, max_transitions=20).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace="#V#test_user",
        ),
        data={
            "title": "Towards Advanced Academic Graph Modeling",
            "speaker_name": "Junyi Chen",
            "summary": "Technical talk on advanced academic graph modeling.",
            "start_time": "2026-03-17T10:00:00+13:00",
            "meeting_link": "https://example.invalid/meet/junyi-chen-talk",
            "meeting_id": "987 654 321",
            "meeting_passcode": "246810",
            "presentation_status": "scheduled",
        },
    )

    assert result.completed is True
    assert result.data.get("talk_representation_materialised") is True
    assert result.data.get("talk_representation_verified") is True
    assert result.data.get("verification_profile") == "technical_scientific_talk"

    presentation_concept_id = str(result.data.get("presentation_concept_id") or "").strip()
    speaker_concept_id = str(result.data.get("speaker_concept_id") or "").strip()
    assert presentation_concept_id
    assert speaker_concept_id

    presentation_concept = concept_service.get_concept_by_concept_id(
        presentation_concept_id
    )
    assert presentation_concept is not None
    presentation_types = _relationship_targets(
        presentation_concept,
        "is_an_instance_of",
    )
    assert "#V#presentation" in presentation_types
    assert "#V#scientific_presentation" in presentation_types

    speaker_concept = concept_service.get_concept_by_concept_id(speaker_concept_id)
    assert speaker_concept is not None
    presenter_targets = _relationship_targets(
        speaker_concept,
        "#V#presenter_of_presentation",
    )
    assert presentation_concept_id in presenter_targets

    description_rows = get_texts_for_concept(
        subject_concept_id=presentation_concept_id,
        predicate="hasDescription",
        limit=10,
    )
    assert any(
        row.get("text") == "Technical talk on advanced academic graph modeling."
        for row in description_rows
        if isinstance(row, dict)
    )

    status_rows = get_texts_for_concept(
        subject_concept_id=presentation_concept_id,
        predicate="#V#presentation_status",
        limit=10,
    )
    assert any(
        row.get("text") == "scheduled"
        for row in status_rows
        if isinstance(row, dict)
    )
