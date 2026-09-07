from __future__ import annotations

import json
from typing import Any

import pytest


@pytest.fixture
def authorised_mail_marker_store(monkeypatch):
    from src.backend.security import access_control
    from src.backend.services import mail_profile_resource_vontology_service as profiles
    from src.backend.services import source_processing_marker_service as service

    resource, alias = "#V#gmail_profile_lab", "lab-mail"
    payloads = {}
    existing = {str(spec["concept_id"]) for spec in service._SUPPORT_TYPE_SPECS}
    monkeypatch.setattr(
        access_control, "get_effective_user_concept_id", lambda: "#V#alice"
    )

    def resolve(*, user_concept_id, requested_profile_id):
        assert user_concept_id == "#V#alice"
        return {
            "success": requested_profile_id in {resource, alias},
            "profile_resource_concept_id": resource,
            "profile_id": alias,
        }

    monkeypatch.setattr(profiles, "resolve_authorised_gmail_profile_for_user", resolve)
    monkeypatch.setattr(
        service, "_find_existing_concept_ids", lambda ids: set(ids) & existing
    )
    monkeypatch.setattr(service, "_concept_exists", lambda cid: cid in existing)
    monkeypatch.setattr(service, "_load_marker_payload", lambda cid: payloads.get(cid))
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **kw: existing.add(kw["concept_id"]),
    )

    def write(**kwargs):
        payloads[kwargs["subject_concept_id"]] = json.loads(kwargs["text"])
        return {"success": True}

    monkeypatch.setattr(service, "upsert_singleton_text_relation", write)
    return service, resource, alias, existing, payloads


@pytest.mark.parametrize("existing_spelling", [None, "alias", "resource"])
def test_mail_profile_spellings_reuse_one_marker_on_read_and_write(
    authorised_mail_marker_store, existing_spelling
):
    service, resource, alias, existing, payloads = authorised_mail_marker_store
    profile = alias if existing_spelling == "alias" else resource
    expected_id = service.source_processing_marker_concept_id(
        source_system="gmail", source_profile=profile, source_item_id="message-1"
    )
    if existing_spelling:
        existing.add(expected_id)
        payloads[expected_id] = {
            "processing_status": "completed",
            "source_fingerprint": "source-1",
        }
        for spelling in (resource, alias):
            read = service.get_source_processing_marker(
                source_system="gmail",
                source_profile=spelling,
                source_item_id="message-1",
                source_fingerprint="source-1",
            )
            assert read["source_processing_marker"] == expected_id
            assert read["source_processing_current"] is True
    for spelling in (resource, alias):
        written = service.record_source_processing_marker(
            source_system="gmail",
            source_profile=spelling,
            source_item_id="message-1",
            processing_status="completed",
            source_fingerprint="source-1",
        )
        assert written["source_processing_marker"] == expected_id
        assert written["source_processing_current"] is True
    assert set(payloads) == {expected_id}
    assert payloads[expected_id]["source_profile"] == profile


def test_existing_duplicate_marker_evidence_is_exposed_without_merging(
    authorised_mail_marker_store,
):
    service, resource, alias, existing, payloads = authorised_mail_marker_store
    ids = [
        service.source_processing_marker_concept_id(
            source_system="gmail", source_profile=spelling, source_item_id="message-1"
        )
        for spelling in (resource, alias)
    ]
    existing.update(ids)
    payloads.update(
        {
            cid: {"processing_status": "completed", "source_fingerprint": str(i)}
            for i, cid in enumerate(ids)
        }
    )
    for spelling in (resource, alias):
        result = service.get_source_processing_marker(
            source_system="gmail",
            source_profile=spelling,
            source_item_id="message-1",
            source_fingerprint="0",
        )
        assert result["source_processing_marker"] == ids[0]
        assert result["alternate_marker_concept_ids"] == [ids[1]]
        assert result["source_profile_resolution"]["runtime_profile_alias"] == alias
    assert payloads[ids[1]]["source_fingerprint"] == "1"


