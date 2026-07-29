from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_RAG_SYNC_QUEUE_MAX_SIZE = 256
_RAG_SYNC_QUEUE: queue.Queue[dict[str, str | None]] = queue.Queue(
    maxsize=_RAG_SYNC_QUEUE_MAX_SIZE
)
_RAG_SYNC_LOCK = threading.Lock()
_RAG_SYNC_PENDING_KEYS: set[tuple[str, str, str | None, str | None]] = set()
_RAG_SYNC_RERUN_KEYS: set[tuple[str, str, str | None, str | None]] = set()
_RAG_SYNC_WORKER_THREAD: threading.Thread | None = None
_QUEUE_RECEIPT_FIELDS = {
    "mechanism": "in_memory_bounded_queue",
    "durable": False,
}


def _run_concept_text_relation_rag_sync(
    *,
    namespace: str,
    concept_id: str,
    predicate: str | None,
    scoped_assertion_id: str | None,
) -> Dict[str, Any]:
    """Run one synchronous RAG refresh outside the primary write path."""

    if scoped_assertion_id:
        from src.backend.services.rag_text_relation_sync_service import (
            sync_scoped_assertions_to_rag,
        )

        return sync_scoped_assertions_to_rag(
            namespace=namespace,
            assertion_ids=[scoped_assertion_id],
        )

    from src.backend.services.rag_text_relation_sync_service import (
        sync_text_relations_to_rag,
    )
    return sync_text_relations_to_rag(
        namespace=namespace,
        concept_ids=[concept_id],
        predicates=[predicate] if predicate else None,
        limit=5000,
    )


def _concept_text_relation_rag_sync_worker() -> None:
    while True:
        job = _RAG_SYNC_QUEUE.get()
        namespace = str(job["namespace"])
        concept_id = str(job["concept_id"])
        predicate_value = job.get("predicate")
        predicate = str(predicate_value) if predicate_value else None
        scoped_assertion_id_value = job.get("scoped_assertion_id")
        scoped_assertion_id = (
            str(scoped_assertion_id_value) if scoped_assertion_id_value else None
        )
        pending_key = (namespace, concept_id, predicate, scoped_assertion_id)
        try:
            result = _run_concept_text_relation_rag_sync(
                namespace=namespace,
                concept_id=concept_id,
                predicate=predicate,
                scoped_assertion_id=scoped_assertion_id,
            )
            if not result.get("success", False):
                logger.warning(
                    "[rag_text_relations] Background RAG sync did not succeed "
                    "concept_id=%s namespace=%s result=%s",
                    concept_id,
                    namespace,
                    result,
                )
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            logger.warning(
                "[rag_text_relations] Background RAG sync failed "
                "concept_id=%s namespace=%s: %s",
                concept_id,
                namespace,
                exc,
            )
        finally:
            with _RAG_SYNC_LOCK:
                if pending_key in _RAG_SYNC_RERUN_KEYS:
                    _RAG_SYNC_RERUN_KEYS.discard(pending_key)
                    try:
                        _RAG_SYNC_QUEUE.put_nowait(job)
                    except queue.Full:  # pragma: no cover - worker drained one slot
                        _RAG_SYNC_PENDING_KEYS.discard(pending_key)
                        logger.warning(
                            "[rag_text_relations] Could not queue requested "
                            "follow-up refresh concept_id=%s namespace=%s",
                            concept_id,
                            namespace,
                        )
                else:
                    _RAG_SYNC_PENDING_KEYS.discard(pending_key)
            _RAG_SYNC_QUEUE.task_done()


