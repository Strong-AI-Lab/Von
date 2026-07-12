from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from src.backend.services.required_tool_identity_service import (
    canonical_required_tool_key,
    canonical_required_tool_keys,
)

WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID = "workflow_mcp.invoke_tool"
_TOOL_NAME_KEYS: tuple[str, ...] = (
    "mcp_resolved_tool",
    "mcp_tool",
    "mcp_requested_tool",
    "tool",
    "method",
)


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def dedupe_string_sequence(raw_values: Sequence[Any] | None) -> list[str]:
    if not isinstance(raw_values, Sequence) or isinstance(
        raw_values, (str, bytes, bytearray)
    ):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        cleaned = clean_text(item)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        values.append(cleaned)
    return values


def canonical_tool_name_from_mapping(mapping: Mapping[str, Any]) -> str | None:
    for key in _TOOL_NAME_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def canonical_step_tool_name(
    *,
    action_id: str,
    output_payload: Mapping[str, Any],
) -> str:
    canonical = canonical_tool_name_from_mapping(output_payload)
    if action_id.lower() == WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID and canonical:
        return canonical
    return action_id


def tool_invocation_names(invocation: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for source in (
        invocation,
        invocation.get("effective_payload"),
        invocation.get("payload"),
        invocation.get("result"),
    ):
        if not isinstance(source, Mapping):
            continue
        for key in _TOOL_NAME_KEYS:
            value = source.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            cleaned = value.strip()
            lowered = cleaned.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            names.append(cleaned)
    return names


def preferred_tool_invocation_name(invocation: Mapping[str, Any]) -> str | None:
    names = tool_invocation_names(invocation)
    if not names:
        return None
    first = names[0]
    if first.lower() == WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID:
        for name in names[1:]:
            if name.lower() != WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID:
                return name
    return first


def tool_invocation_completed_successfully(invocation: Mapping[str, Any]) -> bool:
    if bool(invocation.get("blocked")):
        return False
    if isinstance(invocation.get("error"), str) and str(invocation.get("error")).strip():
        return False

    status = invocation.get("status")
    if isinstance(status, str) and status.strip().lower() in {
        "error",
        "failed",
        "failure",
    }:
        return False

    payload = invocation.get("effective_payload")
    if not isinstance(payload, Mapping):
        payload = invocation.get("payload")
    if isinstance(payload, Mapping):
        status_value = str(payload.get("status") or "").strip().lower()
        if status_value in {"error", "failed", "failure"}:
            return False
        if payload.get("success") is False:
            return False
    return True


def _normalised_status(value: Any) -> str:
    status = clean_text(value).lower()
    return "ok" if status in {"success", "succeeded", "ok"} else "failed"


def _record_fingerprint(record: Mapping[str, Any]) -> tuple[str, str]:
    return (
        clean_text(record.get("tool")).lower(),
        json.dumps(
            record.get("effective_payload") or record.get("payload") or {},
            sort_keys=True,
            default=str,
        ),
    )


def _append_record(
    records: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    record: Mapping[str, Any],
    *,
    max_records: int,
) -> None:
    if len(records) >= max_records:
        return
    if not clean_text(record.get("tool")):
        return
    fingerprint = _record_fingerprint(record)
    if fingerprint in seen:
        return
    seen.add(fingerprint)
    records.append(dict(record))


def _iter_nested_invocation_mappings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nested: list[Mapping[str, Any]] = []
    stack: list[Any] = []
    for key in ("invocations", "tool_invocations", "iteration_results"):
        child = payload.get(key)
        if isinstance(child, Sequence) and not isinstance(
            child, (str, bytes, bytearray)
        ):
            stack.extend(reversed(list(child)))
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            if preferred_tool_invocation_name(value):
                nested.append(value)
            for key in ("invocations", "tool_invocations", "iteration_results"):
                child = value.get(key)
                if isinstance(child, Sequence) and not isinstance(
                    child, (str, bytes, bytearray)
                ):
                    stack.extend(reversed(list(child)))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            stack.extend(reversed(list(value)))
    return nested


def derive_tool_invocation_records_from_step_envelopes(
    raw_envelopes: Any,
    *,
    context_payload: Mapping[str, Any] | None = None,
    workflow_id_filter: str | None = None,
    required_tools: Sequence[str] | None = None,
    payload_projector: Callable[[Mapping[str, Any]], Any] | None = None,
    max_records: int = 80,
) -> list[dict[str, Any]]:
    if not isinstance(raw_envelopes, Sequence) or isinstance(
        raw_envelopes, (str, bytes, bytearray)
    ):
        return []

    context_payload_map = dict(context_payload or {})
    required = dedupe_string_sequence(required_tools)
    required_lookup = canonical_required_tool_keys(
        required,
        known_tool_names=required,
    )
    workflow_filter = clean_text(workflow_id_filter)
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for envelope in raw_envelopes:
        if not isinstance(envelope, Mapping):
            continue
        envelope_workflow_id = clean_text(envelope.get("workflow_id"))
        if workflow_filter and envelope_workflow_id and envelope_workflow_id != workflow_filter:
            continue
        action_id = clean_text(envelope.get("action_id"))
        if not action_id:
            continue
        raw_output_payload = envelope.get("output_payload")
        output_payload = (
            dict(raw_output_payload) if isinstance(raw_output_payload, Mapping) else {}
        )
        state_id = clean_text(envelope.get("state_id"))
        status = _normalised_status(
            envelope.get("action_outcome") or envelope.get("action_status")
        )
        raw_diagnostics = envelope.get("diagnostics")
        diagnostics = dict(raw_diagnostics) if isinstance(raw_diagnostics, Mapping) else {}
        error_text = clean_text(diagnostics.get("error"))
        effective_payload = {**context_payload_map, **output_payload}

        for nested in _iter_nested_invocation_mappings(output_payload):
            nested_tool = preferred_tool_invocation_name(nested)
            if not nested_tool:
                continue
            if (
                required_lookup
                and canonical_required_tool_key(
                    nested_tool,
                    known_tool_names=[*required, nested_tool],
                )
                not in required_lookup
                and action_id.lower() != WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID
            ):
                continue
            raw_payload = nested.get("effective_payload") or nested.get("payload")
            nested_payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
            nested_effective_payload = {**context_payload_map, **nested_payload}
            record: dict[str, Any] = {
                "tool": nested_tool,
                "status": clean_text(nested.get("status")) or "ok",
                "payload": (
                    payload_projector(nested_effective_payload)
                    if payload_projector is not None
                    else nested_effective_payload
                ),
                "effective_payload": nested_effective_payload,
                "workflow_step_evidence": True,
                "workflow_action_id": action_id,
                "workflow_id": clean_text(nested.get("workflow_id")) or envelope_workflow_id,
                "workflow_state_id": clean_text(nested.get("workflow_state_id")) or state_id,
            }
            nested_error = clean_text(nested.get("error"))
            if nested_error:
                record["error"] = nested_error
            _append_record(records, seen, record, max_records=max_records)

        canonical_tool = canonical_step_tool_name(
            action_id=action_id,
            output_payload=output_payload,
        )
        candidate_tool_names = dedupe_string_sequence(
            [action_id, canonical_tool, *tool_invocation_names(output_payload)]
        )
        known_names = [*required, *candidate_tool_names]
        candidate_keys = canonical_required_tool_keys(
            candidate_tool_names,
            known_tool_names=known_names,
        )
        matched_required_tool = next(
            (
                required_tool
                for required_tool in required
                if canonical_required_tool_key(
                    required_tool,
                    known_tool_names=known_names,
                )
                in candidate_keys
            ),
            None,
        )
        step_is_explicit_mcp_tool = (
            action_id.lower() == WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID
        )
        if (
            required_lookup
            and matched_required_tool is None
            and not step_is_explicit_mcp_tool
        ):
            continue
        if matched_required_tool is None and not step_is_explicit_mcp_tool:
            continue
        record = {
            "tool": matched_required_tool or canonical_tool,
            "status": status,
            "payload": (
                payload_projector(effective_payload)
                if payload_projector is not None
                else effective_payload
            ),
            "effective_payload": effective_payload,
            "workflow_step_evidence": True,
            "workflow_action_id": action_id,
            "workflow_id": envelope_workflow_id,
            "workflow_state_id": state_id,
        }
        if error_text:
            record["error"] = error_text
        _append_record(records, seen, record, max_records=max_records)
    return records
