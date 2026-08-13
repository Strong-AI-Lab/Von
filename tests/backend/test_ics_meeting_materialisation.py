from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.backend.services.ics_meeting_parser_service import parse_ics_meeting
from src.backend.services.meeting_file_representation_service import (
    DOCUMENTARY_EVIDENCE_PREDICATE_ID,
    MEETING_DATE_PREDICATE_ID,
    MEETING_END_TIME_PREDICATE_ID,
    MEETING_START_TIME_PREDICATE_ID,
    IcsMeetingMaterialisationError,
    materialise_ics_meeting_representation_for_file_copy,
)
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.meeting_representation_workflow import (
    MEETING_REPRESENTATION_MATERIALISE_ICS_FILE_COPY_ACTION_ID,
    register_meeting_representation_actions,
)


def _meeting(*, uid: str = "meeting-uid@example.org", summary: str = "Lab meeting"):
    parsed = parse_ics_meeting(
        "\r\n".join(
            (
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"SUMMARY:{summary}",
                "DTSTART;TZID=Europe/London:20260812T100000",
                "DTEND;TZID=Europe/London:20260812T110000",
                "ORGANIZER;CN=Alice:mailto:alice@example.org",
                "ATTENDEE;CN=Bob:mailto:bob@example.org",
                "LOCATION:Room 2",
                "DESCRIPTION:Discuss research plans",
                "URL:https://example.org/meeting",
                "STATUS:CONFIRMED",
                "END:VEVENT",
                "END:VCALENDAR",
            )
        )
    )
    assert parsed.meeting is not None
    return parsed.meeting


def _all_day_meeting():
    parsed = parse_ics_meeting(
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:all-day@example.org\r\n"
        "SUMMARY:All-day workshop\r\n"
        "DTSTART;VALUE=DATE:20260813\r\n"
        "DTEND;VALUE=DATE:20260814\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR"
    )
    assert parsed.meeting is not None
    return parsed.meeting


@pytest.fixture(autouse=True)
def _patch_temporal_singleton_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict]:
    from src.backend.services import meeting_file_representation_service as service

    writes: list[dict] = []

    def _upsert(**kwargs):
        writes.append(dict(kwargs))
        return {
            "success": True,
            "concept_id": kwargs["subject_concept_id"],
            "predicate": kwargs["predicate"],
            "kept_relation_id": f"temporal-{len(writes)}",
            "relation_created": True,
        }

    monkeypatch.setattr(service, "upsert_singleton_text_relation", _upsert)
    return writes


def _patch_external_identity_persistence(
    monkeypatch: pytest.MonkeyPatch,
    service,
) -> list[dict]:
    persistence_calls: list[dict] = []

    def _persist(*, concept_id, identifiers):
        identifier = identifiers[0]
        persistence_calls.append(
            {"concept_id": concept_id, "identifiers": list(identifiers)}
        )
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "writes": [
                {
                    "scheme": identifier.scheme,
                    "value": identifier.value,
                    "marker": service.concept_external_identity_service.external_identity_marker(
                        identifier
                    ),
                    "relation_id": "identity-marker-relation",
                }
            ],
            "failures": [],
            "indeterminate_failures": [],
        }

    monkeypatch.setattr(
        service.concept_external_identity_service,
        "persist_external_identity_markers",
        _persist,
    )
    return persistence_calls


def _patch_external_identity_resolution(
    monkeypatch: pytest.MonkeyPatch,
    service,
    *,
    status: str = "not_found",
    candidate_concept_ids: tuple[str, ...] = (),
    resolution_source: str | None = None,
) -> tuple[list[object], list[dict]]:
    resolution_calls: list[object] = []
    persistence_calls = _patch_external_identity_persistence(monkeypatch, service)

    def _resolve(identifier):
        resolution_calls.append(identifier)
        return service.concept_external_identity_service.ExternalIdentityResolution(
            status=status,
            identifier=identifier,
            candidate_concept_ids=candidate_concept_ids,
            resolution_source=(
                resolution_source
                or (
                    "persisted_identity_marker"
                    if status == "resolved"
                    else "no_visible_identity_evidence"
                )
            ),
        )

    monkeypatch.setattr(
        service.concept_external_identity_service,
        "resolve_external_identity_candidates",
        _resolve,
    )
    return resolution_calls, persistence_calls


