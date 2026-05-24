"""Bounded terminal outputs for durable workflow instances."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from ..execution_contracts import (
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
    WORKFLOW_RETURN_PAYLOAD_KEY,
    WORKFLOW_RESULT_ENVELOPE_KEY,
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
)
from ..metadata_validation import LAST_METADATA_EVENT_KEY, WORKFLOW_METADATA_EVENTS_KEY

COMPLETED_WORKFLOW_OUTPUTS_SCHEMA_VERSION = "workflow_completed_outputs.v1"
FAILED_WORKFLOW_OUTPUTS_SCHEMA_VERSION = "workflow_failed_outputs.v1"

_DEFAULT_TEXT_PREVIEW_CHARS = 2000
_MAX_MAPPING_ITEMS = 40
_MAX_SEQUENCE_ITEMS = 8
_MAX_DEPTH = 5
_MAX_RECENT_STEP_ENVELOPES = 3

_RAW_RESPONSE_KEYS = {
    "llm_step_response",
    "validated_json_raw_response",
    "raw_response",
    "response_preview",
    "raw_text",
}
_SECRET_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "credential",
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


def _stringify_key(value: Any) -> str:
    return str(value).strip() if value is not None else ""


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
    lowered = (key or "").lower()
    return bool(lowered) and any(part in lowered for part in parts)


def _compact_value(
    value: Any,
    *,
    key: str | None = None,
    max_text_chars: int = _DEFAULT_TEXT_PREVIEW_CHARS,
    depth: int = 0,
) -> Any:
    if key and _key_contains(key, _SECRET_KEY_PARTS):
        return "[redacted]"

    if isinstance(value, str):
        if key in _RAW_RESPONSE_KEYS:
            return _text_preview(value, max_chars=max_text_chars)
        if key and _key_contains(key, _SUPPRESSED_CONTEXT_KEY_PARTS):
            return {
                "suppressed": True,
                "reason": "prompt_or_history_not_persisted",
                "char_count": len(value),
                "sha256": _hash_text(value),
            }
        if len(value) > max_text_chars:
            return _text_preview(value, max_chars=max_text_chars)
        return value

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, bytes):
        return {
            "suppressed": True,
            "reason": "bytes_not_persisted",
            "byte_count": len(value),
        }

    if key and _key_contains(key, _SUPPRESSED_CONTEXT_KEY_PARTS):
        size = len(value) if hasattr(value, "__len__") else None
        payload: dict[str, Any] = {
            "suppressed": True,
            "reason": "prompt_or_history_not_persisted",
        }
        if isinstance(size, int):
            payload["item_count"] = size
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
            item_key = _stringify_key(raw_key)
            if not item_key:
                continue
            payload[item_key] = _compact_value(
                raw_item,
                key=item_key,
                max_text_chars=max_text_chars,
                depth=depth + 1,
            )
        if len(items) > _MAX_MAPPING_ITEMS:
            payload["_truncated_key_count"] = len(items) - _MAX_MAPPING_ITEMS
        return payload

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        sequence_payload = [
            _compact_value(
                item,
                key=key,
                max_text_chars=max_text_chars,
                depth=depth + 1,
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


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if str(key)}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [
        {str(key): item for key, item in item.items() if str(key)}
        for item in value
        if isinstance(item, Mapping)
    ]


def _latest_step_envelope(context: Mapping[str, Any]) -> dict[str, Any] | None:
    latest = _mapping(context.get(LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY))
    if latest is not None:
        return latest
    envelopes = _mapping_list(context.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY))
    return envelopes[-1] if envelopes else None


def _output_keys(output_payload: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(output_payload, Mapping):
        return []
    return sorted(str(key) for key in output_payload.keys() if str(key))


def _context_key_summary(
    context: Mapping[str, Any],
    *,
    max_keys: int = 80,
) -> dict[str, Any]:
    keys = sorted(str(key) for key in context.keys() if str(key))
    summary: dict[str, Any] = {
        "key_count": len(keys),
        "keys": keys[:max_keys],
    }
    if len(keys) > max_keys:
        summary["omitted_key_count"] = len(keys) - max_keys
    return summary


def _declared_output_payload(
    context: Mapping[str, Any],
    result_envelope: Mapping[str, Any] | None,
) -> Any:
    if isinstance(result_envelope, Mapping) and "declared_output_payload" in result_envelope:
        return result_envelope.get("declared_output_payload")
    if WORKFLOW_RETURN_PAYLOAD_KEY in context:
        return context.get(WORKFLOW_RETURN_PAYLOAD_KEY)
    return None


def _non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def build_completed_workflow_outputs(
    run_data: Mapping[str, Any] | None,
    *,
    result_envelope: Mapping[str, Any] | None = None,
    final_state: str | None = None,
    execution_trace_id: str | None = None,
    max_text_chars: int = _DEFAULT_TEXT_PREVIEW_CHARS,
) -> dict[str, Any]:
    """Build a compact terminal outputs payload for a completed durable run.

    Durable checkpoints may keep the workflow context needed for resume and
    telemetry, but terminal ``outputs`` are read-back/user-facing persistence.
    Persist declared workflow outputs plus safe summaries here rather than
    duplicating the full context, which may contain raw mail, document text, or
    large tool payloads.
    """

    context = dict(run_data) if isinstance(run_data, Mapping) else {}
    envelope = _mapping(result_envelope) or _mapping(
        context.get(WORKFLOW_RESULT_ENVELOPE_KEY)
    )
    declared_outputs = _declared_output_payload(context, envelope)
    metadata_events = _mapping_list(context.get(WORKFLOW_METADATA_EVENTS_KEY))
    last_metadata_event = _mapping(context.get(LAST_METADATA_EVENT_KEY))
    latest_step = _latest_step_envelope(context)

    outputs: dict[str, Any] = {
        "schema_version": COMPLETED_WORKFLOW_OUTPUTS_SCHEMA_VERSION,
        "terminal_status": "completed",
        "completed": True,
        "final_state": str(final_state).strip() if final_state else None,
        "execution_trace_id": (
            str(execution_trace_id).strip() if execution_trace_id else None
        ),
        "workflow_result_envelope": _compact_value(
            envelope,
            key=WORKFLOW_RESULT_ENVELOPE_KEY,
            max_text_chars=max_text_chars,
        ),
        "metadata_validation": {
            "event_count": len(metadata_events),
            "last_event": _compact_value(
                last_metadata_event,
                key=LAST_METADATA_EVENT_KEY,
                max_text_chars=max_text_chars,
            ),
        },
        "context_summary": _context_key_summary(context),
    }

    if isinstance(latest_step, Mapping):
        outputs["latest_step_result_summary"] = {
            "state_id": latest_step.get("state_id"),
            "action_id": latest_step.get("action_id"),
            "action_status": latest_step.get("action_status"),
            "action_outcome": latest_step.get("action_outcome"),
        }

    if declared_outputs is not None:
        compact_declared = _compact_value(
            declared_outputs,
            key="declared_output_payload",
            max_text_chars=max_text_chars,
        )
        outputs["declared_outputs"] = compact_declared
        outputs["terminal_output_source"] = "declared_output_payload"
        if isinstance(declared_outputs, Mapping):
            for raw_key, raw_value in declared_outputs.items():
                key = _stringify_key(raw_key)
                if not key or key in outputs:
                    continue
                outputs[key] = _compact_value(
                    raw_value,
                    key=key,
                    max_text_chars=max_text_chars,
                )
        else:
            outputs["result"] = compact_declared
    elif "result" in context:
        outputs["terminal_output_source"] = "context.result"
        outputs["result"] = _compact_value(
            context.get("result"),
            key="result",
            max_text_chars=max_text_chars,
        )
    else:
        outputs["terminal_output_source"] = "workflow_result_envelope"

    user_response_candidates = {
        "selected_workflow_user_response": _non_empty_text(
            context.get("selected_workflow_user_response")
        ),
        "final_response": _non_empty_text(context.get("final_response")),
        "response_text": _non_empty_text(context.get("response_text")),
        "current_response": _non_empty_text(context.get("current_response")),
    }
    for key, value in user_response_candidates.items():
        if value is not None and key not in outputs:
            outputs[key] = _compact_value(
                value,
                key=key,
                max_text_chars=max_text_chars,
            )
    preferred_response = (
        user_response_candidates.get("selected_workflow_user_response")
        or user_response_candidates.get("final_response")
        or user_response_candidates.get("response_text")
        or user_response_candidates.get("current_response")
    )
    if preferred_response and "response" not in outputs:
        outputs["response"] = _compact_value(
            preferred_response,
            key="response",
            max_text_chars=max_text_chars,
        )

    for key in (
        "workflow_execution_summary",
        "turn_execution_outcome",
    ):
        if key in outputs or key not in context:
            continue
        outputs[key] = _compact_value(
            context.get(key),
            key=key,
            max_text_chars=max_text_chars,
        )

    return outputs


def build_failed_workflow_outputs(
    run_data: Mapping[str, Any] | None,
    *,
    error: str | None,
    error_step: str | None,
    max_text_chars: int = _DEFAULT_TEXT_PREVIEW_CHARS,
) -> dict[str, Any]:
    """Build a compact terminal outputs payload for a failed durable run.

    The durable executor keeps the full workflow context in checkpoint data, but
    terminal ``outputs`` need to stay small and safe because they are surfaced by
    user-facing MCP inspection tools. This helper preserves the evidence needed
    to diagnose failed LLM/action/metadata steps without persisting prompts,
    chat history, credentials, or unbounded raw text in that outputs field.
    """

    context = dict(run_data) if isinstance(run_data, Mapping) else {}
    latest_step = _latest_step_envelope(context)
    output_payload = (
        _mapping(latest_step.get("output_payload"))
        if isinstance(latest_step, Mapping)
        else None
    )
    metadata_events = _mapping_list(context.get(WORKFLOW_METADATA_EVENTS_KEY))
    last_metadata_event = _mapping(context.get(LAST_METADATA_EVENT_KEY))
    step_envelopes = _mapping_list(context.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY))
    recent_step_envelopes = step_envelopes[-_MAX_RECENT_STEP_ENVELOPES:]

    outputs: dict[str, Any] = {
        "schema_version": FAILED_WORKFLOW_OUTPUTS_SCHEMA_VERSION,
        "terminal_status": "failed",
        "error": str(error) if error else None,
        "error_step": str(error_step) if error_step else None,
        "workflow_result_envelope": _compact_value(
            context.get(WORKFLOW_RESULT_ENVELOPE_KEY),
            key=WORKFLOW_RESULT_ENVELOPE_KEY,
            max_text_chars=max_text_chars,
        ),
        "metadata_validation": {
            "event_count": len(metadata_events),
            "last_event": _compact_value(
                last_metadata_event,
                key=LAST_METADATA_EVENT_KEY,
                max_text_chars=max_text_chars,
            ),
        },
        "latest_step_result_envelope": _compact_value(
            latest_step,
            key=LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
            max_text_chars=max_text_chars,
        ),
        "recent_step_result_envelopes": _compact_value(
            recent_step_envelopes,
            key=WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
            max_text_chars=max_text_chars,
        ),
    }

    if isinstance(latest_step, Mapping):
        outputs["failed_action_diagnostics"] = {
            "state_id": latest_step.get("state_id"),
            "action_id": latest_step.get("action_id"),
            "action_status": latest_step.get("action_status"),
            "action_outcome": latest_step.get("action_outcome"),
            "diagnostics": _compact_value(
                latest_step.get("diagnostics"),
                key="diagnostics",
                max_text_chars=max_text_chars,
            ),
            "output_keys": _output_keys(output_payload),
            "output_payload": _compact_value(
                output_payload,
                key="output_payload",
                max_text_chars=max_text_chars,
            ),
        }
        outputs["last_action_outputs"] = _compact_value(
            output_payload,
            key="last_action_outputs",
            max_text_chars=max_text_chars,
        )
        if str(latest_step.get("action_outcome") or "").lower() == "failure":
            outputs["failed_action_outputs"] = outputs["last_action_outputs"]

    return outputs
