"""Canonical turn-level display element contract for chat responses.

JVNAUTOSCI-1149 introduces a first-class response-construction model for
display elements so rendering decisions are explicit and inspectable.
"""

from __future__ import annotations

import re
import math
from typing import Any, Callable, Mapping, Sequence

DISPLAY_ELEMENT_SCHEMA_VERSION = "turn_display_elements_v1"

# Keep this allow-list explicit so element admission is deterministic and
# renderer integration can evolve without ad hoc shape drift.
ALLOWED_DISPLAY_ELEMENT_TYPES = frozenset(
    {
        "calendar_view",
        "chart_view",
        "document_view",
        "hierarchy_view",
        "location_view",
        "text_block",
        "json_block",
        "kanban_view",
        "relation_graph_view",
        "table",
        "relation_truth_state",
        "timeline",
        "task_view",
        "workflow_view",
        "image",
    }
)
OPTIONAL_PAYLOAD_TITLE_ELEMENT_TYPES = frozenset(
    {
        "calendar_view",
        "chart_view",
        "document_view",
        "hierarchy_view",
        "location_view",
        "kanban_view",
        "relation_graph_view",
        "table",
        "timeline",
        "task_view",
        "workflow_view",
    }
)
RELATION_TRUTH_STATE_GROUP_STATUSES = frozenset(
    {"asserted", "missing_expected", "uncertain"}
)
RELATION_GRAPH_ALLOWED_DIRECTIONS = frozenset({"directed", "undirected"})
RELATION_GRAPH_MAX_NODES = 200
RELATION_GRAPH_MAX_EDGES = 400
HIERARCHY_MAX_NODES = 200
HIERARCHY_MAX_EDGES = 400
HIERARCHY_EDGE_BRANCH_KINDS = frozenset(
    {"type_hierarchy", "instance_hierarchy", "organisational_hierarchy", "custom"}
)
HIERARCHY_MAX_DEPTH = 12
CALENDAR_ALLOWED_GRANULARITIES = frozenset({"month", "week", "day"})
CHART_ALLOWED_TYPES = frozenset({"line", "bar", "area", "scatter"})
CHART_MAX_SERIES = 20
CHART_MAX_POINTS_PER_SERIES = 240
CHART_MAX_TOTAL_POINTS = 1200
LOCATION_MAX_POINTS = 240
LOCATION_LATITUDE_MIN = -90.0
LOCATION_LATITUDE_MAX = 90.0
LOCATION_LONGITUDE_MIN = -180.0
LOCATION_LONGITUDE_MAX = 180.0
LOCATION_VIEWPORT_MIN_ZOOM = 1
LOCATION_VIEWPORT_MAX_ZOOM = 20
DOCUMENT_SECTION_EXCERPT_MAX_CHARS = 700
DOCUMENT_SECTION_DIFF_SUMMARY_MAX_CHARS = 280
DOCUMENT_EXCERPT_TRUNCATION_MARKER = " ... [truncated]"

