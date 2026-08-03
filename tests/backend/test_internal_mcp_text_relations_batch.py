from __future__ import annotations

from typing import Any

from src.backend.integrations.internal_mcp import catalogue as catalogue_module
from src.backend.security.access_control import (
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
    override_current_actor,
)

_USER_ID = "#V#batch_text_user"
_ORG_ID = "#V#batch_text_org"


def _complete_query_metadata(query_metadata: dict[str, Any]) -> None:
    query_metadata.update(
        {
            "raw_relation_count": 1,
            "relation_query_limit": 21,
            "relation_query_truncated": False,
            "context_view": "actor_effective",
            "scoped_assertion_count": 0,
            "scoped_query_limit": 14,
            "scoped_query_truncated": False,
            "scoped_visibility_filtered": False,
            "scoped_counts_are_lower_bounds": False,
            "scoped_per_concept": {
                "#V#candidate_a": {
                    "returned": 0,
                    "limit": 7,
                    "has_more": False,
                    "visibility_filtered": False,
                    "counts_are_lower_bounds": False,
                },
                "#V#candidate_b": {
                    "returned": 0,
                    "limit": 7,
                    "has_more": False,
                    "visibility_filtered": False,
                    "counts_are_lower_bounds": False,
                },
            },
        }
    )


def test_batch_dedupes_ids_binds_actor_and_compacts_grouped_rows(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_get_texts_for_concepts(concept_ids, **kwargs):
        captured.update(
            {
                "concept_ids": list(concept_ids),
                "kwargs": dict(kwargs),
                "actor": (
                    get_effective_user_concept_id(),
                    get_effective_organisation_concept_id(),
                ),
            }
        )
        _complete_query_metadata(kwargs["query_metadata"])
        return {
            "#V#candidate_a": [
                {
                    "subject_concept_id": "#V#candidate_a",
                    "predicate": "#V#has_status",
                    "text": "Active",
                    "lang": "en-NZ",
                    "row_kind": "scoped_assertion",
                    "relation_id": None,
                    "assertion_id": "ska_status_a",
                    "canonical_publication": False,
                    "storage_surface": "scoped_assertions",
                    "context": {"large": "omitted from compact rows"},
                    "provenance": {"source": "workbook"},
                    "relation_updated_at": "2026-08-02T12:00:00Z",
                }
            ],
            "#V#candidate_b": [],
        }

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concepts",
        fake_get_texts_for_concepts,
    )

    with override_current_actor(_USER_ID, _ORG_ID):
        result = catalogue_module._get_text_relations_batch(
            concept_ids=[
                " #V#candidate_a ",
                "#V#candidate_a",
                "#V#candidate_b",
            ],
            predicates=["#V#has_status", "#V#has_stage"],
            language="en-NZ",
            limit_per_concept=7,
            recent_first=True,
        )

    assert captured["concept_ids"] == ["#V#candidate_a", "#V#candidate_b"]
    assert captured["actor"] == (_USER_ID, _ORG_ID)
    assert captured["kwargs"]["context_view"] == "actor_effective"
    assert captured["kwargs"]["predicate"] is None
    assert captured["kwargs"]["predicates"] == [
        "#V#has_status",
        "#V#has_stage",
    ]
    assert captured["kwargs"]["lang"] == "en-NZ"
    assert captured["kwargs"]["limit_per_concept"] == 7
    assert captured["kwargs"]["recent_first"] is True

    assert result["schema_version"] == "text_relations_batch.v1"
    assert result["context_view"] == "actor_effective"
    assert result["requested_concept_count"] == 2
    assert result["returned_concept_count"] == 2
    assert result["omitted_concept_count"] == 0
    assert result["relations_found"] == 1
    assert result["coverage_complete"] is True
    assert "counts_are_lower_bounds" not in result
    assert [item["concept_id"] for item in result["items"]] == [
        "#V#candidate_a",
        "#V#candidate_b",
    ]
    assert result["items"][0]["relations"] == [
        {
            "predicate": "#V#has_status",
            "text": "Active",
            "lang": "en-NZ",
            "row_kind": "scoped_assertion",
            "assertion_id": "ska_status_a",
            "canonical_publication": False,
            "storage_surface": "scoped_assertions",
        }
    ]
    assert result["items"][1]["relations"] == []
    assert result["query_diagnostics"]["context_view"] == "actor_effective"


def test_batch_rejects_more_than_one_hundred_unique_ids_before_read(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concepts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an over-limit batch must not reach the read service")
        ),
    )

    result = catalogue_module._get_text_relations_batch(
        concept_ids=[f"#V#candidate_{index}" for index in range(101)]
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_parameter"
    assert result["error_details"] == {
        "parameter": "concept_ids",
        "maximum": 100,
    }


def test_batch_preserves_incomplete_service_coverage(monkeypatch) -> None:
    def fake_get_texts_for_concepts(concept_ids, **kwargs):
        kwargs["query_metadata"].update(
            {
                "relation_query_truncated": True,
                "scoped_query_truncated": False,
                "scoped_counts_are_lower_bounds": False,
                "scoped_per_concept": {
                    "#V#candidate_a": {
                        "has_more": True,
                        "counts_are_lower_bounds": True,
                    }
                },
            }
        )
        return {
            "#V#candidate_a": [
                {
                    "predicate": "#V#has_note",
                    "text": "One bounded row",
                    "lang": "en-NZ",
                    "row_kind": "base_text_relation",
                    "relation_id": "relation_a",
                }
            ]
        }

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concepts",
        fake_get_texts_for_concepts,
    )

    with override_current_actor(_USER_ID, _ORG_ID):
        result = catalogue_module._get_text_relations_batch(
            concept_ids=["#V#candidate_a"],
            limit_per_concept=1,
        )

    assert result["coverage_complete"] is False
    assert result["counts_are_lower_bounds"] is True
    assert result["items"][0]["coverage_complete"] is False
    assert result["items"][0]["counts_are_lower_bounds"] is True
    assert result["query_diagnostics"]["relation_query_truncated"] is True


def test_batch_omitted_concepts_cannot_be_reported_as_complete(monkeypatch) -> None:
    def fake_get_texts_for_concepts(concept_ids, **kwargs):
        _complete_query_metadata(kwargs["query_metadata"])
        return {"#V#candidate_a": []}

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concepts",
        fake_get_texts_for_concepts,
    )

    with override_current_actor(_USER_ID, _ORG_ID):
        result = catalogue_module._get_text_relations_batch(
            concept_ids=["#V#candidate_a", "#V#candidate_b"],
            limit_per_concept=7,
        )

    assert result["returned_concept_count"] == 1
    assert result["omitted_concept_count"] == 1
    assert result["coverage_complete"] is False
    assert result["counts_are_lower_bounds"] is True
