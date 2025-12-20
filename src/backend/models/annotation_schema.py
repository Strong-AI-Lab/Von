"""Annotation schema definitions for per-turn annotations.

This module defines a simple Python dataclass-like validator for the
annotation payload expected by the realtime annotation API. We avoid
external dependencies to keep tests lightweight.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime


def _ensure_str(v: Any) -> bool:
    return isinstance(v, str) and len(v.strip()) > 0


def validate_span(span: Dict[str, Any]) -> Optional[str]:
    if not isinstance(span, dict):
        return "span must be an object"
    if "start" not in span or "end" not in span or "text" not in span:
        return "span must include 'start', 'end', and 'text'"
    try:
        s = int(span["start"])
        e = int(span["end"])
        if s < 0 or e < s:
            return "invalid span range"
    except Exception:
        return "start and end must be integers"
    if not _ensure_str(span["text"]):
        return "span.text must be a non-empty string"
    # optional fields allowed: entity_type, concept_id, suggestions (list)
    return None


def validate_annotation(payload: Dict[str, Any]) -> Optional[str]:
    """Validate the top-level annotation payload.

    Expected keys:
      - conversation_id: str
      - turn_id: str
      - speaker: one of 'user'|'assistant'|other string
      - text: str (the turn text)
      - timestamp: optional ISO8601 string or epoch int
      - spans: optional list of span objects
      - metadata: optional dict
    Returns None on success, or an error message string on failure.
    """
    if not isinstance(payload, dict):
        return "payload must be a JSON object"

    if "conversation_id" not in payload or not _ensure_str(payload["conversation_id"]):
        return "conversation_id is required and must be a non-empty string"
    if "turn_id" not in payload or not _ensure_str(payload["turn_id"]):
        return "turn_id is required and must be a non-empty string"
    if "speaker" not in payload or not _ensure_str(payload["speaker"]):
        return "speaker is required and must be a non-empty string"
    if "text" not in payload or not isinstance(payload["text"], str):
        return "text is required and must be a string"

    # timestamp optional: accept int or ISO string
    if "timestamp" in payload:
        ts = payload["timestamp"]
        if isinstance(ts, int):
            pass
        elif isinstance(ts, str):
            try:
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                return "timestamp string must be ISO8601 format"
        else:
            return "timestamp must be int or ISO8601 string"

    if "spans" in payload:
        spans = payload["spans"]
        if not isinstance(spans, list):
            return "spans must be a list"
        for idx, span in enumerate(spans):
            err = validate_span(span)
            if err:
                return f"spans[{idx}]: {err}"

    if "metadata" in payload and not isinstance(payload["metadata"], dict):
        return "metadata must be an object if provided"

    return None