_JSON_FENCE_PATTERN = re.compile(
    r"```json\s*\n(?P<body>[\s\S]*?)\n```",
    flags=re.IGNORECASE,
)
_FENCED_CODE_BLOCK_PATTERN = re.compile(
    r"```[\s\S]*?```",
    flags=re.IGNORECASE,
)
_MARKDOWN_TABLE_SEPARATOR_PATTERN = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_MARKDOWN_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")
_MARKDOWN_STRONG_LINE_PATTERN = re.compile(r"^\*\*(?P<title>.+?)\*\*$")
_NUMBER_PATTERN = re.compile(r"^-?(?:\d+|\d+\.\d+)$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MARKDOWN_TABLE_TITLE_LOOKBACK_LINES = 5
_RELATION_EXTENT_ARG1_TOKENS = frozenset({"arg1", "subject", "left", "source"})
_RELATION_EXTENT_PREDICATE_TOKENS = frozenset({"predicate", "relation"})
_RELATION_EXTENT_ARG2_TOKENS = frozenset({"arg2", "object", "right", "target"})
_TABLE_COLUMN_VISIBILITY_DEFAULT_MODES = frozenset({"compact", "full"})
_TABLE_PRESENTATION_MODES = frozenset({"augment", "inline_primary"})


def _normalise_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_relation_extent_column_token(value: object) -> str:
    text = _normalise_text(value)
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _normalise_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def _resolve_optional_payload_title(
    payload: Mapping[str, Any] | None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> str | None:
    candidates: list[object] = []
    if isinstance(payload, Mapping):
        candidates.append(payload.get("title"))
    if isinstance(metadata, Mapping):
        metadata_payload = metadata.get("payload")
        if isinstance(metadata_payload, Mapping):
            candidates.extend(
                [
                    metadata_payload.get("title"),
                    metadata_payload.get("section_title"),
                ]
            )
        candidates.extend(
            [
                metadata.get("title"),
                metadata.get("section_title"),
            ]
        )
    for candidate in candidates:
        title = _normalise_text(candidate)
        if title:
            return title
    return None


def _with_optional_payload_title(
    payload: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_payload = dict(payload)
    title = _resolve_optional_payload_title(payload, metadata=metadata)
    if title:
        canonical_payload["title"] = title
    else:
        canonical_payload.pop("title", None)
    return canonical_payload


def _validate_optional_payload_title(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    if "title" not in payload:
        return
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append(f"{label}.payload.title must be a non-empty string when provided")


def _validate_table_column_visibility(
    *,
    payload: Mapping[str, Any],
    label: str,
    valid_column_ids: set[str],
    errors: list[str],
) -> None:
    raw_visibility = payload.get("column_visibility")
    if raw_visibility is None:
        return
    if not isinstance(raw_visibility, Mapping):
        errors.append(f"{label}.payload.column_visibility must be a mapping when provided")
        return

    default_mode = raw_visibility.get("default_mode")
    if default_mode is not None:
        if not isinstance(default_mode, str) or not default_mode.strip():
            errors.append(
                f"{label}.payload.column_visibility.default_mode must be a non-empty string when provided"
            )
        elif default_mode.strip().lower() not in _TABLE_COLUMN_VISIBILITY_DEFAULT_MODES:
            errors.append(
                f"{label}.payload.column_visibility.default_mode must be one of {sorted(_TABLE_COLUMN_VISIBILITY_DEFAULT_MODES)}"
            )

    compact_column_ids = raw_visibility.get("compact_column_ids")
    if not isinstance(compact_column_ids, list) or not compact_column_ids:
        errors.append(
            f"{label}.payload.column_visibility.compact_column_ids must be a non-empty list"
        )
        compact_column_ids = []

    seen_column_ids: set[str] = set()
    for compact_index, raw_column_id in enumerate(compact_column_ids):
        compact_label = (
            f"{label}.payload.column_visibility.compact_column_ids[{compact_index}]"
        )
        if not isinstance(raw_column_id, str) or not raw_column_id.strip():
            errors.append(f"{compact_label} must be a non-empty string")
            continue
        column_id = raw_column_id.strip()
        if column_id in seen_column_ids:
            errors.append(f"{compact_label} must not duplicate another compact column id")
            continue
        seen_column_ids.add(column_id)
        if valid_column_ids and column_id not in valid_column_ids:
            errors.append(f"{compact_label} must reference a declared column")

    if (
        valid_column_ids
        and len(seen_column_ids) > 0
        and len(seen_column_ids) >= len(valid_column_ids)
    ):
        errors.append(
            f"{label}.payload.column_visibility.compact_column_ids must be a strict subset of declared columns"
        )

    for label_key in ("expand_label", "collapse_label"):
        raw_label = raw_visibility.get(label_key)
        if raw_label is not None and (
            not isinstance(raw_label, str) or not raw_label.strip()
        ):
            errors.append(
                f"{label}.payload.column_visibility.{label_key} must be a non-empty string when provided"
            )


def _resolve_relation_extent_compact_column_ids(
    columns: Sequence[Mapping[str, Any]],
) -> list[str] | None:
    role_to_column_id: dict[str, str] = {}

    for column in columns:
        if not isinstance(column, Mapping):
            continue
        column_id = _normalise_text(column.get("column_id"))
        if not column_id:
            continue

        candidate_tokens = {
            _normalise_relation_extent_column_token(column_id),
            _normalise_relation_extent_column_token(column.get("label")),
            _normalise_relation_extent_column_token(column.get("source_key")),
        }
        candidate_tokens.discard("")

        if "arg1" not in role_to_column_id and (
            candidate_tokens & _RELATION_EXTENT_ARG1_TOKENS
        ):
            role_to_column_id["arg1"] = column_id
        if "predicate" not in role_to_column_id and (
            candidate_tokens & _RELATION_EXTENT_PREDICATE_TOKENS
        ):
            role_to_column_id["predicate"] = column_id
        if "arg2" not in role_to_column_id and (
            candidate_tokens & _RELATION_EXTENT_ARG2_TOKENS
        ):
            role_to_column_id["arg2"] = column_id

        if len(role_to_column_id) == 3:
            break

    if len(role_to_column_id) < 3:
        return None

    compact_column_ids = [
        role_to_column_id["arg1"],
        role_to_column_id["predicate"],
        role_to_column_id["arg2"],
    ]
    if len(set(compact_column_ids)) < 3:
        return None
    return compact_column_ids


def _build_default_table_column_visibility(
    columns: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    if not isinstance(columns, Sequence) or isinstance(
        columns, (str, bytes, bytearray)
    ):
        return None

    declared_column_count = sum(
        1
        for column in columns
        if isinstance(column, Mapping) and _normalise_text(column.get("column_id"))
    )
    if declared_column_count <= 3:
        return None

    compact_column_ids = _resolve_relation_extent_compact_column_ids(columns)
    if not compact_column_ids:
        return None

    return {
        "default_mode": "compact",
        "compact_column_ids": compact_column_ids,
        "expand_label": "Expand table",
        "collapse_label": "Show compact view",
    }


def _with_default_table_column_visibility(payload: Mapping[str, Any]) -> dict[str, Any]:
    canonical_payload = dict(payload)
    existing_column_visibility = canonical_payload.get("column_visibility")
    if isinstance(existing_column_visibility, Mapping):
        return canonical_payload

    inferred_column_visibility = _build_default_table_column_visibility(
        canonical_payload.get("columns")
    )
    if inferred_column_visibility:
        canonical_payload["column_visibility"] = inferred_column_visibility
    return canonical_payload


def _truncate_document_text(
    value: object,
    *,
    max_chars: int,
) -> tuple[str | None, bool, int | None]:
    """Normalise + truncate document text while surfacing truncation metadata."""
    text = _normalise_text(value)
    if not text:
        return None, False, None
    original_length = len(text)
    if original_length <= max_chars:
        return text, False, original_length

    marker = DOCUMENT_EXCERPT_TRUNCATION_MARKER
    if max_chars <= len(marker):
        return marker[:max_chars], True, original_length

    prefix_limit = max(24, max_chars - len(marker))
    truncated = text[:prefix_limit].rstrip()
    if not truncated:
        truncated = text[:prefix_limit]
    return f"{truncated}{marker}", True, original_length


def _dedupe_preserve_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def extract_json_fences(text: str | None) -> list[str]:
    """Extract canonical fenced JSON blocks from text."""
    if not isinstance(text, str) or not text.strip():
        return []

    fences: list[str] = []
    for match in _JSON_FENCE_PATTERN.finditer(text):
        body = str(match.group("body") or "").strip()
        if not body:
            continue
        fences.append(f"```json\n{body}\n```")
    return _dedupe_preserve_order(fences)


def _extract_json_body_from_fence(fence: str) -> str:
    match = _JSON_FENCE_PATTERN.search(fence)
    if not match:
        return ""
    return str(match.group("body") or "").strip()


def _split_markdown_table_row(line: str) -> list[str]:
    raw = line.strip()
    if not raw:
        return []
    if "|" not in raw:
        return []
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [cell.strip() for cell in raw.split("|")]


def _build_column_id(label: str, index: int, used_ids: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    base = slug or f"column_{index + 1}"
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _infer_cell_value_type(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return "boolean"
    if _NUMBER_PATTERN.match(lowered):
        return "number"
    if _ISO_DATE_PATTERN.match(lowered):
        return "date"
    return "text"


def _extract_context_title_line(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line or "|" in line:
        return None
    line = re.sub(r"^[-*+]\s+", "", line).strip()

    heading_match = _MARKDOWN_HEADING_PATTERN.match(line)
    if heading_match:
        return _normalise_text(heading_match.group("title"))

    strong_match = _MARKDOWN_STRONG_LINE_PATTERN.match(line)
    if strong_match:
        return _normalise_text(strong_match.group("title"))

    if line.endswith(":"):
        return _normalise_text(line[:-1])
    return None


def _extract_markdown_table_context_title(
    lines: Sequence[str],
    *,
    header_line_index: int,
) -> str | None:
    lookback_start = max(0, header_line_index - _MARKDOWN_TABLE_TITLE_LOOKBACK_LINES)
    for cursor in range(header_line_index - 1, lookback_start - 1, -1):
        candidate_line = lines[cursor]
        if not isinstance(candidate_line, str):
            continue
        if not candidate_line.strip():
            continue
        title = _extract_context_title_line(candidate_line)
        if title:
            return title
        # Stop on first non-empty line without a robust title signal.
        break
    return None


def extract_markdown_tables(text: str | None) -> list[dict[str, Any]]:
    """Extract structured markdown tables from non-code-fenced text."""
    if not isinstance(text, str) or not text.strip():
        return []

    # Avoid treating pipe-delimited content inside code fences as table rows.
    # Preserve newline positions so source spans continue to identify the
    # untouched screen text even when an earlier code fence is removed.
    sanitised = _FENCED_CODE_BLOCK_PATTERN.sub(
        lambda match: "\n" * match.group(0).count("\n"),
        text,
    )
    lines = sanitised.splitlines()
    tables: list[dict[str, Any]] = []

    index = 0
    while index + 1 < len(lines):
        header_line = lines[index]
        separator_line = lines[index + 1]

        header_cells = _split_markdown_table_row(header_line)
        if len(header_cells) < 2:
            index += 1
            continue

        if not _MARKDOWN_TABLE_SEPARATOR_PATTERN.match(separator_line):
            index += 1
            continue

        separator_cells = _split_markdown_table_row(separator_line)
        if len(separator_cells) < len(header_cells):
            index += 1
            continue

        row_values: list[list[str]] = []
        cursor = index + 2
        while cursor < len(lines):
            row_line = lines[cursor]
            row_cells = _split_markdown_table_row(row_line)
            if not row_cells:
                break
            row_values.append((row_cells + [""] * len(header_cells))[: len(header_cells)])
            cursor += 1

        if not row_values:
            index += 1
            continue

        used_column_ids: set[str] = set()
        column_configs: list[dict[str, Any]] = []
        for column_index, label in enumerate(header_cells):
            column_id = _build_column_id(label, column_index, used_column_ids)
            column_configs.append(
                {
                    "column_id": column_id,
                    "label": label or f"Column {column_index + 1}",
                    "data_type": "mixed",
                    "source_key": f"col_{column_index + 1}",
                }
            )

        records: list[dict[str, Any]] = []
        for values in row_values:
            record: dict[str, Any] = {}
            for column_index, value in enumerate(values):
                record[f"col_{column_index + 1}"] = value.strip()
            records.append(record)

        table_title = _extract_markdown_table_context_title(
            lines,
            header_line_index=index,
        )
        payload = build_canonical_table_payload_from_records(
            records=records,
            columns=column_configs,
            default_sort_column_id=(
                str(column_configs[0].get("column_id"))
                if column_configs
                else None
            ),
            default_sort_direction="asc",
            pagination_enabled=True,
            page_size=min(100, len(records)),
            title=table_title,
        )
        payload["source_span"] = {
            "start_line": index + 1,
            "end_line": cursor,
        }
        tables.append(payload)
        index = cursor

    return tables


def _normalise_table_data_type(value: object) -> str:
    if not isinstance(value, str):
        return "text"
    cleaned = value.strip().lower()
    if cleaned in {"text", "number", "boolean", "date", "mixed"}:
        return cleaned
    return "text"


def _coerce_table_display_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _infer_table_value_type(value_raw: object, value_display: str, data_type: str) -> str:
    normalised = _normalise_table_data_type(data_type)
    if normalised != "mixed":
        return normalised

    if isinstance(value_raw, bool):
        return "boolean"
    if isinstance(value_raw, (int, float)) and not isinstance(value_raw, bool):
        return "number"
    return _infer_cell_value_type(value_display)


def build_canonical_table_payload_from_records(
    *,
    records: Sequence[Mapping[str, Any]],
    columns: Sequence[Mapping[str, Any]],
    row_id_field: str | None = None,
    row_provenance_field: str | None = None,
    default_sort_column_id: str | None = None,
    default_sort_direction: str = "asc",
    filters: Sequence[Mapping[str, Any]] | None = None,
    pagination_enabled: bool = True,
    page_size: int | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Build one canonical table payload from configured column metadata + records.

    This helper keeps task/predicate extent table construction on one shape so the
    renderer can stay generic and avoid per-use-case branching.
    """
    used_column_ids: set[str] = set()
    canonical_columns: list[dict[str, Any]] = []
    source_keys_by_column_id: dict[str, str] = {}
    data_type_by_column_id: dict[str, str] = {}

    for index, column in enumerate(columns):
        if not isinstance(column, Mapping):
            continue
        configured_column_id = _normalise_text(column.get("column_id"))
        label = _normalise_text(column.get("label")) or configured_column_id or f"Column {index + 1}"
        column_id = _build_column_id(configured_column_id or label, index, used_column_ids)
        data_type = _normalise_table_data_type(column.get("data_type"))

        canonical_columns.append(
            {
                "column_id": column_id,
                "label": label,
                "data_type": data_type,
                "position": index,
            }
        )
        source_key = _normalise_text(column.get("source_key")) or configured_column_id or column_id
        source_keys_by_column_id[column_id] = source_key
        data_type_by_column_id[column_id] = data_type

    canonical_rows: list[dict[str, Any]] = []
    for row_index, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            continue

        row_id_candidate = (
            _normalise_text(record.get(row_id_field))
            if isinstance(row_id_field, str) and row_id_field
            else None
        )
        row_id = row_id_candidate or f"row_{row_index}"
        cells: list[dict[str, Any]] = []
        for column in canonical_columns:
            column_id = str(column["column_id"])
            source_key = source_keys_by_column_id.get(column_id, column_id)
            value_raw = record.get(source_key)
            value_display = _coerce_table_display_value(value_raw)
            data_type = data_type_by_column_id.get(column_id, "text")
            value_type = _infer_table_value_type(value_raw, value_display, data_type)
            cells.append(
                {
                    "column_id": column_id,
                    "value_raw": value_raw,
                    "value_display": value_display,
                    "value_type": value_type,
                }
            )

        row_payload: dict[str, Any] = {"row_id": row_id, "cells": cells}
        if isinstance(row_provenance_field, str) and row_provenance_field:
            provenance_value = record.get(row_provenance_field)
            if isinstance(provenance_value, Mapping):
                row_payload["provenance"] = dict(provenance_value)
            elif provenance_value is not None:
                row_payload["provenance"] = {"source_ref": str(provenance_value)}

        canonical_rows.append(row_payload)

    resolved_sort_column = _normalise_text(default_sort_column_id)
    declared_column_ids = {str(column["column_id"]) for column in canonical_columns}
    if not resolved_sort_column or resolved_sort_column not in declared_column_ids:
        resolved_sort_column = str(canonical_columns[0]["column_id"]) if canonical_columns else None

    direction = "desc" if str(default_sort_direction).strip().lower() == "desc" else "asc"
    if isinstance(page_size, int) and page_size > 0:
        resolved_page_size = page_size
    else:
        resolved_page_size = min(100, max(1, len(canonical_rows)))

    table_filters: list[dict[str, Any]] = []
    if isinstance(filters, Sequence):
        for value in filters:
            if isinstance(value, Mapping):
                table_filters.append(dict(value))

    payload = {
        "columns": canonical_columns,
        "rows": canonical_rows,
        "sort": {
            "default_column_id": resolved_sort_column,
            "direction": direction,
        },
        "filters": table_filters,
        "pagination": {
            "enabled": bool(pagination_enabled),
            "page_size": resolved_page_size,
            "total_rows": len(canonical_rows),
        },
    }
    inferred_column_visibility = _build_default_table_column_visibility(canonical_columns)
    if inferred_column_visibility:
        payload["column_visibility"] = inferred_column_visibility
    normalised_title = _normalise_text(title)
    if normalised_title:
        payload["title"] = normalised_title
    return payload


def _validate_task_links(
    *,
    links: Any,
    label: str,
    errors: list[str],
) -> None:
    """Validate link entries shared by workflow/timeline/task_view payloads."""
    if links is None:
        return
    if not isinstance(links, list):
        errors.append(f"{label} must be a list when provided")
        return

    for link_index, link in enumerate(links):
        link_label = f"{label}[{link_index}]"
        if not isinstance(link, Mapping):
            errors.append(f"{link_label} must be a mapping")
            continue
        target_id = link.get("target_id")
        if not isinstance(target_id, str) or not target_id.strip():
            errors.append(f"{link_label}.target_id must be a non-empty string")
        label_value = link.get("label")
        if label_value is not None and (
            not isinstance(label_value, str) or not label_value.strip()
        ):
            errors.append(
                f"{link_label}.label must be a non-empty string when provided"
            )
        link_type = link.get("link_type")
        if link_type is not None and (
            not isinstance(link_type, str) or not link_type.strip()
        ):
            errors.append(
                f"{link_label}.link_type must be a non-empty string when provided"
            )
        href = link.get("href")
        if href is not None and (not isinstance(href, str) or not href.strip()):
            errors.append(
                f"{link_label}.href must be a non-empty string when provided"
            )


def _validate_text_block_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    text_value = payload.get("text")
    if not isinstance(text_value, str) or not text_value.strip():
        errors.append(f"{label}.payload.text must be a non-empty string")


def _validate_json_block_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    fence = payload.get("fence")
    if not isinstance(fence, str) or not fence.strip():
        errors.append(f"{label}.payload.fence must be a non-empty string")
    elif "```json" not in fence.lower():
        errors.append(f"{label}.payload.fence must be a fenced JSON block")


def _validate_table_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        errors.append(f"{label}.payload.columns must be a non-empty list")
        columns = []

    valid_column_ids: set[str] = set()
    for column_index, column in enumerate(columns):
        column_label = f"{label}.payload.columns[{column_index}]"
        if not isinstance(column, Mapping):
            errors.append(f"{column_label} must be a mapping")
            continue
        column_id = column.get("column_id")
        if not isinstance(column_id, str) or not column_id.strip():
            errors.append(f"{column_label}.column_id must be a non-empty string")
            continue
        valid_column_ids.add(column_id)

        column_name = column.get("label")
        if not isinstance(column_name, str) or not column_name.strip():
            errors.append(f"{column_label}.label must be a non-empty string")
        data_type = column.get("data_type")
        if not isinstance(data_type, str) or not data_type.strip():
            errors.append(f"{column_label}.data_type must be a non-empty string")

    _validate_table_column_visibility(
        payload=payload,
        label=label,
        valid_column_ids=valid_column_ids,
        errors=errors,
    )

    rows = payload.get("rows")
    if not isinstance(rows, list):
        errors.append(f"{label}.payload.rows must be a list")
        rows = []

    for row_index, row in enumerate(rows):
        row_label = f"{label}.payload.rows[{row_index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{row_label} must be a mapping")
            continue
        row_id = row.get("row_id")
        if not isinstance(row_id, str) or not row_id.strip():
            errors.append(f"{row_label}.row_id must be a non-empty string")

        cells = row.get("cells")
        if not isinstance(cells, list):
            errors.append(f"{row_label}.cells must be a list")
            continue
        if columns and len(cells) != len(columns):
            errors.append(
                f"{row_label}.cells count {len(cells)} does not match column count {len(columns)}"
            )

        for cell_index, cell in enumerate(cells):
            cell_label = f"{row_label}.cells[{cell_index}]"
            if not isinstance(cell, Mapping):
                errors.append(f"{cell_label} must be a mapping")
                continue
            column_id = cell.get("column_id")
            if (
                not isinstance(column_id, str)
                or not column_id.strip()
                or (valid_column_ids and column_id not in valid_column_ids)
            ):
                errors.append(
                    f"{cell_label}.column_id must reference a declared column"
                )
            value_type = cell.get("value_type")
            if not isinstance(value_type, str) or not value_type.strip():
                errors.append(f"{cell_label}.value_type must be a non-empty string")


def _validate_relation_truth_state_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append(f"{label}.payload.title must be a non-empty string")

    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        errors.append(f"{label}.payload.groups must be a non-empty list")
        groups = []

    for group_index, group in enumerate(groups):
        group_label = f"{label}.payload.groups[{group_index}]"
        if not isinstance(group, Mapping):
            errors.append(f"{group_label} must be a mapping")
            continue

        group_name = group.get("label")
        if not isinstance(group_name, str) or not group_name.strip():
            errors.append(f"{group_label}.label must be a non-empty string")

        group_status = group.get("status")
        if (
            not isinstance(group_status, str)
            or group_status.strip() not in RELATION_TRUTH_STATE_GROUP_STATUSES
        ):
            errors.append(
                f"{group_label}.status must be one of {sorted(RELATION_TRUTH_STATE_GROUP_STATUSES)}"
            )

        assertions = group.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            errors.append(f"{group_label}.assertions must be a non-empty list")
            assertions = []

        for assertion_index, assertion in enumerate(assertions):
            assertion_label = f"{group_label}.assertions[{assertion_index}]"
            if not isinstance(assertion, Mapping):
                errors.append(f"{assertion_label} must be a mapping")
                continue

            assertion_id = assertion.get("assertion_id")
            if assertion_id is not None and (
                not isinstance(assertion_id, str) or not assertion_id.strip()
            ):
                errors.append(
                    f"{assertion_label}.assertion_id must be a non-empty string when provided"
                )

            for relation_key in ("arg1", "predicate", "arg2"):
                value = assertion.get(relation_key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{assertion_label}.{relation_key} must be a non-empty string"
                    )

            is_asserted = assertion.get("is_asserted")
            if not isinstance(is_asserted, bool):
                errors.append(f"{assertion_label}.is_asserted must be a boolean")


def _validate_relation_graph_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append(f"{label}.payload.nodes must be a non-empty list")
        nodes = []
    if isinstance(nodes, list) and len(nodes) > RELATION_GRAPH_MAX_NODES:
        errors.append(
            f"{label}.payload.nodes exceeds max size {RELATION_GRAPH_MAX_NODES}"
        )

    valid_node_ids: set[str] = set()
    for node_index, node in enumerate(nodes):
        node_label = f"{label}.payload.nodes[{node_index}]"
        if not isinstance(node, Mapping):
            errors.append(f"{node_label} must be a mapping")
            continue

        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(f"{node_label}.node_id must be a non-empty string")
            continue
        if node_id in valid_node_ids:
            errors.append(f"{node_label}.node_id must be unique")
            continue
        valid_node_ids.add(node_id)

        display_label = node.get("label")
        if not isinstance(display_label, str) or not display_label.strip():
            errors.append(f"{node_label}.label must be a non-empty string")

        node_kind = node.get("node_kind")
        if not isinstance(node_kind, str) or not node_kind.strip():
            errors.append(f"{node_label}.node_kind must be a non-empty string")

        group = node.get("group")
        if group is not None and (not isinstance(group, str) or not group.strip()):
            errors.append(
                f"{node_label}.group must be a non-empty string when provided"
            )

        _validate_task_links(
            links=node.get("task_links"),
            label=f"{node_label}.task_links",
            errors=errors,
        )

    edges = payload.get("edges")
    if not isinstance(edges, list):
        errors.append(f"{label}.payload.edges must be a list")
        edges = []
    if isinstance(edges, list) and len(edges) > RELATION_GRAPH_MAX_EDGES:
        errors.append(
            f"{label}.payload.edges exceeds max size {RELATION_GRAPH_MAX_EDGES}"
        )

    seen_edge_ids: set[str] = set()
    for edge_index, edge in enumerate(edges):
        edge_label = f"{label}.payload.edges[{edge_index}]"
        if not isinstance(edge, Mapping):
            errors.append(f"{edge_label} must be a mapping")
            continue

        edge_id = edge.get("edge_id")
        if not isinstance(edge_id, str) or not edge_id.strip():
            errors.append(f"{edge_label}.edge_id must be a non-empty string")
        elif edge_id in seen_edge_ids:
            errors.append(f"{edge_label}.edge_id must be unique")
        else:
            seen_edge_ids.add(edge_id)

        source_node_id = edge.get("source")
        if not isinstance(source_node_id, str) or not source_node_id.strip():
            errors.append(f"{edge_label}.source must be a non-empty string")
        elif valid_node_ids and source_node_id not in valid_node_ids:
            errors.append(f"{edge_label}.source must reference a declared node")

        target_node_id = edge.get("target")
        if not isinstance(target_node_id, str) or not target_node_id.strip():
            errors.append(f"{edge_label}.target must be a non-empty string")
        elif valid_node_ids and target_node_id not in valid_node_ids:
            errors.append(f"{edge_label}.target must reference a declared node")

        predicate = edge.get("predicate")
        if not isinstance(predicate, str) or not predicate.strip():
            errors.append(f"{edge_label}.predicate must be a non-empty string")

        direction = edge.get("direction")
        if direction is not None:
            if not isinstance(direction, str) or direction.strip() not in (
                RELATION_GRAPH_ALLOWED_DIRECTIONS
            ):
                errors.append(
                    f"{edge_label}.direction must be one of {sorted(RELATION_GRAPH_ALLOWED_DIRECTIONS)} when provided"
                )

        weight = edge.get("weight")
        if weight is not None and isinstance(weight, bool):
            errors.append(f"{edge_label}.weight must be numeric when provided")
        elif weight is not None and not isinstance(weight, (int, float)):
            errors.append(f"{edge_label}.weight must be numeric when provided")

    layout_hint = payload.get("layout_hint")
    if layout_hint is not None and (
        not isinstance(layout_hint, str) or not layout_hint.strip()
    ):
        errors.append(
            f"{label}.payload.layout_hint must be a non-empty string when provided"
        )

    focus_node_id = payload.get("focus_node_id")
    if focus_node_id is not None:
        if not isinstance(focus_node_id, str) or not focus_node_id.strip():
            errors.append(
                f"{label}.payload.focus_node_id must be a non-empty string when provided"
            )
        elif valid_node_ids and focus_node_id not in valid_node_ids:
            errors.append(
                f"{label}.payload.focus_node_id must reference a declared node"
            )


def _validate_hierarchy_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append(f"{label}.payload.nodes must be a non-empty list")
        nodes = []
    if isinstance(nodes, list) and len(nodes) > HIERARCHY_MAX_NODES:
        errors.append(f"{label}.payload.nodes exceeds max size {HIERARCHY_MAX_NODES}")

    valid_node_ids: set[str] = set()
    for node_index, node in enumerate(nodes):
        node_label = f"{label}.payload.nodes[{node_index}]"
        if not isinstance(node, Mapping):
            errors.append(f"{node_label} must be a mapping")
            continue

        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(f"{node_label}.node_id must be a non-empty string")
            continue
        if node_id in valid_node_ids:
            errors.append(f"{node_label}.node_id must be unique")
            continue
        valid_node_ids.add(node_id)

        display_label = node.get("label")
        if not isinstance(display_label, str) or not display_label.strip():
            errors.append(f"{node_label}.label must be a non-empty string")

        node_kind = node.get("node_kind")
        if node_kind is not None and (
            not isinstance(node_kind, str) or not node_kind.strip()
        ):
            errors.append(
                f"{node_label}.node_kind must be a non-empty string when provided"
            )

        for count_key in ("parent_count", "child_count"):
            count_value = node.get(count_key)
            if count_value is None:
                continue
            if isinstance(count_value, bool) or not isinstance(count_value, int):
                errors.append(
                    f"{node_label}.{count_key} must be a non-negative integer when provided"
                )
                continue
            if count_value < 0:
                errors.append(
                    f"{node_label}.{count_key} must be a non-negative integer when provided"
                )

    edges = payload.get("edges")
    if not isinstance(edges, list):
        errors.append(f"{label}.payload.edges must be a list")
        edges = []
    if isinstance(edges, list) and len(edges) > HIERARCHY_MAX_EDGES:
        errors.append(f"{label}.payload.edges exceeds max size {HIERARCHY_MAX_EDGES}")

    seen_edge_ids: set[str] = set()
    for edge_index, edge in enumerate(edges):
        edge_label = f"{label}.payload.edges[{edge_index}]"
        if not isinstance(edge, Mapping):
            errors.append(f"{edge_label} must be a mapping")
            continue

        edge_id = edge.get("edge_id")
        if not isinstance(edge_id, str) or not edge_id.strip():
            errors.append(f"{edge_label}.edge_id must be a non-empty string")
        elif edge_id in seen_edge_ids:
            errors.append(f"{edge_label}.edge_id must be unique")
        else:
            seen_edge_ids.add(edge_id)

        parent_node_id = edge.get("parent_node_id")
        if not isinstance(parent_node_id, str) or not parent_node_id.strip():
            errors.append(f"{edge_label}.parent_node_id must be a non-empty string")
        elif valid_node_ids and parent_node_id not in valid_node_ids:
            errors.append(
                f"{edge_label}.parent_node_id must reference a declared node"
            )

        child_node_id = edge.get("child_node_id")
        if not isinstance(child_node_id, str) or not child_node_id.strip():
            errors.append(f"{edge_label}.child_node_id must be a non-empty string")
        elif valid_node_ids and child_node_id not in valid_node_ids:
            errors.append(
                f"{edge_label}.child_node_id must reference a declared node"
            )

        predicate = edge.get("predicate")
        if predicate is not None and (
            not isinstance(predicate, str) or not predicate.strip()
        ):
            errors.append(
                f"{edge_label}.predicate must be a non-empty string when provided"
            )

        branch_kind = edge.get("branch_kind")
        if branch_kind is not None:
            if (
                not isinstance(branch_kind, str)
                or branch_kind.strip() not in HIERARCHY_EDGE_BRANCH_KINDS
            ):
                errors.append(
                    f"{edge_label}.branch_kind must be one of {sorted(HIERARCHY_EDGE_BRANCH_KINDS)} when provided"
                )

    focus_node_id = payload.get("focus_node_id")
    if focus_node_id is not None:
        if not isinstance(focus_node_id, str) or not focus_node_id.strip():
            errors.append(
                f"{label}.payload.focus_node_id must be a non-empty string when provided"
            )
        elif valid_node_ids and focus_node_id not in valid_node_ids:
            errors.append(
                f"{label}.payload.focus_node_id must reference a declared node"
            )

    root_node_ids = payload.get("root_node_ids")
    if root_node_ids is not None:
        if not isinstance(root_node_ids, list):
            errors.append(f"{label}.payload.root_node_ids must be a list")
        else:
            for root_index, root_node_id in enumerate(root_node_ids):
                root_label = f"{label}.payload.root_node_ids[{root_index}]"
                if not isinstance(root_node_id, str) or not root_node_id.strip():
                    errors.append(f"{root_label} must be a non-empty string")
                    continue
                if valid_node_ids and root_node_id not in valid_node_ids:
                    errors.append(f"{root_label} must reference a declared node")

    expansion = payload.get("expansion")
    if expansion is not None:
        if not isinstance(expansion, Mapping):
            errors.append(f"{label}.payload.expansion must be a mapping")
        else:
            for key in ("show_parents", "show_children", "show_siblings"):
                flag = expansion.get(key)
                if flag is not None and not isinstance(flag, bool):
                    errors.append(
                        f"{label}.payload.expansion.{key} must be a boolean when provided"
                    )

            max_depth = expansion.get("max_depth")
            if max_depth is not None:
                if isinstance(max_depth, bool) or not isinstance(max_depth, int):
                    errors.append(
                        f"{label}.payload.expansion.max_depth must be an integer between 1 and {HIERARCHY_MAX_DEPTH} when provided"
                    )
                elif max_depth < 1 or max_depth > HIERARCHY_MAX_DEPTH:
                    errors.append(
                        f"{label}.payload.expansion.max_depth must be an integer between 1 and {HIERARCHY_MAX_DEPTH} when provided"
                    )


def _validate_chart_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    chart_type_raw = payload.get("chart_type")
    if not isinstance(chart_type_raw, str) or not chart_type_raw.strip():
        errors.append(
            f"{label}.payload.chart_type must be one of {sorted(CHART_ALLOWED_TYPES)}"
        )
    else:
        chart_type = chart_type_raw.strip().lower()
        if chart_type not in CHART_ALLOWED_TYPES:
            errors.append(
                f"{label}.payload.chart_type must be one of {sorted(CHART_ALLOWED_TYPES)}"
            )

    series = payload.get("series")
    if not isinstance(series, list) or not series:
        errors.append(f"{label}.payload.series must be a non-empty list")
        series = []
    if isinstance(series, list) and len(series) > CHART_MAX_SERIES:
        errors.append(f"{label}.payload.series exceeds max size {CHART_MAX_SERIES}")

    seen_series_ids: set[str] = set()
    total_points = 0
    for series_index, series_entry in enumerate(series):
        series_label = f"{label}.payload.series[{series_index}]"
        if not isinstance(series_entry, Mapping):
            errors.append(f"{series_label} must be a mapping")
            continue

        series_id = series_entry.get("series_id")
        if not isinstance(series_id, str) or not series_id.strip():
            errors.append(f"{series_label}.series_id must be a non-empty string")
        elif series_id in seen_series_ids:
            errors.append(f"{series_label}.series_id must be unique")
        else:
            seen_series_ids.add(series_id)

        series_name = series_entry.get("label")
        if not isinstance(series_name, str) or not series_name.strip():
            errors.append(f"{series_label}.label must be a non-empty string")

        points = series_entry.get("points")
        if not isinstance(points, list) or not points:
            errors.append(f"{series_label}.points must be a non-empty list")
            points = []
        if isinstance(points, list) and len(points) > CHART_MAX_POINTS_PER_SERIES:
            errors.append(
                f"{series_label}.points exceeds max size {CHART_MAX_POINTS_PER_SERIES}"
            )

        total_points += len(points)
        for point_index, point in enumerate(points):
            point_label = f"{series_label}.points[{point_index}]"
            if not isinstance(point, Mapping):
                errors.append(f"{point_label} must be a mapping")
                continue

            x_value = point.get("x")
            x_is_valid = (
                isinstance(x_value, str) and bool(x_value.strip())
            ) or (
                isinstance(x_value, (int, float))
                and not isinstance(x_value, bool)
                and not math.isnan(float(x_value))
                and not math.isinf(float(x_value))
            )
            if not x_is_valid:
                errors.append(
                    f"{point_label}.x must be a non-empty string or finite number"
                )

            y_value = _normalise_float(point.get("y"))
            if y_value is None:
                errors.append(f"{point_label}.y must be a finite number")

            point_meta = point.get("meta")
            if point_meta is not None and not isinstance(point_meta, Mapping):
                errors.append(f"{point_label}.meta must be a mapping when provided")

            _validate_task_links(
                links=point.get("task_links"),
                label=f"{point_label}.task_links",
                errors=errors,
            )

        _validate_task_links(
            links=series_entry.get("task_links"),
            label=f"{series_label}.task_links",
            errors=errors,
        )

    if total_points > CHART_MAX_TOTAL_POINTS:
        errors.append(
            f"{label}.payload.series total points exceeds max size {CHART_MAX_TOTAL_POINTS}"
        )

    for axis_key in ("x_axis", "y_axis", "units"):
        axis_value = payload.get(axis_key)
        if axis_value is not None and (
            not isinstance(axis_value, str) or not axis_value.strip()
        ):
            errors.append(
                f"{label}.payload.{axis_key} must be a non-empty string when provided"
            )

    stacked = payload.get("stacked")
    if stacked is not None and not isinstance(stacked, bool):
        errors.append(f"{label}.payload.stacked must be a boolean when provided")

    legend = payload.get("legend")
    if legend is not None and not isinstance(legend, bool):
        errors.append(f"{label}.payload.legend must be a boolean when provided")

    _validate_task_links(
        links=payload.get("task_links"),
        label=f"{label}.payload.task_links",
        errors=errors,
    )


def _validate_location_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    points = payload.get("points")
    if not isinstance(points, list) or not points:
        errors.append(f"{label}.payload.points must be a non-empty list")
        points = []
    if isinstance(points, list) and len(points) > LOCATION_MAX_POINTS:
        errors.append(f"{label}.payload.points exceeds max size {LOCATION_MAX_POINTS}")

    seen_point_ids: set[str] = set()
    for point_index, point in enumerate(points):
        point_label = f"{label}.payload.points[{point_index}]"
        if not isinstance(point, Mapping):
            errors.append(f"{point_label} must be a mapping")
            continue

        point_id = point.get("point_id")
        if not isinstance(point_id, str) or not point_id.strip():
            errors.append(f"{point_label}.point_id must be a non-empty string")
        elif point_id in seen_point_ids:
            errors.append(f"{point_label}.point_id must be unique")
        else:
            seen_point_ids.add(point_id)

        display_label = point.get("label")
        if not isinstance(display_label, str) or not display_label.strip():
            errors.append(f"{point_label}.label must be a non-empty string")

        latitude = _normalise_float(point.get("latitude"))
        longitude = _normalise_float(point.get("longitude"))
        has_coordinates = latitude is not None and longitude is not None
        has_partial_coordinates = (latitude is None) != (longitude is None)
        if has_partial_coordinates:
            errors.append(
                f"{point_label} must provide both latitude and longitude when either coordinate is present"
            )
        if latitude is not None and (
            latitude < LOCATION_LATITUDE_MIN or latitude > LOCATION_LATITUDE_MAX
        ):
            errors.append(
                f"{point_label}.latitude must be between {LOCATION_LATITUDE_MIN:g} and {LOCATION_LATITUDE_MAX:g}"
            )
        if longitude is not None and (
            longitude < LOCATION_LONGITUDE_MIN or longitude > LOCATION_LONGITUDE_MAX
        ):
            errors.append(
                f"{point_label}.longitude must be between {LOCATION_LONGITUDE_MIN:g} and {LOCATION_LONGITUDE_MAX:g}"
            )

        address = point.get("address")
        has_address = isinstance(address, str) and bool(address.strip())
        if address is not None and not has_address:
            errors.append(
                f"{point_label}.address must be a non-empty string when provided"
            )
        if not has_coordinates and not has_address:
            errors.append(
                f"{point_label} must provide at least one spatial anchor (coordinates or address)"
            )

        description = point.get("description")
        if description is not None and (
            not isinstance(description, str) or not description.strip()
        ):
            errors.append(
                f"{point_label}.description must be a non-empty string when provided"
            )

        confidence = point.get("confidence")
        if confidence is not None:
            confidence_value = _normalise_float(confidence)
            if confidence_value is None:
                errors.append(
                    f"{point_label}.confidence must be numeric when provided"
                )
            elif confidence_value < 0.0 or confidence_value > 1.0:
                errors.append(
                    f"{point_label}.confidence must be between 0 and 1 when provided"
                )

        _validate_task_links(
            links=point.get("task_links"),
            label=f"{point_label}.task_links",
            errors=errors,
        )

    viewport = payload.get("viewport")
    if viewport is not None:
        if not isinstance(viewport, Mapping):
            errors.append(f"{label}.payload.viewport must be a mapping")
        else:
            centre_lat_raw = (
                viewport.get("centre_lat")
                if "centre_lat" in viewport
                else viewport.get("center_lat")
            )
            centre_lon_raw = (
                viewport.get("centre_lon")
                if "centre_lon" in viewport
                else viewport.get("center_lon")
            )
            centre_lat = _normalise_float(centre_lat_raw)
            centre_lon = _normalise_float(centre_lon_raw)
            has_centre_lat = centre_lat_raw is not None
            has_centre_lon = centre_lon_raw is not None
            has_partial_centre = has_centre_lat != has_centre_lon
            if has_partial_centre:
                errors.append(
                    f"{label}.payload.viewport must provide both centre_lat and centre_lon when either is present"
                )
            if has_centre_lat and centre_lat is None:
                errors.append(
                    f"{label}.payload.viewport.centre_lat must be numeric when provided"
                )
            if has_centre_lon and centre_lon is None:
                errors.append(
                    f"{label}.payload.viewport.centre_lon must be numeric when provided"
                )
            if centre_lat is not None and (
                centre_lat < LOCATION_LATITUDE_MIN
                or centre_lat > LOCATION_LATITUDE_MAX
            ):
                errors.append(
                    f"{label}.payload.viewport.centre_lat must be between {LOCATION_LATITUDE_MIN:g} and {LOCATION_LATITUDE_MAX:g} when provided"
                )
            if centre_lon is not None and (
                centre_lon < LOCATION_LONGITUDE_MIN
                or centre_lon > LOCATION_LONGITUDE_MAX
            ):
                errors.append(
                    f"{label}.payload.viewport.centre_lon must be between {LOCATION_LONGITUDE_MIN:g} and {LOCATION_LONGITUDE_MAX:g} when provided"
                )

            zoom = viewport.get("zoom")
            if zoom is not None:
                if isinstance(zoom, bool) or not isinstance(zoom, int):
                    errors.append(
                        f"{label}.payload.viewport.zoom must be an integer between {LOCATION_VIEWPORT_MIN_ZOOM} and {LOCATION_VIEWPORT_MAX_ZOOM} when provided"
                    )
                elif (
                    zoom < LOCATION_VIEWPORT_MIN_ZOOM
                    or zoom > LOCATION_VIEWPORT_MAX_ZOOM
                ):
                    errors.append(
                        f"{label}.payload.viewport.zoom must be an integer between {LOCATION_VIEWPORT_MIN_ZOOM} and {LOCATION_VIEWPORT_MAX_ZOOM} when provided"
                    )

    map_provider_hint = payload.get("map_provider_hint")
    if map_provider_hint is not None and (
        not isinstance(map_provider_hint, str) or not map_provider_hint.strip()
    ):
        errors.append(
            f"{label}.payload.map_provider_hint must be a non-empty string when provided"
        )


def _validate_timeline_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        errors.append(f"{label}.payload.items must be a non-empty list")
        items = []

    for item_index, item in enumerate(items):
        item_label = f"{label}.payload.items[{item_index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{item_label} must be a mapping")
            continue

        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append(f"{item_label}.item_id must be a non-empty string")

        display_label = item.get("label")
        if not isinstance(display_label, str) or not display_label.strip():
            errors.append(f"{item_label}.label must be a non-empty string")

        start_at = item.get("start_at")
        end_at = item.get("end_at")
        has_start_at = isinstance(start_at, str) and bool(start_at.strip())
        has_end_at = isinstance(end_at, str) and bool(end_at.strip())
        if start_at is not None and not has_start_at:
            errors.append(
                f"{item_label}.start_at must be a non-empty string when provided"
            )
        if end_at is not None and not has_end_at:
            errors.append(
                f"{item_label}.end_at must be a non-empty string when provided"
            )
        if not has_start_at and not has_end_at:
            errors.append(
                f"{item_label} must provide at least one temporal anchor (start_at or end_at)"
            )

        status = item.get("status")
        if status is not None and (
            not isinstance(status, str) or not status.strip()
        ):
            errors.append(
                f"{item_label}.status must be a non-empty string when provided"
            )
        _validate_task_links(
            links=item.get("task_links"),
            label=f"{item_label}.task_links",
            errors=errors,
        )


def _validate_calendar_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        errors.append(f"{label}.payload.items must be a non-empty list")
        items = []

    for item_index, item in enumerate(items):
        item_label = f"{label}.payload.items[{item_index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{item_label} must be a mapping")
            continue

        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append(f"{item_label}.item_id must be a non-empty string")

        title = item.get("title")
        label_value = item.get("label")
        has_title = (isinstance(title, str) and bool(title.strip())) or (
            isinstance(label_value, str) and bool(label_value.strip())
        )
        if not has_title:
            errors.append(f"{item_label}.title must be a non-empty string")

        start_at = item.get("start_at")
        end_at = item.get("end_at")
        has_start_at = isinstance(start_at, str) and bool(start_at.strip())
        has_end_at = isinstance(end_at, str) and bool(end_at.strip())
        if start_at is not None and not has_start_at:
            errors.append(
                f"{item_label}.start_at must be a non-empty string when provided"
            )
        if end_at is not None and not has_end_at:
            errors.append(
                f"{item_label}.end_at must be a non-empty string when provided"
            )
        if not has_start_at and not has_end_at:
            errors.append(
                f"{item_label} must provide at least one temporal anchor (start_at or end_at)"
            )

        all_day = item.get("all_day")
        if all_day is not None and not isinstance(all_day, bool):
            errors.append(f"{item_label}.all_day must be a boolean when provided")

        for field_name in ("status", "timezone", "description"):
            field_value = item.get(field_name)
            if field_value is None:
                continue
            if not isinstance(field_value, str) or not field_value.strip():
                errors.append(
                    f"{item_label}.{field_name} must be a non-empty string when provided"
                )

        _validate_task_links(
            links=item.get("task_links"),
            label=f"{item_label}.task_links",
            errors=errors,
        )

    default_granularity = payload.get("default_granularity")
    if default_granularity is not None and (
        not isinstance(default_granularity, str)
        or default_granularity.strip() not in CALENDAR_ALLOWED_GRANULARITIES
    ):
        errors.append(
            f"{label}.payload.default_granularity must be one of {sorted(CALENDAR_ALLOWED_GRANULARITIES)} when provided"
        )

    focus_date = payload.get("focus_date")
    if focus_date is not None and (
        not isinstance(focus_date, str) or not focus_date.strip()
    ):
        errors.append(
            f"{label}.payload.focus_date must be a non-empty string when provided"
        )


def _validate_document_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    documents = payload.get("documents")
    if not isinstance(documents, list) or not documents:
        errors.append(f"{label}.payload.documents must be a non-empty list")
        documents = []

    for document_index, document in enumerate(documents):
        document_label = f"{label}.payload.documents[{document_index}]"
        if not isinstance(document, Mapping):
            errors.append(f"{document_label} must be a mapping")
            continue

        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id.strip():
            errors.append(f"{document_label}.document_id must be a non-empty string")

        title = document.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{document_label}.title must be a non-empty string")

        source_uri = document.get("source_uri")
        has_source_uri = isinstance(source_uri, str) and bool(source_uri.strip())
        if source_uri is not None and not has_source_uri:
            errors.append(
                f"{document_label}.source_uri must be a non-empty string when provided"
            )

        source_label = document.get("source_label")
        has_source_label = isinstance(source_label, str) and bool(
            source_label.strip()
        )
        if source_label is not None and not has_source_label:
            errors.append(
                f"{document_label}.source_label must be a non-empty string when provided"
            )
        if not has_source_uri and not has_source_label:
            errors.append(
                f"{document_label} must provide provenance (source_uri or source_label)"
            )

        updated_at = document.get("updated_at")
        if updated_at is not None and (
            not isinstance(updated_at, str) or not updated_at.strip()
        ):
            errors.append(
                f"{document_label}.updated_at must be a non-empty string when provided"
            )

        sections = document.get("sections")
        if not isinstance(sections, list) or not sections:
            errors.append(f"{document_label}.sections must be a non-empty list")
            sections = []

        for section_index, section in enumerate(sections):
            section_label = f"{document_label}.sections[{section_index}]"
            if not isinstance(section, Mapping):
                errors.append(f"{section_label} must be a mapping")
                continue

            section_id = section.get("section_id")
            if not isinstance(section_id, str) or not section_id.strip():
                errors.append(f"{section_label}.section_id must be a non-empty string")

            heading = section.get("heading")
            if not isinstance(heading, str) or not heading.strip():
                errors.append(f"{section_label}.heading must be a non-empty string")

            excerpt = section.get("excerpt")
            if not isinstance(excerpt, str) or not excerpt.strip():
                errors.append(f"{section_label}.excerpt must be a non-empty string")
                excerpt_value = ""
            else:
                excerpt_value = excerpt.strip()
                if len(excerpt_value) > DOCUMENT_SECTION_EXCERPT_MAX_CHARS:
                    errors.append(
                        f"{section_label}.excerpt must be {DOCUMENT_SECTION_EXCERPT_MAX_CHARS} chars or less"
                    )

            excerpt_truncated = section.get("excerpt_truncated")
            if excerpt_truncated is not None and not isinstance(
                excerpt_truncated, bool
            ):
                errors.append(
                    f"{section_label}.excerpt_truncated must be a boolean when provided"
                )
            elif excerpt_truncated and not excerpt_value.endswith(
                DOCUMENT_EXCERPT_TRUNCATION_MARKER
            ):
                errors.append(
                    f"{section_label}.excerpt must include explicit truncation marker when excerpt_truncated is true"
                )

            excerpt_original_char_count = section.get("excerpt_original_char_count")
            if excerpt_original_char_count is not None:
                if not isinstance(excerpt_original_char_count, int):
                    errors.append(
                        f"{section_label}.excerpt_original_char_count must be an integer when provided"
                    )
                elif excerpt_original_char_count <= 0:
                    errors.append(
                        f"{section_label}.excerpt_original_char_count must be positive when provided"
                    )
                elif excerpt_value and excerpt_original_char_count < len(excerpt_value):
                    errors.append(
                        f"{section_label}.excerpt_original_char_count must be >= excerpt length"
                    )

            citation = section.get("citation")
            if citation is not None and (
                not isinstance(citation, str) or not citation.strip()
            ):
                errors.append(
                    f"{section_label}.citation must be a non-empty string when provided"
                )

            diff_summary = section.get("diff_summary")
            if diff_summary is not None:
                if not isinstance(diff_summary, str) or not diff_summary.strip():
                    errors.append(
                        f"{section_label}.diff_summary must be a non-empty string when provided"
                    )
                elif len(diff_summary.strip()) > DOCUMENT_SECTION_DIFF_SUMMARY_MAX_CHARS:
                    errors.append(
                        f"{section_label}.diff_summary must be {DOCUMENT_SECTION_DIFF_SUMMARY_MAX_CHARS} chars or less"
                    )

            _validate_task_links(
                links=section.get("task_links"),
                label=f"{section_label}.task_links",
                errors=errors,
            )


def _validate_task_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append(f"{label}.payload.tasks must be a non-empty list")
        tasks = []

    for task_index, task in enumerate(tasks):
        task_label = f"{label}.payload.tasks[{task_index}]"
        if not isinstance(task, Mapping):
            errors.append(f"{task_label} must be a mapping")
            continue

        task_id = task.get("task_id")
        task_concept_id = task.get("task_concept_id")
        fallback_id = task.get("id")
        has_task_id = (
            isinstance(task_id, str) and bool(task_id.strip())
        ) or (
            isinstance(task_concept_id, str) and bool(task_concept_id.strip())
        ) or (isinstance(fallback_id, str) and bool(fallback_id.strip()))
        if not has_task_id:
            errors.append(
                f"{task_label} must provide a non-empty task identifier (task_id, task_concept_id, or id)"
            )

        title = task.get("title")
        label_value = task.get("label")
        has_title = (isinstance(title, str) and bool(title.strip())) or (
            isinstance(label_value, str) and bool(label_value.strip())
        )
        if not has_title:
            errors.append(
                f"{task_label} must provide a non-empty title (title or label)"
            )

        for field_name in (
            "status",
            "priority",
            "due_date",
            "assignee",
            "description",
        ):
            field_value = task.get(field_name)
            if field_value is None:
                continue
            if not isinstance(field_value, str) or not field_value.strip():
                errors.append(
                    f"{task_label}.{field_name} must be a non-empty string when provided"
                )

        _validate_task_links(
            links=task.get("task_links"),
            label=f"{task_label}.task_links",
            errors=errors,
        )


def _validate_kanban_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        errors.append(f"{label}.payload.columns must be a non-empty list")
        columns = []

    valid_column_ids: set[str] = set()
    for column_index, column in enumerate(columns):
        column_label = f"{label}.payload.columns[{column_index}]"
        if not isinstance(column, Mapping):
            errors.append(f"{column_label} must be a mapping")
            continue

        column_id = column.get("column_id")
        if not isinstance(column_id, str) or not column_id.strip():
            errors.append(f"{column_label}.column_id must be a non-empty string")
            continue
        if column_id in valid_column_ids:
            errors.append(f"{column_label}.column_id must be unique")
            continue
        valid_column_ids.add(column_id)

        column_name = column.get("label")
        if not isinstance(column_name, str) or not column_name.strip():
            errors.append(f"{column_label}.label must be a non-empty string")

        order = column.get("order")
        if order is not None and not isinstance(order, int):
            errors.append(f"{column_label}.order must be an integer when provided")

        wip_limit = column.get("wip_limit")
        if wip_limit is not None and (
            not isinstance(wip_limit, int) or wip_limit <= 0
        ):
            errors.append(
                f"{column_label}.wip_limit must be a positive integer when provided"
            )

    cards = payload.get("cards")
    if not isinstance(cards, list):
        errors.append(f"{label}.payload.cards must be a list")
        cards = []

    seen_card_ids: set[str] = set()
    for card_index, card in enumerate(cards):
        card_label = f"{label}.payload.cards[{card_index}]"
        if not isinstance(card, Mapping):
            errors.append(f"{card_label} must be a mapping")
            continue

        card_id = card.get("card_id")
        if not isinstance(card_id, str) or not card_id.strip():
            errors.append(f"{card_label}.card_id must be a non-empty string")
        elif card_id in seen_card_ids:
            errors.append(f"{card_label}.card_id must be unique")
        else:
            seen_card_ids.add(card_id)

        title = card.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{card_label}.title must be a non-empty string")

        column_id = card.get("column_id")
        if not isinstance(column_id, str) or not column_id.strip():
            errors.append(f"{card_label}.column_id must be a non-empty string")
        elif valid_column_ids and column_id not in valid_column_ids:
            errors.append(f"{card_label}.column_id must reference a declared column")

        for field_name in ("priority", "assignee", "due_date", "description"):
            field_value = card.get(field_name)
            if field_value is None:
                continue
            if not isinstance(field_value, str) or not field_value.strip():
                errors.append(
                    f"{card_label}.{field_name} must be a non-empty string when provided"
                )

        _validate_task_links(
            links=card.get("task_links"),
            label=f"{card_label}.task_links",
            errors=errors,
        )


def _validate_workflow_view_payload(
    *,
    payload: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append(f"{label}.payload.nodes must be a non-empty list")
        nodes = []

    valid_node_ids: set[str] = set()
    for node_index, node in enumerate(nodes):
        node_label = f"{label}.payload.nodes[{node_index}]"
        if not isinstance(node, Mapping):
            errors.append(f"{node_label} must be a mapping")
            continue
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(f"{node_label}.node_id must be a non-empty string")
            continue
        valid_node_ids.add(node_id)

        display_label = node.get("label")
        if not isinstance(display_label, str) or not display_label.strip():
            errors.append(f"{node_label}.label must be a non-empty string")

        status = node.get("status")
        if status is not None and (
            not isinstance(status, str) or not status.strip()
        ):
            errors.append(
                f"{node_label}.status must be a non-empty string when provided"
            )
        _validate_task_links(
            links=node.get("task_links"),
            label=f"{node_label}.task_links",
            errors=errors,
        )

    edges = payload.get("edges")
    if edges is not None and not isinstance(edges, list):
        errors.append(f"{label}.payload.edges must be a list when provided")
        edges = []
    if isinstance(edges, list):
        for edge_index, edge in enumerate(edges):
            edge_label = f"{label}.payload.edges[{edge_index}]"
            if not isinstance(edge, Mapping):
                errors.append(f"{edge_label} must be a mapping")
                continue
            source_node_id = edge.get("source_node_id")
            target_node_id = edge.get("target_node_id")
            if not isinstance(source_node_id, str) or not source_node_id.strip():
                errors.append(f"{edge_label}.source_node_id must be a non-empty string")
            elif valid_node_ids and source_node_id not in valid_node_ids:
                errors.append(
                    f"{edge_label}.source_node_id must reference a declared node"
                )
            if not isinstance(target_node_id, str) or not target_node_id.strip():
                errors.append(f"{edge_label}.target_node_id must be a non-empty string")
            elif valid_node_ids and target_node_id not in valid_node_ids:
                errors.append(
                    f"{edge_label}.target_node_id must reference a declared node"
                )


def _validate_image_payload(*, payload: Mapping[str, Any], label: str, errors: list[str]) -> None:
    if payload.get("version") != 1:
        errors.append(f"{label}.payload.version must be 1")
    asset = payload.get("asset")
    if not isinstance(asset, Mapping):
        errors.append(f"{label}.payload.asset must be a mapping")
        return
    if not isinstance(asset.get("concept_id"), str) or not asset["concept_id"].startswith("#V#"):
        errors.append(f"{label}.payload.asset.concept_id must be a retained asset reference")
    if asset.get("content_type") not in {"image/png", "image/jpeg", "image/webp"}:
        errors.append(f"{label}.payload.asset.content_type is unsupported")
    if not re.fullmatch(r"[a-f0-9]{64}", str(asset.get("sha256") or "")):
        errors.append(f"{label}.payload.asset.sha256 is invalid")
    if any(type(asset.get(key)) is not int or asset[key] <= 0 for key in ("width", "height")):
        errors.append(f"{label}.payload.asset dimensions are invalid")


_DISPLAY_ELEMENT_PAYLOAD_VALIDATORS: dict[str, Callable[..., None]] = {
    "image": _validate_image_payload,
    "calendar_view": _validate_calendar_view_payload,
    "chart_view": _validate_chart_view_payload,
    "document_view": _validate_document_view_payload,
    "hierarchy_view": _validate_hierarchy_view_payload,
    "json_block": _validate_json_block_payload,
    "kanban_view": _validate_kanban_view_payload,
    "location_view": _validate_location_view_payload,
    "relation_graph_view": _validate_relation_graph_view_payload,
    "relation_truth_state": _validate_relation_truth_state_payload,
    "table": _validate_table_payload,
    "task_view": _validate_task_view_payload,
    "text_block": _validate_text_block_payload,
    "timeline": _validate_timeline_payload,
    "workflow_view": _validate_workflow_view_payload,
}


def _validate_display_element_presentation(
    *,
    element: Mapping[str, Any],
    element_type: object,
    label: str,
    errors: list[str],
) -> None:
    presentation = element.get("presentation")
    if presentation is None:
        return
    if element_type != "table":
        errors.append(f"{label}.presentation is currently supported only for table elements")
        return
    if not isinstance(presentation, Mapping):
        errors.append(f"{label}.presentation must be a mapping when provided")
        return

    mode = presentation.get("mode")
    if not isinstance(mode, str) or mode.strip() not in _TABLE_PRESENTATION_MODES:
        errors.append(
            f"{label}.presentation.mode must be one of {sorted(_TABLE_PRESENTATION_MODES)}"
        )
        return

    if mode.strip() != "inline_primary":
        return

    source_element_id = presentation.get("source_element_id")
    if not isinstance(source_element_id, str) or not source_element_id.strip():
        errors.append(
            f"{label}.presentation.source_element_id must be a non-empty string for inline_primary"
        )

    source_span = presentation.get("source_span")
    if not isinstance(source_span, Mapping):
        errors.append(
            f"{label}.presentation.source_span must be a mapping for inline_primary"
        )
        return

    start_line = source_span.get("start_line")
    end_line = source_span.get("end_line")
    if (
        isinstance(start_line, bool)
        or not isinstance(start_line, int)
        or start_line < 1
    ):
        errors.append(
            f"{label}.presentation.source_span.start_line must be a positive integer"
        )
    if (
        isinstance(end_line, bool)
        or not isinstance(end_line, int)
        or end_line < 1
    ):
        errors.append(
            f"{label}.presentation.source_span.end_line must be a positive integer"
        )
    if (
        isinstance(start_line, int)
        and not isinstance(start_line, bool)
        and isinstance(end_line, int)
        and not isinstance(end_line, bool)
        and end_line < start_line
    ):
        errors.append(
            f"{label}.presentation.source_span.end_line must not precede start_line"
        )


def _validate_inline_primary_table_relationships(
    *,
    elements: Sequence[object],
    errors: list[str],
) -> None:
    elements_by_id: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, element in enumerate(elements):
        if not isinstance(element, Mapping):
            continue
        element_id = element.get("element_id")
        if not isinstance(element_id, str) or not element_id.strip():
            continue
        elements_by_id.setdefault(element_id.strip(), []).append((index, element))

    for index, element in enumerate(elements):
        if not isinstance(element, Mapping) or element.get("element_type") != "table":
            continue
        presentation = element.get("presentation")
        if (
            not isinstance(presentation, Mapping)
            or presentation.get("mode") != "inline_primary"
        ):
            continue

        label = f"elements[{index}].presentation"
        source_element_id = presentation.get("source_element_id")
        if not isinstance(source_element_id, str) or not source_element_id.strip():
            continue

        source_matches = elements_by_id.get(source_element_id.strip(), [])
        if len(source_matches) != 1:
            errors.append(
                f"{label}.source_element_id must identify exactly one element in the contract"
            )
            continue

        _, source_element = source_matches[0]
        if (
            source_element.get("element_type") != "text_block"
            or source_element.get("channel") != "screen"
        ):
            errors.append(
                f"{label}.source_element_id must reference a screen text_block element"
            )
            continue

        source_payload = source_element.get("payload")
        source_text = (
            source_payload.get("text")
            if isinstance(source_payload, Mapping)
            else None
        )
        if not isinstance(source_text, str) or not source_text.strip():
            errors.append(
                f"{label}.source_element_id must reference a non-empty text payload"
            )
            continue

        source_span = presentation.get("source_span")
        if not isinstance(source_span, Mapping):
            continue
        start_line = source_span.get("start_line")
        end_line = source_span.get("end_line")
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or start_line < 1
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or end_line < start_line
        ):
            continue

        source_line_count = len(source_text.splitlines())
        if end_line > source_line_count:
            errors.append(
                f"{label}.source_span must fall within the referenced text payload"
            )
            continue

        markdown_table_spans = {
            (
                candidate_span.get("start_line"),
                candidate_span.get("end_line"),
            )
            for table_payload in extract_markdown_tables(source_text)
            if isinstance(table_payload, Mapping)
            for candidate_span in [table_payload.get("source_span")]
            if isinstance(candidate_span, Mapping)
        }
        if (start_line, end_line) not in markdown_table_spans:
            errors.append(
                f"{label}.source_span must identify a Markdown table in the referenced text"
            )


def validate_turn_display_elements(
    contract: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Validate a turn-level display element contract deterministically."""
    errors: list[str] = []

    if not isinstance(contract, Mapping):
        return False, ["contract must be a mapping"]

    schema_version = contract.get("schema_version")
    if schema_version != DISPLAY_ELEMENT_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {DISPLAY_ELEMENT_SCHEMA_VERSION!r}, got {schema_version!r}"
        )

    elements = contract.get("elements")
    if not isinstance(elements, list):
        return False, [*errors, "elements must be a list"]

    for index, element in enumerate(elements):
        label = f"elements[{index}]"
        if not isinstance(element, Mapping):
            errors.append(f"{label} must be a mapping")
            continue

        element_id = element.get("element_id")
        if not isinstance(element_id, str) or not element_id.strip():
            errors.append(f"{label}.element_id must be a non-empty string")

        element_type = element.get("element_type")
        if element_type not in ALLOWED_DISPLAY_ELEMENT_TYPES:
            errors.append(
                f"{label}.element_type {element_type!r} not in allow-list {sorted(ALLOWED_DISPLAY_ELEMENT_TYPES)}"
            )

        order = element.get("order")
        if not isinstance(order, int):
            errors.append(f"{label}.order must be an integer")

        intent = element.get("intent")
        if not isinstance(intent, str) or not intent.strip():
            errors.append(f"{label}.intent must be a non-empty string")

        payload = element.get("payload")
        if not isinstance(payload, Mapping):
            errors.append(f"{label}.payload must be a mapping")
            continue

        provenance = element.get("provenance")
        if not isinstance(provenance, Mapping):
            errors.append(f"{label}.provenance must be a mapping")

        _validate_display_element_presentation(
            element=element,
            element_type=element_type,
            label=label,
            errors=errors,
        )

        if element_type in OPTIONAL_PAYLOAD_TITLE_ELEMENT_TYPES:
            _validate_optional_payload_title(
                payload=payload,
                label=label,
                errors=errors,
            )

        validator = _DISPLAY_ELEMENT_PAYLOAD_VALIDATORS.get(str(element_type))
        if validator is not None:
            validator(
                payload=payload,
                label=label,
                errors=errors,
            )
            continue

    _validate_inline_primary_table_relationships(
        elements=elements,
        errors=errors,
    )

    return len(errors) == 0, errors


def _next_unique_element_id(base: str, used_ids: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _normalise_supplied_screen_tables(
    screen_table_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen table specs.

    Accepts either:
    - raw table payloads (`columns`/`rows` at top level), or
    - wrapped element specs with `payload` + optional metadata.
    """
    if (
        not isinstance(screen_table_elements, Sequence)
        or isinstance(screen_table_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_table_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "columns" in raw_spec and "rows" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "structured_tabular_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_sort": True,
                "supports_filter": True,
                "supports_pagination": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        presentation = metadata.get("presentation")
        if not isinstance(presentation, Mapping):
            presentation = {"mode": "augment"}
        canonical_payload = _with_default_table_column_visibility(
            _with_optional_payload_title(payload, metadata=metadata)
        )

        # Keep externally supplied payloads from invalidating the whole contract.
        # Invalid payloads are dropped and surfaced via reason codes.
        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_table_probe_{index}",
                        "element_type": "table",
                        "channel": "screen",
                        "order": 16,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                        "presentation": dict(presentation),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order") if isinstance(metadata.get("order"), int) else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
                "presentation": dict(presentation),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_workflows(
    screen_workflow_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen workflow view specs."""
    if (
        not isinstance(screen_workflow_elements, Sequence)
        or isinstance(screen_workflow_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_workflow_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "nodes" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "structured_workflow_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_node_links": True,
                "supports_task_navigation": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_workflow_probe_{index}",
                        "element_type": "workflow_view",
                        "channel": "screen",
                        "order": 26,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_timelines(
    screen_timeline_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen timeline specs."""
    if (
        not isinstance(screen_timeline_elements, Sequence)
        or isinstance(screen_timeline_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_timeline_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "items" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "structured_timeline_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_item_links": True,
                "supports_relative_time": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_timeline_probe_{index}",
                        "element_type": "timeline",
                        "channel": "screen",
                        "order": 36,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_calendar_views(
    screen_calendar_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen calendar view specs."""
    if (
        not isinstance(screen_calendar_elements, Sequence)
        or isinstance(screen_calendar_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_calendar_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "items" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "structured_calendar_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_task_links": True,
                "supports_granularity_switch": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_calendar_view_probe_{index}",
                        "element_type": "calendar_view",
                        "channel": "screen",
                        "order": 38,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_chart_views(
    screen_chart_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen chart-view specs."""
    if (
        not isinstance(screen_chart_elements, Sequence)
        or isinstance(screen_chart_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_chart_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "chart_type" in raw_spec and "series" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "structured_chart_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_legend_toggle": True,
                "supports_series_comparison": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_chart_view_probe_{index}",
                        "element_type": "chart_view",
                        "channel": "screen",
                        "order": 37,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_location_views(
    screen_location_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen location-view specs."""
    if (
        not isinstance(screen_location_elements, Sequence)
        or isinstance(screen_location_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_location_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "points" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "structured_location_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_geospatial_plot": True,
                "supports_address_fallback": True,
                "supports_external_map_links": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_location_view_probe_{index}",
                        "element_type": "location_view",
                        "channel": "screen",
                        "order": 39,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_document_views(
    screen_document_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen document view specs."""
    if (
        not isinstance(screen_document_elements, Sequence)
        or isinstance(screen_document_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_document_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "documents" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "structured_document_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_section_links": True,
                "supports_citation_links": True,
                "supports_excerpt_expand": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}

        canonical_documents: list[dict[str, Any]] = []
        raw_documents = payload.get("documents")
        if isinstance(raw_documents, list):
            for document_index, raw_document in enumerate(raw_documents, start=1):
                if not isinstance(raw_document, Mapping):
                    continue

                document_id = (
                    _normalise_text(raw_document.get("document_id"))
                    or _normalise_text(raw_document.get("id"))
                    or f"document_{document_index}"
                )
                title = (
                    _normalise_text(raw_document.get("title"))
                    or _normalise_text(raw_document.get("name"))
                    or document_id
                )

                source_uri = (
                    _normalise_text(raw_document.get("source_uri"))
                    or _normalise_text(raw_document.get("uri"))
                    or _normalise_text(raw_document.get("url"))
                    or _normalise_text(raw_document.get("href"))
                )
                source_label = (
                    _normalise_text(raw_document.get("source_label"))
                    or _normalise_text(raw_document.get("source"))
                    or _normalise_text(raw_document.get("source_name"))
                    or _normalise_text(provenance.get("source_tool"))
                    or _normalise_text(provenance.get("source"))
                )
                if not source_uri and not source_label:
                    source_label = "Unknown source"

                updated_at = (
                    _normalise_text(raw_document.get("updated_at"))
                    or _normalise_text(raw_document.get("last_updated_at"))
                    or _normalise_text(raw_document.get("published"))
                    or _normalise_text(raw_document.get("created_at"))
                )

                sections: list[dict[str, Any]] = []
                raw_sections = raw_document.get("sections")
                if isinstance(raw_sections, list):
                    for section_index, raw_section in enumerate(raw_sections, start=1):
                        if not isinstance(raw_section, Mapping):
                            continue

                        section_id = (
                            _normalise_text(raw_section.get("section_id"))
                            or _normalise_text(raw_section.get("id"))
                            or f"{document_id}_section_{section_index}"
                        )
                        heading = (
                            _normalise_text(raw_section.get("heading"))
                            or _normalise_text(raw_section.get("title"))
                            or f"Section {section_index}"
                        )
                        excerpt_raw = (
                            raw_section.get("excerpt")
                            if raw_section.get("excerpt") is not None
                            else raw_section.get("text")
                        )
                        if excerpt_raw is None:
                            excerpt_raw = (
                                raw_section.get("summary")
                                if raw_section.get("summary") is not None
                                else raw_section.get("content")
                            )
                        excerpt, excerpt_truncated, original_excerpt_length = (
                            _truncate_document_text(
                                excerpt_raw,
                                max_chars=DOCUMENT_SECTION_EXCERPT_MAX_CHARS,
                            )
                        )
                        if not excerpt:
                            continue

                        section_payload: dict[str, Any] = {
                            "section_id": section_id,
                            "heading": heading,
                            "excerpt": excerpt,
                        }
                        if excerpt_truncated:
                            section_payload["excerpt_truncated"] = True
                            if isinstance(original_excerpt_length, int):
                                section_payload["excerpt_original_char_count"] = (
                                    original_excerpt_length
                                )

                        citation = (
                            _normalise_text(raw_section.get("citation"))
                            or _normalise_text(raw_section.get("source_uri"))
                            or _normalise_text(raw_section.get("source_url"))
                            or _normalise_text(raw_section.get("url"))
                            or _normalise_text(raw_section.get("href"))
                        )
                        if citation:
                            section_payload["citation"] = citation

                        diff_summary, _diff_truncated, _original_diff_length = (
                            _truncate_document_text(
                                raw_section.get("diff_summary"),
                                max_chars=DOCUMENT_SECTION_DIFF_SUMMARY_MAX_CHARS,
                            )
                        )
                        if diff_summary:
                            section_payload["diff_summary"] = diff_summary

                        task_links = raw_section.get("task_links")
                        if isinstance(task_links, list):
                            section_payload["task_links"] = list(task_links)

                        sections.append(section_payload)

                if not sections:
                    continue

                document_payload: dict[str, Any] = {
                    "document_id": document_id,
                    "title": title,
                    "sections": sections,
                }
                if source_uri:
                    document_payload["source_uri"] = source_uri
                if source_label:
                    document_payload["source_label"] = source_label
                if updated_at:
                    document_payload["updated_at"] = updated_at
                canonical_documents.append(document_payload)

        canonical_payload = _with_optional_payload_title(
            {"documents": canonical_documents},
            metadata=metadata,
        )

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_document_view_probe_{index}",
                        "element_type": "document_view",
                        "channel": "screen",
                        "order": 40,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_task_views(
    screen_task_view_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen task view specs."""
    if (
        not isinstance(screen_task_view_elements, Sequence)
        or isinstance(screen_task_view_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_task_view_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "tasks" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "structured_task_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_task_links": True,
                "supports_status_badges": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_task_view_probe_{index}",
                        "element_type": "task_view",
                        "channel": "screen",
                        "order": 31,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_kanban_views(
    screen_kanban_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen kanban view specs."""
    if (
        not isinstance(screen_kanban_elements, Sequence)
        or isinstance(screen_kanban_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_kanban_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "columns" in raw_spec and "cards" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = _normalise_text(metadata.get("intent")) or "structured_kanban_view"
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_column_grouping": True,
                "supports_task_links": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_kanban_view_probe_{index}",
                        "element_type": "kanban_view",
                        "channel": "screen",
                        "order": 33,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_relation_graph_views(
    screen_relation_graph_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen relation graph specs."""
    if (
        not isinstance(screen_relation_graph_elements, Sequence)
        or isinstance(screen_relation_graph_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_relation_graph_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "nodes" in raw_spec and "edges" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = _normalise_text(metadata.get("intent")) or "relation_graph_view"
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_pan_zoom": True,
                "supports_clickthrough": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_relation_graph_view_probe_{index}",
                        "element_type": "relation_graph_view",
                        "channel": "screen",
                        "order": 43,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_hierarchy_views(
    screen_hierarchy_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied screen hierarchy specs."""
    if (
        not isinstance(screen_hierarchy_elements, Sequence)
        or isinstance(screen_hierarchy_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_hierarchy_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "nodes" in raw_spec and "edges" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = _normalise_text(metadata.get("intent")) or "hierarchy_view"
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_neighbourhood_toggle": True,
                "supports_focus_navigation": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_hierarchy_view_probe_{index}",
                        "element_type": "hierarchy_view",
                        "channel": "screen",
                        "order": 42,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_relation_truth_states(
    screen_relation_truth_state_elements: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Normalise externally supplied relation truth-state display specs."""
    if (
        not isinstance(screen_relation_truth_state_elements, Sequence)
        or isinstance(screen_relation_truth_state_elements, (str, bytes, bytearray))
    ):
        return [], 0

    normalised: list[dict[str, Any]] = []
    dropped_count = 0

    for index, raw_spec in enumerate(screen_relation_truth_state_elements, start=1):
        if not isinstance(raw_spec, Mapping):
            dropped_count += 1
            continue

        payload: Mapping[str, Any] | None = None
        metadata = raw_spec
        wrapped_payload = raw_spec.get("payload")
        if isinstance(wrapped_payload, Mapping):
            payload = wrapped_payload
        elif "groups" in raw_spec:
            payload = raw_spec

        if not isinstance(payload, Mapping):
            dropped_count += 1
            continue

        intent = (
            _normalise_text(metadata.get("intent"))
            or "truth_state_relation_view"
        )
        constraints = metadata.get("constraints")
        if not isinstance(constraints, Mapping):
            constraints = {
                "supports_compact_cartouches": True,
                "supports_assertion_variants": True,
            }
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        canonical_payload = _with_optional_payload_title(payload, metadata=metadata)

        is_valid, _errors = validate_turn_display_elements(
            {
                "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
                "elements": [
                    {
                        "element_id": f"screen_structured_relation_truth_state_probe_{index}",
                        "element_type": "relation_truth_state",
                        "channel": "screen",
                        "order": 41,
                        "intent": intent,
                        "payload": dict(canonical_payload),
                        "constraints": dict(constraints),
                        "provenance": dict(provenance),
                    }
                ],
                "reason_codes": [],
            }
        )
        if not is_valid:
            dropped_count += 1
            continue

        normalised.append(
            {
                "element_id": _normalise_text(metadata.get("element_id")),
                "order": metadata.get("order")
                if isinstance(metadata.get("order"), int)
                else None,
                "intent": intent,
                "payload": dict(canonical_payload),
                "constraints": dict(constraints),
                "provenance": dict(provenance),
            }
        )

    return normalised, dropped_count


def _normalise_supplied_screen_family(
    raw_elements: Sequence[Mapping[str, Any]] | None,
    *,
    normaliser: Callable[
        [Sequence[Mapping[str, Any]] | None], tuple[list[dict[str, Any]], int]
    ],
    reason_codes: list[str],
    supplied_reason_code: str,
    dropped_reason_code: str,
) -> list[dict[str, Any]]:
    normalised, dropped_count = normaliser(raw_elements)
    if normalised:
        reason_codes.append(supplied_reason_code)
    if dropped_count:
        reason_codes.append(dropped_reason_code)
    return normalised


def _build_structured_screen_specs(
    *,
    supplied_specs: Sequence[Mapping[str, Any]],
    default_source: str,
    index_key: str,
    default_element_id_prefix: str,
    default_intent: str,
    default_constraints: Mapping[str, Any],
) -> list[dict[str, Any]]:
    structured_specs: list[dict[str, Any]] = []
    for index, spec in enumerate(supplied_specs, start=1):
        provenance = dict(spec.get("provenance") or {})
        provenance.setdefault("source", default_source)
        provenance.setdefault(index_key, index)
        structured_specs.append(
            {
                "element_id": spec.get("element_id")
                or f"{default_element_id_prefix}_{index}",
                "order": spec.get("order"),
                "intent": spec.get("intent") or default_intent,
                "payload": spec.get("payload") or {},
                "constraints": spec.get("constraints") or dict(default_constraints),
                "provenance": provenance,
                "presentation": (
                    dict(spec["presentation"])
                    if isinstance(spec.get("presentation"), Mapping)
                    else None
                ),
            }
        )
    return structured_specs


def _collect_explicit_structured_orders(
    *spec_groups: Sequence[Mapping[str, Any]],
) -> set[int]:
    used_orders: set[int] = set()
    for spec_group in spec_groups:
        for spec in spec_group:
            order = spec.get("order")
            if isinstance(order, int):
                used_orders.add(int(order))
    return used_orders


def _emit_structured_screen_elements(
    *,
    elements: list[dict[str, Any]],
    specs: Sequence[Mapping[str, Any]],
    element_type: str,
    default_order_start: int,
    used_ids: set[str],
    used_orders: set[int],
) -> None:
    next_default_order = default_order_start
    for spec in specs:
        order = spec.get("order") if isinstance(spec.get("order"), int) else None
        if order is None:
            while next_default_order in used_orders:
                next_default_order += 1
            order = next_default_order
            used_orders.add(order)
            next_default_order += 1

        element_id = _next_unique_element_id(str(spec["element_id"]), used_ids)
        element = {
            "element_id": element_id,
            "element_type": element_type,
            "channel": "screen",
            "order": int(order),
            "intent": str(spec["intent"]),
            "payload": dict(spec["payload"]),
            "constraints": dict(spec["constraints"]),
            "provenance": dict(spec["provenance"]),
        }
        presentation = spec.get("presentation")
        if isinstance(presentation, Mapping):
            element["presentation"] = dict(presentation)
        elements.append(element)


def build_turn_display_elements(
    *,
    response_text: str | None,
    presenter_channels: Mapping[str, Any] | None,
    content_parts: Sequence[Mapping[str, Any]] | None = None,
    screen_table_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_workflow_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_task_view_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_calendar_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_chart_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_location_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_document_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_kanban_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_timeline_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_hierarchy_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_relation_graph_elements: Sequence[Mapping[str, Any]] | None = None,
    screen_relation_truth_state_elements: Sequence[Mapping[str, Any]] | None = None,
    supplemental_reason_codes: Sequence[str] | None = None,
    required_screen_json_fence: str | None = None,
    screen_backfill_second_pass_attempted: bool = False,
    screen_backfill_second_pass_reason: str | None = None,
    spoken_backfill_second_pass_attempted: bool = False,
    spoken_backfill_second_pass_reason: str | None = None,
) -> dict[str, Any]:
    """Build canonical display elements for a single response turn."""
    presenter_format = (
        presenter_channels.get("format")
        if isinstance(presenter_channels.get("format"), str)
        else None
    ) if isinstance(presenter_channels, Mapping) else None

    screen_text = (
        _normalise_text(presenter_channels.get("screen"))
        if isinstance(presenter_channels, Mapping)
        else None
    )
    spoken_text = (
        _normalise_text(presenter_channels.get("spoken"))
        if isinstance(presenter_channels, Mapping)
        else None
    )
    fallback_response_text = _normalise_text(response_text)

    reason_codes: list[str] = []
    if screen_backfill_second_pass_attempted:
        reason = _normalise_text(screen_backfill_second_pass_reason) or "unspecified"
        reason_codes.append(f"screen_backfill:{reason}")
    if spoken_backfill_second_pass_attempted:
        reason = _normalise_text(spoken_backfill_second_pass_reason) or "unspecified"
        reason_codes.append(f"spoken_backfill:{reason}")
    if (
        isinstance(supplemental_reason_codes, Sequence)
        and not isinstance(supplemental_reason_codes, (str, bytes, bytearray))
    ):
        for raw_reason_code in supplemental_reason_codes:
            reason = _normalise_text(raw_reason_code)
            if reason:
                reason_codes.append(reason)

    elements: list[dict[str, Any]] = []

    effective_screen = screen_text or fallback_response_text
    if effective_screen:
        screen_source = (
            "screen_backfill"
            if screen_backfill_second_pass_attempted
            else ("presenter_channel" if screen_text else "response_text")
        )
        elements.append(
            {
                "element_id": "screen_text",
                "element_type": "text_block",
                "channel": "screen",
                "order": 10,
                "intent": "primary_response",
                "payload": {"text": effective_screen},
                "constraints": {"preserve_fences": True},
                "provenance": {
                    "source": screen_source,
                    "presenter_format": presenter_format,
                    "reason_code": _normalise_text(screen_backfill_second_pass_reason),
                },
            }
        )

    if content_parts:
        import hashlib
        # Explicit ordered rich output owns the primary screen placement. The
        # legacy text projection and other structured families remain available.
        elements = [element for element in elements if element["element_id"] != "screen_text"]
        seen = set()
        for index, part in enumerate(content_parts):
            part_id = str(part.get("part_id") or f"part-{index}")
            if part_id in seen:
                continue
            seen.add(part_id)
            kind = part.get("kind")
            payload = {"text": str(part.get("text") or "")}
            element_type = "text_block"
            if kind == "image" and part.get("version") == 1:
                from .conversation_output_service import public_asset
                payload = {"version": 1, "asset": public_asset(part.get("asset") or {})}
                validation_errors: list[str] = []
                _validate_image_payload(payload=payload, label=part_id, errors=validation_errors)
                if not validation_errors:
                    element_type = "image"
                else:
                    payload = {"text": "Image reference could not be displayed."}
            elif kind not in {"text", "media_error"} or part.get("version") != 1:
                payload = {"text": f"Unsupported visual format: {kind} (version {part.get('version')})."}
            elements.append({"element_id": "content_" + hashlib.sha256(part_id.encode()).hexdigest()[:20],
                             "element_type": element_type, "channel": "screen", "order": 10 + index,
                             "intent": "primary_response", "payload": payload,
                             "provenance": {"source": "retained_content", "part_id": part_id}})

    if spoken_text:
        spoken_source = (
            "spoken_backfill"
            if spoken_backfill_second_pass_attempted
            else "presenter_channel"
        )
        elements.append(
            {
                "element_id": "spoken_text",
                "element_type": "text_block",
                "channel": "spoken",
                "order": 20,
                "intent": "narration",
                "payload": {"text": spoken_text},
                "constraints": {"tts_ready": True},
                "provenance": {
                    "source": spoken_source,
                    "presenter_format": presenter_format,
                    "reason_code": _normalise_text(spoken_backfill_second_pass_reason),
                },
            }
        )

    json_fences = extract_json_fences(effective_screen)
    required_fence = _normalise_text(required_screen_json_fence)
    if required_fence and required_fence not in json_fences:
        json_fences.append(required_fence)
        reason_codes.append("required_screen_json_fence_appended")

    for index, fence in enumerate(json_fences, start=1):
        elements.append(
            {
                "element_id": f"screen_json_block_{index}",
                "element_type": "json_block",
                "channel": "screen",
                "order": 10 + index,
                "intent": "verbatim_json_payload",
                "payload": {
                    "fence": fence,
                    "body": _extract_json_body_from_fence(fence),
                },
                "constraints": {"must_preserve_verbatim": True},
                "provenance": {
                    "source": (
                        "required_prompt_fence"
                        if required_fence and fence == required_fence
                        else "screen_text_fence"
                    ),
                    "required_by_user_prompt": bool(required_fence and fence == required_fence),
                },
            }
        )

    supplied_screen_tables = _normalise_supplied_screen_family(
        screen_table_elements,
        normaliser=_normalise_supplied_screen_tables,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_tables_supplied",
        dropped_reason_code="screen_structured_tables_invalid_dropped",
    )
    supplied_screen_workflows = _normalise_supplied_screen_family(
        screen_workflow_elements,
        normaliser=_normalise_supplied_screen_workflows,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_workflows_supplied",
        dropped_reason_code="screen_structured_workflows_invalid_dropped",
    )
    supplied_screen_task_views = _normalise_supplied_screen_family(
        screen_task_view_elements,
        normaliser=_normalise_supplied_screen_task_views,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_task_views_supplied",
        dropped_reason_code="screen_structured_task_views_invalid_dropped",
    )
    supplied_screen_calendar_views = _normalise_supplied_screen_family(
        screen_calendar_elements,
        normaliser=_normalise_supplied_screen_calendar_views,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_calendar_views_supplied",
        dropped_reason_code="screen_structured_calendar_views_invalid_dropped",
    )
    supplied_screen_chart_views = _normalise_supplied_screen_family(
        screen_chart_elements,
        normaliser=_normalise_supplied_screen_chart_views,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_chart_views_supplied",
        dropped_reason_code="screen_structured_chart_views_invalid_dropped",
    )
    supplied_screen_location_views = _normalise_supplied_screen_family(
        screen_location_elements,
        normaliser=_normalise_supplied_screen_location_views,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_location_views_supplied",
        dropped_reason_code="screen_structured_location_views_invalid_dropped",
    )
    supplied_screen_document_views = _normalise_supplied_screen_family(
        screen_document_elements,
        normaliser=_normalise_supplied_screen_document_views,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_document_views_supplied",
        dropped_reason_code="screen_structured_document_views_invalid_dropped",
    )
    supplied_screen_kanban_views = _normalise_supplied_screen_family(
        screen_kanban_elements,
        normaliser=_normalise_supplied_screen_kanban_views,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_kanban_views_supplied",
        dropped_reason_code="screen_structured_kanban_views_invalid_dropped",
    )
    supplied_screen_timelines = _normalise_supplied_screen_family(
        screen_timeline_elements,
        normaliser=_normalise_supplied_screen_timelines,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_timelines_supplied",
        dropped_reason_code="screen_structured_timelines_invalid_dropped",
    )
    supplied_screen_hierarchy_views = _normalise_supplied_screen_family(
        screen_hierarchy_elements,
        normaliser=_normalise_supplied_screen_hierarchy_views,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_hierarchy_views_supplied",
        dropped_reason_code="screen_structured_hierarchy_views_invalid_dropped",
    )
    supplied_screen_relation_graph_views = _normalise_supplied_screen_family(
        screen_relation_graph_elements,
        normaliser=_normalise_supplied_screen_relation_graph_views,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_relation_graph_views_supplied",
        dropped_reason_code="screen_structured_relation_graph_views_invalid_dropped",
    )
    supplied_screen_relation_truth_states = _normalise_supplied_screen_family(
        screen_relation_truth_state_elements,
        normaliser=_normalise_supplied_screen_relation_truth_states,
        reason_codes=reason_codes,
        supplied_reason_code="screen_structured_relation_truth_states_supplied",
        dropped_reason_code="screen_structured_relation_truth_states_invalid_dropped",
    )

    table_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_tables,
        default_source="screen_structured_table",
        index_key="table_index",
        default_element_id_prefix="screen_structured_table",
        default_intent="structured_tabular_view",
        default_constraints={
            "supports_sort": True,
            "supports_filter": True,
            "supports_pagination": True,
        },
    )

    markdown_tables = extract_markdown_tables(effective_screen)
    for index, table_payload in enumerate(markdown_tables, start=1):
        source_span = table_payload.get("source_span")
        table_specs.append(
            {
                "element_id": f"screen_table_{index}",
                "order": None,
                "intent": "structured_tabular_view",
                "payload": table_payload,
                "constraints": {
                    "supports_sort": True,
                    "supports_filter": True,
                    "supports_pagination": True,
                },
                "provenance": {
                    "source": "screen_markdown_table",
                    "table_index": index,
                    "required_by_user_prompt": False,
                },
                "presentation": {
                    "mode": "inline_primary",
                    "source_element_id": "screen_text",
                    "source_span": (
                        dict(source_span)
                        if isinstance(source_span, Mapping)
                        else {}
                    ),
                },
            }
        )
    if markdown_tables:
        reason_codes.append("screen_markdown_tables_detected")

    workflow_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_workflows,
        default_source="screen_structured_workflow",
        index_key="workflow_index",
        default_element_id_prefix="screen_structured_workflow",
        default_intent="structured_workflow_view",
        default_constraints={
            "supports_node_links": True,
            "supports_task_navigation": True,
        },
    )
    task_view_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_task_views,
        default_source="screen_structured_task_view",
        index_key="task_view_index",
        default_element_id_prefix="screen_structured_task_view",
        default_intent="structured_task_view",
        default_constraints={
            "supports_task_links": True,
            "supports_status_badges": True,
        },
    )
    calendar_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_calendar_views,
        default_source="screen_structured_calendar_view",
        index_key="calendar_index",
        default_element_id_prefix="screen_structured_calendar_view",
        default_intent="structured_calendar_view",
        default_constraints={
            "supports_task_links": True,
            "supports_granularity_switch": True,
        },
    )
    chart_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_chart_views,
        default_source="screen_structured_chart_view",
        index_key="chart_view_index",
        default_element_id_prefix="screen_structured_chart_view",
        default_intent="structured_chart_view",
        default_constraints={
            "supports_legend_toggle": True,
            "supports_series_comparison": True,
        },
    )

    location_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_location_views,
        default_source="screen_structured_location_view",
        index_key="location_view_index",
        default_element_id_prefix="screen_structured_location_view",
        default_intent="structured_location_view",
        default_constraints={
            "supports_geospatial_plot": True,
            "supports_address_fallback": True,
            "supports_external_map_links": True,
        },
    )
    document_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_document_views,
        default_source="screen_structured_document_view",
        index_key="document_view_index",
        default_element_id_prefix="screen_structured_document_view",
        default_intent="structured_document_view",
        default_constraints={
            "supports_section_links": True,
            "supports_citation_links": True,
            "supports_excerpt_expand": True,
        },
    )
    kanban_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_kanban_views,
        default_source="screen_structured_kanban_view",
        index_key="kanban_index",
        default_element_id_prefix="screen_structured_kanban_view",
        default_intent="structured_kanban_view",
        default_constraints={
            "supports_column_grouping": True,
            "supports_task_links": True,
        },
    )
    timeline_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_timelines,
        default_source="screen_structured_timeline",
        index_key="timeline_index",
        default_element_id_prefix="screen_structured_timeline",
        default_intent="structured_timeline_view",
        default_constraints={
            "supports_item_links": True,
            "supports_relative_time": True,
        },
    )
    hierarchy_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_hierarchy_views,
        default_source="screen_structured_hierarchy_view",
        index_key="hierarchy_index",
        default_element_id_prefix="screen_structured_hierarchy_view",
        default_intent="hierarchy_view",
        default_constraints={
            "supports_neighbourhood_toggle": True,
            "supports_focus_navigation": True,
        },
    )
    relation_graph_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_relation_graph_views,
        default_source="screen_structured_relation_graph_view",
        index_key="relation_graph_index",
        default_element_id_prefix="screen_structured_relation_graph_view",
        default_intent="relation_graph_view",
        default_constraints={
            "supports_pan_zoom": True,
            "supports_clickthrough": True,
        },
    )
    relation_truth_state_specs = _build_structured_screen_specs(
        supplied_specs=supplied_screen_relation_truth_states,
        default_source="screen_structured_relation_truth_state",
        index_key="relation_truth_state_index",
        default_element_id_prefix="screen_structured_relation_truth_state",
        default_intent="truth_state_relation_view",
        default_constraints={
            "supports_compact_cartouches": True,
            "supports_assertion_variants": True,
        },
    )

    used_ids: set[str] = set()
    used_orders = _collect_explicit_structured_orders(
        table_specs,
        workflow_specs,
        task_view_specs,
        chart_specs,
        calendar_specs,
        location_specs,
        document_specs,
        kanban_specs,
        timeline_specs,
        hierarchy_specs,
        relation_graph_specs,
        relation_truth_state_specs,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=table_specs,
        element_type="table",
        default_order_start=16,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=timeline_specs,
        element_type="timeline",
        default_order_start=36,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=chart_specs,
        element_type="chart_view",
        default_order_start=37,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=calendar_specs,
        element_type="calendar_view",
        default_order_start=38,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=location_specs,
        element_type="location_view",
        default_order_start=39,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=document_specs,
        element_type="document_view",
        default_order_start=40,
        used_ids=used_ids,
        used_orders=used_orders,
    )

    _emit_structured_screen_elements(
        elements=elements,
        specs=workflow_specs,
        element_type="workflow_view",
        default_order_start=26,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=task_view_specs,
        element_type="task_view",
        default_order_start=31,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=kanban_specs,
        element_type="kanban_view",
        default_order_start=33,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=relation_graph_specs,
        element_type="relation_graph_view",
        default_order_start=43,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=hierarchy_specs,
        element_type="hierarchy_view",
        default_order_start=42,
        used_ids=used_ids,
        used_orders=used_orders,
    )
    _emit_structured_screen_elements(
        elements=elements,
        specs=relation_truth_state_specs,
        element_type="relation_truth_state",
        default_order_start=41,
        used_ids=used_ids,
        used_orders=used_orders,
    )

    # Emit elements in the same deterministic order signalled by `order`.
    # This keeps downstream renderers and regressions aligned on one sequence.
    elements.sort(key=lambda item: (int(item.get("order", 0)), str(item.get("element_id", ""))))

    reason_codes = _dedupe_preserve_order(reason_codes)

    contract: dict[str, Any] = {
        "schema_version": DISPLAY_ELEMENT_SCHEMA_VERSION,
        "elements": elements,
        "reason_codes": reason_codes,
    }
    valid, errors = validate_turn_display_elements(contract)
    contract["validation"] = {
        "valid": valid,
        "errors": errors,
    }
    return contract
