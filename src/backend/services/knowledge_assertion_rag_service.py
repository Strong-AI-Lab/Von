"""Durable, revision-aware RAG maintenance for standalone text assertions.

The canonical assertion row is also the queue record. Assertion admission never
depends on this worker, while leases and revision compare-and-set completion
make lost workers and stale upserts recoverable without a second transaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from pymongo import ReturnDocument

from ..db.mongo_client import get_scoped_knowledge_assertions_collection
from .rag_service import RAGBackendUnavailable, get_rag_service
from .scoped_assertion_service import STANDALONE_TEXT_ASSERTION_FORM

DEFAULT_LEASE_SECONDS = 120
MAX_BATCH_LIMIT = 100
MAX_ERROR_LENGTH = 500
MAX_RETRY_SECONDS = 3600


def _utc_now(value: datetime | None = None) -> datetime:
    candidate = value or datetime.now(timezone.utc)
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=timezone.utc)
    return candidate.astimezone(timezone.utc)


def _collection_or_default(collection: Any = None) -> Any:
    resolved = (
        collection
        if collection is not None
        else get_scoped_knowledge_assertions_collection()
    )
    if resolved is None:
        raise RuntimeError("text assertion storage is unavailable")
    return resolved


def _safe_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if parsed < 1:
        raise ValueError("limit must be at least 1")
    return min(parsed, MAX_BATCH_LIMIT)


def claim_next_assertion_rag_job(
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    collection: Any = None,
) -> dict[str, Any] | None:
    """Atomically claim one due standalone-assertion index operation."""

    clean_worker_id = str(worker_id or "").strip()
    if not clean_worker_id:
        raise ValueError("worker_id is required")
    current_time = _utc_now(now)
    lease_duration = max(1, int(lease_seconds))
    lease_token = str(uuid4())
    coll = _collection_or_default(collection)
    due_filter = {
        "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
        "object_kind": "text",
        "status": {"$in": ["asserted", "retracted"]},
        "$and": [
            {
                "$or": [
                    {
                        "status": "asserted",
                        "rag_index.desired_operation": "upsert",
                    },
                    {
                        "status": "retracted",
                        "rag_index.desired_operation": "delete",
                    },
                ]
            },
            {
                "$or": [
                    {
                        "rag_index.status": {"$in": ["pending", "failed"]},
                        "$or": [
                            {"rag_index.next_attempt_at": {"$lte": current_time}},
                            {"rag_index.next_attempt_at": None},
                            {"rag_index.next_attempt_at": {"$exists": False}},
                        ],
                    },
                    {
                        "rag_index.status": "processing",
                        "rag_index.lease_expires_at": {"$lte": current_time},
                    },
                ]
            },
        ],
    }
    claimed = coll.find_one_and_update(
        due_filter,
        {
            "$set": {
                "rag_index.status": "processing",
                "rag_index.worker_id": clean_worker_id,
                "rag_index.lease_token": lease_token,
                "rag_index.lease_expires_at": current_time
                + timedelta(seconds=lease_duration),
                "rag_index.last_attempt_at": current_time,
            },
            "$inc": {"rag_index.attempt_count": 1},
        },
        sort=[("rag_index.next_attempt_at", 1), ("updated_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return dict(claimed) if isinstance(claimed, Mapping) else None


def _bounded_provenance_metadata(assertion: Mapping[str, Any]) -> dict[str, Any]:
    provenance = assertion.get("provenance")
    if not isinstance(provenance, Mapping):
        return {}
    return {
        key: provenance.get(key)
        for key in (
            "asserted_by_user_concept_id",
            "organisation_concept_id",
            "capability_name",
        )
        if provenance.get(key) is not None
    }


def build_standalone_text_assertion_rag_document(
    assertion: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an exact-body RAG document without inventing relation semantics."""

    if assertion.get("assertion_form") != STANDALONE_TEXT_ASSERTION_FORM:
        raise ValueError("assertion is not a standalone text assertion")
    if assertion.get("status") != "asserted":
        raise ValueError("only asserted text assertions can be indexed")
    assertion_id = str(assertion.get("assertion_id") or "").strip()
    if not assertion_id.startswith("ska_"):
        raise ValueError("assertion has no valid assertion_id")
    revision = int(assertion.get("assertion_revision") or 0)
    if revision < 1:
        raise ValueError("assertion has no valid revision")
    object_text = assertion.get("object_text")
    if not isinstance(object_text, Mapping):
        raise ValueError("assertion has no text body")
    text = object_text.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("assertion has no text body")
    language = str(object_text.get("language") or "en-NZ")
    scope = assertion.get("scope")
    scope = scope if isinstance(scope, Mapping) else {}
    assertion_context = assertion.get("assertion_context")
    assertion_context = (
        assertion_context if isinstance(assertion_context, Mapping) else {}
    )
    metadata = {
        "type": "scoped_knowledge_assertion",
        "source": "scoped_knowledge_assertion",
        "row_kind": "text_assertion",
        "payload_kind": "text",
        "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
        "assertion_id": assertion_id,
        "assertion_revision": revision,
        "relation_id": None,
        "subject_concept_id": None,
        "predicate": None,
        "lang": language,
        "language": language,
        "content_length": len(text),
        "context_id": assertion_context.get("context_id"),
        "context_selection": assertion_context.get("selection"),
        "canonical_publication": False,
        "user_id": scope.get("user_concept_id"),
        "organisation_concept_id": scope.get("organisation_concept_id"),
        "org_id": scope.get("organisation_concept_id"),
        "audience_user_concept_id": scope.get("user_concept_id"),
        "audience_organisation_concept_id": scope.get("organisation_concept_id"),
        "audience_keys": list(scope.get("audience_keys") or ()),
        "assertion_provenance": _bounded_provenance_metadata(assertion),
        "created_at": (
            str(assertion.get("created_at")) if assertion.get("created_at") else None
        ),
        "updated_at": (
            str(assertion.get("updated_at")) if assertion.get("updated_at") else None
        ),
    }
    return {
        "id": f"scoped_assertion:{assertion_id}",
        "text": text,
        "metadata": metadata,
    }