def test_marker_alias_resolution_does_not_infer_other_mailbox_authority(
    authorised_mail_marker_store,
):
    service, resource, alias, existing, payloads = authorised_mail_marker_store
    marker_id = service.source_processing_marker_concept_id(
        source_system="gmail", source_profile=alias, source_item_id="message-1"
    )
    existing.add(marker_id)
    payloads[marker_id] = {"processing_status": "completed"}
    for system, profile in [("gmail", "someone-elses-mail"), ("dataset", resource)]:
        read = service.get_source_processing_marker(
            source_system=system, source_profile=profile, source_item_id="message-1"
        )
        assert read["source_processing_marker_exists"] is False
        assert read["source_profile_resolution"] == {}


@pytest.mark.parametrize(
    "exists,stored,expected,comparison,current",
    [
        (True, "source-1", None, "not_requested", False),
        (True, "source-1", "source-1", "matched", True),
        (True, "source-1", "source-2", "mismatched", False),
        (False, None, "source-1", "marker_missing", False),
        (True, None, "source-1", "stored_fingerprint_missing", False),
    ],
)
def test_marker_distinguishes_unrequested_comparison_from_changed_source(
    exists, stored, expected, comparison, current
):
    from src.backend.services import source_processing_marker_service as service

    result = service._marker_response_from_payload(
        marker_concept_id="#V#marker",
        source_item_id="source-item",
        marker_exists=exists,
        evidence_payload={
            "processing_status": "completed",
            "source_fingerprint": stored,
        },
        expected_source_fingerprint=expected,
    )
    assert result["source_fingerprint_comparison"] == comparison
    assert result["source_fingerprint_matches"] is current
    assert result["source_processing_current"] is current


def test_find_existing_concept_ids_matches_canonicalised_ids(monkeypatch) -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services import source_processing_marker_service as service

    observed_query: dict[str, Any] = {}
    observed_options: dict[str, Any] = {}

    def fake_find(query, _projection, **kwargs):
        observed_query.update(query)
        observed_options.update(kwargs)
        return [{"concept_id": "#V#hassourceprocessingevidencejson"}]

    monkeypatch.setattr(
        ConceptsRepository,
        "find",
        staticmethod(fake_find),
    )

    result = service._find_existing_concept_ids(
        [service.SOURCE_PROCESSING_EVIDENCE_PREDICATE_ID]
    )

    assert result == {service.SOURCE_PROCESSING_EVIDENCE_PREDICATE_ID}
    assert "#V#hassourceprocessingevidencejson" in observed_query["concept_id"]["$in"]
    assert observed_options == {"limit": 2}


def test_find_existing_concept_ids_uses_repository_access_filter(monkeypatch) -> None:
    from src.backend.db.repositories import concepts_repository
    from src.backend.services import source_processing_marker_service as service

    observed_collection_query: dict[str, Any] = {}
    access_filter_calls: list[dict[str, Any]] = []

    class FakeCursor:
        def __init__(self, rows):
            self._rows = iter(rows)
            self.max_time_ms_value = None

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._rows)

        def max_time_ms(self, value):
            self.max_time_ms_value = value
            return self

        def limit(self, _value):
            return self

    class FakeCollection:
        def find(self, query, _projection):
            observed_collection_query.update(query)
            return FakeCursor([{"concept_id": "#V#visible"}])

    def fake_apply_concept_query_filter(query):
        access_filter_calls.append(dict(query))
        return {"$and": [dict(query), {"visibility_test": "allowed"}]}

    monkeypatch.setattr(
        concepts_repository.ConceptsRepository,
        "collection",
        staticmethod(lambda: FakeCollection()),
    )
    monkeypatch.setattr(
        concepts_repository,
        "apply_concept_query_filter",
        fake_apply_concept_query_filter,
    )
    monkeypatch.setattr(
        concepts_repository,
        "prewarm_concept_relationship_access",
        lambda _batch: None,
    )
    monkeypatch.setattr(
        concepts_repository,
        "sanitize_concept_document",
        lambda document: document,
    )

    result = service._find_existing_concept_ids(["#V#visible", "#V#private"])

    assert result == {"#V#visible"}
    assert access_filter_calls == [
        {"concept_id": {"$in": ["#V#visible", "#V#private"]}}
    ]
    assert observed_collection_query == {
        "$and": [
            {"concept_id": {"$in": ["#V#visible", "#V#private"]}},
            {"visibility_test": "allowed"},
        ]
    }


