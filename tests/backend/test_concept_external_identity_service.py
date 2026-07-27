from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_external_identity_service as service
from src.backend.services.concept_external_identity_service import (
    ExternalIdentifier,
    ExternalIdentityInputError,
)


def _identifier(
    *,
    scheme: str = "catalogue",
    value: str = "Record/42:revision=3",
) -> ExternalIdentifier:
    return ExternalIdentifier(scheme=scheme, value=value, source="explicit")


def test_normalises_an_explicit_opaque_scheme_and_value_only() -> None:
    identifiers = service.normalise_create_external_identifiers(
        external_identifiers={
            "scheme": "CATALOG+V1",
            "canonical_value": "  Record/42:revision=3  ",
            "role": "identity",
        },
        concept_name="An unrelated display label",
        kind="instance",
    )

    assert identifiers == (
        ExternalIdentifier(
            scheme="catalog+v1",
            value="Record/42:revision=3",
            source="explicit",
        ),
    )


@pytest.mark.parametrize(
    ("external_identifier", "error_code"),
    [
        (
            {"scheme": "not a scheme", "value": "record-42"},
            "invalid_external_identifier_scheme",
        ),
        ({"scheme": "catalogue", "value": ""}, "invalid_external_identifier"),
        (
            {"scheme": "catalogue", "value": "record-\n42"},
            "invalid_external_identifier",
        ),
        (
            {"scheme": "catalogue", "value": "record-42", "role": "reference"},
            "invalid_external_identifier_role",
        ),
    ],
)
def test_rejects_invalid_identity_envelopes_without_scheme_specific_parsing(
    external_identifier: dict[str, str],
    error_code: str,
) -> None:
    with pytest.raises(ExternalIdentityInputError) as exc_info:
        service.normalise_create_external_identifiers(
            external_identifiers=external_identifier,
            concept_name="Display label",
            kind="instance",
        )

    assert exc_info.value.error_code == error_code


def test_revalidates_preconstructed_identifier_envelopes() -> None:
    with pytest.raises(ExternalIdentityInputError) as exc_info:
        service.normalise_create_external_identifiers(
            external_identifiers=ExternalIdentifier(
                scheme="not a scheme",
                value="record-42",
            )
        )

    assert exc_info.value.error_code == "invalid_external_identifier_scheme"


def test_does_not_infer_identity_from_a_name_or_reconcile_name_semantics() -> None:
    inferred = service.normalise_create_external_identifiers(
        external_identifiers=None,
        concept_name="catalogue:record-42 — A display label",
        kind="instance",
    )
    explicit = service.normalise_create_external_identifiers(
        external_identifiers={"scheme": "catalogue", "value": "record-42"},
        concept_name="A label that contains a different reference",
        kind="type",
    )

    assert inferred == ()
    assert explicit == (_identifier(value="record-42"),)


def test_marker_is_generic_exact_and_unambiguous() -> None:
    identifier = _identifier(scheme="catalog+v1")

    assert service.external_identity_marker(identifier) == (
        "urn:von:external-identity:sha256:"
        "79c18669ef3ee2d50c9f4d32109023b05e1c717471d448af2093895d92176f92"
    )
    assert service.external_identity_marker(
        _identifier(value="Record/42:revision=4")
    ) != service.external_identity_marker(identifier)
    assert service.external_identity_marker(
        _identifier(value="record/42:revision=3")
    ) != service.external_identity_marker(identifier)


