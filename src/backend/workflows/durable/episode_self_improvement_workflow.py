"""Durable action handlers for represented episode self-improvement workflows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...services.episode_evaluation_workflow_contracts import (
    EPISODE_SELF_IMPROVEMENT_LOAD_CONTEXT_ACTION_ID,
    EPISODE_SELF_IMPROVEMENT_LOAD_PROMOTION_CONTEXT_ACTION_ID,
    EPISODE_SELF_IMPROVEMENT_RECORD_PROMOTION_ACTION_ID,
    EPISODE_SELF_IMPROVEMENT_SUBMIT_PROPOSAL_ACTION_ID,
)
from ...services.episode_self_improvement_service import (
    build_workflow_improvement_context,
    build_workflow_promotion_context,
    record_workflow_promotion_evaluation,
    submit_workflow_improvement_proposal,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _normalise_strings(values: Any, *, limit: int = 20) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
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


def _build_load_context_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        result = build_workflow_improvement_context(
            episode_critique_memory_id=_clean_text(
                request.inputs.get("episode_critique_memory_id")
                or request.data.get("episode_critique_memory_id")
            )
            or "",
            suggestion_id=_clean_text(
                request.inputs.get("suggestion_id") or request.data.get("suggestion_id")
            ),
            target_workflow_id=_clean_text(
                request.inputs.get("target_workflow_id")
                or request.data.get("target_workflow_id")
            ),
            namespace=_clean_text(
                request.inputs.get("namespace")
                or request.data.get("namespace")
                or getattr(request.environment, "user_namespace", None)
            ),
        )
        if not bool(result.get("success")):
            return WorkflowActionResult(
                status="failed",
                error=_clean_text(result.get("error"))
                or "episode_self_improvement_context_failed",
                outputs={"workflow_improvement_context": dict(result)},
            )
        outputs = dict(result)
        outputs["result"] = True
        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _build_submit_proposal_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        candidate_workflow_spec = _mapping_or_empty(
            request.data.get("candidate_workflow_spec")
            or request.data.get("repaired_workflow_spec")
        )
        if not candidate_workflow_spec:
            return WorkflowActionResult(
                status="failed",
                error="workflow_improvement_candidate_spec_missing",
            )

        result = submit_workflow_improvement_proposal(
            episode_critique_memory_id=_clean_text(
                request.data.get("episode_critique_memory_id")
            )
            or "",
            target_workflow_id=_clean_text(request.data.get("target_workflow_id")) or "",
            candidate_workflow_spec=candidate_workflow_spec,
            suggestion=_mapping_or_empty(request.data.get("episode_self_improvement_suggestion")),
            proposal_context=_mapping_or_empty(request.data.get("proposal_context")),
            base_definition_hash=_clean_text(request.data.get("base_definition_hash")),
            namespace=_clean_text(request.data.get("namespace")),
            user_id=_clean_text(request.data.get("user_id")),
            org_id=_clean_text(request.data.get("org_id")),
            current_depth=int(request.data.get("episode_evaluation_depth") or 0),
        )
        if not bool(result.get("success")):
            return WorkflowActionResult(
                status="failed",
                error=_clean_text(result.get("error"))
                or "workflow_improvement_proposal_submit_failed",
                outputs={"workflow_improvement_proposal": dict(result)},
            )

        outputs = {
            **dict(result),
            "proposal_id": _clean_text(_mapping_or_empty(result.get("proposal")).get("proposal_id")),
            "proposal_status": _clean_text(
                _mapping_or_empty(result.get("proposal")).get("status")
            ),
            "result": bool(_mapping_or_empty(result.get("proposal")).get("proposal_id")),
        }
        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _build_load_promotion_context_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        result = build_workflow_promotion_context(
            episode_critique_memory_id=_clean_text(
                request.inputs.get("episode_critique_memory_id")
                or request.data.get("episode_critique_memory_id")
            )
            or "",
            target_workflow_id=_clean_text(
                request.inputs.get("target_workflow_id")
                or request.data.get("target_workflow_id")
            )
            or "",
            proposal_id=_clean_text(
                request.inputs.get("proposal_id") or request.data.get("proposal_id")
            ),
            suggestion_id=_clean_text(
                request.inputs.get("suggestion_id") or request.data.get("suggestion_id")
            ),
            namespace=_clean_text(
                request.inputs.get("namespace")
                or request.data.get("namespace")
                or getattr(request.environment, "user_namespace", None)
            ),
        )
        if not bool(result.get("success")):
            return WorkflowActionResult(
                status="failed",
                error=_clean_text(result.get("error"))
                or "workflow_promotion_context_failed",
                outputs={"workflow_promotion_context": dict(result)},
            )
        outputs = dict(result)
        outputs["result"] = True
        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _build_record_promotion_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        assessment = _mapping_or_empty(
            request.data.get("workflow_promotion_assessment")
            or request.data.get("promotion_evaluation")
        )
        recommendation = _clean_text(
            assessment.get("promotion_recommendation") or assessment.get("recommendation")
        )
        if not recommendation:
            return WorkflowActionResult(
                status="failed",
                error="workflow_promotion_recommendation_missing",
            )

        result = record_workflow_promotion_evaluation(
            episode_critique_memory_id=_clean_text(
                request.data.get("episode_critique_memory_id")
            )
            or "",
            target_workflow_id=_clean_text(request.data.get("target_workflow_id")) or "",
            proposal_id=_clean_text(request.data.get("proposal_id")) or "",
            suggestion=_mapping_or_empty(request.data.get("episode_self_improvement_suggestion")),
            benchmark_summary=_mapping_or_empty(
                request.data.get("episode_self_improvement_benchmark_summary")
            ),
            promotion_recommendation=recommendation,
            summary=_clean_text(assessment.get("summary")),
            reasoning=_clean_text(assessment.get("reasoning")),
            required_follow_up=_normalise_strings(
                assessment.get("required_follow_up"),
                limit=10,
            ),
            approval_ready=assessment.get("approval_ready")
            if isinstance(assessment.get("approval_ready"), bool)
            else None,
        )
        if not bool(result.get("success")):
            return WorkflowActionResult(
                status="failed",
                error=_clean_text(result.get("error"))
                or "workflow_promotion_record_failed",
                outputs={"workflow_promotion_record": dict(result)},
            )
        return WorkflowActionResult(
            status="success",
            outputs={
                **dict(result),
                "result": recommendation == "ready_for_review",
            },
        )

    return _handle


def register_episode_self_improvement_actions(registry: ActionRegistry) -> None:
    for spec in (
        ActionSpec(
            action_id=EPISODE_SELF_IMPROVEMENT_LOAD_CONTEXT_ACTION_ID,
            description="Load critique-driven workflow improvement context and benchmark evidence.",
            handler=_build_load_context_handler(),
        ),
        ActionSpec(
            action_id=EPISODE_SELF_IMPROVEMENT_SUBMIT_PROPOSAL_ACTION_ID,
            description="Submit a represented workflow improvement proposal and prepare promotion follow-up.",
            handler=_build_submit_proposal_handler(),
        ),
        ActionSpec(
            action_id=EPISODE_SELF_IMPROVEMENT_LOAD_PROMOTION_CONTEXT_ACTION_ID,
            description="Load proposal, benchmark, and critique context for represented promotion evaluation.",
            handler=_build_load_promotion_context_handler(),
        ),
        ActionSpec(
            action_id=EPISODE_SELF_IMPROVEMENT_RECORD_PROMOTION_ACTION_ID,
            description="Persist a represented workflow promotion recommendation onto the proposal and critique memory.",
            handler=_build_record_promotion_handler(),
        ),
    ):
        registry.register_if_absent(spec)


__all__ = ["register_episode_self_improvement_actions"]
