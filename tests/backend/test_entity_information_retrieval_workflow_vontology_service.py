from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.entity_information_retrieval_workflow_vontology_service import (
    ENTITY_INFORMATION_RETRIEVAL_PROMPT_CONCEPT_ID,
    ENTITY_INFORMATION_RETRIEVAL_WORKFLOW_ID,
    _ensure_entity_information_retrieval_prompt_support,
    bootstrap_canonical_entity_information_retrieval_workflow,
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


def test_bootstrap_materialises_entity_information_retrieval_workflow(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_entity_information_retrieval_workflow()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 1
    assert counts.get("errors") == 0

    definition = load_workflow_definition_from_vontology(
        ENTITY_INFORMATION_RETRIEVAL_WORKFLOW_ID
    )
    assert definition is not None

    routing_profile, routing_source = resolve_workflow_routing_profile(
        ENTITY_INFORMATION_RETRIEVAL_WORKFLOW_ID
    )
    assert isinstance(routing_profile, dict)
    assert routing_source.startswith("text_relation:")
    assert routing_profile.get("role") == "execution"

    discovery_exemplars, discovery_source = resolve_workflow_discovery_exemplars(
        ENTITY_INFORMATION_RETRIEVAL_WORKFLOW_ID
    )
    assert isinstance(discovery_exemplars, dict)
    assert discovery_source.startswith("text_relation:")
    assert "who am i and list my papers" in (discovery_exemplars.get("keywords") or [])

    launch_contract, launch_source = resolve_workflow_launch_input_contract(
        ENTITY_INFORMATION_RETRIEVAL_WORKFLOW_ID
    )
    assert isinstance(launch_contract, dict)
    assert launch_source.startswith("text_relation:")
    assert launch_contract.get("required_inputs") == ["prompt"]

    required_effects_contract = definition.metadata.get("required_effects_contract")
    assert isinstance(required_effects_contract, dict)
    required_effects = required_effects_contract.get("required_effects") or []
    assert isinstance(required_effects, list)
    assert required_effects[0]["effect_id"] == "grounded_entity_information_evidence"
    assert required_effects[0]["effect_type"] == "grounded_evidence"
    assert required_effects[0]["required_tools"] == [
        "fetch_concept",
        "get_predicate_incidence",
        "find_relations_with_argument",
        "list_uncertain_relationship_assertions",
    ]
    assert required_effects[0]["required_tools_match"] == "all"
    assert (
        required_effects[0]["not_executed_reason"]
        == "Required grounded entity-information evidence was not retrieved."
    )

    states = definition.states
    assert definition.initial_state in states
    retrieval_step = states[definition.initial_state]
    assert retrieval_step.actions[0].action_id == "llm.action"
    assert retrieval_step.actions[0].execution_mode == "llm"
    llm_policy = retrieval_step.actions[0].llm_policy
    assert isinstance(llm_policy, dict)
    assert llm_policy.get("tool_mode") == "allowed"
    assert llm_policy.get("allowed_tools") == [
        "get_predicate_incidence",
        "find_relations_with_argument",
        "search_concepts",
        "fetch_concept",
        "list_uncertain_relationship_assertions",
    ]
    assert llm_policy.get("required_tools") == [
        "fetch_concept",
        "get_predicate_incidence",
        "find_relations_with_argument",
        "list_uncertain_relationship_assertions",
    ]
    assert llm_policy.get("max_tool_invocations") == 8
    assert llm_policy.get("tool_argument_defaults") == {
        "get_predicate_incidence": {
            "argument_index": "subject",
            "relation_kind": "binary",
            "include_argument_type_counts": True,
            "limit": 12,
        },
        "find_relations_with_argument": {
            "argument_index": "subject",
            "relation_kind": "binary",
            "limit": 20,
            "__derive_predicate_filter_from_recent_incidence": {
                "enabled": True,
                "max_predicates": 2,
            },
        },
        "list_uncertain_relationship_assertions": {
            "include_legacy": True,
        },
    }
    assert any(
        transition.to_state in states and transition.reason == "next_step"
        for transition in retrieval_step.transitions
    )


def test_bootstrap_skips_republication_when_entity_information_retrieval_workflow_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_entity_information_retrieval_workflow()
    assert ((first_report.get("publication") or {}).get("counts") or {}).get(
        "workflows_published"
    ) == 1

    second_report = bootstrap_canonical_entity_information_retrieval_workflow()
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0


def test_entity_information_retrieval_prompt_support_seeds_content_from_repo_assets(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_entity_information_retrieval_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 1

    rows = get_texts_for_concept(
        ENTITY_INFORMATION_RETRIEVAL_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(prompt_text, str)
    prompt_text_lower = prompt_text.lower()
    assert "Do not use `list_papers`" in prompt_text
    assert "Use `get_predicate_incidence`" in prompt_text
    assert "list_uncertain_relationship_assertions" in prompt_text
    assert "established facts versus likely inferences" in prompt_text
    assert "you must call a represented-knowledge" in prompt_text_lower
    assert "retrieval tool in this turn before answering" in prompt_text_lower
    assert "do not conclude that no papers" in prompt_text_lower
    assert "first ontology-native" in prompt_text_lower
    assert "anchor entity always goes in `concept_id`" in prompt_text_lower
    assert "payload keys named `subject` or `object`" in prompt_text_lower
    assert "preserve the full `#v#" in prompt_text_lower
    assert "must include a `predicate_filter`" in prompt_text_lower
    assert "before any broader relation paging" in prompt_text_lower
    assert (
        "do not use an unfiltered `find_relations_with_argument` call"
        in prompt_text_lower
    )
    assert "do not stop at incidence alone" in prompt_text_lower
    assert "do not assume missing tool results" in prompt_text_lower
    assert "filter the grounded targets by type" in prompt_text_lower
