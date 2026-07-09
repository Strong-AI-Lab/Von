from __future__ import annotations

import json
from typing import Any


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
