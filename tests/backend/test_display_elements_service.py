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


def test_build_turn_display_elements_includes_supplied_task_view_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Task view response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Task view response",
            "spoken": None,
        },
        screen_task_view_elements=[
            {
                "element_id": "screen_task_view",
                "intent": "structured_task_view",
                "payload": {
                    "tasks": [
                        {
                            "task_id": "#V#task_alpha",
                            "title": "Alpha task",
                            "status": "in_progress",
                            "priority": "high",
                            "task_links": [
                                {
                                    "link_type": "jira_issue",
                                    "target_id": "JVNAUTOSCI-1174",
                                    "label": "JVNAUTOSCI-1174",
                                    "href": "https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1174",
                                }
                            ],
                        }
                    ]
                },
            }
        ],
    )

    task_view_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "task_view"
    ]
    assert len(task_view_elements) == 1
    task_view_element = task_view_elements[0]
    assert task_view_element["element_id"] == "screen_task_view"
    assert task_view_element["payload"]["tasks"][0]["task_id"] == "#V#task_alpha"
    assert (
        task_view_element["payload"]["tasks"][0]["task_links"][0]["target_id"]
        == "JVNAUTOSCI-1174"
    )
    assert "screen_structured_task_views_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_supplied_task_views() -> None:
    contract = build_turn_display_elements(
        response_text="Task view response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Task view response",
            "spoken": None,
        },
        screen_task_view_elements=[
            {
                "payload": {
                    "tasks": [
                        {
                            "status": "pending",
                        }
                    ]
                }
            }
        ],
    )

    task_view_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "task_view"
    ]
    assert task_view_elements == []
    assert "screen_structured_task_views_invalid_dropped" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_includes_supplied_kanban_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Kanban response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Kanban response",
            "spoken": None,
        },
        screen_kanban_elements=[
            {
                "element_id": "screen_kanban_view",
                "intent": "structured_kanban_view",
                "payload": {
                    "columns": [
                        {"column_id": "pending", "label": "Pending", "order": 10},
                        {"column_id": "done", "label": "Done", "order": 20},
                    ],
                    "cards": [
                        {
                            "card_id": "#V#task_alpha",
                            "title": "Alpha task",
                            "column_id": "pending",
                            "priority": "high",
                            "task_links": [
                                {
                                    "link_type": "jira_issue",
                                    "target_id": "JVNAUTOSCI-1181",
                                    "label": "JVNAUTOSCI-1181",
                                    "href": "https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1181",
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    )

    kanban_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "kanban_view"
    ]
    assert len(kanban_elements) == 1
    kanban_element = kanban_elements[0]
    assert kanban_element["element_id"] == "screen_kanban_view"
    assert kanban_element["payload"]["columns"][0]["column_id"] == "pending"
    assert kanban_element["payload"]["cards"][0]["column_id"] == "pending"
    assert "screen_structured_kanban_views_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_supplied_kanban_views() -> None:
    contract = build_turn_display_elements(
        response_text="Kanban response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Kanban response",
            "spoken": None,
        },
        screen_kanban_elements=[
            {
                "payload": {
                    "columns": [
                        {"column_id": "pending", "label": "Pending"},
                    ],
                    "cards": [
                        {
                            "card_id": "#V#task_alpha",
                            "title": "Alpha task",
                            "column_id": "missing_column",
                        }
                    ],
                }
            }
        ],
    )

    kanban_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "kanban_view"
    ]
    assert kanban_elements == []
    assert "screen_structured_kanban_views_invalid_dropped" in contract["reason_codes"]
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


def test_build_turn_display_elements_includes_supplied_calendar_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Calendar response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Calendar response",
            "spoken": None,
        },
        screen_calendar_elements=[
            {
                "element_id": "screen_calendar_view",
                "intent": "structured_calendar_view",
                "payload": {
                    "default_granularity": "month",
                    "focus_date": "2026-02-17",
                    "items": [
                        {
                            "item_id": "#V#task_alpha",
                            "title": "Alpha task",
                            "start_at": "2026-02-17",
                            "all_day": True,
                            "status": "pending",
                            "task_links": [
                                {
                                    "link_type": "von_task",
                                    "target_id": "#V#task_alpha",
                                    "label": "#V#task_alpha",
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    )

    calendar_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "calendar_view"
    ]
    assert len(calendar_elements) == 1
    calendar_element = calendar_elements[0]
    assert calendar_element["element_id"] == "screen_calendar_view"
    assert calendar_element["payload"]["items"][0]["item_id"] == "#V#task_alpha"
    assert calendar_element["payload"]["items"][0]["all_day"] is True
    assert "screen_structured_calendar_views_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_supplied_calendar_views() -> None:
    contract = build_turn_display_elements(
        response_text="Calendar response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Calendar response",
            "spoken": None,
        },
        screen_calendar_elements=[
            {
                "payload": {
                    "items": [
                        {
                            "item_id": "bad_item",
                            "start_at": "2026-02-17",
                            "all_day": "yes",
                        }
                    ]
                }
            }
        ],
    )

    calendar_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "calendar_view"
    ]
    assert calendar_elements == []
    assert "screen_structured_calendar_views_invalid_dropped" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_includes_supplied_location_views() -> None:
    contract = build_turn_display_elements(
        response_text="Location response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Location response",
            "spoken": None,
        },
        screen_location_elements=[
            {
                "element_id": "screen_location_view",
                "intent": "structured_location_view",
                "payload": {
                    "title": "Meeting locations",
                    "points": [
                        {
                            "point_id": "#V#meeting_location_1",
                            "label": "AUT city campus",
                            "latitude": -36.8509,
                            "longitude": 174.7676,
                            "address": "55 Wellesley Street East, Auckland",
                        },
                        {
                            "point_id": "#V#meeting_location_2",
                            "label": "Remote participant",
                            "address": "Wellington, New Zealand",
                        },
                    ],
                    "viewport": {
                        "centre_lat": -36.8509,
                        "centre_lon": 174.7676,
                        "zoom": 11,
                    },
                },
            }
        ],
    )

    location_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "location_view"
    ]
    assert len(location_elements) == 1
    location_element = location_elements[0]
    assert location_element["element_id"] == "screen_location_view"
    assert location_element["payload"]["points"][0]["latitude"] == -36.8509
    assert location_element["payload"]["points"][1]["address"] == "Wellington, New Zealand"
    assert "screen_structured_location_views_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_supplied_location_views() -> None:
    contract = build_turn_display_elements(
        response_text="Location response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Location response",
            "spoken": None,
        },
        screen_location_elements=[
            {
                "payload": {
                    "points": [
                        {
                            "point_id": "bad_point",
                            "label": "Missing anchors",
                            "latitude": -36.85,
                        }
                    ]
                }
            }
        ],
    )

    location_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "location_view"
    ]
    assert location_elements == []
    assert "screen_structured_location_views_invalid_dropped" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_includes_supplied_document_views() -> None:
    contract = build_turn_display_elements(
        response_text="Document response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Document response",
            "spoken": None,
        },
        screen_document_elements=[
            {
                "element_id": "screen_document_view",
                "intent": "structured_document_view",
                "payload": {
                    "documents": [
                        {
                            "document_id": "doc_alpha",
                            "title": "Alpha document",
                            "source_uri": "https://example.com/doc-alpha",
                            "source_label": "search_knowledge_base",
                            "updated_at": "2026-02-17T10:00:00Z",
                            "sections": [
                                {
                                    "section_id": "sec_1",
                                    "heading": "Summary",
                                    "excerpt": "Alpha excerpt",
                                    "citation": "https://example.com/doc-alpha#summary",
                                }
                            ],
                        }
                    ]
                },
            }
        ],
    )

    document_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "document_view"
    ]
    assert len(document_elements) == 1
    document_element = document_elements[0]
    assert document_element["element_id"] == "screen_document_view"
    document = document_element["payload"]["documents"][0]
    assert document["document_id"] == "doc_alpha"
    assert document["sections"][0]["heading"] == "Summary"
    assert "screen_structured_document_views_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_truncates_oversized_document_excerpt() -> None:
    oversized_excerpt = "A" * 1200
    contract = build_turn_display_elements(
        response_text="Document response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Document response",
            "spoken": None,
        },
        screen_document_elements=[
            {
                "payload": {
                    "documents": [
                        {
                            "document_id": "doc_alpha",
                            "title": "Alpha document",
                            "source_label": "search_knowledge_base",
                            "sections": [
                                {
                                    "section_id": "sec_1",
                                    "heading": "Summary",
                                    "excerpt": oversized_excerpt,
                                }
                            ],
                        }
                    ]
                }
            }
        ],
    )

    document_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "document_view"
    ]
    assert len(document_elements) == 1
    section = document_elements[0]["payload"]["documents"][0]["sections"][0]
    assert section["excerpt"].endswith("... [truncated]")
    assert section["excerpt_truncated"] is True
    assert section["excerpt_original_char_count"] == len(oversized_excerpt)
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_supplied_document_views() -> None:
    contract = build_turn_display_elements(
        response_text="Document response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Document response",
            "spoken": None,
        },
        screen_document_elements=[
            {
                "payload": {
                    "documents": [
                        {
                            "document_id": "doc_alpha",
                            "title": "Alpha document",
                            "sections": [],
                        }
                    ]
                }
            }
        ],
    )

    document_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "document_view"
    ]
    assert document_elements == []
    assert "screen_structured_document_views_invalid_dropped" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_includes_supplied_relation_graph_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Relation graph response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Relation graph response",
            "spoken": None,
        },
        screen_relation_graph_elements=[
            {
                "element_id": "screen_relation_graph_view",
                "intent": "relation_graph_view",
                "payload": {
                    "nodes": [
                        {
                            "node_id": "#V#michael_witbrock",
                            "label": "Michael Witbrock",
                            "node_kind": "individual",
                        },
                        {
                            "node_id": "#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne",
                            "label": "Panel 4",
                            "node_kind": "individual",
                        },
                    ],
                    "edges": [
                        {
                            "edge_id": "edge_1",
                            "source": "#V#michael_witbrock",
                            "target": "#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne",
                            "predicate": "#V#panelist_in_event",
                            "direction": "directed",
                        }
                    ],
                    "focus_node_id": "#V#michael_witbrock",
                },
            }
        ],
    )

    relation_graph_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "relation_graph_view"
    ]
    assert len(relation_graph_elements) == 1
    relation_graph_element = relation_graph_elements[0]
    assert relation_graph_element["element_id"] == "screen_relation_graph_view"
    assert relation_graph_element["payload"]["nodes"][0]["node_id"] == "#V#michael_witbrock"
    assert relation_graph_element["payload"]["edges"][0]["predicate"] == "#V#panelist_in_event"
    assert (
        "screen_structured_relation_graph_views_supplied" in contract["reason_codes"]
    )
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_relation_graph_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Relation graph response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Relation graph response",
            "spoken": None,
        },
        screen_relation_graph_elements=[
            {
                "payload": {
                    "nodes": [
                        {
                            "node_id": "#V#michael_witbrock",
                            "label": "Michael Witbrock",
                            "node_kind": "individual",
                        }
                    ],
                    "edges": [
                        {
                            "edge_id": "edge_1",
                            "source": "#V#michael_witbrock",
                            "target": "#V#missing_node",
                            "predicate": "#V#panelist_in_event",
                        }
                    ],
                }
            }
        ],
    )

    relation_graph_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "relation_graph_view"
    ]
    assert relation_graph_elements == []
    assert (
        "screen_structured_relation_graph_views_invalid_dropped"
        in contract["reason_codes"]
    )
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_includes_supplied_hierarchy_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Hierarchy response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Hierarchy response",
            "spoken": None,
        },
        screen_hierarchy_elements=[
            {
                "element_id": "screen_hierarchy_view",
                "intent": "hierarchy_view",
                "payload": {
                    "title": "Meeting hierarchy",
                    "nodes": [
                        {
                            "node_id": "#V#meeting",
                            "label": "Meeting",
                            "node_kind": "type",
                        },
                        {
                            "node_id": "#V#reading_group_meeting",
                            "label": "Reading group meeting",
                            "node_kind": "type",
                        },
                        {
                            "node_id": "#V#sail_reading_group_meeting",
                            "label": "SAIL reading group meeting",
                            "node_kind": "type",
                        },
                    ],
                    "edges": [
                        {
                            "edge_id": "hierarchy_edge_1",
                            "parent_node_id": "#V#meeting",
                            "child_node_id": "#V#reading_group_meeting",
                            "predicate": "#V#is_a_type_of",
                            "branch_kind": "type_hierarchy",
                        },
                        {
                            "edge_id": "hierarchy_edge_2",
                            "parent_node_id": "#V#reading_group_meeting",
                            "child_node_id": "#V#sail_reading_group_meeting",
                            "predicate": "#V#is_a_type_of",
                            "branch_kind": "type_hierarchy",
                        },
                    ],
                    "focus_node_id": "#V#sail_reading_group_meeting",
                    "root_node_ids": ["#V#meeting"],
                    "expansion": {
                        "show_parents": True,
                        "show_children": True,
                        "show_siblings": True,
                        "max_depth": 4,
                    },
                },
            }
        ],
    )

    hierarchy_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "hierarchy_view"
    ]
    assert len(hierarchy_elements) == 1
    hierarchy_element = hierarchy_elements[0]
    assert hierarchy_element["element_id"] == "screen_hierarchy_view"
    assert hierarchy_element["payload"]["nodes"][0]["node_id"] == "#V#meeting"
    assert (
        hierarchy_element["payload"]["edges"][0]["parent_node_id"] == "#V#meeting"
    )
    assert "screen_structured_hierarchy_views_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_hierarchy_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Hierarchy response",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Hierarchy response",
            "spoken": None,
        },
        screen_hierarchy_elements=[
            {
                "payload": {
                    "nodes": [
                        {
                            "node_id": "#V#meeting",
                            "label": "Meeting",
                            "node_kind": "type",
                        }
                    ],
                    "edges": [
                        {
                            "edge_id": "hierarchy_edge_1",
                            "parent_node_id": "#V#meeting",
                            "child_node_id": "#V#missing_node",
                            "predicate": "#V#is_a_type_of",
                        }
                    ],
                }
            }
        ],
    )

    hierarchy_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "hierarchy_view"
    ]
    assert hierarchy_elements == []
    assert "screen_structured_hierarchy_views_invalid_dropped" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_includes_supplied_relation_truth_state_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Truth state summary",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Truth state summary",
            "spoken": None,
        },
        screen_relation_truth_state_elements=[
            {
                "element_id": "screen_relation_truth_state",
                "intent": "truth_state_relation_view",
                "payload": {
                    "title": "Current truth state",
                    "groups": [
                        {
                            "label": "Conference-level",
                            "status": "asserted",
                            "assertions": [
                                {
                                    "assertion_id": "a1",
                                    "arg1": "#V#michael_witbrock",
                                    "predicate": "#V#attended_event",
                                    "arg2": "#V#international_ai_cooperation_and_governance_forum_2025_melbourne",
                                    "is_asserted": True,
                                }
                            ],
                        },
                        {
                            "label": "Missing (should exist)",
                            "status": "missing_expected",
                            "assertions": [
                                {
                                    "assertion_id": "a2",
                                    "arg1": "#V#michael_witbrock",
                                    "predicate": "#V#panelist_in_event",
                                    "arg2": "#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne",
                                    "is_asserted": False,
                                }
                            ],
                        },
                    ],
                },
            }
        ],
    )

    relation_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "relation_truth_state"
    ]
    assert len(relation_elements) == 1
    relation_element = relation_elements[0]
    assert relation_element["element_id"] == "screen_relation_truth_state"
    assert relation_element["payload"]["groups"][0]["status"] == "asserted"
    assert relation_element["payload"]["groups"][1]["assertions"][0]["is_asserted"] is False
    assert "screen_structured_relation_truth_states_supplied" in contract["reason_codes"]
    assert contract["validation"]["valid"] is True


