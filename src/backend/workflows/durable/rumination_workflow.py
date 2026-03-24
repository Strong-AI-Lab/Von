"""Rumination orchestrator workflow — the "reflection" layer.

This workflow embodies JVNAUTOSCI-923: a **higher-level** process that
periodically inspects the ontology for quality gaps, prioritises them,
and dispatches enrichment sub-workflows to fill them.

Unlike the parameterised ``enrichment_workflow`` (which generates a single
predicate for a batch of concepts), the rumination orchestrator decides
*what kind* of enrichment is needed across multiple dimensions:

1. **Missing descriptions** — concepts without ``hasDescription``
2. **Missing considerations** — concepts without ``hasConsiderationsForUse``
3. **Missing relations** — individuals missing high-value suggested/salient
   relation predicates (with safe auto-apply from high-confidence hypotheses)
4. **Isolated concepts** — concepts with no outgoing relationships beyond
   type hierarchy

The orchestrator is budget-aware: it tracks how many LLM calls / items
have been processed and stops when the budget is exhausted, deferring
remaining work to the next scheduled run.

States:  assess → plan → dispatch → complete | failed

JIRA: JVNAUTOSCI-923
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
from typing import Any, Dict, List, Optional

from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..action_registry import (
    ActionSpec,
    ActionRegistry,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..workflow_registry import WorkflowRegistration

logger = logging.getLogger(__name__)

RUMINATION_WORKFLOW_ID = "#V#rumination_workflow"

# Default budget: max items to enrich per orchestrator run
DEFAULT_BUDGET = 30

RELATION_COMPLETION_PREDICATE = "__relation_completion__"
DEFAULT_RELATION_SCAN_LIMIT = 120
DEFAULT_RELATION_MAX_PREDICATES_PER_CONCEPT = 4
DEFAULT_RELATION_AUTO_APPLY_CONFIDENCE_THRESHOLD = 0.95
DEFAULT_RELATION_DETAIL_LIMIT = 80
DEFAULT_RELATION_QUESTION_LIMIT = 1
RELATION_AUTO_APPLY_POLICY_VERSION = "relation_auto_apply_policy.v1"
RELATION_PRIORITY_KEYWORDS: tuple[str, ...] = (
    "owner",
    "affiliation",
    "project",
    "deadline",
    "milestone",
    "paper",
    "supervis",
    "depend",
    "task",
    "member",
)
DEFAULT_RELATION_CLASS_THRESHOLDS: dict[str, float] = {
    "ownership": 0.98,
    "affiliation": 0.96,
    "project": 0.96,
    "deadline": 0.97,
    "paper_link": 0.95,
    "supervision": 0.97,
    "dependency": 0.98,
    "membership": 0.96,
    "generic": DEFAULT_RELATION_AUTO_APPLY_CONFIDENCE_THRESHOLD,
}
DEFAULT_RELATION_SOURCE_ADJUSTMENTS: dict[str, float] = {
    "human_validated": 0.08,
    "user_confirmed": 0.06,
    "explicit_user_input": 0.05,
    "llm_extraction": 0.0,
    "heuristic_inference": -0.05,
    "unknown": 0.0,
}

# Gap dimensions the orchestrator checks, in priority order.
# Each entry: (gap_name, predicate_to_check, enrichment_predicate, prompt_concept_id_or_none)
DEFAULT_GAP_DIMENSIONS: List[Dict[str, Any]] = [
    {
        "gap_name": "missing_relations",
        "predicate": RELATION_COMPLETION_PREDICATE,
        "priority": 1,
        "dispatch_mode": "relation_completion",
        "scan_limit": DEFAULT_RELATION_SCAN_LIMIT,
        "max_predicates_per_concept": DEFAULT_RELATION_MAX_PREDICATES_PER_CONCEPT,
        "auto_apply_confidence_threshold": DEFAULT_RELATION_AUTO_APPLY_CONFIDENCE_THRESHOLD,
    },
    {
        "gap_name": "missing_descriptions",
        "predicate": "hasDescription",
        "priority": 2,
        "prompt_concept_id": "#V#generate_concept_description_prompt",
    },
    {
        "gap_name": "missing_considerations",
        "predicate": "#V#has_considerations_for_use",
        "priority": 3,
        "prompt_concept_id": None,  # Uses default enrichment prompt
    },
]


def _resolve_relation_policy_input(
    *,
    task: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Resolve low-imposition acquisition policy from Vontology profiles.

    The profile surface is Vontology-backed so workflow-governed acquisition can
    evolve without hard-coding fresh heuristics for every new workflow.
    """

    from ...services.knowledge_acquisition_profile_vontology_service import (
        ensure_canonical_knowledge_acquisition_profiles,
        load_knowledge_acquisition_profile,
    )

    requested_profile_concept_id = (
        task.get("knowledge_acquisition_profile_concept_id")
        or ctx.get("knowledge_acquisition_profile_concept_id")
    )
    workflow_id = str(
        task.get("workflow_id")
        or ctx.get("workflow_id")
        or RUMINATION_WORKFLOW_ID
    ).strip() or RUMINATION_WORKFLOW_ID

    profile, diagnostics = load_knowledge_acquisition_profile(
        workflow_id=workflow_id,
        profile_concept_id=(
            str(requested_profile_concept_id).strip()
            if isinstance(requested_profile_concept_id, str)
            and str(requested_profile_concept_id).strip()
            else None
        ),
    )
    bootstrap_report: dict[str, Any] | None = None
    if profile is None:
        bootstrap_report = ensure_canonical_knowledge_acquisition_profiles(
            concept_ids=[requested_profile_concept_id]
            if isinstance(requested_profile_concept_id, str)
            and str(requested_profile_concept_id).strip()
            else None,
            link_workflow_ids=[workflow_id],
            provenance={
                "source": "rumination_workflow",
                "reason": "knowledge_acquisition_profile_bootstrap",
            },
            context={"workflow_id": workflow_id},
        )
        profile, diagnostics = load_knowledge_acquisition_profile(
            workflow_id=workflow_id,
            profile_concept_id=(
                str(requested_profile_concept_id).strip()
                if isinstance(requested_profile_concept_id, str)
                and str(requested_profile_concept_id).strip()
                else None
            ),
        )

    relation_auto_apply_policy = {}
    decision_policy = {}
    question_limit = DEFAULT_RELATION_QUESTION_LIMIT
    detail_limit = DEFAULT_RELATION_DETAIL_LIMIT
    profile_concept_id = None
    policy_error = None
    if isinstance(profile, dict):
        relation_auto_apply_policy = dict(
            profile.get("relation_auto_apply_policy") or {}
        )
        decision_policy = dict(profile.get("decision_policy") or {})
        question_limit = int(profile.get("question_limit") or question_limit)
        detail_limit = int(profile.get("detail_limit") or detail_limit)
        profile_concept_id = str(profile.get("profile_concept_id") or "").strip() or None
        if "policy_version" not in relation_auto_apply_policy:
            relation_auto_apply_policy["policy_version"] = str(
                relation_auto_apply_policy.get("policy_version")
                or profile.get("profile_id")
                or RELATION_AUTO_APPLY_POLICY_VERSION
            ).strip() or RELATION_AUTO_APPLY_POLICY_VERSION
    else:
        policy_error = "knowledge_acquisition_profile_unavailable"

    return {
        "workflow_id": workflow_id,
        "requested_profile_concept_id": requested_profile_concept_id,
        "profile_concept_id": profile_concept_id,
        "relation_auto_apply_policy": relation_auto_apply_policy,
        "decision_policy": decision_policy,
        "question_limit": max(1, question_limit),
        "detail_limit": max(1, detail_limit),
        "diagnostics": diagnostics,
        "bootstrap_report": bootstrap_report,
        "error": policy_error,
    }


