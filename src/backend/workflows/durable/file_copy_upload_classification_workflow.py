"""Durable file-copy upload classification subworkflow (JVNAUTOSCI-1309).

This workflow classifies newly uploaded file copies into a routing mode:
- specialised subworkflow route (when available and high confidence),
- baseline interpretation route,
- explicit fail-closed route for low-confidence mutation paths, or
- no-op route when interpretation fallback is disabled.

The classification decision is persisted as a singleton text relation so route
selection remains inspectable after execution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from typing import Any, Mapping

from ...services.file_copy_typing_service import infer_file_copy_typing
from ...services.text_value_service import upsert_singleton_text_relation
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..workflow_registry import WorkflowRegistration

FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID = (
    "#V#file_copy_upload_classification_workflow"
)

FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE = (
    "#V#has_file_copy_upload_route_decision_json"
)
FILE_COPY_UPLOAD_CLASSIFICATION_VERSION = "file_copy_upload_classification.v1"

DEFAULT_MINIMUM_ROUTE_SCORE = 0.58
DEFAULT_MINIMUM_MUTATION_CONFIDENCE = 0.84
DEFAULT_ALLOW_INTERPRET_FALLBACK = True
DEFAULT_FORCE_ROUTE_KEY_SENTINEL = "__auto__"
DEFAULT_UNUSED_SPECIALISED_WORKFLOW_ID = "__unused__"

DEFAULT_SCHOLARLY_WORKFLOW_ID = "#V#scholarly_paper_representation_workflow"
DEFAULT_CV_WORKFLOW_ID = "#V#file_copy_cv_representation_workflow"
DEFAULT_BUSINESS_CARD_WORKFLOW_ID = "#V#file_copy_business_card_representation_workflow"

ROUTE_KEY_SCHOLARLY = "scholarly"
ROUTE_KEY_CV = "cv"
ROUTE_KEY_BUSINESS_CARD = "business_card"
ROUTE_KEY_MEETING = "meeting"
ROUTE_KEY_INTERPRET = "interpret"
ROUTE_KEY_NOOP = "noop"

ROUTE_MODE_SPECIALISED = "specialised"
ROUTE_MODE_INTERPRET = "interpret"
ROUTE_MODE_FAIL_CLOSED = "fail_closed"
ROUTE_MODE_NOOP = "noop"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalise_filename(value: Any) -> str:
    return _clean_text(value).lower()


def _normalise_content_type(value: Any) -> str:
    raw = _clean_text(value).lower()
    if not raw:
        return ""
    token = raw.split(";", 1)[0].strip()
    return token


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed


def _extract_route_from_force_override(value: Any) -> str | None:
    token = _clean_text(value).lower()
    if token in {
        ROUTE_KEY_SCHOLARLY,
        ROUTE_KEY_CV,
        ROUTE_KEY_BUSINESS_CARD,
        ROUTE_KEY_MEETING,
        ROUTE_KEY_INTERPRET,
        ROUTE_KEY_NOOP,
    }:
        return token
    return None


def _candidate_list_from_any(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


@lru_cache(maxsize=1)
def _discover_known_workflow_ids() -> tuple[str, ...]:
    """Resolve currently registered workflow IDs for availability checks."""

    try:
        from .registry_factory import build_workflow_registry_read_only

        registry = build_workflow_registry_read_only()
        ids = sorted(
            {
                str(workflow_id).strip()
                for workflow_id in registry.all_workflow_ids()
                if isinstance(workflow_id, str) and str(workflow_id).strip()
            }
        )
        return tuple(ids)
    except Exception:
        return ()


def _resolve_available_workflow_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = _candidate_list_from_any(payload.get("available_workflow_ids"))
    if explicit:
        return tuple(explicit)
    return _discover_known_workflow_ids()


def _resolve_specialised_candidates(
    *,
    route_key: str,
    payload: Mapping[str, Any],
) -> list[str]:
    nested = payload.get("specialised_workflow_ids")
    nested_map = nested if isinstance(nested, Mapping) else {}
    candidates: list[str] = []

    if route_key == ROUTE_KEY_SCHOLARLY:
        candidates.extend(
            _candidate_list_from_any(payload.get("scholarly_workflow_id"))
        )
        candidates.extend(
            _candidate_list_from_any(nested_map.get("scholarly"))
        )
        env_value = _clean_text(os.getenv("VON_FILE_COPY_SCHOLARLY_WORKFLOW_ID"))
        if env_value:
            candidates.append(env_value)
        candidates.append(DEFAULT_SCHOLARLY_WORKFLOW_ID)
    elif route_key == ROUTE_KEY_CV:
        candidates.extend(_candidate_list_from_any(payload.get("cv_workflow_id")))
        candidates.extend(_candidate_list_from_any(nested_map.get("cv")))
        env_value = _clean_text(os.getenv("VON_FILE_COPY_CV_WORKFLOW_ID"))
        if env_value:
            candidates.append(env_value)
        candidates.append(DEFAULT_CV_WORKFLOW_ID)
    elif route_key == ROUTE_KEY_BUSINESS_CARD:
        candidates.extend(
            _candidate_list_from_any(payload.get("business_card_workflow_id"))
        )
        candidates.extend(
            _candidate_list_from_any(nested_map.get("business_card"))
        )
        env_value = _clean_text(os.getenv("VON_FILE_COPY_BUSINESS_CARD_WORKFLOW_ID"))
        if env_value:
            candidates.append(env_value)
        candidates.append(DEFAULT_BUSINESS_CARD_WORKFLOW_ID)
    elif route_key == ROUTE_KEY_MEETING:
        candidates.extend(_candidate_list_from_any(payload.get("meeting_workflow_id")))
        candidates.extend(_candidate_list_from_any(nested_map.get("meeting")))
        env_value = _clean_text(os.getenv("VON_FILE_COPY_MEETING_WORKFLOW_ID"))
        if env_value:
            candidates.append(env_value)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        token = _clean_text(item)
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _resolve_typing_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    supplied = raw.get("typing_result")
    if isinstance(supplied, Mapping):
        typing_result = dict(supplied)
    else:
        typing_result = infer_file_copy_typing(
            content_type=_clean_text(raw.get("content_type")) or None,
            original_filename=_clean_text(raw.get("original_filename")) or None,
            size_bytes=_coerce_int(raw.get("size_bytes")),
        )

    route_scores = typing_result.get("route_scores")
    if not isinstance(route_scores, Mapping):
        route_scores = {}

    typed_route_hint = _clean_text(typing_result.get("route_hint")).lower()
    if typed_route_hint and typed_route_hint not in route_scores:
        try:
            route_scores = dict(route_scores)
            route_scores[typed_route_hint] = float(
                typing_result.get("route_confidence") or 0.0
            )
        except Exception:
            route_scores = dict(route_scores)

    typing_result["route_scores"] = {
        _clean_text(key): round(float(value), 4)
        for key, value in dict(route_scores).items()
        if _clean_text(key)
    }
    return typing_result


def _select_primary_route(
    scored: Mapping[str, float],
    *,
    minimum_score: float,
) -> tuple[str, float]:
    winner = ROUTE_KEY_INTERPRET
    winner_score = 0.0
    for key, value in scored.items():
        if value > winner_score:
            winner = key
            winner_score = float(value)
    if winner_score < minimum_score:
        return ROUTE_KEY_INTERPRET, round(max(0.4, winner_score), 4)
    return winner, round(winner_score, 4)


def _build_classification_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    filename = _normalise_filename(raw.get("original_filename"))
    content_type = _normalise_content_type(raw.get("content_type"))
    size_bytes = _coerce_int(raw.get("size_bytes"))
    allow_interpret_fallback = _coerce_bool(
        raw.get("allow_interpret_fallback"),
        default=DEFAULT_ALLOW_INTERPRET_FALLBACK,
    )
    min_route_score = _coerce_float(
        raw.get("minimum_route_score"),
        default=DEFAULT_MINIMUM_ROUTE_SCORE,
        minimum=0.2,
        maximum=0.95,
    )
    min_mutation_confidence = _coerce_float(
        raw.get("minimum_mutation_confidence"),
        default=DEFAULT_MINIMUM_MUTATION_CONFIDENCE,
        minimum=0.5,
        maximum=0.99,
    )

    forced_route = _extract_route_from_force_override(raw.get("force_route_key"))
    typing_result = _resolve_typing_result(raw)
    scored = {
        str(key).strip(): round(float(value), 4)
        for key, value in dict(typing_result.get("route_scores") or {}).items()
        if str(key).strip()
    }
    route_key: str
    route_confidence: float
    reasons: list[str] = []
    if forced_route is not None:
        route_key = forced_route
        route_confidence = 1.0
        reasons.append("forced_route_override")
    else:
        typed_route_hint = _clean_text(typing_result.get("route_hint")).lower()
        typed_route_confidence = round(
            float(typing_result.get("route_confidence") or 0.0), 4
        )
        if typed_route_hint and typed_route_confidence >= min_route_score:
            route_key = typed_route_hint
            route_confidence = typed_route_confidence
            reasons.append("typed_file_copy_context")
        else:
            route_key, route_confidence = _select_primary_route(
                scored,
                minimum_score=min_route_score,
            )
            reasons.append(
                "typed_route_hint_below_threshold"
                if typed_route_hint
                else "typed_file_copy_context_missing_route_hint"
            )

    available_workflow_ids = set(_resolve_available_workflow_ids(raw))
    target_workflow_id: str | None = None
    target_workflow_available = False
    unsupported_specialised_route = False
    unsupported_route_reason: str | None = None
    mutation_route = route_key in {
        ROUTE_KEY_SCHOLARLY,
        ROUTE_KEY_CV,
        ROUTE_KEY_BUSINESS_CARD,
        ROUTE_KEY_MEETING,
    }

    route_mode = ROUTE_MODE_INTERPRET
    fail_closed = False
    if route_key == ROUTE_KEY_NOOP:
        route_mode = ROUTE_MODE_NOOP
        reasons.append("noop_route_selected")
    elif mutation_route:
        candidates = _resolve_specialised_candidates(route_key=route_key, payload=raw)
        for candidate in candidates:
            if candidate in available_workflow_ids:
                target_workflow_id = candidate
                target_workflow_available = True
                break

        if not target_workflow_available:
            unsupported_specialised_route = True
            unsupported_route_reason = "specialised_workflow_unavailable"
            reasons.append(unsupported_route_reason)
            mutation_route = False
            route_mode = (
                ROUTE_MODE_INTERPRET if allow_interpret_fallback else ROUTE_MODE_NOOP
            )
        else:
            route_mode = ROUTE_MODE_SPECIALISED
            if route_confidence < min_mutation_confidence:
                fail_closed = True
                route_mode = ROUTE_MODE_FAIL_CLOSED
                reasons.append("mutation_route_confidence_below_threshold")
    else:
        route_mode = (
            ROUTE_MODE_INTERPRET if allow_interpret_fallback else ROUTE_MODE_NOOP
        )
        if route_mode == ROUTE_MODE_NOOP:
            reasons.append("interpret_fallback_disabled")

    concept_id = _clean_text(raw.get("file_copy_concept_id")) or _clean_text(
        raw.get("concept_id")
    )
    return {
        "classification_version": FILE_COPY_UPLOAD_CLASSIFICATION_VERSION,
        "classified_at": _utc_now_iso(),
        "concept_id": concept_id or None,
        "file_copy_concept_id": concept_id or None,
        "route_key": route_key,
        "route_mode": route_mode,
        "route_confidence": route_confidence,
        "minimum_route_score": min_route_score,
        "minimum_mutation_confidence": min_mutation_confidence,
        "mutation_route": bool(mutation_route),
        "fail_closed": bool(fail_closed),
        "target_workflow_id": target_workflow_id,
        "target_workflow_available": bool(target_workflow_available),
        "unsupported_specialised_route": bool(unsupported_specialised_route),
        "unsupported_route_reason": unsupported_route_reason,
        "allow_interpret_fallback": bool(allow_interpret_fallback),
        "route_reasons": list(reasons),
        "scored_candidates": dict(scored),
        "filename": filename or None,
        "content_type": content_type or None,
        "size_bytes": size_bytes,
        "typing_result": typing_result,
        "typing_schema_version": typing_result.get("schema_version"),
        "typing_primary_type_concept_id": typing_result.get("primary_type_concept_id"),
        "typing_semantic_type_concept_id": typing_result.get(
            "semantic_type_concept_id"
        ),
        "typing_format_type_concept_id": typing_result.get("format_type_concept_id"),
        "typing_asserted_type_concept_ids": list(
            typing_result.get("asserted_type_concept_ids") or []
        ),
    }


def _handle_classify_upload(request: WorkflowActionRequest) -> WorkflowActionResult:
    combined: dict[str, Any] = dict(request.data or {})
    combined.update(dict(request.inputs or {}))
    decision = _build_classification_payload(combined)
    return WorkflowActionResult(status="success", outputs=decision)


def _handle_persist_route_decision(request: WorkflowActionRequest) -> WorkflowActionResult:
    concept_id = _clean_text(request.data.get("file_copy_concept_id")) or _clean_text(
        request.data.get("concept_id")
    )
    if not concept_id:
        return WorkflowActionResult(
            status="success",
            outputs={
                "route_decision_persisted": False,
                "route_decision_persist_error": "missing_file_copy_concept_id",
            },
        )

    payload = {
        "classification_version": str(
            request.data.get("classification_version")
            or FILE_COPY_UPLOAD_CLASSIFICATION_VERSION
        ),
        "classified_at": request.data.get("classified_at"),
        "route_key": request.data.get("route_key"),
        "route_mode": request.data.get("route_mode"),
        "route_confidence": request.data.get("route_confidence"),
        "minimum_route_score": request.data.get("minimum_route_score"),
        "minimum_mutation_confidence": request.data.get("minimum_mutation_confidence"),
        "mutation_route": bool(request.data.get("mutation_route")),
        "fail_closed": bool(request.data.get("fail_closed")),
        "target_workflow_id": request.data.get("target_workflow_id"),
        "target_workflow_available": bool(request.data.get("target_workflow_available")),
        "unsupported_specialised_route": bool(
            request.data.get("unsupported_specialised_route")
        ),
        "unsupported_route_reason": request.data.get("unsupported_route_reason"),
        "allow_interpret_fallback": bool(request.data.get("allow_interpret_fallback", True)),
        "route_reasons": list(request.data.get("route_reasons") or []),
        "scored_candidates": dict(request.data.get("scored_candidates") or {}),
        "filename": request.data.get("filename"),
        "content_type": request.data.get("content_type"),
        "typing_schema_version": request.data.get("typing_schema_version"),
        "typing_primary_type_concept_id": request.data.get(
            "typing_primary_type_concept_id"
        ),
        "typing_semantic_type_concept_id": request.data.get(
            "typing_semantic_type_concept_id"
        ),
        "typing_format_type_concept_id": request.data.get(
            "typing_format_type_concept_id"
        ),
        "typing_asserted_type_concept_ids": list(
            request.data.get("typing_asserted_type_concept_ids") or []
        ),
    }

    try:
        upsert = upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate=FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE,
            text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            lang="en-NZ",
            garbage_collect=True,
            context={
                "source": FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
                "classification_version": FILE_COPY_UPLOAD_CLASSIFICATION_VERSION,
            },
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "route_decision_persisted": True,
                "route_decision_predicate": FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE,
                "route_decision_relation_id": (
                    upsert.get("kept_relation_id") if isinstance(upsert, Mapping) else None
                ),
                "route_decision_replaced_count": (
                    int(upsert.get("replaced_count") or 0)
                    if isinstance(upsert, Mapping)
                    else 0
                ),
            },
        )
    except Exception as exc:
        return WorkflowActionResult(
            status="success",
            outputs={
                "route_decision_persisted": False,
                "route_decision_persist_error": str(exc),
                "route_decision_predicate": FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE,
            },
        )


def build_file_copy_upload_classification_workflow_test_definition() -> WorkflowDefinition:
    classify_writes_context_keys = [
        "classification_version",
        "route_key",
        "route_mode",
        "route_confidence",
        "minimum_mutation_confidence",
        "minimum_route_score",
        "route_reasons",
        "mutation_route",
        "fail_closed",
        "target_workflow_id",
        "target_workflow_available",
        "unsupported_specialised_route",
        "unsupported_route_reason",
        "allow_interpret_fallback",
        "available_workflow_ids",
        "typing_result",
    ]
    persist_decision_writes_context_keys = [
        "route_decision_persisted",
        "route_decision_predicate",
        "route_decision_relation_id",
        "route_decision_replaced_count",
        "route_decision_persist_error",
    ]
    classify = WorkflowStateSpec(
        state_id="classify",
        actions=(
            WorkflowActionInvocation(
                action_id="file_copy_upload.classify",
                description=(
                    "Classify uploaded file-copy routing mode and confidence for "
                    "workflow-first upload handling."
                ),
            ),
        ),
        metadata={"writes_context_keys": classify_writes_context_keys},
        transitions=(
            WorkflowTransitionSpec(
                to_state="persist_decision",
                condition=lambda _ctx: True,
                reason="classification_completed",
            ),
        ),
    )

    persist_decision = WorkflowStateSpec(
        state_id="persist_decision",
        actions=(
            WorkflowActionInvocation(
                action_id="file_copy_upload.persist_decision",
                description=(
                    "Persist upload route decision so branch-selection behaviour "
                    "remains inspectable."
                ),
            ),
        ),
        metadata={"writes_context_keys": persist_decision_writes_context_keys},
        transitions=(
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="decision_persisted",
            ),
        ),
    )

    return WorkflowDefinition(
        workflow_id=FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
        initial_state="classify",
        states={
            "classify": classify,
            "persist_decision": persist_decision,
            "complete": WorkflowStateSpec(state_id="complete", terminal=True),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Classify uploaded file copies for workflow routing with explicit "
            "confidence guardrails and inspectable decision persistence."
        ),
    )


def build_file_copy_upload_classification_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
        definition=build_file_copy_upload_classification_workflow_test_definition(),
        purpose=(
            "Classify uploaded file copies and persist route decisions before "
            "dispatching into specialised or baseline workflows."
        ),
        source="built_in",
    )


def register_file_copy_upload_classification_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id="file_copy_upload.classify",
            handler=_handle_classify_upload,
            description="Classify uploaded file-copy routing mode and confidence.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id="file_copy_upload.persist_decision",
            handler=_handle_persist_route_decision,
            description=(
                "Persist file-copy upload route decision as singleton text relation."
            ),
        )
    )


__all__ = [
    "DEFAULT_ALLOW_INTERPRET_FALLBACK",
    "DEFAULT_BUSINESS_CARD_WORKFLOW_ID",
    "DEFAULT_CV_WORKFLOW_ID",
    "DEFAULT_FORCE_ROUTE_KEY_SENTINEL",
    "DEFAULT_MINIMUM_MUTATION_CONFIDENCE",
    "DEFAULT_MINIMUM_ROUTE_SCORE",
    "DEFAULT_SCHOLARLY_WORKFLOW_ID",
    "DEFAULT_UNUSED_SPECIALISED_WORKFLOW_ID",
    "FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID",
    "FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE",
    "FILE_COPY_UPLOAD_CLASSIFICATION_VERSION",
    "build_file_copy_upload_classification_workflow_test_definition",
    "build_file_copy_upload_classification_workflow_test_registration",
    "register_file_copy_upload_classification_actions",
]
