from __future__ import annotations

from src.backend.integrations.google.gmail_message_projection import (
    MAX_GMAIL_ATTACHMENT_SUMMARIES,
    MAX_GMAIL_MESSAGE_MIME_PARTS,
    project_gmail_message_detail,
)
from src.backend.services.selected_referent_contract import (
    MAX_EXACT_REFERENT_ID_CHARS,
)


def test_project_gmail_message_detail_keeps_handles_without_raw_mime_data() -> None:
    message = {
        "id": "message-1",
        "threadId": "thread-1",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "A compact preview",
        "historyId": "history-1",
        "internalDate": "1720000000000",
        "sizeEstimate": 1234,
        "raw": "top-level-secret-base64",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "Subject", "value": "Travel details"},
                {"name": "Date", "value": "Mon, 10 Aug 2026 09:00:00 +0100"},
            ],
            "parts": [
                {
                    "partId": "0",
                    "mimeType": "text/plain",
                    "body": {"data": "body-secret-base64", "size": 10},
                },
                {
                    "partId": "1",
                    "filename": "ticket.pdf",
                    "mimeType": "application/pdf",
                    "body": {
                        "attachmentId": "attachment-1",
                        "data": "attachment-secret-base64",
                        "size": 456,
                    },
                },
            ],
        },
    }

    result = project_gmail_message_detail(message)

    assert result == {
        "id": "message-1",
        "message_id": "message-1",
        "threadId": "thread-1",
        "thread_id": "thread-1",
        "labelIds": ["INBOX", "UNREAD"],
        "label_ids": ["INBOX", "UNREAD"],
        "snippet": "A compact preview",
        "history_id": "history-1",
        "internal_date": "1720000000000",
        "size_estimate": 1234,
        "sender": "Sender <sender@example.com>",
        "from": "Sender <sender@example.com>",
        "subject": "Travel details",
        "date": "Mon, 10 Aug 2026 09:00:00 +0100",
        "mime_type": "multipart/mixed",
        "attachments": [
            {
                "attachment_id": "attachment-1",
                "filename": "ticket.pdf",
                "content_type": "application/pdf",
                "part_id": "1",
                "size_bytes": 456,
            }
        ],
        "attachment_count": 1,
        "attachments_truncated": False,
    }
    assert "payload" not in result
    assert "raw" not in result
    assert "data" not in repr(result)
    assert "secret-base64" not in repr(result)


def test_project_gmail_message_detail_bounds_parts_and_attachment_summaries() -> None:
    parts = [
        {
            "partId": str(index),
            "filename": f"attachment-{index}.txt",
            "mimeType": "text/plain",
            "body": {"attachmentId": f"attachment-{index}", "size": index},
        }
        for index in range(MAX_GMAIL_MESSAGE_MIME_PARTS + 20)
    ]
    message = {"payload": {"mimeType": "multipart/mixed", "parts": parts}}

    result = project_gmail_message_detail(message)

    assert len(result["attachments"]) == MAX_GMAIL_ATTACHMENT_SUMMARIES
    # The root consumes one of the bounded MIME traversal slots.
    assert result["attachment_count"] == MAX_GMAIL_MESSAGE_MIME_PARTS - 1
    assert result["attachments_truncated"] is True


def test_project_gmail_message_detail_handles_metadata_format_payload() -> None:
    result = project_gmail_message_detail(
        {
            "id": "message-1",
            "payload": {
                "headers": [{"name": "Subject", "value": "Only metadata"}],
            },
        }
    )

    assert result["message_id"] == "message-1"
    assert result["subject"] == "Only metadata"
    assert result["attachments"] == []
    assert result["attachment_count"] == 0
    assert result["attachments_truncated"] is False


def test_project_gmail_message_detail_preserves_long_exact_handles() -> None:
    message_id = "message-" + ("m" * 700)
    thread_id = "thread-" + ("t" * 700)
    attachment_id = "attachment-" + ("a" * 700)

    result = project_gmail_message_detail(
        {
            "id": message_id,
            "threadId": thread_id,
            "payload": {
                "parts": [
                    {
                        "filename": "ticket.pdf",
                        "body": {"attachmentId": attachment_id},
                    }
                ]
            },
        }
    )

    assert result["message_id"] == message_id
    assert result["thread_id"] == thread_id
    assert result["attachments"][0]["attachment_id"] == attachment_id
    assert result["attachments_truncated"] is False


def test_project_gmail_message_detail_omits_over_limit_attachment_handle() -> None:
    attachment_id = "a" * (MAX_EXACT_REFERENT_ID_CHARS + 1)

    result = project_gmail_message_detail(
        {
            "payload": {
                "parts": [
                    {
                        "filename": "ticket.pdf",
                        "body": {"attachmentId": attachment_id},
                    }
                ]
            }
        }
    )

    assert result["attachments"] == []
    assert result["attachment_count"] == 1
    assert result["attachments_truncated"] is True
    assert attachment_id[:512] not in repr(result)
