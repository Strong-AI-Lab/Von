"""Bounded plain-text extraction from Gmail ``format=full`` payloads."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

DEFAULT_GMAIL_BODY_CHAR_LIMIT = 65_536


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._chunks)


def _decode_body_data(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    compact = value.strip()
    padding = "=" * (-len(compact) % 4)
    try:
        decoded = base64.urlsafe_b64decode((compact + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return None
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return decoded.decode(encoding)
        except UnicodeDecodeError:
            continue
    return decoded.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed external HTML is expected
        return value
    return parser.text()


def _collect_text_parts(
    part: Mapping[str, Any],
    *,
    plain_parts: list[str],
    html_parts: list[str],
) -> None:
    raw_children = part.get("parts")
    if isinstance(raw_children, list):
        for child in raw_children:
            if isinstance(child, Mapping):
                _collect_text_parts(
                    child,
                    plain_parts=plain_parts,
                    html_parts=html_parts,
                )

    body = part.get("body")
    if not isinstance(body, Mapping):
        return
    decoded = _decode_body_data(body.get("data"))
    if not decoded:
        return
    mime_type = str(part.get("mimeType") or "text/plain").strip().lower()
    if mime_type == "text/plain":
        plain_parts.append(decoded)
    elif mime_type == "text/html":
        html_parts.append(_html_to_text(decoded))


def extract_gmail_message_body(
    message: Mapping[str, Any],
    *,
    max_chars: int = DEFAULT_GMAIL_BODY_CHAR_LIMIT,
) -> tuple[str | None, bool]:
    """Return bounded body text and whether the original text was truncated.

    Plain text is preferred over HTML alternatives. Attachment bodies are not
    fetched or decoded by this helper.
    """

    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return None, False

    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_text_parts(
        payload,
        plain_parts=plain_parts,
        html_parts=html_parts,
    )
    selected_parts = plain_parts or html_parts
    text = "\n\n".join(part.strip() for part in selected_parts if part.strip()).strip()
    if not text:
        return None, False

    effective_limit = max(1, int(max_chars))
    if len(text) <= effective_limit:
        return text, False
    return text[:effective_limit], True


__all__ = ["DEFAULT_GMAIL_BODY_CHAR_LIMIT", "extract_gmail_message_body"]
