from __future__ import annotations

import base64

from src.backend.integrations.google.gmail_message_body import (
    DEFAULT_GMAIL_BODY_CHAR_LIMIT,
    extract_gmail_message_body,
)


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def test_extract_gmail_message_body_prefers_plain_text_alternative() -> None:
    message = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": _encoded("<p>HTML body</p>")},
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("Plain body")},
                },
            ],
        }
    }

    assert extract_gmail_message_body(message) == ("Plain body", False)


def test_extract_gmail_message_body_converts_html_and_reports_truncation() -> None:
    message = {
        "payload": {
            "mimeType": "text/html",
            "body": {"data": _encoded("<p>Hello &amp; goodbye</p>")},
        }
    }

    assert extract_gmail_message_body(message, max_chars=7) == ("Hello &", True)


def test_extract_gmail_message_body_ignores_attachment_only_parts() -> None:
    message = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "attachment-1"},
                }
            ],
        }
    }

    assert extract_gmail_message_body(message) == (None, False)


def test_extract_gmail_message_body_ignores_inline_text_attachment_data() -> None:
    message = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("Actual message")},
                },
                {
                    "filename": "notes.txt",
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("Attachment contents")},
                },
            ],
        }
    }

    assert extract_gmail_message_body(message) == ("Actual message", False)


def test_extract_gmail_message_body_bounds_mime_part_traversal() -> None:
    message = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "application/octet-stream",
                    "body": {},
                }
                for _index in range(300)
            ],
        }
    }
    message["payload"]["parts"][-1] = {
        "mimeType": "text/plain",
        "body": {"data": _encoded("must not require unbounded traversal")},
    }

    assert extract_gmail_message_body(message) == (None, True)


def test_extract_gmail_message_body_bounds_decoded_source_work() -> None:
    message = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _encoded("x" * 1_000_000)},
        }
    }

    text, truncated = extract_gmail_message_body(message)

    assert text == "x" * DEFAULT_GMAIL_BODY_CHAR_LIMIT
    assert truncated is True
