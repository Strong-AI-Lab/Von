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
    python_decision_count = 0
    possibly_inappropriate_count = 0
    decision_classes: list[str] = []
    decision_sources: list[str] = []
    seen_classes: set[str] = set()
    seen_sources: set[str] = set()

    for entry in aux_entries or ():
        if not isinstance(entry, Mapping):
            continue
        entry_stage = normalise_stage_name(
            entry.get("stage") or entry.get("guardrail_stage")
        )
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