def test_public_concept_existence_read_fails_closed_without_store(
    monkeypatch,
) -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services import source_processing_marker_service as service

    monkeypatch.setattr(
        ConceptsRepository,
        "collection",
        staticmethod(lambda: None),
    )

    with pytest.raises(RuntimeError, match="concept_store_unavailable"):
        service.find_existing_accessible_concept_ids(["#V#candidate"])


def test_source_processing_marker_records_and_reads_represented_evidence(
    monkeypatch,
) -> None:
    from src.backend.services import source_processing_marker_service as service

    existing: set[str] = {"#V#thing"}
    created: list[dict[str, Any]] = []
    text_rows: dict[str, str] = {}

    def fake_get_concept(concept_id: str):
        if concept_id not in existing:
            raise service.concept_service.ConceptNotFoundError(concept_id)
        return {"concept_id": concept_id}

    def fake_create_concept(**kwargs):
        existing.add(kwargs["concept_id"])
        created.append(dict(kwargs))
        return {"concept_id": kwargs["concept_id"]}

    def fake_upsert_singleton_text_relation(**kwargs):
        text_rows[kwargs["subject_concept_id"]] = kwargs["text"]
        return {"updated": True, "subject_concept_id": kwargs["subject_concept_id"]}

    def fake_get_texts_for_concept(concept_id: str, **_kwargs):
        text = text_rows.get(concept_id)
        return [{"text": text}] if text else []

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        fake_get_concept,
    )
    monkeypatch.setattr(
        service,
        "_find_existing_concept_ids",
        lambda concept_ids: set(concept_ids).intersection(existing),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", fake_create_concept)
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        fake_upsert_singleton_text_relation,
    )
    monkeypatch.setattr(service, "get_texts_for_concept", fake_get_texts_for_concept)

    result = service.record_source_processing_marker(
        source_system="gmail",
        source_profile="vonwitbrock-gmail",
        source_item_id="19f3b77986f71a4d",
        represented_outputs=[
            {
                "result": {
                    "paper_concept_id": "#V#paper_2606_30544",
                    "file_copy_concept_id": "#V#file_copy_2606_30544",
                    "arxiv_id": "2606.30544",
                }
            }
        ],
        workflow_id="#V#email_arxiv_ingestion_from_message_workflow",
    )

    assert result["success"] is True
    assert result["source_processing_marker_created"] is True
    assert result["message_processing_marker_seen"] is True
    assert result["processed_message_id"] == "19f3b77986f71a4d"
    assert result["paper_concept_id"] == "#V#paper_2606_30544"
    assert result["file_copy_concept_id"] == "#V#file_copy_2606_30544"
    assert result["arxiv_id"] == "2606.30544"
    assert any(
        item["concept_id"] == service.SOURCE_PROCESSING_MARKER_TYPE_ID
        for item in created
    )

    evidence = json.loads(text_rows[result["source_processing_marker"]])
    assert evidence["source_system"] == "gmail"
    assert evidence["source_profile"] == "vonwitbrock-gmail"
    assert evidence["message_processing_marker"] == result["source_processing_marker"]

    readback = service.get_source_processing_marker(
        source_system="gmail",
        source_profile="vonwitbrock-gmail",
        source_item_id="19f3b77986f71a4d",
    )

    assert readback["source_processing_marker_exists"] is True
    assert readback["message_processing_marker_seen"] is True
    assert readback["paper_concept_id"] == "#V#paper_2606_30544"
    assert readback["arxiv_id"] == "2606.30544"


def test_source_processing_marker_missing_read_is_typed(monkeypatch) -> None:
    from src.backend.services import source_processing_marker_service as service

    def fake_get_concept(concept_id: str):
        raise service.concept_service.ConceptNotFoundError(concept_id)

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        fake_get_concept,
    )
    result = service.get_source_processing_marker(
        source_system="gmail",
        source_profile="profile",
        source_item_id="msg-1",
    )

    assert result["success"] is True
    assert result["source_processing_marker_exists"] is False
    assert result["message_processing_marker_seen"] is False
    assert result["message_processing_marker"] is None
    assert result["source_processing_evidence"] == {}