def _retry_delay_seconds(attempt_count: Any) -> int:
    try:
        attempts = max(1, int(attempt_count))
    except (TypeError, ValueError):
        attempts = 1
    return min(2 ** min(attempts, 12), MAX_RETRY_SECONDS)


def complete_assertion_rag_job(
    *,
    assertion_id: str,
    claimed_revision: int,
    lease_token: str,
    operation: str,
    success: bool,
    error_code: str | None = None,
    error: str | None = None,
    now: datetime | None = None,
    attempt_count: int = 1,
    collection: Any = None,
) -> dict[str, Any]:
    """CAS-complete one job; a newer assertion revision always wins."""

    current_time = _utc_now(now)
    coll = _collection_or_default(collection)
    completion_filter = {
        "assertion_id": assertion_id,
        "assertion_revision": claimed_revision,
        "rag_index.desired_revision": claimed_revision,
        "rag_index.status": "processing",
        "rag_index.lease_token": lease_token,
    }
    if success:
        terminal_status = "indexed" if operation == "upsert" else "absent"
        update = {
            "$set": {
                "rag_index.status": terminal_status,
                "rag_index.indexed_revision": claimed_revision,
                "rag_index.last_success_at": current_time,
                "rag_index.last_error_code": None,
                "rag_index.last_error": None,
                "rag_index.next_attempt_at": None,
            },
            "$unset": {
                "rag_index.worker_id": "",
                "rag_index.lease_token": "",
                "rag_index.lease_expires_at": "",
            },
        }
    else:
        update = {
            "$set": {
                "rag_index.status": "failed",
                "rag_index.last_error_code": str(error_code or "rag_operation_failed")[
                    :100
                ],
                "rag_index.last_error": str(error or "RAG operation failed")[
                    :MAX_ERROR_LENGTH
                ],
                "rag_index.next_attempt_at": current_time
                + timedelta(seconds=_retry_delay_seconds(attempt_count)),
            },
            "$unset": {
                "rag_index.worker_id": "",
                "rag_index.lease_token": "",
                "rag_index.lease_expires_at": "",
            },
        }
    updated = coll.find_one_and_update(
        completion_filter,
        update,
        return_document=ReturnDocument.AFTER,
    )
    if not isinstance(updated, Mapping):
        return {
            "success": False,
            "completed": False,
            "superseded": True,
            "assertion_id": assertion_id,
            "claimed_revision": claimed_revision,
        }
    return {
        "success": success,
        "completed": True,
        "superseded": False,
        "assertion_id": assertion_id,
        "claimed_revision": claimed_revision,
        "rag_index": dict(updated.get("rag_index") or {}),
    }


