from __future__ import annotations

from src.backend.services.display_elements_service import (
    build_canonical_table_payload_from_records,
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


def test_build_turn_display_elements_includes_supplied_table_elements() -> None:
    supplied_table_payload = build_canonical_table_payload_from_records(
        records=[
            {
                "task_id": "task_alpha",
                "task_name": "Alpha",
                "status": "done",
                "source": {"source_concept_id": "#V#task_alpha"},
            }
        ],
        columns=[
            {"column_id": "task_name", "label": "Task", "source_key": "task_name"},
            {"column_id": "status", "label": "Status", "source_key": "status"},
        ],
        row_id_field="task_id",
        row_provenance_field="source",
    )
    contract = build_turn_display_elements(
        response_text="Structured task output",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Structured task output",
            "spoken": "Here is the task view.",
        },
        screen_table_elements=[supplied_table_payload],
    )

    table_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "table"
    ]
    assert len(table_elements) == 1
    table_element = table_elements[0]
    assert table_element["element_id"] == "screen_structured_table_1"
    assert table_element["payload"]["rows"][0]["row_id"] == "task_alpha"
    assert (
        table_element["payload"]["rows"][0]["provenance"]["source_concept_id"]
        == "#V#task_alpha"
    )
    assert "screen_structured_tables_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_supplied_tables() -> None:
    contract = build_turn_display_elements(
        response_text="Task response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Task response",
            "spoken": None,
        },
        screen_table_elements=[
            {
                "payload": {
                    "columns": [],
                    "rows": [],
                }
            }
        ],
    )

    table_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "table"
    ]
    assert table_elements == []
    assert "screen_structured_tables_invalid_dropped" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_includes_supplied_workflow_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Workflow response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Workflow response",
            "spoken": None,
        },
        screen_workflow_elements=[
            {
                "element_id": "screen_workflow_view",
                "intent": "structured_workflow_view",
                "payload": {
                    "layout": "list",
                    "nodes": [
                        {
                            "node_id": "inst_1",
                            "label": "#V#salient_predicate_governance_workflow",
                            "status": "running",
                            "task_links": [
                                {
                                    "link_type": "von_task",
                                    "target_id": "#V#task_alpha",
                                    "label": "#V#task_alpha",
                                },
                                {
                                    "link_type": "jira_issue",
                                    "target_id": "JVNAUTOSCI-1148",
                                    "label": "JVNAUTOSCI-1148",
                                    "href": "https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1148",
                                },
                            ],
                        }
                    ],
                    "edges": [],
                },
            }
        ],
    )

    workflow_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "workflow_view"
    ]
    assert len(workflow_elements) == 1
    workflow_element = workflow_elements[0]
    assert workflow_element["element_id"] == "screen_workflow_view"
    assert workflow_element["payload"]["nodes"][0]["node_id"] == "inst_1"
    assert (
        workflow_element["payload"]["nodes"][0]["task_links"][0]["target_id"]
        == "#V#task_alpha"
    )
    assert "screen_structured_workflows_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_supplied_workflows() -> None:
    contract = build_turn_display_elements(
        response_text="Workflow response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Workflow response",
            "spoken": None,
        },
        screen_workflow_elements=[
            {
                "payload": {
                    "layout": "list",
                    "nodes": [],
                    "edges": [],
                }
            }
        ],
    )

    workflow_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "workflow_view"
    ]
    assert workflow_elements == []
    assert "screen_structured_workflows_invalid_dropped" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_includes_supplied_timeline_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Timeline response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Timeline response",
            "spoken": None,
        },
        screen_timeline_elements=[
            {
                "element_id": "screen_timeline_view",
                "intent": "structured_timeline_view",
                "payload": {
                    "items": [
                        {
                            "item_id": "event_1",
                            "label": "Task created",
                            "start_at": "2026-02-17T09:10:00Z",
                            "status": "pending",
                            "task_links": [
                                {
                                    "link_type": "von_task",
                                    "target_id": "#V#task_alpha",
                                    "label": "#V#task_alpha",
                                },
                                {
                                    "link_type": "jira_issue",
                                    "target_id": "JVNAUTOSCI-1138",
                                    "label": "JVNAUTOSCI-1138",
                                    "href": "https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1138",
                                },
                            ],
                        }
                    ]
                },
            }
        ],
    )

    timeline_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "timeline"
    ]
    assert len(timeline_elements) == 1
    timeline_element = timeline_elements[0]
    assert timeline_element["element_id"] == "screen_timeline_view"
    assert timeline_element["payload"]["items"][0]["item_id"] == "event_1"
    assert (
        timeline_element["payload"]["items"][0]["task_links"][0]["target_id"]
        == "#V#task_alpha"
    )
    assert "screen_structured_timelines_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_supplied_timelines() -> None:
    contract = build_turn_display_elements(
        response_text="Timeline response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Timeline response",
            "spoken": None,
        },
        screen_timeline_elements=[
            {
                "payload": {
                    "items": [
                        {
                            "item_id": "event_invalid",
                            "label": "Missing anchors",
                            "status": "pending",
                        }
                    ]
                }
            }
        ],
    )

    timeline_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "timeline"
    ]
    assert timeline_elements == []
    assert "screen_structured_timelines_invalid_dropped" in contract["reason_codes"]
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