@pytest.mark.parametrize("source_profile", [None, "", "   "])
def test_source_processing_marker_write_rejects_missing_profile_before_write(
    monkeypatch,
    source_profile,
) -> None:
    from src.backend.services import source_processing_marker_service as service

    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **_kwargs: pytest.fail("marker write must not start"),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **_kwargs: pytest.fail("marker evidence write must not start"),
    )

    result = service.record_source_processing_marker(
        source_system="gmail",
        source_profile=source_profile,
        source_item_id="msg-1",
    )

    assert result == {
        "success": False,
        "error_code": "missing_source_processing_marker_key",
        "error": (
            "source_system, source_profile, and source_item_id are required "
            "before recording a source-processing marker."
        ),
        "missing": ["source_profile"],
    }


@pytest.mark.parametrize(
    ("represented_outputs", "error_code"),
    (
        ([], "represented_artifacts_required"),
        (
            [{"paper_concept_id": "#V#paper_not_yet_durable"}],
            "represented_artifacts_not_found",
        ),
    ),
)
def test_completion_marker_cannot_precede_represented_artefact_readback(
    monkeypatch,
    represented_outputs,
    error_code,
) -> None:
    from src.backend.services import source_processing_marker_service as service

    monkeypatch.setattr(service, "_find_existing_concept_ids", lambda _ids: set())
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **_kwargs: pytest.fail("marker concept must not be created"),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **_kwargs: pytest.fail("marker evidence must not be written"),
    )

    result = service.record_source_processing_marker(
        source_system="gmail",
        source_profile="vonwitbrock-gmail",
        source_item_id="message-before-paper",
        represented_outputs=represented_outputs,
        require_represented_artifacts=True,
    )

    assert result["success"] is False
    assert result["error_code"] == error_code


def test_source_processing_marker_compares_and_supersedes_fingerprints(
    monkeypatch,
) -> None:
    from src.backend.services import source_processing_marker_service as service

    existing: set[str] = {spec["concept_id"] for spec in service._SUPPORT_TYPE_SPECS}
    text_rows: dict[str, str] = {}

    def fake_get_concept(concept_id: str):
        if concept_id not in existing:
            raise service.concept_service.ConceptNotFoundError(concept_id)
        return {"concept_id": concept_id}

    def fake_create_concept(**kwargs):
        existing.add(kwargs["concept_id"])
        return {"concept_id": kwargs["concept_id"]}

    def fake_upsert(**kwargs):
        text_rows[kwargs["subject_concept_id"]] = kwargs["text"]
        return {"updated": True}

    def fake_get_texts(concept_id: str, **_kwargs):
        text = text_rows.get(concept_id)
        return [{"text": text}] if text else []

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        fake_get_concept,
    )
    monkeypatch.setattr(
        service,
        "_find_existing_concept_ids",
        lambda concept_ids: set(concept_ids).intersection(existing),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", fake_create_concept)
    monkeypatch.setattr(service, "upsert_singleton_text_relation", fake_upsert)
    monkeypatch.setattr(service, "get_texts_for_concept", fake_get_texts)

    existing.add("#V#represented-one")
    first = service.record_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-one",
        represented_artifact_concept_ids=["#V#represented-one"],
        processing_evidence={"source_record_version_id": "version-one"},
        processing_authority_fingerprint="sha256:authority-one",
    )
    unchanged = service.get_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-one",
        processing_authority_fingerprint="sha256:authority-one",
    )
    authority_stale = service.get_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-one",
        processing_authority_fingerprint="sha256:authority-two",
    )
    existing.remove("#V#represented-one")
    artefact_missing = service.get_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-one",
        processing_authority_fingerprint="sha256:authority-one",
    )
    existing.add("#V#represented-one")
    stale = service.get_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-two",
    )
    existing.add("#V#represented-two")
    second = service.record_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-two",
        represented_artifact_concept_ids=["#V#represented-two"],
        processing_evidence={"source_record_version_id": "version-two"},
        processing_authority_fingerprint="sha256:authority-two",
    )
    service.record_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-two",
        processing_status="invalidated",
        processing_authority_fingerprint="sha256:authority-two",
    )
    invalidated = service.get_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-two",
        processing_authority_fingerprint="sha256:authority-two",
    )

    assert first["source_fingerprint_matches"] is True
    assert unchanged["source_processing_current"] is True
    assert authority_stale["source_processing_current"] is False
    assert authority_stale["processing_authority_matches"] is False
    assert artefact_missing["source_processing_current"] is False
    assert artefact_missing["represented_artifacts_exist"] is False
    assert stale["source_processing_current"] is False
    assert second["source_fingerprint_matches"] is True
    evidence = second["source_processing_evidence"]
    assert evidence["previous_source_fingerprint"] == "fingerprint-one"
    assert evidence["source_fingerprint_history"] == [
        "fingerprint-one",
        "fingerprint-two",
    ]
    assert invalidated["source_fingerprint_matches"] is True
    assert invalidated["processing_status"] == "invalidated"
    assert invalidated["source_processing_current"] is False


