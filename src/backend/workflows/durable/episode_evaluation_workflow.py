"""Durable action handlers for actor/critic episode evaluation."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping, Sequence

from ...services.episode_critic_evidence_service import (
    build_episode_critic_evidence_bundle,
)
from ...services.episode_evaluator_contract_service import (
    build_episode_evaluator_contract,
)
from ...services.episode_critique_memory_service import (
    upsert_episode_critique_memory_from_episode_assessment,
)
from ...services.episode_critique_routing_service import (
    route_episode_critique_memory,
)
from ...services.episode_self_improvement_service import (
    launch_episode_self_improvement_workflows,
)
from ...services.episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_BUILD_EVIDENCE_ACTION_ID,
    EPISODE_EVALUATION_PERSIST_MEMORY_ACTION_ID,
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


def _normalise_string_list(value: Any, *, limit: int = 40) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item)
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


def _normalise_grounded_helpfulness_assessment(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}

    status = _clean_text(value.get("status"))
    summary = _clean_text(value.get("summary"))
    confidence = value.get("confidence")
    counterfactual = _clean_text(value.get("counterfactual_recommended_action"))
    reason_codes = _normalise_string_list(value.get("reason_codes"), limit=20)
    evidence_receipt_ids = _normalise_string_list(
        value.get("evidence_receipt_ids"),
        limit=20,
    )

    if not any(
        (
            status,
            summary,
            confidence is not None,
            counterfactual,
            reason_codes,
            evidence_receipt_ids,
        )
    ):
        return {}

    normalised: dict[str, Any] = {"axis_id": "grounded_helpfulness"}
    if status:
        normalised["status"] = status
    if summary:
        normalised["summary"] = summary
    if confidence is not None:
        normalised["confidence"] = confidence
    if reason_codes:
        normalised["reason_codes"] = reason_codes
    if evidence_receipt_ids:
        normalised["evidence_receipt_ids"] = evidence_receipt_ids
    if counterfactual:
        normalised["counterfactual_recommended_action"] = counterfactual
    return normalised


def _merge_grounded_helpfulness_assessment(
    assessment: Mapping[str, Any],
    grounded_helpfulness_assessment: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(assessment)
    axis_payload = _normalise_grounded_helpfulness_assessment(
        grounded_helpfulness_assessment
    )
    if not axis_payload:
        return merged

    existing_contract = _mapping_or_empty(merged.get("evaluator_contract"))
    raw_axes = existing_contract.get("axes")
    axes = [
        dict(item)
        for item in raw_axes
        if isinstance(item, Mapping)
        and _clean_text(item.get("axis_id")) != "grounded_helpfulness"
    ] if isinstance(raw_axes, Sequence) and not isinstance(raw_axes, (str, bytes, bytearray)) else []
    axes.append(axis_payload)

    existing_contract["axes"] = axes
    merged["evaluator_contract"] = existing_contract
    merged["grounded_helpfulness_assessment"] = axis_payload
    return merged


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


def _hash_payload(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def _build_root_causes_from_gaps(
    capability_gaps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    root_causes: list[dict[str, Any]] = []
    for gap in capability_gaps:
        gap_id = _clean_text(gap.get("gap_id"))
        if not gap_id:
            continue
        root_causes.append(
            {
                "cause_id": gap_id,
                "severity": "high" if bool(gap.get("required")) else "medium",
                "rationale": _clean_text(gap.get("description"))
                or "Episode evidence bundle capability gap.",
            }
        )
    return root_causes


def _build_fallback_assessment(bundle: Mapping[str, Any]) -> dict[str, Any]:
    capability_gaps = [
        item
        for item in (bundle.get("capability_gaps") or [])
        if isinstance(item, Mapping)
    ]
    gap_codes = _normalise_string_list(bundle.get("fail_closed_reason_codes"), limit=40)
    locator = _mapping_or_empty(bundle.get("episode_locator"))
    workflow_id = _clean_text(locator.get("workflow_id"))
    format_over_content_diagnostic = _mapping_or_empty(
        bundle.get("format_over_content_diagnostic")
    )
    recommendations = [
        "Inspect the retained episode evidence receipts before making a stronger judgement."
    ]
    if gap_codes:
        recommendations.append(
            "Close the capability gaps so the critic can evaluate the episode authoritatively."
        )
    elif _clean_text(format_over_content_diagnostic.get("status")) == "suspected":
        recommendations.append(
            "Review whether the selected model and output contract over-favoured tidy structure over the most useful content."
        )
    if workflow_id:
        recommendations.append(
            f"Review the reusable behaviour surfaces around {workflow_id} before applying a repair."
        )
    evidence_receipt_ids = [
        value
        for value in (
            (
                f"episode_bundle_receipt:{_clean_text(_mapping_or_empty(bundle.get('bundle_receipt')).get('sha256'))}"
                if _clean_text(_mapping_or_empty(bundle.get("bundle_receipt")).get("sha256"))
                else None
            ),
            (
                f"episode_request:{_clean_text(locator.get('request_id'))}"
                if _clean_text(locator.get("request_id"))
                else None
            ),
        )
        if value
    ]
    evaluator_contract = build_episode_evaluator_contract(
        legacy_verdict="inconclusive",
        legacy_confidence=0.0,
        legacy_unresolved_check_count=len(gap_codes),
        summary="Episode evidence was not complete enough for a reliable critic judgement.",
        format_over_content_diagnostic=format_over_content_diagnostic,
        evidence_receipt_ids=evidence_receipt_ids,
        source_memory_ids=[],
        subject_workflow_ids=[workflow_id] if workflow_id else [],
        subject_tool_names=[],
        subject_concept_ids=[],
        measured_at_utc=None,
    )
    result = {
        "verdict": _clean_text(evaluator_contract.get("verdict")) or "inconclusive",
        "confidence": evaluator_contract.get("confidence"),
        "unresolved_check_count": evaluator_contract.get("unresolved_check_count"),
        "summary": (
            "Episode evidence was not complete enough for a reliable critic judgement."
        ),
        "maintenance_follow_up_recommended": bool(gap_codes),
        "maintenance_follow_up_reason": (
            "episode_bundle_fail_closed" if gap_codes else None
        ),
        "recommendations": recommendations[:6],
        "root_causes": _build_root_causes_from_gaps(capability_gaps),
        "improvement_suggestions": [],
        "evaluator_contract": evaluator_contract,
    }
    if format_over_content_diagnostic:
        result["format_over_content_diagnostic"] = format_over_content_diagnostic
    return result


def _build_evidence_bundle_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        request_id = _clean_text(
            request.inputs.get("request_id") or request.data.get("request_id")
        )
        instance_id = _clean_text(
            request.inputs.get("instance_id") or request.data.get("instance_id")
        )
        namespace = (
            _clean_text(request.inputs.get("namespace"))
            or _clean_text(request.data.get("namespace"))
            or _clean_text(getattr(request.environment, "user_namespace", None))
        )
        if not request_id and not instance_id:
            return WorkflowActionResult(
                status="failed",
                error="episode_evaluation_subject_missing",
            )

        bundle = build_episode_critic_evidence_bundle(
            request_id=request_id,
            instance_id=instance_id,
            namespace=namespace,
        )
        if not bool(bundle.get("success")):
            return WorkflowActionResult(
                status="failed",
                error=_clean_text(bundle.get("error"))
                or _clean_text(bundle.get("error_code"))
                or "episode_critic_bundle_failed",
                outputs={"episode_evidence_bundle": dict(bundle)},
            )

        locator = _mapping_or_empty(bundle.get("episode_locator"))
        ready_for_critic = bool(bundle.get("ready_for_critic")) and not bool(
            bundle.get("fail_closed")
        )
        default_apply_repairs = _coerce_bool(
            request.inputs.get("maintenance_apply_repairs_default")
            or request.data.get("maintenance_apply_repairs_default")
            or os.getenv("VON_WORKFLOW_INTROSPECTION_AUTO_APPLY", "1"),
            default=True,
        )

        outputs = {
            "episode_evidence_bundle": dict(bundle),
            "episode_evidence_bundle_receipt": _mapping_or_empty(
                bundle.get("bundle_receipt")
            ),
            "episode_locator": locator,
            "source_resolution": _mapping_or_empty(bundle.get("source_resolution")),
            "expected_context": bundle.get("expected_context"),
            "observed_evidence": _mapping_or_empty(bundle.get("observed_evidence")),
            "capability_gaps": list(bundle.get("capability_gaps") or []),
            "format_over_content_diagnostic": _mapping_or_empty(
                bundle.get("format_over_content_diagnostic")
            ),
            "fail_closed_reason_codes": list(bundle.get("fail_closed_reason_codes") or []),
            "selected_workflow_id": _clean_text(locator.get("workflow_id")),
            "request_id": _clean_text(locator.get("request_id")) or request_id,
            "instance_id": _clean_text(locator.get("instance_id")) or instance_id,
            "session_id": _clean_text(locator.get("session_id")),
            "user_id": _clean_text(locator.get("user_id")),
            "org_id": _clean_text(request.inputs.get("org_id") or request.data.get("org_id")),
            "namespace": _clean_text(locator.get("namespace")) or namespace,
            "maintenance_apply_repairs_default": default_apply_repairs,
            "episode_critic_fallback_assessment": _build_fallback_assessment(bundle),
            "result": ready_for_critic,
        }
        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _resolve_critic_assessment(request: WorkflowActionRequest) -> dict[str, Any]:
    grounded_helpfulness_assessment = (
        request.data.get("grounded_helpfulness_assessment")
        if isinstance(request.data.get("grounded_helpfulness_assessment"), Mapping)
        else None
    )
    for key in ("critic_assessment", "episode_critic_fallback_assessment"):
        value = request.data.get(key)
        if isinstance(value, Mapping):
            return _merge_grounded_helpfulness_assessment(
                value,
                grounded_helpfulness_assessment,
            )
    if grounded_helpfulness_assessment is not None:
        return _merge_grounded_helpfulness_assessment({}, grounded_helpfulness_assessment)
    return {}


def _build_maintenance_launch_payload(
    *,
    bundle: Mapping[str, Any],
    assessment: Mapping[str, Any],
    memory_id: str,
    request: WorkflowActionRequest,
) -> dict[str, Any]:
    locator = _mapping_or_empty(bundle.get("episode_locator"))
    summary = _clean_text(assessment.get("summary")) or "Episode critic follow-up"
    workflow_id = _clean_text(locator.get("workflow_id"))
    request_id = _clean_text(locator.get("request_id"))
    session_id = _clean_text(locator.get("session_id"))
    incident_text_parts = [summary]
    if workflow_id:
        incident_text_parts.append(f"workflow: {workflow_id}")
    reason = _clean_text(assessment.get("maintenance_follow_up_reason"))
    if reason:
        incident_text_parts.append(f"reason: {reason}")
    incident_text = " | ".join(incident_text_parts)
    maintenance_inputs = {
        "namespace": _clean_text(locator.get("namespace"))
        or _clean_text(getattr(request.environment, "user_namespace", None)),
        "request_id": request_id,
        "session_id": session_id,
        "selected_workflow_id": workflow_id,
        "incident_text": incident_text,
        "apply_repairs": bool(request.data.get("maintenance_apply_repairs_default")),
        "dry_run": False,
        "episode_critique_memory_id": memory_id,
        "episode_evaluation_depth": request.data.get("episode_evaluation_depth"),
    }
    event_id = memory_id.replace("#V#", "")
    return {
        "inputs": maintenance_inputs,
        "source_event_id": event_id,
        "event_idempotency_key": (
            "episode_critic.maintenance_follow_up:"
            f"{_hash_payload({'memory_id': memory_id, 'assessment': assessment})}"
        ),
    }


def _build_persist_memory_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        bundle = _mapping_or_empty(request.data.get("episode_evidence_bundle"))
        if not bundle:
            return WorkflowActionResult(
                status="failed",
                error="episode_evidence_bundle_missing",
            )
        assessment = _resolve_critic_assessment(request)
        if not assessment:
            return WorkflowActionResult(
                status="failed",
                error="critic_assessment_missing",
            )

        outcome = upsert_episode_critique_memory_from_episode_assessment(
            evidence_bundle=bundle,
            assessment=assessment,
            namespace=_clean_text(request.data.get("namespace")),
            user_id=_clean_text(request.data.get("user_id")),
            org_id=_clean_text(request.data.get("org_id")),
        )
        if not bool(outcome.get("success")):
            return WorkflowActionResult(
                status="failed",
                error=_clean_text(outcome.get("reason"))
                or "episode_critique_memory_persist_failed",
                outputs={"episode_critique_memory_upsert": dict(outcome)},
            )

        memory_id = _clean_text(outcome.get("memory_id"))
        remediation_routing = route_episode_critique_memory(
            memory_id=memory_id,
            memory_state=_mapping_or_empty(outcome.get("state")),
            actor_concept_id=_clean_text(request.data.get("user_id")),
        )
        maintenance_follow_up_requested = bool(
            assessment.get("maintenance_follow_up_recommended")
        )
        self_improvement_result = (
            launch_episode_self_improvement_workflows(
                memory_id=memory_id,
                memory_state=_mapping_or_empty(outcome.get("state")),
                user_id=_clean_text(request.data.get("user_id")),
                org_id=_clean_text(request.data.get("org_id")),
                namespace=_clean_text(request.data.get("namespace")),
                current_depth=int(request.data.get("episode_evaluation_depth") or 0),
            )
            if memory_id
            else {"success": False, "launches": []}
        )
        self_improvement_launches = list(self_improvement_result.get("launches") or [])
        maintenance_payload = (
            _build_maintenance_launch_payload(
                bundle=bundle,
                assessment=assessment,
                memory_id=memory_id or "",
                request=request,
            )
            if maintenance_follow_up_requested and memory_id
            else {}
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "episode_critique_memory_upsert": dict(outcome),
                "episode_critique_memory_id": memory_id,
                "improvement_suggestions": list(
                    (_mapping_or_empty(outcome.get("state"))).get(
                        "improvement_suggestions"
                    )
                    or []
                ),
                "improvement_suggestion_count": len(
                    list(
                        (_mapping_or_empty(outcome.get("state"))).get(
                            "improvement_suggestions"
                        )
                        or []
                    )
                ),
                "improvement_suggestion_categories": _normalise_string_list(
                    [
                        _mapping_or_empty(item).get("category")
                        for item in (
                            (_mapping_or_empty(outcome.get("state"))).get(
                                "improvement_suggestions"
                            )
                            or []
                        )
                    ],
                    limit=20,
                ),
                "self_improvement": dict(self_improvement_result),
                "self_improvement_launches": self_improvement_launches,
                "self_improvement_workflow_count": len(
                    [item for item in self_improvement_launches if item.get("success")]
                ),
                "self_improvement_target_workflow_ids": _normalise_string_list(
                    [
                        _mapping_or_empty(item).get("target_workflow_id")
                        for item in self_improvement_launches
                    ],
                    limit=20,
                ),
                "remediation_routing": remediation_routing,
                "remediation_routing_decision": _clean_text(
                    remediation_routing.get("decision")
                ),
                "remediation_routing_reason_codes": _normalise_string_list(
                    remediation_routing.get("reason_codes"),
                    limit=20,
                ),
                "remediation_routing_fingerprint": _clean_text(
                    remediation_routing.get("routing_fingerprint")
                ),
                "remediation_repeat_count": remediation_routing.get("repeat_count"),
                "remediation_task_id": _clean_text(
                    remediation_routing.get("remediation_task_id")
                ),
                "remediation_task_ids": _normalise_string_list(
                    remediation_routing.get("remediation_task_ids"),
                    limit=40,
                ),
                "remediation_issue_key": _clean_text(
                    remediation_routing.get("remediation_issue_key")
                ),
                "remediation_issue_keys": _normalise_string_list(
                    remediation_routing.get("remediation_issue_keys"),
                    limit=40,
                ),
                "remediation_task_action": _clean_text(
                    remediation_routing.get("task_action")
                ),
                "remediation_jira_action": _clean_text(
                    remediation_routing.get("jira_action")
                ),
                "maintenance_launch_inputs": maintenance_payload.get("inputs"),
                "maintenance_launch_source_event_id": maintenance_payload.get(
                    "source_event_id"
                ),
                "maintenance_launch_event_idempotency_key": maintenance_payload.get(
                    "event_idempotency_key"
                ),
                "maintenance_follow_up_requested": maintenance_follow_up_requested,
                "maintenance_follow_up_reason": _clean_text(
                    assessment.get("maintenance_follow_up_reason")
                ),
                "result": maintenance_follow_up_requested,
            },
        )

    return _handle


def register_episode_evaluation_actions(registry: ActionRegistry) -> None:
    for spec in (
        ActionSpec(
            action_id=EPISODE_EVALUATION_BUILD_EVIDENCE_ACTION_ID,
            description="Build the authoritative episode-critic evidence bundle.",
            handler=_build_evidence_bundle_handler(),
        ),
        ActionSpec(
            action_id=EPISODE_EVALUATION_PERSIST_MEMORY_ACTION_ID,
            description=(
                "Persist an episode-critic judgement and derive any maintenance "
                "follow-up launch payload."
            ),
            handler=_build_persist_memory_handler(),
        ),
    ):
        registry.register_if_absent(spec)


__all__ = ["register_episode_evaluation_actions"]
