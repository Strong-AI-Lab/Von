from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_external_identity_service as service
from src.backend.services.concept_external_identity_service import (
    ExternalIdentifier,
    ExternalIdentityInputError,
)


def test_normalises_explicit_arxiv_url_and_version_to_base_id() -> None:
    identifiers = service.normalise_create_external_identifiers(
        external_identifiers=[
            {
                "scheme": "arxiv",
                "value": "https://arxiv.org/pdf/2506.03346v3.pdf",
                "role": "identity",
            }
        ],
        concept_name="A title without identifier text",
        kind="instance",
    )

    assert identifiers == (
        ExternalIdentifier(
            scheme="arxiv",
            value="2506.03346",
            source="explicit",
        ),
    )


def test_leading_arxiv_label_is_accepted_but_incidental_number_is_not() -> None:
    leading = service.normalise_create_external_identifiers(
        external_identifiers=None,
        concept_name="arXiv:2506.03346v2 — A test of five storage effects",
        kind="instance",
    )
    incidental = service.normalise_create_external_identifiers(
        external_identifiers=None,
        concept_name="Dataset notes mentioning 2506.03346",
        kind="instance",
    )
    labelled_incidental = service.normalise_create_external_identifiers(
        external_identifiers=None,
        concept_name="Commentary on arXiv:2506.03346",
        kind="instance",
    )

    assert leading == (
        ExternalIdentifier(
            scheme="arxiv",
            value="2506.03346",
            source="leading_arxiv_label",
        ),
    )
    assert incidental == ()
    assert labelled_incidental == ()


def test_explicit_identity_and_leading_label_must_agree() -> None:
    with pytest.raises(ExternalIdentityInputError) as exc_info:
        service.normalise_create_external_identifiers(
            external_identifiers=[{"scheme": "arxiv", "value": "2506.03346"}],
            concept_name="arXiv:2408.02603 — Different paper",
            kind="instance",
        )

    assert exc_info.value.error_code == "external_identifier_name_mismatch"


def test_resolver_filters_inaccessible_and_non_exact_legacy_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = ExternalIdentifier(
        scheme="arxiv",
        value="2506.03346",
        source="explicit",
    )
    visible_id = "#V#legacy_visible"
    hidden_id = "#V#legacy_hidden"
    incidental_id = "#V#incidental_number"
    labelled_incidental_id = "#V#labelled_incidental"

    monkeypatch.setattr(
        service,
        "_arxiv_identity_relation_candidates",
        lambda *_args, **_kwargs: {
            visible_id,
            hidden_id,
            incidental_id,
            labelled_incidental_id,
        },
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": visible_id},
            {"concept_id": hidden_id},
            {"concept_id": incidental_id},
            {"concept_id": labelled_incidental_id},
        ],
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda candidate_ids: set(candidate_ids) - {hidden_id},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            visible_id: [
                {
                    "text": (
                        "arXiv 2506.03346 — Negligible effects of "
                        "environmental fluctuations"
                    )
                }
            ],
            incidental_id: [{"text": "Dataset notes mentioning 2506.03346"}],
            labelled_incidental_id: [{"text": "Commentary on arXiv:2506.03346"}],
        },
    )

    resolution = service.resolve_external_identity_candidates(identifier)

    assert resolution.status == "resolved"
    assert resolution.candidate_concept_ids == (visible_id,)


def test_resolver_returns_all_visible_exact_candidates_as_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = ExternalIdentifier(
        scheme="arxiv",
        value="2506.03346",
        source="leading_arxiv_label",
    )
    candidate_ids = {"#V#paper_b", "#V#paper_a"}

    monkeypatch.setattr(
        service,
        "_arxiv_identity_relation_candidates",
        lambda *_args, **_kwargs: set(candidate_ids),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": concept_id} for concept_id in candidate_ids
        ],
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda values: set(values),
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            concept_id: [{"text": "arXiv:2506.03346 — Paper"}]
            for concept_id in candidate_ids
        },
    )

    resolution = service.resolve_external_identity_candidates(identifier)

    assert resolution.status == "ambiguous"
    assert resolution.candidate_concept_ids == (
        "#V#paper_a",
        "#V#paper_b",
    )


