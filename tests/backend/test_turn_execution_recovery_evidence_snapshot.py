from __future__ import annotations

from src.backend.workflows.durable.turn_execution_runtime_support import (
    _bounded_snapshot,
    build_turn_recovery_tool_batch_outputs,
)


def test_bounded_snapshot_retains_salient_scalar_mapping_beyond_list_window() -> None:
    payload = {
        "records": [
            {"name": f"noise-{index}", "value": f"ignored-{index}"}
            for index in range(8)
        ]
        + [
            {"name": "title", "value": "Quarterly evidence report"},
            {"name": "status", "value": "ready"},
        ]
    }

    snapshot = _bounded_snapshot(payload, max_depth=2, max_items=3)

    records = snapshot["records"]
    assert {row.get("value") for row in records if isinstance(row, dict)} >= {
        "Quarterly evidence report",
        "ready",
    }
    assert all(
        row != {"_truncated": "mapping"}
        for row in records
        if isinstance(row, dict)
    )


def test_recovery_tool_batch_evidence_preserves_gmail_header_fields() -> None:
    headers = [
        {"name": f"X-Noise-{index}", "value": f"noise-{index}"}
        for index in range(12)
    ] + [
        {"name": "From", "value": "Example Sender <sender@example.test>"},
        {"name": "Subject", "value": "Important subject line"},
        {"name": "Date", "value": "Wed, 06 May 2026 12:34:56 +1200"},
        {"name": "Message-ID", "value": "<message-id@example.test>"},
    ]
    raw_result = {
        "id": "msg-123",
        "threadId": "msg-123",
        "labelIds": ["UNREAD", "INBOX"],
        "snippet": "Short preview",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "body": {"size": 24011, "data": "x" * 2000},
        },
        "sizeEstimate": 24011,
    }

    outputs = build_turn_recovery_tool_batch_outputs(
        requested_tool_calls=[
            {
                "tool": "gmail_get_message",
                "arguments": {"profile": "zhan-gmail", "id": "msg-123"},
            }
        ],
        invocation_records=[
            {
                "tool": "gmail_get_message",
                "payload": {"profile": "zhan-gmail", "id": "msg-123"},
                "status": "ok",
                "result_preview": raw_result,
            }
        ],
        selected_workflow_id="#V#chat_assistant_workflow",
    )

    invocation = outputs["completion_report"]["tool_invocations"][0]
    preview_headers = invocation["result_preview"]["payload"]["headers"]
    header_values = {
        row.get("name"): row.get("value")
        for row in preview_headers
        if isinstance(row, dict)
    }

    assert header_values["From"] == "Example Sender <sender@example.test>"
    assert header_values["Subject"] == "Important subject line"
    assert header_values["Date"] == "Wed, 06 May 2026 12:34:56 +1200"
    assert header_values["Message-ID"] == "<message-id@example.test>"
    assert {"_truncated": "mapping"} not in preview_headers

    tool_message = outputs["tool_messages"][0]["content"]
    assert "Example Sender" in tool_message
    assert "Important subject line" in tool_message
    assert "Wed, 06 May 2026" in tool_message