def build_rumination_workflow_test_definition() -> WorkflowDefinition:
    """Build the rumination orchestrator workflow definition.

    Context keys consumed:
        - budget (optional, default 30): Max items to enrich per run
        - gap_dimensions (optional): Override the default gap dimensions list
        - kind_filter (optional): Restrict to a concept kind

    Context keys produced:
        - gap_assessment: Dict mapping gap_name → count of concepts with that gap
        - enrichment_plan: Ordered list of enrichment tasks to run
        - dispatched_tasks: Results from dispatched enrichments
        - relation_metrics: proposed/applied/deferred/confirmed relation counts
        - relation_changes: detailed proposed/applied relation assertions
        - relation_questions: deferred clarification questions
        - rumination_result: Final summary
    """

    assess = WorkflowStateSpec(
        state_id="assess",
        actions=(
            WorkflowActionInvocation(
                action_id="rumination.assess_gaps",
                description="Scan ontology for quality gaps across multiple dimensions.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="plan",
                condition=lambda ctx: bool(ctx.get("gap_assessment"))
                and any(v > 0 for v in (ctx.get("gap_assessment") or {}).values()),
                reason="gaps_found",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="no_gaps_found",
            ),
        ),
    )

    plan = WorkflowStateSpec(
        state_id="plan",
        actions=(
            WorkflowActionInvocation(
                action_id="rumination.plan_enrichment",
                description="Prioritise gaps and allocate budget across enrichment tasks.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="dispatch",
                condition=lambda ctx: bool(ctx.get("enrichment_plan")),
                reason="plan_ready",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="nothing_to_do",
            ),
        ),
    )

    dispatch = WorkflowStateSpec(
        state_id="dispatch",
        actions=(
            WorkflowActionInvocation(
                action_id="rumination.dispatch_enrichment",
                description="Execute enrichment for the next planned task.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="dispatch",
                condition=lambda ctx: ctx.get("has_more_tasks", False),
                reason="more_tasks",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="all_dispatched",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="rumination.finalise",
                description="Report rumination results.",
            ),
        ),
        terminal=True,
    )

    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=RUMINATION_WORKFLOW_ID,
        initial_state="assess",
        states={
            "assess": assess,
            "plan": plan,
            "dispatch": dispatch,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose="Proactive knowledge quality orchestrator — assesses gaps and dispatches enrichment.",
    )


# ---------------------------------------------------------------------------
# Action Handlers
# ---------------------------------------------------------------------------


def _count_concepts_missing_predicate(predicate: str, limit: int = 500) -> int:
    """Count how many concepts are missing a given text relation predicate.

    Returns the count (capped at limit for performance).
    """
    from ...db.repositories.text_value_repository import TextRelationsRepository
    from ...db.repositories.concepts_repository import ConceptsRepository

    existing_rels = TextRelationsRepository.find(
        {"predicate": predicate}, projection={"subject_concept_id": 1}
    )
    excluded_ids = {r["subject_concept_id"] for r in existing_rels}

    query: Dict[str, Any] = {}
    if excluded_ids:
        query["concept_id"] = {"$nin": list(excluded_ids)}

    return ConceptsRepository.count_documents(query)


def _count_isolated_concepts(limit: int = 500) -> int:
    """Count concepts with no outgoing relationships beyond type hierarchy."""
    from ...db.repositories.concepts_repository import ConceptsRepository

    # Find concepts where the only relationships are is_a_type_of / is_an_instance_of
    # This is an approximation — we count concepts with ≤ 2 relationship keys
    pipeline = [
        {
            "$project": {
                "concept_id": 1,
                "rel_keys": {"$objectToArray": {"$ifNull": ["$relationships", {}]}},
            }
        },
        {
            "$project": {
                "concept_id": 1,
                "non_hierarchy_keys": {
                    "$filter": {
                        "input": "$rel_keys",
                        "as": "r",
                        "cond": {
                            "$not": {
                                "$in": [
                                    "$$r.k",
                                    [
                                        "is_a_type_of",
                                        "is_an_instance_of",
                                        "has_subtype",
                                        "has_instance",
                                    ],
                                ]
                            }
                        },
                    },
                },
            }
        },
        {
            "$match": {
                "$or": [
                    {"non_hierarchy_keys": {"$size": 0}},
                    {"non_hierarchy_keys": {"$exists": False}},
                ]
            }
        },
        {"$count": "total"},
    ]

    try:
        coll = ConceptsRepository.collection()
        if coll is None:
            return 0
        result = list(coll.aggregate(pipeline))
        return result[0]["total"] if result else 0
    except Exception as e:
        logger.warning("[rumination] Failed to count isolated concepts: %s", e)
        return 0


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compute_relation_priority(predicate: str) -> tuple[int, list[str]]:
    if not isinstance(predicate, str):
        return 0, []
    lowered = predicate.lower()
    matched = [kw for kw in RELATION_PRIORITY_KEYWORDS if kw in lowered]
    return len(matched), matched