def test_ics_materialisation_uses_uid_identity_and_returns_real_effect_receipt(
    monkeypatch: pytest.MonkeyPatch,
    _patch_temporal_singleton_writes: list[dict],
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    resolution_calls, persistence_calls = _patch_external_identity_resolution(
        monkeypatch, service
    )
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: (_ for _ in ()).throw(
            service.concept_service.ConceptNotFoundError("not found")
        ),
    )
    concept_creates: list[dict] = []
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **kwargs: concept_creates.append(dict(kwargs)) or {"success": True},
    )
    text_writes: list[dict] = []
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **kwargs: (
            text_writes.append(dict(kwargs))
            or {"relation_id": f"rel-{len(text_writes)}"},
            None,
        ),
    )
    relationship_writes: list[dict] = []
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda **kwargs: (
            relationship_writes.append(dict(kwargs))
            or {"success": True, "modified": True}
        ),
    )

    result = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        file_copy_concept_id="#V#file",
        meeting=_meeting(),
    )

    assert result["success"] is True
    assert result["created_meeting_concept"] is True
    assert result["meeting_concept_id"].startswith("#V#meeting_ics_")
    assert "pending" not in result["response_text"].casefold()
    assert len(resolution_calls) == 1
    assert resolution_calls[0].scheme == "icalendar.uid"
    assert resolution_calls[0].value == "meeting-uid@example.org"
    assert persistence_calls[0]["concept_id"] == result["meeting_concept_id"]
    assert persistence_calls[0]["identifiers"][0].scheme == "icalendar.uid"
    assert concept_creates[0]["created_by_concept_id"] == "#V#user"
    assert concept_creates[0]["organisation_concept_id"] == "#V#org"
    assert concept_creates[0]["event_namespace"] == "#V#user@org"
    assert concept_creates[0]["parent_concept_ids"] == ["#V#meeting"]
    assert any(
        row["text"] == "meeting-uid@example.org"
        and row["context"]["identifier_type"] == "icalendar.uid"
        for row in text_writes
    )
    assert any(row["text"] == "Lab meeting" for row in text_writes)
    assert not any(row["text"].startswith("DTSTART: ") for row in text_writes)
    assert [
        (row["predicate"], row["text"])
        for row in _patch_temporal_singleton_writes
    ] == [
        (MEETING_DATE_PREDICATE_ID, "2026-08-12"),
        (MEETING_START_TIME_PREDICATE_ID, "2026-08-12T10:00:00"),
        (MEETING_END_TIME_PREDICATE_ID, "2026-08-12T11:00:00"),
    ]
    assert all(
        row["context"]["time_zone"] == "Europe/London"
        for row in _patch_temporal_singleton_writes
    )
    assert len(result["temporal_effect_receipts"]) == 3
    assert not any(
        row["text"].startswith(("ORGANIZER: ", "ATTENDEE: ")) for row in text_writes
    )
    assert relationship_writes == [
        {
            "source_id": "#V#file",
            "predicate": DOCUMENTARY_EVIDENCE_PREDICATE_ID,
            "target": result["meeting_concept_id"],
        }
    ]
    receipt = result["relationship_effect_receipt"]
    assert receipt["effective_arguments"] == relationship_writes[0]
    assert receipt["effective_payload"] == {
        "success": True,
        "effect_status": "succeeded",
        "relationship_type": "concept_relation",
        "source_id": "#V#file",
        "predicate": DOCUMENTARY_EVIDENCE_PREDICATE_ID,
        "predicate_input": DOCUMENTARY_EVIDENCE_PREDICATE_ID,
        "target": result["meeting_concept_id"],
        "added": True,
        "changed": True,
    }


def test_ics_temporal_projection_preserves_all_day_exclusive_end() -> None:
    from src.backend.services import meeting_file_representation_service as service

    assert service._ics_temporal_relation_specs(_all_day_meeting()) == [
        (
            MEETING_DATE_PREDICATE_ID,
            "2026-08-13",
            {
                "icalendar_property": "DTSTART",
                "source": "ics_meeting_representation",
                "value_type": "date",
                "is_utc": False,
                "raw_value": "20260813",
            },
        ),
        (
            MEETING_START_TIME_PREDICATE_ID,
            "2026-08-13",
            {
                "icalendar_property": "DTSTART",
                "source": "ics_meeting_representation",
                "value_type": "date",
                "is_utc": False,
                "raw_value": "20260813",
            },
        ),
        (
            MEETING_END_TIME_PREDICATE_ID,
            "2026-08-14",
            {
                "icalendar_property": "DTEND",
                "source": "ics_meeting_representation",
                "value_type": "date",
                "is_utc": False,
                "raw_value": "20260814",
                "end_semantics": "exclusive",
            },
        ),
    ]


