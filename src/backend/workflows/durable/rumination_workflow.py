"""Rumination orchestrator workflow — the "reflection" layer.

This workflow embodies JVNAUTOSCI-923: a **higher-level** process that
periodically inspects the ontology for quality gaps, prioritises them,
and dispatches enrichment sub-workflows to fill them.

Unlike the parameterised ``enrichment_workflow`` (which generates a single
predicate for a batch of concepts), the rumination orchestrator decides
*what kind* of enrichment is needed across multiple dimensions:

1. **Missing descriptions** — concepts without ``hasDescription``
2. **Missing considerations** — concepts without ``hasConsiderationsForUse``
3. **Isolated concepts** — concepts with no outgoing relationships beyond
   type hierarchy
4. **Stale embeddings** — concepts with ``embedding_status == 'stale'``

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

# Gap dimensions the orchestrator checks, in priority order.
# Each entry: (gap_name, predicate_to_check, enrichment_predicate, prompt_concept_id_or_none)
DEFAULT_GAP_DIMENSIONS: List[Dict[str, Any]] = [
    {
        "gap_name": "missing_descriptions",
        "predicate": "hasDescription",
        "priority": 1,
        "prompt_concept_id": "#V#generate_concept_description_prompt",
    },
    {
        "gap_name": "missing_considerations",
        "predicate": "#V#has_considerations_for_use",
        "priority": 2,
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


def _handle_assess_gaps(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Scan the ontology for quality gaps across multiple dimensions."""
    try:
        ctx = request.data
        dimensions = ctx.get("gap_dimensions") or DEFAULT_GAP_DIMENSIONS

        gap_assessment: Dict[str, int] = {}

        for dim in dimensions:
            gap_name = dim["gap_name"]
            predicate = dim["predicate"]
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

            enrichment_plan.append(
                {
                    "gap_name": gap_name,
                    "predicate": dim["predicate"],
                    "prompt_concept_id": dim.get("prompt_concept_id"),
                    "allocation": allocation,
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

        if plan_index >= len(plan):
            return WorkflowActionResult(
                outputs={
                    "has_more_tasks": False,
                    "dispatched_tasks": dispatched,
                    "total_processed": total_processed,
                    "total_failed": total_failed,
                }
            )

        task = plan[plan_index]
        predicate = task["predicate"]
        allocation = task["allocation"]
        prompt_concept_id = task.get("prompt_concept_id")
        gap_name = task["gap_name"]

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

    rumination_result = {
        "success": total_failed == 0,
        "gap_assessment": gap_assessment,
        "tasks_dispatched": len(dispatched),
        "total_processed": total_processed,
        "total_failed": total_failed,
        "task_details": dispatched,
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