def _requeue_current_revision_after_superseded_effect(
    *,
    assertion_id: str,
    claimed_revision: int,
    now: datetime,
    collection: Any,
) -> bool:
    """Ensure a stale backend effect cannot become the final physical state.

    A newer delete can finish before an older upsert (or conversely). When the
    older backend call returns, its completion CAS correctly misses, but that
    stale operation may nevertheless have changed the index after the newer
    job finished. Requeueing the current revision makes its desired state run
    once more after the stale effect.
    """

    current = collection.find_one(
        {
            "assertion_id": assertion_id,
            "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
        },
        {
            "_id": 0,
            "assertion_revision": 1,
            "status": 1,
        },
    )
    if not isinstance(current, Mapping):
        return False
    current_revision = int(current.get("assertion_revision") or 0)
    if current_revision < 1 or current_revision == claimed_revision:
        return False
    current_status = str(current.get("status") or "")
    if current_status not in {"asserted", "retracted"}:
        return False
    desired_operation = "delete" if current_status == "retracted" else "upsert"
    result = collection.update_one(
        {
            "assertion_id": assertion_id,
            "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
            "assertion_revision": current_revision,
        },
        {
            "$set": {
                "rag_index.status": "pending",
                "rag_index.desired_operation": desired_operation,
                "rag_index.desired_revision": current_revision,
                "rag_index.indexed_revision": None,
                "rag_index.next_attempt_at": now,
                "rag_index.lease_token": None,
                "rag_index.lease_expires_at": None,
                "rag_index.last_error_code": None,
                "rag_index.last_error": None,
            },
            "$unset": {"rag_index.worker_id": ""},
        },
    )
    return bool(result.modified_count)


def process_claimed_assertion_rag_job(
    claimed: Mapping[str, Any],
    *,
    rag_service: Any = None,
    now: datetime | None = None,
    collection: Any = None,
) -> dict[str, Any]:
    """Execute and complete one already-claimed exact upsert or delete."""

    assertion_id = str(claimed.get("assertion_id") or "").strip()
    revision = int(claimed.get("assertion_revision") or 0)
    rag_index = claimed.get("rag_index")
    rag_index = rag_index if isinstance(rag_index, Mapping) else {}
    lease_token = str(rag_index.get("lease_token") or "").strip()
    operation = str(rag_index.get("desired_operation") or "").strip()
    namespace = str(rag_index.get("namespace") or "").strip()
    attempt_count = int(rag_index.get("attempt_count") or 1)
    error_code: str | None = None
    error: str | None = None
    operation_success = False
    backend_attempted = False
    backend_receipt: dict[str, Any] = {}
    try:
        if not assertion_id or revision < 1 or not lease_token or not namespace:
            raise ValueError("claimed assertion RAG job is incomplete")
        if operation not in {"upsert", "delete"}:
            raise ValueError("claimed assertion RAG operation is invalid")
        service = rag_service or get_rag_service()
        doc_id = f"scoped_assertion:{assertion_id}"
        if operation == "upsert":
            payload = build_standalone_text_assertion_rag_document(claimed)
            backend_attempted = True
            indexed, failed = service.upsert_documents(
                [payload],
                namespace=namespace,
            )
            operation_success = int(indexed) == 1 and int(failed) == 0
            backend_receipt = {
                "indexed": int(indexed),
                "failed": int(failed),
                "doc_id": doc_id,
            }
            if not operation_success:
                error_code = "rag_upsert_incomplete"
                error = f"RAG upsert returned indexed={indexed}, failed={failed}"
        else:
            if claimed.get("status") != "retracted":
                raise ValueError("delete job does not match canonical assertion status")
            backend_attempted = True
            deleted = service.delete_documents([doc_id], namespace=namespace)
            # Zero is a successful idempotent desired-absence result.
            operation_success = True
            backend_receipt = {"deleted": int(deleted), "doc_id": doc_id}
    except RAGBackendUnavailable as exc:
        error_code = "rag_backend_unavailable"
        error = str(exc)
    except Exception as exc:
        error_code = type(exc).__name__
        error = str(exc)

    completion = complete_assertion_rag_job(
        assertion_id=assertion_id,
        claimed_revision=revision,
        lease_token=lease_token,
        operation=operation,
        success=operation_success,
        error_code=error_code,
        error=error,
        now=now,
        attempt_count=attempt_count,
        collection=collection,
    )
    requeued_current_revision = False
    if completion.get("superseded") and backend_attempted:
        requeued_current_revision = _requeue_current_revision_after_superseded_effect(
            assertion_id=assertion_id,
            claimed_revision=revision,
            now=_utc_now(now),
            collection=_collection_or_default(collection),
        )
    return {
        **completion,
        "operation": operation,
        "namespace": namespace,
        "backend_receipt": backend_receipt,
        "current_revision_requeued": requeued_current_revision,
        **({"error_code": error_code, "error": error} if error else {}),
    }


