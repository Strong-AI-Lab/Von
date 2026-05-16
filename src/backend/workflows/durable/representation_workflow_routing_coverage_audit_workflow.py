"""Support actions for representation-workflow routing coverage audits."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping, Sequence

from ...services.concept_service import ConceptNotFoundError, get_concept_by_concept_id
from ...services.episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_PERSIST_MEMORY_ACTION_ID,
)
from ...services.representation_workflow_routing_coverage_audit_contracts import (
    REPRESENTATION_ROUTING_AUDIT_BUILD_EPISODE_EVIDENCE_ACTION_ID,
    REPRESENTATION_ROUTING_AUDIT_COLLECT_EVIDENCE_ACTION_ID,
    REPRESENTATION_ROUTING_AUDIT_FINALISE_ACTION_ID,
    REPRESENTATION_ROUTING_AUDIT_LOAD_PROFILE_ACTION_ID,
    REPRESENTATION_ROUTING_AUDIT_REPORT_SCHEMA_VERSION,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
)
from ...services.representation_workflow_routing_coverage_audit_vontology_service import (
    load_representation_workflow_routing_coverage_audit_profile,
)
from ...services.text_value_service import get_texts_for_concept
from ...services.workflow_discovery_service import discover_workflows
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WORKFLOW_STEP_EXECUTION_MODE_LLM,
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..vontology_loader import (
    build_workflow_process_graph,
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
    resolve_workflow_description,
)
from ..workflow_registry import WorkflowRegistration

_ROUTING_PROFILE_PREDICATE = "#V#hasWorkflowRoutingProfileJson"
_DISCOVERY_EXEMPLARS_PREDICATE = "#V#hasWorkflowDiscoveryExemplarsJson"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _normalise_strings(value: Any, *, limit: int = 100) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _safe_int(value: Any, *, default: int = 0, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _hash_payload(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _get_concept_or_none(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    except Exception:
        return None
    return concept if isinstance(concept, Mapping) else None


def _first_text_relation_json(
    concept_id: str, predicate: str
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        rows = get_texts_for_concept(concept_id, predicate=predicate, limit=4)
    except Exception as exc:
        return None, f"text_relation_fetch_failed:{type(exc).__name__}"
    for row in rows:
        text = _clean_text(_mapping_or_empty(row).get("text"))
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None, "json_invalid"
        return payload if isinstance(payload, dict) else None, None
    return None, "missing"


def _rank_map(matches: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for index, item in enumerate(matches, start=1):
        workflow_id = _clean_text(item.get("concept_id"))
        if workflow_id and workflow_id not in ranks:
            ranks[workflow_id] = index
    return ranks


def _case_hit_summary(
    *,
    discovery_payload: Mapping[str, Any],
    expected_workflow_ids: Sequence[str],
    acceptable_workflow_ids: Sequence[str],
) -> dict[str, Any]:
    candidates = [
        _mapping_or_empty(item)
        for item in discovery_payload.get("candidates") or []
        if isinstance(item, Mapping)
    ]
    routing_matches = [
        _mapping_or_empty(item)
        for item in discovery_payload.get("routing_matches") or []
        if isinstance(item, Mapping)
    ]
    candidate_ranks = _rank_map(candidates)
    routing_ranks = _rank_map(routing_matches)
    expected_ids = _normalise_strings(expected_workflow_ids)
    acceptable_ids = _normalise_strings(acceptable_workflow_ids)
    expected_or_acceptable = list(dict.fromkeys([*expected_ids, *acceptable_ids]))

    def _ranks(ids: Sequence[str], rank_by_id: Mapping[str, int]) -> dict[str, int]:
        return {
            workflow_id: int(rank_by_id[workflow_id])
            for workflow_id in ids
            if workflow_id in rank_by_id
        }

    expected_candidate_ranks = _ranks(expected_ids, candidate_ranks)
    expected_routing_ranks = _ranks(expected_ids, routing_ranks)
    acceptable_candidate_ranks = _ranks(expected_or_acceptable, candidate_ranks)
    acceptable_routing_ranks = _ranks(expected_or_acceptable, routing_ranks)
    top_candidate_id = (
        _clean_text(candidates[0].get("concept_id")) if candidates else None
    )
    top_routing_id = (
        _clean_text(routing_matches[0].get("concept_id")) if routing_matches else None
    )
    return {
        "candidate_ids": [
            workflow_id
            for workflow_id in (
                _clean_text(item.get("concept_id")) for item in candidates
            )
            if workflow_id
        ],
        "routing_match_ids": [
            workflow_id
            for workflow_id in (
                _clean_text(item.get("concept_id")) for item in routing_matches
            )
            if workflow_id
        ],
        "expected_candidate_ranks": expected_candidate_ranks,
        "expected_routing_ranks": expected_routing_ranks,
        "acceptable_candidate_ranks": acceptable_candidate_ranks,
        "acceptable_routing_ranks": acceptable_routing_ranks,
        "expected_candidate_present": bool(expected_candidate_ranks),
        "expected_routing_present": bool(expected_routing_ranks),
        "expected_top_ranked": top_routing_id in expected_ids
        or (not top_routing_id and top_candidate_id in expected_ids),
        "expected_or_acceptable_top_ranked": top_routing_id in expected_or_acceptable
        or (not top_routing_id and top_candidate_id in expected_or_acceptable),
        "top_candidate_id": top_candidate_id,
        "top_routing_match_id": top_routing_id,
    }


def _collect_case_report(
    *,
    raw_case: Mapping[str, Any],
    audit_policy: Mapping[str, Any],
) -> dict[str, Any]:
    case = dict(raw_case)
    query = _clean_text(case.get("query")) or ""
    expected_workflow_ids = _normalise_strings(case.get("expected_workflow_ids"))
    acceptable_workflow_ids = _normalise_strings(case.get("acceptable_workflow_ids"))
    try:
        discovery_result = discover_workflows(
            query,
            relevance_threshold=_safe_float(
                audit_policy.get("discovery_relevance_threshold"),
                default=0.05,
            ),
            max_results=_safe_int(
                audit_policy.get("max_discovery_results"),
                default=8,
                minimum=1,
            ),
            timeout_seconds=_safe_float(
                audit_policy.get("discovery_timeout_seconds"),
                default=8.0,
            ),
            allow_non_executable=bool(audit_policy.get("allow_non_executable")),
        )
        discovery_payload = (
            discovery_result.to_dict()
            if hasattr(discovery_result, "to_dict")
            else _mapping_or_empty(discovery_result)
        )
        discovery_error = None
    except Exception as exc:
        discovery_payload = {
            "requested_query": query,
            "candidates": [],
            "routing_matches": [],
            "errors": [f"{type(exc).__name__}:{exc}"],
            "match_absence_reason": "discovery_exception",
        }
        discovery_error = f"workflow_discovery_failed:{type(exc).__name__}"

    hit_summary = _case_hit_summary(
        discovery_payload=discovery_payload,
        expected_workflow_ids=expected_workflow_ids,
        acceptable_workflow_ids=acceptable_workflow_ids,
    )
    return {
        "case_id": _clean_text(case.get("case_id")),
        "task_class": _clean_text(case.get("task_class")),
        "priority": _clean_text(case.get("priority")) or "medium",
        "query": query,
        "expected_workflow_ids": expected_workflow_ids,
        "acceptable_workflow_ids": acceptable_workflow_ids,
        "notes": _clean_text(case.get("notes")),
        "discovery": discovery_payload,
        "hit_summary": hit_summary,
        "status": (
            "expected_top_ranked"
            if bool(hit_summary.get("expected_top_ranked"))
            else (
                "expected_present"
                if bool(hit_summary.get("expected_routing_present"))
                or bool(hit_summary.get("expected_candidate_present"))
                else "expected_missing"
            )
        ),
        "discovery_error": discovery_error,
        "evidence_ref": f"case:{_clean_text(case.get('case_id')) or 'unknown'}",
    }


def _case_metric_rate(case_reports: Sequence[Mapping[str, Any]], key: str) -> float:
    if not case_reports:
        return 0.0
    count = 0
    for report in case_reports:
        hit_summary = _mapping_or_empty(report.get("hit_summary"))
        if bool(hit_summary.get(key)):
            count += 1
    return round(count / len(case_reports), 4)


def _workflow_ids_for_inventory(
    *,
    profile: Mapping[str, Any],
    case_reports: Sequence[Mapping[str, Any]],
) -> list[str]:
    inventory_policy = _mapping_or_empty(profile.get("inventory_policy"))
    max_workflows = _safe_int(
        inventory_policy.get("max_workflows"), default=100, minimum=1
    )
    workflow_ids: list[str] = []

    def _add_many(values: Any) -> None:
        for workflow_id in _normalise_strings(values, limit=500):
            if workflow_id not in workflow_ids:
                workflow_ids.append(workflow_id)

    mode = _clean_text(inventory_policy.get("mode")) or "expected_workflows"
    if mode == "all_vontology":
        try:
            _add_many(discover_workflow_ids())
        except Exception:
            pass
    else:
        _add_many(inventory_policy.get("workflow_ids"))

    for report in case_reports:
        _add_many(report.get("expected_workflow_ids"))
        _add_many(report.get("acceptable_workflow_ids"))
        hit_summary = _mapping_or_empty(report.get("hit_summary"))
        _add_many(hit_summary.get("candidate_ids"))
        _add_many(hit_summary.get("routing_match_ids"))
    return workflow_ids[:max_workflows]


def _workflow_inventory_row(workflow_id: str) -> dict[str, Any]:
    concept = _get_concept_or_none(workflow_id)
    graph: Mapping[str, Any] | None = None
    graph_warnings: list[str] = []
    try:
        raw_graph, warnings = build_workflow_process_graph(workflow_id)
        graph = raw_graph if isinstance(raw_graph, Mapping) else None
        graph_warnings = _normalise_strings(warnings, limit=20)
    except Exception as exc:
        graph_warnings = [f"graph_build_failed:{type(exc).__name__}"]

    try:
        definition = load_workflow_definition_from_vontology(workflow_id)
        definition_loadable = definition is not None
    except Exception:
        definition_loadable = False

    description_text = ""
    description_source = None
    try:
        description_text, description_source = resolve_workflow_description(
            workflow_id,
            workflow_source="vontology",
        )
    except Exception:
        description_text = ""
        description_source = "unavailable"

    routing_profile, routing_profile_error = _first_text_relation_json(
        workflow_id,
        _ROUTING_PROFILE_PREDICATE,
    )
    discovery_exemplars, discovery_exemplars_error = _first_text_relation_json(
        workflow_id,
        _DISCOVERY_EXEMPLARS_PREDICATE,
    )
    steps = graph.get("steps") if isinstance(graph, Mapping) else None
    return {
        "workflow_id": workflow_id,
        "concept_exists": isinstance(concept, Mapping),
        "name": _clean_text(_mapping_or_empty(concept).get("name")),
        "description": _clean_text(description_text),
        "description_source": description_source,
        "definition_loadable": definition_loadable,
        "graph_present": isinstance(graph, Mapping),
        "graph_step_count": len(steps) if isinstance(steps, list) else 0,
        "graph_warning_count": len(graph_warnings),
        "graph_warnings": graph_warnings,
        "routing_profile": routing_profile,
        "routing_profile_error": routing_profile_error,
        "discovery_exemplars": discovery_exemplars,
        "discovery_exemplars_error": discovery_exemplars_error,
    }


def _build_metrics(
    *,
    case_reports: Sequence[Mapping[str, Any]],
    inventory_rows: Sequence[Mapping[str, Any]],
    audit_policy: Mapping[str, Any],
) -> dict[str, Any]:
    case_count = len(case_reports)
    expected_candidate_presence_rate = _case_metric_rate(
        case_reports,
        "expected_candidate_present",
    )
    expected_routing_presence_rate = _case_metric_rate(
        case_reports,
        "expected_routing_present",
    )
    expected_top_rank_rate = _case_metric_rate(case_reports, "expected_top_ranked")
    expected_or_acceptable_top_rank_rate = _case_metric_rate(
        case_reports,
        "expected_or_acceptable_top_ranked",
    )
    minimum_presence = _safe_float(
        audit_policy.get("minimum_expected_presence_rate"),
        default=0.9,
    )
    minimum_top_rank = _safe_float(
        audit_policy.get("minimum_expected_top_rank_rate"),
        default=0.75,
    )
    return {
        "case_count": case_count,
        "high_priority_case_count": len(
            [item for item in case_reports if item.get("priority") == "high"]
        ),
        "expected_candidate_presence_rate": expected_candidate_presence_rate,
        "expected_routing_presence_rate": expected_routing_presence_rate,
        "expected_top_rank_rate": expected_top_rank_rate,
        "expected_or_acceptable_top_rank_rate": expected_or_acceptable_top_rank_rate,
        "budget_exhausted_case_count": len(
            [
                item
                for item in case_reports
                if bool(
                    _mapping_or_empty(item.get("discovery")).get("budget_exhausted")
                )
            ]
        ),
        "discovery_error_case_count": len(
            [
                item
                for item in case_reports
                if item.get("discovery_error")
                or _mapping_or_empty(item.get("discovery")).get("errors")
            ]
        ),
        "inventory_workflow_count": len(inventory_rows),
        "inventory_missing_concept_count": len(
            [item for item in inventory_rows if not bool(item.get("concept_exists"))]
        ),
        "inventory_missing_graph_count": len(
            [item for item in inventory_rows if not bool(item.get("graph_present"))]
        ),
        "objective_thresholds": {
            "minimum_expected_presence_rate": minimum_presence,
            "minimum_expected_top_rank_rate": minimum_top_rank,
        },
        "objective_status": (
            "passes_profile_thresholds"
            if expected_routing_presence_rate >= minimum_presence
            and expected_top_rank_rate >= minimum_top_rank
            else "below_profile_thresholds"
        ),
    }


def _handle_load_profile(request: WorkflowActionRequest) -> WorkflowActionResult:
    profile_concept_id = _clean_text(
        request.inputs.get("profile_concept_id")
        or request.data.get("profile_concept_id")
    )
    profile, diagnostics = load_representation_workflow_routing_coverage_audit_profile(
        workflow_id=(
            _clean_text(request.workflow_id)
            or REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID
        ),
        profile_concept_id=profile_concept_id,
    )
    if not isinstance(profile, Mapping):
        return WorkflowActionResult(
            status="failed",
            error=_clean_text(diagnostics.get("error_code"))
            or "representation_routing_audit_profile_unavailable",
            outputs={"representation_routing_audit_profile_diagnostics": diagnostics},
        )
    return WorkflowActionResult(
        outputs={
            "representation_routing_audit_profile": dict(profile),
            "representation_routing_audit_profile_diagnostics": diagnostics,
            "result": True,
        }
    )


def _handle_collect_evidence(request: WorkflowActionRequest) -> WorkflowActionResult:
    profile = _mapping_or_empty(
        request.data.get("representation_routing_audit_profile")
    )
    if not profile:
        return WorkflowActionResult(
            status="failed",
            error="representation_routing_audit_profile_missing",
        )

    audit_policy = _mapping_or_empty(profile.get("audit_policy"))
    max_cases = _safe_int(audit_policy.get("max_cases"), default=16, minimum=1)
    raw_cases = profile.get("audit_cases")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        return WorkflowActionResult(
            status="failed",
            error="representation_routing_audit_cases_missing",
        )
    case_reports = [
        _collect_case_report(raw_case=case, audit_policy=audit_policy)
        for case in raw_cases[:max_cases]
        if isinstance(case, Mapping)
    ]
    inventory_rows = [
        _workflow_inventory_row(workflow_id)
        for workflow_id in _workflow_ids_for_inventory(
            profile=profile,
            case_reports=case_reports,
        )
    ]
    report = {
        "schema_version": REPRESENTATION_ROUTING_AUDIT_REPORT_SCHEMA_VERSION,
        "audit_run_id": _clean_text(request.data.get("audit_run_id"))
        or f"representation_routing_audit:{_hash_payload({'profile': profile, 'cases': case_reports})[:16]}",
        "workflow_id": REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
        "generated_at_utc": _utcnow_iso(),
        "profile_id": _clean_text(profile.get("profile_id")),
        "profile_concept_id": _clean_text(profile.get("profile_concept_id")),
        "suggestion_authority": "workflow_llm_step",
        "python_authored_improvement_suggestions": False,
        "audit_policy": audit_policy,
        "evaluation_dimensions": list(profile.get("evaluation_dimensions") or []),
        "suggestion_policy": _mapping_or_empty(profile.get("suggestion_policy")),
        "cases": case_reports,
        "inventory": inventory_rows,
        "metrics": _build_metrics(
            case_reports=case_reports,
            inventory_rows=inventory_rows,
            audit_policy=audit_policy,
        ),
    }
    return WorkflowActionResult(
        outputs={
            "representation_routing_audit_report": report,
            "representation_routing_audit_metrics": report["metrics"],
            "result": True,
        }
    )


def _resolve_critic_assessment(context: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("critic_assessment", "representation_routing_audit_assessment"):
        value = context.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    validated = _mapping_or_empty(context.get("validated_json"))
    nested = validated.get("critic_assessment")
    if isinstance(nested, Mapping):
        return dict(nested)
    return dict(validated) if validated else {}


def _handle_build_episode_evidence(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    report = _mapping_or_empty(request.data.get("representation_routing_audit_report"))
    profile = _mapping_or_empty(
        request.data.get("representation_routing_audit_profile")
    )
    assessment = _resolve_critic_assessment(request.data)
    if not report:
        return WorkflowActionResult(
            status="failed",
            error="representation_routing_audit_report_missing",
        )
    if not assessment:
        return WorkflowActionResult(
            status="failed",
            error="critic_assessment_missing",
        )
    request_id = (
        _clean_text(request.data.get("request_id"))
        or _clean_text(request.inputs.get("request_id"))
        or _clean_text(report.get("audit_run_id"))
        or f"representation_routing_audit:{_hash_payload(report)[:16]}"
    )
    bundle = {
        "schema_version": "representation_routing_audit_episode_evidence_bundle.v1",
        "ready_for_critic": True,
        "fail_closed": False,
        "episode_locator": {
            "request_id": request_id,
            "episode_id": _clean_text(request.data.get("episode_id")),
            "instance_id": _clean_text(request.data.get("instance_id")),
            "workflow_id": REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
            "session_id": _clean_text(request.data.get("session_id")),
            "namespace": _clean_text(request.data.get("namespace"))
            or _clean_text(getattr(request.environment, "user_namespace", None)),
            "user_id": _clean_text(request.data.get("user_id"))
            or _clean_text(getattr(request.environment, "user_concept_id", None)),
            "org_id": _clean_text(request.data.get("org_id"))
            or _clean_text(getattr(request.environment, "org_concept_id", None)),
            "execution_trace_id": _clean_text(request.data.get("execution_trace_id")),
        },
        "expected_context": {
            "audit_profile_id": _clean_text(profile.get("profile_id")),
            "profile_concept_id": _clean_text(profile.get("profile_concept_id")),
            "suggestion_authority": "workflow_llm_step",
            "python_authored_improvement_suggestions": False,
            "self_improvement_contract": _mapping_or_empty(
                profile.get("self_improvement_contract")
            ),
        },
        "observed_evidence": {
            "representation_routing_audit_report": report,
            "representation_routing_audit_metrics": _mapping_or_empty(
                report.get("metrics")
            ),
            "workflow_definition_identity": {
                "workflow_id": REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
                "source": "vontology_workflow_seed",
                "prompt_concept_id": (
                    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
                ),
            },
        },
        "receipts": {
            "audit_report_sha256": _hash_payload(report),
            "critic_assessment_sha256": _hash_payload(assessment),
        },
    }
    bundle["bundle_receipt"] = {
        "sha256": _hash_payload(bundle),
        "created_at_utc": _utcnow_iso(),
    }
    return WorkflowActionResult(
        outputs={
            "episode_evidence_bundle": bundle,
            "critic_assessment": assessment,
            "episode_locator": bundle["episode_locator"],
            "namespace": bundle["episode_locator"].get("namespace"),
            "user_id": bundle["episode_locator"].get("user_id"),
            "org_id": bundle["episode_locator"].get("org_id"),
            "maintenance_apply_repairs_default": bool(
                request.data.get("maintenance_apply_repairs_default", True)
            ),
            "result": True,
        }
    )


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    report = _mapping_or_empty(request.data.get("representation_routing_audit_report"))
    assessment = _resolve_critic_assessment(request.data)
    result = {
        "schema_version": "representation_routing_audit_result.v1",
        "workflow_id": REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
        "completed_at_utc": _utcnow_iso(),
        "audit_run_id": _clean_text(report.get("audit_run_id")),
        "metrics": _mapping_or_empty(report.get("metrics")),
        "critic_assessment": assessment,
        "episode_critique_memory_id": _clean_text(
            request.data.get("episode_critique_memory_id")
        ),
        "improvement_suggestions": list(
            request.data.get("improvement_suggestions") or []
        ),
        "self_improvement": _mapping_or_empty(request.data.get("self_improvement")),
    }
    return WorkflowActionResult(
        outputs={
            "representation_routing_audit_result": result,
            "completed": True,
            "terminal_status": "completed",
            "result": result,
        }
    )


def build_representation_workflow_routing_coverage_audit_test_definition() -> (
    WorkflowDefinition
):
    return WorkflowDefinition(
        workflow_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
        initial_state="load_profile",
        termination_states=("completed", "failed"),
        purpose=(
            "Audit representation workflow routing coverage and store "
            "workflow-authored improvement suggestions through episode memory."
        ),
        states={
            "load_profile": WorkflowStateSpec(
                state_id="load_profile",
                actions=(
                    WorkflowActionInvocation(
                        action_id=REPRESENTATION_ROUTING_AUDIT_LOAD_PROFILE_ACTION_ID,
                        description="Load the represented audit profile.",
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                        reason="on_failure",
                    ),
                    WorkflowTransitionSpec(
                        to_state="collect_evidence",
                        condition=lambda ctx: bool(
                            ctx.get("representation_routing_audit_profile")
                        ),
                        reason="profile_loaded",
                    ),
                ),
            ),
            "collect_evidence": WorkflowStateSpec(
                state_id="collect_evidence",
                actions=(
                    WorkflowActionInvocation(
                        action_id=REPRESENTATION_ROUTING_AUDIT_COLLECT_EVIDENCE_ACTION_ID,
                        description="Collect generic discovery and inventory evidence.",
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                        reason="on_failure",
                    ),
                    WorkflowTransitionSpec(
                        to_state="evaluate_evidence",
                        condition=lambda ctx: bool(
                            ctx.get("representation_routing_audit_report")
                        ),
                        reason="evidence_ready",
                    ),
                ),
            ),
            "evaluate_evidence": WorkflowStateSpec(
                state_id="evaluate_evidence",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        execution_mode=WORKFLOW_STEP_EXECUTION_MODE_LLM,
                        prompt_contract={
                            "requested_prompt_concept_ids": [
                                REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_PROMPT_CONCEPT_ID
                            ]
                        },
                        llm_policy={
                            "policy_stage": "representation_routing_audit",
                            "tool_mode": "none",
                            "context_fields": [
                                {
                                    "context_key": "representation_routing_audit_profile",
                                    "label": "Audit profile",
                                },
                                {
                                    "context_key": "representation_routing_audit_report",
                                    "label": "Audit evidence report",
                                },
                                {
                                    "context_key": "workflow_success_guidance_history",
                                    "label": "Historical successful-run guidance",
                                },
                                {
                                    "context_key": "workflow_failure_avoidance_history",
                                    "label": "Historical failure-avoidance guidance",
                                },
                                {
                                    "context_key": "workflow_low_imposition_exploration_history",
                                    "label": "Low-imposition exploration guidance",
                                },
                            ],
                        },
                        validation_policy={"output_format": "json_value"},
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                        reason="on_failure",
                    ),
                    WorkflowTransitionSpec(
                        to_state="build_episode_evidence",
                        condition=lambda ctx: bool(ctx.get("critic_assessment")),
                        reason="critic_assessment_ready",
                    ),
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda _ctx: True,
                        reason="critic_assessment_missing",
                    ),
                ),
                metadata={
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "validated_json.critic_assessment",
                            "context_key": "critic_assessment",
                        }
                    ]
                },
            ),
            "build_episode_evidence": WorkflowStateSpec(
                state_id="build_episode_evidence",
                actions=(
                    WorkflowActionInvocation(
                        action_id=(
                            REPRESENTATION_ROUTING_AUDIT_BUILD_EPISODE_EVIDENCE_ACTION_ID
                        ),
                        description="Package the workflow-authored assessment as episode evidence.",
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                        reason="on_failure",
                    ),
                    WorkflowTransitionSpec(
                        to_state="persist_episode_memory",
                        condition=lambda ctx: bool(ctx.get("episode_evidence_bundle")),
                        reason="episode_evidence_ready",
                    ),
                ),
            ),
            "persist_episode_memory": WorkflowStateSpec(
                state_id="persist_episode_memory",
                actions=(
                    WorkflowActionInvocation(
                        action_id=EPISODE_EVALUATION_PERSIST_MEMORY_ACTION_ID,
                        description=(
                            "Persist the LLM-authored assessment and launch the "
                            "existing self-improvement workflows when recommended."
                        ),
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                        reason="on_failure",
                    ),
                    WorkflowTransitionSpec(
                        to_state="finalise",
                        condition=lambda _ctx: True,
                        reason="memory_persisted",
                    ),
                ),
            ),
            "finalise": WorkflowStateSpec(
                state_id="finalise",
                actions=(
                    WorkflowActionInvocation(
                        action_id=REPRESENTATION_ROUTING_AUDIT_FINALISE_ACTION_ID,
                        description="Return the audit result.",
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                        reason="on_failure",
                    ),
                    WorkflowTransitionSpec(
                        to_state="completed",
                        condition=lambda _ctx: True,
                        reason="finalised",
                    ),
                ),
            ),
            "completed": WorkflowStateSpec(state_id="completed", terminal=True),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
    )


def build_representation_workflow_routing_coverage_audit_test_registration() -> (
    WorkflowRegistration
):
    return WorkflowRegistration(
        workflow_id=REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
        definition=build_representation_workflow_routing_coverage_audit_test_definition(),
        purpose=(
            "Audit representation workflow routing coverage and preserve "
            "workflow-authored improvement suggestions through episode memory."
        ),
        source="built_in",
    )


def register_representation_workflow_routing_coverage_audit_actions(
    registry: ActionRegistry,
) -> None:
    for spec in (
        ActionSpec(
            action_id=REPRESENTATION_ROUTING_AUDIT_LOAD_PROFILE_ACTION_ID,
            description="Load represented profile for routing coverage audit.",
            handler=_handle_load_profile,
        ),
        ActionSpec(
            action_id=REPRESENTATION_ROUTING_AUDIT_COLLECT_EVIDENCE_ACTION_ID,
            description="Collect generic representation-routing audit evidence.",
            handler=_handle_collect_evidence,
        ),
        ActionSpec(
            action_id=REPRESENTATION_ROUTING_AUDIT_BUILD_EPISODE_EVIDENCE_ACTION_ID,
            description="Build episode evidence from a workflow-authored audit assessment.",
            handler=_handle_build_episode_evidence,
        ),
        ActionSpec(
            action_id=REPRESENTATION_ROUTING_AUDIT_FINALISE_ACTION_ID,
            description="Finalise representation-routing audit result.",
            handler=_handle_finalise,
        ),
    ):
        registry.register_if_absent(spec)


__all__ = [
    "build_representation_workflow_routing_coverage_audit_test_definition",
    "build_representation_workflow_routing_coverage_audit_test_registration",
    "register_representation_workflow_routing_coverage_audit_actions",
]
