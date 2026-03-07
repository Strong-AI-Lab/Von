"""Authoritative file-copy typing helpers.

This module centralises reusable file-typing behaviour so upload workflows,
interpretation pathways, and workflow discovery all reason over the same
persisted taxonomy instead of repeating filename/MIME heuristics ad hoc.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback
from .computer_file_copy_service import (
    build_file_copy_artifact_record,
    ensure_specific_computer_file_copy_type_exists,
)
from .relationship_write_service import add_relationship
from .text_value_service import upsert_singleton_text_relation

logger = logging.getLogger(__name__)

FILE_COPY_TYPING_PREDICATE = "#V#has_file_copy_typing_json"
FILE_COPY_TYPING_SCHEMA_VERSION = "file_copy_typing.v1"

_ARXIV_FILENAME_RE = re.compile(r"(?<!\d)\d{4}\.\d{4,5}(?:v\d+)?", re.IGNORECASE)
_SCHOLARLY_TOKEN_RE = re.compile(
    r"\b(arxiv|paper|preprint|manuscript|journal|conference|doi)\b",
    re.IGNORECASE,
)
_CV_TOKEN_RE = re.compile(
    r"\b(cv|resume|résumé|curriculum[\s\-_]*vitae)\b",
    re.IGNORECASE,
)
_BUSINESS_CARD_TOKEN_RE = re.compile(
    r"\b(business[\s\-_]*card|biz[\s\-_]*card|contact[\s\-_]*card)\b",
    re.IGNORECASE,
)
_MEETING_TOKEN_RE = re.compile(
    r"\b(meeting|transcript|minutes|agenda|calendar)\b",
    re.IGNORECASE,
)
_IMAGE_EXTENSION_RE = re.compile(
    r"\.(png|jpg|jpeg|webp|gif|bmp|tif|tiff|heic|heif)$",
    re.IGNORECASE,
)

_DOCX_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }
)
_PPTX_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    }
)
_XLSX_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }
)
_HTML_MIME_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})
_MARKDOWN_MIME_TYPES: frozenset[str] = frozenset(
    {"text/markdown", "text/x-markdown"}
)
_CSV_MIME_TYPES: frozenset[str] = frozenset({"text/csv", "application/csv"})
_LATEX_MIME_TYPES: frozenset[str] = frozenset({"text/x-tex", "application/x-tex"})
_ODT_MIME_TYPES: frozenset[str] = frozenset(
    {"application/vnd.oasis.opendocument.text"}
)
_RTF_MIME_TYPES: frozenset[str] = frozenset({"application/rtf", "text/rtf"})
_EML_MIME_TYPES: frozenset[str] = frozenset({"message/rfc822"})
_GENERIC_BINARY_MIME_TYPES: frozenset[str] = frozenset(
    {"application/octet-stream", "binary/octet-stream"}
)

_TYPE_BLUEPRINTS: dict[str, dict[str, Any]] = {
    "#V#pdf_computer_file_copy": {
        "name": "PDF file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode a PDF document.",
        "system_tags": ["file", "pdf"],
    },
    "#V#msword_docx_computer_file_copy": {
        "name": "Microsoft Word document file copy",
        "parent": "#V#computer_file_copy",
        "description": (
            "A computer file copy whose stored bytes encode a Microsoft Word "
            "document, typically DOCX."
        ),
        "system_tags": ["file", "docx", "word"],
    },
    "#V#powerpoint_pptx_computer_file_copy": {
        "name": "PowerPoint presentation file copy",
        "parent": "#V#computer_file_copy",
        "description": (
            "A computer file copy whose stored bytes encode a PowerPoint "
            "presentation, typically PPTX."
        ),
        "system_tags": ["file", "pptx", "presentation"],
    },
    "#V#spreadsheet_xlsx_computer_file_copy": {
        "name": "Spreadsheet file copy",
        "parent": "#V#computer_file_copy",
        "description": (
            "A computer file copy whose stored bytes encode a spreadsheet, "
            "typically XLSX."
        ),
        "system_tags": ["file", "spreadsheet", "xlsx"],
    },
    "#V#html_computer_file_copy": {
        "name": "HTML file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode HTML markup.",
        "system_tags": ["file", "html", "web"],
    },
    "#V#markdown_computer_file_copy": {
        "name": "Markdown file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode Markdown text.",
        "system_tags": ["file", "markdown", "text"],
    },
    "#V#csv_computer_file_copy": {
        "name": "CSV file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode comma-separated values.",
        "system_tags": ["file", "csv", "tabular"],
    },
    "#V#latex_computer_file_copy": {
        "name": "LaTeX file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode LaTeX source.",
        "system_tags": ["file", "latex", "text"],
    },
    "#V#odt_computer_file_copy": {
        "name": "OpenDocument text file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode an ODT document.",
        "system_tags": ["file", "odt", "document"],
    },
    "#V#rtf_computer_file_copy": {
        "name": "Rich Text Format file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode RTF content.",
        "system_tags": ["file", "rtf", "text"],
    },
    "#V#email_message_file_copy": {
        "name": "Email message file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode an email message.",
        "system_tags": ["file", "email", "eml"],
    },
    "#V#image_computer_file_copy": {
        "name": "Image file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode an image.",
        "system_tags": ["file", "image"],
    },
    "#V#plain_text_computer_file_copy": {
        "name": "Plain-text file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy whose stored bytes encode plain text.",
        "system_tags": ["file", "text"],
    },
    "#V#scholarly_paper_file_copy": {
        "name": "Scholarly paper file copy",
        "parent": "#V#computer_file_copy",
        "description": (
            "A computer file copy that most likely contains a scholarly paper "
            "or preprint."
        ),
        "system_tags": ["file", "scholarly", "paper"],
    },
    "#V#curriculum_vitae_file_copy": {
        "name": "Curriculum vitae file copy",
        "parent": "#V#computer_file_copy",
        "description": (
            "A computer file copy that most likely contains a curriculum vitae "
            "or resume."
        ),
        "system_tags": ["file", "cv", "resume"],
    },
    "#V#business_card_file_copy": {
        "name": "Business card file copy",
        "parent": "#V#computer_file_copy",
        "description": "A computer file copy that most likely contains a business card.",
        "system_tags": ["file", "business_card", "contact"],
    },
    "#V#meeting_transcript_file_copy": {
        "name": "Meeting transcript file copy",
        "parent": "#V#computer_file_copy",
        "description": (
            "A computer file copy that most likely contains meeting transcript, "
            "minutes, agenda, or calendar material."
        ),
        "system_tags": ["file", "meeting", "transcript"],
    },
}

_FORMAT_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "pdf_mime_or_extension",
        "type_concept_id": "#V#pdf_computer_file_copy",
        "mime_types": ("application/pdf",),
        "extensions": (".pdf",),
    },
    {
        "rule_id": "docx_mime_or_extension",
        "type_concept_id": "#V#msword_docx_computer_file_copy",
        "mime_types": tuple(_DOCX_MIME_TYPES),
        "extensions": (".docx", ".doc"),
    },
    {
        "rule_id": "pptx_mime_or_extension",
        "type_concept_id": "#V#powerpoint_pptx_computer_file_copy",
        "mime_types": tuple(_PPTX_MIME_TYPES),
        "extensions": (".pptx", ".ppt"),
    },
    {
        "rule_id": "xlsx_mime_or_extension",
        "type_concept_id": "#V#spreadsheet_xlsx_computer_file_copy",
        "mime_types": tuple(_XLSX_MIME_TYPES),
        "extensions": (".xlsx", ".xls"),
    },
    {
        "rule_id": "html_mime_or_extension",
        "type_concept_id": "#V#html_computer_file_copy",
        "mime_types": tuple(_HTML_MIME_TYPES),
        "extensions": (".html", ".htm"),
    },
    {
        "rule_id": "markdown_mime_or_extension",
        "type_concept_id": "#V#markdown_computer_file_copy",
        "mime_types": tuple(_MARKDOWN_MIME_TYPES),
        "extensions": (".md", ".markdown"),
    },
    {
        "rule_id": "csv_mime_or_extension",
        "type_concept_id": "#V#csv_computer_file_copy",
        "mime_types": tuple(_CSV_MIME_TYPES),
        "extensions": (".csv",),
    },
    {
        "rule_id": "latex_mime_or_extension",
        "type_concept_id": "#V#latex_computer_file_copy",
        "mime_types": tuple(_LATEX_MIME_TYPES),
        "extensions": (".tex",),
    },
    {
        "rule_id": "odt_mime_or_extension",
        "type_concept_id": "#V#odt_computer_file_copy",
        "mime_types": tuple(_ODT_MIME_TYPES),
        "extensions": (".odt",),
    },
    {
        "rule_id": "rtf_mime_or_extension",
        "type_concept_id": "#V#rtf_computer_file_copy",
        "mime_types": tuple(_RTF_MIME_TYPES),
        "extensions": (".rtf",),
    },
    {
        "rule_id": "eml_mime_or_extension",
        "type_concept_id": "#V#email_message_file_copy",
        "mime_types": tuple(_EML_MIME_TYPES),
        "extensions": (".eml",),
    },
)

_ROUTE_HINT_TO_TYPE_ID: dict[str, str] = {
    "scholarly": "#V#scholarly_paper_file_copy",
    "cv": "#V#curriculum_vitae_file_copy",
    "business_card": "#V#business_card_file_copy",
    "meeting": "#V#meeting_transcript_file_copy",
}


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
    token = content_type.split(";", 1)[0].strip().lower()
    return token or None


def _normalise_filename(value: Any) -> str:
    filename = _normalise_optional_text(value)
    return filename.lower() if isinstance(filename, str) else ""


def _normalise_filename_extension(value: Any) -> str | None:
    filename = _normalise_filename(value)
    if not filename:
        return None
    _base, extension = os.path.splitext(filename)
    return extension or None


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        token = _normalise_optional_text(value)
        if not token:
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(token)
    return ordered


def _slugify_type_fragment(value: str | None) -> str:
    cleaned = _normalise_optional_text(value)
    if not cleaned:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "_", cleaned.lower()).strip("_")
    return slug


def _labelify_type_fragment(value: str | None) -> str:
    cleaned = _normalise_optional_text(value)
    if not cleaned:
        return ""
    tokens = [
        token
        for token in re.split(r"[^A-Za-z0-9]+", cleaned)
        if isinstance(token, str) and token.strip()
    ]
    if not tokens:
        return ""
    acronyms = {
        "api",
        "csv",
        "css",
        "docx",
        "gif",
        "gz",
        "heic",
        "heif",
        "html",
        "jpeg",
        "jpg",
        "json",
        "md",
        "odt",
        "pdf",
        "png",
        "pptx",
        "py",
        "rtf",
        "sql",
        "svg",
        "tar",
        "tex",
        "ts",
        "txt",
        "xlsx",
        "xml",
        "yaml",
        "yml",
        "zip",
    }
    parts: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered in acronyms or len(lowered) <= 4:
            parts.append(lowered.upper())
        else:
            parts.append(lowered.capitalize())
    return " ".join(parts)


def _build_dynamic_content_type_descriptor(
    content_type_token: str | None,
) -> dict[str, Any] | None:
    token = _normalise_content_type_token(content_type_token)
    if not token or token in _GENERIC_BINARY_MIME_TYPES or "/" not in token:
        return None
    media_type, media_subtype = token.split("/", 1)
    media_type_slug = _slugify_type_fragment(media_type)
    media_subtype_slug = _slugify_type_fragment(media_subtype)
    if not media_type_slug or not media_subtype_slug:
        return None
    subtype_label = _labelify_type_fragment(media_subtype)
    concept_id = (
        f"#V#content_type_{media_type_slug}_{media_subtype_slug}_computer_file_copy"
    )
    return {
        "concept_id": concept_id,
        "name": f"{token} file copy",
        "parent": "#V#computer_file_copy",
        "description": (
            f'A computer file copy whose MIME content type is "{token}".'
        ),
        "system_tags": ["file", "mime", media_type_slug, media_subtype_slug],
        "matched_signals": ["mime_type"],
        "rule_id": "dynamic_content_type",
        "display_name": f"{subtype_label} file copy ({token})",
    }


def _build_dynamic_extension_descriptor(extension: str | None) -> dict[str, Any] | None:
    if not isinstance(extension, str) or not extension.strip():
        return None
    clean_extension = extension.strip().lower()
    if not clean_extension.startswith("."):
        clean_extension = f".{clean_extension}"
    extension_slug = _slugify_type_fragment(clean_extension.lstrip("."))
    if not extension_slug:
        return None
    extension_label = _labelify_type_fragment(clean_extension.lstrip("."))
    concept_id = f"#V#extension_{extension_slug}_computer_file_copy"
    return {
        "concept_id": concept_id,
        "name": f"{clean_extension} file copy",
        "parent": "#V#computer_file_copy",
        "description": (
            "A computer file copy identified by the filename extension "
            f'"{clean_extension}" when no more specific curated file type is available.'
        ),
        "system_tags": ["file", "extension", extension_slug],
        "matched_signals": ["filename_extension"],
        "rule_id": "dynamic_extension",
        "display_name": f"{extension_label} file copy ({clean_extension})",
    }


def _build_dynamic_format_descriptor(
    *,
    content_type_token: str | None,
    extension: str | None,
) -> dict[str, Any] | None:
    return _build_dynamic_content_type_descriptor(
        content_type_token
    ) or _build_dynamic_extension_descriptor(extension)


def _resolve_type_blueprint(
    *,
    type_concept_id: str,
    typing_result: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    blueprint = _TYPE_BLUEPRINTS.get(type_concept_id)
    if isinstance(blueprint, Mapping):
        return dict(blueprint)
    if not isinstance(typing_result, Mapping):
        return None
    content_descriptor = _build_dynamic_content_type_descriptor(
        _normalise_optional_text(typing_result.get("content_type_token"))
    )
    if (
        isinstance(content_descriptor, Mapping)
        and content_descriptor.get("concept_id") == type_concept_id
    ):
        return dict(content_descriptor)
    extension_descriptor = _build_dynamic_extension_descriptor(
        _normalise_optional_text(typing_result.get("filename_extension"))
    )
    if (
        isinstance(extension_descriptor, Mapping)
        and extension_descriptor.get("concept_id") == type_concept_id
    ):
        return dict(extension_descriptor)
    return None


def _resolve_format_type(
    *,
    content_type_token: str | None,
    extension: str | None,
    filename: str,
) -> tuple[str | None, list[str], list[str]]:
    if isinstance(content_type_token, str) and content_type_token.startswith("image/"):
        return "#V#image_computer_file_copy", ["mime_type"], ["image_mime_family"]
    if _IMAGE_EXTENSION_RE.search(filename):
        return "#V#image_computer_file_copy", ["filename_extension"], ["image_extension"]
    if isinstance(content_type_token, str) and content_type_token == "text/plain":
        return "#V#plain_text_computer_file_copy", ["mime_type"], ["plain_text_mime"]
    if extension == ".txt":
        return "#V#plain_text_computer_file_copy", ["filename_extension"], ["plain_text_extension"]

    for rule in _FORMAT_RULES:
        mime_types = {str(item).strip().lower() for item in rule.get("mime_types", ())}
        extensions = {str(item).strip().lower() for item in rule.get("extensions", ())}
        mime_match = (
            isinstance(content_type_token, str) and content_type_token in mime_types
        )
        extension_match = isinstance(extension, str) and extension in extensions
        if not (mime_match or extension_match):
            continue
        signals: list[str] = []
        if mime_match:
            signals.append("mime_type")
        if extension_match:
            signals.append("filename_extension")
        return str(rule.get("type_concept_id") or "").strip() or None, signals, [
            str(rule.get("rule_id") or "").strip()
        ]
    dynamic_descriptor = _build_dynamic_format_descriptor(
        content_type_token=content_type_token,
        extension=extension,
    )
    if isinstance(dynamic_descriptor, Mapping):
        return (
            str(dynamic_descriptor.get("concept_id") or "").strip() or None,
            list(dynamic_descriptor.get("matched_signals") or []),
            [str(dynamic_descriptor.get("rule_id") or "").strip()],
        )
    return None, [], []


def _score_route_hints(
    *,
    filename: str,
    content_type_token: str | None,
    size_bytes: int | None,
    format_type_concept_id: str | None,
) -> dict[str, float]:
    is_pdf = format_type_concept_id == "#V#pdf_computer_file_copy"
    is_image = format_type_concept_id == "#V#image_computer_file_copy"
    is_docx = format_type_concept_id == "#V#msword_docx_computer_file_copy"

    scholarly_score = 0.0
    if is_pdf:
        scholarly_score += 0.2
    if _ARXIV_FILENAME_RE.search(filename):
        scholarly_score += 0.65
    if _SCHOLARLY_TOKEN_RE.search(filename):
        scholarly_score += 0.2

    cv_score = 0.0
    if _CV_TOKEN_RE.search(filename):
        cv_score += 0.72
    if is_pdf:
        cv_score += 0.2
    if is_docx:
        cv_score += 0.15

    business_card_score = 0.0
    if _BUSINESS_CARD_TOKEN_RE.search(filename):
        business_card_score += 0.78
    if is_image:
        business_card_score += 0.16
    if isinstance(size_bytes, int) and size_bytes > 0 and size_bytes < 3_000_000:
        business_card_score += 0.05

    meeting_score = 0.0
    if _MEETING_TOKEN_RE.search(filename):
        meeting_score += 0.72
    if is_pdf or is_docx:
        meeting_score += 0.1
    if isinstance(content_type_token, str) and content_type_token in {"text/plain", "text/markdown"}:
        meeting_score += 0.08

    return {
        "scholarly": round(min(0.99, scholarly_score), 4),
        "cv": round(min(0.99, cv_score), 4),
        "business_card": round(min(0.99, business_card_score), 4),
        "meeting": round(min(0.99, meeting_score), 4),
    }


def _pick_route_hint(route_scores: Mapping[str, float]) -> tuple[str | None, float]:
    winner: str | None = None
    winner_score = 0.0
    for route_hint, score in route_scores.items():
        numeric_score = float(score or 0.0)
        if numeric_score > winner_score:
            winner = route_hint
            winner_score = numeric_score
    if winner is None or winner_score < 0.6:
        return None, round(winner_score, 4)
    return winner, round(winner_score, 4)


def infer_file_copy_typing(
    *,
    content_type: str | None,
    original_filename: str | None,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    """Infer reusable taxonomy and route hints for one file copy."""

    content_type_token = _normalise_content_type_token(content_type)
    filename = _normalise_filename(original_filename)
    extension = _normalise_filename_extension(original_filename)

    format_type_concept_id, matched_signals, matched_rule_ids = _resolve_format_type(
        content_type_token=content_type_token,
        extension=extension,
        filename=filename,
    )
    route_scores = _score_route_hints(
        filename=filename,
        content_type_token=content_type_token,
        size_bytes=size_bytes,
        format_type_concept_id=format_type_concept_id,
    )
    route_hint, route_confidence = _pick_route_hint(route_scores)
    semantic_type_concept_id = (
        _ROUTE_HINT_TO_TYPE_ID.get(route_hint) if isinstance(route_hint, str) else None
    )

    asserted_type_concept_ids = _dedupe_strings(
        [
            semantic_type_concept_id or "",
            format_type_concept_id or "",
        ]
    )
    primary_type_concept_id = (
        semantic_type_concept_id
        or format_type_concept_id
        or "#V#computer_file_copy"
    )

    return {
        "schema_version": FILE_COPY_TYPING_SCHEMA_VERSION,
        "determinable": bool(asserted_type_concept_ids),
        "primary_type_concept_id": primary_type_concept_id,
        "semantic_type_concept_id": semantic_type_concept_id,
        "format_type_concept_id": format_type_concept_id,
        "asserted_type_concept_ids": asserted_type_concept_ids,
        "route_hint": route_hint,
        "route_confidence": route_confidence,
        "route_scores": dict(route_scores),
        "matched_signals": list(matched_signals),
        "matched_rule_ids": list(
            _dedupe_strings([*(matched_rule_ids or ()), route_hint or ""])
        ),
        "content_type_token": content_type_token,
        "filename_extension": extension,
        "original_filename": original_filename,
        "size_bytes": size_bytes,
    }


def ensure_file_copy_typing_types_exist(
    typing_result: Mapping[str, Any],
    *,
    logger_override: logging.Logger | None = None,
) -> list[str]:
    """Ensure all typed file-copy concepts referenced by the result exist."""

    created_or_known: list[str] = []
    for type_concept_id in typing_result.get("asserted_type_concept_ids") or ():
        if not isinstance(type_concept_id, str) or not type_concept_id.strip():
            continue
        blueprint = _resolve_type_blueprint(
            type_concept_id=type_concept_id.strip(),
            typing_result=typing_result,
        )
        if not isinstance(blueprint, Mapping):
            continue
        ensure_specific_computer_file_copy_type_exists(
            type_concept_id=type_concept_id.strip(),
            name=str(blueprint.get("name") or type_concept_id.strip()),
            parent_type_concept_id=str(
                blueprint.get("parent") or "#V#computer_file_copy"
            ),
            description=str(
                blueprint.get("description")
                or "A specific computer file copy subtype."
            ),
            system_tags=list(blueprint.get("system_tags") or []),
            logger=logger_override or logger,
        )
        created_or_known.append(type_concept_id.strip())
    return created_or_known


def persist_file_copy_typing(
    *,
    file_copy_concept_id: str,
    typing_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist authoritative file-copy typing evidence and type assertions."""

    concept_id = _normalise_optional_text(file_copy_concept_id)
    if not concept_id:
        return {
            "success": False,
            "error": "missing_file_copy_concept_id",
            "typing_persisted": False,
        }

    typed_result = dict(typing_result or {})
    typed_result.setdefault("schema_version", FILE_COPY_TYPING_SCHEMA_VERSION)
    typed_result["recorded_at"] = _utc_now_iso()
    typed_result["file_copy_concept_id"] = concept_id

    ensured_type_ids = ensure_file_copy_typing_types_exist(typed_result)
    structural_relations: list[dict[str, Any]] = []
    relation_errors: list[dict[str, Any]] = []

    for type_concept_id in ensured_type_ids:
        relation_result = add_relationship(
            concept_id,
            "is_an_instance_of",
            type_concept_id,
        )
        if isinstance(relation_result, Mapping) and relation_result.get("success") is True:
            structural_relations.append(
                {
                    "predicate": "is_an_instance_of",
                    "target_id": type_concept_id,
                    "modified": bool(relation_result.get("forward_modified")),
                }
            )
            continue
        relation_errors.append(
            {
                "predicate": "is_an_instance_of",
                "target_id": type_concept_id,
                "details": dict(relation_result) if isinstance(relation_result, Mapping) else relation_result,
            }
        )

    upsert_result = upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=FILE_COPY_TYPING_PREDICATE,
        text=json.dumps(typed_result, ensure_ascii=False, sort_keys=True),
        lang="en-NZ",
        garbage_collect=True,
        context={
            "source": "#V#file_copy_typing_workflow",
            "schema_version": FILE_COPY_TYPING_SCHEMA_VERSION,
        },
    )

    return {
        "success": True,
        "typing_persisted": True,
        "typing_predicate": FILE_COPY_TYPING_PREDICATE,
        "typing_relation_id": (
            upsert_result.get("kept_relation_id")
            if isinstance(upsert_result, Mapping)
            else None
        ),
        "typing_replaced_count": (
            int(upsert_result.get("replaced_count") or 0)
            if isinstance(upsert_result, Mapping)
            else 0
        ),
        "typing_result": typed_result,
        "asserted_type_concept_ids": list(ensured_type_ids),
        "structural_relations": structural_relations,
        "relation_errors": relation_errors,
    }


