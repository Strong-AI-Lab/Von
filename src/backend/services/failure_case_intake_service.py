"""Failure-case intake support for replay-backed prompt improvement.

This module deliberately stays below the policy layer.  It collects and
compacts existing turn evidence so VWL/prompt-governed workflows can classify a
failure, generate prompt hypotheses, and decide whether anything is promotable.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

FAILURE_CASE_INTAKE_SCHEMA_VERSION = "failure_case_intake.v1"
FAILURE_CASE_INTAKE_COLLECT_ACTION_ID = "failure_case.intake.collect"
FAILURE_CASE_INTAKE_MCP_TOOL_NAME = "failure_case_intake_collect"

_DEFAULT_TEXT_LIMIT = 4000
_MAX_CANDIDATE_TURNS = 24
_PROMPT_ID_KEYS = frozenset(
    {
        "base_prompt_id",
        "base_prompt_concept_id",
        "prompt_concept_id",
        "prompt_id",
        "resolved_prompt_concept_id",
        "selected_prompt_id",
        "selected_prompt_concept_id",
        "selector_prompt_id",
    }
)


@dataclass(frozen=True)
class MCPCallResult:
    payload: dict[str, Any]
    duration_ms: float | None = None


class _DefaultMCPInvoker:
    """Invoke the existing internal MCP catalogue through the real gateway."""

    def __init__(self) -> None:
        self._gateway: Any | None = None

    def _get_gateway(self) -> Any:
        if self._gateway is None:
            from ..integrations.internal_mcp import (
                InternalMCPGateway,
                InternalMCPTransport,
                build_default_catalogue,
            )

            self._gateway = InternalMCPGateway(
                catalogue=build_default_catalogue(),
                transport=InternalMCPTransport(),
                enabled=True,
            )
        return self._gateway

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> MCPCallResult:
        result = self._get_gateway().invoke(tool_name, dict(payload))
        result_payload = result.payload if isinstance(result.payload, Mapping) else {}
        return MCPCallResult(
            payload={str(key): value for key, value in result_payload.items()},
            duration_ms=float(result.duration_ms),
        )


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [_mapping(item) for item in value if isinstance(item, Mapping)]


def _dedupe_strings(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:20]


def _bounded_text(value: Any, *, limit: int) -> dict[str, Any] | None:
    text = _safe_str(value)
    if text is None:
        return None
    bounded_limit = max(0, int(limit))
    return {
        "text": text[:bounded_limit],
        "char_count": len(text),
        "sha256": _short_hash(text),
        "truncated": len(text) > bounded_limit,
    }


def _first_path(payload: Mapping[str, Any], *paths: Sequence[str]) -> Any:
    for path in paths:
        cursor: Any = payload
        found = True
        for key in path:
            if not isinstance(cursor, Mapping):
                found = False
                break
            cursor = cursor.get(key)
        if found and cursor not in (None, ""):
            return cursor
    return None


def _looks_like_signed_conversation_ref(value: Mapping[str, Any]) -> bool:
    return (
        _safe_str(value.get("schema_version")) == "conversation_scope_binding.v1"
        and _safe_str(value.get("binding_kind")) == "conversation_scope"
        and bool(_safe_str(value.get("signature")))
    )


def _unwrap_conversation_payload(
    conversation_ref: Mapping[str, Any] | None,
    chat_history_lookup: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    raw_ref = _mapping(conversation_ref)
    lookup = _mapping(chat_history_lookup)
    ref_kind = "absent"

    if raw_ref.get("kind") == "von_conversation_ref":
        nested_ref = _mapping(raw_ref.get("conversation_ref"))
        nested_lookup = _mapping(raw_ref.get("chat_history_lookup"))
        if nested_ref:
            raw_ref = nested_ref
            ref_kind = "emitted_von_conversation_ref"
        if nested_lookup and not lookup:
            lookup = nested_lookup
    elif raw_ref:
        ref_kind = (
            "signed_conversation_scope_binding"
            if _looks_like_signed_conversation_ref(raw_ref)
            else "conversation_ref_payload"
        )

    return raw_ref, lookup, ref_kind


def _conversation_tool_arguments(
    *,
    conversation_ref: Mapping[str, Any] | None,
    chat_history_lookup: Mapping[str, Any] | None,
    session_id: str | None,
    namespace: str | None,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    include_legacy: bool | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_ref, lookup, ref_kind = _unwrap_conversation_payload(
        conversation_ref,
        chat_history_lookup,
    )
    diagnostics: dict[str, Any] = {
        "input_kind": ref_kind,
        "signed_reference": _looks_like_signed_conversation_ref(raw_ref),
    }

    if raw_ref and _looks_like_signed_conversation_ref(raw_ref):
        args: dict[str, Any] = {"conversation_ref": raw_ref}
        if namespace:
            args["namespace"] = namespace
        if user_concept_id:
            args["user_concept_id"] = user_concept_id
        if organisation_concept_id:
            args["organisation_concept_id"] = organisation_concept_id
        if include_legacy is not None:
            args["include_legacy"] = bool(include_legacy)
        diagnostics["argument_mode"] = "signed_conversation_ref"
        return args, diagnostics

    resolved_session_id = (
        _safe_str(session_id)
        or _safe_str(raw_ref.get("chat_session_id"))
        or _safe_str(raw_ref.get("session_id"))
        or _safe_str(lookup.get("session_id"))
    )
    resolved_namespace = (
        _safe_str(namespace)
        or _safe_str(raw_ref.get("namespace"))
        or _safe_str(raw_ref.get("read_namespace"))
        or _safe_str(lookup.get("namespace"))
    )
    resolved_user = (
        _safe_str(user_concept_id)
        or _safe_str(raw_ref.get("user_concept_id"))
        or _safe_str(raw_ref.get("history_owner_user_id"))
        or _safe_str(lookup.get("user_id"))
        or _safe_str(lookup.get("user_concept_id"))
    )
    resolved_org = (
        _safe_str(organisation_concept_id)
        or _safe_str(raw_ref.get("organisation_concept_id"))
        or _safe_str(raw_ref.get("org_id"))
        or _safe_str(lookup.get("organisation_concept_id"))
    )

    args = {}
    if resolved_session_id:
        args["session_id"] = resolved_session_id
    if resolved_namespace:
        args["namespace"] = resolved_namespace
    if resolved_user:
        args["user_concept_id"] = resolved_user
    if resolved_org:
        args["organisation_concept_id"] = resolved_org
    if include_legacy is not None:
        args["include_legacy"] = bool(include_legacy)
    elif raw_ref.get("include_legacy") is not None:
        args["include_legacy"] = bool(raw_ref.get("include_legacy"))
    elif lookup.get("include_legacy") is not None:
        args["include_legacy"] = bool(lookup.get("include_legacy"))

    diagnostics.update(
        {
            "argument_mode": "raw_authorised_chat_history_parameters",
            "session_id_source": (
                "explicit"
                if _safe_str(session_id)
                else (
                    "conversation_ref"
                    if _safe_str(raw_ref.get("session_id"))
                    or _safe_str(raw_ref.get("chat_session_id"))
                    else (
                        "chat_history_lookup"
                        if _safe_str(lookup.get("session_id"))
                        else None
                    )
                )
            ),
        }
    )
    return args, diagnostics


def _invoke_mcp(
    invoker: Any,
    tool_name: str,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    try:
        if hasattr(invoker, "invoke") and callable(invoker.invoke):
            raw_result = invoker.invoke(tool_name, dict(payload))
        elif callable(invoker):
            raw_result = invoker(tool_name, dict(payload))
        else:
            raise TypeError("mcp_invoker must be callable or expose invoke()")
        duration_ms = None
        if isinstance(raw_result, MCPCallResult):
            result_payload = raw_result.payload
            duration_ms = raw_result.duration_ms
        elif hasattr(raw_result, "payload"):
            raw_payload = getattr(raw_result, "payload", None)
            result_payload = _mapping(raw_payload)
            raw_duration = getattr(raw_result, "duration_ms", None)
            duration_ms = (
                float(raw_duration) if isinstance(raw_duration, (int, float)) else None
            )
        else:
            result_payload = _mapping(raw_result)
        if duration_ms is None:
            duration_ms = (time.perf_counter() - started) * 1000.0
        success = result_payload.get("success") is not False
        call_record = {
            "tool_name": tool_name,
            "success": bool(success),
            "duration_ms": round(float(duration_ms), 3),
        }
        if not success:
            call_record["error_code"] = _safe_str(result_payload.get("error_code"))
            call_record["error"] = _safe_str(result_payload.get("error"))
        return result_payload, call_record
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000.0
        error_payload = {
            "success": False,
            "error_code": "mcp_invocation_failed",
            "error": str(exc),
            "tool_name": tool_name,
        }
        return error_payload, {
            "tool_name": tool_name,
            "success": False,
            "duration_ms": round(float(duration_ms), 3),
            "error_code": "mcp_invocation_failed",
            "error": str(exc),
        }


def _flatten_segments(segments_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_segments = segments_payload.get("segments")
    if not isinstance(raw_segments, Sequence) or isinstance(
        raw_segments,
        (str, bytes, bytearray),
    ):
        return []
    entries: list[dict[str, Any]] = []
    for segment_index, segment in enumerate(raw_segments):
        if isinstance(segment, Mapping):
            raw_history = segment.get("history")
            segment_entries = (
                raw_history
                if isinstance(raw_history, Sequence)
                and not isinstance(raw_history, (str, bytes, bytearray))
                else []
            )
        elif isinstance(segment, Sequence) and not isinstance(
            segment,
            (str, bytes, bytearray),
        ):
            segment_entries = segment
        else:
            continue
        for item in segment_entries:
            if not isinstance(item, Mapping):
                continue
            copied = _mapping(item)
            copied.setdefault("segment_index", segment_index)
            entries.append(copied)
    return entries


def _debug_request_id(entry: Mapping[str, Any]) -> str | None:
    debug = _mapping(entry.get("llm_debug_data"))
    return _safe_str(debug.get("request_id"))


def _candidate_workflow_id(payload: Mapping[str, Any]) -> str | None:
    return _safe_str(
        _first_path(
            payload,
            ("workflow_selection", "selected_workflow_id"),
            ("workflow_routing_diagnostics", "selected_workflow_id"),
            ("workflow_routing_diagnostics", "workflow_id"),
            ("workflow_discovery", "selected_workflow_id"),
            ("selected_workflow_trace", "selected_workflow_id"),
            ("selected_workflow_trace", "workflow_id"),
        )
    )


def _candidate_model_id(payload: Mapping[str, Any]) -> str | None:
    return _safe_str(
        _first_path(
            payload,
            ("model",),
            ("selected_model",),
            ("model_id",),
            ("llm_interaction", "model"),
            ("llm_step_envelope", "selected_model"),
        )
    )


def _build_turn_candidates(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if entry.get("role") != "assistant":
            continue
        debug = _mapping(entry.get("llm_debug_data"))
        request_id = _safe_str(debug.get("request_id"))
        if not request_id:
            continue
        history_location = _mapping(entry.get("history_location"))
        candidates.append(
            {
                "request_id": request_id,
                "model": _candidate_model_id(debug),
                "selected_workflow_id": _candidate_workflow_id(debug),
                "history_location": history_location or None,
                "history_index": history_location.get("history_index"),
                "content_preview": (_safe_str(entry.get("content")) or "")[:240],
                "entry_index": index,
            }
        )
        if len(candidates) >= _MAX_CANDIDATE_TURNS:
            break
    return candidates


def _resolve_request_id_from_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    target_model: str | None,
    workflow_id: str | None,
) -> tuple[str | None, dict[str, Any]]:
    filtered = list(candidates)
    model = _safe_str(target_model)
    if model:
        filtered = [
            candidate
            for candidate in filtered
            if (_safe_str(candidate.get("model")) or "").lower() == model.lower()
        ]
    workflow = _safe_str(workflow_id)
    if workflow:
        filtered = [
            candidate
            for candidate in filtered
            if (_safe_str(candidate.get("selected_workflow_id")) or "").lower()
            == workflow.lower()
        ]
    diagnostics = {
        "candidate_count": len(candidates),
        "filtered_candidate_count": len(filtered),
        "target_model_filter": model,
        "workflow_id_filter": workflow,
    }
    if len(filtered) == 1:
        return _safe_str(filtered[0].get("request_id")), diagnostics
    return None, diagnostics


def _find_target_entry(
    entries: Sequence[Mapping[str, Any]],
    request_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    target_index = None
    for index, entry in enumerate(entries):
        if _debug_request_id(entry) == request_id:
            target_index = index
            break
    if target_index is None:
        return None, None
    target_entry = _mapping(entries[target_index])
    user_entry: dict[str, Any] | None = None
    for prior_index in range(target_index - 1, -1, -1):
        prior = entries[prior_index]
        if prior.get("role") == "user":
            user_entry = _mapping(prior)
            break
    return target_entry, user_entry


def _extract_prompt_text(
    *,
    target_debug: Mapping[str, Any],
    user_entry: Mapping[str, Any] | None,
    diagnostics: Mapping[str, Any],
    turn_record: Mapping[str, Any],
) -> str | None:
    for candidate in (
        _safe_str(target_debug.get("prompt_text")),
        _safe_str(target_debug.get("prompt")),
        _safe_str(_mapping(target_debug.get("user_prompt")).get("prompt_text")),
        _safe_str(_mapping(target_debug.get("user_prompt")).get("content")),
        _safe_str(diagnostics.get("prompt_preview")),
        _safe_str(_mapping(turn_record.get("prompt")).get("preview")),
        (
            _safe_str(user_entry.get("content"))
            if isinstance(user_entry, Mapping)
            else None
        ),
    ):
        if candidate:
            return candidate
    return None


def _iter_nested_mappings(
    value: Any, *, max_depth: int = 5
) -> Sequence[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []

    def _walk(item: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(item, Mapping):
            found.append(item)
            for child in item.values():
                _walk(child, depth + 1)
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for child in list(item)[:80]:
                _walk(child, depth + 1)

    _walk(value, 0)
    return found


def _extract_prompt_metadata(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    ids: list[str] = []
    variant_selections: list[dict[str, Any]] = []
    for payload in payloads:
        for mapping in _iter_nested_mappings(payload):
            for key, value in mapping.items():
                if key in _PROMPT_ID_KEYS:
                    text = _safe_str(value)
                    if text:
                        ids.append(text)
            selection = mapping.get("prompt_variant_selection")
            if isinstance(selection, Mapping):
                compact = {
                    str(key): item
                    for key, item in selection.items()
                    if key
                    in {
                        "base_prompt_concept_id",
                        "selected_prompt_concept_id",
                        "match_reason",
                        "fallback_reason",
                        "matched_model",
                        "matched_model_family",
                    }
                }
                if compact:
                    variant_selections.append(compact)
    return {
        "prompt_ids": _dedupe_strings(ids),
        "prompt_variant_selections": variant_selections[:12],
    }


def _extract_model_summary(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    candidates: list[str] = []
    for payload in payloads:
        for mapping in _iter_nested_mappings(payload, max_depth=4):
            for key in ("model", "selected_model", "model_id"):
                text = _safe_str(mapping.get(key))
                if text:
                    candidates.append(text)
    deduped = _dedupe_strings(candidates)
    return {"primary_model": deduped[0] if deduped else None, "model_ids": deduped[:16]}


def _normalise_tool_status(value: Any) -> str:
    text = (_safe_str(value) or "").lower()
    if text in {"completed", "complete", "ok", "success", "succeeded"}:
        return "success"
    if text in {"failed", "failure", "error", "blocked", "cancelled", "canceled"}:
        return "failed"
    if text in {"pending", "started", "running"}:
        return "pending"
    return "unknown"


def _extract_tool_rows(*payloads: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        for field_name in ("tool_history", "tool_invocations", "invocations"):
            raw = payload.get(field_name)
            for item in _mapping_list(raw):
                tool_name = (
                    _safe_str(item.get("tool"))
                    or _safe_str(item.get("method"))
                    or _safe_str(item.get("name"))
                    or _safe_str(item.get("tool_name"))
                )
                if not tool_name:
                    continue
                status = _normalise_tool_status(
                    item.get("status") or item.get("outcome") or item.get("state")
                )
                row: dict[str, Any] = {"tool": tool_name, "status": status}
                duration = item.get("duration_ms")
                if isinstance(duration, (int, float)) and not isinstance(
                    duration, bool
                ):
                    row["duration_ms"] = float(duration)
                rows.append(row)
    return rows


def _extract_required_tools(*payloads: Mapping[str, Any]) -> list[str]:
    tools: list[str] = []
    for payload in payloads:
        execution = _mapping(payload.get("execution"))
        summary = _mapping(execution.get("summary"))
        for raw in (
            payload.get("workflow_required_effects_required_tools"),
            summary.get("workflow_required_effects_required_tools"),
        ):
            if isinstance(raw, Sequence) and not isinstance(
                raw, (str, bytes, bytearray)
            ):
                tools.extend(raw)
        for effect in _mapping_list(payload.get("required_effects")):
            raw_tools = effect.get("required_tools")
            if isinstance(raw_tools, Sequence) and not isinstance(
                raw_tools,
                (str, bytes, bytearray),
            ):
                tools.extend(raw_tools)
    return _dedupe_strings(tools)


def _build_tool_ledger(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    rows = _extract_tool_rows(*payloads)
    counts = {"total": 0, "success": 0, "failed": 0, "pending": 0, "unknown": 0}
    by_tool: dict[str, dict[str, Any]] = {}
    for row in rows:
        status = _normalise_tool_status(row.get("status"))
        counts["total"] += 1
        counts[status if status in counts else "unknown"] += 1
        tool = _safe_str(row.get("tool")) or "unknown"
        bucket = by_tool.setdefault(
            tool,
            {
                "tool": tool,
                "total": 0,
                "success": 0,
                "failed": 0,
                "pending": 0,
                "unknown": 0,
            },
        )
        bucket["total"] += 1
        bucket[status if status in bucket else "unknown"] += 1
    required_tools = _extract_required_tools(*payloads)
    observed = {tool.lower() for tool in by_tool}
    missing_required = [tool for tool in required_tools if tool.lower() not in observed]
    return {
        "counts": counts,
        "by_tool": sorted(by_tool.values(), key=lambda item: item["tool"]),
        "required_tools": required_tools,
        "missing_required_tools": missing_required,
    }


def _extract_completion_gate(
    diagnostics: Mapping[str, Any],
    turn_record: Mapping[str, Any],
    turn_summary: Mapping[str, Any],
) -> dict[str, Any]:
    gate = _mapping(turn_record.get("completion_gate")) or _mapping(
        diagnostics.get("completion_gate")
    )
    if gate:
        return gate
    summary: dict[str, Any] = {}
    for source_key, target_key in (
        ("decision", "decision"),
        ("decision_reason", "decision_reason"),
        ("safe_to_claim_completion", "safe_to_claim_completion"),
        ("requires_follow_up", "requires_follow_up"),
        ("blocking_effect_ids", "blocking_effect_ids"),
    ):
        if source_key in turn_summary:
            summary[target_key] = turn_summary.get(source_key)
    return summary


def _extract_critic(
    diagnostics: Mapping[str, Any],
    turn_record: Mapping[str, Any],
    turn_summary: Mapping[str, Any],
) -> dict[str, Any]:
    critic = _mapping(turn_record.get("critic")) or _mapping(diagnostics.get("critic"))
    if critic:
        return critic
    summary = _mapping(turn_summary.get("critic_summary"))
    return {"summary": summary} if summary else {}


def _selected_workflow_summary(
    diagnostics: Mapping[str, Any],
    turn_record: Mapping[str, Any],
    turn_summary: Mapping[str, Any],
    target_debug: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_selection = _mapping(turn_record.get("workflow_selection")) or _mapping(
        diagnostics.get("workflow_selection")
    )
    selected = (
        _safe_str(workflow_selection.get("selected_workflow_id"))
        or _safe_str(turn_summary.get("selected_workflow_id"))
        or _candidate_workflow_id(diagnostics)
        or _candidate_workflow_id(target_debug)
    )
    selector_verdict = (
        _safe_str(workflow_selection.get("selector_verdict"))
        or _safe_str(turn_summary.get("selector_verdict"))
        or _safe_str(
            _first_path(target_debug, ("workflow_selection", "selector_verdict"))
        )
    )
    return {
        "selected_workflow_id": selected,
        "selector_verdict": selector_verdict,
        "workflow_selection": workflow_selection or None,
    }


def _source_access_refs(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for payload in payloads:
        access = payload.get("mcp_access")
        if isinstance(access, Mapping):
            refs.update({str(key): item for key, item in access.items()})
    return refs


def _find_turn_summary(
    turn_list_payload: Mapping[str, Any],
    request_id: str | None,
) -> dict[str, Any]:
    for item in _mapping_list(turn_list_payload.get("items")):
        if request_id and _safe_str(item.get("request_id")) == request_id:
            return item
    items = _mapping_list(turn_list_payload.get("items"))
    return items[0] if len(items) == 1 else {}


def _failure_response(
    *,
    error_code: str,
    message: str,
    source_calls: Sequence[Mapping[str, Any]],
    source_reference: Mapping[str, Any],
    turn_candidates: Sequence[Mapping[str, Any]] | None = None,
    request_resolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": FAILURE_CASE_INTAKE_SCHEMA_VERSION,
        "success": False,
        "error_code": error_code,
        "error": message,
        "source_reference": dict(source_reference),
        "source_calls": [dict(item) for item in source_calls],
    }
    if turn_candidates is not None:
        payload["turn_candidates"] = [dict(item) for item in turn_candidates]
    if request_resolution is not None:
        payload["request_resolution"] = dict(request_resolution)
    return payload


def collect_failure_case_intake(
    *,
    conversation_ref: Mapping[str, Any] | None = None,
    chat_history_lookup: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    conversation_session_id: str | None = None,
    namespace: str | None = None,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    target_model: str | None = None,
    comparator_model: str | None = None,
    workflow_id: str | None = None,
    stage_id: str | None = None,
    include_legacy: bool | None = None,
    history_tail_limit: int | None = None,
    max_text_chars: int = _DEFAULT_TEXT_LIMIT,
    mcp_invoker: Any | None = None,
) -> dict[str, Any]:
    """Collect compact evidence for one failed turn.

    The collector is intentionally descriptive: it identifies what the model saw
    and what the runtime recorded, but it does not decide what prompt should be
    changed or whether a candidate variant should be promoted.
    """

    invoker = mcp_invoker or _DefaultMCPInvoker()
    source_calls: list[dict[str, Any]] = []
    request_id_value = _safe_str(request_id)
    namespace_value = _safe_str(namespace)
    text_limit = _safe_int(max_text_chars)
    if text_limit is None:
        text_limit = _DEFAULT_TEXT_LIMIT
    history_tail_limit_value = _safe_int(history_tail_limit)
    conversation_args, reference_diagnostics = _conversation_tool_arguments(
        conversation_ref=conversation_ref,
        chat_history_lookup=chat_history_lookup,
        session_id=_safe_str(session_id) or _safe_str(conversation_session_id),
        namespace=namespace_value,
        user_concept_id=_safe_str(user_concept_id),
        organisation_concept_id=_safe_str(organisation_concept_id),
        include_legacy=include_legacy,
    )

    segments_payload: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    segments_attempted = bool(
        conversation_args.get("conversation_ref") or conversation_args.get("session_id")
    )
    if segments_attempted:
        segment_args = {
            **conversation_args,
            "include_debug": True,
        }
        if history_tail_limit_value is not None:
            segment_args["history_tail_limit"] = history_tail_limit_value
        segments_payload, call = _invoke_mcp(
            invoker, "chat_history_get_segments", segment_args
        )
        source_calls.append(call)
        if segments_payload.get("success") is not False:
            entries = _flatten_segments(segments_payload)

    turn_candidates = _build_turn_candidates(entries)
    request_resolution: dict[str, Any] = {"provided_request_id": request_id_value}
    if not request_id_value:
        request_id_value, resolution = _resolve_request_id_from_candidates(
            turn_candidates,
            target_model=target_model,
            workflow_id=workflow_id,
        )
        request_resolution.update(resolution)
        request_resolution["resolved_request_id"] = request_id_value
        if not request_id_value:
            return _failure_response(
                error_code="request_id_required_for_failure_case_intake",
                message=(
                    "Failure-case intake needs an exact request_id or a unique "
                    "candidate turn after target_model/workflow_id filtering."
                ),
                source_calls=source_calls,
                source_reference=reference_diagnostics,
                turn_candidates=turn_candidates,
                request_resolution=request_resolution,
            )

    diagnostics_payload, call = _invoke_mcp(
        invoker,
        "turn_execution_get_diagnostics",
        {"request_id": request_id_value, "namespace": namespace_value},
    )
    source_calls.append(call)

    if (
        not entries
        and isinstance(diagnostics_payload.get("mcp_access"), Mapping)
        and isinstance(
            _mapping(diagnostics_payload.get("mcp_access")).get(
                "chat_history_get_segments"
            ),
            Mapping,
        )
    ):
        segment_descriptor = _mapping(
            _mapping(diagnostics_payload.get("mcp_access")).get(
                "chat_history_get_segments"
            )
        )
        segment_args = _mapping(segment_descriptor.get("arguments"))
        if segment_args:
            segments_payload, call = _invoke_mcp(
                invoker,
                "chat_history_get_segments",
                segment_args,
            )
            source_calls.append(call)
            if segments_payload.get("success") is not False:
                entries = _flatten_segments(segments_payload)
                turn_candidates = _build_turn_candidates(entries)

    target_entry, user_entry = _find_target_entry(entries, request_id_value)
    target_debug = _mapping(_mapping(target_entry).get("llm_debug_data"))

    debug_payload: dict[str, Any] = {}
    debug_descriptor = _mapping(
        _mapping(diagnostics_payload.get("mcp_access")).get(
            "chat_history_get_debug_entry"
        )
    )
    debug_args = _mapping(debug_descriptor.get("arguments"))
    if debug_args:
        debug_payload, call = _invoke_mcp(
            invoker,
            "chat_history_get_debug_entry",
            debug_args,
        )
        source_calls.append(call)
        debug_data = _mapping(debug_payload.get("llm_debug_data"))
        if debug_data:
            target_debug = debug_data

    turn_record_payload, call = _invoke_mcp(
        invoker,
        "turn_execution_get",
        {"request_id": request_id_value, "namespace": namespace_value},
    )
    source_calls.append(call)
    turn_record = _mapping(turn_record_payload)
    if turn_record.get("success") is False:
        turn_record = {}

    chat_session_id = (
        _safe_str(segments_payload.get("chat_session_id"))
        or _safe_str(segments_payload.get("session_id"))
        or _safe_str(diagnostics_payload.get("chat_session_id"))
        or _safe_str(
            _mapping(diagnostics_payload.get("history_location")).get("session_id")
        )
    )
    turn_list_payload: dict[str, Any] = {}
    if chat_session_id or namespace_value:
        list_args: dict[str, Any] = {
            "namespace": namespace_value,
            "limit": 50,
            "offset": 0,
        }
        if chat_session_id:
            list_args["session_id"] = chat_session_id
        turn_list_payload, call = _invoke_mcp(invoker, "turn_execution_list", list_args)
        source_calls.append(call)
    turn_summary = _find_turn_summary(turn_list_payload, request_id_value)

    prompt_text = _extract_prompt_text(
        target_debug=target_debug,
        user_entry=user_entry,
        diagnostics=diagnostics_payload,
        turn_record=turn_record,
    )
    response_text = (
        _safe_str(_mapping(turn_record.get("final_response")).get("text"))
        or _safe_str(_mapping(turn_record.get("final_response")).get("preview"))
        or _safe_str(_mapping(target_entry).get("content"))
    )
    workflow = _selected_workflow_summary(
        diagnostics_payload,
        turn_record,
        turn_summary,
        target_debug,
    )
    prompt_metadata = _extract_prompt_metadata(
        target_debug,
        diagnostics_payload,
        turn_record,
    )
    model_summary = _extract_model_summary(
        target_debug, diagnostics_payload, turn_record
    )
    completion_gate = _extract_completion_gate(
        diagnostics_payload,
        turn_record,
        turn_summary,
    )
    critic = _extract_critic(diagnostics_payload, turn_record, turn_summary)
    execution = _mapping(turn_record.get("execution")) or _mapping(
        diagnostics_payload.get("execution")
    )
    tool_ledger = _build_tool_ledger(
        diagnostics_payload,
        turn_record,
        execution,
        turn_summary,
    )

    history_location = (
        _mapping(_mapping(target_entry).get("history_location"))
        or _mapping(diagnostics_payload.get("history_location"))
        or _mapping(debug_payload.get("history_location"))
    )
    resolved_namespace = (
        _safe_str(diagnostics_payload.get("namespace"))
        or _safe_str(segments_payload.get("namespace"))
        or namespace_value
    )
    resolved_org = (
        _safe_str(diagnostics_payload.get("derived_organisation_concept_id"))
        or _safe_str(segments_payload.get("organisation_concept_id"))
        or _safe_str(organisation_concept_id)
    )
    target_model_value = _safe_str(target_model)
    target_model_matches = None
    if target_model_value and model_summary.get("primary_model"):
        target_model_matches = (
            str(model_summary["primary_model"]).lower() == target_model_value.lower()
        )

    source_status = {
        "chat_history_segments": {
            "attempted": segments_attempted,
            "success": (
                segments_payload.get("success") is not False
                if segments_attempted
                else None
            ),
            "segment_count": segments_payload.get("segment_count"),
            "entry_count": len(entries),
            "history_truncated": bool(segments_payload.get("history_truncated")),
        },
        "chat_history_debug_entry": {
            "attempted": bool(debug_args),
            "success": bool(debug_payload.get("success")) if debug_args else None,
        },
        "turn_execution_diagnostics": {
            "success": diagnostics_payload.get("success") is not False,
            "diagnostics_source": diagnostics_payload.get("diagnostics_source"),
        },
        "turn_execution_get": {
            "success": bool(turn_record_payload.get("success")),
        },
        "turn_execution_list": {
            "attempted": bool(turn_list_payload),
            "success": (
                turn_list_payload.get("success") is not False
                if turn_list_payload
                else None
            ),
            "total": turn_list_payload.get("total"),
        },
    }

    missing_core_sources: list[str] = []
    if diagnostics_payload.get("success") is False:
        missing_core_sources.append("turn_execution_get_diagnostics")
    if not prompt_text:
        missing_core_sources.append("prompt_text")
    if not response_text:
        missing_core_sources.append("user_visible_response")

    payload = {
        "schema_version": FAILURE_CASE_INTAKE_SCHEMA_VERSION,
        "success": not missing_core_sources,
        "error_code": (
            "failure_case_intake_incomplete" if missing_core_sources else None
        ),
        "request_id": request_id_value,
        "conversation": {
            "chat_session_id": chat_session_id,
            "namespace": resolved_namespace,
            "organisation_concept_id": resolved_org,
            "history_location": history_location or None,
            "access_mode": segments_payload.get("access_mode"),
            "identifier_binding": segments_payload.get("identifier_binding"),
        },
        "source_reference": reference_diagnostics,
        "request_resolution": request_resolution,
        "turn": {
            "request_id": request_id_value,
            "prompt": _bounded_text(prompt_text, limit=text_limit),
            "user_visible_response": _bounded_text(
                response_text,
                limit=text_limit,
            ),
        },
        "model": {
            **model_summary,
            "target_model": target_model_value,
            "target_model_matches_primary": target_model_matches,
            "comparator_model": _safe_str(comparator_model),
        },
        "workflow": {
            **workflow,
            "expected_workflow_id": _safe_str(workflow_id),
            "workflow_matches_expected": (
                None
                if not _safe_str(workflow_id)
                or not workflow.get("selected_workflow_id")
                else str(workflow["selected_workflow_id"]).lower()
                == str(workflow_id).lower()
            ),
            "stage_id": _safe_str(stage_id),
        },
        "prompt_metadata": prompt_metadata,
        "tool_ledger": tool_ledger,
        "completion_gate": completion_gate,
        "critic": critic,
        "required_effects": _mapping_list(turn_record.get("required_effects"))
        or _mapping_list(diagnostics_payload.get("required_effects")),
        "telemetry": {
            "source_status": source_status,
            "source_calls": source_calls,
            "missing_core_sources": missing_core_sources,
            "evidence_refs": _source_access_refs(
                diagnostics_payload,
                segments_payload,
                debug_payload,
            ),
        },
        "turn_candidates": turn_candidates,
        "policy_boundary": {
            "classification_performed": False,
            "prompt_hypothesis_generated": False,
            "promotion_recommendation_generated": False,
            "reason": (
                "Failure-case intake is support evidence only; classification, "
                "prompt-candidate generation, replay scoring, and promotion "
                "belong to represented workflow/prompt policy stages."
            ),
        },
    }
    if missing_core_sources:
        payload["error"] = (
            "Failure-case intake could not resolve all core evidence fields."
        )
    return payload


__all__ = [
    "FAILURE_CASE_INTAKE_COLLECT_ACTION_ID",
    "FAILURE_CASE_INTAKE_MCP_TOOL_NAME",
    "FAILURE_CASE_INTAKE_SCHEMA_VERSION",
    "MCPCallResult",
    "collect_failure_case_intake",
]
