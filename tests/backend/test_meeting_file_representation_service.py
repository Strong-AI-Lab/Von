from __future__ import annotations


def test_materialise_meeting_representation_for_transcript_persists_core_effects(
    monkeypatch,
):
    from src.backend.services.meeting_file_representation_service import (
        materialise_meeting_representation_for_file_copy,
    )

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        lambda **_kwargs: {"results": []},
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.create_concept",
        lambda **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.update_concept",
        lambda *_args, **_kwargs: {"success": True},
    )

    relation_writes: list[dict[str, object]] = []

    def _fake_upsert_text_for_concept(**kwargs):
        relation_writes.append(dict(kwargs))
        return {
            "relation_id": f"rel-{len(relation_writes)}",
            "relation_created": True,
            "predicate": kwargs.get("predicate"),
        }

    relationship_writes: list[dict[str, object]] = []

    def _fake_add_relationship(**kwargs):
        relationship_writes.append(dict(kwargs))
        return {"success": True, "forward_modified": True}

    monkeypatch.setattr(
        "src.backend.services.meeting_file_representation_service.upsert_text_for_concept",
        _fake_upsert_text_for_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.meeting_file_representation_service.add_relationship",
        _fake_add_relationship,
    )

    result = materialise_meeting_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_meeting_1",
        extracted_text=(
            "Meeting: Weekly Research Sync\n"
            "Date: 2026-03-05 10:00\n"
            "Attendees: Jane Doe, John Smith\n"
            "Action item: Jane to prepare summary for next week.\n"
        ),
        original_filename="weekly-research-transcript.txt",
        interpretation={"description": "Transcript extracted from uploaded meeting notes."},
    )

    assert result["attempted"] is True
    assert result["verified"] is True
    assert result["success"] is True
    assert result["reason"] == "meeting_representation_verified"
    assert result["representation_mode"] == "transcript"
    assert result["meeting_name"] == "Weekly Research Sync"
    assert "2026-03-05 10:00" in result["datetime_candidates"]
    assert "Jane Doe" in result["participants"]
    assert result["meeting_concept_id"].startswith("#V#meeting_weekly_research_sync_")

    predicates = [row["predicate"] for row in relation_writes]
    assert "hasName" in predicates
    assert "#V#hasNote" in predicates

    assert relationship_writes == [
        {
            "source_id": "#V#uploaded_file_copy_meeting_1",
            "predicate": "#V#documentary_evidence_for",
            "target": result["meeting_concept_id"],
        }
    ]


def test_materialise_meeting_representation_fails_when_identity_unresolved():
    from src.backend.services.meeting_file_representation_service import (
        materialise_meeting_representation_for_file_copy,
    )

    result = materialise_meeting_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_meeting_2",
        extracted_text=(
            "Meeting transcript\n"
            "Agenda\n"
            "Minutes\n"
        ),
        original_filename="meeting-transcript.txt",
    )

    assert result["attempted"] is True
    assert result["verified"] is False
    assert result["success"] is False
    assert result["reason"] == "meeting_identity_unresolved"
    assert result["meeting_concept_id"] is None


def test_materialise_meeting_representation_is_not_attempted_for_unrelated_text():
    from src.backend.services.meeting_file_representation_service import (
        materialise_meeting_representation_for_file_copy,
    )

    result = materialise_meeting_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_generic_1",
        extracted_text="This is a product specification and architecture document.",
        original_filename="specification.txt",
    )

    assert result["attempted"] is False
    assert result["verified"] is False
    assert result["success"] is False
    assert result["reason"] == "not_meeting_artefact"