def test_build_turn_display_elements_drops_invalid_relation_truth_state_elements() -> None:
    contract = build_turn_display_elements(
        response_text="Truth state summary",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Truth state summary",
            "spoken": None,
        },
        screen_relation_truth_state_elements=[
            {
                "payload": {
                    "title": "Current truth state",
                    "groups": [
                        {
                            "label": "Bad status group",
                            "status": "unknown",
                            "assertions": [
                                {
                                    "arg1": "#V#a",
                                    "predicate": "#V#p",
                                    "arg2": "#V#b",
                                    "is_asserted": "yes",
                                }
                            ],
                        }
                    ],
                }
            }
        ],
    )

    relation_elements = [
        element
        for element in contract["elements"]
        if element["element_type"] == "relation_truth_state"
    ]
    assert relation_elements == []
    assert (
        "screen_structured_relation_truth_states_invalid_dropped"
        in contract["reason_codes"]
    )
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


def test_validate_turn_display_elements_rejects_calendar_item_without_title() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_calendar_view",
                    "element_type": "calendar_view",
                    "channel": "screen",
                    "order": 38,
                    "intent": "structured_calendar_view",
                    "payload": {
                        "items": [
                            {
                                "item_id": "event_1",
                                "start_at": "2026-02-17",
                                "all_day": "yes",
                            }
                        ],
                        "default_granularity": "hourly",
                    },
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any(".title must be a non-empty string" in message for message in errors)
    assert any(".all_day must be a boolean" in message for message in errors)
    assert any("default_granularity must be one of" in message for message in errors)