def test_ics_uid_identity_keeps_same_title_different_uids_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    _patch_external_identity_resolution(monkeypatch, service)
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: (_ for _ in ()).throw(
            service.concept_service.ConceptNotFoundError("not found")
        ),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", lambda **_kwargs: {})
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **_kwargs: ({"relation_id": "rel"}, None),
    )
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda **_kwargs: {"success": True, "forward_modified": True},
    )

    first = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user",
        organisation_concept_id=None,
        namespace=None,
        file_copy_concept_id="#V#file-1",
        meeting=_meeting(uid="uid-one", summary="Same title"),
    )
    second = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user",
        organisation_concept_id=None,
        namespace=None,
        file_copy_concept_id="#V#file-2",
        meeting=_meeting(uid="uid-two", summary="Same title"),
    )

    assert first["meeting_concept_id"] != second["meeting_concept_id"]


def test_ics_uid_identity_keeps_same_uid_distinct_across_actor_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    _patch_external_identity_resolution(monkeypatch, service)
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: (_ for _ in ()).throw(
            service.concept_service.ConceptNotFoundError("not found")
        ),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", lambda **_kwargs: {})
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **_kwargs: ({"relation_id": "rel"}, None),
    )
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda **_kwargs: {"success": True, "forward_modified": True},
    )

    first = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user_one",
        organisation_concept_id="#V#shared_org",
        namespace="#V#user_one@shared_org",
        file_copy_concept_id="#V#file-1",
        meeting=_meeting(uid="shared-provider-uid"),
    )
    second = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user_two",
        organisation_concept_id="#V#shared_org",
        namespace="#V#user_two@shared_org",
        file_copy_concept_id="#V#file-2",
        meeting=_meeting(uid="shared-provider-uid"),
    )

    assert first["meeting_concept_id"] != second["meeting_concept_id"]


def test_ics_uid_identity_is_case_sensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    _patch_external_identity_resolution(monkeypatch, service)
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: (_ for _ in ()).throw(
            service.concept_service.ConceptNotFoundError("not found")
        ),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", lambda **_kwargs: {})
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **_kwargs: ({"relation_id": "rel"}, None),
    )
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda **_kwargs: {"success": True, "forward_modified": True},
    )

    lower = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        file_copy_concept_id="#V#file-1",
        meeting=_meeting(uid="case-sensitive-uid"),
    )
    upper = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        file_copy_concept_id="#V#file-2",
        meeting=_meeting(uid="CASE-SENSITIVE-UID"),
    )

    assert lower["meeting_concept_id"] != upper["meeting_concept_id"]


@pytest.mark.parametrize(
    ("resolution_status", "expected_error"),
    [
        ("ambiguous", "ics_uid_identity_ambiguous"),
        ("unverified", "ics_uid_identity_unverified"),
    ],
)
def test_ics_materialisation_fails_closed_on_non_final_uid_resolution(
    monkeypatch: pytest.MonkeyPatch,
    resolution_status: str,
    expected_error: str,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    _resolution_calls, persistence_calls = _patch_external_identity_resolution(
        monkeypatch,
        service,
        status=resolution_status,
        candidate_concept_ids=("#V#candidate",),
        resolution_source="legacy_text_reference",
    )
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **_kwargs: pytest.fail("non-final identity must not create"),
    )

    with pytest.raises(IcsMeetingMaterialisationError) as error:
        materialise_ics_meeting_representation_for_file_copy(
            user_concept_id="#V#user",
            organisation_concept_id="#V#org",
            namespace="#V#user@org",
            file_copy_concept_id="#V#file",
            meeting=_meeting(),
        )

    assert error.value.code == expected_error
    assert persistence_calls == []


