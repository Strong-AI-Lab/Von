from __future__ import annotations


def test_extract_organisation_candidates_from_text_includes_provenance() -> None:
    from src.backend.services.file_copy_interpretation_service import (
        extract_organisation_candidates_from_text,
    )

    rows = extract_organisation_candidates_from_text(
        text=(
            "Ministry of Health collaborated with University of Auckland and WHO "
            "on the governance programme."
        ),
        source="unit_test_diagram_segment",
        page_number=2,
        figure_id="page_2_diagram_candidate",
        extraction_method="pdf_page_ocr",
    )

    names = {str(row.get("name")) for row in rows}
    assert "Ministry of Health" in names
    assert "University of Auckland" in names
    assert "WHO" in names
    assert rows
    for row in rows:
        provenance = row.get("provenance") or {}
        assert provenance.get("source") == "unit_test_diagram_segment"
        assert provenance.get("page_number") == 2
        assert provenance.get("figure_id") == "page_2_diagram_candidate"


def test_extract_relationship_candidates_from_text_detects_connectors() -> None:
    from src.backend.services.file_copy_interpretation_service import (
        extract_relationship_candidates_from_text,
    )

    relation_rows = extract_relationship_candidates_from_text(
        text="University of Auckland -> Ministry of Health\nWHO -- UNICEF",
        organisation_names=[
            "University of Auckland",
            "Ministry of Health",
            "WHO",
            "UNICEF",
        ],
        page_number=1,
        figure_id="page_1_diagram_candidate",
        extraction_method="pdf_page_ocr",
    )

    assert relation_rows
    relation_hints = {str(row.get("relation_hint")) for row in relation_rows}
    assert "directed_link" in relation_hints
    assert "association" in relation_hints
    for row in relation_rows:
        provenance = row.get("provenance") or {}
        assert provenance.get("page_number") == 1
        assert provenance.get("figure_id") == "page_1_diagram_candidate"


def test_summarise_diagram_organisation_candidates_marks_diagram_only() -> None:
    from src.backend.services.file_copy_interpretation_service import (
        summarise_diagram_organisation_candidates,
    )

    summary = summarise_diagram_organisation_candidates(
        prose_text="This paper references Ministry of Health in prose text.",
        diagram_segments=[
            {
                "text": "Ministry of Health -> University of Auckland\nWHO -- UNICEF",
                "page_number": 3,
                "figure_id": "figure_3",
                "extraction_method": "pdf_page_ocr",
            }
        ],
    )

    assert summary.get("requires_human_confirmation") is True
    diagram_only = summary.get("diagram_only_organisations") or []
    diagram_only_names = {str(row.get("name")) for row in diagram_only}
    assert "University of Auckland" in diagram_only_names
    assert "WHO" in diagram_only_names
    assert summary.get("diagram_relationship_candidates")