def process_pending_assertion_rag_jobs(
    *,
    limit: int = 10,
    worker_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    rag_service: Any = None,
    now: datetime | None = None,
    collection: Any = None,
) -> dict[str, Any]:
    """Process a bounded synchronous batch for the existing RAG worker loop."""

    batch_limit = _safe_limit(limit)
    effective_worker_id = str(worker_id or f"assertion-rag-{uuid4()}")
    counts = {
        "claimed": 0,
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "superseded": 0,
    }
    receipts: list[dict[str, Any]] = []
    for _ in range(batch_limit):
        claimed = claim_next_assertion_rag_job(
            worker_id=effective_worker_id,
            now=now,
            lease_seconds=lease_seconds,
            collection=collection,
        )
        if claimed is None:
            break
        counts["claimed"] += 1
        receipt = process_claimed_assertion_rag_job(
            claimed,
            rag_service=rag_service,
            now=now,
            collection=collection,
        )
        receipts.append(receipt)
        if receipt.get("completed"):
            counts["completed"] += 1
        if receipt.get("superseded"):
            counts["superseded"] += 1
        elif receipt.get("success"):
            counts["succeeded"] += 1
        else:
            counts["failed"] += 1
    return {"success": counts["failed"] == 0, **counts, "receipts": receipts}


