from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.paper_representation_workflow_vontology_service import (
    ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_paper_representation_workflows,
)
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


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


def test_bootstrap_materialises_paper_representation_workflow_family(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_paper_representation_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 2
    assert counts.get("errors") == 0

    scholarly_definition = load_workflow_definition_from_vontology(
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert scholarly_definition is not None

    arxiv_definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert arxiv_definition is not None

    delegate_state_id = authority_service._step_concept_id(
        workflow_id=ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
        state_id="delegate_to_general_paper_workflow",
    )
    delegate_state = arxiv_definition.states[delegate_state_id]
    contract = delegate_state.metadata["subworkflow_contract"]
    assert contract["workflow_id"] == SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    assert "file_copy_concept_id" in contract["provided_inputs"]
    assert "paper_concept_id" in contract["provided_inputs"]
    assert "arxiv_id" in contract["provided_inputs"]
    assert "author_names" in contract["provided_inputs"]
    assert "topic_labels" in contract["provided_inputs"]

    scholarly_concept = concept_service.get_concept_by_concept_id(
        SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    assert scholarly_concept is not None
    scholarly_types = (scholarly_concept.get("relationships") or {}).get(
        "is_an_instance_of"
    ) or []
    assert "#V#ai_workflow" in scholarly_types
    assert "#V#durable_workflow" in scholarly_types

    step_concept = concept_service.get_concept_by_concept_id(delegate_state_id)
    assert step_concept is not None
    step_types = (step_concept.get("relationships") or {}).get("is_an_instance_of") or []
    assert "#V#workflow_step" in step_types


def test_bootstrap_skips_republication_when_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_paper_representation_workflows()
    first_counts = (first_report.get("publication") or {}).get("counts") or {}
    assert first_counts.get("workflows_published") == 2

    second_report = bootstrap_canonical_paper_representation_workflows()
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0
    assert second_report.get("typed_workflow_ids") == []
    assert second_report.get("typed_step_ids") == []
