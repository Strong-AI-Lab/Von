from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.entity_representation_workflow_vontology_service import (
    ENTITY_REPRESENTATION_PAYLOAD_PROMPT_CONCEPT_ID,
    ENTITY_REPRESENTATION_PREFLIGHT_PROMPT_CONCEPT_ID,
    _ensure_entity_representation_prompt_support,
    ENTITY_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_entity_representation_workflows,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_input_contract,
    resolve_workflow_routing_profile,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
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


def test_bootstrap_materialises_entity_representation_workflow_family(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_entity_representation_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 1
    assert counts.get("errors") == 0

    definition = load_workflow_definition_from_vontology(
        ENTITY_REPRESENTATION_WORKFLOW_ID
    )
    assert definition is not None

    terminal_contract = definition.metadata.get("terminal_success_contract")
    assert isinstance(terminal_contract, dict), (
        "entity representation workflow must have a terminal success contract"
    )
    assert terminal_contract.get("success_statuses") == ["completed"]
    assert terminal_contract.get("require_completed_true") is True

    launch_contract, launch_source = resolve_workflow_launch_input_contract(
        ENTITY_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(launch_contract, dict)
    assert launch_source.startswith("text_relation:")
    assert launch_contract.get("required_inputs") == ["prompt"]

    routing_profile, routing_source = resolve_workflow_routing_profile(
        ENTITY_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(routing_profile, dict)
    assert routing_source.startswith("text_relation:")
    assert routing_profile.get("role") == "execution"

    discovery_exemplars, discovery_source = resolve_workflow_discovery_exemplars(
        ENTITY_REPRESENTATION_WORKFLOW_ID
    )
    assert isinstance(discovery_exemplars, dict)
    assert discovery_source.startswith("text_relation:")
    assert "entity representation workflow" in (
        discovery_exemplars.get("keywords") or []
    )

    preflight_prompt_rows = get_texts_for_concept(
        ENTITY_REPRESENTATION_PREFLIGHT_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    payload_prompt_rows = get_texts_for_concept(
        ENTITY_REPRESENTATION_PAYLOAD_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert preflight_prompt_rows
    assert payload_prompt_rows


def test_bootstrap_skips_republication_when_entity_workflow_family_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_entity_representation_workflows()
    assert ((first_report.get("publication") or {}).get("counts") or {}).get(
        "workflows_published"
    ) == 1

    second_report = bootstrap_canonical_entity_representation_workflows()
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0


def test_entity_representation_prompt_support_seeds_content_from_repo_assets(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_entity_representation_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 2

    preflight_rows = get_texts_for_concept(
        ENTITY_REPRESENTATION_PREFLIGHT_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    payload_rows = get_texts_for_concept(
        ENTITY_REPRESENTATION_PAYLOAD_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )

    preflight_text = next(
        ((row or {}).get("text") for row in preflight_rows if (row or {}).get("text")),
        "",
    )
    payload_text = next(
        ((row or {}).get("text") for row in payload_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(preflight_text, str)
    assert isinstance(payload_text, str)
    assert "workflow_creation_prompt must begin with" in preflight_text
    assert "entity_source_text should preserve" in payload_text
