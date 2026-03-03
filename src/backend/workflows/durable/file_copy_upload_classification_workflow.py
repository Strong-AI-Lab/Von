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
import re
from typing import Any, Mapping

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

DEFAULT_SCHOLARLY_WORKFLOW_ID = "#V#integration_scholarly_paper_representation_workflow"
DEFAULT_CV_WORKFLOW_ID = "#V#file_copy_cv_representation_workflow"
DEFAULT_BUSINESS_CARD_WORKFLOW_ID = "#V#file_copy_business_card_representation_workflow"

ROUTE_KEY_SCHOLARLY = "scholarly"
ROUTE_KEY_CV = "cv"
ROUTE_KEY_BUSINESS_CARD = "business_card"
ROUTE_KEY_INTERPRET = "interpret"
ROUTE_KEY_NOOP = "noop"

ROUTE_MODE_SPECIALISED = "specialised"
ROUTE_MODE_INTERPRET = "interpret"
ROUTE_MODE_FAIL_CLOSED = "fail_closed"
ROUTE_MODE_NOOP = "noop"

_ARXIV_FILENAME_RE = re.compile(r"(?<!\d)\d{4}\.\d{4,5}(?:v\d+)?", re.IGNORECASE)
_SCHOLARLY_TOKEN_RE = re.compile(
    r"\b(arxiv|paper|preprint|manuscript|journal|conference|doi)\b",
    re.IGNORECASE,
)
_CV_TOKEN_RE = re.compile(
    r"\b(cv|resume|résumé|curriculum[\s\-_]*vitae)\b",
    re.IGNORECASE,
)
_BUSINESS_CARD_TOKEN_RE = re.compile(
    r"\b(business[\s\-_]*card|biz[\s\-_]*card|contact[\s\-_]*card)\b",
    re.IGNORECASE,
)


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

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        token = _clean_text(item)
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _resolve_score_candidates(
    *,
    filename: str,
    content_type: str,
    size_bytes: int | None,
) -> dict[str, float]:
    is_pdf = content_type == "application/pdf" or filename.endswith(".pdf")
    is_image = content_type.startswith("image/") or filename.endswith(
        (
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".bmp",
            ".tif",
            ".tiff",
            ".heic",
            ".heif",
        )
    )

    scholarly_score = 0.0
    if is_pdf:
        scholarly_score += 0.2
    if _ARXIV_FILENAME_RE.search(filename):
        scholarly_score += 0.65
    if _SCHOLARLY_TOKEN_RE.search(filename):
        scholarly_score += 0.2
    scholarly_score = min(0.99, scholarly_score)

    cv_score = 0.0
    if _CV_TOKEN_RE.search(filename):
        cv_score += 0.72
    if is_pdf:
        cv_score += 0.2
    if content_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }:
        cv_score += 0.15
    cv_score = min(0.99, cv_score)

    business_card_score = 0.0
    if _BUSINESS_CARD_TOKEN_RE.search(filename):
        business_card_score += 0.78
    if is_image:
        business_card_score += 0.16
    if isinstance(size_bytes, int) and size_bytes > 0 and size_bytes < 3_000_000:
        business_card_score += 0.05
    business_card_score = min(0.99, business_card_score)

    return {
        ROUTE_KEY_SCHOLARLY: round(scholarly_score, 4),
        ROUTE_KEY_CV: round(cv_score, 4),
        ROUTE_KEY_BUSINESS_CARD: round(business_card_score, 4),
    }


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
        default=True,
    )
    min_route_score = _coerce_float(
        raw.get("minimum_route_score"),
        default=0.58,
        minimum=0.2,
        maximum=0.95,
    )
    min_mutation_confidence = _coerce_float(
        raw.get("minimum_mutation_confidence"),
        default=0.84,
        minimum=0.5,
        maximum=0.99,
    )

    forced_route = _extract_route_from_force_override(raw.get("force_route_key"))
    scored = _resolve_score_candidates(
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    route_key: str
    route_confidence: float
    reasons: list[str] = []
    if forced_route is not None:
        route_key = forced_route
        route_confidence = 1.0
        reasons.append("forced_route_override")
    else:
        route_key, route_confidence = _select_primary_route(
            scored,
            minimum_score=min_route_score,
        )
        reasons.append("heuristic_classifier")

    available_workflow_ids = set(_resolve_available_workflow_ids(raw))
    target_workflow_id: str | None = None
    target_workflow_available = False
    mutation_route = route_key in {
        ROUTE_KEY_SCHOLARLY,
        ROUTE_KEY_CV,
        ROUTE_KEY_BUSINESS_CARD,
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
            reasons.append("specialised_workflow_unavailable")
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
        "allow_interpret_fallback": bool(allow_interpret_fallback),
        "route_reasons": list(reasons),
        "scored_candidates": dict(scored),
        "filename": filename or None,
        "content_type": content_type or None,
        "size_bytes": size_bytes,
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
        "allow_interpret_fallback": bool(request.data.get("allow_interpret_fallback", True)),
        "route_reasons": list(request.data.get("route_reasons") or []),
        "scored_candidates": dict(request.data.get("scored_candidates") or {}),
        "filename": request.data.get("filename"),
        "content_type": request.data.get("content_type"),
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


def build_file_copy_upload_classification_workflow() -> WorkflowDefinition:
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


def get_file_copy_upload_classification_workflow_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
        definition=build_file_copy_upload_classification_workflow(),
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
    "FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID",
    "FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE",
    "FILE_COPY_UPLOAD_CLASSIFICATION_VERSION",
    "build_file_copy_upload_classification_workflow",
    "get_file_copy_upload_classification_workflow_registration",
    "register_file_copy_upload_classification_actions",
]