def maybe_sync_concept_text_relations_to_rag(
    *,
    namespace: Optional[str],
    concept_id: str,
    predicate: Optional[str] = None,
    scoped_assertion_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Queue a best-effort concept RAG refresh outside the primary write path.

    The bounded queue prevents derived index maintenance from delaying or
    changing the receipt for the durable Vontology write. Exact duplicate work
    is coalesced while pending. Queue saturation remains an observable,
    recoverable indexing miss rather than backpressure on the primary mutation.
    """

    if not namespace or not isinstance(namespace, str) or not namespace.strip():
        return {
            **_QUEUE_RECEIPT_FIELDS,
            "success": False,
            "scheduled": False,
            "skipped": True,
            "reason": "namespace_required",
        }

    if not isinstance(concept_id, str) or not concept_id.strip():
        return {
            **_QUEUE_RECEIPT_FIELDS,
            "success": False,
            "scheduled": False,
            "skipped": True,
            "reason": "concept_id_required",
        }

    clean_predicate = None
    if isinstance(predicate, str) and predicate.strip():
        clean_predicate = predicate.strip()
    clean_namespace = namespace.strip()
    clean_concept_id = concept_id.strip()
    clean_scoped_assertion_id = (
        scoped_assertion_id.strip()
        if isinstance(scoped_assertion_id, str)
        and scoped_assertion_id.strip().startswith("ska_")
        else None
    )
    pending_key = (
        clean_namespace,
        clean_concept_id,
        clean_predicate,
        clean_scoped_assertion_id,
    )
    job = {
        "namespace": clean_namespace,
        "concept_id": clean_concept_id,
        "predicate": clean_predicate,
        "scoped_assertion_id": clean_scoped_assertion_id,
    }

    global _RAG_SYNC_WORKER_THREAD
    try:
        with _RAG_SYNC_LOCK:
            if pending_key in _RAG_SYNC_PENDING_KEYS:
                # A refresh already running may have read before this write.
                # Mark it dirty so the worker performs one follow-up refresh
                # after the current pass rather than losing the newer state.
                _RAG_SYNC_RERUN_KEYS.add(pending_key)
                return {
                    **_QUEUE_RECEIPT_FIELDS,
                    "success": True,
                    "scheduled": False,
                    "reason": "already_scheduled",
                    "rerun_requested": True,
                }
            if (
                _RAG_SYNC_WORKER_THREAD is None
                or not _RAG_SYNC_WORKER_THREAD.is_alive()
            ):
                worker = threading.Thread(
                    target=_concept_text_relation_rag_sync_worker,
                    name="rag-text-relation-sync",
                    daemon=True,
                )
                worker.start()
                _RAG_SYNC_WORKER_THREAD = worker

            _RAG_SYNC_PENDING_KEYS.add(pending_key)
            try:
                _RAG_SYNC_QUEUE.put_nowait(job)
            except queue.Full:
                _RAG_SYNC_PENDING_KEYS.discard(pending_key)
                _RAG_SYNC_RERUN_KEYS.discard(pending_key)
                logger.warning(
                    "[rag_text_relations] Background RAG sync queue full; "
                    "concept_id=%s namespace=%s remains manually reindexable",
                    clean_concept_id,
                    clean_namespace,
                )
                return {
                    **_QUEUE_RECEIPT_FIELDS,
                    "success": False,
                    "scheduled": False,
                    "skipped": False,
                    "reason": "queue_full",
                }
    except Exception as exc:  # primary-write receipt must not depend on maintenance
        with _RAG_SYNC_LOCK:
            _RAG_SYNC_PENDING_KEYS.discard(pending_key)
            _RAG_SYNC_RERUN_KEYS.discard(pending_key)
        logger.warning(
            "[rag_text_relations] Could not schedule background RAG sync "
            "concept_id=%s namespace=%s: %s",
            clean_concept_id,
            clean_namespace,
            exc,
        )
        return {
            **_QUEUE_RECEIPT_FIELDS,
            "success": False,
            "scheduled": False,
            "skipped": False,
            "reason": "schedule_failed",
            "error": f"RAG sync scheduling failed: {exc}",
        }
    return {
        **_QUEUE_RECEIPT_FIELDS,
        "success": True,
        "scheduled": True,
    }


def maybe_delete_text_relation_doc_from_rag(
    *,
    namespace: Optional[str],
    relation_id: Optional[str],
) -> Dict[str, Any]:
    """Best-effort: delete a single text relation doc from the RAG store."""

    if not namespace or not isinstance(namespace, str) or not namespace.strip():
        return {"success": False, "skipped": True, "reason": "namespace_required"}

    if not relation_id or not isinstance(relation_id, str) or not relation_id.strip():
        return {"success": False, "skipped": True, "reason": "relation_id_required"}

    doc_id = f"text_relation:{relation_id.strip()}"

    try:
        from src.backend.services.rag_service import get_rag_service

        rag = get_rag_service()
        deleted = rag.delete_documents([doc_id], namespace=namespace.strip())
        return {"success": True, "deleted": int(deleted), "doc_id": doc_id}
    except Exception as exc:
        logger.warning(
            "[rag_text_relations] RAG delete failed relation_id=%s namespace=%s: %s",
            relation_id,
            namespace,
            exc,
        )
        return {
            "success": False,
            "skipped": False,
            "error": f"RAG delete failed: {exc}",
            "doc_id": doc_id,
        }
