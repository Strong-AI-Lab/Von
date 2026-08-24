from __future__ import annotations

from typing import Any

import pytest

import src.backend.services.concept_summary_field_resolver as resolver_module
import src.backend.services.concept_summary_field_vontology_service as summary_seed_module
from src.backend.services import concept_service
from src.backend.services.concept_summary_field_vontology_service import (
    bootstrap_canonical_concept_summary_fields,
    ensure_concept_summary_fields_current_for_startup,
    reconcile_canonical_concept_summary_fields,
)


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


def test_summary_field_bootstrap_uses_batched_noop_fast_path(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = bootstrap_canonical_concept_summary_fields()
    assert first["success"] is True

    concept_reads = 0
    text_reads = 0
    original_concept_read = concept_service.get_concepts_by_concept_ids_exact
    original_text_read = summary_seed_module.get_texts_for_concepts

    def _concept_read(concept_ids):
        nonlocal concept_reads
        concept_reads += 1
        return original_concept_read(concept_ids)

    def _text_read(*args, **kwargs):
        nonlocal text_reads
        text_reads += 1
        return original_text_read(*args, **kwargs)

    monkeypatch.setattr(
        concept_service,
        "get_concepts_by_concept_ids_exact",
        _concept_read,
    )
    monkeypatch.setattr(summary_seed_module, "get_texts_for_concepts", _text_read)
    monkeypatch.setattr(
        summary_seed_module,
        "upsert_singleton_text_relation",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("current singleton text must not be rewritten")
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "create_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("current concepts must not be recreated")
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "update_concept",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("current relationships must not be rewritten")
        ),
    )

    second = bootstrap_canonical_concept_summary_fields()

    assert second["success"] is True
    assert second["changed"] is False
    assert second["read_strategy"] == "batched_canonical_state"
    assert second["read_phases"] == 2
    assert second["counts"]["field_configs_changed"] == 0
    assert second["counts"]["type_bindings_changed"] == 0
    assert concept_reads == 1
    assert text_reads == 1


def test_summary_field_startup_receipt_hit_performs_no_canonical_scan(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summary_seed_module.seed_freshness,
        "check_startup_seed_freshness",
        lambda **_kwargs: {
            "fresh": True,
            "reason": "dependency_receipt_current",
            "events_examined": 0,
            "metadata": {"counts": {"fields_seen": 17}},
        },
    )
    monkeypatch.setattr(
        concept_service,
        "get_concepts_by_concept_ids_exact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("receipt hit must not scan canonical concepts")
        ),
    )
    monkeypatch.setattr(
        summary_seed_module,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("receipt hit must not scan canonical text")
        ),
    )

    report = ensure_concept_summary_fields_current_for_startup()

    assert report["success"] is True
    assert report["skipped"] is True
    assert report["read_strategy"] == "dependency_receipt"
    assert report["canonical_read_batches"] == {
        "concepts": 0,
        "text_assertions": 0,
    }


def test_summary_field_startup_miss_requires_explicit_reconciliation_without_scan(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summary_seed_module.seed_freshness,
        "check_startup_seed_freshness",
        lambda **_kwargs: {"fresh": False, "reason": "receipt_missing"},
    )
    monkeypatch.setattr(
        concept_service,
        "get_concepts_by_concept_ids_exact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup receipt miss must not scan canonical concepts")
        ),
    )
    monkeypatch.setattr(
        summary_seed_module,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup receipt miss must not scan canonical text")
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "create_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup receipt miss must not create concepts")
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "update_concept",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup receipt miss must not update concepts")
        ),
    )
    monkeypatch.setattr(
        summary_seed_module,
        "upsert_singleton_text_relation",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup receipt miss must not write text relations")
        ),
    )

    report = ensure_concept_summary_fields_current_for_startup()

    assert report["success"] is False
    assert report["ready"] is False
    assert report["state"] == "unavailable"
    assert report["reason"] == "startup_seed_reconciliation_required"
    assert report["reconciliation_required"] is True
    assert report["canonical_read_batches"] == {
        "concepts": 0,
        "text_assertions": 0,
    }
    assert report["freshness_receipt"]["reason"] == "receipt_missing"


def test_summary_field_explicit_reconciliation_verifies_after_write(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[dict[str, Any]] = []
    recorded: list[dict[str, Any]] = []

    def _observe() -> dict[str, Any]:
        observation = {
            "success": True,
            "_start_at_operation_time": f"before-pass-{len(observations) + 1}",
        }
        observations.append(observation)
        return observation

    def _record(**kwargs):
        recorded.append(kwargs)
        return {"persisted": True, "reason": "dependency_receipt_persisted"}

    monkeypatch.setattr(
        summary_seed_module.seed_freshness,
        "begin_startup_seed_freshness_observation",
        _observe,
    )
    monkeypatch.setattr(
        summary_seed_module.seed_freshness,
        "record_startup_seed_freshness",
        _record,
    )

    report = reconcile_canonical_concept_summary_fields()

    assert report["success"] is True
    assert report["ready"] is True
    assert report["state"] == "ready"
    assert report["changed"] is True
    assert report["reconciliation_pass_count"] == 2
    assert report["passes"][0]["changed"] is True
    assert report["passes"][1]["changed"] is False
    assert report["freshness_receipt"]["persisted"] is True
    assert recorded[0]["observation"]["_start_at_operation_time"] == (
        "before-pass-2"
    )
