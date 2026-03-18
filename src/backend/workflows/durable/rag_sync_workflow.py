"""RAG text relation sync workflow definition and action handlers.

This module provides a durable workflow for synchronising Vontology text
relations to the RAG store. It replaces the synchronous batch operation
with a checkpointable, resumable background workflow.

Phase 4 of JVNAUTOSCI-1075: Durable Workflow System Implementation.
Design doc: docs/engineering/durable_workflow_system_design.md
"""

from __future__ import annotations

import logging
import os
from typing import Any

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


# Workflow ID for RAG text relation sync
RAG_TEXT_RELATION_SYNC_WORKFLOW_ID = "#V#rag_text_relation_sync_workflow"

# Default batch size for RAG sync operations
DEFAULT_RAG_SYNC_BATCH_SIZE = 200
DEFAULT_RAG_SYNC_LIMIT = 5000


def build_rag_text_relation_sync_workflow_test_definition() -> WorkflowDefinition:
    """Build the RAG text relation sync workflow definition.

    This workflow synchronises text relations from Vontology to the RAG store
    in a durable, checkpointable manner.

    States:
        - collect: Gather text relation documents for the namespace
        - batch: Prepare the next batch for upserting
        - upsert: Upsert the current batch to RAG store
        - complete: Terminal success state
        - failed: Terminal failure state

    Context keys used:
        - namespace: Required. The namespace to sync (e.g., "#V#user@org")
        - predicates: Optional. List of predicate IDs to filter
        - languages: Optional. List of language codes to filter
        - concept_ids: Optional. List of concept IDs to filter
        - limit: Optional. Max documents to sync (default: 5000)
        - batch_size: Optional. Documents per batch (default: 200)

    Context keys produced:
        - collected_docs: List of TextRelationRagDoc objects (as dicts)
        - batch_index: Current batch index
        - total_batches: Total number of batches
        - current_batch: Current batch of docs being processed
        - added: Cumulative count of successfully added docs
        - failed: Cumulative count of failed docs
        - has_more_batches: Boolean flag for batch loop
        - sync_result: Final sync result summary
    """

    collect = WorkflowStateSpec(
        state_id="collect",
        actions=(
            WorkflowActionInvocation(
                action_id="rag_sync.collect_docs",
                description="Collect text relation documents for the namespace.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="batch",
                condition=lambda ctx: bool(ctx.get("collected_docs")),
                reason="docs_collected",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="no_docs_to_sync",
            ),
        ),
    )

    batch = WorkflowStateSpec(
        state_id="batch",
        actions=(
            WorkflowActionInvocation(
                action_id="rag_sync.prepare_batch",
                description="Prepare the next batch of documents for upserting.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="upsert",
                condition=lambda ctx: bool(ctx.get("current_batch")),
                reason="batch_prepared",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="all_batches_processed",
            ),
        ),
    )

    upsert = WorkflowStateSpec(
        state_id="upsert",
        actions=(
            WorkflowActionInvocation(
                action_id="rag_sync.upsert_batch",
                description="Upsert the current batch to RAG store.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="batch",
                condition=lambda ctx: ctx.get("has_more_batches", False),
                reason="more_batches",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="all_batches_done",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="rag_sync.finalise",
                description="Finalise sync and produce result summary.",
            ),
        ),
        terminal=True,
    )

    failed = WorkflowStateSpec(
        state_id="failed",
        terminal=True,
    )

    return WorkflowDefinition(
        workflow_id=RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
        initial_state="collect",
        states={
            "collect": collect,
            "batch": batch,
            "upsert": upsert,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose="Synchronise Vontology text relations to RAG store with checkpointing.",
    )


# ---------------------------------------------------------------------------
# Action Handlers
# ---------------------------------------------------------------------------


def _handle_collect_docs(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Collect text relation documents for the namespace.

    Reads from request.data:
        - namespace (required)
        - predicates (optional)
        - languages (optional)
        - concept_ids (optional)
        - limit (optional, default: 5000)

    Writes to outputs:
        - collected_docs: List of doc dicts
        - total_candidates: Number of docs collected
        - batch_index: Initialised to 0
        - added: Initialised to 0
        - failed: Initialised to 0
    """
    try:
        from ...services.rag_text_relation_sync_service import (
            collect_text_relation_docs_for_namespace,
        )

        ctx = request.data
        namespace = ctx.get("namespace")

        if not namespace:
            return WorkflowActionResult(
                status="failed",
                error="namespace_required",
            )

        predicates = ctx.get("predicates")
        languages = ctx.get("languages")
        concept_ids = ctx.get("concept_ids")
        limit = int(ctx.get("limit", DEFAULT_RAG_SYNC_LIMIT))

        logger.info(
            "[rag_sync_workflow] Collecting docs for namespace=%s limit=%d",
            namespace,
            limit,
        )

        docs = collect_text_relation_docs_for_namespace(
            namespace=namespace,
            predicates=predicates,
            languages=languages,
            concept_ids=concept_ids,
            limit=limit,
        )

        # Convert to serialisable dicts for checkpointing
        doc_dicts = [
            {"doc_id": d.doc_id, "text": d.text, "metadata": d.metadata} for d in docs
        ]

        batch_size = int(ctx.get("batch_size", DEFAULT_RAG_SYNC_BATCH_SIZE))
        total_batches = (
            (len(doc_dicts) + batch_size - 1) // batch_size if doc_dicts else 0
        )

        logger.info(
            "[rag_sync_workflow] Collected %d docs (%d batches) for namespace=%s",
            len(doc_dicts),
            total_batches,
            namespace,
        )

        return WorkflowActionResult(
            outputs={
                "collected_docs": doc_dicts,
                "total_candidates": len(doc_dicts),
                "batch_index": 0,
                "total_batches": total_batches,
                "added": 0,
                "failed": 0,
            }
        )
    except Exception as exc:
        logger.exception("[rag_sync_workflow] collect_docs failed: %s", exc)
        return WorkflowActionResult(status="failed", error=str(exc))


def _handle_prepare_batch(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Prepare the next batch of documents for upserting.

    Reads from request.data:
        - collected_docs: Full list of doc dicts
        - batch_index: Current batch index
        - batch_size (optional, default: 200)

    Writes to outputs:
        - current_batch: List of docs for this batch
        - batch_index: Incremented batch index
        - has_more_batches: Boolean
    """
    try:
        ctx = request.data
        docs = ctx.get("collected_docs", [])
        batch_index = int(ctx.get("batch_index", 0))
        batch_size = int(ctx.get("batch_size", DEFAULT_RAG_SYNC_BATCH_SIZE))

        start = batch_index * batch_size
        end = start + batch_size
        current_batch = docs[start:end]

        has_more = end < len(docs)

        logger.debug(
            "[rag_sync_workflow] Prepared batch %d: %d docs (has_more=%s)",
            batch_index,
            len(current_batch),
            has_more,
        )

        return WorkflowActionResult(
            outputs={
                "current_batch": current_batch,
                "batch_index": batch_index + 1,
                "has_more_batches": has_more,
            }
        )
    except Exception as exc:
        logger.exception("[rag_sync_workflow] prepare_batch failed: %s", exc)
        return WorkflowActionResult(status="failed", error=str(exc))


def _handle_upsert_batch(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Upsert the current batch to RAG store.

    Reads from request.data:
        - namespace (required)
        - current_batch: List of doc dicts
        - added: Running total of successfully added docs
        - failed: Running total of failed docs

    Writes to outputs:
        - added: Updated total
        - failed: Updated total
        - batch_added: Docs added in this batch
        - batch_failed: Docs failed in this batch
    """
    try:
        from ...services.rag_service import RAGBackendUnavailable, get_rag_service

        ctx = request.data
        namespace = ctx.get("namespace")
        current_batch = ctx.get("current_batch", [])
        added = int(ctx.get("added", 0))
        failed = int(ctx.get("failed", 0))

        if not current_batch:
            return WorkflowActionResult(
                outputs={
                    "batch_added": 0,
                    "batch_failed": 0,
                    "added": added,
                    "failed": failed,
                }
            )

        try:
            rag = get_rag_service()
        except RAGBackendUnavailable as exc:
            logger.warning("[rag_sync_workflow] RAG service unavailable: %s", exc)
            return WorkflowActionResult(
                status="failed",
                error=f"rag_unavailable:{exc}",
            )

        # Format for RAG service
        payload = [
            {"id": d["doc_id"], "text": d["text"], "metadata": d["metadata"]}
            for d in current_batch
        ]

        batch_ok, batch_bad = rag.upsert_documents(payload, namespace=namespace)

        new_added = added + int(batch_ok)
        new_failed = failed + int(batch_bad)

        logger.info(
            "[rag_sync_workflow] Upserted batch: ok=%d failed=%d (total: added=%d failed=%d)",
            batch_ok,
            batch_bad,
            new_added,
            new_failed,
        )

        return WorkflowActionResult(
            outputs={
                "batch_added": int(batch_ok),
                "batch_failed": int(batch_bad),
                "added": new_added,
                "failed": new_failed,
            }
        )
    except Exception as exc:
        logger.exception("[rag_sync_workflow] upsert_batch failed: %s", exc)
        return WorkflowActionResult(status="failed", error=str(exc))


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Finalise sync and produce result summary.

    Reads from request.data:
        - namespace
        - total_candidates
        - added
        - failed

    Writes to outputs:
        - sync_result: Summary dict
    """
    ctx = request.data
    namespace = ctx.get("namespace")
    total_candidates = int(ctx.get("total_candidates", 0))
    added = int(ctx.get("added", 0))
    failed = int(ctx.get("failed", 0))

    sync_result = {
        "success": failed == 0,
        "namespace": namespace,
        "total_candidates": total_candidates,
        "added": added,
        "failed": failed,
    }

    logger.info(
        "[rag_sync_workflow] Completed: namespace=%s candidates=%d added=%d failed=%d",
        namespace,
        total_candidates,
        added,
        failed,
    )

    return WorkflowActionResult(outputs={"sync_result": sync_result})


# ---------------------------------------------------------------------------
# Registration Helpers
# ---------------------------------------------------------------------------


def build_rag_text_relation_sync_workflow_test_registration() -> WorkflowRegistration:
    """Get the workflow registration for RAG text relation sync."""
    return WorkflowRegistration(
        workflow_id=RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
        definition=build_rag_text_relation_sync_workflow_test_definition(),
        purpose="Synchronise Vontology text relations to RAG store with checkpointing.",
        source="built_in",
    )


def register_rag_sync_actions(registry: ActionRegistry) -> None:
    """Register RAG sync action handlers with an ActionRegistry."""
    actions = [
        ActionSpec(
            action_id="rag_sync.collect_docs",
            handler=_handle_collect_docs,
            description="Collect text relation documents for the namespace.",
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="rag_sync.prepare_batch",
            handler=_handle_prepare_batch,
            description="Prepare the next batch of documents for upserting.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="rag_sync.upsert_batch",
            handler=_handle_upsert_batch,
            description="Upsert the current batch to RAG store.",
            side_effects="write",
        ),
        ActionSpec(
            action_id="rag_sync.finalise",
            handler=_handle_finalise,
            description="Finalise sync and produce result summary.",
            side_effects="none",
        ),
    ]

    for spec in actions:
        try:
            registry.register(spec)
        except ValueError:
            # Already registered (e.g., during hot reload)
            pass
