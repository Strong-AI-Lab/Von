"""Helpers for interpreting uploaded file-copy content (JVNAUTOSCI-1302).

This module keeps image/document interpretation logic out of MCP handlers so
the same behaviour can be reused by workflow-driven ingestion paths.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..languagemodels.model_defaults import DEFAULT_OPENAI_MODEL
from .llm_api_key_resolution import get_gemini_api_key

logger = logging.getLogger(__name__)

_DOCX_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }
)

_FILE_SUBTYPE_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "docx_mime_or_extension",
        "type_concept_id": "#V#msword_docx_computer_file_copy",
        "mime_types": _DOCX_MIME_TYPES,
        "extensions": (".docx",),
    },
)

_DIAGRAM_KEYWORD_PATTERN = re.compile(
    r"\b(diagram|ecosystem|governance|network|stakeholder|consortium|"
    r"alliance|architecture|workflow|pipeline|flow|map|chart)\b",
    re.IGNORECASE,
)
_RELATION_CONNECTOR_PATTERN = re.compile(r"(?:->|=>|→|↔|<->|--|—|-)")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_content_type_token(value: Any) -> str | None:
    content_type = _normalise_optional_text(value)
    if not content_type:
        return None
    # MIME parameters are optional for detection; keep only the media type token.
    token = content_type.split(";", 1)[0].strip().lower()
    return token or None


def _normalise_filename_extension(value: Any) -> str | None:
    filename = _normalise_optional_text(value)
    if not filename:
        return None
    _base, extension = os.path.splitext(filename.lower())
    return extension or None


def infer_uploaded_file_subtype(
    *,
    content_type: str | None,
    original_filename: str | None,
) -> dict[str, Any]:
    """Infer a specific uploaded file-copy subtype when evidence is sufficient."""
    try:
        from .file_copy_typing_service import infer_file_copy_typing

        typing_result = infer_file_copy_typing(
            content_type=content_type,
            original_filename=original_filename,
        )
        matched_rule_ids = typing_result.get("matched_rule_ids")
        rule_id = (
            matched_rule_ids[0]
            if isinstance(matched_rule_ids, list) and matched_rule_ids
            else None
        )
        return {
            "determinable": bool(typing_result.get("determinable")),
            "type_concept_id": typing_result.get("primary_type_concept_id"),
            "rule_id": rule_id,
            "matched_signals": list(typing_result.get("matched_signals") or []),
            "content_type_token": typing_result.get("content_type_token"),
            "filename_extension": typing_result.get("filename_extension"),
            "semantic_type_concept_id": typing_result.get("semantic_type_concept_id"),
            "format_type_concept_id": typing_result.get("format_type_concept_id"),
            "asserted_type_concept_ids": list(
                typing_result.get("asserted_type_concept_ids") or []
            ),
        }
    except Exception:
        content_type_token = _normalise_content_type_token(content_type)
        extension = _normalise_filename_extension(original_filename)

        for rule in _FILE_SUBTYPE_RULES:
            mime_types = {
                item.strip().lower()
                for item in (rule.get("mime_types") or ())
                if isinstance(item, str) and item.strip()
            }
            extensions = {
                item.strip().lower()
                for item in (rule.get("extensions") or ())
                if isinstance(item, str) and item.strip()
            }
            mime_match = (
                isinstance(content_type_token, str) and content_type_token in mime_types
            )
            extension_match = isinstance(extension, str) and extension in extensions
            if not (mime_match or extension_match):
                continue
            matched_signals: list[str] = []
            if mime_match:
                matched_signals.append("mime_type")
            if extension_match:
                matched_signals.append("filename_extension")
            return {
                "determinable": True,
                "type_concept_id": rule.get("type_concept_id"),
                "rule_id": rule.get("rule_id"),
                "matched_signals": matched_signals,
                "content_type_token": content_type_token,
                "filename_extension": extension,
            }

        return {
            "determinable": False,
            "type_concept_id": None,
            "rule_id": None,
            "matched_signals": [],
            "content_type_token": content_type_token,
            "filename_extension": extension,
        }


def _normalise_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalise_candidate_name(value: str) -> str:
    cleaned = _normalise_whitespace(value).strip(" \t\r\n.,;:()[]{}")
    if cleaned.lower().startswith("the "):
        cleaned = cleaned[4:].strip()
    return cleaned


def _coerce_segment_id(
    value: Any,
    *,
    fallback_prefix: str,
    index: int,
) -> str:
    cleaned = _normalise_optional_text(value)
    if cleaned:
        return cleaned
    return f"{fallback_prefix}_{index + 1}"


def _prepare_diagram_interpretation_segments(
    *,
    prose_text: str | None,
    diagram_segments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    prepared_segments: list[dict[str, Any]] = []
    prose_segment_ids: set[str] = set()

    prose_value = _normalise_optional_text(prose_text)
    if prose_value:
        segment_id = "prose_segment_1"
        prose_segment_ids.add(segment_id)
        prepared_segments.append(
            {
                "segment_id": segment_id,
                "source_type": "prose_text",
                "page_number": None,
                "figure_id": None,
                "extraction_method": "pymupdf_text_layer",
                "text": prose_value,
            }
        )

    for index, raw_segment in enumerate(diagram_segments):
        if not isinstance(raw_segment, Mapping):
            continue
        text_value = _normalise_optional_text(raw_segment.get("text"))
        if not text_value:
            continue
        prepared_segments.append(
            {
                "segment_id": _coerce_segment_id(
                    raw_segment.get("segment_id"),
                    fallback_prefix="diagram_segment",
                    index=index,
                ),
                "source_type": "diagram_segment",
                "page_number": (
                    raw_segment.get("page_number")
                    if isinstance(raw_segment.get("page_number"), int)
                    else None
                ),
                "figure_id": _normalise_optional_text(raw_segment.get("figure_id")),
                "extraction_method": _normalise_optional_text(
                    raw_segment.get("extraction_method")
                ),
                "text": text_value,
            }
        )

    return prepared_segments, prose_segment_ids


def _build_segment_lookup(
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    lookup: dict[str, Mapping[str, Any]] = {}
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        segment_id = _normalise_optional_text(segment.get("segment_id"))
        if not segment_id:
            continue
        lookup[segment_id] = segment
    return lookup


def _build_candidate_provenance(
    segment_refs: Sequence[str],
    *,
    segment_lookup: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool, bool]:
    provenance: list[dict[str, Any]] = []
    has_prose = False
    has_diagram = False
    for segment_ref in segment_refs:
        segment = segment_lookup.get(segment_ref)
        if not isinstance(segment, Mapping):
            continue
        source_type = _normalise_optional_text(segment.get("source_type")) or "unknown"
        provenance.append(
            {
                "source": source_type,
                "segment_id": segment_ref,
                "page_number": (
                    segment.get("page_number")
                    if isinstance(segment.get("page_number"), int)
                    else None
                ),
                "figure_id": _normalise_optional_text(segment.get("figure_id")),
                "extraction_method": _normalise_optional_text(
                    segment.get("extraction_method")
                ),
            }
        )
        if source_type == "prose_text":
            has_prose = True
        elif source_type == "diagram_segment":
            has_diagram = True
    return provenance, has_prose, has_diagram


def _normalise_authoritative_organisation_candidate(
    raw_candidate: Mapping[str, Any],
    *,
    segment_lookup: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    name = _normalise_optional_text(raw_candidate.get("name"))
    if not name:
        return None
    segment_refs = [
        item.strip()
        for item in (raw_candidate.get("segment_refs") or [])
        if isinstance(item, str) and item.strip()
    ]
    provenance, has_prose, has_diagram = _build_candidate_provenance(
        segment_refs,
        segment_lookup=segment_lookup,
    )
    if not provenance:
        return None
    return {
        "name": name,
        "segment_refs": list(segment_refs),
        "evidence_excerpt": _normalise_optional_text(
            raw_candidate.get("evidence_excerpt")
        ),
        "provenance": provenance,
        "_has_prose": has_prose,
        "_has_diagram": has_diagram,
    }


def _normalise_authoritative_relationship_candidate(
    raw_candidate: Mapping[str, Any],
    *,
    segment_lookup: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    source_name = _normalise_optional_text(raw_candidate.get("source_name"))
    target_name = _normalise_optional_text(raw_candidate.get("target_name"))
    relation_hint = _normalise_optional_text(raw_candidate.get("relation_hint"))
    if not source_name or not target_name or not relation_hint:
        return None
    segment_refs = [
        item.strip()
        for item in (raw_candidate.get("segment_refs") or [])
        if isinstance(item, str) and item.strip()
    ]
    provenance, _has_prose, has_diagram = _build_candidate_provenance(
        segment_refs,
        segment_lookup=segment_lookup,
    )
    if not provenance:
        return None
    return {
        "source_name": source_name,
        "target_name": target_name,
        "relation_hint": relation_hint,
        "segment_refs": list(segment_refs),
        "evidence_excerpt": _normalise_optional_text(
            raw_candidate.get("evidence_excerpt")
        ),
        "provenance": provenance,
        "_has_diagram": has_diagram,
    }


def is_pdf_file(*, content_type: str | None, filename: str | None) -> bool:
    content_type_clean = _normalise_optional_text(content_type)
    if isinstance(content_type_clean, str) and content_type_clean.lower().startswith(
        "application/pdf"
    ):
        return True
    filename_clean = _normalise_optional_text(filename)
    return bool(isinstance(filename_clean, str) and filename_clean.lower().endswith(".pdf"))


def extract_organisation_candidates_from_text(
    *,
    text: str | None,
    source: str,
    page_number: int | None = None,
    figure_id: str | None = None,
    extraction_method: str | None = None,
    max_candidates: int = 40,
) -> list[dict[str, Any]]:
    """Extract candidate organisations via the authoritative interpretation path."""

    source_text = _normalise_optional_text(text)
    if not source_text:
        return []

    from .file_copy_diagram_interpretation_vontology_service import (
        infer_file_copy_diagram_semantics,
    )

    source_type = source or ("prose_text" if source == "pdf_prose_text" else "diagram_segment")
    segment = {
        "segment_id": "single_segment_1",
        "source_type": source_type,
        "page_number": page_number,
        "figure_id": figure_id,
        "extraction_method": extraction_method,
        "text": source_text,
    }
    payload, _diagnostics = infer_file_copy_diagram_semantics(
        segments=[segment],
        max_candidates=max_candidates,
    )
    segment_lookup = _build_segment_lookup([segment])
    rows: list[dict[str, Any]] = []
    for raw_candidate in payload.get("organisation_candidates") or []:
        if not isinstance(raw_candidate, Mapping):
            continue
        row = _normalise_authoritative_organisation_candidate(
            raw_candidate,
            segment_lookup=segment_lookup,
        )
        if row is not None:
            rows.append(row)
    return _merge_organisation_candidates(rows, max_candidates=max_candidates)


def extract_relationship_candidates_from_text(
    *,
    text: str | None,
    organisation_names: Sequence[str],
    page_number: int | None = None,
    figure_id: str | None = None,
    extraction_method: str | None = None,
    max_candidates: int = 40,
) -> list[dict[str, Any]]:
    """Extract relation candidates via the authoritative interpretation path."""

    source_text = _normalise_optional_text(text)
    if not source_text:
        return []

    from .file_copy_diagram_interpretation_vontology_service import (
        infer_file_copy_diagram_semantics,
    )

    segment = {
        "segment_id": "single_segment_1",
        "source_type": "diagram_segment",
        "page_number": page_number,
        "figure_id": figure_id,
        "extraction_method": extraction_method,
        "text": source_text,
    }
    payload, _diagnostics = infer_file_copy_diagram_semantics(
        segments=[segment],
        max_candidates=max_candidates,
        allowed_organisation_names=[
            item.strip()
            for item in organisation_names
            if isinstance(item, str) and item.strip()
        ],
    )
    segment_lookup = _build_segment_lookup([segment])
    rows: list[dict[str, Any]] = []
    for raw_candidate in payload.get("relationship_candidates") or []:
        if not isinstance(raw_candidate, Mapping):
            continue
        row = _normalise_authoritative_relationship_candidate(
            raw_candidate,
            segment_lookup=segment_lookup,
        )
        if row is not None:
            rows.append(row)
    return _merge_relationship_candidates(rows, max_candidates=max_candidates)


def _merge_organisation_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_name = row.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        key = raw_name.strip().casefold()
        target = merged.get(key)
        provenance = list(row.get("provenance") or [])
        evidence_excerpt = _normalise_optional_text(row.get("evidence_excerpt"))
        segment_refs = [
            item.strip()
            for item in (row.get("segment_refs") or [])
            if isinstance(item, str) and item.strip()
        ]
        if target is None:
            target = {
                "name": raw_name.strip(),
                "segment_refs": set(segment_refs),
                "evidence_excerpts": [evidence_excerpt] if evidence_excerpt else [],
                "provenance": [
                    dict(item)
                    for item in provenance
                    if isinstance(item, Mapping)
                ],
            }
            merged[key] = target
            continue
        refs = target.get("segment_refs")
        if not isinstance(refs, set):
            refs = set()
        refs.update(segment_refs)
        target["segment_refs"] = refs
        excerpts = target.get("evidence_excerpts")
        if not isinstance(excerpts, list):
            excerpts = []
        if evidence_excerpt and evidence_excerpt not in excerpts:
            excerpts.append(evidence_excerpt)
        target["evidence_excerpts"] = excerpts
        provenance_entries = target.get("provenance")
        if not isinstance(provenance_entries, list):
            provenance_entries = []
        for item in provenance:
            if isinstance(item, Mapping):
                provenance_entries.append(dict(item))
        target["provenance"] = provenance_entries

    sorted_rows = sorted(
        merged.values(),
        key=lambda item: (
            -len(list(item.get("provenance") or [])),
            str(item.get("name") or ""),
        ),
    )
    sorted_rows = sorted_rows[: max(1, int(max_candidates))]
    output: list[dict[str, Any]] = []
    for row in sorted_rows:
        provenance_entries = list(row.get("provenance") or [])
        evidence_excerpts = [
            str(item)
            for item in (row.get("evidence_excerpts") or [])
            if isinstance(item, str) and item.strip()
        ]
        output.append(
            {
                "name": row.get("name"),
                "evidence_count": len(provenance_entries),
                "evidence_excerpts": evidence_excerpts,
                "provenance": provenance_entries,
            }
        )
    return output


def _merge_relationship_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        source_name = _normalise_optional_text(row.get("source_name"))
        target_name = _normalise_optional_text(row.get("target_name"))
        relation_hint = _normalise_optional_text(row.get("relation_hint"))
        if not source_name or not target_name or not relation_hint:
            continue
        key = (
            source_name.casefold(),
            target_name.casefold(),
            relation_hint.casefold(),
        )
        provenance = list(row.get("provenance") or [])
        evidence_excerpt = _normalise_optional_text(row.get("evidence_excerpt"))
        segment_refs = [
            item.strip()
            for item in (row.get("segment_refs") or [])
            if isinstance(item, str) and item.strip()
        ]
        target = merged.get(key)
        if target is None:
            merged[key] = {
                "source_name": source_name,
                "target_name": target_name,
                "relation_hint": relation_hint,
                "segment_refs": set(segment_refs),
                "evidence_excerpts": [evidence_excerpt] if evidence_excerpt else [],
                "provenance": [
                    dict(item)
                    for item in provenance
                    if isinstance(item, Mapping)
                ],
            }
            continue
        refs = target.get("segment_refs")
        if not isinstance(refs, set):
            refs = set()
        refs.update(segment_refs)
        target["segment_refs"] = refs
        excerpts = target.get("evidence_excerpts")
        if not isinstance(excerpts, list):
            excerpts = []
        if evidence_excerpt and evidence_excerpt not in excerpts:
            excerpts.append(evidence_excerpt)
        target["evidence_excerpts"] = excerpts
        provenance_entries = target.get("provenance")
        if not isinstance(provenance_entries, list):
            provenance_entries = []
        for item in provenance:
            if isinstance(item, Mapping):
                provenance_entries.append(dict(item))
        target["provenance"] = provenance_entries

    sorted_rows = sorted(
        merged.values(),
        key=lambda item: (
            -len(list(item.get("provenance") or [])),
            str(item.get("source_name") or ""),
            str(item.get("target_name") or ""),
            str(item.get("relation_hint") or ""),
        ),
    )
    sorted_rows = sorted_rows[: max(1, int(max_candidates))]
    output: list[dict[str, Any]] = []
    for row in sorted_rows:
        output.append(
            {
                "source_name": row.get("source_name"),
                "target_name": row.get("target_name"),
                "relation_hint": row.get("relation_hint"),
                "evidence_count": len(list(row.get("provenance") or [])),
                "evidence_excerpts": [
                    str(item)
                    for item in (row.get("evidence_excerpts") or [])
                    if isinstance(item, str) and item.strip()
                ],
                "provenance": list(row.get("provenance") or []),
            }
        )
    return output


def summarise_diagram_organisation_candidates(
    *,
    prose_text: str | None,
    diagram_segments: Sequence[Mapping[str, Any]],
    max_candidates: int = 40,
) -> dict[str, Any]:
    """Summarise authoritative organisation/relation candidates by provenance."""

    from .file_copy_diagram_interpretation_vontology_service import (
        infer_file_copy_diagram_semantics,
    )

    prepared_segments, prose_segment_ids = _prepare_diagram_interpretation_segments(
        prose_text=prose_text,
        diagram_segments=diagram_segments,
    )
    payload, diagnostics = infer_file_copy_diagram_semantics(
        segments=prepared_segments,
        max_candidates=max_candidates,
    )
    segment_lookup = _build_segment_lookup(prepared_segments)

    prose_rows: list[dict[str, Any]] = []
    diagram_rows: list[dict[str, Any]] = []
    relationship_rows: list[dict[str, Any]] = []

    for raw_candidate in payload.get("organisation_candidates") or []:
        if not isinstance(raw_candidate, Mapping):
            continue
        row = _normalise_authoritative_organisation_candidate(
            raw_candidate,
            segment_lookup=segment_lookup,
        )
        if row is None:
            continue
        if bool(row.get("_has_prose")):
            prose_rows.append(row)
        if bool(row.get("_has_diagram")):
            diagram_rows.append(row)

    for raw_candidate in payload.get("relationship_candidates") or []:
        if not isinstance(raw_candidate, Mapping):
            continue
        row = _normalise_authoritative_relationship_candidate(
            raw_candidate,
            segment_lookup=segment_lookup,
        )
        if row is None or not bool(row.get("_has_diagram")):
            continue
        relationship_rows.append(row)

    prose_candidates = _merge_organisation_candidates(prose_rows, max_candidates=max_candidates)
    diagram_candidates = _merge_organisation_candidates(diagram_rows, max_candidates=max_candidates)
    prose_names = {
        str(row.get("name")).casefold()
        for row in prose_candidates
        if isinstance(row.get("name"), str) and str(row.get("name")).strip()
    }
    diagram_only = [
        row
        for row in diagram_candidates
        if isinstance(row.get("name"), str) and row["name"].casefold() not in prose_names
    ]

    return {
        "prose_organisations": prose_candidates,
        "diagram_organisations": diagram_candidates,
        "diagram_only_organisations": diagram_only,
        "diagram_relationship_candidates": _merge_relationship_candidates(
            relationship_rows,
            max_candidates=max_candidates,
        ),
        "requires_human_confirmation": True,
        "authority_diagnostics": diagnostics,
        "prose_segment_ids": sorted(prose_segment_ids),
    }


def _extract_pdf_page_ocr_text(page: Any, *, dpi: int) -> dict[str, Any]:
    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except Exception as exc:
        return {
            "text": None,
            "method": "pdf_diagram_ocr_unavailable",
            "error": str(exc),
        }

    try:
        pixmap = page.get_pixmap(dpi=dpi)
        png_bytes = pixmap.tobytes("png")
        with Image.open(io.BytesIO(png_bytes)) as image:
            text = pytesseract.image_to_string(image).strip()
        return {
            "text": text or None,
            "method": "pdf_page_ocr",
            "error": None,
        }
    except Exception as exc:
        return {
            "text": None,
            "method": "pdf_page_ocr_failed",
            "error": str(exc),
        }


def extract_pdf_diagram_organisation_candidates(
    *,
    data_bytes: bytes,
    content_type: str | None,
    original_filename: str | None,
    prose_text: str | None,
    max_pages: int = 8,
    max_candidates: int = 40,
    ocr_dpi: int = 220,
) -> dict[str, Any]:
    """Extract organisation candidates from PDF diagram pages with provenance."""

    if not is_pdf_file(content_type=content_type, filename=original_filename):
        return {
            "available": False,
            "reason": "not_pdf",
            "requires_human_confirmation": True,
            "prose_organisations": [],
            "diagram_organisations": [],
            "diagram_only_organisations": [],
            "diagram_relationship_candidates": [],
            "page_summaries": [],
            "errors": [],
        }

    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as exc:
        return {
            "available": False,
            "reason": "pymupdf_unavailable",
            "requires_human_confirmation": True,
            "prose_organisations": [],
            "diagram_organisations": [],
            "diagram_only_organisations": [],
            "diagram_relationship_candidates": [],
            "page_summaries": [],
            "errors": [str(exc)],
        }

    page_summaries: list[dict[str, Any]] = []
    diagram_segments: list[dict[str, Any]] = []
    errors: list[str] = []
    pages_scanned = 0
    total_pages = 0

    try:
        with fitz.open(stream=bytes(data_bytes), filetype="pdf") as doc:
            total_pages = len(doc)
            pages_to_scan = max(1, min(int(max_pages), total_pages if total_pages > 0 else 1))
            for page_index in range(pages_to_scan):
                page = doc.load_page(page_index)
                pages_scanned += 1
                page_text = str(page.get_text("text") or "")
                image_count = 0
                try:
                    image_count = len(page.get_images(full=True))
                except Exception:
                    image_count = 0

                signals: list[str] = []
                if image_count > 0:
                    signals.append("embedded_images")
                if _DIAGRAM_KEYWORD_PATTERN.search(page_text):
                    signals.append("diagram_keywords")
                if _RELATION_CONNECTOR_PATTERN.search(page_text):
                    signals.append("connector_tokens")

                is_diagram_candidate = bool(signals)
                ocr_payload: dict[str, Any] = {
                    "text": None,
                    "method": "diagram_not_detected",
                    "error": None,
                }
                if is_diagram_candidate:
                    ocr_payload = _extract_pdf_page_ocr_text(page, dpi=max(120, int(ocr_dpi)))
                    ocr_text = _normalise_optional_text(ocr_payload.get("text"))
                    if ocr_text:
                        diagram_segments.append(
                            {
                                "text": ocr_text,
                                "page_number": page_index + 1,
                                "figure_id": f"page_{page_index + 1}_diagram_candidate",
                                "extraction_method": ocr_payload.get("method"),
                            }
                        )
                    elif isinstance(ocr_payload.get("error"), str):
                        errors.append(str(ocr_payload["error"]))

                page_summaries.append(
                    {
                        "page_number": page_index + 1,
                        "diagram_candidate": is_diagram_candidate,
                        "signals": signals,
                        "image_count": image_count,
                        "ocr_method": ocr_payload.get("method"),
                        "ocr_error": ocr_payload.get("error"),
                    }
                )
    except Exception as exc:
        errors.append(str(exc))

    summary = summarise_diagram_organisation_candidates(
        prose_text=prose_text,
        diagram_segments=diagram_segments,
        max_candidates=max_candidates,
    )
    summary.update(
        {
            "available": True,
            "method": "pymupdf_diagram_ocr",
            "page_summaries": page_summaries,
            "pages_scanned": pages_scanned,
            "total_pages": total_pages,
            "errors": errors,
        }
    )
    return summary


def is_image_file(*, content_type: str | None, filename: str | None) -> bool:
    content_type_clean = _normalise_optional_text(content_type)
    if isinstance(content_type_clean, str) and content_type_clean.lower().startswith(
        "image/"
    ):
        return True

    filename_clean = _normalise_optional_text(filename)
    if not filename_clean:
        return False
    lowered = filename_clean.lower()
    return lowered.endswith(
        (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".heic",
            ".heif",
        )
    )


def extract_image_metadata(data_bytes: bytes) -> dict[str, Any]:
    """Return lightweight technical metadata for an image byte payload."""

    try:
        from PIL import Image  # type: ignore[import-not-found]
    except Exception as exc:
        return {"available": False, "error": f"pillow_unavailable:{exc}"}

    try:
        with Image.open(io.BytesIO(bytes(data_bytes))) as image:
            image_for_palette = image.convert("RGB")
            # Quantise to keep palette extraction fast and deterministic.
            palette_image = image_for_palette.quantize(colors=8, method=2)
            palette_data: list[int] = []
            for pixel in palette_image.getdata():
                if isinstance(pixel, (int, float)):
                    palette_data.append(int(pixel))
            palette_counts = Counter(palette_data)
            palette = palette_image.getpalette() or []
            dominant_colours: list[list[int]] = []
            for index_raw, _count in palette_counts.most_common(3):
                if not isinstance(index_raw, (int, float)):
                    continue
                base = int(index_raw) * 3
                if base + 2 < len(palette):
                    dominant_colours.append(
                        [int(palette[base]), int(palette[base + 1]), int(palette[base + 2])]
                    )

            exif_available = False
            exif_tag_count = 0
            try:
                exif_payload = image.getexif()
                if exif_payload:
                    exif_available = True
                    exif_tag_count = len(exif_payload)
            except Exception:
                exif_available = False
                exif_tag_count = 0

            return {
                "available": True,
                "width": int(image.width),
                "height": int(image.height),
                "mode": str(image.mode),
                "format": str(image.format or "").upper() or None,
                "dominant_colours_rgb": dominant_colours,
                "exif_available": exif_available,
                "exif_tag_count": int(exif_tag_count),
            }
    except Exception as exc:
        return {"available": False, "error": f"image_metadata_failed:{exc}"}


def extract_image_ocr_text(data_bytes: bytes) -> dict[str, Any]:
    """Extract OCR text from image bytes using pytesseract when available."""

    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except Exception as exc:
        return {
            "text": None,
            "method": "image_ocr_unavailable",
            "error": str(exc),
        }

    try:
        with Image.open(io.BytesIO(bytes(data_bytes))) as image:
            text = pytesseract.image_to_string(image).strip()
        return {
            "text": text or None,
            "method": "image_ocr",
            "error": None,
        }
    except Exception as exc:
        return {
            "text": None,
            "method": "image_ocr_failed",
            "error": str(exc),
        }


def _resolve_active_provider_and_model(
    *,
    model_override: str | None,
) -> tuple[str | None, str | None]:
    """Resolve active provider/model from settings with optional override."""

    try:
        from .settings_service import resolve_llm_setting
        from ..languagemodels.llm_interface import (
            resolve_openai_model_name,
            resolve_provider_from_model_concept,
        )

        active = resolve_llm_setting()
    except Exception:
        active = None
        resolve_openai_model_name = None  # type: ignore[assignment]
        resolve_provider_from_model_concept = None  # type: ignore[assignment]

    provider: str | None = None
    model: str | None = None
    if isinstance(active, dict):
        provider_raw = active.get("provider")
        if isinstance(provider_raw, str) and provider_raw.strip():
            provider = provider_raw.strip().lower()
        model_raw = active.get("model")
        if isinstance(model_raw, str) and model_raw.strip():
            model = model_raw.strip()
            if model.startswith("#V#") and callable(resolve_provider_from_model_concept):
                resolved = resolve_provider_from_model_concept(model)
                if isinstance(resolved, str) and resolved.strip():
                    provider = resolved.strip().lower()
            if provider == "openai" and callable(resolve_openai_model_name):
                resolved_model = resolve_openai_model_name(model)
                if isinstance(resolved_model, str) and resolved_model.strip():
                    model = resolved_model.strip()

    if isinstance(model_override, str) and model_override.strip():
        model = model_override.strip()

    return provider, model


def _external_image_model_execution_denial(
    *,
    provider: str,
    model: str,
) -> dict[str, Any] | None:
    """Return the image-service error shape when scoped eligibility is absent."""

    from ..languagemodels.llm_interface import (
        ModelExecutionEligibilityError,
        assert_model_execution_allowed,
    )

    try:
        assert_model_execution_allowed(provider=provider, model=model)
    except ModelExecutionEligibilityError as exc:
        return {
            "description": None,
            "method": f"{provider}_vision_not_enabled",
            "error": str(exc),
            "failure_kind": exc.failure_kind,
            "provider": provider,
            "model": exc.model or model,
        }
    return None


def _describe_image_with_openai(
    *,
    data_bytes: bytes,
    content_type: str | None,
    model: str | None,
    prompt: str,
) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except Exception as exc:
        return {
            "description": None,
            "method": "openai_vision_unavailable",
            "error": str(exc),
            "provider": "openai",
            "model": model,
        }

    api_key: str | None = None
    try:
        from .settings_service import get_openai_env_var

        env_var = get_openai_env_var()
        if isinstance(env_var, str) and env_var.strip():
            api_key = os.getenv(env_var.strip())
    except Exception:
        api_key = None

    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {
            "description": None,
            "method": "openai_vision_unavailable",
            "error": "missing_openai_api_key",
            "provider": "openai",
            "model": model,
        }

    target_model = model or DEFAULT_OPENAI_MODEL
    eligibility_denial = _external_image_model_execution_denial(
        provider="openai",
        model=target_model,
    )
    if eligibility_denial is not None:
        return eligibility_denial
    mime = _normalise_optional_text(content_type) or "image/png"
    image_b64 = base64.b64encode(bytes(data_bytes)).decode("ascii")
    data_url = f"data:{mime};base64,{image_b64}"

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=target_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }
            ],
            max_tokens=500,
            temperature=0.2,
        )
        content = response.choices[0].message.content
        description = content.strip() if isinstance(content, str) else None
        if not description:
            return {
                "description": None,
                "method": "openai_vision_failed",
                "error": "empty_response",
                "provider": "openai",
                "model": target_model,
            }
        return {
            "description": description,
            "method": "openai_vision",
            "error": None,
            "provider": "openai",
            "model": target_model,
        }
    except Exception as exc:
        return {
            "description": None,
            "method": "openai_vision_failed",
            "error": str(exc),
            "provider": "openai",
            "model": target_model,
        }


def _describe_image_with_gemini(
    *,
    data_bytes: bytes,
    content_type: str | None,
    model: str | None,
    prompt: str,
) -> dict[str, Any]:
    try:
        from google import genai  # type: ignore
    except Exception as exc:
        return {
            "description": None,
            "method": "gemini_vision_unavailable",
            "error": str(exc),
            "provider": "gemini",
            "model": model,
        }

    api_key = get_gemini_api_key()
    if not api_key:
        return {
            "description": None,
            "method": "gemini_vision_unavailable",
            "error": "missing_gemini_api_key",
            "provider": "gemini",
            "model": model,
        }

    target_model = model or "gemini-2.0-flash"
    eligibility_denial = _external_image_model_execution_denial(
        provider="gemini",
        model=target_model,
    )
    if eligibility_denial is not None:
        return eligibility_denial
    mime = _normalise_optional_text(content_type) or "image/png"
    try:
        genai.configure(api_key=api_key)  # type: ignore[attr-defined]
        model_instance = genai.GenerativeModel(target_model)  # type: ignore[attr-defined]
        response = model_instance.generate_content(
            [prompt, {"mime_type": mime, "data": bytes(data_bytes)}]
        )
        response_text = getattr(response, "text", None)
        description = response_text.strip() if isinstance(response_text, str) else None
        if not description:
            return {
                "description": None,
                "method": "gemini_vision_failed",
                "error": "empty_response",
                "provider": "gemini",
                "model": target_model,
            }
        return {
            "description": description,
            "method": "gemini_vision",
            "error": None,
            "provider": "gemini",
            "model": target_model,
        }
    except Exception as exc:
        return {
            "description": None,
            "method": "gemini_vision_failed",
            "error": str(exc),
            "provider": "gemini",
            "model": target_model,
        }


def describe_image_semantics(
    *,
    data_bytes: bytes,
    content_type: str | None,
    model_override: str | None = None,
    prompt_override: str | None = None,
) -> dict[str, Any]:
    """Describe image semantics using the active multimodal-capable provider."""

    provider, model = _resolve_active_provider_and_model(model_override=model_override)
    prompt = (
        _normalise_optional_text(prompt_override)
        or (
            "Describe this image in 2-5 concise sentences using New Zealand English. "
            "Include salient entities, likely setting, visible text, and whether people "
            "or buildings are present. Do not identify real people."
        )
    )

    if provider == "openai":
        return _describe_image_with_openai(
            data_bytes=data_bytes,
            content_type=content_type,
            model=model,
            prompt=prompt,
        )
    if provider == "gemini":
        return _describe_image_with_gemini(
            data_bytes=data_bytes,
            content_type=content_type,
            model=model,
            prompt=prompt,
        )

    return {
        "description": None,
        "method": "semantic_vision_not_configured",
        "error": (
            f"active_provider_not_supported:{provider}"
            if provider
            else "active_provider_not_configured"
        ),
        "provider": provider,
        "model": model,
    }


def _infer_subject_tags(*, semantic_description: str | None, ocr_text: str | None) -> list[str]:
    haystack_parts = []
    if isinstance(semantic_description, str) and semantic_description.strip():
        haystack_parts.append(semantic_description.strip().lower())
    if isinstance(ocr_text, str) and ocr_text.strip():
        haystack_parts.append(ocr_text.strip().lower())
    haystack = "\n".join(haystack_parts)
    if not haystack:
        return []

    tags: list[str] = []
    if re.search(r"\b(face|person|portrait|selfie|people)\b", haystack):
        tags.append("face_or_person")
    if re.search(
        r"\b(building|architecture|office|tower|house|campus|street|facade|fa[cç]ade)\b",
        haystack,
    ):
        tags.append("building_or_structure")
    if re.search(r"\b(screenshot|ui|interface|menu|window|table|form)\b", haystack):
        tags.append("screenshot_or_interface")
    if len(re.findall(r"[A-Za-z0-9]", haystack)) > 80:
        tags.append("text_heavy")
    return tags


def build_image_interpretation(
    *,
    data_bytes: bytes,
    content_type: str | None,
    original_filename: str | None,
    include_semantic_description: bool,
    model_override: str | None = None,
    prompt_override: str | None = None,
) -> dict[str, Any]:
    metadata = extract_image_metadata(data_bytes)
    ocr = extract_image_ocr_text(data_bytes)
    semantic = (
        describe_image_semantics(
            data_bytes=data_bytes,
            content_type=content_type,
            model_override=model_override,
            prompt_override=prompt_override,
        )
        if include_semantic_description
        else {
            "description": None,
            "method": "semantic_disabled",
            "error": None,
            "provider": None,
            "model": model_override,
        }
    )

    ocr_text = _normalise_optional_text(ocr.get("text"))
    semantic_description = _normalise_optional_text(semantic.get("description"))

    width = metadata.get("width")
    height = metadata.get("height")
    format_name = metadata.get("format")
    metadata_bits = []
    if isinstance(width, int) and isinstance(height, int):
        metadata_bits.append(f"{width}x{height}")
    if isinstance(format_name, str) and format_name.strip():
        metadata_bits.append(format_name.strip())
    metadata_suffix = f" ({', '.join(metadata_bits)})" if metadata_bits else ""

    if semantic_description:
        description = semantic_description
    elif ocr_text:
        preview = ocr_text[:220] + ("..." if len(ocr_text) > 220 else "")
        description = f"Image with readable text{metadata_suffix}: {preview}"
    else:
        filename_clean = _normalise_optional_text(original_filename) or "uploaded image"
        description = f"{filename_clean.capitalize()}{metadata_suffix}"

    subject_tags = _infer_subject_tags(
        semantic_description=semantic_description,
        ocr_text=ocr_text,
    )

    return {
        "kind": "image",
        "interpreted_at": _utc_now_iso(),
        "description": description,
        "subject_tags": subject_tags,
        "content_text": ocr_text,
        "content_length": len(ocr_text) if isinstance(ocr_text, str) else 0,
        "image_metadata": metadata,
        "ocr": ocr,
        "semantic": semantic,
    }


def build_document_interpretation(
    *,
    extracted_text: str | None,
    content_type: str | None,
    original_filename: str | None,
) -> dict[str, Any]:
    text = _normalise_optional_text(extracted_text)
    if text:
        preview = text[:260] + ("..." if len(text) > 260 else "")
        description = f"Document text extracted: {preview}"
    else:
        filename_clean = _normalise_optional_text(original_filename) or "uploaded file"
        content_type_clean = _normalise_optional_text(content_type)
        if content_type_clean:
            description = f"{filename_clean} ({content_type_clean}) uploaded"
        else:
            description = f"{filename_clean} uploaded"

    return {
        "kind": "document",
        "interpreted_at": _utc_now_iso(),
        "description": description,
        "subject_tags": ["document"],
        "content_text": text,
        "content_length": len(text) if isinstance(text, str) else 0,
    }