def test_resolver_uses_one_bounded_identity_candidate_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _search(arxiv_id: str) -> set[str]:
        calls.append(arxiv_id)
        return set()

    monkeypatch.setattr(service, "_arxiv_identity_relation_candidates", _search)
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )

    resolution = service.resolve_external_identity_candidates(
        ExternalIdentifier(
            scheme="arxiv",
            value="2506.03346",
            source="explicit",
        )
    )

    assert resolution.status == "not_found"
    assert calls == ["2506.03346"]


def test_identity_lookup_uses_bounded_fingerprint_and_relation_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_queries: list[dict[str, Any]] = []
    relation_queries: list[dict[str, Any]] = []

    def _find_text(query: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        text_queries.append({"query": query, **kwargs})
        return [{"_id": "text-1"}]

    def _find_relation(
        query: dict[str, Any],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        relation_queries.append({"query": query, **kwargs})
        return [{"subject_concept_id": "#V#legacy_paper"}]

    monkeypatch.setattr(service.TextValuesRepository, "find", _find_text)
    monkeypatch.setattr(service.TextRelationsRepository, "find", _find_relation)

    candidates = service._arxiv_identity_relation_candidates("2506.03346")

    assert candidates == {"#V#legacy_paper"}
    fingerprint_ranges = text_queries[0]["query"]["$or"]
    range_bounds = [
        (
            item["fingerprint"]["$gte"],
            item["fingerprint"]["$lt"],
        )
        for item in fingerprint_ranges
    ]
    assert {lower for lower, _upper in range_bounds} == {
        "2506.03346",
        "http://arxiv.org/abs/2506.03346",
        "http://arxiv.org/pdf/2506.03346",
        "http://www.arxiv.org/abs/2506.03346",
        "http://www.arxiv.org/pdf/2506.03346",
        "https://arxiv.org/abs/2506.03346",
        "https://arxiv.org/pdf/2506.03346",
        "https://www.arxiv.org/abs/2506.03346",
        "https://www.arxiv.org/pdf/2506.03346",
        "arxiv2506.03346",
        "arxiv:2506.03346",
        "arxiv: 2506.03346",
        "arxiv 2506.03346",
        "arxiv :2506.03346",
        "arxiv : 2506.03346",
    }
    assert all(
        item["fingerprint"]["$type"] == "string"
        and item["fingerprint"]["$lt"].endswith("\uffff")
        for item in fingerprint_ranges
    )
    accepted_exact_forms = (
        "2506.03346",
        "2506.03346v12",
        "arXiv2506.03346v2",
        "arXiv : 2506.03346v3",
        "http://arxiv.org/abs/2506.03346v4",
        "https://arxiv.org/pdf/2506.03346.pdf",
        "http://www.arxiv.org/pdf/2506.03346v5.pdf?download=1",
        "https://www.arxiv.org/abs/2506.03346v6/#section",
    )

    def _is_indexed_candidate(value: str) -> bool:
        fingerprint = f"{value.casefold()}||en-nz"
        return any(lower <= fingerprint < upper for lower, upper in range_bounds)

    assert all(
        service.normalise_arxiv_id(value) == "2506.03346"
        for value in accepted_exact_forms
    )
    assert all(_is_indexed_candidate(value) for value in accepted_exact_forms)
    assert all(
        service._verified_arxiv_ids_from_name(value) == {"2506.03346"}
        for value in accepted_exact_forms
    )
    assert text_queries[0]["limit"] == 200
    assert relation_queries[0]["query"]["predicate"] == "hasName"
    assert relation_queries[0]["limit"] == 2_000


def test_identity_post_verification_rejects_indexed_false_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    false_prefix_names = {
        "#V#extra_digit": "2506.033460",
        "#V#version_suffix": "2506.03346v2extra",
        "#V#url_extra_digit": "https://arxiv.org/abs/2506.033460",
        "#V#url_pdf_suffix": ("https://www.arxiv.org/pdf/2506.03346v2.pdfx?download=1"),
        "#V#label_extra_digit": "arXiv:2506.033460 — Different paper",
    }
    candidate_ids = set(false_prefix_names)

    monkeypatch.setattr(
        service,
        "_arxiv_identity_relation_candidates",
        lambda *_args, **_kwargs: set(candidate_ids),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": concept_id} for concept_id in candidate_ids
        ],
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda values: set(values),
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            concept_id: [{"text": text}]
            for concept_id, text in false_prefix_names.items()
        },
    )

    resolution = service.resolve_external_identity_candidates(
        ExternalIdentifier(
            scheme="arxiv",
            value="2506.03346",
            source="explicit",
        )
    )

    assert all(
        service._verified_arxiv_ids_from_name(value) == set()
        for value in false_prefix_names.values()
    )
    assert resolution.status == "not_found"
    assert resolution.candidate_concept_ids == ()


