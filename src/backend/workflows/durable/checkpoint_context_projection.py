"""Bounded projections for durable workflow checkpoint context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from typing import Any

from ..execution_contracts import (
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
    WORKFLOW_RESULT_ENVELOPE_KEY,
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
)

CHECKPOINT_CONTEXT_PROJECTION_SCHEMA_VERSION = (
    "workflow_checkpoint_context_projection.v1"
)
CHECKPOINT_CONTEXT_PROJECTION_KEY = "workflow_checkpoint_context_projection"

_DEFAULT_MAX_VALUE_BSON_BYTES = 256 * 1024
_DEFAULT_MAX_TOTAL_BSON_BYTES = 8 * 1024 * 1024
_DEFAULT_TEXT_PREVIEW_CHARS = 2000
_MAX_MAPPING_ITEMS = 40
_MAX_SEQUENCE_ITEMS = 8
_MAX_DEPTH = 5
_MAX_PROJECTED_KEY_RECORDS = 80

_ALWAYS_COMPACT_KEYS = {
    "last_action_outputs",
    "last_failed_action_outputs",
    WORKFLOW_RESULT_ENVELOPE_KEY,
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
}
_PROJECTION_METADATA_KEYS = {
    CHECKPOINT_CONTEXT_PROJECTION_KEY,
}
_LOSSLESS_OFFLOAD_KEYS = {
    "structured_tool_continuation",
}
_SECRET_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "credential",
    "encrypted_content",
    "password",
    "private_key",
    "secret",
    "token",
)
_SUPPRESSED_CONTEXT_KEY_PARTS = (
    "chat_history",
    "context_messages",
    "conversation_history",
    "messages",
    "prompt",
    "transcript",
)
_RAW_TEXT_KEY_PARTS = (
    "body",
    "document_text",
    "html",
    "markdown",
    "raw",
    "raw_response",
    "raw_text",
    "text",
)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _text_preview(text: str, *, max_chars: int) -> dict[str, Any]:
    limit = max(0, int(max_chars))
    return {
        "text_preview": text[:limit],
        "char_count": len(text),
        "sha256": _hash_text(text),
        "truncated": len(text) > limit,
    }


def _key_contains(key: str | None, parts: Sequence[str]) -> bool:
    lowered = str(key or "").lower()
    return bool(lowered) and any(part in lowered for part in parts)


def _measure_bson_size(value: Any) -> int:
    try:
        from bson import BSON

        return len(BSON.encode({"value": value}))
    except Exception:
        try:
            return len(
                json.dumps(value, ensure_ascii=True, default=str).encode("utf-8")
            )
        except Exception:
            return len(repr(value).encode("utf-8", errors="replace"))


def _compact_value(
    value: Any,
    *,
    key: str | None,
    depth: int = 0,
    max_text_chars: int = _DEFAULT_TEXT_PREVIEW_CHARS,
) -> Any:
    if key and _key_contains(key, _SECRET_KEY_PARTS):
        return "[redacted]"

    if isinstance(value, str):
        if key and _key_contains(key, _SUPPRESSED_CONTEXT_KEY_PARTS):
            return {
                "suppressed": True,
                "reason": "prompt_or_history_not_persisted",
                "char_count": len(value),
                "sha256": _hash_text(value),
            }
        if len(value) > max_text_chars or (
            key and _key_contains(key, _RAW_TEXT_KEY_PARTS)
        ):
            return _text_preview(value, max_chars=max_text_chars)
        return value

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, (datetime, date)):
        return value

    if isinstance(value, bytes):
        return {
            "suppressed": True,
            "reason": "bytes_not_persisted",
            "byte_count": len(value),
        }

    if key and _key_contains(key, _SUPPRESSED_CONTEXT_KEY_PARTS):
        payload: dict[str, Any] = {
            "suppressed": True,
            "reason": "prompt_or_history_not_persisted",
        }
        try:
            payload["item_count"] = len(value)
        except Exception:
            pass
        return payload

    if depth >= _MAX_DEPTH:
        if isinstance(value, Mapping):
            return {
                "truncated": True,
                "reason": "max_depth",
                "type": "mapping",
                "key_count": len(value),
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return {
                "truncated": True,
                "reason": "max_depth",
                "type": "sequence",
                "item_count": len(value),
            }
        return repr(value)

    if isinstance(value, Mapping):
        payload: dict[str, Any] = {}
        items = list(value.items())
        for raw_key, raw_item in items[:_MAX_MAPPING_ITEMS]:
            item_key = str(raw_key).strip() if raw_key is not None else ""
            if not item_key:
                continue
            payload[item_key] = _compact_value(
                raw_item,
                key=item_key,
                depth=depth + 1,
                max_text_chars=max_text_chars,
            )
        if len(items) > _MAX_MAPPING_ITEMS:
            payload["_truncated_key_count"] = len(items) - _MAX_MAPPING_ITEMS
        return payload

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        sequence_payload = [
            _compact_value(
                item,
                key=key,
                depth=depth + 1,
                max_text_chars=max_text_chars,
            )
            for item in list(value)[:_MAX_SEQUENCE_ITEMS]
        ]
        if len(value) > _MAX_SEQUENCE_ITEMS:
            sequence_payload.append(
                {
                    "truncated": True,
                    "reason": "max_items",
                    "omitted_item_count": len(value) - _MAX_SEQUENCE_ITEMS,
                }
            )
        return sequence_payload

    return repr(value)


def _safe_copy_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float, datetime, date)):
        return value
    if isinstance(value, bytes):
        return _compact_value(value, key=None)
    if isinstance(value, Mapping):
        return {
            str(raw_key): _safe_copy_value(raw_value)
            for raw_key, raw_value in value.items()
            if str(raw_key)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_copy_value(item) for item in value]
    try:
        return deepcopy(value)
    except Exception:
        return repr(value)


def _projection_record(
    *,
    key: str,
    reason: str,
    original_size: int,
    projected_size: int,
) -> dict[str, Any]:
    return {
        "key": key,
        "reason": reason,
        "original_bson_size_bytes": int(original_size),
        "projected_bson_size_bytes": int(projected_size),
    }


def project_workflow_context_for_checkpoint(
    context: Mapping[str, Any],
    *,
    max_value_bson_bytes: int = _DEFAULT_MAX_VALUE_BSON_BYTES,
    max_total_bson_bytes: int = _DEFAULT_MAX_TOTAL_BSON_BYTES,
    max_text_preview_chars: int = _DEFAULT_TEXT_PREVIEW_CHARS,
    lossless_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a bounded persisted projection of workflow runtime context.

    The active executor keeps the full in-memory context for the current run.
    Checkpoints are a persistence/resume support surface, so they store compact
    summaries for large or diagnostic-only payloads rather than duplicating raw
    tool results, prompts, or document text into the workflow instance row.
    """

    if not isinstance(context, Mapping):
        return {}

    declared_lossless_keys = {
        str(item).strip() for item in lossless_keys if str(item).strip()
    }
    projected: dict[str, Any] = {}
    projected_key_records: list[dict[str, Any]] = []
    already_projected_keys: set[str] = set()
    original_bson_size = _measure_bson_size({"workflow_data": dict(context)})

    for raw_key, value in context.items():
        key = str(raw_key).strip() if raw_key is not None else ""
        if not key or key in _PROJECTION_METADATA_KEYS:
            continue

        original_size = _measure_bson_size(value)
        reason: str | None = None
        if key in _LOSSLESS_OFFLOAD_KEYS or (
            key in declared_lossless_keys
            and not _key_contains(key, _SECRET_KEY_PARTS)
        ):
            # The instance manager immediately replaces this provider state
            # or the containing workflow data with a namespace-scoped blob
            # reference. Preserve execution-required values byte-for-byte until
            # that offload boundary; truncation here would make a durable resume
            # semantically different from the uninterrupted run.
            projected[key] = _safe_copy_value(value)
            continue
        if key in _ALWAYS_COMPACT_KEYS:
            reason = "diagnostic_payload_bounded"
        elif key and _key_contains(key, _SECRET_KEY_PARTS):
            reason = "secret_redacted"
        elif original_size > max(1, int(max_value_bson_bytes)):
            reason = "oversized_value_bounded"

        if reason is None:
            projected[key] = _safe_copy_value(value)
            continue

        compacted = _compact_value(
            value,
            key=key,
            max_text_chars=max_text_preview_chars,
        )
        projected[key] = compacted
        already_projected_keys.add(key)
        projected_key_records.append(
            _projection_record(
                key=key,
                reason=reason,
                original_size=original_size,
                projected_size=_measure_bson_size(compacted),
            )
        )

    budget = max(1, int(max_total_bson_bytes))
    projected_bson_size = _measure_bson_size({"workflow_data": projected})
    if projected_bson_size > budget:
        remaining_sizes = sorted(
            (
                (_measure_bson_size(value), key)
                for key, value in projected.items()
                if key not in already_projected_keys
                and key not in _LOSSLESS_OFFLOAD_KEYS
                and key not in declared_lossless_keys
            ),
            reverse=True,
        )
        for original_size, key in remaining_sizes:
            compacted = _compact_value(
                projected.get(key),
                key=key,
                max_text_chars=max_text_preview_chars,
            )
            projected[key] = compacted
            already_projected_keys.add(key)
            projected_key_records.append(
                _projection_record(
                    key=key,
                    reason="total_context_budget_bounded",
                    original_size=original_size,
                    projected_size=_measure_bson_size(compacted),
                )
            )
            projected_bson_size = _measure_bson_size({"workflow_data": projected})
            if projected_bson_size <= budget:
                break

    projected_bson_size = _measure_bson_size({"workflow_data": projected})
    projection_metadata = {
        "schema_version": CHECKPOINT_CONTEXT_PROJECTION_SCHEMA_VERSION,
        "original_bson_size_bytes": original_bson_size,
        "projected_bson_size_bytes": projected_bson_size,
        "max_value_bson_bytes": max(1, int(max_value_bson_bytes)),
        "max_total_bson_bytes": budget,
        "projected_key_count": len(projected_key_records),
        "projected_keys": projected_key_records[:_MAX_PROJECTED_KEY_RECORDS],
        "omitted_projected_key_count": max(
            0,
            len(projected_key_records) - _MAX_PROJECTED_KEY_RECORDS,
        ),
    }
    projected[CHECKPOINT_CONTEXT_PROJECTION_KEY] = projection_metadata
    projection_metadata["projected_bson_size_bytes"] = _measure_bson_size(
        {"workflow_data": projected}
    )
    return projected
