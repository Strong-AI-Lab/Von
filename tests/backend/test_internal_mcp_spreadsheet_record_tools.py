from __future__ import annotations

from src.backend.integrations.internal_mcp import spreadsheet_record_tools
from src.backend.services import source_processing_marker_service
from src.backend.services import spreadsheet_materialisation_guard_service
from src.backend.services import spreadsheet_record_ingestion_service


def _guard_preview() -> dict:
    return {
        "success": True,
        "materialisation_guard": {
            "concept_slots": [
                {"allowed_existing_concept_ids": ["#V#candidate_b"]},
                {
                    "allowed_existing_concept_ids": [
                        "#V#candidate_a",
                        "#V#candidate_b",
                    ]
                },
            ]
        },
    }


def test_materialisation_request_binds_one_bounded_existence_read(
    monkeypatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        spreadsheet_materialisation_guard_service,
        "build_spreadsheet_kr_materialisation_guard",
        lambda **_kwargs: _guard_preview(),
    )

    def _find_existing(candidate_ids):
        captured["candidate_ids"] = candidate_ids
        return {"#V#candidate_b"}

    monkeypatch.setattr(
        source_processing_marker_service,
        "find_existing_accessible_concept_ids",
        _find_existing,
    )

    def _build_request(*, record, reusable_existing_concept_ids):
        captured["record"] = record
        captured["reusable_existing_concept_ids"] = (
            reusable_existing_concept_ids
        )
        return {"success": True}

    monkeypatch.setattr(
        spreadsheet_record_ingestion_service,
        "build_spreadsheet_record_materialisation_request",
        _build_request,
    )
    record = {"ready_for_materialisation": True}

    result = (
        spreadsheet_record_tools._build_spreadsheet_record_materialisation_request(
            record=record
        )
    )

    assert result == {"success": True}
    assert captured == {
        "candidate_ids": ["#V#candidate_a", "#V#candidate_b"],
        "record": record,
        "reusable_existing_concept_ids": ["#V#candidate_b"],
    }


def test_materialisation_request_fails_closed_when_existence_read_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        spreadsheet_materialisation_guard_service,
        "build_spreadsheet_kr_materialisation_guard",
        lambda **_kwargs: _guard_preview(),
    )

    def _raise_lookup(_candidate_ids):
        raise TimeoutError("bounded lookup timed out")

    monkeypatch.setattr(
        source_processing_marker_service,
        "find_existing_accessible_concept_ids",
        _raise_lookup,
    )

    def _unexpected_build(**_kwargs):
        raise AssertionError("write authority must not be built after lookup failure")

    monkeypatch.setattr(
        spreadsheet_record_ingestion_service,
        "build_spreadsheet_record_materialisation_request",
        _unexpected_build,
    )

    result = (
        spreadsheet_record_tools._build_spreadsheet_record_materialisation_request(
            record={"ready_for_materialisation": True}
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "spreadsheet_reuse_authority_read_failed"
    assert result["error_details"] == {"candidate_count": 2}
