from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.paper_recommendation_constants import (
    PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
    PAPER_RECOMMENDATION_POLICY_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
    PAPER_RECOMMENDATION_WORKFLOW_ID,
)
from src.backend.services.paper_recommendation_policy_authority_service import (
    ensure_paper_recommendation_policy_authority,
    resolve_paper_recommendation_policy,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.workflows import workflow_concept_authority_service as authority_service


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
    yield
    authority_service.clear_workflow_type_resolution_cache()


def test_paper_recommendation_policy_authority_materialises_seed_profile(
    _reset_mock_db: Any,
) -> None:
    report = ensure_paper_recommendation_policy_authority()

    assert report["success"] is True
    assert report["policy_concept_id"] == PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID
    assert report["seeded_policy"] is True
    assert report["linked_workflow"] is True

    policy_rows = get_texts_for_concept(
        PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID,
        predicate=PAPER_RECOMMENDATION_POLICY_JSON_PREDICATE_ID,
        limit=5,
    )
    workflow_link_rows = get_texts_for_concept(
        PAPER_RECOMMENDATION_WORKFLOW_ID,
        predicate=PAPER_RECOMMENDATION_POLICY_LINK_PREDICATE_ID,
        limit=5,
    )

    assert len(policy_rows) == 1
    assert workflow_link_rows[0]["text"] == PAPER_RECOMMENDATION_POLICY_PROFILE_CONCEPT_ID

    policy = resolve_paper_recommendation_policy(auto_materialise=False)

    assert policy.policy_version == "paper_recommendation_policy.v3.vontology_authority"
    assert policy.paper_text_predicates("summary") == ("hasDescription", "hasContent")
    assert policy.delivery_required_type_ids == ("#V#von_user", "#V#researcher")
    assert policy.normalise_feedback_label("partial") == ("partly_useful", 0.0)
    assert "project_description" in {
        str(field["field"]) for field in policy.profile_field_specs()
    }