def test_exact_marker_lookup_uses_bounded_indexed_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier()
    marker = service.external_identity_marker(identifier)
    text_queries: list[dict[str, Any]] = []
    relation_queries: list[dict[str, Any]] = []

    def _find_text(query: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        text_queries.append({"query": query, **kwargs})
        return [
            {"_id": "exact-text", "text": marker},
            {"_id": "near-text", "text": f"{marker}-other"},
        ]

    def _find_relation(
        query: dict[str, Any],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        relation_queries.append({"query": query, **kwargs})
        return [{"subject_concept_id": "#V#record_42"}]

    monkeypatch.setattr(service.TextValuesRepository, "find", _find_text)
    monkeypatch.setattr(service.TextRelationsRepository, "find", _find_relation)

    scan = service._identity_marker_relation_candidates(identifier)
    assert scan.candidate_concept_ids == ("#V#record_42",)
    assert scan.exhaustive is True
    assert scan.saturated_stages == ()
    fingerprint_range = text_queries[0]["query"]["fingerprint"]
    assert fingerprint_range == {
        "$type": "string",
        "$gte": f"{marker.casefold()}||",
        "$lt": f"{marker.casefold()}||\uffff",
    }
    assert text_queries[0]["limit"] == 201
    assert relation_queries[0]["query"]["object_text_id"]["$in"] == [
        "exact-text",
    ]
    assert relation_queries[0]["query"]["predicate"] == {
        "$in": ["hasName", "#V#hasName"]
    }
    assert relation_queries[0]["limit"] == 2_001


def test_exact_marker_candidates_resolve_and_multiple_markers_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier()
    marker_candidates: set[str] = {"#V#record_b", "#V#record_a"}

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(marker_candidates),
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )

    ambiguous = service.resolve_external_identity_candidates(identifier)
    marker_candidates.remove("#V#record_b")
    resolved = service.resolve_external_identity_candidates(identifier)

    assert ambiguous.status == "ambiguous"
    assert ambiguous.candidate_concept_ids == ("#V#record_a", "#V#record_b")
    assert ambiguous.resolution_source == "persisted_identity_marker"
    assert resolved.status == "resolved"
    assert resolved.candidate_concept_ids == ("#V#record_a",)
    assert resolved.resolution_source == "persisted_identity_marker"


def test_incomplete_exact_marker_scan_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier()

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: service.ExternalIdentityCandidateScan(
            candidate_concept_ids=("#V#visible_marker_holder",),
            exhaustive=False,
            saturated_stages=("text_relations",),
        ),
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )

    resolution = service.resolve_external_identity_candidates(identifier)

    assert resolution.status == "ambiguous"
    assert resolution.candidate_concept_ids == ("#V#visible_marker_holder",)
    assert resolution.resolution_source == (
        "persisted_identity_marker_lookup_incomplete"
    )


def test_legacy_text_reference_is_only_an_unverified_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier(value="record-42")

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        service,
        "_legacy_text_reference_candidates",
        lambda _identifier: {"#V#legacy_record"},
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )
    monkeypatch.setattr(
        service,
        "_confirmed_candidate_ids",
        lambda _identifier, _candidate_ids: set(),
    )

    resolution = service.resolve_external_identity_candidates(identifier)

    assert resolution.status == "unverified"
    assert resolution.candidate_concept_ids == ("#V#legacy_record",)
    assert resolution.resolution_source == "legacy_text_reference"