def _list_relation_gap_candidates(
    *,
    scan_limit: int,
    max_predicates_per_concept: int,
    candidate_concept_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan individual concepts and list missing relation opportunities.

    This intentionally uses bounded scans to keep rumination predictable.
    """
    from ...db.repositories.concepts_repository import ConceptsRepository
    from ...services.relation_elicitation_service import RelationElicitationService
    from ...vontology.utils_vontology import get_concept_display_name_with_names_fallback

    service = RelationElicitationService(llm_client=False)
    projection = {
        "concept_id": 1,
        "name": 1,
        "names": 1,
        "relationships": 1,
        "hypothesized_relations": 1,
        "uncertain_relationship_assertions": 1,
    }
    if candidate_concept_ids:
        concept_docs = ConceptsRepository.find(
            {"concept_id": {"$in": [cid for cid in candidate_concept_ids if isinstance(cid, str)]}},
            projection=projection,
            limit=scan_limit,
        )
    else:
        concept_docs = ConceptsRepository.find(
            {"relationships.is_an_instance_of": {"$exists": True, "$ne": []}},
            projection=projection,
            limit=scan_limit,
        )

    candidates: list[dict[str, Any]] = []
    for concept_doc in concept_docs:
        concept_id = concept_doc.get("concept_id")
        if not isinstance(concept_id, str) or not concept_id:
            continue

        missing_predicates = service.get_elicitation_opportunities(
            concept_id,
            include_reverse_subtypes=False,
            include_hypothesized=True,
        )
        if not missing_predicates:
            continue

        scored: list[tuple[str, int, list[str]]] = []
        for predicate in missing_predicates:
            score, matched = _compute_relation_priority(predicate)
            scored.append((predicate, score, matched))

        scored.sort(key=lambda item: (-item[1], item[0]))
        selected = scored[: max(1, max_predicates_per_concept)]
        selected_predicates = [item[0] for item in selected]

        matched_keywords: set[str] = set()
        priority_score = 0
        for _, score, matched in selected:
            priority_score += score
            matched_keywords.update(matched)

        try:
            concept_name = get_concept_display_name_with_names_fallback(concept_doc)
        except Exception:
            concept_name = concept_doc.get("name") or concept_id

        candidates.append(
            {
                "concept_id": concept_id,
                "concept_name": concept_name or concept_id,
                "missing_predicates": selected_predicates,
                "total_missing_predicates": len(missing_predicates),
                "priority_score": priority_score,
                "matched_priority_keywords": sorted(matched_keywords),
            }
        )

    # Highest-priority and highest-coverage concepts first.
    candidates.sort(
        key=lambda item: (
            -int(item.get("priority_score", 0)),
            -int(item.get("total_missing_predicates", 0)),
            str(item.get("concept_id", "")),
        )
    )
    return candidates


def _resolve_relation_auto_apply_candidate(
    *,
    instance_doc: dict[str, Any],
    predicate: str,
    confidence_threshold: float,
    policy_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve an auto-apply candidate and return eligibility diagnostics."""
    if not isinstance(predicate, str) or not predicate.startswith("#V#"):
        return {"status": "ineligible", "reason": "invalid_predicate"}

    relationships = instance_doc.get("relationships") or {}
    if isinstance(relationships, dict) and relationships.get(predicate):
        return {"status": "ineligible", "reason": "already_present"}

    from ...services.uncertain_relationship_service import (
        ACTIVE_UNCERTAIN_RELATIONSHIP_STATUSES,
        list_uncertain_relationship_assertions,
    )

    source_id = str(instance_doc.get("concept_id") or "").strip()
    canonical_assertions = list_uncertain_relationship_assertions(
        source_id=source_id,
        predicate=predicate,
        statuses=tuple(ACTIVE_UNCERTAIN_RELATIONSHIP_STATUSES),
        include_legacy=True,
        source_doc=instance_doc,
    )
    canonical_hypotheses: list[dict[str, Any]] = []
    for assertion in canonical_assertions:
        if not isinstance(assertion, dict):
            continue
        canonical_hypotheses.append(
            {
                "hypothesis_id": assertion.get("assertion_id"),
                "id": assertion.get("assertion_id"),
                "value": assertion.get("target"),
                "confidence_score": assertion.get("confidence_score"),
                "source": (assertion.get("provenance") or {}).get("source"),
                "source_interaction_id": (assertion.get("provenance") or {}).get(
                    "source_interaction_id"
                ),
                "evidence_count": assertion.get("evidence_count"),
            }
        )

    hypotheses_list: list[Any] = list(canonical_hypotheses)
    if not hypotheses_list:
        return {"status": "ineligible", "reason": "no_hypothesis"}

    policy = _resolve_relation_auto_apply_policy(
        predicate=predicate,
        confidence_threshold=confidence_threshold,
        policy_input=policy_input,
    )
    effective_threshold = float(policy["effective_threshold"])
    min_evidence_count = int(policy["min_evidence_count"])

    candidates: list[dict[str, Any]] = []
    last_failure_reason = "no_eligible_hypothesis"
    last_failure_details: dict[str, Any] = {}
    for idx, hypothesis in enumerate(hypotheses_list):
        if not isinstance(hypothesis, dict):
            last_failure_reason = "invalid_hypothesis_payload"
            continue
        target_id = hypothesis.get("value")
        if not isinstance(target_id, str) or not target_id.startswith("#V#"):
            last_failure_reason = "invalid_hypothesis_target"
            continue
        raw_confidence = _coerce_float(hypothesis.get("confidence_score"), default=0.0)
        source = str(hypothesis.get("source") or "unknown").strip().lower() or "unknown"
        evidence_count = _coerce_int(
            hypothesis.get("evidence_count"),
            default=len(hypothesis.get("evidence") or [])
            if isinstance(hypothesis.get("evidence"), list)
            else 1,
        )
        if evidence_count < min_evidence_count:
            last_failure_reason = "insufficient_evidence"
            last_failure_details = {
                "evidence_count": evidence_count,
                "required_min_evidence_count": min_evidence_count,
            }
            continue
        confidence = _calibrate_hypothesis_confidence(
            hypothesis=hypothesis,
            source=source,
            base_confidence=raw_confidence,
            policy=policy,
        )
        if confidence < effective_threshold:
            last_failure_reason = "below_confidence_threshold"
            last_failure_details = {
                "confidence_score": confidence,
                "raw_confidence_score": raw_confidence,
                "effective_threshold": effective_threshold,
            }
            continue
        candidates.append(
            {
                "target_id": target_id,
                "confidence_score": confidence,
                "raw_confidence_score": raw_confidence,
                "source": source,
                "hypothesis_id": _resolve_hypothesis_id(
                    hypothesis=hypothesis,
                    predicate=predicate,
                    target_id=target_id,
                    fallback_index=idx,
                ),
                "evidence_count": evidence_count,
            }
        )
    if not candidates:
        result = {
            "status": "ineligible",
            "reason": last_failure_reason,
            "effective_threshold": effective_threshold,
            "policy_version": policy["policy_version"],
        }
        if last_failure_details:
            result["reason_details"] = last_failure_details
        return result

    candidates.sort(key=lambda item: (-float(item["confidence_score"]), str(item["target_id"])))
    selected = candidates[0]
    target_id = str(selected["target_id"])

    from ...db.repositories.concepts_repository import ConceptsRepository

    target_exists = ConceptsRepository.find_one(
        {"concept_id": target_id}, projection={"concept_id": 1}
    )
    if not target_exists:
        return {
            "status": "ineligible",
            "reason": "target_not_found",
            "target_id": target_id,
            "effective_threshold": effective_threshold,
            "policy_version": policy["policy_version"],
        }

    return {
        "status": "eligible",
        "target_id": target_id,
        "confidence_score": float(selected["confidence_score"]),
        "raw_confidence_score": float(selected["raw_confidence_score"]),
        "source": str(selected["source"]),
        "hypothesis_id": str(selected["hypothesis_id"]),
        "evidence_count": int(selected["evidence_count"]),
        "required_min_evidence_count": min_evidence_count,
        "effective_threshold": effective_threshold,
        "policy_version": policy["policy_version"],
    }


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_hypothesis_id(
    *,
    hypothesis: dict[str, Any],
    predicate: str,
    target_id: str,
    fallback_index: int,
) -> str:
    raw = hypothesis.get("hypothesis_id") or hypothesis.get("id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    source_marker = str(
        hypothesis.get("source_interaction_id")
        or hypothesis.get("source")
        or "unknown_source"
    ).strip()
    digest_seed = f"{predicate}|{target_id}|{source_marker}|{fallback_index}"
    digest = hashlib.sha1(digest_seed.encode("utf-8")).hexdigest()[:16]
    return f"hyp_{digest}"


def _classify_relation_predicate(predicate: str) -> str:
    lowered = str(predicate or "").lower()
    if any(k in lowered for k in ("owner",)):
        return "ownership"
    if any(k in lowered for k in ("affiliation",)):
        return "affiliation"
    if any(k in lowered for k in ("project",)):
        return "project"
    if any(k in lowered for k in ("deadline", "milestone")):
        return "deadline"
    if any(k in lowered for k in ("paper", "author")):
        return "paper_link"
    if any(k in lowered for k in ("supervis",)):
        return "supervision"
    if any(k in lowered for k in ("depend",)):
        return "dependency"
    if any(k in lowered for k in ("member", "membership")):
        return "membership"
    return "generic"


def _resolve_relation_auto_apply_policy(
    *,
    predicate: str,
    confidence_threshold: float,
    policy_input: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve auto-apply policy from defaults plus optional context overrides."""
    policy_input = dict(policy_input or {})

    predicate_class = _classify_relation_predicate(predicate)
    class_thresholds = dict(DEFAULT_RELATION_CLASS_THRESHOLDS)
    class_thresholds_raw = policy_input.get("class_thresholds")
    if isinstance(class_thresholds_raw, dict):
        class_thresholds.update(class_thresholds_raw)
    predicate_thresholds_raw = policy_input.get("predicate_thresholds")
    predicate_thresholds = (
        predicate_thresholds_raw if isinstance(predicate_thresholds_raw, dict) else {}
    )
    default_threshold = _coerce_float(
        policy_input.get("default_threshold"),
        default=confidence_threshold,
    )
    class_threshold = _coerce_float(
        class_thresholds.get(predicate_class),
        default=default_threshold,
    )
    predicate_threshold = _coerce_float(
        predicate_thresholds.get(predicate),
        default=class_threshold,
    )

    min_evidence_count_default = _coerce_int(
        policy_input.get("min_evidence_count"),
        default=1,
    )
    min_evidence_by_predicate_raw = policy_input.get("min_evidence_by_predicate")
    min_evidence_by_predicate = (
        min_evidence_by_predicate_raw
        if isinstance(min_evidence_by_predicate_raw, dict)
        else {}
    )
    min_evidence_count = _coerce_int(
        min_evidence_by_predicate.get(predicate),
        default=min_evidence_count_default,
    )

    source_adjustments = dict(DEFAULT_RELATION_SOURCE_ADJUSTMENTS)
    source_adjustments_raw = policy_input.get("source_adjustments")
    if isinstance(source_adjustments_raw, dict):
        source_adjustments.update(source_adjustments_raw)

    policy_version = str(
        policy_input.get("policy_version")
        or RELATION_AUTO_APPLY_POLICY_VERSION
    ).strip() or RELATION_AUTO_APPLY_POLICY_VERSION

    return {
        "policy_version": policy_version,
        "predicate_class": predicate_class,
        "effective_threshold": predicate_threshold,
        "min_evidence_count": max(0, min_evidence_count),
        "source_adjustments": source_adjustments,
    }


def _calibrate_hypothesis_confidence(
    *,
    hypothesis: dict[str, Any],
    source: str,
    base_confidence: float,
    policy: dict[str, Any],
) -> float:
    source_adjustments_raw = policy.get("source_adjustments")
    source_adjustments = (
        source_adjustments_raw if isinstance(source_adjustments_raw, dict) else {}
    )
    source_key = source if source in source_adjustments else "unknown"
    adjustment = _coerce_float(source_adjustments.get(source_key), default=0.0)
    calibrated = base_confidence + adjustment
    return max(0.0, min(1.0, calibrated))


def _record_relation_auto_apply_audit(
    *,
    source_id: str,
    predicate: str,
    target_id: str,
    audit_payload: dict[str, Any],
) -> None:
    """Persist lightweight audit metadata for auto-applied relation assertions."""
    from ...db.repositories.concepts_repository import ConceptsRepository

    event = {
        "event_type": "relation_auto_apply",
        "applied_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_id": source_id,
        "predicate": predicate,
        "target_id": target_id,
        **dict(audit_payload or {}),
    }
    ConceptsRepository.update_one(
        {"concept_id": source_id},
        {"$push": {"relationship_auto_apply_audit": {"$each": [event], "$slice": -200}}},
    )


def _build_relation_completion_question(
    *,
    service: Any,
    instance_doc: dict[str, Any],
    predicate: str,
) -> str:
    concept_id = str(instance_doc.get("concept_id") or "")
    question = None
    try:
        question = service.generate_question_for_elicit(concept_id, predicate)
    except Exception:
        question = None
    if isinstance(question, str) and question.strip():
        return question.strip()

    from ...vontology.utils_vontology import get_concept_display_name_with_names_fallback

    try:
        concept_name = get_concept_display_name_with_names_fallback(instance_doc)
    except Exception:
        concept_name = instance_doc.get("name") or concept_id or "this concept"
    predicate_label = predicate.replace("#V#", "").replace("_", " ").strip()
    if not predicate_label:
        predicate_label = "relation"
    return f"What should the '{predicate_label}' relation be for {concept_name}?"


def _dispatch_relation_completion_task(
    *,
    task: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Process relation completion for one rumination plan task."""
    from ...db.repositories.concepts_repository import ConceptsRepository
    from ...services.relation_elicitation_service import RelationElicitationService
    from ...services.relationship_write_service import add_relationship

    dry_run = bool(ctx.get("dry_run", False))
    allocation = max(0, int(task.get("allocation", 0)))
    confidence_threshold = _coerce_float(
        task.get(
            "auto_apply_confidence_threshold",
            ctx.get(
                "relation_auto_apply_confidence_threshold",
                DEFAULT_RELATION_AUTO_APPLY_CONFIDENCE_THRESHOLD,
            ),
        ),
        default=DEFAULT_RELATION_AUTO_APPLY_CONFIDENCE_THRESHOLD,
    )
    relation_policy_context = _resolve_relation_policy_input(task=task, ctx=ctx)
    detail_limit = int(
        ctx.get(
            "relation_detail_limit",
            relation_policy_context.get("detail_limit", DEFAULT_RELATION_DETAIL_LIMIT),
        )
    )
    question_limit = int(
        ctx.get(
            "relation_question_limit",
            relation_policy_context.get(
                "question_limit", DEFAULT_RELATION_QUESTION_LIMIT
            ),
        )
    )
    relation_auto_apply_policy = dict(
        relation_policy_context.get("relation_auto_apply_policy") or {}
    )

    candidates = list(ctx.get("relation_gap_candidates") or [])
    selected_candidates = candidates[:allocation] if allocation > 0 else []
    service = RelationElicitationService(llm_client=False)

    proposed_relations = 0
    auto_applied_relations = 0
    would_apply_relations = 0
    deferred_relations = 0
    confirmed_relations = 0
    failed_relations = 0
    deferral_reason_counts: dict[str, int] = {}
    proposal_details: list[dict[str, Any]] = []
    deferred_questions: list[dict[str, Any]] = []

    if relation_policy_context.get("error"):
        return {
            "gap_name": task.get("gap_name"),
            "predicate": task.get("predicate"),
            "dispatch_mode": "relation_completion",
            "allocation": allocation,
            "dry_run": dry_run,
            "metrics": {
                "concepts_considered": 0,
                "proposed_relations": 0,
                "auto_applied_relations": 0,
                "would_apply_relations": 0,
                "deferred_relations": 0,
                "confirmed_relations": 0,
                "failed_relations": 1,
                "deferral_reason_counts": {},
            },
            "proposal_details": [],
            "deferred_questions": [],
            "policy_profile_concept_id": relation_policy_context.get(
                "profile_concept_id"
            ),
            "policy_resolution": relation_policy_context,
            "policy_error": relation_policy_context.get("error"),
        }

    for candidate in selected_candidates:
        concept_id = candidate.get("concept_id")
        if not isinstance(concept_id, str) or not concept_id:
            continue

        instance_doc = ConceptsRepository.find_one(
            {"concept_id": concept_id},
            projection={
                "concept_id": 1,
                "name": 1,
                "names": 1,
                "relationships": 1,
                "hypothesized_relations": 1,
                "uncertain_relationship_assertions": 1,
            },
        )
        if not instance_doc:
            continue

        missing_predicates = list(candidate.get("missing_predicates") or [])
        for predicate in missing_predicates:
            if not isinstance(predicate, str) or not predicate:
                continue
            proposed_relations += 1

            detail: dict[str, Any] = {
                "source_id": concept_id,
                "predicate": predicate,
                "dry_run": dry_run,
            }

            auto_candidate = _resolve_relation_auto_apply_candidate(
                instance_doc=instance_doc,
                predicate=predicate,
                confidence_threshold=confidence_threshold,
                policy_input=relation_auto_apply_policy,
            )
            if auto_candidate.get("status") == "eligible":
                detail["target_id"] = auto_candidate["target_id"]
                detail["confidence_score"] = auto_candidate["confidence_score"]
                detail["raw_confidence_score"] = auto_candidate["raw_confidence_score"]
                detail["candidate_source"] = auto_candidate["source"]
                detail["hypothesis_id"] = auto_candidate["hypothesis_id"]
                detail["policy_version"] = auto_candidate["policy_version"]
                detail["effective_threshold"] = auto_candidate["effective_threshold"]
                detail["evidence_count"] = auto_candidate["evidence_count"]
                detail["required_min_evidence_count"] = auto_candidate[
                    "required_min_evidence_count"
                ]

                if dry_run:
                    detail["action"] = "would_auto_apply"
                    would_apply_relations += 1
                else:
                    try:
                        result = add_relationship(
                            concept_id,
                            predicate,
                            auto_candidate["target_id"],
                        )
                        if bool(result.get("success")):
                            detail["action"] = "auto_applied"
                            auto_applied_relations += 1
                            confirmed_relations += 1
                            try:
                                _record_relation_auto_apply_audit(
                                    source_id=concept_id,
                                    predicate=predicate,
                                    target_id=auto_candidate["target_id"],
                                    audit_payload={
                                        "hypothesis_id": auto_candidate["hypothesis_id"],
                                        "confidence_score": auto_candidate["confidence_score"],
                                        "raw_confidence_score": auto_candidate[
                                            "raw_confidence_score"
                                        ],
                                        "policy_version": auto_candidate["policy_version"],
                                        "effective_threshold": auto_candidate[
                                            "effective_threshold"
                                        ],
                                        "candidate_source": auto_candidate["source"],
                                        "evidence_count": auto_candidate["evidence_count"],
                                    },
                                )
                            except Exception as audit_exc:
                                detail["audit_error"] = str(audit_exc)
                        else:
                            detail["action"] = "deferred_after_apply_failure"
                            detail["error"] = result.get("error")
                            detail["deferral_reason"] = "apply_write_failure"
                            deferred_relations += 1
                            failed_relations += 1
                            deferral_reason_counts["apply_write_failure"] = (
                                int(deferral_reason_counts.get("apply_write_failure", 0)) + 1
                            )
                    except Exception as exc:
                        detail["action"] = "deferred_after_apply_exception"
                        detail["error"] = str(exc)
                        detail["deferral_reason"] = "apply_write_exception"
                        deferred_relations += 1
                        failed_relations += 1
                        deferral_reason_counts["apply_write_exception"] = (
                            int(deferral_reason_counts.get("apply_write_exception", 0)) + 1
                        )
            else:
                deferral_reason = str(auto_candidate.get("reason") or "no_candidate")
                detail["action"] = "deferred_question"
                detail["deferral_reason"] = deferral_reason
                if auto_candidate.get("reason_details"):
                    detail["deferral_reason_details"] = dict(
                        auto_candidate.get("reason_details") or {}
                    )
                deferred_relations += 1
                deferral_reason_counts[deferral_reason] = (
                    int(deferral_reason_counts.get(deferral_reason, 0)) + 1
                )
                if len(deferred_questions) < question_limit:
                    deferred_questions.append(
                        {
                            "source_id": concept_id,
                            "predicate": predicate,
                            "question": _build_relation_completion_question(
                                service=service,
                                instance_doc=instance_doc,
                                predicate=predicate,
                            ),
                        }
                    )

            if len(proposal_details) < detail_limit:
                proposal_details.append(detail)

    relation_metrics = {
        "concepts_considered": len(selected_candidates),
        "proposed_relations": proposed_relations,
        "auto_applied_relations": auto_applied_relations,
        "would_apply_relations": would_apply_relations,
        "deferred_relations": deferred_relations,
        "confirmed_relations": confirmed_relations,
        "failed_relations": failed_relations,
        "deferral_reason_counts": deferral_reason_counts,
    }

    task_result = {
        "gap_name": task.get("gap_name"),
        "predicate": task.get("predicate"),
        "dispatch_mode": "relation_completion",
        "allocation": allocation,
        "dry_run": dry_run,
        "metrics": relation_metrics,
        "proposal_details": proposal_details,
        "deferred_questions": deferred_questions,
        "policy_profile_concept_id": relation_policy_context.get("profile_concept_id"),
        "policy_resolution": relation_policy_context,
    }
    return task_result


def _handle_assess_gaps(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Scan the ontology for quality gaps across multiple dimensions."""
    try:
        ctx = request.data
        dimensions = ctx.get("gap_dimensions") or DEFAULT_GAP_DIMENSIONS

        gap_assessment: Dict[str, int] = {}
        relation_gap_candidates: list[dict[str, Any]] = []
        relation_gap_summary: dict[str, Any] = {}

        for dim in dimensions:
            gap_name = dim["gap_name"]
            predicate = dim["predicate"]
            dispatch_mode = dim.get("dispatch_mode")
            if (
                dispatch_mode == "relation_completion"
                or predicate == RELATION_COMPLETION_PREDICATE
            ):
                try:
                    scan_limit = int(
                        dim.get(
                            "scan_limit",
                            ctx.get("relation_scan_limit", DEFAULT_RELATION_SCAN_LIMIT),
                        )
                    )
                    max_predicates_per_concept = int(
                        dim.get(
                            "max_predicates_per_concept",
                            ctx.get(
                                "relation_max_predicates_per_concept",
                                DEFAULT_RELATION_MAX_PREDICATES_PER_CONCEPT,
                            ),
                        )
                    )
                    candidate_concept_ids = dim.get("candidate_concept_ids")
                    if not isinstance(candidate_concept_ids, list):
                        candidate_concept_ids = ctx.get("relation_candidate_concept_ids")
                    if not isinstance(candidate_concept_ids, list):
                        candidate_concept_ids = None
                    relation_gap_candidates = _list_relation_gap_candidates(
                        scan_limit=max(1, scan_limit),
                        max_predicates_per_concept=max(1, max_predicates_per_concept),
                        candidate_concept_ids=candidate_concept_ids,
                    )
                    gap_assessment[gap_name] = len(relation_gap_candidates)
                    relation_gap_summary[gap_name] = {
                        "scan_limit": scan_limit,
                        "concepts_with_missing_relations": len(relation_gap_candidates),
                        "opportunity_count": sum(
                            len(item.get("missing_predicates") or [])
                            for item in relation_gap_candidates
                        ),
                    }
                    logger.info(
                        "[rumination] Gap '%s' (relation_completion): %d concepts",
                        gap_name,
                        gap_assessment[gap_name],
                    )
                except Exception as e:
                    logger.warning(
                        "[rumination] Failed to assess relation gap '%s': %s",
                        gap_name,
                        e,
                    )
                    gap_assessment[gap_name] = -1
                    relation_gap_summary[gap_name] = {"error": str(e)}
                continue
            try:
                count = _count_concepts_missing_predicate(predicate)
                gap_assessment[gap_name] = count
                logger.info(
                    "[rumination] Gap '%s' (predicate=%s): %d concepts",
                    gap_name,
                    predicate,
                    count,
                )
            except Exception as e:
                logger.warning(
                    "[rumination] Failed to assess gap '%s': %s", gap_name, e
                )
                gap_assessment[gap_name] = -1  # Error sentinel

        # Also check for isolated concepts
        try:
            isolated = _count_isolated_concepts()
            gap_assessment["isolated_concepts"] = isolated
            logger.info("[rumination] Isolated concepts: %d", isolated)
        except Exception as e:
            logger.warning("[rumination] Failed to count isolated concepts: %s", e)

        total_gaps = sum(v for v in gap_assessment.values() if v > 0)
        logger.info(
            "[rumination] Assessment complete: %d total gaps across %d dimensions",
            total_gaps,
            len(gap_assessment),
        )

        return WorkflowActionResult(
            outputs={
                "gap_assessment": gap_assessment,
                "gap_dimensions": dimensions,
                "relation_gap_candidates": relation_gap_candidates,
                "relation_gap_summary": relation_gap_summary,
            }
        )
    except Exception as e:
        logger.exception("[rumination] assess_gaps failed")
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_plan_enrichment(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Prioritise gaps and allocate budget across enrichment tasks.

    Budget allocation strategy: higher-priority gaps get a larger share.
    Within each gap, we cap at the number of actual candidates.
    """
    try:
        ctx = request.data
        budget = int(ctx.get("budget", DEFAULT_BUDGET))
        gap_assessment = ctx.get("gap_assessment", {})
        dimensions = ctx.get("gap_dimensions") or DEFAULT_GAP_DIMENSIONS

        # Sort dimensions by priority (lower number = higher priority)
        sorted_dims = sorted(dimensions, key=lambda d: d.get("priority", 99))

        enrichment_plan: List[Dict[str, Any]] = []
        remaining_budget = budget

        for dim in sorted_dims:
            gap_name = dim["gap_name"]
            count = gap_assessment.get(gap_name, 0)
            if count <= 0:
                continue
            if remaining_budget <= 0:
                break

            # Allocate: min(remaining, count, budget_per_gap)
            allocation = min(remaining_budget, count)

            dispatch_mode = dim.get("dispatch_mode")
            if not isinstance(dispatch_mode, str) or not dispatch_mode:
                dispatch_mode = (
                    "relation_completion"
                    if dim.get("predicate") == RELATION_COMPLETION_PREDICATE
                    else "text_enrichment"
                )

            enrichment_plan.append(
                {
                    "gap_name": gap_name,
                    "predicate": dim["predicate"],
                    "dispatch_mode": dispatch_mode,
                    "prompt_concept_id": dim.get("prompt_concept_id"),
                    "allocation": allocation,
                    "scan_limit": dim.get("scan_limit"),
                    "max_predicates_per_concept": dim.get("max_predicates_per_concept"),
                    "auto_apply_confidence_threshold": dim.get(
                        "auto_apply_confidence_threshold",
                        DEFAULT_RELATION_AUTO_APPLY_CONFIDENCE_THRESHOLD,
                    ),
                }
            )
            remaining_budget -= allocation

        logger.info(
            "[rumination] Plan: %d tasks, total allocation=%d (budget=%d)",
            len(enrichment_plan),
            budget - remaining_budget,
            budget,
        )

        return WorkflowActionResult(
            outputs={
                "enrichment_plan": enrichment_plan,
                "plan_index": 0,
                "dispatched_tasks": [],
                "total_processed": 0,
                "total_failed": 0,
                "relation_metrics": {
                    "concepts_considered": 0,
                    "proposed_relations": 0,
                    "auto_applied_relations": 0,
                    "would_apply_relations": 0,
                    "deferred_relations": 0,
                    "confirmed_relations": 0,
                    "failed_relations": 0,
                    "deferral_reason_counts": {},
                },
                "relation_changes": [],
                "relation_questions": [],
            }
        )
    except Exception as e:
        logger.exception("[rumination] plan_enrichment failed")
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_dispatch_enrichment(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Execute enrichment for the next planned task.

    Instead of creating a separate workflow instance (which would add
    complexity), we directly invoke the enrichment helpers inline.
    This keeps the rumination orchestrator as a single checkpointable unit.
    """
    try:
        ctx = request.data
        plan = ctx.get("enrichment_plan", [])
        plan_index = ctx.get("plan_index", 0)
        dispatched = ctx.get("dispatched_tasks", [])
        total_processed = ctx.get("total_processed", 0)
        total_failed = ctx.get("total_failed", 0)
        relation_metrics = dict(ctx.get("relation_metrics") or {})
        relation_changes = list(ctx.get("relation_changes") or [])
        relation_questions = list(ctx.get("relation_questions") or [])

        if plan_index >= len(plan):
            return WorkflowActionResult(
                outputs={
                    "has_more_tasks": False,
                    "dispatched_tasks": dispatched,
                    "total_processed": total_processed,
                    "total_failed": total_failed,
                    "relation_metrics": relation_metrics,
                    "relation_changes": relation_changes,
                    "relation_questions": relation_questions,
                }
            )

        task = plan[plan_index]
        predicate = task["predicate"]
        allocation = task["allocation"]
        prompt_concept_id = task.get("prompt_concept_id")
        gap_name = task["gap_name"]
        dispatch_mode = task.get("dispatch_mode", "text_enrichment")

        if dispatch_mode == "relation_completion":
            logger.info(
                "[rumination] Dispatching relation completion for '%s' (allocation=%d)",
                gap_name,
                allocation,
            )
            relation_task_result = _dispatch_relation_completion_task(task=task, ctx=ctx)
            dispatched.append(relation_task_result)

            task_metrics = dict(relation_task_result.get("metrics") or {})
            for metric_key in (
                "concepts_considered",
                "proposed_relations",
                "auto_applied_relations",
                "would_apply_relations",
                "deferred_relations",
                "confirmed_relations",
                "failed_relations",
            ):
                relation_metrics[metric_key] = int(relation_metrics.get(metric_key, 0)) + int(
                    task_metrics.get(metric_key, 0)
                )
            current_reasons = relation_metrics.get("deferral_reason_counts")
            if not isinstance(current_reasons, dict):
                current_reasons = {}
            task_reasons = task_metrics.get("deferral_reason_counts")
            if isinstance(task_reasons, dict):
                for reason, count in task_reasons.items():
                    current_reasons[str(reason)] = int(current_reasons.get(str(reason), 0)) + int(
                        count
                    )
            relation_metrics["deferral_reason_counts"] = current_reasons

            relation_changes.extend(relation_task_result.get("proposal_details") or [])
            relation_questions.extend(
                relation_task_result.get("deferred_questions") or []
            )

            return WorkflowActionResult(
                outputs={
                    "plan_index": plan_index + 1,
                    "has_more_tasks": plan_index + 1 < len(plan),
                    "dispatched_tasks": dispatched,
                    "total_processed": total_processed
                    + int(task_metrics.get("auto_applied_relations", 0)),
                    "total_failed": total_failed
                    + int(task_metrics.get("failed_relations", 0)),
                    "relation_metrics": relation_metrics,
                    "relation_changes": relation_changes,
                    "relation_questions": relation_questions,
                }
            )

        logger.info(
            "[rumination] Dispatching enrichment for '%s' (predicate=%s, allocation=%d)",
            gap_name,
            predicate,
            allocation,
        )

        # Import and use the enrichment workflow helpers directly
        from .enrichment_workflow import (
            _find_concepts_missing_predicate,
            _resolve_prompt_template,
            _build_concept_context,
            _upsert_text_value_internal,
        )
        from ...languagemodels.llm_interface import get_llm_client
        from ...db.repositories.concepts_repository import ConceptsRepository

        candidates = _find_concepts_missing_predicate(predicate, allocation)
        if not candidates:
            dispatched.append(
                {
                    "gap_name": gap_name,
                    "predicate": predicate,
                    "candidates": 0,
                    "processed": 0,
                    "failed": 0,
                }
            )
            return WorkflowActionResult(
                outputs={
                    "plan_index": plan_index + 1,
                    "has_more_tasks": plan_index + 1 < len(plan),
                    "dispatched_tasks": dispatched,
                    "total_processed": total_processed,
                    "total_failed": total_failed,
                    "relation_metrics": relation_metrics,
                    "relation_changes": relation_changes,
                    "relation_questions": relation_questions,
                }
            )

        prompt_ctx: Dict[str, Any] = {}
        if prompt_concept_id:
            prompt_ctx["prompt_concept_id"] = prompt_concept_id
        prompt_template, prompt_diagnostics = _resolve_prompt_template(prompt_ctx)
        if not prompt_template:
            logger.warning(
                "[rumination] Prompt unavailable for %s enrichment task (predicate=%s): %s",
                gap_name,
                predicate,
                prompt_diagnostics,
            )
            dispatched.append(
                {
                    "gap_name": gap_name,
                    "predicate": predicate,
                    "candidates": len(candidates),
                    "processed": 0,
                    "failed": len(candidates),
                    "prompt_diagnostics": prompt_diagnostics,
                }
            )
            return WorkflowActionResult(
                outputs={
                    "plan_index": plan_index + 1,
                    "has_more_tasks": plan_index + 1 < len(plan),
                    "dispatched_tasks": dispatched,
                    "total_processed": total_processed,
                    "total_failed": total_failed + len(candidates),
                    "relation_metrics": relation_metrics,
                    "relation_changes": relation_changes,
                    "relation_questions": relation_questions,
                }
            )

        llm = get_llm_client()
        task_processed = 0
        task_failed = 0

        for concept_id in candidates:
            try:
                concept = ConceptsRepository.find_one({"concept_id": concept_id})
                if not concept:
                    continue

                tmpl_ctx = _build_concept_context(concept)
                tmpl_ctx["predicate"] = predicate

                try:
                    prompt = prompt_template.format(**tmpl_ctx)
                except KeyError:
                    prompt = prompt_template.format_map(tmpl_ctx)

                response = llm.generate(prompt, llm_params={"max_tokens": 400})
                text = (
                    response.strip()
                    if isinstance(response, str)
                    else str(response).strip()
                )

                if text and len(text) >= 10:
                    _upsert_text_value_internal(concept_id, predicate, text)
                    task_processed += 1
                else:
                    task_failed += 1

            except Exception as e:
                logger.error(
                    "[rumination] Error enriching %s with %s: %s",
                    concept_id,
                    predicate,
                    e,
                )
                task_failed += 1

        dispatched.append(
            {
                "gap_name": gap_name,
                "predicate": predicate,
                "candidates": len(candidates),
                "processed": task_processed,
                "failed": task_failed,
            }
        )

        logger.info(
            "[rumination] Task '%s' complete: processed=%d failed=%d",
            gap_name,
            task_processed,
            task_failed,
        )

        return WorkflowActionResult(
            outputs={
                "plan_index": plan_index + 1,
                "has_more_tasks": plan_index + 1 < len(plan),
                "dispatched_tasks": dispatched,
                "total_processed": total_processed + task_processed,
                "total_failed": total_failed + task_failed,
                "relation_metrics": relation_metrics,
                "relation_changes": relation_changes,
                "relation_questions": relation_questions,
            }
        )
    except Exception as e:
        logger.exception("[rumination] dispatch_enrichment failed")
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Produce a summary of the rumination run."""
    ctx = request.data
    gap_assessment = ctx.get("gap_assessment", {})
    dispatched = ctx.get("dispatched_tasks", [])
    total_processed = ctx.get("total_processed", 0)
    total_failed = ctx.get("total_failed", 0)
    relation_metrics = dict(ctx.get("relation_metrics") or {})
    relation_changes = list(ctx.get("relation_changes") or [])
    relation_questions = list(ctx.get("relation_questions") or [])

    rumination_result = {
        "success": total_failed == 0,
        "gap_assessment": gap_assessment,
        "tasks_dispatched": len(dispatched),
        "total_processed": total_processed,
        "total_failed": total_failed,
        "task_details": dispatched,
        "relation_metrics": relation_metrics,
        "relation_changes_count": len(relation_changes),
        "relation_questions_count": len(relation_questions),
        "relation_changes": relation_changes,
        "relation_questions": relation_questions,
    }

    logger.info(
        "[rumination] Complete: tasks=%d processed=%d failed=%d",
        len(dispatched),
        total_processed,
        total_failed,
    )

    return WorkflowActionResult(outputs={"rumination_result": rumination_result})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def build_rumination_workflow_test_registration() -> WorkflowRegistration:
    """Get the workflow registration for the rumination orchestrator."""
    return WorkflowRegistration(
        workflow_id=RUMINATION_WORKFLOW_ID,
        definition=build_rumination_workflow_test_definition(),
        purpose="Proactive knowledge quality orchestrator — assesses gaps and dispatches enrichment.",
        source="built_in",
    )


def register_rumination_actions(registry: ActionRegistry) -> None:
    """Register rumination action handlers with an ActionRegistry."""
    actions = [
        ActionSpec(
            action_id="rumination.assess_gaps",
            handler=_handle_assess_gaps,
            description="Scan ontology for quality gaps across multiple dimensions.",
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="rumination.plan_enrichment",
            handler=_handle_plan_enrichment,
            description="Prioritise gaps and allocate budget across enrichment tasks.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="rumination.dispatch_enrichment",
            handler=_handle_dispatch_enrichment,
            description="Execute enrichment for the next planned task.",
            side_effects="write",
        ),
        ActionSpec(
            action_id="rumination.finalise",
            handler=_handle_finalise,
            description="Report rumination results.",
            side_effects="none",
        ),
    ]
    for spec in actions:
        try:
            registry.register(spec)
        except ValueError:
            pass  # Already registered (hot reload)
