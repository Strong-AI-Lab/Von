from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.paper_recommendation_constants import (
    PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
)
from src.backend.services.paper_recommendation_workflow_vontology_service import (
    _ensure_paper_recommendation_prompt_support,
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


def test_paper_recommendation_prompt_support_seeds_content_from_repo_asset(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_paper_recommendation_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 2

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