def test_legacy_scan_reports_upstream_text_query_saturation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier(value="record-42")
    text_queries: list[dict[str, Any]] = []
    relation_queries: list[dict[str, Any]] = []

    def _find_text(query: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        text_queries.append({"query": query, **kwargs})
        return [
            {
                "_id": f"text-{index}",
                "text": f"Reference to record-42 row {index}",
            }
            for index in range(201)
        ]

    def _find_relation(
        query: dict[str, Any],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        relation_queries.append({"query": query, **kwargs})
        return [{"subject_concept_id": "#V#legacy_record"}]

    monkeypatch.setattr(service.TextValuesRepository, "find", _find_text)
    monkeypatch.setattr(service.TextRelationsRepository, "find", _find_relation)

    scan = service._legacy_text_reference_candidates(identifier)

    assert scan.candidate_concept_ids == ("#V#legacy_record",)
    assert scan.exhaustive is False
    assert scan.saturated_stages == ("text_values",)
    assert text_queries[0]["limit"] == 201
    assert relation_queries[0]["limit"] == 2_001


def test_exact_reviewed_legacy_non_matches_allow_stable_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier(value="record-42")
    legacy_ids = {"#V#unrelated_note", "#V#unrelated_archive"}

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        service,
        "_legacy_text_reference_candidates",
        lambda _identifier: set(legacy_ids),
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )
    monkeypatch.setattr(
        service,
        "_confirmed_candidate_ids",
        lambda _identifier, _candidate_ids: set(),
    )

    partial_review = service.resolve_external_identity_candidates(
        identifier,
        rejected_candidate_concept_ids=("#V#unrelated_note",),
    )
    exact_review = service.resolve_external_identity_candidates(
        identifier,
        rejected_candidate_concept_ids=tuple(sorted(legacy_ids)),
    )

    assert partial_review.status == "unverified"
    assert partial_review.candidate_concept_ids == tuple(sorted(legacy_ids))
    assert exact_review.status == "not_found"
    assert exact_review.candidate_concept_ids == ()
    assert exact_review.resolution_source == "caller_rejected_legacy_candidates"


def test_truncated_legacy_candidate_set_cannot_be_rejected_as_exhaustive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier(value="record-42")
    visible_legacy_ids = {
        f"#V#legacy_reference_{index:03d}" for index in range(51)
    }
    exposed_ids = tuple(sorted(visible_legacy_ids)[:50])

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        service,
        "_legacy_text_reference_candidates",
        lambda _identifier: service.ExternalIdentityCandidateScan(
            candidate_concept_ids=tuple(sorted(visible_legacy_ids)),
            exhaustive=False,
            saturated_stages=("text_values",),
        ),
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: (
            set(visible_legacy_ids)
            if set(candidate_ids) == visible_legacy_ids
            else set(candidate_ids)
        ),
    )
    monkeypatch.setattr(
        service,
        "_confirmed_candidate_ids",
        lambda _identifier, _candidate_ids: set(),
    )

    resolution = service.resolve_external_identity_candidates(
        identifier,
        rejected_candidate_concept_ids=exposed_ids,
    )

    assert resolution.status == "unverified"
    assert resolution.candidate_concept_ids == exposed_ids
    assert resolution.resolution_source == "legacy_text_reference_overflow"


def test_visible_candidate_bound_is_applied_after_actor_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_ids = [f"#V#candidate_{index:03d}" for index in range(51)]
    visible_id = candidate_ids[-1]
    find_batches: list[list[str]] = []

    def _find(
        query: dict[str, Any],
        **_kwargs: Any,
    ) -> list[dict[str, str]]:
        batch = list(query["concept_id"]["$in"])
        find_batches.append(batch)
        return [{"concept_id": candidate_id} for candidate_id in batch]

    monkeypatch.setattr(service.ConceptsRepository, "find", _find)
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: {visible_id} if visible_id in concept_ids else set(),
    )

    assert service._existing_visible_concept_ids(candidate_ids) == {visible_id}
    assert len(find_batches) == 2
    assert visible_id in find_batches[-1]


def test_asserted_candidate_requires_visible_matching_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier(value="record-42")
    selected_id = "#V#legacy_record"

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            selected_id: [{"text": "Imported reference record-42."}]
        },
    )

    resolution = service.resolve_external_identity_candidates(
        identifier,
        asserted_candidate_concept_ids=(selected_id,),
    )

    assert resolution.status == "resolved"
    assert resolution.candidate_concept_ids == (selected_id,)
    assert resolution.resolution_source == "caller_confirmed_visible_evidence"


def test_short_opaque_value_cannot_confirm_from_incidental_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier(value="x")
    selected_id = "#V#legacy_record"

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )
    monkeypatch.setattr(
        service,
        "_legacy_text_reference_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            selected_id: [{"text": "x"}],
        },
    )

    resolution = service.resolve_external_identity_candidates(
        identifier,
        asserted_candidate_concept_ids=(selected_id,),
    )

    assert resolution.status == "not_found"
    assert resolution.candidate_concept_ids == ()


def test_stable_identity_collision_is_unverified_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier()
    occupied_id = "#V#occupied_stable_identity"

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        service,
        "_confirmed_candidate_ids",
        lambda _identifier, _candidate_ids: set(),
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )

    resolution = service.resolve_external_identity_candidates(
        identifier,
        canonical_concept_id_candidates=(occupied_id,),
    )

    assert resolution.status == "unverified"
    assert resolution.candidate_concept_ids == (occupied_id,)
    assert resolution.resolution_source == "stable_identity_collision"