def test_ics_materialisation_rejects_resolved_uid_on_non_meeting_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    _resolution_calls, persistence_calls = _patch_external_identity_resolution(
        monkeypatch,
        service,
        status="resolved",
        candidate_concept_ids=("#V#not_a_meeting",),
        resolution_source="persisted_identity_marker",
    )
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {"is_an_instance_of": ["#V#person"]},
        },
    )
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **_kwargs: pytest.fail("wrong-type identity must not create"),
    )

    with pytest.raises(IcsMeetingMaterialisationError) as error:
        materialise_ics_meeting_representation_for_file_copy(
            user_concept_id="#V#user",
            organisation_concept_id="#V#org",
            namespace="#V#user@org",
            file_copy_concept_id="#V#file",
            meeting=_meeting(),
        )

    assert error.value.code == "ics_uid_concept_collision"
    assert error.value.details == {
        "concept_id": "#V#not_a_meeting",
        "reason": "not_a_meeting",
    }
    assert persistence_calls == []


def test_ics_materialisation_reuses_live_shaped_canonical_uid_marker_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    uid = "existing-uid@example.org"
    existing_id = "#V#external_identity_icalendar_uid_8780c00984682eac_scope_151aea196a"
    identity_service = service.concept_external_identity_service
    resolution_calls: list[object] = []
    real_resolve = identity_service.resolve_external_identity_candidates
    monkeypatch.setattr(
        identity_service,
        "_identity_marker_relation_candidates",
        lambda identifier: (
            resolution_calls.append(identifier)
            or identity_service.ExternalIdentityCandidateScan(
                candidate_concept_ids=(existing_id,),
                exhaustive=True,
            )
        ),
    )
    monkeypatch.setattr(
        identity_service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )
    monkeypatch.setattr(
        identity_service,
        "resolve_external_identity_candidates",
        real_resolve,
    )
    persistence_calls = _patch_external_identity_persistence(monkeypatch, service)
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {"is_an_instance_of": ["#V#meeting"]},
        },
    )
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **_kwargs: pytest.fail("same UID must not create a second meeting"),
    )
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **_kwargs: ({"relation_id": "rel"}, None),
    )
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda **_kwargs: {"success": True, "modified": False},
    )

    result = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        file_copy_concept_id="#V#file",
        meeting=_meeting(uid=uid, summary="Renamed meeting"),
    )

    assert result["meeting_concept_id"] == existing_id
    assert result["created_meeting_concept"] is False
    assert result["operation"] == "updated"
    assert len(resolution_calls) == 1
    assert resolution_calls[0].scheme == "icalendar.uid"
    assert resolution_calls[0].value == uid
    assert persistence_calls[0]["concept_id"] == existing_id


def test_ics_materialisation_same_actor_same_uid_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    created_docs: dict[str, dict] = {}
    create_calls: list[dict] = []
    resolution_calls: list[object] = []

    def _resolve(identifier):
        resolution_calls.append(identifier)
        if not created_docs:
            return service.concept_external_identity_service.ExternalIdentityResolution(
                status="not_found",
                identifier=identifier,
                resolution_source="no_visible_identity_evidence",
            )
        return service.concept_external_identity_service.ExternalIdentityResolution(
            status="resolved",
            identifier=identifier,
            candidate_concept_ids=(next(iter(created_docs)),),
            resolution_source="persisted_identity_marker",
        )

    monkeypatch.setattr(
        service.concept_external_identity_service,
        "resolve_external_identity_candidates",
        _resolve,
    )
    _patch_external_identity_resolution(monkeypatch, service)
    monkeypatch.setattr(
        service.concept_external_identity_service,
        "resolve_external_identity_candidates",
        _resolve,
    )

    def _get(concept_id):
        if concept_id in created_docs:
            return created_docs[concept_id]
        raise service.concept_service.ConceptNotFoundError("not found")

    def _create(**kwargs):
        create_calls.append(dict(kwargs))
        created_docs[kwargs["concept_id"]] = {
            "concept_id": kwargs["concept_id"],
            "relationships": {"is_an_instance_of": ["#V#meeting"]},
            "system_tags": ["ics"],
        }
        return created_docs[kwargs["concept_id"]]

    monkeypatch.setattr(
        service.concept_service, "get_concept_by_concept_id_exact", _get
    )
    monkeypatch.setattr(service.concept_service, "create_concept", _create)
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **_kwargs: ({"relation_id": "rel"}, None),
    )
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda **_kwargs: {"success": True, "modified": False},
    )

    first = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        file_copy_concept_id="#V#file-1",
        meeting=_meeting(uid="idempotent-uid", summary="First summary"),
    )
    second = materialise_ics_meeting_representation_for_file_copy(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        file_copy_concept_id="#V#file-2",
        meeting=_meeting(uid="idempotent-uid", summary="Updated summary"),
    )

    assert first["meeting_concept_id"] == second["meeting_concept_id"]
    assert first["created_meeting_concept"] is True
    assert second["created_meeting_concept"] is False
    assert len(create_calls) == 1
    assert len(resolution_calls) == 2