def test_spreadsheet_marker_requires_represented_outputs(monkeypatch) -> None:
    from src.backend.services import source_processing_marker_service as service

    existing: set[str] = {spec["concept_id"] for spec in service._SUPPORT_TYPE_SPECS}
    text_rows: dict[str, str] = {}

    monkeypatch.setattr(
        service,
        "_find_existing_concept_ids",
        lambda concept_ids: set(concept_ids).intersection(existing),
    )
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **kwargs: (
            existing.add(kwargs["concept_id"]) or {"concept_id": kwargs["concept_id"]}
        ),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: (
            text_rows.__setitem__(kwargs["subject_concept_id"], kwargs["text"])
            or {"updated": True}
        ),
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, **_kwargs: (
            [{"text": text_rows[concept_id]}] if concept_id in text_rows else []
        ),
    )
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda concept_id: (
            {"concept_id": concept_id}
            if concept_id in existing
            else (_ for _ in ()).throw(service.concept_service.ConceptNotFoundError())
        ),
    )

    written = service.record_source_processing_marker(
        source_system="spreadsheet_record",
        source_profile="private-dataset",
        source_item_id="opaque-record-key",
        source_fingerprint="fingerprint-one",
    )

    assert written["source_fingerprint_matches"] is True
    assert written["source_processing_current"] is False


def test_gmail_marker_can_require_current_represented_artifacts(monkeypatch) -> None:
    from src.backend.services import source_processing_marker_service as service

    existing = {"#V#paper_2608_00038"}
    monkeypatch.setattr(
        service,
        "_find_existing_concept_ids",
        lambda concept_ids: set(concept_ids).intersection(existing),
    )
    payload = {
        "source_system": "gmail",
        "processing_status": "processed",
        "source_fingerprint": "gmail-message-id:v1:profile:message-1",
        "processing_authority_fingerprint": "email-arxiv-ingestion:v2",
        "represented_artifact_concept_ids": ["#V#paper_2608_00038"],
    }

    current = service._marker_response_from_payload(
        marker_concept_id="#V#marker",
        source_item_id="message-1",
        marker_exists=True,
        evidence_payload=payload,
        expected_source_fingerprint="gmail-message-id:v1:profile:message-1",
        expected_processing_authority_fingerprint="email-arxiv-ingestion:v2",
        require_represented_artifacts=True,
    )
    existing.clear()
    missing_artifact = service._marker_response_from_payload(
        marker_concept_id="#V#marker",
        source_item_id="message-1",
        marker_exists=True,
        evidence_payload=payload,
        expected_source_fingerprint="gmail-message-id:v1:profile:message-1",
        expected_processing_authority_fingerprint="email-arxiv-ingestion:v2",
        require_represented_artifacts=True,
    )

    assert current["represented_artifacts_required"] is True
    assert current["represented_artifacts_exist"] is True
    assert current["source_processing_current"] is True
    assert missing_artifact["represented_artifacts_exist"] is False
    assert missing_artifact["source_processing_current"] is False
