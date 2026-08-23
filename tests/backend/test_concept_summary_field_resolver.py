from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.concept_summary_field_vontology_service import (
    bootstrap_canonical_concept_summary_fields,
)
import src.backend.services.concept_summary_field_resolver as resolver_module


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    resolver_module.get_concept_summary_field_resolver().invalidate_cache()
    yield
    resolver_module.get_concept_summary_field_resolver().invalidate_cache()


def test_summary_field_resolver_uses_vontology_override_when_present(monkeypatch) -> None:
    rows_by_key: dict[tuple[str, str | None], list[dict[str, str]]] = {
        (
            "#V#summary_field_author",
            "#V#has_summary_relationship_predicates_json",
        ): [
            {
                "text": '["#V#has_author", "#V#has_first_author", "#V#authored_by", "#V#lead_author"]'
            }
        ],
        (
            "#V#summary_field_email",
            "#V#has_summary_text_predicates_json",
        ): [
            {
                "text": '["#V#has_email", "has_email", "#V#primary_email"]'
            }
        ],
    }

    def _get_rows(
        subject_concept_id: str,
        predicate: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, str]]:
        del limit
        return rows_by_key.get((subject_concept_id, predicate), [])

    monkeypatch.setattr(
        resolver_module,
        "get_texts_for_concept",
        _get_rows,
    )

    resolver = resolver_module.ConceptSummaryFieldResolver(cache_ttl_seconds=60.0)

    assert resolver.get_relationship_predicates_for_field("author") == (
        "#V#has_author",
        "#V#has_first_author",
        "#V#authored_by",
        "#V#lead_author",
    )
    assert resolver.get_text_predicates_for_field("email") == (
        "#V#has_email",
        "has_email",
        "#V#primary_email",
    )


def test_summary_field_resolver_returns_empty_when_vontology_has_no_rows(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        resolver_module,
        "get_texts_for_concept",
        lambda subject_concept_id, predicate=None, limit=5: [],
    )

    resolver = resolver_module.ConceptSummaryFieldResolver(cache_ttl_seconds=60.0)

    assert resolver.get_text_predicates_for_field("description") == ()
    assert resolver.get_relationship_predicates_for_field("task_source") == ()


def test_summary_field_resolver_resolves_type_field_bindings(monkeypatch) -> None:
    concept_docs: dict[str, dict[str, Any]] = {
        "#V#person": {
            "concept_id": "#V#person",
            "relationships": {
                "#V#has_summary_fields": [
                    "#V#summary_field_description",
                    "#V#summary_field_email",
                    "#V#summary_field_affiliation",
                ]
            },
        }
    }

    monkeypatch.setattr(
        resolver_module.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: list(concept_docs.values()),
    )
    monkeypatch.setattr(
        resolver_module,
        "get_concept_by_concept_id",
        lambda concept_id: concept_docs[concept_id],
    )
    monkeypatch.setattr(
        resolver_module,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {},
    )

    resolver = resolver_module.ConceptSummaryFieldResolver(cache_ttl_seconds=60.0)

    assert resolver.get_summary_fields_for_type("#V#person") == (
        "description",
        "email",
        "affiliation",
    )
    assert resolver.get_summary_fields_for_types(["#V#researcher", "#V#person"]) == (
        "description",
        "email",
        "affiliation",
    )


def test_summary_field_bootstrap_materialises_vontology_metadata(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_concept_summary_fields()
    assert report["success"] is True
    assert report["counts"]["fields_seen"] == 17
    assert report["counts"]["errors"] == 0

    person_doc = concept_service.get_concept_by_concept_id("#V#person")
    assert person_doc is not None
    assert "#V#summary_field_email" in (
        (person_doc.get("relationships") or {}).get("#V#has_summary_fields") or []
    )

    resolver = resolver_module.ConceptSummaryFieldResolver(cache_ttl_seconds=60.0)
    assert resolver.get_relationship_predicates_for_field("author") == (
        "#V#has_author",
        "#V#has_first_author",
        "#V#authored_by",
    )
    assert resolver.get_summary_fields_for_type("#V#person") == (
        "description",
        "email",
        "affiliation",
        "authored_work",
    )