def test_ics_materialisation_fails_before_fact_writes_when_uid_marker_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    monkeypatch.setattr(
        service.concept_external_identity_service,
        "resolve_external_identity_candidates",
        lambda identifier: (
            service.concept_external_identity_service.ExternalIdentityResolution(
                status="not_found",
                identifier=identifier,
                resolution_source="no_visible_identity_evidence",
            )
        ),
    )
    monkeypatch.setattr(
        service.concept_external_identity_service,
        "persist_external_identity_markers",
        lambda **_kwargs: {
            "success": False,
            "effect_status": "indeterminate",
            "changed": None,
            "writes": [],
            "failures": [],
            "indeterminate_failures": [{"stage": "external_identity_persistence"}],
        },
    )
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: (_ for _ in ()).throw(
            service.concept_service.ConceptNotFoundError("not found")
        ),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", lambda **_kwargs: {})
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **_kwargs: pytest.fail("facts must wait for canonical identity marker"),
    )

    with pytest.raises(IcsMeetingMaterialisationError) as error:
        materialise_ics_meeting_representation_for_file_copy(
            user_concept_id="#V#user",
            organisation_concept_id="#V#org",
            namespace="#V#user@org",
            file_copy_concept_id="#V#file",
            meeting=_meeting(),
        )

    assert error.value.code == "ics_uid_identity_persist_failed"
    assert error.value.details["created_meeting_concept"] is True
    assert (
        error.value.details["identity_persistence"]["effect_status"] == "indeterminate"
    )


def test_ics_materialisation_reports_partial_state_when_evidence_write_raises(
    monkeypatch: pytest.MonkeyPatch,
    _patch_temporal_singleton_writes: list[dict],
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    _patch_external_identity_resolution(monkeypatch, service)
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: (_ for _ in ()).throw(
            service.concept_service.ConceptNotFoundError("not found")
        ),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", lambda **_kwargs: {})
    text_writes: list[dict] = []
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **kwargs: (
            text_writes.append(dict(kwargs))
            or {"relation_id": f"rel-{len(text_writes)}"},
            None,
        ),
    )
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("write exploded")),
    )

    with pytest.raises(IcsMeetingMaterialisationError) as error:
        materialise_ics_meeting_representation_for_file_copy(
            user_concept_id="#V#user",
            organisation_concept_id="#V#org",
            namespace="#V#user@org",
            file_copy_concept_id="#V#file",
            meeting=_meeting(),
        )

    assert error.value.code == "ics_documentary_evidence_persist_failed"
    assert error.value.details == {
        "meeting_concept_id": text_writes[0]["subject_concept_id"],
        "created_meeting_concept": True,
        "persisted_text_relation_count": (
            len(text_writes) + len(_patch_temporal_singleton_writes)
        ),
        "exception_type": "RuntimeError",
    }


def test_ics_materialisation_fails_before_notes_when_temporal_write_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import meeting_file_representation_service as service

    _patch_external_identity_resolution(monkeypatch, service)
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: (_ for _ in ()).throw(
            service.concept_service.ConceptNotFoundError("not found")
        ),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", lambda **_kwargs: {})
    note_writes: list[dict] = []
    monkeypatch.setattr(
        service,
        "write_text_relation",
        lambda **kwargs: (
            note_writes.append(dict(kwargs)) or {"relation_id": "unexpected"},
            None,
        ),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("temporal failed")),
    )

    with pytest.raises(IcsMeetingMaterialisationError) as error:
        materialise_ics_meeting_representation_for_file_copy(
            user_concept_id="#V#user",
            organisation_concept_id="#V#org",
            namespace="#V#user@org",
            file_copy_concept_id="#V#file",
            meeting=_meeting(),
        )

    assert error.value.code == "ics_meeting_temporal_fact_persist_failed"
    assert error.value.details["predicate"] == MEETING_DATE_PREDICATE_ID
    assert error.value.details["exception_type"] == "RuntimeError"
    assert note_writes == []


