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

_ORGANISATION_SUFFIX_PATTERN = re.compile(
    r"\b("
    r"(?:[A-Z][A-Za-z0-9&'().,\-]*(?:[ \t]+[A-Z][A-Za-z0-9&'().,\-]*){0,8})[ \t]+"
    r"(?:"
    r"University|Institute|Organisation|Organization|Agency|Council|Ministry|"
    r"Department|Centre|Center|Committee|Commission|Foundation|Association|"
    r"Alliance|Consortium|Laboratory|Laboratories|Lab|School|Company|"
    r"Corporation|Limited|Ltd|Inc|LLC|Group|Office|Authority|Bank|Society|"
    r"Hospital|College|Press|Secretariat|Trust"
    r")"
    r")\b"
)
_ORGANISATION_PREFIX_PATTERN = re.compile(
    r"\b("
    r"(?:"
    r"University|Institute|Organisation|Organization|Agency|Council|Ministry|"
    r"Department|Centre|Center|Committee|Commission|Foundation|Association|"
    r"Alliance|Consortium|Laboratory|Laboratories|Lab|School|Company|"
    r"Corporation|Bank|Society|Hospital|College|Office|Authority|Press|Secretariat|Trust"
    r")[ \t]+of[ \t]+"
    r"(?:[A-Z][A-Za-z0-9&'().,\-]*(?:[ \t]+[A-Z][A-Za-z0-9&'().,\-]*){0,8})"
    r")\b"
)
_ALL_CAPS_ORG_PATTERN = re.compile(r"\b[A-Z][A-Z0-9&.\-]{1,15}\b")
_DIAGRAM_KEYWORD_PATTERN = re.compile(
    r"\b(diagram|ecosystem|governance|network|stakeholder|consortium|"
    r"alliance|architecture|workflow|pipeline|flow|map|chart)\b",
    re.IGNORECASE,
)
_RELATION_CONNECTOR_PATTERN = re.compile(r"(?:->|=>|→|↔|<->|--|—|-)")
_ORGANISATION_STOPWORDS = frozenset(
    {
        "AND",
        "OR",
        "FOR",
        "THE",
        "WITH",
        "FROM",
        "THIS",
        "THAT",
        "FIGURE",
        "TABLE",
        "DATA",
        "MODEL",
        "SYSTEM",
        "INPUT",
        "OUTPUT",
        "OCR",
        "PDF",
        "API",
        "HTTP",
    }
)


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
    """Extract candidate organisation names from text with provenance metadata."""

    source_text = _normalise_optional_text(text)
    if not source_text:
        return []

    candidate_map: dict[str, dict[str, Any]] = {}

    def _register(raw_name: str, *, rule: str, base_confidence: float) -> None:
        name = _normalise_candidate_name(raw_name)
        if not name or len(name) < 3:
            return
        upper_name = name.upper()
        if upper_name in _ORGANISATION_STOPWORDS:
            return

        key = name.casefold()
        row = candidate_map.get(key)
        if row is None:
            row = {
                "name": name,
                "confidence": float(base_confidence),
                "evidence_count": 1,
                "evidence_rules": {rule},
            }
            candidate_map[key] = row
        else:
            row["confidence"] = max(float(row.get("confidence", 0.0)), base_confidence)
            row["evidence_count"] = int(row.get("evidence_count", 0)) + 1
            rules = row.get("evidence_rules")
            if not isinstance(rules, set):
                rules = set()
            rules.add(rule)
            row["evidence_rules"] = rules

    for match in _ORGANISATION_PREFIX_PATTERN.finditer(source_text):
        _register(match.group(1), rule="org_prefix", base_confidence=0.8)

    for match in _ORGANISATION_SUFFIX_PATTERN.finditer(source_text):
        _register(match.group(1), rule="org_suffix", base_confidence=0.82)

    for match in _ALL_CAPS_ORG_PATTERN.finditer(source_text):
        token = match.group(0).strip()
        if len(token) < 3:
            continue
        if token in _ORGANISATION_STOPWORDS:
            continue
        _register(token, rule="all_caps", base_confidence=0.55)

    rows = sorted(
        candidate_map.values(),
        key=lambda item: (-float(item.get("confidence", 0.0)), str(item.get("name", ""))),
    )
    rows = rows[: max(1, int(max_candidates))]

    extracted_at = _utc_now_iso()
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append(
            {
                "name": row["name"],
                "confidence": round(float(row["confidence"]), 3),
                "evidence_count": int(row["evidence_count"]),
                "evidence_rules": sorted(str(rule) for rule in (row.get("evidence_rules") or [])),
                "provenance": {
                    "source": source,
                    "page_number": page_number,
                    "figure_id": figure_id,
                    "extraction_method": extraction_method,
                    "extracted_at": extracted_at,
                },
            }
        )
    return output


