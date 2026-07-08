from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.paper_recommendation_constants import (
    GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
    PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_REQUESTED_EVENT_TYPE,
    PAPER_RECOMMENDATION_WORKFLOW_ID,
)
from src.backend.services.paper_recommendation_workflow_vontology_service import (
    _LEGACY_PROFILE_JSON_PREDICATE_ID,
    _ensure_paper_recommendation_event_bindings,
    _ensure_paper_recommendation_prompt_support,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_event_integration_service import (
    EVENT_TYPE_RELATIONSHIP_ADDED,
    EVENT_TYPE_RELATIONSHIP_REMOVED,
    EVENT_TYPE_TEXT_RELATION_UPDATED,
    EVENT_TYPE_TEXT_RELATION_UPSERTED,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.durable import WorkflowInstanceManager


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in (
            "concepts",
            "text_relations",
            "text_values",
            "workflow_event_bindings",
        ):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    yield
    authority_service.clear_workflow_type_resolution_cache()


def test_paper_recommendation_prompt_support_seeds_content_from_repo_asset(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_paper_recommendation_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 3

    prompt_rows = get_texts_for_concept(
        PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in prompt_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(prompt_text, str)
    assert "Prefer semantically relevant matches across languages" in prompt_text
    assert "Use the embedding_score only as one signal" in prompt_text

    delivery_rows = get_texts_for_concept(
        PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    delivery_text = next(
        ((row or {}).get("text") for row in delivery_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(delivery_text, str)
    assert "Von has {recommendation_count} new {recommendation_noun} for you." in delivery_text
    assert "{recommendation_items}" in delivery_text

    rationale_rows = get_texts_for_concept(
        PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    rationale_text = next(
        ((row or {}).get("text") for row in rationale_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(rationale_text, str)
    assert "Do not mention embeddings, ranking pipelines, or internal selection machinery." in rationale_text
    assert "rationale_summary: one paragraph suitable for direct user display" in rationale_text


def test_paper_recommendation_event_bindings_are_conditioned_by_policy(
    _reset_mock_db: Any,
) -> None:
    manager = WorkflowInstanceManager()
    for event_type in (
        PAPER_RECOMMENDATION_REQUESTED_EVENT_TYPE,
        EVENT_TYPE_TEXT_RELATION_UPSERTED,
        EVENT_TYPE_TEXT_RELATION_UPDATED,
        EVENT_TYPE_RELATIONSHIP_ADDED,
        EVENT_TYPE_RELATIONSHIP_REMOVED,
    ):
        manager.upsert_event_binding(
            event_type=event_type,
            workflow_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
            input_mapping={},
            condition=None,
            enabled=True,
            actor="legacy_bootstrap",
            replace_existing=True,
        )

    report = _ensure_paper_recommendation_event_bindings()

    assert report["success"] is True
    assert report["binding_count"] == 5
    assert report["created_count"] == 0
    assert report["updated_count"] == 4

    bindings = {
        item.event_type: item
        for item in manager.list_event_bindings(
            enabled_only=True,
            limit=20,
        )
        if item.workflow_id == PAPER_RECOMMENDATION_WORKFLOW_ID
    }

    assert bindings[PAPER_RECOMMENDATION_REQUESTED_EVENT_TYPE].condition is None

    text_condition = bindings[EVENT_TYPE_TEXT_RELATION_UPSERTED].condition
    assert text_condition == {
        "kind": "context_value_in",
        "key": "event.predicate",
        "values": sorted(
            {
                GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
                _LEGACY_PROFILE_JSON_PREDICATE_ID,
            }
        ),
    }
    assert bindings[EVENT_TYPE_TEXT_RELATION_UPDATED].condition == text_condition

    relationship_condition = bindings[EVENT_TYPE_RELATIONSHIP_ADDED].condition
    assert relationship_condition == {
        "kind": "context_value_in",
        "key": "event.predicate",
        "values": [
            "#V#has_project",
            "#V#has_research_interest",
            "#V#member_of_organisation",
            "#V#working_on_project",
        ],
    }
    assert bindings[EVENT_TYPE_RELATIONSHIP_REMOVED].condition == relationship_condition
