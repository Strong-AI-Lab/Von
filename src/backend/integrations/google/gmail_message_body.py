"""Bounded plain-text extraction from Gmail ``format=full`` payloads."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

DEFAULT_GMAIL_BODY_CHAR_LIMIT = 20_000
MAX_GMAIL_BODY_CHAR_LIMIT = 65_536
MAX_GMAIL_MIME_PARTS = 256
MAX_GMAIL_BODY_SOURCE_BYTES = 262_144


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._chunks)


def _decode_body_data(value: Any, *, max_source_bytes: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if max_source_bytes <= 0:
        return None
    compact = "".join(value.split())
    maximum_encoded_chars = max(4, ((max_source_bytes + 2) // 3) * 4)
    if len(compact) > maximum_encoded_chars:
        compact = compact[: maximum_encoded_chars - (maximum_encoded_chars % 4)]
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


def _bounded_mime_parts(
    payload: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], bool]:
    pending: list[Mapping[str, Any]] = [payload]
    parts: list[Mapping[str, Any]] = []
    while pending and len(parts) < MAX_GMAIL_MIME_PARTS:
        part = pending.pop(0)
        parts.append(part)
        raw_children = part.get("parts")
        if isinstance(raw_children, list):
            pending.extend(
                child for child in raw_children if isinstance(child, Mapping)
            )
    return parts, bool(pending)


def _collect_preferred_text(
    parts: list[Mapping[str, Any]],
    *,
    mime_type: str,
    max_chars: int,
) -> tuple[str | None, bool]:
    source_bytes_remaining = min(
        MAX_GMAIL_BODY_SOURCE_BYTES,
        max(4, max_chars * 4),
    )
    values: list[str] = []
    projected_length = 0
    source_truncated = False
    for part in parts:
        if str(part.get("mimeType") or "text/plain").strip().lower() != mime_type:
            continue
        # A textual attachment is not the message body. Gmail may inline small
        # attachment bytes in ``body.data`` rather than supplying an
        # attachmentId, so filename is the primary exclusion signal.
        filename = part.get("filename")
        if isinstance(filename, str) and filename.strip():
            continue
        body = part.get("body")
        if not isinstance(body, Mapping):
            continue
        if isinstance(body.get("attachmentId"), str):
            continue
        raw_data = body.get("data")
        if not isinstance(raw_data, str) or not raw_data.strip():
            continue
        compact_length = len("".join(raw_data.split()))
        estimated_source_bytes = (compact_length * 3) // 4
        available_source_bytes = source_bytes_remaining
        decoded = _decode_body_data(
            raw_data,
            max_source_bytes=available_source_bytes,
        )
        if estimated_source_bytes > available_source_bytes:
            source_truncated = True
        source_bytes_remaining -= min(
            source_bytes_remaining,
            estimated_source_bytes,
        )
        if decoded:
            value = _html_to_text(decoded) if mime_type == "text/html" else decoded
            value = value.strip()
            if value:
                values.append(value)
                projected_length += len(value) + (2 if len(values) > 1 else 0)
        if projected_length > max_chars or source_bytes_remaining <= 0:
            source_truncated = True
            break
    text = "\n\n".join(values).strip()
    return text or None, source_truncated


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

    effective_limit = max(1, int(max_chars))
    parts, parts_truncated = _bounded_mime_parts(payload)
    text, source_truncated = _collect_preferred_text(
        parts,
        mime_type="text/plain",
        max_chars=effective_limit,
    )
    if text is None:
        text, source_truncated = _collect_preferred_text(
            parts,
            mime_type="text/html",
            max_chars=effective_limit,
        )
    if not text:
        return None, parts_truncated or source_truncated

    truncated = parts_truncated or source_truncated or len(text) > effective_limit
    return text[:effective_limit], truncated


__all__ = [
    "DEFAULT_GMAIL_BODY_CHAR_LIMIT",
    "MAX_GMAIL_BODY_CHAR_LIMIT",
    "MAX_GMAIL_BODY_SOURCE_BYTES",
    "MAX_GMAIL_MIME_PARTS",
    "extract_gmail_message_body",
]
