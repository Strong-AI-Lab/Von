from __future__ import annotations

import sys
from types import SimpleNamespace


def test_extract_organisation_candidates_from_text_uses_authoritative_interpretation(
    monkeypatch,
) -> None:
    from src.backend.services.file_copy_interpretation_service import (
        extract_organisation_candidates_from_text,
    )

    monkeypatch.setattr(
        "src.backend.services.file_copy_diagram_interpretation_vontology_service.infer_file_copy_diagram_semantics",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_diagram_interpretation.v1",
                "organisation_candidates": [
                    {
                        "name": "Te Whatu Ora",
                        "segment_refs": ["single_segment_1"],
                        "evidence_excerpt": "Te Whatu Ora | Research partners",
                    }
                ],
                "relationship_candidates": [],
            },
            {"status": "ok"},
        ),
    )

    rows = extract_organisation_candidates_from_text(
        text="te whatu ora | research partners",
        source="unit_test_diagram_segment",
        page_number=2,
        figure_id="page_2_diagram_candidate",
        extraction_method="pdf_page_ocr",
    )

    assert [row["name"] for row in rows] == ["Te Whatu Ora"]
    assert "confidence" not in rows[0]
    assert rows[0]["evidence_count"] == 1
    assert rows[0]["evidence_excerpts"] == ["Te Whatu Ora | Research partners"]
    provenance = rows[0]["provenance"][0]
    assert provenance["source"] == "unit_test_diagram_segment"
    assert provenance["page_number"] == 2
    assert provenance["figure_id"] == "page_2_diagram_candidate"


def test_summarise_diagram_organisation_candidates_separates_prose_and_diagram(
    monkeypatch,
) -> None:
    from src.backend.services.file_copy_interpretation_service import (
        summarise_diagram_organisation_candidates,
    )

    monkeypatch.setattr(
        "src.backend.services.file_copy_diagram_interpretation_vontology_service.infer_file_copy_diagram_semantics",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_diagram_interpretation.v1",
                "organisation_candidates": [
                    {
                        "name": "Ministry of Health",
                        "segment_refs": ["prose_segment_1", "diagram_segment_1"],
                        "evidence_excerpt": "Ministry of Health",
                    },
                    {
                        "name": "Te Whatu Ora",
                        "segment_refs": ["diagram_segment_1"],
                        "evidence_excerpt": "te whatu ora",
                    },
                    {
                        "name": "University of Auckland",
                        "segment_refs": ["diagram_segment_1"],
                        "evidence_excerpt": "University of Auckland",
                    },
                ],
                "relationship_candidates": [
                    {
                        "source_name": "Te Whatu Ora",
                        "target_name": "University of Auckland",
                        "relation_hint": "association",
                        "segment_refs": ["diagram_segment_1"],
                        "evidence_excerpt": "te whatu ora -- university of auckland",
                    }
                ],
            },
            {"status": "ok"},
        ),
    )

    summary = summarise_diagram_organisation_candidates(
        prose_text="This paper references Ministry of Health in prose text.",
        diagram_segments=[
            {
                "text": "te whatu ora -- university of auckland\nMinistry of Health",
                "page_number": 3,
                "figure_id": "figure_3",
                "extraction_method": "pdf_page_ocr",
            }
        ],
    )

    assert summary["requires_human_confirmation"] is True
    prose_names = {row["name"] for row in summary["prose_organisations"]}
    assert prose_names == {"Ministry of Health"}
    diagram_names = {row["name"] for row in summary["diagram_organisations"]}
    assert diagram_names == {
        "Ministry of Health",
        "Te Whatu Ora",
        "University of Auckland",
    }
    diagram_only_names = {
        row["name"] for row in summary["diagram_only_organisations"]
    }
    assert diagram_only_names == {"Te Whatu Ora", "University of Auckland"}
    relationship_rows = summary["diagram_relationship_candidates"]
    assert relationship_rows[0]["source_name"] == "Te Whatu Ora"
    assert relationship_rows[0]["target_name"] == "University of Auckland"
    assert relationship_rows[0]["relation_hint"] == "association"
    assert relationship_rows[0]["provenance"][0]["page_number"] == 3
    assert summary["authority_diagnostics"]["status"] == "ok"


def test_summarise_diagram_organisation_candidates_fails_closed_when_authority_unavailable(
    monkeypatch,
) -> None:
    from src.backend.services.file_copy_interpretation_service import (
        summarise_diagram_organisation_candidates,
    )

    monkeypatch.setattr(
        "src.backend.services.file_copy_diagram_interpretation_vontology_service.infer_file_copy_diagram_semantics",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_diagram_interpretation.v1",
                "organisation_candidates": [],
                "relationship_candidates": [],
            },
            {"status": "prompt_unavailable", "error": "prompt_missing"},
        ),
    )

    summary = summarise_diagram_organisation_candidates(
        prose_text="Prose mentions a consortium.",
        diagram_segments=[
            {
                "text": "Consortium map with organisations",
                "page_number": 1,
                "figure_id": "figure_1",
                "extraction_method": "pdf_page_ocr",
            }
        ],
    )

    assert summary["prose_organisations"] == []
    assert summary["diagram_organisations"] == []
    assert summary["diagram_only_organisations"] == []
    assert summary["diagram_relationship_candidates"] == []
    assert summary["requires_human_confirmation"] is True
    assert summary["authority_diagnostics"]["status"] == "prompt_unavailable"


def test_extract_pdf_diagram_organisation_candidates_uses_authoritative_interpretation(
    monkeypatch,
) -> None:
    from src.backend.services.file_copy_interpretation_service import (
        extract_pdf_diagram_organisation_candidates,
    )

    class _FakePage:
        def get_text(self, mode):
            assert mode == "text"
            return "ecosystem map"

        def get_images(self, full=True):
            assert full is True
            return [object()]

    class _FakeDoc:
        def __len__(self):
            return 1

        def load_page(self, index):
            assert index == 0
            return _FakePage()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda **_kwargs: _FakeDoc()))
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service._extract_pdf_page_ocr_text",
        lambda page, dpi: {
            "text": "Te Whatu Ora -- University of Auckland",
            "method": "pdf_page_ocr",
            "error": None,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_diagram_interpretation_vontology_service.infer_file_copy_diagram_semantics",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_diagram_interpretation.v1",
                "organisation_candidates": [
                    {
                        "name": "Te Whatu Ora",
                        "segment_refs": ["diagram_segment_1"],
                        "evidence_excerpt": "Te Whatu Ora",
                    },
                    {
                        "name": "University of Auckland",
                        "segment_refs": ["diagram_segment_1"],
                        "evidence_excerpt": "University of Auckland",
                    },
                ],
                "relationship_candidates": [
                    {
                        "source_name": "Te Whatu Ora",
                        "target_name": "University of Auckland",
                        "relation_hint": "association",
                        "segment_refs": ["diagram_segment_1"],
                        "evidence_excerpt": "Te Whatu Ora -- University of Auckland",
                    }
                ],
            },
            {"status": "ok"},
        ),
    )

    summary = extract_pdf_diagram_organisation_candidates(
        data_bytes=b"%PDF-1.4",
        content_type="application/pdf",
        original_filename="ecosystem.pdf",
        prose_text=None,
    )

    assert summary["available"] is True
    assert summary["method"] == "pymupdf_diagram_ocr"
    diagram_names = {row["name"] for row in summary["diagram_organisations"]}
    assert diagram_names == {"Te Whatu Ora", "University of Auckland"}
    assert summary["diagram_relationship_candidates"][0]["relation_hint"] == "association"
    assert summary["page_summaries"][0]["diagram_candidate"] is True