def test_exact_visible_stable_candidate_can_be_confirmed_for_marker_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier()
    occupied_id = "#V#occupied_stable_identity"

    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        service,
        "_confirmed_candidate_ids",
        lambda _identifier, _candidate_ids: set(),
    )
    monkeypatch.setattr(
        service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )

    resolution = service.resolve_external_identity_candidates(
        identifier,
        canonical_concept_id_candidates=(occupied_id,),
        asserted_candidate_concept_ids=(occupied_id,),
    )

    assert resolution.status == "resolved"
    assert resolution.candidate_concept_ids == (occupied_id,)
    assert resolution.resolution_source == "caller_confirmed_stable_identity"


def test_stable_id_is_parent_and_kind_neutral_but_actor_scoped() -> None:
    identifier = _identifier()

    global_instance = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#archival_record",
        scope_mode="global_general",
    )
    global_type = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="type",
        parent_id="#V#catalogue_entry",
        scope_mode="global_general",
    )
    org_a_user_a = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#archival_record",
        actor_user_id="#V#user_a",
        actor_org_id="#V#org_a",
    )
    org_a_user_b = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#catalogue_entry",
        actor_user_id="#V#user_b",
        actor_org_id="#V#org_a",
    )
    org_b = service.canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id="#V#archival_record",
        actor_user_id="#V#user_a",
        actor_org_id="#V#org_b",
    )

    assert global_instance is not None
    assert global_instance == global_type
    assert global_instance.startswith("#V#external_identity_catalogue_")
    assert "record" not in global_instance.casefold()
    assert "revision" not in global_instance.casefold()
    assert "_scope_" not in global_instance
    assert org_a_user_a == org_a_user_b
    assert org_a_user_a != global_instance
    assert org_b not in {global_instance, org_a_user_a}


def test_persists_one_generic_marker_and_retains_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier()
    calls: list[dict[str, Any]] = []

    def _upsert(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        raise RuntimeError("marker write failed")

    monkeypatch.setattr(service, "upsert_text_for_concept", _upsert)
    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )

    result = service.persist_external_identity_markers(
        concept_id="#V#record_42",
        identifiers=[identifier],
    )

    marker = service.external_identity_marker(identifier)
    assert calls == [
        {
            "subject_concept_id": "#V#record_42",
            "predicate": "hasName",
            "text": marker,
            "lang": "und",
            "provenance": {
                "source": "create_concepts_external_identity",
                "external_identifier_scheme": "catalogue",
            },
            "context": {
                "name_type": "CODE",
                "source": "create_concepts_external_identity",
                "identity_marker": True,
            },
        }
    ]
    assert result["success"] is False
    assert result["effect_status"] == "indeterminate"
    assert result["changed"] is None
    assert result["writes"] == []
    assert result["failures"] == []
    assert result["indeterminate_failures"] == [
        {
            "stage": "external_identity_persistence",
            "scheme": "catalogue",
            "value": "Record/42:revision=3",
            "error": "marker write failed",
            "exception_type": "RuntimeError",
            "outcome": "indeterminate",
            "readback_verified": False,
            "readback_error": None,
        }
    ]


def test_marker_exception_is_succeeded_when_exact_readback_verifies_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = _identifier()

    monkeypatch.setattr(
        service,
        "upsert_text_for_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("acknowledgement lost")
        ),
    )
    monkeypatch.setattr(
        service,
        "_identity_marker_relation_candidates",
        lambda _identifier: service.ExternalIdentityCandidateScan(
            candidate_concept_ids=("#V#record_42",),
            exhaustive=False,
            saturated_stages=("text_relations",),
        ),
    )

    result = service.persist_external_identity_markers(
        concept_id="#V#record_42",
        identifiers=[identifier],
    )

    assert result["success"] is True
    assert result["effect_status"] == "succeeded"
    assert result["changed"] is None
    assert result["failures"] == []
    assert result["indeterminate_failures"] == []
    assert result["writes"][0]["write_outcome"] == (
        "readback_verified_after_exception"
    )
    assert result["writes"][0]["write_error"] == "acknowledgement lost"
