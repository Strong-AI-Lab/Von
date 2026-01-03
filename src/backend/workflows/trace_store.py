from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..db.mongo_client import get_db

logger = logging.getLogger(__name__)

WORKFLOW_EXECUTIONS_COLLECTION_NAME = "workflow_executions"

_indexes_ensured = False


def _ensure_indexes() -> None:
    """Create the minimal indexes for workflow tracing (best effort)."""

    global _indexes_ensured
    if _indexes_ensured:
        return

    db = get_db()
    if db is None:
        return

    try:
        from pymongo import DESCENDING

        coll = db[WORKFLOW_EXECUTIONS_COLLECTION_NAME]
        # Common query patterns
        coll.create_index([("workflow_id", DESCENDING), ("start_time", DESCENDING)])
        coll.create_index([("execution_id", DESCENDING)], unique=True)
        coll.create_index([("user_namespace", DESCENDING), ("start_time", DESCENDING)])

        # TTL for routine traces: 30 days.
        # Using start_time rather than end_time ensures even crashed traces expire.
        coll.create_index("start_time", expireAfterSeconds=30 * 24 * 60 * 60)
    except Exception as exc:  # pragma: no cover - depends on DB backend
        logger.debug("[workflow_trace] Index creation skipped/failed: %s", exc)
    finally:
        _indexes_ensured = True


def insert_workflow_execution_trace(trace_doc: Dict[str, Any]) -> Optional[str]:
    """Insert a workflow execution trace document.

    Returns the execution_id when stored, otherwise None.
    """

    if not isinstance(trace_doc, dict):
        return None

    execution_id = trace_doc.get("execution_id")
    if not isinstance(execution_id, str) or not execution_id.strip():
        return None

    db = get_db()
    if db is None:
        return None

    _ensure_indexes()

    # Normalise timestamps if they were serialised as strings.
    start_time = trace_doc.get("start_time")
    if isinstance(start_time, str):
        try:
            trace_doc["start_time"] = datetime.fromisoformat(start_time)
        except Exception:
            trace_doc["start_time"] = datetime.now(timezone.utc)

    coll = db[WORKFLOW_EXECUTIONS_COLLECTION_NAME]
    try:
        coll.insert_one(trace_doc)
        return execution_id
    except Exception as exc:  # pragma: no cover - DB dependent
        logger.warning(
            "[workflow_trace] Failed to store trace execution_id=%s: %s",
            execution_id,
            exc,
        )
        return None


def get_workflow_execution_trace(execution_id: str) -> Optional[Dict[str, Any]]:
    db = get_db()
    if db is None:
        return None

    coll = db[WORKFLOW_EXECUTIONS_COLLECTION_NAME]
    try:
        doc = coll.find_one({"execution_id": execution_id}, {"_id": 0})
        return doc if isinstance(doc, dict) else None
    except Exception:  # pragma: no cover
        return None


def list_recent_workflow_execution_traces(
    limit: int = 20, *, namespace: str | None = None
) -> List[Dict[str, Any]]:
    db = get_db()
    if db is None:
        return []

    _ensure_indexes()

    coll = db[WORKFLOW_EXECUTIONS_COLLECTION_NAME]
    try:
        query: Dict[str, Any] = {}
        if isinstance(namespace, str) and namespace.strip():
            query["user_namespace"] = namespace.strip()
        cursor = (
            coll.find(query, {"_id": 0})
            .sort("start_time", -1)
            .limit(max(1, min(200, int(limit))))
        )
        return [doc for doc in cursor if isinstance(doc, dict)]
    except Exception:  # pragma: no cover
        return []