def reconcile_assertion_rag_state(
    *,
    limit: int = 100,
    include_failed: bool = False,
    now: datetime | None = None,
    collection: Any = None,
) -> dict[str, Any]:
    """Repair missing/mismatched operational state without touching RAG itself."""

    batch_limit = _safe_limit(limit)
    current_time = _utc_now(now)
    coll = _collection_or_default(collection)
    problem_filters: list[dict[str, Any]] = [
        {"rag_index": {"$exists": False}},
        {"rag_index.status": {"$exists": False}},
        {"rag_index.status": {"$in": [None, ""]}},
        {"rag_index.namespace": {"$exists": False}},
        {"rag_index.namespace": {"$in": [None, ""]}},
        {
            "$expr": {
                "$ne": [
                    "$rag_index.desired_revision",
                    "$assertion_revision",
                ]
            }
        },
        {
            "status": "asserted",
            "rag_index.desired_operation": {"$ne": "upsert"},
        },
        {
            "status": "retracted",
            "rag_index.desired_operation": {"$ne": "delete"},
        },
        {
            "rag_index.status": "processing",
            "rag_index.lease_expires_at": {"$lte": current_time},
        },
        {
            "rag_index.status": "processing",
            "rag_index.lease_expires_at": {"$exists": False},
        },
        {
            "rag_index.status": "processing",
            "rag_index.lease_expires_at": None,
        },
        {
            "rag_index.status": "processing",
            "rag_index.lease_expires_at": {"$not": {"$type": "date"}},
        },
        {
            "rag_index.status": {"$in": ["indexed", "absent"]},
            "$expr": {
                "$ne": [
                    "$rag_index.indexed_revision",
                    "$assertion_revision",
                ]
            },
        },
        {
            "rag_index.status": {
                "$nin": [
                    "pending",
                    "processing",
                    "indexed",
                    "absent",
                    "failed",
                ]
            }
        },
        {"status": "asserted", "rag_index.status": "absent"},
        {"status": "retracted", "rag_index.status": "indexed"},
    ]
    if include_failed:
        problem_filters.append({"rag_index.status": "failed"})
    candidates = list(
        coll.find(
            {
                "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
                "object_kind": "text",
                "status": {"$in": ["asserted", "retracted"]},
                "$or": problem_filters,
            }
        )
        .sort([("updated_at", 1), ("assertion_id", 1)])
        .limit(batch_limit)
    )
    repaired = 0
    for assertion in candidates:
        if not isinstance(assertion, Mapping):
            continue
        revision = int(assertion.get("assertion_revision") or 1)
        desired_operation = (
            "delete" if assertion.get("status") == "retracted" else "upsert"
        )
        rag_index = assertion.get("rag_index")
        rag_index = rag_index if isinstance(rag_index, Mapping) else {}
        status = str(rag_index.get("status") or "")
        lease_expires_at = rag_index.get("lease_expires_at")
        invalid_or_expired_lease = bool(
            status == "processing"
            and (
                not isinstance(lease_expires_at, datetime)
                or _utc_now(lease_expires_at) <= current_time
            )
        )
        valid_statuses = {"pending", "processing", "indexed", "absent", "failed"}
        invalid_status = status not in valid_statuses
        terminal_revision_mismatch = bool(
            status in {"indexed", "absent"}
            and int(rag_index.get("indexed_revision") or 0) != revision
        )
        terminal_operation_mismatch = bool(
            (assertion.get("status") == "asserted" and status == "absent")
            or (assertion.get("status") == "retracted" and status == "indexed")
        )
        mismatch = bool(
            int(rag_index.get("desired_revision") or 0) != revision
            or rag_index.get("desired_operation") != desired_operation
            or not str(rag_index.get("namespace") or "").strip()
        )
        should_repair = (
            not status
            or mismatch
            or invalid_or_expired_lease
            or invalid_status
            or terminal_revision_mismatch
            or terminal_operation_mismatch
            or (include_failed and status == "failed")
        )
        if not should_repair:
            continue
        scope = assertion.get("scope")
        scope = scope if isinstance(scope, Mapping) else {}
        provenance = assertion.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        namespace = str(
            rag_index.get("namespace")
            or provenance.get("namespace")
            or scope.get("namespace")
            or ""
        ).strip()
        if not namespace:
            continue
        result = coll.update_one(
            {
                "assertion_id": assertion.get("assertion_id"),
                "assertion_revision": assertion.get("assertion_revision"),
            },
            {
                "$set": {
                    "rag_index": {
                        "status": "pending",
                        "desired_operation": desired_operation,
                        "desired_revision": revision,
                        "indexed_revision": None,
                        "namespace": namespace,
                        "attempt_count": int(rag_index.get("attempt_count") or 0),
                        "next_attempt_at": current_time,
                        "lease_token": None,
                        "lease_expires_at": None,
                        "last_attempt_at": rag_index.get("last_attempt_at"),
                        "last_success_at": rag_index.get("last_success_at"),
                        "last_error_code": None,
                        "last_error": None,
                    }
                }
            },
        )
        repaired += int(result.modified_count)
    return {
        "success": True,
        "scanned": len(candidates),
        "repaired": repaired,
        "limit": batch_limit,
    }


__all__ = [
    "build_standalone_text_assertion_rag_document",
    "claim_next_assertion_rag_job",
    "complete_assertion_rag_job",
    "process_claimed_assertion_rag_job",
    "process_pending_assertion_rag_jobs",
    "reconcile_assertion_rag_state",
]
