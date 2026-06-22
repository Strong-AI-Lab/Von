"""Projection helpers for represented turn context-adjudication telemetry."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping, Sequence

TURN_CONTEXT_ADJUDICATION_PROJECTION_SCHEMA_VERSION = (
    "turn_context_adjudication_projection.v1"
)

_HANDOFF_CONTEXT_KEYS = (
    "turn_context_handoff_decision",
    "turn_context_handoff_mode",
    "turn_context_handoff_summary",
    "turn_context_handoff_messages",
    "turn_context_handoff_lineage",
    "turn_context_handoff_omitted_context_reasons",
    "turn_context_handoff_risks",
    "turn_context_handoff_routing_evidence_scope",
    "turn_context_handoff_expected_outcome_scope",
    "turn_context_handoff_answer_scope",
)

_DECISION_FIELD_BY_CONTEXT_KEY = {
    "turn_context_handoff_mode": "mode",
    "turn_context_handoff_summary": "summary",
    "turn_context_handoff_messages": "turn_context_handoff_messages",
    "turn_context_handoff_lineage": "lineage",
    "turn_context_handoff_omitted_context_reasons": "omitted_context_reasons",
    "turn_context_handoff_risks": "risks",
    "turn_context_handoff_routing_evidence_scope": "routing_evidence_scope",
    "turn_context_handoff_expected_outcome_scope": "expected_outcome_scope",
    "turn_context_handoff_answer_scope": "answer_scope",
}

_CONTAINER_KEYS = (
    "context_adjudication",
    "turn_context_adjudication",
    "turn_context_handoff",
    "turn_execution_diagnostics",
    "selected_workflow_trace",
    "completion_report",
    "execution",
    "summary",
    "workflow_context",
    "context",
    "data",
    "result",
    "outputs",
    "llm_step_envelope",
    "prompt_contract",
    "prompt_resolution",
    "workflow_model_policy",
    "metadata",
    "background_task_status",
)

_CONTAINER_LIST_KEYS = (
    "stage_diagnostics",
    "progress_history",
    "progress_events",
    "llm_calls",
    "aux_llm_calls",
    "events",
    "trace_events",
)

_PROMPT_ID_KEYS = (
    "context_adjudication_prompt_id",
    "selected_prompt_id",
    "resolved_prompt_concept_id",
    "base_prompt_id",
)

_MODEL_KEYS = (
    "context_adjudication_model",
    "selected_model",
    "model_name",
    "model",
)

_MAX_CONTAINER_SEQUENCE_ITEMS = 200


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _safe_sequence(value: Any) -> list[Any] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    return list(value)


def _copy_jsonish(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_jsonish(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_copy_jsonish(item) for item in value]
    return deepcopy(value)


def _iter_candidate_mappings(value: Any, *, depth: int = 0) -> Sequence[Mapping[str, Any]]:
    mapping = _safe_mapping(value)
    if mapping is None or depth > 5:
        return ()

    candidates: list[Mapping[str, Any]] = [mapping]
    for key in _CONTAINER_KEYS:
        child = mapping.get(key)
        child_mapping = _safe_mapping(child)
        if child_mapping is not None:
            candidates.extend(_iter_candidate_mappings(child_mapping, depth=depth + 1))
            continue
        child_sequence = _safe_sequence(child)
        if child_sequence is None:
            continue
        for item in child_sequence[:_MAX_CONTAINER_SEQUENCE_ITEMS]:
            if isinstance(item, Mapping):
                candidates.extend(_iter_candidate_mappings(item, depth=depth + 1))

    for key in _CONTAINER_LIST_KEYS:
        child_sequence = _safe_sequence(mapping.get(key))
        if child_sequence is None:
            continue
        for item in child_sequence[:_MAX_CONTAINER_SEQUENCE_ITEMS]:
            if isinstance(item, Mapping):
                candidates.extend(_iter_candidate_mappings(item, depth=depth + 1))

    return candidates


def _looks_like_context_adjudication_stage(mapping: Mapping[str, Any]) -> bool:
    for key in ("stage", "workflow_stage_id", "workflow_state_id", "state_id"):
        value = _safe_str(mapping.get(key))
        if value and "context_adjudication" in value:
            return True
    return False


def _json_object_from_llm_response_preview(
    mapping: Mapping[str, Any],
) -> dict[str, Any] | None:
    preview = _safe_mapping(mapping.get("llm_response_preview"))
    if preview is None:
        return None

    text = _safe_str(preview.get("text"))
    if text is None:
        text = _safe_str(preview.get("preview"))
    if text is None:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    return {str(key): _copy_jsonish(value) for key, value in parsed.items()}


def _candidate_with_preview_adjudication_decision(
    candidate: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not _looks_like_context_adjudication_stage(candidate):
        return None

    decision = _json_object_from_llm_response_preview(candidate)
    if decision is None:
        return None

    projected = {
        str(key): _copy_jsonish(value)
        for key, value in candidate.items()
        if isinstance(key, str)
    }
    projected["validated_json"] = decision
    projected["turn_context_handoff_decision"] = decision
    projected["projection_source_detail"] = "llm_response_preview.text"
    projected["validated_output_recorded"] = False
    for context_key, decision_key in _DECISION_FIELD_BY_CONTEXT_KEY.items():
        value = decision.get(decision_key)
        if value is not None:
            projected[context_key] = _copy_jsonish(value)
    return projected


def _find_handoff_candidate(
    sources: Sequence[tuple[str, Mapping[str, Any] | None]],
) -> tuple[str, Mapping[str, Any]] | None:
    for source_name, source in sources:
        if not isinstance(source, Mapping):
            continue
        for candidate in _iter_candidate_mappings(source):
            if any(key in candidate for key in _HANDOFF_CONTEXT_KEYS):
                return source_name, candidate
            if (
                candidate.get("schema_version")
                == TURN_CONTEXT_ADJUDICATION_PROJECTION_SCHEMA_VERSION
            ):
                return source_name, candidate
            if _looks_like_context_adjudication_stage(candidate) and isinstance(
                candidate.get("validated_json"), Mapping
            ):
                return source_name, candidate
            preview_candidate = _candidate_with_preview_adjudication_decision(
                candidate
            )
            if preview_candidate is not None:
                return source_name, preview_candidate
    return None


def _first_string_from_candidates(
    candidates: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> str | None:
    for candidate in candidates:
        for key in keys:
            value = _safe_str(candidate.get(key))
            if value:
                return value
    return None


def _first_mapping_from_candidates(
    candidates: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> dict[str, Any] | None:
    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, Mapping):
                return {
                    str(item_key): _copy_jsonish(item)
                    for item_key, item in value.items()
                }
    return None


def _projection_from_existing(candidate: Mapping[str, Any], source_name: str) -> dict[str, Any]:
    projection = {
        str(key): _copy_jsonish(value)
        for key, value in candidate.items()
        if isinstance(key, str)
    }
    projection.setdefault(
        "schema_version", TURN_CONTEXT_ADJUDICATION_PROJECTION_SCHEMA_VERSION
    )
    projection.setdefault("available", True)
    projection.setdefault("source", source_name)
    return projection


def build_turn_context_adjudication_projection(
    sources: Sequence[tuple[str, Mapping[str, Any] | None]],
) -> dict[str, Any] | None:
    """Project recorded context-adjudication outputs without re-deciding them."""

    found = _find_handoff_candidate(sources)
    if found is None:
        return None

    source_name, candidate = found
    if (
        candidate.get("schema_version")
        == TURN_CONTEXT_ADJUDICATION_PROJECTION_SCHEMA_VERSION
    ):
        return _projection_from_existing(candidate, source_name)

    candidates = list(_iter_candidate_mappings(candidate))
    raw_decision = candidate.get("turn_context_handoff_decision")
    if not isinstance(raw_decision, Mapping):
        raw_decision = candidate.get("validated_json")
    decision = (
        {str(key): _copy_jsonish(value) for key, value in raw_decision.items()}
        if isinstance(raw_decision, Mapping)
        else None
    )

    decision_source = decision if isinstance(decision, Mapping) else {}
    projection: dict[str, Any] = {
        "schema_version": TURN_CONTEXT_ADJUDICATION_PROJECTION_SCHEMA_VERSION,
        "available": True,
        "source": source_name,
        "field_count": sum(1 for key in _HANDOFF_CONTEXT_KEYS if key in candidate),
    }

    if decision is not None:
        projection["decision"] = decision
    elif _safe_str(candidate.get("turn_context_handoff_decision")):
        projection["decision_text"] = _safe_str(
            candidate.get("turn_context_handoff_decision")
        )

    for context_key, decision_key in _DECISION_FIELD_BY_CONTEXT_KEY.items():
        value = candidate.get(context_key)
        if value is None and isinstance(decision_source, Mapping):
            value = decision_source.get(decision_key)
        if value is not None:
            projection[decision_key] = _copy_jsonish(value)

    source_detail = _safe_str(candidate.get("projection_source_detail"))
    if source_detail:
        projection["source_detail"] = source_detail
    if "validated_output_recorded" in candidate:
        projection["validated_output_recorded"] = bool(
            candidate.get("validated_output_recorded")
        )

    prompt_id = _first_string_from_candidates(candidates, _PROMPT_ID_KEYS)
    if prompt_id:
        projection["prompt_id"] = prompt_id
    model_name = _first_string_from_candidates(candidates, _MODEL_KEYS)
    if model_name:
        projection["model"] = model_name
    model_policy = _first_mapping_from_candidates(
        candidates,
        ("llm_policy", "workflow_model_policy", "selected_model_candidate"),
    )
    if model_policy is not None:
        projection["model_policy"] = model_policy

    return projection