def test_scholarly_parent_scope_accepts_known_and_bounded_descendant_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import context_bundle_service

    monkeypatch.setattr(
        context_bundle_service,
        "_collect_type_ancestors",
        lambda concept_id, **_kwargs: (
            [{"concept_id": "#V#scholarly_article", "depth": 1}]
            if concept_id == "#V#specialised_paper"
            else []
        ),
    )

    assert service.is_supported_arxiv_paper_parent("#V#scientific_paper") is True
    assert service.is_supported_arxiv_paper_parent("#V#specialised_paper") is True
    assert service.is_supported_arxiv_paper_parent("#V#research_note") is False


def test_actor_scope_stable_ids_share_by_org_and_isolate_other_scopes() -> None:
    identifier = ExternalIdentifier(
        scheme="arxiv",
        value="2506.03346",
        source="explicit",
    )

    global_id = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#scientific_paper",
        scope_mode="global_general",
        actor_user_id="#V#user_a",
        actor_org_id="#V#org_a",
    )
    org_a_user_a = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#scientific_paper",
        actor_user_id="#V#user_a",
        actor_org_id="#V#org_a",
    )
    org_a_user_b = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#scientific_paper",
        actor_user_id="#V#user_b",
        actor_org_id="#V#org_a",
    )
    org_a_general = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#scientific_paper",
        scope_mode="organisation_general",
        actor_user_id="#V#user_b",
        actor_org_id="#V#org_a",
    )
    org_b = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#scientific_paper",
        actor_user_id="#V#user_a",
        actor_org_id="#V#org_b",
    )
    user_only = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#scientific_paper",
        actor_user_id="#V#user_a",
    )

    assert global_id is not None
    assert "_scope_" not in global_id
    assert org_a_user_a == org_a_user_b == org_a_general
    assert org_a_user_a != global_id
    assert org_b != org_a_user_a
    assert user_only not in {global_id, org_a_user_a, org_b}


def test_persist_external_identity_names_retains_per_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _upsert(**kwargs: Any) -> dict[str, Any]:
        text = str(kwargs["text"])
        calls.append(text)
        if text.startswith("https://"):
            raise RuntimeError("identity URL write failed")
        return {
            "relation_id": "rel-arxiv-id",
            "relation_created": True,
            "context_updated": False,
        }

    monkeypatch.setattr(service, "upsert_text_for_concept", _upsert)

    result = service.persist_external_identity_names(
        concept_id="#V#paper",
        identifiers=[
            ExternalIdentifier(
                scheme="arxiv",
                value="2506.03346",
                source="explicit",
            )
        ],
    )

    assert calls == [
        "2506.03346",
        "https://arxiv.org/abs/2506.03346",
    ]
    assert result["success"] is False
    assert result["writes"][0]["relation_id"] == "rel-arxiv-id"
    assert result["failures"] == [
        {
            "stage": "external_identity_persistence",
            "scheme": "arxiv",
            "value": "https://arxiv.org/abs/2506.03346",
            "error": "identity URL write failed",
            "exception_type": "RuntimeError",
        }
    ]
