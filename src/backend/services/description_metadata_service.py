"""Helpers for migrating legacy inline description metadata into structured fields."""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple


_METADATA_LINE_RE = re.compile(
    r"^(?:[-*]\s*)?(source|attribution|attributed\s+to|confidence|confidence\s+score|timestamp|generated\s+at|parent|parent\s+id)\s*[:=-]\s*(.+)$",
    re.IGNORECASE,
)


def _coerce_confidence(raw_value: str) -> float | None:
    token = raw_value.strip().rstrip("%").strip()
    if not token:
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    if value > 1 and value <= 100:
        value = value / 100.0
    if value < 0:
        value = 0.0
    if value > 1:
        value = 1.0
    return value


def extract_inline_description_metadata(raw_text: str) -> Tuple[str, Dict[str, Any]]:
    """Strip a leading metadata block from description text.

    Legacy descriptions sometimes start with lines like:
      Source: ...
      Confidence: ...
      Timestamp: ...

    We treat that as metadata only when at least two recognised metadata lines
    appear at the top of the text before the first blank line.
    """

    if not isinstance(raw_text, str):
        return "", {}

    text = raw_text.strip()
    if not text:
        return "", {}

    lines = text.splitlines()
    parsed: Dict[str, Any] = {}
    consumed = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            break
        match = _METADATA_LINE_RE.match(stripped)
        if not match:
            break
        key = match.group(1).strip().lower().replace(" ", "_")
        value = match.group(2).strip()
        if not value:
            break
        consumed += 1
        if key == "attributed_to":
            parsed["attribution"] = value
        elif key == "generated_at":
            parsed["timestamp"] = value
        elif key == "parent_id":
            parsed["parent"] = value
        elif key == "confidence_score":
            parsed["confidence"] = value
        else:
            parsed[key] = value

    if consumed < 2:
        return text, {}

    remainder_lines = lines[consumed:]
    while remainder_lines and not remainder_lines[0].strip():
        remainder_lines.pop(0)
    cleaned = "\n".join(remainder_lines).strip()
    if not cleaned:
        cleaned = text

    confidence_raw = parsed.get("confidence")
    confidence_score = None
    if isinstance(confidence_raw, str):
        confidence_score = _coerce_confidence(confidence_raw)

    structured: Dict[str, Any] = {}
    if isinstance(parsed.get("source"), str):
        structured["source"] = parsed["source"]
    if isinstance(parsed.get("attribution"), str):
        structured["attribution"] = parsed["attribution"]
    if isinstance(parsed.get("timestamp"), str):
        structured["timestamp"] = parsed["timestamp"]
    if isinstance(parsed.get("parent"), str):
        structured["parent"] = parsed["parent"]
    if confidence_score is not None:
        structured["confidence_score"] = confidence_score
    structured["migrated_from_inline_header"] = True
    structured["raw_header_fields"] = parsed

    return cleaned, structured
