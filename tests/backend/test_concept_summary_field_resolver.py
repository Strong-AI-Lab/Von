from __future__ import annotations

import src.backend.services.concept_summary_field_resolver as resolver_module


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


def test_summary_field_resolver_falls_back_to_defaults_when_vontology_has_no_rows(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        resolver_module,
        "get_texts_for_concept",
        lambda subject_concept_id, predicate=None, limit=5: [],
    )

    resolver = resolver_module.ConceptSummaryFieldResolver(cache_ttl_seconds=60.0)

    assert resolver.get_text_predicates_for_field("description") == (
        "hasDescription",
        "#V#hasDescription",
    )
    assert resolver.get_relationship_predicates_for_field("task_source") == (
        "#V#hastasksource",
        "#V#hasTaskSource",
    )