def test_validate_turn_display_elements_rejects_invalid_location_view_shape() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_location_view",
                    "element_type": "location_view",
                    "channel": "screen",
                    "order": 39,
                    "intent": "structured_location_view",
                    "payload": {
                        "points": [
                            {
                                "point_id": "point_1",
                                "label": "Invalid coordinate",
                                "latitude": 123.0,
                                "longitude": 174.7,
                            },
                            {
                                "point_id": "point_2",
                                "label": "Missing anchors",
                            },
                        ],
                        "viewport": {
                            "centre_lat": -36.8,
                            "zoom": 99,
                        },
                    },
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any(".latitude must be between -90 and 90" in message for message in errors)
    assert any("must provide at least one spatial anchor" in message for message in errors)
    assert any("must provide both centre_lat and centre_lon" in message for message in errors)
    assert any(".viewport.zoom must be an integer between 1 and 20" in message for message in errors)


def test_validate_turn_display_elements_rejects_invalid_document_view_shape() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_document_view",
                    "element_type": "document_view",
                    "channel": "screen",
                    "order": 40,
                    "intent": "structured_document_view",
                    "payload": {
                        "documents": [
                            {
                                "document_id": "doc_alpha",
                                "title": "Alpha document",
                                "sections": [
                                    {
                                        "section_id": "sec_1",
                                        "heading": "Summary",
                                        "excerpt": "X" * 900,
                                    }
                                ],
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
    assert any("must provide provenance (source_uri or source_label)" in msg for msg in errors)
    assert any(".excerpt must be 700 chars or less" in msg for msg in errors)


def test_validate_turn_display_elements_rejects_task_view_without_task_id() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_task_view",
                    "element_type": "task_view",
                    "channel": "screen",
                    "order": 31,
                    "intent": "structured_task_view",
                    "payload": {
                        "tasks": [
                            {
                                "title": "Task without id",
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
    assert any("must provide a non-empty task identifier" in message for message in errors)


def test_validate_turn_display_elements_rejects_kanban_card_without_column() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_kanban_view",
                    "element_type": "kanban_view",
                    "channel": "screen",
                    "order": 33,
                    "intent": "structured_kanban_view",
                    "payload": {
                        "columns": [
                            {"column_id": "pending", "label": "Pending"},
                        ],
                        "cards": [
                            {
                                "card_id": "card_1",
                                "title": "Task without valid column",
                                "column_id": "missing",
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
    assert any("column_id must reference a declared column" in message for message in errors)


def test_validate_turn_display_elements_rejects_invalid_relation_graph_shape() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_relation_graph_view",
                    "element_type": "relation_graph_view",
                    "channel": "screen",
                    "order": 43,
                    "intent": "relation_graph_view",
                    "payload": {
                        "nodes": [
                            {
                                "node_id": "#V#michael_witbrock",
                                "label": "Michael Witbrock",
                                "node_kind": "individual",
                            }
                        ],
                        "edges": [
                            {
                                "edge_id": "edge_1",
                                "source": "#V#michael_witbrock",
                                "target": "#V#missing_node",
                                "predicate": "#V#panelist_in_event",
                                "direction": "sideways",
                            }
                        ],
                        "focus_node_id": "#V#missing_node",
                    },
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any("target must reference a declared node" in message for message in errors)
    assert any("direction must be one of" in message for message in errors)
    assert any("focus_node_id must reference a declared node" in message for message in errors)


def test_validate_turn_display_elements_rejects_invalid_hierarchy_shape() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_hierarchy_view",
                    "element_type": "hierarchy_view",
                    "channel": "screen",
                    "order": 42,
                    "intent": "hierarchy_view",
                    "payload": {
                        "nodes": [
                            {
                                "node_id": "#V#reading_group_meeting",
                                "label": "Reading group meeting",
                            }
                        ],
                        "edges": [
                            {
                                "edge_id": "hierarchy_edge_1",
                                "parent_node_id": "#V#missing_parent",
                                "child_node_id": "#V#reading_group_meeting",
                                "predicate": "#V#is_a_type_of",
                                "branch_kind": "unknown_branch",
                            }
                        ],
                        "focus_node_id": "#V#missing_focus",
                        "expansion": {
                            "show_children": "yes",
                            "max_depth": 999,
                        },
                    },
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any("parent_node_id must reference a declared node" in message for message in errors)
    assert any("branch_kind must be one of" in message for message in errors)
    assert any("focus_node_id must reference a declared node" in message for message in errors)
    assert any("show_children must be a boolean" in message for message in errors)
    assert any("max_depth must be an integer between 1 and 12" in message for message in errors)


def test_validate_turn_display_elements_rejects_invalid_relation_truth_state_shape() -> None:
    valid, errors = validate_turn_display_elements(
        {
            "schema_version": "turn_display_elements_v1",
            "elements": [
                {
                    "element_id": "screen_relation_truth_state",
                    "element_type": "relation_truth_state",
                    "channel": "screen",
                    "order": 41,
                    "intent": "truth_state_relation_view",
                    "payload": {
                        "title": "Current truth state",
                        "groups": [
                            {
                                "label": "Missing",
                                "status": "missing_expected",
                                "assertions": [
                                    {
                                        "arg1": "#V#michael_witbrock",
                                        "predicate": "#V#panelist_in_event",
                                        "arg2": "#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne",
                                        "is_asserted": "false",
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
    assert any("is_asserted must be a boolean" in message for message in errors)


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


def test_build_canonical_table_payload_infers_relation_extent_compact_visibility() -> None:
    payload = build_canonical_table_payload_from_records(
        records=[
            {
                "assertion_id": "assertion_1",
                "subject": "#V#michael_witbrock",
                "predicate": "#V#attended_event",
                "object": "#V#iaicgf_2025",
                "status": "asserted",
            }
        ],
        columns=[
            {"column_id": "arg1", "label": "Arg1", "source_key": "subject"},
            {"column_id": "predicate", "label": "Predicate", "source_key": "predicate"},
            {"column_id": "arg2", "label": "Arg2", "source_key": "object"},
            {"column_id": "status", "label": "Status", "source_key": "status"},
        ],
        row_id_field="assertion_id",
        default_sort_column_id="arg1",
    )

    assert payload["column_visibility"]["default_mode"] == "compact"
    assert payload["column_visibility"]["compact_column_ids"] == [
        "arg1",
        "predicate",
        "arg2",
    ]
    assert payload["column_visibility"]["expand_label"] == "Expand table"
    assert payload["column_visibility"]["collapse_label"] == "Show compact view"


def test_extract_markdown_tables_derives_context_title() -> None:
    tables = extract_markdown_tables(
        (
            "### Predicate status snapshot\n"
            "| Predicate | Status |\n"
            "| --- | --- |\n"
            "| #V#attended_event | asserted |\n"
        )
    )

    assert len(tables) == 1
    assert tables[0]["title"] == "Predicate status snapshot"


def test_build_turn_display_elements_infers_compact_relation_extent_columns() -> None:
    contract = build_turn_display_elements(
        response_text="Relation extent summary",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Relation extent summary",
            "spoken": None,
        },
        screen_table_elements=[
            {
                "payload": {
                    "columns": [
                        {"column_id": "arg1", "label": "Arg1", "data_type": "text"},
                        {"column_id": "predicate", "label": "Predicate", "data_type": "text"},
                        {"column_id": "arg2", "label": "Arg2", "data_type": "text"},
                        {"column_id": "status", "label": "Status", "data_type": "text"},
                    ],
                    "rows": [
                        {
                            "row_id": "row_1",
                            "cells": [
                                {
                                    "column_id": "arg1",
                                    "value_raw": "#V#a",
                                    "value_display": "#V#a",
                                    "value_type": "text",
                                },
                                {
                                    "column_id": "predicate",
                                    "value_raw": "#V#p",
                                    "value_display": "#V#p",
                                    "value_type": "text",
                                },
                                {
                                    "column_id": "arg2",
                                    "value_raw": "#V#b",
                                    "value_display": "#V#b",
                                    "value_type": "text",
                                },
                                {
                                    "column_id": "status",
                                    "value_raw": "asserted",
                                    "value_display": "asserted",
                                    "value_type": "text",
                                },
                            ],
                        }
                    ],
                }
            }
        ],
    )

    table_payload = next(
        element["payload"]
        for element in contract["elements"]
        if element["element_type"] == "table"
    )
    assert table_payload["column_visibility"]["default_mode"] == "compact"
    assert table_payload["column_visibility"]["compact_column_ids"] == [
        "arg1",
        "predicate",
        "arg2",
    ]


def test_build_turn_display_elements_preserves_optional_payload_titles() -> None:
    contract = build_turn_display_elements(
        response_text="Structured summary",
        presenter_channels={
            "format": "tagged_blocks_v1",
            "screen": "Structured summary",
            "spoken": None,
        },
        screen_table_elements=[
            {
                "payload": {
                    "title": "Task status table",
                    "columns": [
                        {
                            "column_id": "task",
                            "label": "Task",
                            "data_type": "text",
                        },
                        {
                            "column_id": "status",
                            "label": "Status",
                            "data_type": "text",
                        },
                    ],
                    "rows": [
                        {
                            "row_id": "row_1",
                            "cells": [
                                {
                                    "column_id": "task",
                                    "value_raw": "Alpha",
                                    "value_display": "Alpha",
                                    "value_type": "text",
                                },
                                {
                                    "column_id": "status",
                                    "value_raw": "done",
                                    "value_display": "done",
                                    "value_type": "text",
                                },
                            ],
                        }
                    ],
                }
            }
        ],
        screen_workflow_elements=[
            {
                "payload": {
                    "title": "Workflow executions",
                    "nodes": [
                        {
                            "node_id": "inst_1",
                            "label": "Workflow 1",
                            "status": "running",
                        }
                    ],
                    "edges": [],
                }
            }
        ],
        screen_task_view_elements=[
            {
                "payload": {
                    "title": "Action items",
                    "tasks": [
                        {
                            "task_id": "#V#task_alpha",
                            "title": "Alpha task",
                        }
                    ],
                }
            }
        ],
        screen_kanban_elements=[
            {
                "payload": {
                    "title": "Kanban backlog",
                    "columns": [
                        {"column_id": "pending", "label": "Pending"},
                    ],
                    "cards": [],
                }
            }
        ],
        screen_timeline_elements=[
            {
                "payload": {
                    "title": "Timeline of changes",
                    "items": [
                        {
                            "item_id": "event_1",
                            "label": "Updated task",
                            "start_at": "2026-02-17T09:10:00Z",
                        }
                    ],
                }
            }
        ],
        screen_calendar_elements=[
            {
                "payload": {
                    "title": "Calendar schedule",
                    "items": [
                        {
                            "item_id": "calendar_1",
                            "title": "Alpha task",
                            "start_at": "2026-02-17",
                        }
                    ],
                }
            }
        ],
        screen_location_elements=[
            {
                "payload": {
                    "title": "Location markers",
                    "points": [
                        {
                            "point_id": "location_1",
                            "label": "Auckland office",
                            "latitude": -36.8509,
                            "longitude": 174.7676,
                        }
                    ],
                }
            }
        ],
        screen_document_elements=[
            {
                "payload": {
                    "title": "Document excerpts",
                    "documents": [
                        {
                            "document_id": "doc_1",
                            "title": "Doc one",
                            "source_label": "search_knowledge_base",
                            "sections": [
                                {
                                    "section_id": "sec_1",
                                    "heading": "Summary",
                                    "excerpt": "Alpha excerpt",
                                }
                            ],
                        }
                    ],
                }
            }
        ],
        screen_hierarchy_elements=[
            {
                "payload": {
                    "title": "Meeting hierarchy",
                    "nodes": [
                        {
                            "node_id": "#V#meeting",
                            "label": "Meeting",
                            "node_kind": "type",
                        },
                        {
                            "node_id": "#V#reading_group_meeting",
                            "label": "Reading group meeting",
                            "node_kind": "type",
                        },
                    ],
                    "edges": [
                        {
                            "edge_id": "hierarchy_edge_1",
                            "parent_node_id": "#V#meeting",
                            "child_node_id": "#V#reading_group_meeting",
                            "predicate": "#V#is_a_type_of",
                        }
                    ],
                }
            }
        ],
        screen_relation_graph_elements=[
            {
                "payload": {
                    "title": "Relation network",
                    "nodes": [
                        {
                            "node_id": "#V#a",
                            "label": "A",
                            "node_kind": "individual",
                        },
                        {
                            "node_id": "#V#b",
                            "label": "B",
                            "node_kind": "individual",
                        },
                    ],
                    "edges": [
                        {
                            "edge_id": "edge_1",
                            "source": "#V#a",
                            "target": "#V#b",
                            "predicate": "#V#related_to",
                        }
                    ],
                }
            }
        ],
    )

    payload_title_by_type = {
        element["element_type"]: element["payload"].get("title")
        for element in contract["elements"]
        if element["element_type"] in {
            "table",
            "workflow_view",
            "task_view",
            "kanban_view",
            "timeline",
            "calendar_view",
            "location_view",
            "document_view",
            "hierarchy_view",
            "relation_graph_view",
        }
    }

    assert payload_title_by_type["table"] == "Task status table"
    assert payload_title_by_type["workflow_view"] == "Workflow executions"
    assert payload_title_by_type["task_view"] == "Action items"
    assert payload_title_by_type["kanban_view"] == "Kanban backlog"
    assert payload_title_by_type["timeline"] == "Timeline of changes"
    assert payload_title_by_type["calendar_view"] == "Calendar schedule"
    assert payload_title_by_type["location_view"] == "Location markers"
    assert payload_title_by_type["document_view"] == "Document excerpts"
    assert payload_title_by_type["hierarchy_view"] == "Meeting hierarchy"
    assert payload_title_by_type["relation_graph_view"] == "Relation network"
    assert contract["validation"]["valid"] is True


def test_validate_turn_display_elements_rejects_blank_optional_payload_title() -> None:
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
                        "title": "   ",
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
                                        "column_id": "task",
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
    assert any(".payload.title must be a non-empty string when provided" in message for message in errors)


def test_validate_turn_display_elements_rejects_invalid_table_column_visibility() -> None:
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
                            {"column_id": "arg1", "label": "Arg1", "data_type": "text"},
                            {
                                "column_id": "predicate",
                                "label": "Predicate",
                                "data_type": "text",
                            },
                            {"column_id": "arg2", "label": "Arg2", "data_type": "text"},
                            {"column_id": "status", "label": "Status", "data_type": "text"},
                        ],
                        "rows": [
                            {
                                "row_id": "row_1",
                                "cells": [
                                    {
                                        "column_id": "arg1",
                                        "value_raw": "#V#a",
                                        "value_display": "#V#a",
                                        "value_type": "text",
                                    },
                                    {
                                        "column_id": "predicate",
                                        "value_raw": "#V#p",
                                        "value_display": "#V#p",
                                        "value_type": "text",
                                    },
                                    {
                                        "column_id": "arg2",
                                        "value_raw": "#V#b",
                                        "value_display": "#V#b",
                                        "value_type": "text",
                                    },
                                    {
                                        "column_id": "status",
                                        "value_raw": "asserted",
                                        "value_display": "asserted",
                                        "value_type": "text",
                                    },
                                ],
                            }
                        ],
                        "column_visibility": {
                            "default_mode": "compact",
                            "compact_column_ids": ["arg1", "predicate", "missing_column"],
                            "expand_label": "Expand table",
                            "collapse_label": "Show compact view",
                        },
                    },
                    "provenance": {"source": "test"},
                }
            ],
            "reason_codes": [],
        }
    )

    assert valid is False
    assert any(
        "compact_column_ids[2] must reference a declared column" in message
        for message in errors
    )
