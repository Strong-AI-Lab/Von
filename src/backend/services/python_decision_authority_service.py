"""Shared helpers for Python-side decision authority telemetry.

These helpers keep two related concerns centralised:

1. Standard annotation for code-side decisions that may influence routing,
   tool availability, completion, or response shaping.
2. Stage-level summaries of whether recorded LLM input/output exists and how
   much Python-side decision authority was exercised in that stage.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

_PROMPT_SEMANTIC_DECISION_SOURCES = frozenset(
    {
        "prompt_semantic_inference",
        "prompt_shape_heuristic",
        "lexical_heuristic",
        "response_semantic_inference",
        "semantic_veto",
        "semantic_override",
    }
)
_STAGELESS_LLM_ENTRY_STAGE_MAP = {
    "workflow_selector_prompt": "selector_preparation",
    "workflow_selector": "selector_decision",
    "workflow_selector_override": "workflow_dispatch",
}
_STAGE_LLM_EXCHANGE_SUMMARY_LIMIT = 5
_STAGE_LLM_TEXT_PREVIEW_CHAR_LIMIT = 240


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def normalise_stage_name(value: Any) -> str | None:
    text = _safe_str(value)
    if text is None:
        return None
    return text.strip().lower()


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _normalise_entry_type(value: Any) -> str | None:
    text = _safe_str(value)
    if text is None:
        return None
    return text.lower()


def _resolve_entry_stage_name(entry: Mapping[str, Any]) -> str | None:
    explicit_stage = normalise_stage_name(
        entry.get("stage") or entry.get("guardrail_stage")
    )
    if explicit_stage:
        return explicit_stage
    entry_type = _normalise_entry_type(entry.get("type"))
    if entry_type is None:
        return None
    return _STAGELESS_LLM_ENTRY_STAGE_MAP.get(entry_type)


def _build_text_preview(value: Any) -> dict[str, Any] | None:
    text = None
    char_count = None
    if isinstance(value, Mapping):
        text = value.get("text")
        if not isinstance(text, str):
            content = value.get("content")
            if isinstance(content, Mapping):
                text = content.get("text")
            elif isinstance(content, str):
                text = content
        char_count = _safe_int(value.get("char_count"))
    elif isinstance(value, str):
        text = value
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    total_chars = char_count if isinstance(char_count, int) and char_count > 0 else len(text)
    preview = stripped[:_STAGE_LLM_TEXT_PREVIEW_CHAR_LIMIT]
    return {
        "text": preview,
        "char_count": total_chars,
        "truncated": total_chars > _STAGE_LLM_TEXT_PREVIEW_CHAR_LIMIT,
    }


def _extract_prompt_preview(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    request = entry.get("request")
    if isinstance(request, Mapping):
        preview = _build_text_preview(request.get("prompt"))
        if preview is not None:
            return preview
    preview = _build_text_preview(entry.get("prompt"))
    if preview is not None:
        return preview
    return _build_text_preview(entry.get("prompt_preview"))


def _extract_response_preview(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    preview = _build_text_preview(entry.get("response"))
    if preview is not None:
        return preview
    preview = _build_text_preview(entry.get("response_preview"))
    if preview is not None:
        return preview
    fallback_attempts = entry.get("fallback_attempts")
    if not isinstance(fallback_attempts, list):
        return None
    for attempt in reversed(fallback_attempts):
        if not isinstance(attempt, Mapping):
            continue
        preview = _build_text_preview(attempt.get("response"))
        if preview is not None:
            return preview
    return None


def _collect_unique_strings(*values: Any) -> list[str]:
    collected: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, list):
            iterable = value
        else:
            iterable = (value,)
        for item in iterable:
            text = _safe_str(item)
            if text is None or text in seen:
                continue
            seen.add(text)
            collected.append(text)
    return collected


def _extract_failure_kinds(entry: Mapping[str, Any]) -> list[str]:
    failure_kinds: list[str] = []
    errors = entry.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, Mapping):
                failure_kinds.extend(
                    _collect_unique_strings(error.get("failure_kind"))
                )
    fallback_attempts = entry.get("fallback_attempts")
    if isinstance(fallback_attempts, list):
        for attempt in fallback_attempts:
            if isinstance(attempt, Mapping):
                failure_kinds.extend(
                    _collect_unique_strings(attempt.get("failure_kind"))
                )
    return _collect_unique_strings(failure_kinds)


def _build_llm_exchange_summary(
    entry: Mapping[str, Any],
    *,
    entry_has_input: bool,
    entry_has_output: bool,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "entry_type": _normalise_entry_type(entry.get("type")) or "unknown",
        "stage": _resolve_entry_stage_name(entry),
        "llm_input_recorded": bool(entry_has_input),
        "llm_output_recorded": bool(entry_has_output),
    }

    request = entry.get("request")
    request_mapping = request if isinstance(request, Mapping) else {}
    selected = entry.get("selected")
    selected_mapping = selected if isinstance(selected, Mapping) else {}
    selection_metadata = entry.get("selection_metadata")
    selection_metadata_mapping = (
        selection_metadata if isinstance(selection_metadata, Mapping) else {}
    )

    optional_values: dict[str, Any] = {
        "policy_stage": _safe_str(entry.get("policy_stage")),
        "prompt_id": _safe_str(entry.get("prompt_id")),
        "workflow_id": _safe_str(entry.get("workflow_id")),
        "verdict": _safe_str(entry.get("verdict")),
        "selection_source": _safe_str(entry.get("selection_source")),
        "selection_mode": _safe_str(entry.get("selection_mode")),
        "explicit_stage_model_override_origin": _safe_str(
            entry.get("explicit_stage_model_override_origin")
        ),
        "selection_resolution": _safe_str(
            selection_metadata_mapping.get("selection_resolution")
        ),
        "requested_provider": _safe_str(entry.get("requested_provider")),
        "requested_model": _safe_str(entry.get("requested_model")),
        "selected_provider": _safe_str(selected_mapping.get("provider")),
        "selected_model": _safe_str(
            selected_mapping.get("model_resolved") or selected_mapping.get("model")
        )
        or _safe_str(entry.get("model_name")),
        "context_message_count": _safe_int(request_mapping.get("context_message_count")),
        "tool_definition_count": _safe_int(request_mapping.get("tool_definition_count")),
        "fallback_attempt_count": _safe_int(entry.get("fallback_attempt_count")),
        "failure_count": _safe_int(entry.get("failure_count")),
    }

    for key, value in optional_values.items():
        if value is not None:
            summary[key] = value

    for key, value in {
        "prompt_preview": _extract_prompt_preview(entry),
        "candidate_list_preview": _build_text_preview(entry.get("candidate_list")),
        "continuation_context_preview": _build_text_preview(
            entry.get("continuation_context")
        ),
        "response_preview": _extract_response_preview(entry),
    }.items():
        if value is not None:
            summary[key] = value

    requested_prompt_ids = _collect_unique_strings(entry.get("requested_prompt_ids"))
    if requested_prompt_ids:
        summary["requested_prompt_ids"] = requested_prompt_ids

    raw_context_lineage = entry.get("context_lineage")
    if isinstance(raw_context_lineage, Mapping):
        context_lineage = {
            str(key): value
            for key, value in raw_context_lineage.items()
            if isinstance(key, str)
        }
        if context_lineage:
            summary["context_lineage"] = context_lineage

    candidate_entries = entry.get("candidate_entries")
    candidate_entry_count = _safe_int(
        entry.get("candidate_entry_count")
        if "candidate_entry_count" in entry
        else (len(candidate_entries) if isinstance(candidate_entries, list) else None)
    )
    if candidate_entry_count is not None:
        summary["candidate_entry_count"] = candidate_entry_count

    discovery_candidate_count = _safe_int(entry.get("discovery_candidate_count"))
    if discovery_candidate_count is not None:
        summary["discovery_candidate_count"] = discovery_candidate_count

    if "fallback_used" in entry:
        summary["fallback_used"] = bool(entry.get("fallback_used"))
    if "follows_active_llm" in entry:
        summary["follows_active_llm"] = bool(entry.get("follows_active_llm"))
    if "explicit_stage_model_override" in entry:
        summary["explicit_stage_model_override"] = bool(
            entry.get("explicit_stage_model_override")
        )

    failure_kinds = _extract_failure_kinds(entry)
    if failure_kinds:
        summary["failure_kinds"] = failure_kinds

    return summary


def annotate_python_decision_event(
    payload: Mapping[str, Any] | None,
    *,
    stage: str | None,
    component: str,
    function: str,
    decision_class: str,
    decision_source: str,
    changed_outcome: bool,
    reason_code: str | None = None,
    possible_inappropriate_python_code_use: bool | None = None,
) -> dict[str, Any]:
    """Attach a standard Python-decision-telemetry envelope to a payload."""

    event = dict(payload or {})
    event_stage = normalise_stage_name(stage) or normalise_stage_name(
        event.get("stage") or event.get("guardrail_stage")
    )
    if event_stage:
        event["stage"] = event_stage
    event["decision_authority_origin"] = "python"
    event["component"] = str(component or "").strip() or "unknown"
    event["function"] = str(function or "").strip() or "unknown"
    event["decision_class"] = str(decision_class or "").strip() or "unknown"
    event["decision_source"] = str(decision_source or "").strip() or "unknown"
    event["changed_outcome"] = bool(changed_outcome)
    if isinstance(reason_code, str) and reason_code.strip():
        event["reason_code"] = reason_code.strip()
    if possible_inappropriate_python_code_use is None:
        possible_inappropriate_python_code_use = (
            event["decision_source"] in _PROMPT_SEMANTIC_DECISION_SOURCES
        )
    event["possible_inappropriate_python_code_use"] = bool(
        possible_inappropriate_python_code_use
    )
    return event


def _entry_records_llm_input(entry: Mapping[str, Any]) -> bool:
    request = entry.get("request")
    if isinstance(request, Mapping):
        prompt = request.get("prompt")
        if isinstance(prompt, Mapping) and _safe_str(prompt.get("text")):
            return True
        if _safe_str(prompt):
            return True
        context_messages = request.get("context_messages")
        if isinstance(context_messages, list) and context_messages:
            return True
    prompt = entry.get("prompt")
    if isinstance(prompt, Mapping) and _safe_str(prompt.get("text")):
        return True
    if _safe_str(prompt):
        return True
    prompt_preview = entry.get("prompt_preview")
    if isinstance(prompt_preview, str) and prompt_preview.strip():
        return True
    return False


def _attempt_records_llm_output(attempt: Mapping[str, Any]) -> bool:
    if isinstance(attempt.get("response"), Mapping) and _safe_str(
        attempt.get("response", {}).get("text")
    ):
        return True
    if _safe_str(attempt.get("response")):
        return True
    if bool(attempt.get("raw_response_present")):
        return True
    return False


def _entry_records_llm_output(entry: Mapping[str, Any]) -> bool:
    response = entry.get("response")
    if isinstance(response, Mapping) and _safe_str(response.get("text")):
        return True
    if _safe_str(response):
        return True
    response_preview = entry.get("response_preview")
    if isinstance(response_preview, str) and response_preview.strip():
        return True
    fallback_attempts = entry.get("fallback_attempts")
    if isinstance(fallback_attempts, list):
        for attempt in fallback_attempts:
            if isinstance(attempt, Mapping) and _attempt_records_llm_output(attempt):
                return True
    return False


def build_stage_authority_summary(
    *,
    stage_id: str | None,
    aux_entries: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Summarise recorded LLM exchange and Python decision use for one stage."""

    stage_key = normalise_stage_name(stage_id)
    llm_input_recorded = False
    llm_output_recorded = False
    llm_exchange_record_count = 0
    llm_exchange_entry_types: list[str] = []
    llm_exchange_summaries: list[dict[str, Any]] = []
    llm_exchange_summary_truncated_count = 0
    latest_llm_exchange: dict[str, Any] | None = None
    python_decision_count = 0
    possibly_inappropriate_count = 0
    decision_classes: list[str] = []
    decision_sources: list[str] = []
    seen_classes: set[str] = set()
    seen_sources: set[str] = set()
    seen_llm_entry_types: set[str] = set()

    for entry in aux_entries or ():
        if not isinstance(entry, Mapping):
            continue
        entry_stage = _resolve_entry_stage_name(entry)
        if stage_key and entry_stage != stage_key:
            continue

        entry_has_input = _entry_records_llm_input(entry)
        entry_has_output = _entry_records_llm_output(entry)
        if entry_has_input:
            llm_input_recorded = True
        if entry_has_output:
            llm_output_recorded = True
        if entry_has_input or entry_has_output:
            llm_exchange_record_count += 1
            entry_type = _normalise_entry_type(entry.get("type"))
            if entry_type and entry_type not in seen_llm_entry_types:
                seen_llm_entry_types.add(entry_type)
                llm_exchange_entry_types.append(entry_type)
            latest_llm_exchange = _build_llm_exchange_summary(
                entry,
                entry_has_input=entry_has_input,
                entry_has_output=entry_has_output,
            )
            if len(llm_exchange_summaries) < _STAGE_LLM_EXCHANGE_SUMMARY_LIMIT:
                llm_exchange_summaries.append(latest_llm_exchange)
            else:
                llm_exchange_summary_truncated_count += 1

        if _safe_str(entry.get("decision_authority_origin")) != "python":
            continue

        python_decision_count += 1
        if bool(entry.get("possible_inappropriate_python_code_use")):
            possibly_inappropriate_count += 1
        decision_class = _safe_str(entry.get("decision_class"))
        if decision_class and decision_class not in seen_classes:
            seen_classes.add(decision_class)
            decision_classes.append(decision_class)
        decision_source = _safe_str(entry.get("decision_source"))
        if decision_source and decision_source not in seen_sources:
            seen_sources.add(decision_source)
            decision_sources.append(decision_source)

    return {
        "llm_input_recorded": llm_input_recorded,
        "llm_output_recorded": llm_output_recorded,
        "has_recorded_llm_exchange": llm_input_recorded and llm_output_recorded,
        "llm_exchange_record_count": llm_exchange_record_count,
        "llm_exchange_entry_types": llm_exchange_entry_types,
        "llm_exchange_summaries": llm_exchange_summaries,
        "llm_exchange_summary_truncated_count": llm_exchange_summary_truncated_count,
        "latest_llm_exchange": latest_llm_exchange,
        "missing_recorded_llm_input": not llm_input_recorded,
        "missing_recorded_llm_output": not llm_output_recorded,
        "python_decision_count": python_decision_count,
        "possibly_inappropriate_python_code_use_count": possibly_inappropriate_count,
        "python_decision_classes": decision_classes,
        "python_decision_sources": decision_sources,
    }


__all__ = [
    "annotate_python_decision_event",
    "build_stage_authority_summary",
    "normalise_stage_name",
]
