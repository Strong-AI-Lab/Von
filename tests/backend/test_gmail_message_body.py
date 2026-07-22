from __future__ import annotations

import base64

from src.backend.integrations.google.gmail_message_body import (
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
