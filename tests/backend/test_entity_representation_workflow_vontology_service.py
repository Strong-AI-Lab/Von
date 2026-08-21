from __future__ import annotations

import json
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
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
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

    template_publication = report.get("template_publication") or {}
    assert template_publication.get("repo_seed_version") == "5"
    assert (template_publication.get("counts") or {}).get("persisted_templates") == 4

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
    input_mappings = launch_contract.get("input_mappings") or []
    assert {
        mapping.get("target_context_key")
        for mapping in input_mappings
        if isinstance(mapping, dict)
    } >= {"prompt", "augmented_context", "conversation_situation"}

    preflight_state = next(
        state
        for state_id, state in definition.states.items()
        if state_id.endswith("_decide_entity_path")
    )
    context_keys = {
        item.get("context_key")
        for item in (preflight_state.metadata.get("llm_policy") or {}).get(
            "context_fields", []
        )
        if isinstance(item, dict)
    }
    assert {"augmented_context", "conversation_situation"}.issubset(context_keys)

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

    dispatch_mapping_concept_ids: set[str] = set()
    for state_suffix in (
        "execute_person_workflow",
        "execute_company_workflow",
        "execute_event_workflow",
        "execute_place_workflow",
    ):
        state = next(
            state
            for state_id, state in definition.states.items()
            if state_id.endswith("_" + state_suffix)
        )
        assert state.metadata.get("writes_context_keys") in (None, [])
        output_mappings = state.metadata.get("tool_output_context_mappings") or []
        assert any(
            isinstance(mapping, dict)
            and mapping.get("context_key") == "child_workflow_failed"
            for mapping in output_mappings
        )
        subworkflow_contract = state.metadata.get("subworkflow_contract") or {}
        assert subworkflow_contract.get("failure_mode") == "capture_child_failure"
        assert "prompt" in (state.metadata.get("reads_context_keys") or [])

        state_mapping_ids = {
            str(mapping.get("mapping_concept_id") or "").strip()
            for mapping in [
                *(subworkflow_contract.get("input_mappings") or []),
                *output_mappings,
            ]
            if isinstance(mapping, dict)
            and str(mapping.get("mapping_concept_id") or "").strip()
        }
        assert len(state_mapping_ids) == 8
        assert dispatch_mapping_concept_ids.isdisjoint(state_mapping_ids)
        dispatch_mapping_concept_ids.update(state_mapping_ids)

    assert len(dispatch_mapping_concept_ids) == 32

    discovery_state = next(
        state
        for state_id, state in definition.states.items()
        if state_id.endswith("_discover_existing_workflows")
    )
    candidate_workflow_ids = discovery_state.actions[0].inputs.get(
        "candidate_workflow_ids"
    )
    assert isinstance(candidate_workflow_ids, str)
    assert json.loads(candidate_workflow_ids) == [
        "#V#person_representation_workflow",
        "#V#company_representation_workflow",
        "#V#event_representation_workflow",
        "#V#place_representation_workflow",
    ]

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
    second_template_publication = second_report.get("template_publication") or {}
    assert (second_template_publication.get("counts") or {}).get(
        "skipped_current_templates"
    ) == 4
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
    assert "requested_facts must be an array" in payload_text
    assert "does not stop being requested" in payload_text
    assert "does not establish it or satisfy the requested fact" in payload_text