def _execute_action(*, inputs: dict, context: dict | None = None):
    registry = ActionRegistry()
    register_meeting_representation_actions(registry)
    execution_context = dict(context or {})
    result = registry.execute(
        MEETING_REPRESENTATION_MATERIALISE_ICS_FILE_COPY_ACTION_ID,
        inputs=inputs,
        context=execution_context,
        env=WorkflowEnvironment(
            llm_client=None,
            user_concept_id="#V#trusted_user",
            org_concept_id="#V#trusted_org",
            user_namespace="#V#trusted_user@trusted_org",
        ),
    )
    return result, execution_context


def _patch_successful_ics_action(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_text: str,
) -> None:
    from src.backend.workflows.durable import meeting_representation_workflow as action

    monkeypatch.setattr(
        action,
        "fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "data": source_text.encode("utf-8")},
    )
    monkeypatch.setattr(
        action,
        "materialise_ics_meeting_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "reason": "represented",
            "meeting_concept_id": "#V#meeting",
            "relationship_effect_receipt": {"success": True},
            "temporal_effect_receipts": [{"tool": "upsert_singleton_text_relation"}],
            "response_text": "Represented the meeting.",
        },
    )


def test_ics_action_missing_file_is_normal_not_applicable(monkeypatch) -> None:
    from src.backend.workflows.durable import meeting_representation_workflow as action

    monkeypatch.setattr(
        action,
        "fetch_file_copy_bytes",
        lambda **_kwargs: pytest.fail("missing file input must not fetch"),
    )

    result, _context = _execute_action(inputs={})

    assert result.status == "success"
    assert result.outputs["ics_materialisation_outcome"] == "not_applicable"
    assert result.outputs["ics_fast_path_status"] == "not_applicable"
    assert result.outputs["ics_materialisation_succeeded"] is False


def test_ics_action_deduplicates_organizer_attendee_by_casefolded_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:participant-dedup\r\nSUMMARY:Participant deduplication\r\n"
        "ORGANIZER;CN=Alice:mailto:Alice@Example.org\r\n"
        "ATTENDEE;CN=Alice Smith:mailto:alice@example.org\r\n"
        "ATTENDEE;CN=Bob:mailto:bob@example.org\r\n"
        "END:VEVENT\r\nEND:VCALENDAR"
    )
    _patch_successful_ics_action(monkeypatch, source_text=source_text)

    result, _context = _execute_action(inputs={"file_copy_concept_id": "#V#file"})

    assert result.status == "success"
    assert result.outputs["ics_participant_count"] == 2
    assert result.outputs["ics_participants"] == [
        {
            "uri": "mailto:Alice@Example.org",
            "common_name": "Alice",
            "email": "Alice@Example.org",
            "roles": ["organizer", "attendee"],
        },
        {
            "uri": "mailto:bob@example.org",
            "common_name": "Bob",
            "email": "bob@example.org",
            "roles": ["attendee"],
        },
    ]


def test_ics_action_counts_distinct_organizer_and_attendees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:distinct-participants\r\nSUMMARY:Distinct participants\r\n"
        "ORGANIZER;CN=Alice:mailto:alice@example.org\r\n"
        "ATTENDEE;CN=Bob:mailto:bob@example.org\r\n"
        "ATTENDEE;CN=Carol:mailto:carol@example.org\r\n"
        "END:VEVENT\r\nEND:VCALENDAR"
    )
    _patch_successful_ics_action(monkeypatch, source_text=source_text)

    result, _context = _execute_action(inputs={"file_copy_concept_id": "#V#file"})

    assert result.status == "success"
    assert result.outputs["ics_participant_count"] == 3
    assert [
        participant["email"] for participant in result.outputs["ics_participants"]
    ] == ["alice@example.org", "bob@example.org", "carol@example.org"]
    assert [
        participant["roles"] for participant in result.outputs["ics_participants"]
    ] == [["organizer"], ["attendee"], ["attendee"]]


def test_ics_action_bounds_distinct_participant_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attendee_lines = "".join(
        f"ATTENDEE;CN=Person {index}:mailto:person{index}@example.org\r\n"
        for index in range(101)
    )
    source_text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:bounded-participants\r\nSUMMARY:Bounded participants\r\n"
        f"{attendee_lines}"
        "END:VEVENT\r\nEND:VCALENDAR"
    )
    _patch_successful_ics_action(monkeypatch, source_text=source_text)

    result, _context = _execute_action(inputs={"file_copy_concept_id": "#V#file"})

    assert result.status == "success"
    assert result.outputs["ics_participant_count"] == 80
    assert len(result.outputs["ics_participants"]) == 80
    assert result.outputs["ics_participants"][-1]["email"] == ("person79@example.org")


