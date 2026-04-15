"""Build bounded episode-critic evidence bundles for completed episodes.

This service composes existing authoritative evidence stores rather than
inventing another ad-hoc prompt payload:

- projected ``turn_execution_records``
- embedded/reconstructable chat-history debug payloads
- durable workflow instances and trace links
- workflow-use episodes

The resulting bundle is bounded for LLM consumption but carries receipts for the
authoritative source payloads so later analysis can refer back to stable
artefacts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .chat_history_service import get_chat_history_collection_service
from .namespace_service import coerce_namespace
from .turn_execution_record_service import (
    build_turn_execution_record,
    get_turn_execution_records_collection,
    infer_turn_execution_workflow_routing_from_debug,
)
from .workflow_episode_service import get_latest_workflow_use_episode
from ..workflows import get_workflow_execution_trace
from ..workflows.durable import WorkflowInstanceManager
from ..workflows.durable.execution_observability import (
    build_workflow_execution_trace_summary,
    build_workflow_instance_payload,
)
from ..workflows.durable.registry_factory import (
    build_durable_workflow_registry_read_only,
)
from ..workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
)

logger = logging.getLogger(__name__)

EPISODE_CRITIC_EVIDENCE_BUNDLE_SCHEMA_VERSION = "episode_critic_evidence_bundle.v1"
_DEFAULT_NEIGHBOUR_MESSAGE_COUNT = 2
_MAX_NEIGHBOUR_MESSAGE_COUNT = 6
_STRUCTURED_RESPONSE_FORMATS = frozenset(
    {
        "json",
        "json_array",
        "json_object",
        "dict",
        "list",
        "array",
        "object",
    }
)

_DEFAULT_SECTION_LIMITS: dict[str, int] = {
    "max_string_chars": 1000,
    "max_list_items": 30,
    "max_depth": 6,
}

_SECTION_LIMITS: dict[str, dict[str, int]] = {
    "turn_execution_record": {
        "max_string_chars": 1200,
        "max_list_items": 60,
        "max_depth": 7,
    },
    "selected_llm_debug": {
        "max_string_chars": 1000,
        "max_list_items": 50,
        "max_depth": 6,
    },
    "aux_llm_calls": {
        "max_string_chars": 900,
        "max_list_items": 20,
        "max_depth": 5,
    },
    "tool_ledger": {
        "max_string_chars": 1000,
        "max_list_items": 60,
        "max_depth": 6,
    },
    "workflow_instance": {
        "max_string_chars": 900,
        "max_list_items": 40,
        "max_depth": 6,
    },
    "workflow_episode": {
        "max_string_chars": 800,
        "max_list_items": 30,
        "max_depth": 5,
    },
    "workflow_trace": {
        "max_string_chars": 1000,
        "max_list_items": 60,
        "max_depth": 6,
    },
    "workflow_definition_identity": {
        "max_string_chars": 800,
        "max_list_items": 20,
        "max_depth": 4,
    },
    "neighbouring_context": {
        "max_string_chars": 700,
        "max_list_items": 12,
        "max_depth": 5,
    },
    "expected_context": {
        "max_string_chars": 1000,
        "max_list_items": 50,
        "max_depth": 6,
    },
}

_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_positive_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, coerced))


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def _payload_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
    except Exception:
        safe_value = _bounded_json_value(
            value,
            max_string_chars=_DEFAULT_SECTION_LIMITS["max_string_chars"],
            max_list_items=_DEFAULT_SECTION_LIMITS["max_list_items"],
            max_depth=_DEFAULT_SECTION_LIMITS["max_depth"],
        )
        return json.dumps(
            safe_value,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )


def _payload_char_count(value: Any) -> int:
    return len(_payload_text(value))


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_payload_text(value).encode("utf-8")).hexdigest()


def _payload_preview(value: Any, *, max_chars: int = 500) -> str | None:
    text = _payload_text(value)
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"


def _normalise_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        normalised: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str):
                normalised[key] = item
        return normalised
    return None


def _normalise_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        normalised = _normalise_mapping(item)
        if normalised is not None:
            items.append(normalised)
    return items


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return _normalise_mapping(value) or {}


def _normalise_string_list(value: Any) -> list[str]:
    if isinstance(value, (set, tuple)):
        value = list(value)
    if not isinstance(value, list):
        return []
    results: list[str] = []
    for item in value:
        text = _safe_str(item)
        if text and text not in results:
            results.append(text)
    return results


def _build_gap(
    gap_id: str,
    detail: str,
    *,
    required: bool,
    section_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "gap_id": gap_id,
        "detail": detail,
        "required": bool(required),
    }
    if section_id:
        payload["section_id"] = section_id
    return payload


def _bounded_json_value(
    value: Any,
    *,
    max_string_chars: int,
    max_list_items: int,
    max_depth: int,
    _depth: int = 0,
) -> Any:
    if _depth >= max_depth:
        return "[truncated: max depth reached]"

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _REDACT_PATTERNS):
            preview = value[: min(64, len(value))]
            return f"[redacted] {preview}..."
        if len(value) > max_string_chars:
            return (
                value[:max_string_chars]
                + f"\n... [truncated {len(value) - max_string_chars} chars]"
            )
        return value

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > max_list_items:
            head = list(value[: max(0, max_list_items - 1)])
            tail = list(value[-1:]) if max_list_items > 0 else []
            return [
                *[
                    _bounded_json_value(
                        item,
                        max_string_chars=max_string_chars,
                        max_list_items=max_list_items,
                        max_depth=max_depth,
                        _depth=_depth + 1,
                    )
                    for item in head
                ],
                {
                    "_truncated": True,
                    "_omitted_items": len(value) - len(head) - len(tail),
                },
                *[
                    _bounded_json_value(
                        item,
                        max_string_chars=max_string_chars,
                        max_list_items=max_list_items,
                        max_depth=max_depth,
                        _depth=_depth + 1,
                    )
                    for item in tail
                ],
            ]
        return [
            _bounded_json_value(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for item in value
        ]

    if isinstance(value, Mapping):
        payload: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if any(pattern.search(key) for pattern in _REDACT_PATTERNS):
                payload[key] = "[redacted]"
                continue
            payload[key] = _bounded_json_value(
                raw_value,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        return payload

    return str(value)


def _build_section(
    *,
    section_id: str,
    value: Any,
    source_system: str,
    required: bool,
    locator: Mapping[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    limits = dict(_DEFAULT_SECTION_LIMITS)
    limits.update(_SECTION_LIMITS.get(section_id, {}))
    bounded_value = (
        _bounded_json_value(
            value,
            max_string_chars=limits["max_string_chars"],
            max_list_items=limits["max_list_items"],
            max_depth=limits["max_depth"],
        )
        if value is not None
        else None
    )
    authoritative_sha = _hash_payload(value) if value is not None else None
    bounded_sha = _hash_payload(bounded_value) if bounded_value is not None else None
    authoritative_chars = _payload_char_count(value) if value is not None else None
    bounded_chars = (
        _payload_char_count(bounded_value) if bounded_value is not None else None
    )
    receipt: dict[str, Any] = {
        "section_id": section_id,
        "source_system": source_system,
        "required": bool(required),
        "present": value is not None,
        "authoritative_sha256": authoritative_sha,
        "authoritative_char_count": authoritative_chars,
        "bounded_sha256": bounded_sha,
        "bounded_char_count": bounded_chars,
        "truncated": bool(
            value is not None
            and bounded_value is not None
            and authoritative_sha != bounded_sha
        ),
    }
    if locator:
        receipt["locator"] = dict(locator)
    preview = _payload_preview(bounded_value)
    if preview:
        receipt["preview"] = preview
    return bounded_value, receipt


def _tool_name_family(tool_name: str) -> str:
    lowered = tool_name.strip().lower()
    if not lowered:
        return "unknown"
    if lowered.startswith("search_"):
        return "search"
    if lowered.startswith("rag_"):
        return "rag"
    if lowered.startswith("workflow_"):
        return "workflow"
    if lowered.startswith("jira_"):
        return "jira"
    if lowered.startswith("gmail_"):
        return "gmail"
    if lowered.startswith("testing_") or lowered.startswith("experiment_"):
        return "testing"
    if lowered.startswith("turn_execution_"):
        return "turn_execution"
    if lowered.startswith("renderer_"):
        return "renderer"
    if lowered.startswith("download_") or lowered.startswith("upload_"):
        return "file_transfer"
    if "_" in lowered:
        return lowered.split("_", 1)[0]
    return "misc"


def _extract_tool_names(invocations: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(invocations, list):
        return names
    for invocation in invocations:
        if not isinstance(invocation, Mapping):
            continue
        for key in ("tool", "name", "method"):
            tool_name = _safe_str(invocation.get(key))
            if tool_name and tool_name not in names:
                names.append(tool_name)
                break
    return names


def _extract_request_id_from_instance(instance: Any | None) -> str | None:
    if instance is None:
        return None

    inputs = getattr(instance, "inputs", None)
    if isinstance(inputs, Mapping):
        request_id = _safe_str(inputs.get("turn_id")) or _safe_str(
            inputs.get("request_id")
        )
        if request_id:
            return request_id

    workflow_data = getattr(instance, "workflow_data", None)
    if isinstance(workflow_data, Mapping):
        runtime = workflow_data.get("turn_execution_runtime")
        if isinstance(runtime, Mapping):
            request_id = _safe_str(runtime.get("request_id"))
            if request_id:
                return request_id

    outputs = getattr(instance, "outputs", None)
    if isinstance(outputs, Mapping):
        outcome = outputs.get("turn_execution_outcome")
        if isinstance(outcome, Mapping):
            request_id = _safe_str(outcome.get("request_id"))
            if request_id:
                return request_id

    return None


def _load_turn_execution_record(
    *,
    request_id: str | None,
    namespace: str | None,
) -> dict[str, Any] | None:
    request_id_value = _safe_str(request_id)
    if not request_id_value:
        return None
    coll = get_turn_execution_records_collection()
    if coll is None:
        return None
    query: dict[str, Any] = {"request_id": request_id_value}
    if namespace:
        query["namespace"] = namespace
    doc = coll.find_one(query, {"_id": 0})
    return _normalise_mapping(doc)


def _query_chat_history_document(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any] | None:
    coll = get_chat_history_collection_service(read_only=True)
    if coll is None:
        return None

    queries: list[dict[str, Any]] = []
    if user_id and session_id:
        base = {"user_id": user_id, "session_id": session_id}
        if namespace:
            queries.append({**base, "namespace": namespace})
        queries.append(base)
    elif request_id:
        base = {
            "history": {
                "$elemMatch": {
                    "role": "assistant",
                    "llm_debug_data.request_id": request_id,
                }
            }
        }
        if namespace:
            queries.append({**base, "namespace": namespace})
        queries.append(base)

    projection = {
        "_id": 0,
        "user_id": 1,
        "session_id": 1,
        "namespace": 1,
        "organisation_concept_id": 1,
        "history": 1,
    }
    for query in queries:
        try:
            doc = coll.find_one(query, projection)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[episode_critic] chat_history query failed: %s", exc)
            continue
        normalised = _normalise_mapping(doc)
        if normalised is not None:
            return normalised
    return None


def _extract_prompt_text_from_debug(
    llm_debug_data: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(llm_debug_data, Mapping):
        return None

    direct_prompt = _safe_str(llm_debug_data.get("prompt_text"))
    if direct_prompt:
        return direct_prompt

    messages = llm_debug_data.get("messages")
    if isinstance(messages, list):
        for item in reversed(messages):
            if not isinstance(item, Mapping):
                continue
            if item.get("role") != "user":
                continue
            content = _safe_str(item.get("content"))
            if content:
                return content

    user_prompt = llm_debug_data.get("user_prompt")
    if isinstance(user_prompt, Mapping):
        for key in ("prompt_text", "content", "preview"):
            content = _safe_str(user_prompt.get(key))
            if content:
                return content

    return None


def _resolve_history_context(
    *,
    request_id: str | None,
    user_id: str | None,
    session_id: str | None,
    namespace: str | None,
) -> dict[str, Any] | None:
    doc = _query_chat_history_document(
        user_id=user_id,
        session_id=session_id,
        request_id=request_id,
        namespace=namespace,
    )
    if not isinstance(doc, Mapping):
        return None

    history = doc.get("history")
    if not isinstance(history, list):
        return None

    target_index: int | None = None
    target_message: dict[str, Any] | None = None
    for index, raw_message in enumerate(history):
        if not isinstance(raw_message, Mapping):
            continue
        if raw_message.get("role") != "assistant":
            continue
        llm_debug = raw_message.get("llm_debug_data")
        if request_id and isinstance(llm_debug, Mapping):
            debug_request_id = _safe_str(llm_debug.get("request_id"))
            if debug_request_id == request_id:
                target_index = index
                target_message = dict(raw_message)
                break

    if target_message is None:
        return None

    prompt_text = None
    llm_debug = target_message.get("llm_debug_data")
    if isinstance(llm_debug, Mapping):
        prompt_text = _extract_prompt_text_from_debug(llm_debug)
    if prompt_text is None and target_index is not None:
        for prior_index in range(target_index - 1, -1, -1):
            prior = history[prior_index]
            if not isinstance(prior, Mapping):
                continue
            if prior.get("role") == "user":
                prompt_text = _safe_str(prior.get("content"))
                if prompt_text:
                    break

    return {
        "user_id": _safe_str(doc.get("user_id")),
        "session_id": _safe_str(doc.get("session_id")),
        "namespace": _safe_str(doc.get("namespace")),
        "org_id": _safe_str(doc.get("organisation_concept_id")),
        "history": _normalise_mapping_list(history),
        "target_index": target_index,
        "target_message": target_message,
        "target_llm_debug": _normalise_mapping(llm_debug),
        "prompt_text": prompt_text,
    }


def _reconstruct_turn_execution_record_from_history(
    *,
    request_id: str,
    history_context: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    llm_debug = history_context.get("target_llm_debug")
    if not isinstance(llm_debug, Mapping):
        return None, None

    embedded = llm_debug.get("turn_execution_record")
    if isinstance(embedded, Mapping):
        payload = _mapping_or_empty(embedded)
        payload.setdefault(
            "reconstruction",
            {
                "source": "chat_history.llm_debug_data.turn_execution_record",
                "method": "embedded",
            },
        )
        return payload, "chat_history.embedded_turn_execution_record"

    target_message = history_context.get("target_message")
    if not isinstance(target_message, Mapping):
        return None, None

    rebuilt = build_turn_execution_record(
        request_id=request_id,
        session_id=_safe_str(history_context.get("session_id")),
        namespace=_safe_str(history_context.get("namespace")),
        actor_concept_id=_safe_str(llm_debug.get("actor_concept_id")),
        user_id=_safe_str(history_context.get("user_id")),
        org_id=_safe_str(history_context.get("org_id")),
        prompt_text=_safe_str(history_context.get("prompt_text")),
        response_text=_safe_str(target_message.get("content")),
        interaction_timestamp_utc=llm_debug.get("interaction_timestamp_utc")
        or target_message.get("timestamp"),
        workflow_discovery=_normalise_mapping(llm_debug.get("workflow_discovery")),
        workflow_routing=infer_turn_execution_workflow_routing_from_debug(
            llm_debug=llm_debug
        ),
        tool_invocations=(
            llm_debug.get("tool_invocations")
            if isinstance(llm_debug.get("tool_invocations"), list)
            else []
        ),
        search_evidence=(
            llm_debug.get("search_evidence")
            if isinstance(llm_debug.get("search_evidence"), list)
            else []
        ),
        turn_execution_diagnostics=_normalise_mapping(
            llm_debug.get("turn_execution_diagnostics")
        ),
        aux_llm_calls=(
            llm_debug.get("aux_llm_calls")
            if isinstance(llm_debug.get("aux_llm_calls"), list)
            else []
        ),
    )
    rebuilt["reconstruction"] = {
        "source": "chat_history.llm_debug_data",
        "method": "build_turn_execution_record",
    }
    return rebuilt, "chat_history.reconstructed_turn_execution_record"


def _build_neighbouring_context(
    *,
    history_context: Mapping[str, Any] | None,
    neighbour_message_count: int,
) -> dict[str, Any] | None:
    if not isinstance(history_context, Mapping):
        return None
    history = history_context.get("history")
    target_index = history_context.get("target_index")
    if not isinstance(history, list) or not isinstance(target_index, int):
        return None

    start = max(0, target_index - neighbour_message_count)
    end = min(len(history), target_index + neighbour_message_count + 1)
    messages: list[dict[str, Any]] = []
    for index in range(start, end):
        raw = history[index]
        if not isinstance(raw, Mapping):
            continue
        llm_debug = raw.get("llm_debug_data")
        message_payload: dict[str, Any] = {
            "history_index": index,
            "role": _safe_str(raw.get("role")),
            "content": _safe_str(raw.get("content")),
            "timestamp": raw.get("timestamp"),
            "is_target": index == target_index,
        }
        if isinstance(llm_debug, Mapping):
            request_id = _safe_str(llm_debug.get("request_id"))
            if request_id:
                message_payload["request_id"] = request_id
        messages.append(message_payload)

    return {
        "session_id": _safe_str(history_context.get("session_id")),
        "target_history_index": target_index,
        "message_count": len(messages),
        "messages": messages,
    }


def _build_selected_llm_debug(llm_debug: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(llm_debug, Mapping):
        return None
    diagnostics = llm_debug.get("turn_execution_diagnostics")
    selected_diagnostics: dict[str, Any] | None = None
    if isinstance(diagnostics, Mapping):
        selected_diagnostics = {}
        for key in (
            "latest_progress",
            "timing_breakdown",
            "workflow_stage_path",
            "workflow_stage_model",
            "workflow_routing_diagnostics",
        ):
            if key in diagnostics:
                selected_diagnostics[key] = diagnostics.get(key)
        if not selected_diagnostics:
            selected_diagnostics = None

    payload: dict[str, Any] = {
        "request_id": _safe_str(llm_debug.get("request_id")),
        "model": _safe_str(llm_debug.get("model")),
        "workflow_discovery": _normalise_mapping(llm_debug.get("workflow_discovery")),
        "workflow_routing": infer_turn_execution_workflow_routing_from_debug(
            llm_debug=llm_debug
        ),
        "llm_allowed_tools": _normalise_string_list(llm_debug.get("llm_allowed_tools")),
        "allowed_write_tools": _normalise_string_list(
            llm_debug.get("allowed_write_tools")
        ),
        "turn_execution_diagnostics": selected_diagnostics,
    }
    if "workflow_routing_diagnostics" in llm_debug and isinstance(
        llm_debug.get("workflow_routing_diagnostics"), Mapping
    ):
        payload["workflow_routing_diagnostics"] = _mapping_or_empty(
            llm_debug.get("workflow_routing_diagnostics")
        )
    if "response_chars" in llm_debug:
        payload["response_chars"] = llm_debug.get("response_chars")
    if "fallback_used" in llm_debug:
        payload["fallback_used"] = bool(llm_debug.get("fallback_used"))
    return payload


def _build_tool_ledger(
    *,
    turn_record: Mapping[str, Any] | None,
    llm_debug: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    execution = (
        _mapping_or_empty(turn_record.get("execution"))
        if isinstance(turn_record, Mapping)
        else {}
    )
    tool_invocations = _normalise_mapping_list(execution.get("tool_invocations"))
    if not tool_invocations and isinstance(llm_debug, Mapping):
        tool_invocations = _normalise_mapping_list(llm_debug.get("tool_invocations"))

    search_evidence = _normalise_mapping_list(execution.get("search_evidence"))
    if not search_evidence and isinstance(llm_debug, Mapping):
        search_evidence = _normalise_mapping_list(llm_debug.get("search_evidence"))

    tool_messages = (
        llm_debug.get("tool_messages")
        if isinstance(llm_debug, Mapping)
        and isinstance(llm_debug.get("tool_messages"), list)
        else []
    )

    allowed_tool_names = _normalise_string_list(
        llm_debug.get("llm_allowed_tools") if isinstance(llm_debug, Mapping) else []
    )
    invoked_tool_names = _extract_tool_names(tool_invocations)
    if not allowed_tool_names:
        allowed_tool_names = list(invoked_tool_names)

    allowed_write_tools = _normalise_string_list(
        llm_debug.get("allowed_write_tools") if isinstance(llm_debug, Mapping) else []
    )
    tool_families = sorted({_tool_name_family(name) for name in allowed_tool_names})

    if not any((tool_invocations, search_evidence, tool_messages, allowed_tool_names)):
        return None

    return {
        "tool_invocations": tool_invocations,
        "search_evidence": search_evidence,
        "tool_messages": tool_messages,
        "allowed_tool_names": allowed_tool_names,
        "allowed_write_tools": allowed_write_tools,
        "allowed_tool_families": tool_families,
        "tool_invocation_count": len(tool_invocations),
        "search_evidence_count": len(search_evidence),
    }


def _build_workflow_instance_section(instance: Any | None) -> dict[str, Any] | None:
    if instance is None:
        return None
    workflow_data = _mapping_or_empty(getattr(instance, "workflow_data", None))
    outputs = _mapping_or_empty(getattr(instance, "outputs", None))
    return {
        "status": build_workflow_instance_payload(
            instance,
            include_inputs=True,
            include_outputs=True,
            include_workflow_data=False,
        ),
        "turn_execution_runtime": _normalise_mapping(
            workflow_data.get("turn_execution_runtime")
        ),
        "turn_execution_outcome": _normalise_mapping(
            outputs.get("turn_execution_outcome")
        ),
        "workflow_execution_summary": _normalise_mapping(
            outputs.get("workflow_execution_summary")
        ),
        "workflow_definition_identity": _normalise_mapping(
            outputs.get("workflow_definition_identity")
        ),
    }


def _resolve_workflow_definition_identity(
    *,
    workflow_id: str | None,
    turn_record: Mapping[str, Any] | None,
    instance: Any | None,
    episode: Mapping[str, Any] | None,
    trace_doc: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(turn_record, Mapping):
        execution = turn_record.get("execution")
        if isinstance(execution, Mapping):
            contract = execution.get("required_effects_contract")
            if isinstance(contract, Mapping):
                identity = contract.get("workflow_definition_identity")
                if isinstance(identity, Mapping):
                    return (
                        _mapping_or_empty(identity),
                        "turn_execution_record.execution.required_effects_contract",
                    )

    if instance is not None:
        workflow_data = getattr(instance, "workflow_data", None)
        if isinstance(workflow_data, Mapping):
            runtime = workflow_data.get("turn_execution_runtime")
            if isinstance(runtime, Mapping):
                identity = runtime.get("workflow_definition_identity")
                if isinstance(identity, Mapping):
                    return (
                        _mapping_or_empty(identity),
                        "workflow_instance.workflow_data.turn_execution_runtime",
                    )
                contract = runtime.get("contract")
                if isinstance(contract, Mapping):
                    identity = contract.get("workflow_definition_identity")
                    if isinstance(identity, Mapping):
                        return (
                            _mapping_or_empty(identity),
                            "workflow_instance.workflow_data.turn_execution_runtime.contract",
                        )
        outputs = getattr(instance, "outputs", None)
        if isinstance(outputs, Mapping):
            identity = outputs.get("workflow_definition_identity")
            if isinstance(identity, Mapping):
                return _mapping_or_empty(identity), "workflow_instance.outputs"

    if isinstance(episode, Mapping):
        identity = episode.get("workflow_definition_identity")
        if isinstance(identity, Mapping):
            return _mapping_or_empty(identity), "workflow_use_episode"

    if isinstance(trace_doc, Mapping):
        identity = trace_doc.get("workflow_definition_identity")
        if isinstance(identity, Mapping):
            return _mapping_or_empty(identity), "workflow_trace"
        metadata = trace_doc.get("metadata")
        if isinstance(metadata, Mapping):
            identity = metadata.get("workflow_definition_identity")
            if isinstance(identity, Mapping):
                return _mapping_or_empty(identity), "workflow_trace.metadata"

    workflow_id_value = _safe_str(workflow_id)
    if not workflow_id_value:
        return None, None

    try:
        registry = build_durable_workflow_registry_read_only()
        definition = registry.get(workflow_id_value)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "[episode_critic] workflow identity registry lookup failed for %s: %s",
            workflow_id_value,
            exc,
        )
        definition = None

    if definition is None:
        return None, None

    return (
        build_workflow_definition_identity(
            workflow_id=workflow_id_value,
            source="runtime_registry",
            definition=definition,
            authoritative_definition=None,
        ),
        "workflow_registry",
    )


def _build_expected_context(
    *,
    turn_record: Mapping[str, Any] | None,
    llm_debug: Mapping[str, Any] | None,
    workflow_identity: Mapping[str, Any] | None,
    instance: Any | None,
) -> dict[str, Any] | None:
    if not isinstance(turn_record, Mapping):
        if instance is None:
            return None
        workflow_id = _safe_str(getattr(instance, "workflow_id", None))
        source_event_type = _safe_str(getattr(instance, "source_event_type", None))
        source_event_id = _safe_str(getattr(instance, "source_event_id", None))
        return {
            "selected_workflow": {
                "selected_workflow_id": workflow_id,
                "source_event_type": source_event_type,
                "source_event_id": source_event_id,
            },
            "routing_quality_signals": None,
            "workflow_stage_path": None,
            "workflow_definition_identity": _normalise_mapping(workflow_identity),
            "intended_effects": [],
            "completion_criteria": {
                "terminal_status": _safe_str(getattr(instance, "status", None)),
                "final_state": _safe_str(getattr(instance, "current_state", None)),
                "has_outputs": getattr(instance, "outputs", None) is not None,
                "source_event_type": source_event_type,
                "source_event_id": source_event_id,
                "step_index": getattr(instance, "step_index", None),
                "retry_count": getattr(instance, "retry_count", None),
            },
            "allowed_tool_names": [],
            "allowed_write_tools": [],
            "allowed_tool_families": [],
            "fail_closed_policy": {
                "profile_fail_closed": False,
                "profile_fail_closed_reason": None,
                "fail_closed_on_missing_requirements": False,
                "completion_block_on_unresolved_effects": False,
            },
        }

    execution = _mapping_or_empty(turn_record.get("execution"))
    contract = _mapping_or_empty(execution.get("required_effects_contract"))
    profile_resolution = _mapping_or_empty(contract.get("profile_resolution"))
    default_decision_policy = _mapping_or_empty(
        contract.get("default_decision_policy")
    )
    required_effects = _normalise_mapping_list(turn_record.get("required_effects"))
    postcondition_checks = _normalise_mapping_list(turn_record.get("postcondition_checks"))
    completion_gate = _mapping_or_empty(turn_record.get("completion_gate"))
    critic = _mapping_or_empty(turn_record.get("critic"))
    llm_allowed_tools = _normalise_string_list(
        llm_debug.get("llm_allowed_tools") if isinstance(llm_debug, Mapping) else []
    )
    allowed_write_tools = _normalise_string_list(
        llm_debug.get("allowed_write_tools") if isinstance(llm_debug, Mapping) else []
    )
    invoked_tools = _extract_tool_names(execution.get("tool_invocations"))
    allowed_tool_names = list(llm_allowed_tools or invoked_tools)
    if allowed_write_tools:
        for tool_name in allowed_write_tools:
            if tool_name not in allowed_tool_names:
                allowed_tool_names.append(tool_name)

    workflow_data = _mapping_or_empty(getattr(instance, "workflow_data", None))
    runtime = _mapping_or_empty(workflow_data.get("turn_execution_runtime"))
    runtime_contract = _mapping_or_empty(runtime.get("contract"))
    selection = _normalise_mapping(runtime_contract.get("selection")) or _normalise_mapping(
        turn_record.get("workflow_selection")
    ) or {}
    routing_quality_signals = _build_routing_quality_signals(
        turn_record=turn_record,
        llm_debug=llm_debug,
    )

    return {
        "selected_workflow": (
            selection
        ),
        "routing_quality_signals": routing_quality_signals,
        "workflow_stage_path": (
            runtime.get("workflow_stage_path")
            if isinstance(runtime, Mapping) and runtime.get("workflow_stage_path") is not None
            else execution.get("workflow_stage_path")
        ),
        "workflow_definition_identity": (
            _normalise_mapping(workflow_identity)
        ),
        "intended_effects": required_effects,
        "completion_criteria": {
            "required_effect_count": len(required_effects),
            "postcondition_check_count": len(postcondition_checks),
            "default_decision_policy": default_decision_policy,
            "profile_resolution": profile_resolution,
            "completion_gate_workflow_id": _safe_str(completion_gate.get("workflow_id")),
            "critic_workflow_id": _safe_str(critic.get("workflow_id")),
        },
        "allowed_tool_names": allowed_tool_names,
        "allowed_write_tools": allowed_write_tools,
        "allowed_tool_families": sorted(
            {_tool_name_family(tool_name) for tool_name in allowed_tool_names}
        ),
        "fail_closed_policy": {
            "profile_fail_closed": bool(profile_resolution.get("fail_closed")),
            "profile_fail_closed_reason": _safe_str(
                profile_resolution.get("fail_closed_reason")
            ),
            "fail_closed_on_missing_requirements": default_decision_policy.get(
                "fail_closed_on_missing_requirements"
            ),
            "completion_block_on_unresolved_effects": default_decision_policy.get(
                "completion_block_on_unresolved_effects"
            ),
        },
    }


def _build_routing_quality_signals(
    *,
    turn_record: Mapping[str, Any],
    llm_debug: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    workflow_selection = _mapping_or_empty(turn_record.get("workflow_selection"))
    routing_diagnostics = _mapping_or_empty(turn_record.get("workflow_routing_diagnostics"))
    if not routing_diagnostics and isinstance(llm_debug, Mapping):
        direct_routing = llm_debug.get("workflow_routing_diagnostics")
        if isinstance(direct_routing, Mapping):
            routing_diagnostics = _mapping_or_empty(direct_routing)
        else:
            diagnostics = _mapping_or_empty(llm_debug.get("turn_execution_diagnostics"))
            routing_diagnostics = _mapping_or_empty(
                diagnostics.get("workflow_routing_diagnostics")
            )

    selected_workflow_id = _safe_str(workflow_selection.get("selected_workflow_id")) or _safe_str(
        routing_diagnostics.get("selected_workflow_id")
    )
    selected_workflow_lower = (selected_workflow_id or "").lower()

    discovery = _mapping_or_empty(routing_diagnostics.get("discovery"))
    selector = _mapping_or_empty(routing_diagnostics.get("selector"))
    dispatch = _mapping_or_empty(routing_diagnostics.get("dispatch"))
    completion_gate = _mapping_or_empty(turn_record.get("completion_gate"))

    routing_match_ids = _normalise_string_list(discovery.get("routing_match_ids"))
    if not routing_match_ids:
        routing_match_ids = [
            workflow_id
            for workflow_id in (
                _safe_str(item.get("concept_id"))
                for item in _normalise_mapping_list(discovery.get("routing_matches"))
            )
            if workflow_id
        ]

    unselected_routing_match_ids = [
        workflow_id
        for workflow_id in routing_match_ids
        if workflow_id.lower() != selected_workflow_lower
    ]
    selected_workflow_in_routing_matches = None
    if selected_workflow_id and routing_match_ids:
        selected_workflow_in_routing_matches = (
            selected_workflow_id.lower()
            in {workflow_id.lower() for workflow_id in routing_match_ids}
        )

    selection_rationale = _safe_str(workflow_selection.get("selection_rationale")) or _safe_str(
        routing_diagnostics.get("selection_rationale")
    )
    selector_prompt_id = _safe_str(selector.get("prompt_id")) or _safe_str(
        workflow_selection.get("prompt_id")
    )
    selector_requested_prompt_ids = _normalise_string_list(
        selector.get("requested_prompt_ids")
        or workflow_selection.get("requested_prompt_ids")
    )
    dispatch_failure_codes = _normalise_string_list(dispatch.get("failure_codes"))
    completion_gate_failure_codes = _normalise_string_list(
        completion_gate.get("blocking_failure_codes")
    )

    diagnostic_flags: list[str] = []
    if unselected_routing_match_ids:
        diagnostic_flags.append("unselected_routing_matches_present")
    if selected_workflow_id and routing_match_ids and selected_workflow_in_routing_matches is False:
        diagnostic_flags.append("selected_workflow_not_in_routing_matches")
    if bool(dispatch.get("zero_tools_executed")):
        diagnostic_flags.append("dispatch_zero_execution")
    if dispatch_failure_codes:
        diagnostic_flags.append("dispatch_failure_recorded")
    if bool(completion_gate.get("requires_follow_up")):
        diagnostic_flags.append("completion_requires_follow_up")
    if completion_gate_failure_codes:
        diagnostic_flags.append("completion_gate_failure_recorded")
    if _safe_str(selector.get("prompt_failure_reason")):
        diagnostic_flags.append("selector_prompt_failure_recorded")
    if not selection_rationale:
        diagnostic_flags.append("selection_rationale_missing")

    if not any(
        (
            selected_workflow_id,
            selector_prompt_id,
            routing_match_ids,
            unselected_routing_match_ids,
            dispatch_failure_codes,
            completion_gate_failure_codes,
            diagnostic_flags,
        )
    ):
        return None

    return {
        "selected_workflow_id": selected_workflow_id,
        "selector_prompt_id": selector_prompt_id,
        "selector_requested_prompt_ids": selector_requested_prompt_ids,
        "selector_verdict": _safe_str(workflow_selection.get("selector_verdict"))
        or _safe_str(routing_diagnostics.get("selector_verdict")),
        "selector_source": _safe_str(workflow_selection.get("selector_source"))
        or _safe_str(routing_diagnostics.get("selector_source")),
        "selection_rationale": selection_rationale,
        "routing_match_count": len(routing_match_ids),
        "routing_match_ids": routing_match_ids,
        "selected_workflow_in_routing_matches": selected_workflow_in_routing_matches,
        "unselected_routing_match_ids": unselected_routing_match_ids,
        "dispatch_selected_execution_mode": _safe_str(
            dispatch.get("selected_execution_mode")
        ),
        "dispatch_terminal_status": _safe_str(dispatch.get("dispatch_terminal_status")),
        "dispatch_zero_execution": bool(dispatch.get("zero_tools_executed")),
        "dispatch_failure_codes": dispatch_failure_codes,
        "completion_gate_decision": _safe_str(completion_gate.get("decision")),
        "completion_gate_requires_follow_up": bool(
            completion_gate.get("requires_follow_up")
        ),
        "completion_gate_blocking_failure_codes": completion_gate_failure_codes,
        "diagnostic_flags": diagnostic_flags,
    }


def _build_format_over_content_diagnostic(
    *,
    turn_record: Mapping[str, Any] | None,
    selected_llm_debug: Mapping[str, Any] | None,
    expected_context: Mapping[str, Any] | None,
    tool_ledger: Mapping[str, Any] | None,
    fail_closed_reason_codes: Sequence[str],
) -> dict[str, Any] | None:
    if not isinstance(turn_record, Mapping):
        return None

    routing_diagnostics = _mapping_or_empty(turn_record.get("workflow_routing_diagnostics"))
    selector = _mapping_or_empty(routing_diagnostics.get("selector"))
    dispatch = _mapping_or_empty(routing_diagnostics.get("dispatch"))
    completion_gate = _mapping_or_empty(turn_record.get("completion_gate"))
    routing_quality_signals = (
        _mapping_or_empty(expected_context.get("routing_quality_signals"))
        if isinstance(expected_context, Mapping)
        else {}
    )
    selection_metadata = _mapping_or_empty(selector.get("selection_metadata"))
    selected_model_candidate = _mapping_or_empty(selector.get("selected_model_candidate"))

    raw_response_format = (
        _safe_str(selection_metadata.get("raw_response_format"))
        or _safe_str(selector.get("raw_response_format"))
    )
    raw_response_format_lower = (raw_response_format or "").lower()
    structured_selection_detected = bool(
        selection_metadata.get("structured_selection_detected")
    )
    structured_output_signal = structured_selection_detected or (
        raw_response_format_lower in _STRUCTURED_RESPONSE_FORMATS
    )

    selected_model = _safe_str(selector.get("model_name")) or (
        _safe_str(selected_llm_debug.get("model"))
        if isinstance(selected_llm_debug, Mapping)
        else None
    )
    selected_provider = (
        _safe_str(selected_model_candidate.get("provider"))
        or _safe_str(selected_model_candidate.get("provider_name"))
        or _safe_str(selected_model_candidate.get("llm_provider"))
        or _safe_str(selected_model_candidate.get("provider_id"))
    )

    allowed_tool_families = (
        _normalise_string_list(expected_context.get("allowed_tool_families"))
        if isinstance(expected_context, Mapping)
        else []
    )
    allowed_tool_names = (
        _normalise_string_list(expected_context.get("allowed_tool_names"))
        if isinstance(expected_context, Mapping)
        else []
    )
    grounded_tool_path_available = bool(
        {"search", "retrieval"}.intersection(
            {item.lower() for item in allowed_tool_families}
        )
    ) or any(
        tool_name.lower().startswith(("search_", "retrieve_", "query_"))
        for tool_name in allowed_tool_names
    )

    tool_invocation_count = (
        int(tool_ledger.get("tool_invocation_count") or 0)
        if isinstance(tool_ledger, Mapping)
        else 0
    )
    search_evidence_count = (
        int(tool_ledger.get("search_evidence_count") or 0)
        if isinstance(tool_ledger, Mapping)
        else 0
    )
    grounded_tool_path_unused = grounded_tool_path_available and (
        tool_invocation_count <= 0 and search_evidence_count <= 0
    )

    completion_requires_follow_up = bool(completion_gate.get("requires_follow_up"))
    dispatch_zero_execution = bool(dispatch.get("zero_tools_executed"))
    dispatch_failure_codes = _normalise_string_list(dispatch.get("failure_codes"))
    completion_gate_failure_codes = _normalise_string_list(
        completion_gate.get("blocking_failure_codes")
    )
    routing_alternatives_present = bool(
        _normalise_string_list(routing_quality_signals.get("unselected_routing_match_ids"))
    )
    parse_or_repair_pressure = bool(
        selector.get("fallback_used")
        or int(selector.get("model_failure_count") or 0) > 0
        or _safe_str(selector.get("prompt_failure_reason"))
    )

    if fail_closed_reason_codes:
        return {
            "status": "insufficient_evidence",
            "summary": (
                "Episode evidence was incomplete, so format-over-content pressure "
                "cannot be diagnosed reliably."
            ),
            "confidence": 0.0,
            "reason_codes": [
                "episode_bundle_fail_closed",
                *list(fail_closed_reason_codes)[:6],
            ],
            "selected_model": selected_model,
            "selected_provider": selected_provider,
            "observed_stage_id": "selector_decision" if structured_output_signal else None,
            "raw_response_format": raw_response_format,
            "structured_output_signal": structured_output_signal,
            "grounded_tool_path_available": grounded_tool_path_available,
            "grounded_tool_path_unused": grounded_tool_path_unused,
            "tool_invocation_count": tool_invocation_count,
            "search_evidence_count": search_evidence_count,
        }

    if not structured_output_signal:
        return {
            "status": "not_indicated",
            "summary": (
                "The retained episode evidence does not show a structured-output "
                "contract strong enough to blame for the content failure."
            ),
            "confidence": 0.18,
            "reason_codes": ["no_structured_output_signal"],
            "selected_model": selected_model,
            "selected_provider": selected_provider,
            "observed_stage_id": None,
            "raw_response_format": raw_response_format,
            "structured_output_signal": False,
            "grounded_tool_path_available": grounded_tool_path_available,
            "grounded_tool_path_unused": grounded_tool_path_unused,
            "tool_invocation_count": tool_invocation_count,
            "search_evidence_count": search_evidence_count,
        }

    contributing_reason_codes: list[str] = ["structured_output_contract_present"]
    if grounded_tool_path_unused:
        contributing_reason_codes.append("grounded_tool_path_unused")
    if dispatch_zero_execution:
        contributing_reason_codes.append("dispatch_zero_execution")
    if completion_requires_follow_up:
        contributing_reason_codes.append("completion_requires_follow_up")
    if dispatch_failure_codes:
        contributing_reason_codes.append("dispatch_failure_recorded")
    if completion_gate_failure_codes:
        contributing_reason_codes.append("completion_gate_failure_recorded")
    if routing_alternatives_present:
        contributing_reason_codes.append("routing_alternatives_present")
    if parse_or_repair_pressure:
        contributing_reason_codes.append("parse_or_repair_pressure_observed")

    supporting_symptom_count = max(0, len(contributing_reason_codes) - 1)
    if supporting_symptom_count <= 0:
        return {
            "status": "not_indicated",
            "summary": (
                "A structured output contract was present, but the retained evidence "
                "does not show that it materially displaced the most useful content."
            ),
            "confidence": 0.22,
            "reason_codes": [
                "structured_output_contract_present",
                "no_content_pressure_symptom_detected",
            ],
            "selected_model": selected_model,
            "selected_provider": selected_provider,
            "observed_stage_id": "selector_decision",
            "raw_response_format": raw_response_format,
            "structured_output_signal": True,
            "grounded_tool_path_available": grounded_tool_path_available,
            "grounded_tool_path_unused": grounded_tool_path_unused,
            "tool_invocation_count": tool_invocation_count,
            "search_evidence_count": search_evidence_count,
        }

    confidence = min(0.85, 0.38 + (supporting_symptom_count * 0.11))
    return {
        "status": "suspected",
        "summary": (
            "Structured-output pressure on the selected model may have contributed "
            "to a content-poor episode outcome."
        ),
        "confidence": round(confidence, 2),
        "reason_codes": contributing_reason_codes[:8],
        "selected_model": selected_model,
        "selected_provider": selected_provider,
        "observed_stage_id": "selector_decision",
        "raw_response_format": raw_response_format,
        "structured_output_signal": True,
        "grounded_tool_path_available": grounded_tool_path_available,
        "grounded_tool_path_unused": grounded_tool_path_unused,
        "tool_invocation_count": tool_invocation_count,
        "search_evidence_count": search_evidence_count,
    }


def build_episode_critic_evidence_bundle(
    *,
    request_id: str | None = None,
    instance_id: str | None = None,
    namespace: str | None = None,
    neighbour_turn_count: int = _DEFAULT_NEIGHBOUR_MESSAGE_COUNT,
) -> dict[str, Any]:
    request_id_value = _safe_str(request_id)
    instance_id_value = _safe_str(instance_id)
    namespace_value = coerce_namespace(namespace) or _safe_str(namespace)
    neighbour_count = _coerce_positive_int(
        neighbour_turn_count,
        default=_DEFAULT_NEIGHBOUR_MESSAGE_COUNT,
        minimum=0,
        maximum=_MAX_NEIGHBOUR_MESSAGE_COUNT,
    )

    if not request_id_value and not instance_id_value:
        return {
            "success": False,
            "error": "missing_parameter",
            "error_code": "missing_parameter",
            "details": {"missing_any_of": ["request_id", "instance_id"]},
        }

    manager = WorkflowInstanceManager()
    instance = None
    if instance_id_value:
        instance = manager.get_instance(instance_id_value)
        if instance is None:
            return {
                "success": False,
                "error": f"Workflow instance not found: {instance_id_value}",
                "error_code": "not_found",
            }

    resolved_request_id = request_id_value or _extract_request_id_from_instance(instance)
    turn_record = _load_turn_execution_record(
        request_id=resolved_request_id,
        namespace=namespace_value,
    )
    turn_record_source = "mongo.turn_execution_records" if turn_record else None

    resolved_namespace = (
        _safe_str(turn_record.get("namespace")) if isinstance(turn_record, Mapping) else None
    ) or (
        _safe_str(getattr(instance, "namespace", None)) if instance is not None else None
    ) or namespace_value

    history_context = _resolve_history_context(
        request_id=resolved_request_id,
        user_id=(
            _safe_str(turn_record.get("user_id"))
            if isinstance(turn_record, Mapping)
            else _safe_str(getattr(instance, "user_id", None))
        ),
        session_id=(
            _safe_str(turn_record.get("session_id")) if isinstance(turn_record, Mapping) else None
        )
        or (
            _safe_str(getattr(instance, "inputs", {}).get("conversation_session_id"))
            if instance is not None and isinstance(getattr(instance, "inputs", None), Mapping)
            else None
        ),
        namespace=resolved_namespace,
    )

    if turn_record is None and resolved_request_id and isinstance(history_context, Mapping):
        turn_record, turn_record_source = _reconstruct_turn_execution_record_from_history(
            request_id=resolved_request_id,
            history_context=history_context,
        )

    if isinstance(turn_record, Mapping):
        resolved_request_id = _safe_str(turn_record.get("request_id")) or resolved_request_id
        resolved_namespace = _safe_str(turn_record.get("namespace")) or resolved_namespace

    if namespace_value and resolved_namespace and namespace_value != resolved_namespace:
        return {
            "success": False,
            "error": (
                f"Resolved episode namespace {resolved_namespace} does not match requested "
                f"namespace {namespace_value}."
            ),
            "error_code": "namespace_mismatch",
            "details": {
                "requested_namespace": namespace_value,
                "resolved_namespace": resolved_namespace,
            },
        }

    if instance is None and resolved_request_id:
        session_id = (
            _safe_str(turn_record.get("session_id")) if isinstance(turn_record, Mapping) else None
        )
        instance_candidates = manager.list_instances(
            namespace=resolved_namespace,
            conversation_session_id=session_id,
            request_id=resolved_request_id,
            limit=5,
        )
        if instance_candidates:
            instance = instance_candidates[0]
            instance_id_value = _safe_str(getattr(instance, "instance_id", None))

    workflow_id = (
        _safe_str(turn_record.get("workflow_selection", {}).get("selected_workflow_id"))
        if isinstance(turn_record, Mapping)
        and isinstance(turn_record.get("workflow_selection"), Mapping)
        else None
    ) or (
        _safe_str(getattr(instance, "workflow_id", None)) if instance is not None else None
    )

    episode = get_latest_workflow_use_episode(
        workflow_id=workflow_id,
        namespace=resolved_namespace,
        session_id=(
            _safe_str(turn_record.get("session_id")) if isinstance(turn_record, Mapping) else None
        ),
        turn_id=resolved_request_id,
    )

    trace_doc = None
    if instance is not None:
        execution_trace_id = _safe_str(getattr(instance, "execution_trace_id", None))
        if execution_trace_id:
            trace_doc = get_workflow_execution_trace(execution_trace_id)

    workflow_identity, workflow_identity_source = _resolve_workflow_definition_identity(
        workflow_id=workflow_id,
        turn_record=turn_record,
        instance=instance,
        episode=episode,
        trace_doc=trace_doc,
    )
    episode_subject_kind = (
        "turn_execution_request"
        if isinstance(turn_record, Mapping)
        else "workflow_terminal_instance"
        if instance is not None
        else "workflow_use_episode"
    )

    llm_debug = (
        history_context.get("target_llm_debug") if isinstance(history_context, Mapping) else None
    )
    selected_llm_debug = _build_selected_llm_debug(
        llm_debug if isinstance(llm_debug, Mapping) else None
    )
    aux_llm_calls = (
        llm_debug.get("aux_llm_calls")
        if isinstance(llm_debug, Mapping) and isinstance(llm_debug.get("aux_llm_calls"), list)
        else []
    )
    tool_ledger = _build_tool_ledger(
        turn_record=turn_record if isinstance(turn_record, Mapping) else None,
        llm_debug=llm_debug if isinstance(llm_debug, Mapping) else None,
    )
    neighbouring_context = _build_neighbouring_context(
        history_context=history_context,
        neighbour_message_count=neighbour_count,
    )
    expected_context = _build_expected_context(
        turn_record=turn_record if isinstance(turn_record, Mapping) else None,
        llm_debug=llm_debug if isinstance(llm_debug, Mapping) else None,
        workflow_identity=workflow_identity,
        instance=instance,
    )
    workflow_instance_section = _build_workflow_instance_section(instance)
    workflow_trace_section = (
        {
            "summary": build_workflow_execution_trace_summary(trace_doc),
            "trace": _mapping_or_empty(trace_doc),
        }
        if isinstance(trace_doc, Mapping)
        else None
    )

    observed_evidence_raw: dict[str, Any] = {
        "turn_execution_record": _normalise_mapping(turn_record),
        "selected_llm_debug": selected_llm_debug,
        "aux_llm_calls": aux_llm_calls or None,
        "tool_ledger": tool_ledger,
        "workflow_instance": workflow_instance_section,
        "workflow_episode": _normalise_mapping(episode),
        "workflow_trace": workflow_trace_section,
        "neighbouring_context": neighbouring_context,
        "workflow_definition_identity": _normalise_mapping(workflow_identity),
    }

    receipts: dict[str, Any] = {}
    observed_evidence: dict[str, Any] = {}
    source_systems: list[str] = []
    required_sections = {
        "workflow_definition_identity",
        "expected_context",
    }
    if episode_subject_kind == "turn_execution_request":
        required_sections.update(
            {
                "turn_execution_record",
                "selected_llm_debug",
                "neighbouring_context",
            }
        )
    else:
        required_sections.update(
            {
                "workflow_instance",
            }
        )
    for section_id, value in observed_evidence_raw.items():
        source_system = {
            "turn_execution_record": "mongo.turn_execution_records",
            "selected_llm_debug": "mongo.chat_history",
            "aux_llm_calls": "mongo.chat_history",
            "tool_ledger": "mongo.turn_execution_records",
            "workflow_instance": "mongo.workflow_instances",
            "workflow_episode": "mongo.workflow_use_episodes",
            "workflow_trace": "mongo.workflow_executions",
            "neighbouring_context": "mongo.chat_history",
            "workflow_definition_identity": "workflow.identity",
        }[section_id]
        bounded_value, receipt = _build_section(
            section_id=section_id,
            value=value,
            source_system=source_system,
            required=section_id in required_sections,
            locator={
                "request_id": resolved_request_id,
                "instance_id": instance_id_value,
                "workflow_id": workflow_id,
            },
        )
        observed_evidence[section_id] = bounded_value
        receipts[section_id] = receipt
        if receipt.get("present") and source_system not in source_systems:
            source_systems.append(source_system)

    bounded_expected_context, expected_receipt = _build_section(
        section_id="expected_context",
        value=expected_context,
        source_system="episode.expected_context",
        required=True,
        locator={
            "request_id": resolved_request_id,
            "instance_id": instance_id_value,
            "workflow_id": workflow_id,
        },
    )
    receipts["expected_context"] = expected_receipt

    capability_gaps: list[dict[str, Any]] = []
    if episode_subject_kind == "turn_execution_request" and not isinstance(
        turn_record, Mapping
    ):
        capability_gaps.append(
            _build_gap(
                "turn_execution_record_missing",
                "No projected or reconstructable turn execution record was found for this episode.",
                required=True,
                section_id="turn_execution_record",
            )
        )
    if (
        episode_subject_kind == "turn_execution_request"
        and resolved_request_id
        and not isinstance(selected_llm_debug, Mapping)
    ):
        capability_gaps.append(
            _build_gap(
                "selected_llm_debug_missing",
                "No target assistant llm_debug_data payload was found in chat history for the request.",
                required=True,
                section_id="selected_llm_debug",
            )
        )
    if (
        episode_subject_kind == "turn_execution_request"
        and resolved_request_id
        and not isinstance(neighbouring_context, Mapping)
    ):
        capability_gaps.append(
            _build_gap(
                "neighbouring_context_missing",
                "No neighbouring conversational context could be resolved for the target request.",
                required=True,
                section_id="neighbouring_context",
            )
        )
    if not isinstance(workflow_identity, Mapping):
        capability_gaps.append(
            _build_gap(
                "workflow_definition_identity_missing",
                "No workflow definition identity was available from the turn record, workflow runtime, episode, trace, or registry.",
                required=True,
                section_id="workflow_definition_identity",
            )
        )
    if not isinstance(expected_context, Mapping):
        capability_gaps.append(
            _build_gap(
                "expected_context_missing",
                "Expected-behaviour context could not be derived for the episode.",
                required=True,
                section_id="expected_context",
            )
        )

    fail_closed_reason_codes = [
        gap["gap_id"] for gap in capability_gaps if gap.get("required") is True
    ]
    fail_closed = bool(fail_closed_reason_codes)
    format_over_content_diagnostic = _build_format_over_content_diagnostic(
        turn_record=turn_record if isinstance(turn_record, Mapping) else None,
        selected_llm_debug=selected_llm_debug,
        expected_context=bounded_expected_context
        if isinstance(bounded_expected_context, Mapping)
        else None,
        tool_ledger=tool_ledger if isinstance(tool_ledger, Mapping) else None,
        fail_closed_reason_codes=fail_closed_reason_codes,
    )

    source_resolution = {
        "turn_execution_record_source": turn_record_source,
        "workflow_definition_identity_source": workflow_identity_source,
        "history_context_found": bool(history_context),
        "workflow_instance_found": instance is not None,
        "workflow_episode_found": isinstance(episode, Mapping),
        "workflow_trace_found": isinstance(trace_doc, Mapping),
        "resolved_request_id": resolved_request_id,
        "resolved_namespace": resolved_namespace,
    }

    episode_locator = {
        "subject_kind": episode_subject_kind,
        "request_id": resolved_request_id,
        "instance_id": instance_id_value,
        "namespace": resolved_namespace,
        "session_id": (
            _safe_str(turn_record.get("session_id")) if isinstance(turn_record, Mapping) else None
        )
        or (
            _safe_str(history_context.get("session_id"))
            if isinstance(history_context, Mapping)
            else None
        ),
        "user_id": (
            _safe_str(turn_record.get("user_id")) if isinstance(turn_record, Mapping) else None
        )
        or (
            _safe_str(history_context.get("user_id"))
            if isinstance(history_context, Mapping)
            else None
        ),
        "workflow_id": workflow_id,
        "execution_trace_id": (
            _safe_str(getattr(instance, "execution_trace_id", None))
            if instance is not None
            else None
        ),
        "episode_id": _safe_str(episode.get("episode_id")) if isinstance(episode, Mapping) else None,
        "resumable": True,
    }

    bundle_receipt_payload = {
        "schema_version": EPISODE_CRITIC_EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "episode_locator": episode_locator,
        "receipt_hashes": {
            section_id: receipt.get("authoritative_sha256")
            for section_id, receipt in receipts.items()
            if isinstance(receipt, Mapping)
        },
    }
    if isinstance(format_over_content_diagnostic, Mapping):
        bundle_receipt_payload["derived_diagnostics"] = {
            "format_over_content_diagnostic_sha256": _hash_payload(
                format_over_content_diagnostic
            )
        }
    bundle_receipt = {
        "sha256": _hash_payload(bundle_receipt_payload),
        "section_count": len(receipts),
    }

    return {
        "success": True,
        "schema_version": EPISODE_CRITIC_EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "ready_for_critic": not fail_closed,
        "fail_closed": fail_closed,
        "fail_closed_reason_codes": fail_closed_reason_codes,
        "episode_locator": episode_locator,
        "source_resolution": source_resolution,
        "provenance": {
            "source_systems": source_systems,
            "namespace": resolved_namespace,
        },
        "expected_context": bounded_expected_context,
        "observed_evidence": observed_evidence,
        "receipts": receipts,
        "bundle_receipt": bundle_receipt,
        "capability_gaps": capability_gaps,
        "format_over_content_diagnostic": format_over_content_diagnostic,
    }


__all__ = [
    "EPISODE_CRITIC_EVIDENCE_BUNDLE_SCHEMA_VERSION",
    "build_episode_critic_evidence_bundle",
]
