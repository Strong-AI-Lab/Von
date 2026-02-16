from __future__ import annotations

from src.backend.services.display_elements_service import (
    build_turn_display_elements,
    extract_markdown_tables,
    validate_turn_display_elements,
)


def test_build_turn_display_elements_includes_screen_and_spoken_blocks() -> None:
    contract = build_turn_display_elements(
        response_text="Fallback response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "On-screen text",
            "spoken": "Talk track",
        },
    )

    assert contract["schema_version"] == "turn_display_elements_v1"
    assert contract["validation"]["valid"] is True
    elements = contract["elements"]
    assert isinstance(elements, list)

    screen = next(item for item in elements if item["element_id"] == "screen_text")
    spoken = next(item for item in elements if item["element_id"] == "spoken_text")
    assert screen["payload"]["text"] == "On-screen text"
    assert spoken["payload"]["text"] == "Talk track"
    assert contract["reason_codes"] == []


def test_build_turn_display_elements_appends_required_json_fence() -> None:
    required_fence = "```json\n{\"sentinel\": \"required\"}\n```"
    contract = build_turn_display_elements(
        response_text="Screen text without json block",
        presenter_channels={
            "format": "screen_backfill_from_response_v1",
            "screen": "Screen text without json block",
            "spoken": None,
        },
        required_screen_json_fence=required_fence,
        screen_backfill_second_pass_attempted=True,
        screen_backfill_second_pass_reason="missing_screen_fence",
    )

    assert contract["validation"]["valid"] is True
    json_blocks = [
        item for item in contract["elements"] if item["element_type"] == "json_block"
    ]
    assert len(json_blocks) == 1
    assert json_blocks[0]["payload"]["fence"] == required_fence
    assert "screen_backfill:missing_screen_fence" in contract["reason_codes"]
    assert "required_screen_json_fence_appended" in contract["reason_codes"]


def test_validate_turn_display_elements_rejects_unknown_element_type() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "bad",
                    "element_type": "unsupported_kind",
                    "channel": "screen",
                    "order": 10,
                    "intent": "primary_response",
                    "payload": {"text": "Hello"},
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any("unsupported_kind" in message for message in errors)


def test_build_turn_display_elements_orders_elements_deterministically() -> None:
    required_fence = "```json\n{\"sentinel\": \"required\"}\n```"
    contract = build_turn_display_elements(
        response_text=(
            "Screen content\n"
            "```json\n"
            "{\"existing\": true}\n"
            "```"
        ),
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": (
                "Screen content\n"
                "```json\n"
                "{\"existing\": true}\n"
                "```"
            ),
            "spoken": "Talk track",
        },
        required_screen_json_fence=required_fence,
    )

    element_ids = [element["element_id"] for element in contract["elements"]]
    assert element_ids == [
        "screen_text",
        "screen_json_block_1",
        "screen_json_block_2",
        "spoken_text",
    ]

    orders = [element["order"] for element in contract["elements"]]
    assert orders == sorted(orders)


def test_extract_markdown_tables_parses_rows_and_typed_cells() -> None:
    tables = extract_markdown_tables(
        (
            "Summary table:\n\n"
            "| Task | Status | Due Date | Progress |\n"
            "| --- | --- | --- | --- |\n"
            "| Alpha | done | 2026-02-16 | 1.0 |\n"
            "| Beta | true | 2026-03-01 | 0.25 |\n"
        )
    )

    assert len(tables) == 1
    table = tables[0]
    assert [column["label"] for column in table["columns"]] == [
        "Task",
        "Status",
        "Due Date",
        "Progress",
    ]
    assert table["rows"][0]["row_id"] == "row_1"
    first_row_types = [cell["value_type"] for cell in table["rows"][0]["cells"]]
    assert first_row_types == ["text", "text", "date", "number"]
    second_row_types = [cell["value_type"] for cell in table["rows"][1]["cells"]]
    assert second_row_types == ["text", "boolean", "date", "number"]


def test_build_turn_display_elements_includes_table_elements_for_markdown_tables() -> None:
    contract = build_turn_display_elements(
        response_text=(
            "| Task | Status |\n"
            "| --- | --- |\n"
            "| Alpha | done |\n"
            "| Beta | in_progress |\n"
        ),
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": (
                "| Task | Status |\n"
                "| --- | --- |\n"
                "| Alpha | done |\n"
                "| Beta | in_progress |\n"
            ),
            "spoken": "Here are the tasks.",
        },
    )

    table_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "table"
    ]
    assert len(table_elements) == 1
    table_element = table_elements[0]
    assert table_element["payload"]["columns"][0]["label"] == "Task"
    assert table_element["payload"]["rows"][0]["cells"][1]["value_raw"] == "done"
    assert "screen_markdown_tables_detected" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_validate_turn_display_elements_rejects_invalid_table_shape() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_table_1",
                    "element_type": "table",
                    "channel": "screen",
                    "order": 15,
                    "intent": "structured_tabular_view",
                    "payload": {
                        "columns": [
                            {
                                "column_id": "task",
                                "label": "Task",
                                "data_type": "text",
                            }
                        ],
                        "rows": [
                            {
                                "row_id": "row_1",
                                "cells": [
                                    {
                                        "column_id": "missing_column",
                                        "value_raw": "Alpha",
                                        "value_display": "Alpha",
                                        "value_type": "text",
                                    }
                                ],
                            }
                        ],
                    },
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any("must reference a declared column" in message for message in errors)
