"""Compact, bounded projections of Gmail message detail responses."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

from ...services.selected_referent_contract import normalise_exact_referent_id

MAX_GMAIL_MESSAGE_MIME_PARTS = 256
MAX_GMAIL_ATTACHMENT_SUMMARIES = 64


def _bounded_text(value: Any, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_chars]


def _message_headers(payload: Mapping[str, Any]) -> dict[str, str]:
    raw_headers = payload.get("headers")
    if not isinstance(raw_headers, list):
        return {}

    headers: dict[str, str] = {}
    for raw_header in raw_headers:
        if not isinstance(raw_header, Mapping):
            continue
        name = _bounded_text(raw_header.get("name"), max_chars=128)
        value = _bounded_text(raw_header.get("value"), max_chars=8_192)
        if name is None or value is None:
            continue
        headers[name.lower()] = value
    return headers


def _attachment_projection(
    part: Mapping[str, Any],
    *,
    attachment_id: str,
) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "attachment_id": attachment_id,
    }
    filename = _bounded_text(part.get("filename"), max_chars=512)
    if filename is not None:
        projection["filename"] = filename
    content_type = _bounded_text(part.get("mimeType"), max_chars=255)
    if content_type is not None:
        projection["content_type"] = content_type.lower()
    part_id = _bounded_text(part.get("partId"), max_chars=128)
    if part_id is not None:
        projection["part_id"] = part_id
    body = part.get("body")
    if isinstance(body, Mapping):
        size = body.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            projection["size_bytes"] = size
    return projection


def project_gmail_message_detail(
    message: Mapping[str, Any],
    *,
    max_attachment_summaries: int = MAX_GMAIL_ATTACHMENT_SUMMARIES,
) -> dict[str, Any]:
    """Return stable Gmail detail fields without raw MIME or body data.

    MIME traversal and attachment summaries are independently bounded. The
    projection deliberately excludes each part's ``body.data`` and Gmail's
    top-level ``raw`` field; callers may add a separately bounded text body.
    """

    projection: dict[str, Any] = {}
    message_id = normalise_exact_referent_id(
        message.get("message_id") or message.get("id")
    )
    if message_id is not None:
        projection["id"] = message_id
        projection["message_id"] = message_id

    thread_id = normalise_exact_referent_id(
        message.get("thread_id") or message.get("threadId")
    )
    if thread_id is not None:
        projection["threadId"] = thread_id
        projection["thread_id"] = thread_id

    raw_label_ids = message.get("label_ids") or message.get("labelIds")
    if isinstance(raw_label_ids, list):
        label_ids = [
            label
            for item in raw_label_ids[:128]
            if (label := normalise_exact_referent_id(item)) is not None
        ]
        projection["labelIds"] = label_ids
        projection["label_ids"] = list(label_ids)

    for source_key, target_key, max_chars in (
        ("snippet", "snippet", 8_192),
        ("historyId", "history_id", 512),
        ("internalDate", "internal_date", 512),
    ):
        value = _bounded_text(message.get(source_key), max_chars=max_chars)
        if value is not None:
            projection[target_key] = value

    size_estimate = message.get("sizeEstimate")
    if (
        isinstance(size_estimate, int)
        and not isinstance(size_estimate, bool)
        and size_estimate >= 0
    ):
        projection["size_estimate"] = size_estimate

    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return projection

    headers = _message_headers(payload)
    sender = headers.get("from")
    if sender is not None:
        projection["sender"] = sender
        projection["from"] = sender
    subject = headers.get("subject")
    if subject is not None:
        projection["subject"] = subject
    date = headers.get("date")
    if date is not None:
        projection["date"] = date
    mime_type = _bounded_text(payload.get("mimeType"), max_chars=255)
    if mime_type is not None:
        projection["mime_type"] = mime_type.lower()

    effective_summary_limit = max(0, int(max_attachment_summaries))
    pending: deque[Mapping[str, Any]] = deque([payload])
    visited_parts = 0
    attachment_count = 0
    attachments: list[dict[str, Any]] = []
    attachment_identity_omitted = False
    mime_traversal_truncated = False
    while pending and visited_parts < MAX_GMAIL_MESSAGE_MIME_PARTS:
        part = pending.popleft()
        visited_parts += 1
        raw_children = part.get("parts")
        if isinstance(raw_children, list):
            available_child_slots = max(
                0,
                MAX_GMAIL_MESSAGE_MIME_PARTS - visited_parts - len(pending),
            )
            bounded_children = raw_children[:available_child_slots]
            pending.extend(
                child for child in bounded_children if isinstance(child, Mapping)
            )
            if len(raw_children) > len(bounded_children):
                mime_traversal_truncated = True

        body = part.get("body")
        if not isinstance(body, Mapping):
            continue
        raw_attachment_id = body.get("attachmentId")
        if not isinstance(raw_attachment_id, str) or not raw_attachment_id.strip():
            continue
        attachment_count += 1
        attachment_id = normalise_exact_referent_id(raw_attachment_id)
        if attachment_id is None:
            attachment_identity_omitted = True
            continue
        if len(attachments) < effective_summary_limit:
            attachments.append(
                _attachment_projection(part, attachment_id=attachment_id)
            )

    projection["attachments"] = attachments
    projection["attachment_count"] = attachment_count
    projection["attachments_truncated"] = bool(
        pending
        or mime_traversal_truncated
        or attachment_identity_omitted
        or attachment_count > len(attachments)
    )
    return projection


__all__ = [
    "MAX_GMAIL_ATTACHMENT_SUMMARIES",
    "MAX_GMAIL_MESSAGE_MIME_PARTS",
    "project_gmail_message_detail",
]
