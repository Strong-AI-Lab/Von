"""Shared helpers for recognising file-copy concept references in text."""

from __future__ import annotations

import re
from typing import Any

_FILE_COPY_CONCEPT_ID_PATTERN = re.compile(
    r"#V#[A-Za-z0-9][A-Za-z0-9._-]*file_copy[A-Za-z0-9._-]*",
    flags=re.IGNORECASE,
)
_TRAILING_REFERENCE_PUNCTUATION = ".,;:!?)]}>\"'"


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_file_copy_token(value: Any) -> str | None:
    text = _safe_str(value)
    if not text:
        return None
    cleaned = text.rstrip(_TRAILING_REFERENCE_PUNCTUATION).strip()
    return cleaned or None


def is_file_copy_concept_id(value: Any) -> bool:
    text = _normalise_file_copy_token(value)
    if not text:
        return False
    return bool(_FILE_COPY_CONCEPT_ID_PATTERN.fullmatch(text))


def extract_file_copy_concept_ids_from_text(text: Any) -> list[str]:
    source = _safe_str(text)
    if not source:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _FILE_COPY_CONCEPT_ID_PATTERN.findall(source):
        token = _normalise_file_copy_token(match)
        if not token:
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(token)
    return ordered


__all__ = [
    "extract_file_copy_concept_ids_from_text",
    "is_file_copy_concept_id",
]
