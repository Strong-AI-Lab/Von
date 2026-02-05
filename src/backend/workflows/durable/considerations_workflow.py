"""Workflow to generate 'Considerations for Use' for concepts.

This workflow identifies concepts that lack a 'hasConsiderationsForUse' text relation
and uses an LLM to generate one based on the concept's description and name.
It runs as a background durable workflow with checkpointing.

JIRA: JVNAUTOSCI-1081
"""

from __future__ import annotations

import logging
from typing import Any, List, Dict, Optional

from bson import ObjectId

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

from ...db.repositories.concepts_repository import ConceptsRepository
from ...db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ...languagemodels.llm_interface import get_llm_client, LLMInterface as LLMClient
from ...models.text_value_models import TextValueModel, TextRelationModel
from ...vontology.utils_vontology import (
    get_concept_display_name_with_names_fallback,
    get_concept_description,
)

logger = logging.getLogger(__name__)

# Workflow ID
GENERATE_CONSIDERATIONS_WORKFLOW_ID = "#V#generate_considerations_workflow"
PREDICATE_ID = "#V#has_considerations_for_use"

DEFAULT_BATCH_SIZE = 5
DEFAULT_CANDIDATE_LIMIT = 50


def build_generate_considerations_workflow() -> WorkflowDefinition:
    """Build the workflow definition."""

    collect = WorkflowStateSpec(
        state_id="collect",
        actions=(
            WorkflowActionInvocation(
                action_id="considerations.collect_candidates",
                description="Find concepts needing considerations.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="batch",
                condition=lambda ctx: bool(ctx.get("candidate_ids")),
                reason="candidates_found",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="no_candidates",
            ),
        ),
    )

    batch = WorkflowStateSpec(
        state_id="batch",
        actions=(
            WorkflowActionInvocation(
                action_id="considerations.prepare_batch",
                description="Prepare next batch of concepts.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="generate",
                condition=lambda ctx: bool(ctx.get("current_batch")),
                reason="batch_ready",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="all_processed",
            ),
        ),
    )

    generate = WorkflowStateSpec(
        state_id="generate",
        actions=(
            WorkflowActionInvocation(
                action_id="considerations.process_batch",
                description="Generate and save considerations for batch.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="batch",
                condition=lambda ctx: ctx.get("has_more_batches", False),
                reason="more_batches_pending",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="processing_complete_no_more_batches",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="considerations.finalise",
                description="Report results.",
            ),
        ),
        terminal=True,
    )

    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=GENERATE_CONSIDERATIONS_WORKFLOW_ID,
        initial_state="collect",
        states={
            "collect": collect,
            "batch": batch,
            "generate": generate,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose="Generate 'Considerations for Use' for concepts lacking them.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_concepts_missing_predicate(predicate: str, limit: int) -> List[str]:
    """Find concept IDs that do not have the specified text relation predicate.

    This is a heuristic search. It might be expensive on huge DBs.
    Strategy:
    1. Get all concepts (limited).
    2. Check relations.
    Better Strategy for large DB: Use aggregation (lookup) or just scan 'recent' concepts.

    For now, we'll scan concepts updated recently or just a limit.
    """
    # Find all concepts that HAVE the predicate
    existing_rels = TextRelationsRepository.find(
        {"predicate": predicate}, projection={"subject_concept_id": 1}
    )
    excluded_ids = {r["subject_concept_id"] for r in existing_rels}

    # Find candidate concepts (excluding those IDs)
    # We restrict to 'individual' kind or specific types if needed.
    # For general use, we'll take any concept.
    query = {
        "concept_id": {"$nin": list(excluded_ids)},
        # Maybe filter by namespace or update time?
        # "updated_at": ...
    }

    candidates = ConceptsRepository.find(
        query, limit=limit, projection={"concept_id": 1}
    )
    return [c["concept_id"] for c in candidates]


def _upsert_text_value_internal(concept_id: str, predicate: str, text: str) -> None:
    """Internal helper to upsert text value without user-context auth checks."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # 1. Prepare TextValue
    # We duplicate logic from text_value_service to avoid 'can_access_concept' checks
    # which rely on request context not available in background workers.

    # Normalize
    stored_text = text.strip()
    lang = "en-NZ"  # Default for generated content
    # Fingerprint
    import re

    norm = stored_text.strip().replace("\r\n", "\n").replace("\r", "\n")
    norm = re.sub(r"[\t\f\v ]+", " ", norm)
    norm = re.sub(r" *\n *", "\n", norm).lower()
    fingerprint = f"{norm}||{lang.lower()}"

    # Find or Create TextValue
    existing_tv = TextValuesRepository.find_one(
        {"fingerprint": fingerprint, "lang": lang}
    )
    if existing_tv:
        tv_id = existing_tv["_id"]
    else:
        res = TextValuesRepository.insert_one(
            {
                "text": stored_text,
                "lang": lang,
                "provenance": {
                    "source": "start_generate_considerations_workflow",
                    "model": "llm",
                },
                "fingerprint": fingerprint,
                "created_at": now,
                "updated_at": now,
            }
        )
        tv_id = res.inserted_id

    # 2. Link Relation
    # Check existing relation
    existing_rel = TextRelationsRepository.find_one(
        {"subject_concept_id": concept_id, "predicate": predicate}
    )

    if existing_rel:
        if existing_rel.get("object_text_id") != tv_id:
            TextRelationsRepository.update_one(
                {"_id": existing_rel["_id"]},
                {"$set": {"object_text_id": tv_id, "updated_at": now}},
            )
    else:
        TextRelationsRepository.insert_one(
            {
                "subject_concept_id": concept_id,
                "predicate": predicate,
                "object_text_id": tv_id,
                "context": {},
                "created_at": now,
                "updated_at": now,
            }
        )
        # Mark concept stale
        ConceptsRepository.update_one(
            {"concept_id": concept_id}, {"$set": {"embedding_status": "stale"}}
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_collect_candidates(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Find candidate concepts."""
    try:
        ctx = request.data
        limit = int(ctx.get("limit", DEFAULT_CANDIDATE_LIMIT))

        # If user provided specific IDs, use them
        if ctx.get("force_concept_ids"):
            candidates = ctx.get("force_concept_ids")
        else:
            candidates = _find_concepts_missing_predicate(PREDICATE_ID, limit)

        logger.info(
            f"[{GENERATE_CONSIDERATIONS_WORKFLOW_ID}] Found {len(candidates)} candidates."
        )

        return WorkflowActionResult(
            outputs={
                "candidate_ids": candidates,
                "total_candidates": len(candidates),
                "batch_index": 0,
                "processed_count": 0,
                "failed_count": 0,
            }
        )
    except Exception as e:
        logger.exception("collect_candidates failed")
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_prepare_batch(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Slice next batch."""
    try:
        ctx = request.data
        candidates = ctx.get("candidate_ids", [])
        batch_index = ctx.get("batch_index", 0)
        batch_size = int(ctx.get("batch_size", DEFAULT_BATCH_SIZE))

        start = batch_index * batch_size
        end = start + batch_size
        current_batch = candidates[start:end]
        has_more = end < len(candidates)

        return WorkflowActionResult(
            outputs={
                "current_batch": current_batch,
                "batch_index": batch_index + 1,
                "has_more_batches": has_more,
            }
        )
    except Exception as e:
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_process_batch(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Generate and save considerations text for each concept in the current batch.

    Uses the synchronous LLMClient.generate() API. If async LLM calls
    are needed in future, make the ActionRegistry.execute() path async-aware
    first (see JVNAUTOSCI-803).
    """

    try:
        ctx = request.data
        batch = ctx.get("current_batch", [])
        processed = ctx.get("processed_count", 0)
        failed = ctx.get("failed_count", 0)

        if not batch:
            return WorkflowActionResult(
                outputs={"processed_count": processed, "failed_count": failed}
            )

        llm = get_llm_client()

        newly_processed = 0
        newly_failed = 0

        for concept_id in batch:
            try:
                # 1. Get Context
                concept = ConceptsRepository.find_one({"concept_id": concept_id})
                if not concept:
                    continue

                name = get_concept_display_name_with_names_fallback(concept)
                description = (
                    get_concept_description(concept) or "No description available."
                )

                # 2. Prompt
                prompt = (
                    f"You are a knowledge engineer assistant.\n"
                    f"Concept: {name} ({concept_id})\n"
                    f"Description: {description}\n\n"
                    f"Please write a 'Considerations for Use' section for this concept.\n"
                    f"Explain when to use it, when to avoid it, and any nuances.\n"
                    f"Keep it concise (under 200 words)."
                )

                # 3. Generate
                # Using simple generate for now.
                response = llm.generate(prompt, max_tokens=300)
                text = response.text.strip()

                if text:
                    # 4. Save
                    _upsert_text_value_internal(concept_id, PREDICATE_ID, text)
                    newly_processed += 1
                else:
                    logger.warning(f"Empty generation for {concept_id}")
                    newly_failed += 1

            except Exception as e:
                logger.error(f"Error processing {concept_id}: {e}")
                newly_failed += 1

        return WorkflowActionResult(
            outputs={
                "processed_count": processed + newly_processed,
                "failed_count": failed + newly_failed,
            }
        )

    except Exception as e:
        logger.exception("process_batch failed")
        return WorkflowActionResult(status="failed", error=str(e))


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    return WorkflowActionResult(outputs={"result": "Success"})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def get_considerations_workflow_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=GENERATE_CONSIDERATIONS_WORKFLOW_ID,
        definition=build_generate_considerations_workflow(),
        purpose="Auto-generate Considerations for Use.",
        source="built_in",
    )


def register_considerations_actions(registry: ActionRegistry) -> None:
    registry.register(
        ActionSpec(
            "considerations.collect_candidates",
            _handle_collect_candidates,
            side_effects="read_only",
        )
    )
    registry.register(
        ActionSpec(
            "considerations.prepare_batch", _handle_prepare_batch, side_effects="none"
        )
    )
    registry.register(
        ActionSpec(
            "considerations.process_batch", _handle_process_batch, side_effects="write"
        )
    )
    registry.register(
        ActionSpec("considerations.finalise", _handle_finalise, side_effects="none")
    )
