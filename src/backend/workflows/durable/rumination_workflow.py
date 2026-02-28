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
DEFAULT_RELATION_QUESTION_LIMIT = 50
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


def build_rumination_workflow() -> WorkflowDefinition:
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
) -> dict[str, Any] | None:
    """Resolve a safe auto-apply target from hypothesised relations."""
    if not isinstance(predicate, str) or not predicate.startswith("#V#"):
        return None

    relationships = instance_doc.get("relationships") or {}
    if isinstance(relationships, dict) and relationships.get(predicate):
        return None

    hypotheses = (instance_doc.get("hypothesized_relations") or {}).get(predicate)
    if isinstance(hypotheses, dict):
        hypotheses = [hypotheses]
    if not isinstance(hypotheses, list) or not hypotheses:
        return None

    candidates: list[tuple[float, str]] = []
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        target_id = hypothesis.get("value")
        if not isinstance(target_id, str) or not target_id.startswith("#V#"):
            continue
        confidence = _coerce_float(hypothesis.get("confidence_score"), default=0.0)
        if confidence < confidence_threshold:
            continue
        candidates.append((confidence, target_id))
    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item[0], item[1]))
    confidence, target_id = candidates[0]

    from ...db.repositories.concepts_repository import ConceptsRepository

    target_exists = ConceptsRepository.find_one(
        {"concept_id": target_id}, projection={"concept_id": 1}
    )
    if not target_exists:
        return None

    return {
        "target_id": target_id,
        "confidence_score": confidence,
        "source": "hypothesized_relation",
    }


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
    detail_limit = int(
        ctx.get("relation_detail_limit", DEFAULT_RELATION_DETAIL_LIMIT)
    )
    question_limit = int(
        ctx.get("relation_question_limit", DEFAULT_RELATION_QUESTION_LIMIT)
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
    proposal_details: list[dict[str, Any]] = []
    deferred_questions: list[dict[str, Any]] = []

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
            )
            if auto_candidate:
                detail["target_id"] = auto_candidate["target_id"]
                detail["confidence_score"] = auto_candidate["confidence_score"]
                detail["candidate_source"] = auto_candidate["source"]

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
                        else:
                            detail["action"] = "deferred_after_apply_failure"
                            detail["error"] = result.get("error")
                            deferred_relations += 1
                            failed_relations += 1
                    except Exception as exc:
                        detail["action"] = "deferred_after_apply_exception"
                        detail["error"] = str(exc)
                        deferred_relations += 1
                        failed_relations += 1
            else:
                detail["action"] = "deferred_question"
                deferred_relations += 1
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
        prompt_template = _resolve_prompt_template(prompt_ctx)

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


def get_rumination_workflow_registration() -> WorkflowRegistration:
    """Get the workflow registration for the rumination orchestrator."""
    return WorkflowRegistration(
        workflow_id=RUMINATION_WORKFLOW_ID,
        definition=build_rumination_workflow(),
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