@pytest.mark.parametrize(
    "source_text",
    [
        "Ordinary meeting notes, not an iCalendar file.",
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR",
        (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:one\r\nSUMMARY:One\r\n"
            "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:two\r\nSUMMARY:Two\r\n"
            "END:VEVENT\r\nEND:VCALENDAR"
        ),
    ],
)
def test_ics_action_unsuitable_source_falls_back_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    source_text: str,
) -> None:
    from src.backend.workflows.durable import meeting_representation_workflow as action

    monkeypatch.setattr(
        action,
        "fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "data": source_text.encode("utf-8")},
    )
    monkeypatch.setattr(
        action,
        "materialise_ics_meeting_representation_for_file_copy",
        lambda **_kwargs: pytest.fail("unsuitable ICS must not write"),
    )

    result, _context = _execute_action(inputs={"file_copy_concept_id": "#V#file"})

    assert result.status == "success"
    assert result.outputs["ics_materialisation_outcome"] == "not_applicable"
    assert result.outputs["ics_materialisation_succeeded"] is False


@pytest.mark.parametrize(
    "recurrence_property",
    [
        "RRULE:FREQ=WEEKLY;COUNT=4",
        "RDATE:20260819T090000Z",
        "EXDATE:20260819T090000Z",
        "RECURRENCE-ID:20260812T090000Z",
    ],
)
def test_ics_action_recurrence_falls_back_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    recurrence_property: str,
) -> None:
    from src.backend.workflows.durable import meeting_representation_workflow as action

    source_text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:recurring-event\r\nSUMMARY:Recurring meeting\r\n"
        f"DTSTART:20260812T090000Z\r\n{recurrence_property}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR"
    )
    monkeypatch.setattr(
        action,
        "fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "data": source_text.encode("utf-8")},
    )
    monkeypatch.setattr(
        action,
        "materialise_ics_meeting_representation_for_file_copy",
        lambda **_kwargs: pytest.fail("recurrence must fall back before writes"),
    )

    result, _context = _execute_action(inputs={"file_copy_concept_id": "#V#file"})

    assert result.status == "success"
    assert result.outputs["ics_materialisation_outcome"] == "not_applicable"
    assert result.outputs["ics_materialisation_reason"] == "unsupported_recurrence"
    assert result.outputs["ics_materialisation_succeeded"] is False
    assert result.outputs["ics_parse_result"]["outcome"] == "not_applicable"
    assert result.outputs["ics_parse_result"]["error_code"] == (
        "unsupported_recurrence"
    )


def test_ics_action_non_utf8_source_falls_back_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import meeting_representation_workflow as action

    monkeypatch.setattr(
        action,
        "fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "data": b"\xff\xfe\x00\x81"},
    )
    monkeypatch.setattr(
        action,
        "materialise_ics_meeting_representation_for_file_copy",
        lambda **_kwargs: pytest.fail("non-text source must not write"),
    )

    result, _context = _execute_action(inputs={"file_copy_concept_id": "#V#file"})

    assert result.status == "success"
    assert result.outputs["ics_materialisation_outcome"] == "not_applicable"
    assert result.outputs["ics_materialisation_reason"] == "ics_source_not_utf8_text"
    assert result.outputs["ics_materialisation_succeeded"] is False


