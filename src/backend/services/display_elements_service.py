"""Canonical turn-level display element contract for chat responses.

JVNAUTOSCI-1149 introduces a first-class response-construction model for
display elements so rendering decisions are explicit and inspectable.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
import re

DISPLAY_ELEMENT_SCHEMA_VERSION = "turn_display_elements_v1"

# Keep this allow-list explicit so element admission is deterministic and
# renderer integration can evolve without ad hoc shape drift.
ALLOWED_DISPLAY_ELEMENT_TYPES = frozenset(
    {
        "text_block",
        "json_block",
        "table",
        "timeline",
        "task_view",
        "workflow_view",
    }
)

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
_NUMBER_PATTERN = re.compile(r"^-?(?:\d+|\d+\.\d+)$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalise_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


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


def extract_markdown_tables(text: str | None) -> list[dict[str, Any]]:
    """Extract structured markdown tables from non-code-fenced text."""
    if not isinstance(text, str) or not text.strip():
        return []

    # Avoid treating pipe-delimited content inside code fences as table rows.
    sanitised = _FENCED_CODE_BLOCK_PATTERN.sub("", text)
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
        columns: list[dict[str, Any]] = []
        for column_index, label in enumerate(header_cells):
            column_id = _build_column_id(label, column_index, used_column_ids)
            columns.append(
                {
                    "column_id": column_id,
                    "label": label or f"Column {column_index + 1}",
                    "data_type": "mixed",
                    "position": column_index,
                }
            )

        rows: list[dict[str, Any]] = []
        for row_index, values in enumerate(row_values, start=1):
            cells: list[dict[str, Any]] = []
            for column_index, value in enumerate(values):
                column_id = columns[column_index]["column_id"]
                cleaned = value.strip()
                cells.append(
                    {
                        "column_id": column_id,
                        "value_raw": cleaned,
                        "value_display": cleaned,
                        "value_type": _infer_cell_value_type(cleaned),
                    }
                )
            rows.append(
                {
                    "row_id": f"row_{row_index}",
                    "cells": cells,
                }
            )

        tables.append(
            {
                "columns": columns,
                "rows": rows,
                "sort": {
                    "default_column_id": columns[0]["column_id"] if columns else None,
                    "direction": "asc",
                },
                "filters": [],
                "pagination": {
                    "enabled": True,
                    "page_size": min(100, len(rows)),
                    "total_rows": len(rows),
                },
                "source_span": {
                    "start_line": index + 1,
                    "end_line": cursor,
                },
            }
        )
        index = cursor

    return tables


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

        if element_type == "text_block":
            text_value = payload.get("text")
            if not isinstance(text_value, str) or not text_value.strip():
                errors.append(f"{label}.payload.text must be a non-empty string")
        elif element_type == "json_block":
            fence = payload.get("fence")
            if not isinstance(fence, str) or not fence.strip():
                errors.append(f"{label}.payload.fence must be a non-empty string")
            elif "```json" not in fence.lower():
                errors.append(f"{label}.payload.fence must be a fenced JSON block")
        elif element_type == "table":
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

    return len(errors) == 0, errors


def build_turn_display_elements(
    *,
    response_text: str | None,
    presenter_channels: Mapping[str, Any] | None,
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

    markdown_tables = extract_markdown_tables(effective_screen)
    for index, table_payload in enumerate(markdown_tables, start=1):
        elements.append(
            {
                "element_id": f"screen_table_{index}",
                "element_type": "table",
                "channel": "screen",
                "order": 15 + index,
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
            }
        )
    if markdown_tables:
        reason_codes.append("screen_markdown_tables_detected")

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
