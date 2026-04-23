"""Represented critique-driven workflow self-improvement support surfaces."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .episode_critique_benchmark_service import build_episode_critique_benchmark_report
from .episode_critique_memory_service import (
    get_episode_critique_memory_state,
    record_episode_critique_memory_self_improvement,
)
from .episode_evaluation_workflow_contracts import (
    EPISODE_SELF_IMPROVEMENT_MAX_LAUNCHES,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
)
from .workflow_authoring_vontology_service import (
    build_workflow_authoring_prompt_contract,
    get_workflow_authoring_prompt_health_status,
)
from ..workflows.durable import WorkflowInstanceManager
from ..workflows.durable.workflow_instance_submission_service import (
    submit_verified_workflow_instance,
)
from ..workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
    resolve_workflow_publication_lifecycle,
)
from ..workflows.workflow_authoring_service import (
    serialise_workflow_definition_to_authoring_spec,
)
from ..workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
)
from ..workflows.workflow_studio_service import (
    get_workflow_authoring_proposal,
    record_workflow_authoring_promotion_evaluation,
    submit_workflow_authoring_proposal,
)

logger = logging.getLogger(__name__)

EPISODE_SELF_IMPROVEMENT_PROPOSAL_CONTEXT_SCHEMA_VERSION = (
    "episode_self_improvement_proposal_context.v1"
)
EPISODE_SELF_IMPROVEMENT_PROMOTION_EVALUATION_SCHEMA_VERSION = (
    "episode_self_improvement_promotion_evaluation.v1"
)

_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _normalise_strings(values: Any, *, limit: int = 40) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _json_fingerprint(value: Any, *, length: int = 16) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _benchmark_prompt_context(report: Mapping[str, Any]) -> dict[str, Any]:
    benchmark_signals = [
        _mapping_or_empty(item)
        for item in (report.get("benchmark_signals") or [])
        if isinstance(item, Mapping)
    ]
    failed_signals = [
        {
            "signal_id": _safe_str(item.get("signal_id")),
            "title": _safe_str(item.get("title")),
            "dimension": _safe_str(item.get("dimension")),
            "details": _mapping_or_empty(item.get("details")),
        }
        for item in benchmark_signals
        if _safe_str(item.get("status")) == "fail"
    ]
    capability_gaps = [
        _mapping_or_empty(item)
        for item in (report.get("capability_gaps") or [])
        if isinstance(item, Mapping)
    ]
    return {
        "benchmark_fingerprint": _safe_str(report.get("benchmark_fingerprint")),
        "benchmark_signal_summary": _mapping_or_empty(
            report.get("benchmark_signal_summary")
        ),
        "failed_benchmark_signals": failed_signals[:6],
        "capability_gaps": capability_gaps[:6],
        "recommendations": _normalise_strings(report.get("recommendations"), limit=8),
    }


def _find_workflow_target_suggestion(
    *,
    memory_state: Mapping[str, Any],
    suggestion_id: str | None = None,
    target_workflow_id: str | None = None,
) -> dict[str, Any] | None:
    wanted_suggestion_id = _safe_str(suggestion_id)
    wanted_workflow_id = _safe_str(target_workflow_id)
    for raw_item in memory_state.get("improvement_suggestions") or []:
        item = _mapping_or_empty(raw_item)
        if _safe_str(item.get("target_surface")) != "workflow":
            continue
        item_workflow_id = _safe_str(item.get("target_workflow_id"))
        item_suggestion_id = _safe_str(item.get("suggestion_id"))
        if not item_workflow_id or not item_suggestion_id:
            continue
        if wanted_suggestion_id and item_suggestion_id != wanted_suggestion_id:
            continue
        if wanted_workflow_id and item_workflow_id != wanted_workflow_id:
            continue
        return item
    return None


def list_episode_self_improvement_candidates(
    *,
    memory_state: Mapping[str, Any],
    limit: int = EPISODE_SELF_IMPROVEMENT_MAX_LAUNCHES,
) -> list[dict[str, Any]]:
    suggestions = [
        _mapping_or_empty(item)
        for item in (memory_state.get("improvement_suggestions") or [])
        if isinstance(item, Mapping)
    ]
    candidates: list[dict[str, Any]] = []
    seen_workflow_ids: set[str] = set()
    for item in sorted(
        suggestions,
        key=lambda row: (
            _PRIORITY_RANK.get(_safe_str(row.get("priority")) or "low", 99),
            _safe_str(row.get("target_workflow_id")) or "",
            _safe_str(row.get("suggestion_id")) or "",
        ),
    ):
        if _safe_str(item.get("target_surface")) != "workflow":
            continue
        workflow_id = _safe_str(item.get("target_workflow_id"))
        suggestion_id = _safe_str(item.get("suggestion_id"))
        if not workflow_id or not suggestion_id or workflow_id in seen_workflow_ids:
            continue
        seen_workflow_ids.add(workflow_id)
        proposal = _mapping_or_empty(get_workflow_authoring_proposal(workflow_id))
        candidates.append(
            {
                **item,
                "active_proposal_status": _safe_str(proposal.get("status")),
                "active_proposal_id": _safe_str(proposal.get("proposal_id")),
            }
        )
        if len(candidates) >= max(1, min(int(limit), EPISODE_SELF_IMPROVEMENT_MAX_LAUNCHES)):
            break
    return candidates


def launch_episode_self_improvement_workflows(
    *,
    memory_id: str | None = None,
    memory_state: Mapping[str, Any] | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    namespace: str | None = None,
    current_depth: int = 0,
) -> dict[str, Any]:
    state = (
        dict(memory_state)
        if isinstance(memory_state, Mapping)
        else get_episode_critique_memory_state(_safe_str(memory_id) or "")
    )
    if not isinstance(state, Mapping):
        return {"success": False, "reason": "episode_critique_memory_missing"}

    resolved_memory_id = _safe_str(state.get("memory_id")) or _safe_str(memory_id)
    resolved_namespace = _safe_str(namespace) or _safe_str(state.get("namespace"))
    resolved_user_id = _safe_str(user_id) or _safe_str(state.get("user_id"))
    resolved_org_id = _safe_str(org_id) or _safe_str(state.get("org_id"))
    if not resolved_memory_id or not resolved_namespace or not resolved_user_id:
        return {"success": False, "reason": "self_improvement_actor_context_missing"}

    launches: list[dict[str, Any]] = []
    manager = WorkflowInstanceManager()
    for candidate in list_episode_self_improvement_candidates(memory_state=state):
        target_workflow_id = _safe_str(candidate.get("target_workflow_id"))
        suggestion_id = _safe_str(candidate.get("suggestion_id"))
        if not target_workflow_id or not suggestion_id:
            continue
        created_at_utc = _utcnow_iso()
        existing_status = _safe_str(candidate.get("active_proposal_status"))
        if existing_status == "pending_review":
            launches.append(
                {
                    "created_at_utc": created_at_utc,
                    "suggestion_id": suggestion_id,
                    "target_workflow_id": target_workflow_id,
                    "launch_workflow_id": EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
                    "success": False,
                    "status": "suppressed_pending_review_proposal",
                    "proposal_id": _safe_str(candidate.get("active_proposal_id")),
                    "reason": "workflow_authoring_proposal_pending_review",
                }
            )
            continue

        inputs = {
            "episode_critique_memory_id": resolved_memory_id,
            "suggestion_id": suggestion_id,
            "target_workflow_id": target_workflow_id,
            "namespace": resolved_namespace,
            "user_id": resolved_user_id,
            "org_id": resolved_org_id,
            "episode_evaluation_depth": int(current_depth or 0),
        }
        submission = submit_verified_workflow_instance(
            manager=manager,
            workflow_id=EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
            user_id=resolved_user_id,
            org_id=resolved_org_id,
            namespace=resolved_namespace,
            inputs=inputs,
            max_retries=1,
            source_event_type="episode_critic.self_improvement_proposal",
            source_event_id=resolved_memory_id.replace("#V#", ""),
            event_idempotency_key=(
                "episode_critic.self_improvement_proposal:"
                f"{_json_fingerprint({'memory_id': resolved_memory_id, 'suggestion_id': suggestion_id})}"
            ),
        )
        launches.append(
            {
                "created_at_utc": created_at_utc,
                "suggestion_id": suggestion_id,
                "target_workflow_id": target_workflow_id,
                "launch_workflow_id": EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
                "success": bool(submission.success),
                "status": submission.status,
                "instance_id": submission.instance_id,
                "reason": _safe_str(submission.error_code) or _safe_str(submission.error),
            }
        )

    record_result = record_episode_critique_memory_self_improvement(
        memory_id=resolved_memory_id,
        launches=launches,
    )
    return {
        "success": True,
        "memory_id": resolved_memory_id,
        "launches": launches,
        "launched_count": sum(1 for item in launches if item.get("success")),
        "suppressed_count": sum(1 for item in launches if not item.get("success")),
        "state": record_result.get("state"),
    }


def build_workflow_improvement_context(
    *,
    episode_critique_memory_id: str,
    suggestion_id: str | None = None,
    target_workflow_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    memory_state = get_episode_critique_memory_state(episode_critique_memory_id)
    if not isinstance(memory_state, Mapping):
        return {"success": False, "error": "episode_critique_memory_missing"}

    suggestion = _find_workflow_target_suggestion(
        memory_state=memory_state,
        suggestion_id=suggestion_id,
        target_workflow_id=target_workflow_id,
    )
    if not suggestion:
        return {"success": False, "error": "workflow_target_improvement_suggestion_missing"}

    resolved_target_workflow_id = _safe_str(suggestion.get("target_workflow_id"))
    if not resolved_target_workflow_id:
        return {"success": False, "error": "workflow_target_id_missing"}

    existing_proposal = _mapping_or_empty(
        get_workflow_authoring_proposal(resolved_target_workflow_id)
    )
    if _safe_str(existing_proposal.get("status")) == "pending_review":
        return {
            "success": False,
            "error": "workflow_authoring_proposal_pending_review",
            "proposal_id": _safe_str(existing_proposal.get("proposal_id")),
        }

    definition = load_workflow_definition_from_vontology(resolved_target_workflow_id)
    if definition is None:
        return {
            "success": False,
            "error": "workflow_definition_missing",
            "workflow_id": resolved_target_workflow_id,
        }

    try:
        authoring_spec = serialise_workflow_definition_to_authoring_spec(definition)
    except Exception as exc:
        return {
            "success": False,
            "error": "workflow_definition_serialisation_failed",
            "details": str(exc),
        }

    definition_identity = build_workflow_definition_identity(
        workflow_id=resolved_target_workflow_id,
        source="episode_self_improvement_context",
        definition=definition,
        authoritative_definition=definition,
    )
    benchmark_report = build_episode_critique_benchmark_report(
        namespace=_safe_str(namespace) or _safe_str(memory_state.get("namespace")) or "",
        workflow_id=resolved_target_workflow_id,
        scan_limit=200,
        max_audit_cases=5,
    )
    benchmark_summary = _benchmark_prompt_context(_mapping_or_empty(benchmark_report))

    proposal_context = {
        "schema_version": EPISODE_SELF_IMPROVEMENT_PROPOSAL_CONTEXT_SCHEMA_VERSION,
        "source": "episode_self_improvement_workflow",
        "episode_critique_memory_id": episode_critique_memory_id,
        "request_id": _safe_str(memory_state.get("request_id")),
        "source_workflow_id": _safe_str(
            _mapping_or_empty(memory_state.get("subject_episode")).get("workflow_id")
        ),
        "suggestion": suggestion,
        "benchmark_summary": benchmark_summary,
    }

    return {
        "success": True,
        "episode_critique_memory_id": episode_critique_memory_id,
        "target_workflow_id": resolved_target_workflow_id,
        "target_workflow_definition_identity": definition_identity,
        "base_definition_hash": _safe_str(definition_identity.get("definition_hash")),
        "existing_workflow_spec": authoring_spec,
        "episode_self_improvement_suggestion": suggestion,
        "episode_self_improvement_benchmark_summary": benchmark_summary,
        "proposal_context": proposal_context,
        "workflow_authoring_prompt_contract": build_workflow_authoring_prompt_contract(),
        "workflow_authoring_prompt_health": get_workflow_authoring_prompt_health_status(),
    }


def _normalise_candidate_workflow_spec(
    *,
    candidate_workflow_spec: Mapping[str, Any],
    target_workflow_id: str,
) -> dict[str, Any]:
    payload = {str(key): value for key, value in candidate_workflow_spec.items() if isinstance(key, str)}
    candidate_workflow_id = _safe_str(payload.get("workflow_id"))
    if candidate_workflow_id and candidate_workflow_id != target_workflow_id:
        raise ValueError("workflow_improvement_candidate_workflow_id_mismatch")
    payload["workflow_id"] = target_workflow_id
    return payload


def submit_workflow_improvement_proposal(
    *,
    episode_critique_memory_id: str,
    target_workflow_id: str,
    candidate_workflow_spec: Mapping[str, Any],
    suggestion: Mapping[str, Any],
    proposal_context: Mapping[str, Any] | None = None,
    base_definition_hash: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    current_depth: int = 0,
) -> dict[str, Any]:
    resolved_target_workflow_id = _safe_str(target_workflow_id)
    if not resolved_target_workflow_id:
        return {"success": False, "error": "target_workflow_id_required"}

    try:
        normalised_spec = _normalise_candidate_workflow_spec(
            candidate_workflow_spec=candidate_workflow_spec,
            target_workflow_id=resolved_target_workflow_id,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    submit_result = submit_workflow_authoring_proposal(
        resolved_target_workflow_id,
        authoring_spec=normalised_spec,
        base_definition_hash=_safe_str(base_definition_hash) or None,
        session_id=None,
        turn_id=_safe_str(episode_critique_memory_id),
        proposed_by=_safe_str(user_id) or None,
        proposal_context=proposal_context,
    )
    if not bool(submit_result.get("success")):
        return {
            "success": False,
            "error": _safe_str(submit_result.get("error")) or "workflow_proposal_submit_failed",
        }

    proposal = _mapping_or_empty(submit_result.get("proposal"))
    candidate_validation = _mapping_or_empty(submit_result.get("candidate_validation"))
    suggestion_payload = _mapping_or_empty(suggestion)
    proposal_entry = {
        "created_at_utc": _utcnow_iso(),
        "episode_critique_memory_id": episode_critique_memory_id,
        "suggestion_id": _safe_str(suggestion_payload.get("suggestion_id")),
        "target_workflow_id": resolved_target_workflow_id,
        "proposal_id": _safe_str(proposal.get("proposal_id")),
        "status": _safe_str(proposal.get("status")),
        "candidate_validation": candidate_validation or None,
    }
    record_episode_critique_memory_self_improvement(
        memory_id=episode_critique_memory_id,
        proposals=[proposal_entry],
    )

    promotion_launch_inputs = {
        "episode_critique_memory_id": episode_critique_memory_id,
        "target_workflow_id": resolved_target_workflow_id,
        "proposal_id": _safe_str(proposal.get("proposal_id")),
        "suggestion_id": _safe_str(suggestion_payload.get("suggestion_id")),
        "namespace": _safe_str(namespace),
        "user_id": _safe_str(user_id),
        "org_id": _safe_str(org_id),
        "episode_evaluation_depth": int(current_depth or 0),
    }
    return {
        "success": True,
        "workflow_id": resolved_target_workflow_id,
        "proposal": proposal,
        "candidate_validation": candidate_validation,
        "promotion_launch_inputs": promotion_launch_inputs,
        "promotion_launch_source_event_id": (
            _safe_str(proposal.get("proposal_id")) or resolved_target_workflow_id
        ),
        "promotion_launch_event_idempotency_key": (
            "episode_critic.self_improvement_promotion:"
            f"{_json_fingerprint({'proposal_id': proposal.get('proposal_id'), 'workflow_id': resolved_target_workflow_id})}"
        ),
    }


def build_workflow_promotion_context(
    *,
    episode_critique_memory_id: str,
    target_workflow_id: str,
    proposal_id: str | None = None,
    suggestion_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    memory_state = get_episode_critique_memory_state(episode_critique_memory_id)
    if not isinstance(memory_state, Mapping):
        return {"success": False, "error": "episode_critique_memory_missing"}

    suggestion = _find_workflow_target_suggestion(
        memory_state=memory_state,
        suggestion_id=suggestion_id,
        target_workflow_id=target_workflow_id,
    )
    if not suggestion:
        return {"success": False, "error": "workflow_target_improvement_suggestion_missing"}

    proposal = _mapping_or_empty(get_workflow_authoring_proposal(target_workflow_id))
    if not proposal:
        return {"success": False, "error": "workflow_authoring_proposal_missing"}
    expected_proposal_id = _safe_str(proposal_id)
    stored_proposal_id = _safe_str(proposal.get("proposal_id"))
    if expected_proposal_id and stored_proposal_id and expected_proposal_id != stored_proposal_id:
        return {"success": False, "error": "workflow_authoring_proposal_id_mismatch"}

    lifecycle, _source = resolve_workflow_publication_lifecycle(target_workflow_id)
    benchmark_report = build_episode_critique_benchmark_report(
        namespace=_safe_str(namespace) or _safe_str(memory_state.get("namespace")) or "",
        workflow_id=target_workflow_id,
        scan_limit=200,
        max_audit_cases=5,
    )
    return {
        "success": True,
        "episode_critique_memory_id": episode_critique_memory_id,
        "target_workflow_id": target_workflow_id,
        "proposal_id": stored_proposal_id,
        "workflow_authoring_proposal": proposal,
        "workflow_publication_lifecycle": _mapping_or_empty(lifecycle),
        "episode_self_improvement_suggestion": suggestion,
        "episode_self_improvement_benchmark_summary": _benchmark_prompt_context(
            _mapping_or_empty(benchmark_report)
        ),
    }


def record_workflow_promotion_evaluation(
    *,
    episode_critique_memory_id: str,
    target_workflow_id: str,
    proposal_id: str,
    suggestion: Mapping[str, Any],
    benchmark_summary: Mapping[str, Any],
    promotion_recommendation: str,
    summary: str | None = None,
    reasoning: str | None = None,
    required_follow_up: Sequence[str] | None = None,
    approval_ready: bool | None = None,
) -> dict[str, Any]:
    evaluation = {
        "schema_version": EPISODE_SELF_IMPROVEMENT_PROMOTION_EVALUATION_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "target_workflow_id": target_workflow_id,
        "episode_critique_memory_id": episode_critique_memory_id,
        "suggestion_id": _safe_str(_mapping_or_empty(suggestion).get("suggestion_id")),
        "promotion_recommendation": _safe_str(promotion_recommendation),
        "summary": _safe_str(summary),
        "reasoning": _safe_str(reasoning),
        "required_follow_up": _normalise_strings(required_follow_up, limit=10),
        "approval_ready": approval_ready if isinstance(approval_ready, bool) else None,
        "benchmark_fingerprint": _safe_str(
            _mapping_or_empty(benchmark_summary).get("benchmark_fingerprint")
        ),
        "benchmark_signal_summary": _mapping_or_empty(
            _mapping_or_empty(benchmark_summary).get("benchmark_signal_summary")
        ),
        "created_at_utc": _utcnow_iso(),
    }
    result = record_workflow_authoring_promotion_evaluation(
        target_workflow_id,
        promotion_evaluation=evaluation,
        proposal_id=proposal_id,
    )
    record_episode_critique_memory_self_improvement(
        memory_id=episode_critique_memory_id,
        promotion_evaluations=[evaluation],
    )
    return {
        "success": True,
        "workflow_id": target_workflow_id,
        "proposal": result.get("proposal"),
        "promotion_evaluation": evaluation,
    }


__all__ = [
    "EPISODE_SELF_IMPROVEMENT_PROMOTION_EVALUATION_SCHEMA_VERSION",
    "EPISODE_SELF_IMPROVEMENT_PROPOSAL_CONTEXT_SCHEMA_VERSION",
    "build_workflow_improvement_context",
    "build_workflow_promotion_context",
    "launch_episode_self_improvement_workflows",
    "list_episode_self_improvement_candidates",
    "record_workflow_promotion_evaluation",
    "submit_workflow_improvement_proposal",
]