def _lookup_type_display_name(
    type_concept_id: str,
    *,
    typing_result: Mapping[str, Any] | None = None,
) -> str:
    blueprint = _resolve_type_blueprint(
        type_concept_id=type_concept_id,
        typing_result=typing_result,
    )
    if isinstance(blueprint, Mapping):
        name = _normalise_optional_text(
            blueprint.get("display_name") or blueprint.get("name")
        )
        if name:
            return name

    try:
        from . import concept_service

        concept_doc = concept_service.get_concept_by_concept_id(type_concept_id)
    except Exception:
        concept_doc = None
    if isinstance(concept_doc, Mapping):
        try:
            return get_concept_display_name_with_names_fallback(dict(concept_doc))
        except Exception:
            pass
    return type_concept_id


def build_file_copy_typing_context(
    *,
    file_copy_concept_id: str,
) -> dict[str, Any] | None:
    """Build a typed artefact context payload for routing and discovery."""

    artifact_record = build_file_copy_artifact_record(
        file_copy_concept_id=file_copy_concept_id
    )
    if not isinstance(artifact_record, Mapping):
        return None

    raw_size_bytes = artifact_record.get("size_bytes")
    inferred = infer_file_copy_typing(
        content_type=_normalise_optional_text(artifact_record.get("content_type")),
        original_filename=_normalise_optional_text(artifact_record.get("name")),
        size_bytes=int(raw_size_bytes) if isinstance(raw_size_bytes, int) else None,
    )
    stored_type_ids = [
        str(item).strip()
        for item in (artifact_record.get("type_concept_ids") or [])
        if isinstance(item, str) and str(item).strip()
    ]
    combined_type_ids = _dedupe_strings(
        [*stored_type_ids, *(inferred.get("asserted_type_concept_ids") or [])]
    )
    type_display_names = [
        _lookup_type_display_name(item, typing_result=inferred)
        for item in combined_type_ids
    ]

    return {
        "file_copy_concept_id": str(artifact_record.get("concept_id") or file_copy_concept_id),
        "original_filename": _normalise_optional_text(artifact_record.get("name")),
        "content_type": _normalise_optional_text(artifact_record.get("content_type")),
        "size_bytes": artifact_record.get("size_bytes"),
        "route_hint": inferred.get("route_hint"),
        "route_confidence": inferred.get("route_confidence"),
        "type_concept_ids": combined_type_ids,
        "type_display_names": type_display_names,
        "typing_result": inferred,
    }


__all__ = [
    "FILE_COPY_TYPING_PREDICATE",
    "FILE_COPY_TYPING_SCHEMA_VERSION",
    "build_file_copy_typing_context",
    "ensure_file_copy_typing_types_exist",
    "infer_file_copy_typing",
    "persist_file_copy_typing",
]