def test_validate_turn_display_elements_rejects_invalid_workflow_links() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_workflow_view",
                    "element_type": "workflow_view",
                    "channel": "screen",
                    "order": 26,
                    "intent": "structured_workflow_view",
                    "payload": {
                        "nodes": [
                            {
                                "node_id": "workflow_1",
                                "label": "Workflow 1",
                                "status": "running",
                            }
                        ],
                        "edges": [
                            {
                                "source_node_id": "workflow_1",
                                "target_node_id": "missing_node",
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
    assert any("target_node_id must reference a declared node" in message for message in errors)


def test_validate_turn_display_elements_rejects_timeline_without_temporal_anchor() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_timeline_view",
                    "element_type": "timeline",
                    "channel": "screen",
                    "order": 36,
                    "intent": "structured_timeline_view",
                    "payload": {
                        "items": [
                            {
                                "item_id": "event_1",
                                "label": "Task updated",
                            }
                        ]
                    },
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any("must provide at least one temporal anchor" in message for message in errors)


def test_build_canonical_table_payload_from_records_preserves_row_provenance() -> None:
    payload = build_canonical_table_payload_from_records(
        records=[
            {
                "task_id": "task_alpha",
                "task_name": "Alpha",
                "status": "done",
                "due_date": "2026-02-16",
                "source": {"source_concept_id": "#V#task_alpha"},
            },
            {
                "task_id": "task_beta",
                "task_name": "Beta",
                "status": "in_progress",
                "due_date": "2026-03-01",
                "source": {"source_concept_id": "#V#task_beta"},
            },
        ],
        columns=[
            {
                "column_id": "task_name",
                "label": "Task",
                "source_key": "task_name",
                "data_type": "text",
            },
            {
                "column_id": "status",
                "label": "Status",
                "source_key": "status",
                "data_type": "text",
            },
            {
                "column_id": "due_date",
                "label": "Due Date",
                "source_key": "due_date",
                "data_type": "date",
            },
        ],
        row_id_field="task_id",
        row_provenance_field="source",
        default_sort_column_id="due_date",
        default_sort_direction="asc",
        pagination_enabled=True,
        page_size=25,
    )

    assert [column["column_id"] for column in payload["columns"]] == [
        "task_name",
        "status",
        "due_date",
    ]
    assert payload["rows"][0]["row_id"] == "task_alpha"
    assert payload["rows"][0]["provenance"]["source_concept_id"] == "#V#task_alpha"
    assert payload["rows"][0]["cells"][2]["value_type"] == "date"
    assert payload["sort"]["default_column_id"] == "due_date"
    assert payload["pagination"]["enabled"] is True
    assert payload["pagination"]["page_size"] == 25


def test_task_and_predicate_extent_payloads_validate_under_same_table_contract() -> None:
    task_payload = build_canonical_table_payload_from_records(
        records=[
            {
                "task_id": "task_alpha",
                "task_name": "Alpha",
                "status": "done",
                "source": {"source_concept_id": "#V#task_alpha"},
            }
        ],
        columns=[
            {"column_id": "task_name", "label": "Task", "source_key": "task_name"},
            {"column_id": "status", "label": "Status", "source_key": "status"},
        ],
        row_id_field="task_id",
        row_provenance_field="source",
        default_sort_column_id="task_name",
    )

    predicate_payload = build_canonical_table_payload_from_records(
        records=[
            {
                "assertion_id": "assertion_1",
                "subject": "#V#task_alpha",
                "predicate": "#V#depends_on",
                "object": "#V#task_beta",
                "assertion_meta": {"assertion_id": "assertion_1"},
            }
        ],
        columns=[
            {"column_id": "subject", "label": "Subject", "source_key": "subject"},
            {"column_id": "predicate", "label": "Predicate", "source_key": "predicate"},
            {"column_id": "object", "label": "Object", "source_key": "object"},
        ],
        row_id_field="assertion_id",
        row_provenance_field="assertion_meta",
        default_sort_column_id="subject",
        pagination_enabled=False,
    )

    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_table_task",
                    "element_type": "table",
                    "channel": "screen",
                    "order": 15,
                    "intent": "structured_tabular_view",
                    "payload": task_payload,
                    "provenance": {"source": "task_query"},
                },
                {
                    "element_id": "screen_table_predicate_extent",
                    "element_type": "table",
                    "channel": "screen",
                    "order": 16,
                    "intent": "structured_tabular_view",
                    "payload": predicate_payload,
                    "provenance": {"source": "predicate_extent_query"},
                },
            ],
            "reason_codes": [],
        }
    )

    assert valid is True
    assert errors == []
    assert predicate_payload["rows"][0]["provenance"]["assertion_id"] == "assertion_1"