def test_ics_action_file_read_exception_returns_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import meeting_representation_workflow as action

    monkeypatch.setattr(
        action,
        "fetch_file_copy_bytes",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    result, _context = _execute_action(inputs={"file_copy_concept_id": "#V#file"})

    assert result.status == "failed"
    assert result.error == "ics_file_copy_read_failed:unexpected_failure"
    assert result.outputs["ics_materialisation_outcome"] == "failed"
    assert result.outputs["ics_materialisation_succeeded"] is False
    assert result.outputs["ics_materialisation_error_details"] == {
        "file_copy_concept_id": "#V#file",
        "exception_type": "RuntimeError",
    }


def test_ics_action_uses_only_trusted_actor_and_suppresses_nested_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import meeting_representation_workflow as action

    source = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:trusted-uid\r\nSUMMARY:Trusted meeting\r\n"
        "END:VEVENT\r\nEND:VCALENDAR"
    )
    fetch_calls: list[dict] = []
    monkeypatch.setattr(
        action,
        "fetch_file_copy_bytes",
        lambda **kwargs: (
            fetch_calls.append(dict(kwargs))
            or {"success": True, "data": source.encode("utf-8")}
        ),
    )
    suppression_reasons: list[str] = []

    @contextmanager
    def _suppress(reason: str):
        suppression_reasons.append(reason)
        yield

    monkeypatch.setattr(action, "suppress_event_workflow_launches", _suppress)
    materialise_calls: list[dict] = []
    receipt = {
        "tool": "add_relationship",
        "status": "ok",
        "effective_arguments": {
            "source_id": "#V#file",
            "predicate": DOCUMENTARY_EVIDENCE_PREDICATE_ID,
            "target": "#V#meeting",
        },
        "effective_payload": {"success": True},
    }
    monkeypatch.setattr(
        action,
        "materialise_ics_meeting_representation_for_file_copy",
        lambda **kwargs: (
            materialise_calls.append(dict(kwargs))
            or {
                "success": True,
                "reason": "pending_readback",
                "meeting_concept_id": "#V#meeting",
                "relationship_effect_receipt": receipt,
                "temporal_effect_receipts": [
                    {"tool": "upsert_singleton_text_relation"}
                ],
                "response_text": "Represented the meeting.",
            }
        ),
    )

    result, _context = _execute_action(
        inputs={
            "file_copy_concept_id": "#V#file",
            "user_concept_id": "#V#forged_user",
            "organisation_concept_id": "#V#forged_org",
            "namespace": "#V#forged_user@forged_org",
        }
    )

    assert result.status == "success"
    assert result.outputs["ics_materialisation_outcome"] == "materialised"
    assert result.outputs["ics_materialisation_succeeded"] is True
    assert result.outputs["relationship_effect_receipt"] == receipt
    assert result.outputs["temporal_effect_receipts"] == [
        {"tool": "upsert_singleton_text_relation"}
    ]
    assert "pending" not in result.outputs["response_text"].casefold()
    assert fetch_calls[0]["user_concept_id"] == "#V#trusted_user"
    assert fetch_calls[0]["organisation_concept_id"] == "#V#trusted_org"
    assert fetch_calls[0]["namespace"] == "#V#trusted_user@trusted_org"
    assert materialise_calls[0]["user_concept_id"] == "#V#trusted_user"
    assert materialise_calls[0]["organisation_concept_id"] == "#V#trusted_org"
    assert materialise_calls[0]["namespace"] == "#V#trusted_user@trusted_org"
    assert suppression_reasons == [
        MEETING_REPRESENTATION_MATERIALISE_ICS_FILE_COPY_ACTION_ID
    ]


def test_ics_action_write_failure_fails_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.workflows.durable import meeting_representation_workflow as action

    source = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:write-failure\r\nSUMMARY:Write failure\r\n"
        "END:VEVENT\r\nEND:VCALENDAR"
    )
    monkeypatch.setattr(
        action,
        "fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "data": source.encode("utf-8")},
    )
    monkeypatch.setattr(
        action,
        "materialise_ics_meeting_representation_for_file_copy",
        lambda **_kwargs: (_ for _ in ()).throw(
            IcsMeetingMaterialisationError("ics_documentary_evidence_persist_failed")
        ),
    )

    result, _context = _execute_action(inputs={"file_copy_concept_id": "#V#file"})

    assert result.status == "failed"
    assert result.error == "ics_documentary_evidence_persist_failed"
    assert result.outputs["ics_materialisation_outcome"] == "failed"
    assert result.outputs["ics_fast_path_status"] == "failed"
    assert result.outputs["ics_materialisation_succeeded"] is False
    assert (
        result.outputs["ics_materialisation_reason"]
        == "ics_documentary_evidence_persist_failed"
    )


def test_durable_registry_registers_ics_materialisation_action() -> None:
    from src.backend.workflows.durable.registry_factory import (
        build_durable_action_registry,
    )

    registry = build_durable_action_registry()
    spec = registry.get(MEETING_REPRESENTATION_MATERIALISE_ICS_FILE_COPY_ACTION_ID)

    assert spec is not None
    assert spec.output_schema is not None
    properties = spec.output_schema["properties"]
    assert properties["ics_participant_count"]["maximum"] == 80
    assert properties["ics_participants"]["maxItems"] == 80
