from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.concept_search_instance_retrieval_workflow_vontology_service import (
    CONCEPT_SEARCH_INSTANCE_RETRIEVAL_PROMPT_CONCEPT_ID,
    CONCEPT_SEARCH_INSTANCE_RETRIEVAL_WORKFLOW_ID,
    _ensure_concept_search_instance_retrieval_prompt_support,
    bootstrap_canonical_concept_search_instance_retrieval_workflow,
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


def test_bootstrap_materialises_concept_search_instance_retrieval_workflow(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_concept_search_instance_retrieval_workflow()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert counts.get("workflows_published") == 1
    assert counts.get("errors") == 0

    definition = load_workflow_definition_from_vontology(
        CONCEPT_SEARCH_INSTANCE_RETRIEVAL_WORKFLOW_ID
    )
    assert definition is not None

    routing_profile, routing_source = resolve_workflow_routing_profile(
        CONCEPT_SEARCH_INSTANCE_RETRIEVAL_WORKFLOW_ID
    )
    assert isinstance(routing_profile, dict)
    assert routing_source.startswith("text_relation:")
    assert routing_profile.get("role") == "execution"

    discovery_exemplars, discovery_source = resolve_workflow_discovery_exemplars(
        CONCEPT_SEARCH_INSTANCE_RETRIEVAL_WORKFLOW_ID
    )
    assert isinstance(discovery_exemplars, dict)
    assert discovery_source.startswith("text_relation:")
    assert "concept profile retrieval" in (discovery_exemplars.get("keywords") or [])
    assert "do you have a represented vontology concept for" in (
        discovery_exemplars.get("excluded_query_cues") or []
    )
    assert any(
        "zero-result exact search" in note
        for note in (discovery_exemplars.get("routing_notes") or [])
    )
    launch_contract, launch_source = resolve_workflow_launch_input_contract(
        CONCEPT_SEARCH_INSTANCE_RETRIEVAL_WORKFLOW_ID
    )
    assert isinstance(launch_contract, dict)
    assert launch_source.startswith("text_relation:")
    assert launch_contract.get("required_inputs") == ["prompt"]
    launch_sources = {
        mapping.get("source_expression")
        for mapping in launch_contract.get("input_mappings") or []
    }
    assert "inputs.augmented_context" in launch_sources
    assert "inputs.conversation_situation" in launch_sources

    required_effects_contract = definition.metadata.get("required_effects_contract")
    assert isinstance(required_effects_contract, dict)
    assert (
        required_effects_contract.get("contract_id")
        == "grounded_concept_profile_retrieval_evidence"
    )
    required_effects = required_effects_contract.get("required_effects") or []
    assert isinstance(required_effects, list)
    assert required_effects[0]["effect_id"] == "grounded_concept_profile_evidence"
    assert required_effects[0]["effect_type"] == "grounded_evidence"
    assert required_effects[0]["required_tools"] == [
        "fetch_concept",
        "get_text_relations_summary",
        "find_relations_with_argument",
    ]
    assert required_effects[0]["required_tools_match"] == "all"
    assert (
        required_effects[0]["wrong_target_failure_code"]
        == "concept_profile_evidence_wrong_target"
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
        "fetch_concept",
        "get_text_relations_summary",
        "find_relations_with_argument",
        "search_concepts",
    ]
    assert llm_policy.get("required_tools") == [
        "fetch_concept",
        "get_text_relations_summary",
        "find_relations_with_argument",
    ]
    assert llm_policy.get("max_tool_invocations") == 6
    tool_argument_defaults = llm_policy.get("tool_argument_defaults") or {}
    assert "fetch_concept" not in tool_argument_defaults
    assert tool_argument_defaults.get("get_text_relations_summary") == {
        "max_relation_ids_per_group": 20
    }
    assert tool_argument_defaults.get("find_relations_with_argument") == {
        "argument_index": "any",
        "relation_kind": "any",
        "include_text_snippets": True,
        "include_concept_preview": False,
        "limit": 30,
    }
    assert llm_policy.get("context_messages_context_key") == "augmented_context"
    assert "conversation_situation" in {
        field.get("context_key")
        for field in llm_policy.get("context_fields") or []
    }
def test_concept_search_instance_retrieval_prompt_support_seeds_content(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_concept_search_instance_retrieval_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 1

    rows = get_texts_for_concept(
        CONCEPT_SEARCH_INSTANCE_RETRIEVAL_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(prompt_text, str)
    prompt_text_lower = prompt_text.lower()
    assert "every required concept-profile retrieval" in prompt_text_lower
    assert "same id in the `concept_id` argument" in prompt_text_lower
    assert "do not substitute the authenticated user concept" in prompt_text_lower
    assert "get_text_relations_summary" in prompt_text
    assert "find_relations_with_argument" in prompt_text


def test_bootstrap_skips_republication_when_concept_search_workflow_is_current(
    _reset_mock_db: Any,
) -> None:
    first_report = bootstrap_canonical_concept_search_instance_retrieval_workflow()
    assert ((first_report.get("publication") or {}).get("counts") or {}).get(
        "workflows_published"
    ) == 1

    second_report = bootstrap_canonical_concept_search_instance_retrieval_workflow()
    second_publication = second_report.get("publication") or {}
    second_counts = second_publication.get("counts") or {}

    assert second_publication.get("skipped") is True
    assert second_publication.get("skip_reason") == "existing_materialisation_valid"
    assert second_counts.get("workflows_published") == 0
    assert second_counts.get("errors") == 0