def extract_relationship_candidates_from_text(
    *,
    text: str | None,
    organisation_names: Sequence[str],
    page_number: int | None = None,
    figure_id: str | None = None,
    extraction_method: str | None = None,
    max_candidates: int = 40,
) -> list[dict[str, Any]]:
    """Extract lightweight relation candidates from diagram-like connector text."""

    source_text = _normalise_optional_text(text)
    if not source_text:
        return []
    names = [item.strip() for item in organisation_names if isinstance(item, str) and item.strip()]
    if len(names) < 2:
        return []

    candidate_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int | None, str | None]] = set()
    lowered_pairs = [(name, name.casefold()) for name in names]

    for line in source_text.splitlines():
        cleaned_line = _normalise_whitespace(line)
        if not cleaned_line:
            continue
        connector_match = _RELATION_CONNECTOR_PATTERN.search(cleaned_line)
        if connector_match is None:
            continue
        connector = connector_match.group(0)
        relation_hint = "directed_link" if connector in {"->", "=>", "→"} else "association"

        hits: list[tuple[int, str]] = []
        lowered_line = cleaned_line.casefold()
        for original_name, folded_name in lowered_pairs:
            idx = lowered_line.find(folded_name)
            if idx >= 0:
                hits.append((idx, original_name))
        if len(hits) < 2:
            continue
        hits.sort(key=lambda item: item[0])

        for index in range(len(hits) - 1):
            left = hits[index][1]
            right = hits[index + 1][1]
            key = (left.casefold(), right.casefold(), relation_hint, page_number, figure_id)
            if key in seen:
                continue
            seen.add(key)
            candidate_rows.append(
                {
                    "source_name": left,
                    "target_name": right,
                    "relation_hint": relation_hint,
                    "confidence": 0.58 if relation_hint == "directed_link" else 0.5,
                    "provenance": {
                        "source": "pdf_diagram_relation_candidate",
                        "page_number": page_number,
                        "figure_id": figure_id,
                        "extraction_method": extraction_method,
                        "connector": connector,
                        "line_excerpt": cleaned_line[:260],
                    },
                }
            )

    return candidate_rows[: max(1, int(max_candidates))]


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
        provenance = row.get("provenance")
        if target is None:
            target = {
                "name": raw_name.strip(),
                "confidence": float(row.get("confidence") or 0.0),
                "evidence_count": int(row.get("evidence_count") or 0),
                "evidence_rules": set(row.get("evidence_rules") or []),
                "provenance": [dict(provenance)] if isinstance(provenance, Mapping) else [],
            }
            merged[key] = target
            continue
        target["confidence"] = max(
            float(target.get("confidence") or 0.0),
            float(row.get("confidence") or 0.0),
        )
        target["evidence_count"] = int(target.get("evidence_count") or 0) + int(
            row.get("evidence_count") or 0
        )
        rules = target.get("evidence_rules")
        if not isinstance(rules, set):
            rules = set()
        rules.update(row.get("evidence_rules") or [])
        target["evidence_rules"] = rules
        if isinstance(provenance, Mapping):
            provenance_entries = target.get("provenance")
            if not isinstance(provenance_entries, list):
                provenance_entries = []
            provenance_entries.append(dict(provenance))
            target["provenance"] = provenance_entries

    sorted_rows = sorted(
        merged.values(),
        key=lambda item: (-float(item.get("confidence") or 0.0), str(item.get("name") or "")),
    )
    sorted_rows = sorted_rows[: max(1, int(max_candidates))]
    output: list[dict[str, Any]] = []
    for row in sorted_rows:
        output.append(
            {
                "name": row.get("name"),
                "confidence": round(float(row.get("confidence") or 0.0), 3),
                "evidence_count": int(row.get("evidence_count") or 0),
                "evidence_rules": sorted(str(rule) for rule in (row.get("evidence_rules") or [])),
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
    """Summarise prose vs diagram organisation candidates with provenance separation."""

    prose_rows = extract_organisation_candidates_from_text(
        text=prose_text,
        source="pdf_prose_text",
        extraction_method="pymupdf_text_layer",
        max_candidates=max_candidates,
    )
    diagram_rows: list[dict[str, Any]] = []
    relationship_rows: list[dict[str, Any]] = []

    for segment in diagram_segments:
        text_value = segment.get("text")
        if not isinstance(text_value, str) or not text_value.strip():
            continue
        page_number_raw = segment.get("page_number")
        page_number = int(page_number_raw) if isinstance(page_number_raw, int) else None
        figure_id = (
            str(segment.get("figure_id")).strip()
            if isinstance(segment.get("figure_id"), str) and str(segment.get("figure_id")).strip()
            else None
        )
        extraction_method = (
            str(segment.get("extraction_method")).strip()
            if isinstance(segment.get("extraction_method"), str)
            and str(segment.get("extraction_method")).strip()
            else None
        )
        page_candidates = extract_organisation_candidates_from_text(
            text=text_value,
            source="pdf_diagram_segment",
            page_number=page_number,
            figure_id=figure_id,
            extraction_method=extraction_method,
            max_candidates=max_candidates,
        )
        diagram_rows.extend(page_candidates)
        relationship_rows.extend(
            extract_relationship_candidates_from_text(
                text=text_value,
                organisation_names=[str(row.get("name")) for row in page_candidates],
                page_number=page_number,
                figure_id=figure_id,
                extraction_method=extraction_method,
                max_candidates=max_candidates,
            )
        )

    prose_candidates = _merge_organisation_candidates(
        prose_rows,
        max_candidates=max_candidates,
    )
    diagram_candidates = _merge_organisation_candidates(
        diagram_rows,
        max_candidates=max_candidates,
    )
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
        "diagram_relationship_candidates": relationship_rows[: max(1, int(max_candidates))],
        "requires_human_confirmation": True,
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

    target_model = model or "gpt-4.1-mini"
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

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {
            "description": None,
            "method": "gemini_vision_unavailable",
            "error": "missing_gemini_api_key",
            "provider": "gemini",
            "model": model,
        }

    target_model = model or "gemini-2.0-flash"
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
