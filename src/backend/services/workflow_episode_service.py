"""Workflow usage episode tracking and aggregate synchronisation.

This module is the canonical path for recording workflow-use episodes across
execution surfaces (chat-turn orchestration and durable workers) and keeping
workflow concept usage aggregates up to date.

Design notes:
1. Episodes are idempotent when callers provide a stable key.
2. Aggregates are recomputed from episode documents to avoid double counting.
3. Workflow concept aggregates are stored under
   ``concept_data.workflow_use_aggregates`` for monitor/UI queries.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure

from ..db.mongo_client import get_db
from ..db.repositories.concepts_repository import ConceptsRepository

logger = logging.getLogger(__name__)

WORKFLOW_USE_EPISODES_COLLECTION = "workflow_use_episodes"

_indexes_ensured = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _namespace_equivalents(namespace: str | None) -> list[str]:
    """Return deterministic namespace variants for cross-format compatibility.

    Workflow episodes have historically used both ``user/org`` and
    ``#V#user@org`` forms. The monitor should treat these as equivalent when
    querying episodes, while still remaining namespace-scoped. New workflow
    launches should emit canonical namespaces; this helper is read-side
    compatibility for historical data only.
    """
    clean_namespace = _safe_str(namespace)
    if clean_namespace is None:
        return []

    values: list[str] = [clean_namespace]

    def _add(candidate: str | None) -> None:
        candidate_clean = _safe_str(candidate)
        if candidate_clean and candidate_clean not in values:
            values.append(candidate_clean)

    user_part: str | None = None
    org_part: str | None = None

    if "@" in clean_namespace:
        user_raw, org_raw = clean_namespace.split("@", 1)
        user_part = _safe_str(user_raw)
        org_part = _safe_str(org_raw)
        if user_part and org_part:
            _add(f"{user_part}/{org_part}")
            if user_part.startswith("#V#") and org_part and not org_part.startswith("#V#"):
                _add(f"{user_part}/#V#{org_part}")

    if "/" in clean_namespace:
        user_raw, org_raw = clean_namespace.split("/", 1)
        user_part = _safe_str(user_raw) or user_part
        org_part = _safe_str(org_raw) or org_part
        if user_part and org_part:
            _add(f"{user_part}@{org_part}")
            if user_part.startswith("#V#") and org_part.startswith("#V#"):
                _add(f"{user_part}@{org_part[3:]}")
            if user_part.startswith("#V#") and org_part and not org_part.startswith("#V#"):
                _add(f"{user_part}@{org_part}")

    # Cross-era compatibility: some historical writes used user-only namespace
    # or user/default placeholders before org-scoped namespaces were stable.
    if user_part:
        _add(user_part)
        _add(f"{user_part}/default")
        if user_part.startswith("#V#"):
            _add(f"{user_part}/#V#default")
    elif clean_namespace.startswith("#V#"):
        _add(f"{clean_namespace}/default")
        _add(f"{clean_namespace}/#V#default")

    return values


def _build_episode_query(
    *,
    workflow_id: str | None = None,
    workflow_ids: Iterable[str] | None = None,
    namespace: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Build a normalised Mongo query for workflow episode filtering."""

    query: dict[str, Any] = {}

    cleaned_ids = [
        item.strip()
        for item in (workflow_ids or [])
        if isinstance(item, str) and item.strip()
    ]
    if cleaned_ids:
        query["workflow_id"] = {"$in": sorted(set(cleaned_ids))}
    else:
        clean_workflow_id = _safe_str(workflow_id)
        if clean_workflow_id:
            query["workflow_id"] = clean_workflow_id

    clean_namespace = _safe_str(namespace)
    if clean_namespace:
        namespace_values = _namespace_equivalents(clean_namespace)
        if len(namespace_values) <= 1:
            query["namespace"] = clean_namespace
        else:
            query["namespace"] = {"$in": namespace_values}

    clean_session_id = _safe_str(session_id)
    if clean_session_id:
        query["session_id"] = clean_session_id

    clean_turn_id = _safe_str(turn_id)
    if clean_turn_id:
        query["turn_id"] = clean_turn_id

    return query


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def build_workflow_episode_stable_key(
    *,
    workflow_id: str,
    source: str,
    turn_id: str | None = None,
    session_id: str | None = None,
    instance_id: str | None = None,
    attempt_number: int | None = None,
    stage: str | None = None,
) -> str:
    """Build a deterministic stable key for idempotent episode writes."""

    parts = [workflow_id.strip(), source.strip()]
    clean_turn_id = _safe_str(turn_id)
    clean_session_id = _safe_str(session_id)
    clean_instance_id = _safe_str(instance_id)
    clean_stage = _safe_str(stage)

    if clean_turn_id:
        parts.append(f"turn:{clean_turn_id}")
    if clean_session_id:
        parts.append(f"session:{clean_session_id}")
    if clean_instance_id:
        parts.append(f"instance:{clean_instance_id}")
    if isinstance(attempt_number, int) and attempt_number > 0:
        parts.append(f"attempt:{attempt_number}")
    if clean_stage:
        parts.append(f"stage:{clean_stage}")
    return "|".join(parts)


def _build_episode_id(stable_key: str | None = None) -> str:
    if isinstance(stable_key, str) and stable_key.strip():
        digest = hashlib.sha256(stable_key.strip().encode("utf-8")).hexdigest()[:24]
        return f"wfep_{digest}"
    return str(uuid.uuid4())


def _get_collection():
    _ensure_indexes()
    db = get_db()
    if db is None:
        return None
    return db[WORKFLOW_USE_EPISODES_COLLECTION]


def _ensure_indexes() -> None:
    global _indexes_ensured
    if _indexes_ensured:
        return

    db = get_db()
    if db is None:
        return

    coll = db[WORKFLOW_USE_EPISODES_COLLECTION]
    try:
        existing = [idx.get("name") for idx in coll.list_indexes()]
        if "episode_id_unique" not in existing:
            coll.create_index(
                [("episode_id", ASCENDING)],
                unique=True,
                name="episode_id_unique",
            )
        if "workflow_attempted_at" not in existing:
            coll.create_index(
                [("workflow_id", ASCENDING), ("attempt_started_at", DESCENDING)],
                name="workflow_attempted_at",
            )
        if "namespace_attempted_at" not in existing:
            coll.create_index(
                [("namespace", ASCENDING), ("attempt_started_at", DESCENDING)],
                name="namespace_attempted_at",
            )
        if "turn_attempted_at" not in existing:
            coll.create_index(
                [("turn_id", ASCENDING), ("attempt_started_at", DESCENDING)],
                name="turn_attempted_at",
            )
        if "session_attempted_at" not in existing:
            coll.create_index(
                [("session_id", ASCENDING), ("attempt_started_at", DESCENDING)],
                name="session_attempted_at",
            )
    except OperationFailure as exc:
        logger.warning("[workflow_episode] Index creation partially failed: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[workflow_episode] Could not create indexes: %s", exc)
    finally:
        _indexes_ensured = True


def _serialise_episode(doc: Mapping[str, Any]) -> dict[str, Any]:
    reason_raw = doc.get("termination_reason")
    reason: dict[str, Any] | None = None
    if isinstance(reason_raw, Mapping):
        reason = {
            "code": _safe_str(reason_raw.get("code")),
            "detail": _safe_str(reason_raw.get("detail")),
        }
    metadata_raw = doc.get("metadata")
    metadata = metadata_raw if isinstance(metadata_raw, Mapping) else {}
    workflow_definition_identity = metadata.get("workflow_definition_identity")
    if not isinstance(workflow_definition_identity, Mapping):
        workflow_definition_identity = None

    return {
        "episode_id": _safe_str(doc.get("episode_id")),
        "workflow_id": _safe_str(doc.get("workflow_id")),
        "source": _safe_str(doc.get("source")),
        "status": _safe_str(doc.get("status")),
        "completed": bool(doc.get("completed")),
        "terminal_stage": _safe_str(doc.get("terminal_stage")),
        "final_state": _safe_str(doc.get("final_state")),
        "termination_reason": reason,
        "attempt_started_at": _iso_or_none(doc.get("attempt_started_at")),
        "updated_at": _iso_or_none(doc.get("updated_at")),
        "completed_at": _iso_or_none(doc.get("completed_at")),
        "namespace": _safe_str(doc.get("namespace")),
        "session_id": _safe_str(doc.get("session_id")),
        "turn_id": _safe_str(doc.get("turn_id")),
        "user_id": _safe_str(doc.get("user_id")),
        "org_id": _safe_str(doc.get("org_id")),
        "instance_id": _safe_str(doc.get("instance_id")),
        "stable_key": _safe_str(doc.get("stable_key")),
        "workflow_definition_identity": (
            dict(workflow_definition_identity)
            if isinstance(workflow_definition_identity, Mapping)
            else None
        ),
    }


def _compute_episode_aggregate(workflow_id: str) -> dict[str, Any]:
    coll = _get_collection()
    if coll is None:
        return {
            "workflow_id": workflow_id,
            "attempts": 0,
            "completions": 0,
            "completion_rate": None,
            "last_episode_at": None,
            "updated_at": _utcnow().isoformat(),
        }

    pipeline = [
        {"$match": {"workflow_id": workflow_id}},
        {
            "$group": {
                "_id": "$workflow_id",
                "attempts": {"$sum": 1},
                "completions": {
                    "$sum": {"$cond": [{"$eq": ["$completed", True]}, 1, 0]}
                },
                "last_episode_at": {"$max": "$attempt_started_at"},
            }
        },
    ]
    grouped = list(coll.aggregate(pipeline))
    if not grouped:
        return {
            "workflow_id": workflow_id,
            "attempts": 0,
            "completions": 0,
            "completion_rate": None,
            "last_episode_at": None,
            "updated_at": _utcnow().isoformat(),
        }

    attempts = int(grouped[0].get("attempts", 0))
    completions = int(grouped[0].get("completions", 0))
    completion_rate = (completions / attempts) if attempts > 0 else None
    return {
        "workflow_id": workflow_id,
        "attempts": attempts,
        "completions": completions,
        "completion_rate": completion_rate,
        "last_episode_at": _iso_or_none(grouped[0].get("last_episode_at")),
        "updated_at": _utcnow().isoformat(),
    }


def sync_workflow_concept_aggregates(workflow_id: str) -> dict[str, Any]:
    """Recompute and persist usage aggregates for a workflow concept."""

    cleaned_workflow_id = _safe_str(workflow_id)
    if not cleaned_workflow_id:
        return {
            "workflow_id": workflow_id,
            "attempts": 0,
            "completions": 0,
            "completion_rate": None,
            "last_episode_at": None,
            "updated_at": _utcnow().isoformat(),
            "concept_found": False,
        }

    aggregate = _compute_episode_aggregate(cleaned_workflow_id)
    aggregate["concept_found"] = False

    concept_doc = ConceptsRepository.find_one(
        {"concept_id": cleaned_workflow_id},
        projection={"concept_id": 1},
    )
    if not concept_doc:
        return aggregate

    try:
        ConceptsRepository.update_one(
            {"concept_id": cleaned_workflow_id},
            {
                "$set": {
                    "concept_data.workflow_use_aggregates.attempts": aggregate[
                        "attempts"
                    ],
                    "concept_data.workflow_use_aggregates.completions": aggregate[
                        "completions"
                    ],
                    "concept_data.workflow_use_aggregates.completion_rate": aggregate[
                        "completion_rate"
                    ],
                    "concept_data.workflow_use_aggregates.last_episode_at": aggregate[
                        "last_episode_at"
                    ],
                    "concept_data.workflow_use_aggregates.updated_at": aggregate[
                        "updated_at"
                    ],
                }
            },
        )
        aggregate["concept_found"] = True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[workflow_episode] Failed to sync concept aggregate for %s: %s",
            cleaned_workflow_id,
            exc,
        )
    return aggregate


def start_workflow_use_episode(
    *,
    workflow_id: str,
    source: str,
    namespace: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    instance_id: str | None = None,
    stable_key: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create or upsert a workflow-use episode attempt."""

    cleaned_workflow_id = _safe_str(workflow_id)
    cleaned_source = _safe_str(source)
    if not cleaned_workflow_id or not cleaned_source:
        return None

    coll = _get_collection()
    if coll is None:
        return None

    now = _utcnow()
    episode_id = _build_episode_id(stable_key)
    stable_key_clean = _safe_str(stable_key)
    safe_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}

    set_fields: dict[str, Any] = {
        "updated_at": now,
        "source": cleaned_source,
    }
    for key, value in (
        ("namespace", _safe_str(namespace)),
        ("session_id", _safe_str(session_id)),
        ("turn_id", _safe_str(turn_id)),
        ("user_id", _safe_str(user_id)),
        ("org_id", _safe_str(org_id)),
        ("instance_id", _safe_str(instance_id)),
        ("stable_key", stable_key_clean),
    ):
        if value is not None:
            set_fields[key] = value
    if safe_metadata:
        set_fields["metadata"] = safe_metadata

    result = coll.update_one(
        {"episode_id": episode_id},
        {
            "$setOnInsert": {
                "episode_id": episode_id,
                "workflow_id": cleaned_workflow_id,
                "status": "attempted",
                "completed": False,
                "attempt_started_at": now,
                "created_at": now,
            },
            "$set": set_fields,
        },
        upsert=True,
    )

    aggregate = sync_workflow_concept_aggregates(cleaned_workflow_id)
    return {
        "episode_id": episode_id,
        "workflow_id": cleaned_workflow_id,
        "created": bool(result.upserted_id),
        "aggregate": aggregate,
    }


def finalise_workflow_use_episode(
    *,
    workflow_id: str,
    completed: bool,
    terminal_stage: str | None,
    episode_id: str | None = None,
    stable_key: str | None = None,
    final_state: str | None = None,
    termination_code: str | None = None,
    termination_detail: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Mark an episode completed or terminated."""

    cleaned_workflow_id = _safe_str(workflow_id)
    if not cleaned_workflow_id:
        return None

    resolved_episode_id = _safe_str(episode_id)
    if resolved_episode_id is None:
        resolved_episode_id = _build_episode_id(stable_key)
    if not resolved_episode_id:
        return None

    coll = _get_collection()
    if coll is None:
        return None

    now = _utcnow()
    clean_stage = _safe_str(terminal_stage) or "unknown"
    clean_code = _safe_str(termination_code)
    clean_detail = _safe_str(termination_detail)
    if completed and clean_code is None:
        clean_code = "completed"
    if (not completed) and clean_code is None:
        clean_code = "terminated"

    safe_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}

    set_fields: dict[str, Any] = {
        "updated_at": now,
        "workflow_id": cleaned_workflow_id,
        "status": "completed" if completed else "terminated",
        "completed": bool(completed),
        "terminal_stage": clean_stage,
        "final_state": _safe_str(final_state),
        "termination_reason": {"code": clean_code, "detail": clean_detail},
        "completed_at": now,
    }
    if safe_metadata:
        set_fields["metadata"] = safe_metadata

    coll.update_one(
        {"episode_id": resolved_episode_id},
        {
            "$setOnInsert": {
                "episode_id": resolved_episode_id,
                "attempt_started_at": now,
                "created_at": now,
                "source": "unknown",
            },
            "$set": set_fields,
        },
        upsert=True,
    )

    aggregate = sync_workflow_concept_aggregates(cleaned_workflow_id)
    return {
        "episode_id": resolved_episode_id,
        "workflow_id": cleaned_workflow_id,
        "completed": bool(completed),
        "terminal_stage": clean_stage,
        "termination_reason": {"code": clean_code, "detail": clean_detail},
        "aggregate": aggregate,
    }


def list_workflow_use_episodes(
    *,
    workflow_id: str | None = None,
    namespace: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    coll = _get_collection()
    if coll is None:
        return []

    safe_limit = max(1, min(int(limit), 200))
    query = _build_episode_query(
        workflow_id=workflow_id,
        namespace=namespace,
        session_id=session_id,
        turn_id=turn_id,
    )

    cursor = coll.find(query).sort("attempt_started_at", DESCENDING).limit(safe_limit)
    return [_serialise_episode(doc) for doc in cursor]


def get_latest_workflow_use_episode(
    *,
    workflow_id: str | None = None,
    namespace: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the newest workflow-use episode matching the supplied filters."""

    coll = _get_collection()
    if coll is None:
        return None

    query = _build_episode_query(
        workflow_id=workflow_id,
        namespace=namespace,
        session_id=session_id,
        turn_id=turn_id,
    )
    doc = coll.find_one(query, sort=[("attempt_started_at", DESCENDING)])
    if not isinstance(doc, Mapping):
        return None
    return _serialise_episode(doc)


def count_workflow_use_episodes(
    *,
    workflow_id: str | None = None,
    namespace: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> int:
    """Return the number of workflow-use episodes matching supplied filters."""

    coll = _get_collection()
    if coll is None:
        return 0

    query = _build_episode_query(
        workflow_id=workflow_id,
        namespace=namespace,
        session_id=session_id,
        turn_id=turn_id,
    )
    return int(coll.count_documents(query))


def get_workflow_episode_counts_for_workflows(
    workflow_ids: Iterable[str],
    *,
    namespace: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, int]:
    """Return episode counts keyed by workflow ID for monitor summaries."""

    ids = [
        item.strip()
        for item in workflow_ids
        if isinstance(item, str) and item.strip()
    ]
    if not ids:
        return {}

    unique_ids = sorted(set(ids))
    counts: dict[str, int] = {workflow_id: 0 for workflow_id in unique_ids}

    coll = _get_collection()
    if coll is None:
        return counts

    query = _build_episode_query(
        workflow_ids=unique_ids,
        namespace=namespace,
        session_id=session_id,
        turn_id=turn_id,
    )
    pipeline = [
        {"$match": query},
        {"$group": {"_id": "$workflow_id", "count": {"$sum": 1}}},
    ]
    for row in coll.aggregate(pipeline):
        workflow_id = _safe_str(row.get("_id"))
        if workflow_id is None or workflow_id not in counts:
            continue
        counts[workflow_id] = int(row.get("count", 0))
    return counts


def get_workflow_usage_aggregates_for_workflows(
    workflow_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Return attempts/completions aggregates keyed by workflow_id."""

    ids = [
        item.strip()
        for item in workflow_ids
        if isinstance(item, str) and item.strip()
    ]
    if not ids:
        return {}

    unique_ids = sorted(set(ids))
    aggregate_map: dict[str, dict[str, Any]] = {
        workflow_id: {
            "attempts": 0,
            "completions": 0,
            "completion_rate": None,
            "last_episode_at": None,
            "updated_at": None,
        }
        for workflow_id in unique_ids
    }

    projection = {
        "concept_id": 1,
        "concept_data.workflow_use_aggregates": 1,
    }
    for doc in ConceptsRepository.find(
        {"concept_id": {"$in": unique_ids}},
        projection=projection,
    ):
        concept_id = _safe_str(doc.get("concept_id"))
        if concept_id is None or concept_id not in aggregate_map:
            continue

        concept_data = doc.get("concept_data")
        usage = None
        if isinstance(concept_data, Mapping):
            usage_raw = concept_data.get("workflow_use_aggregates")
            if isinstance(usage_raw, Mapping):
                usage = usage_raw

        if not usage:
            continue

        attempts = usage.get("attempts")
        completions = usage.get("completions")
        completion_rate = usage.get("completion_rate")
        aggregate_map[concept_id] = {
            "attempts": int(attempts) if isinstance(attempts, (int, float)) else 0,
            "completions": (
                int(completions) if isinstance(completions, (int, float)) else 0
            ),
            "completion_rate": (
                float(completion_rate)
                if isinstance(completion_rate, (int, float))
                else None
            ),
            "last_episode_at": _iso_or_none(usage.get("last_episode_at")),
            "updated_at": _iso_or_none(usage.get("updated_at")),
        }

    # Fallback: derive from episode logs when concept aggregates are not populated.
    coll = _get_collection()
    if coll is not None:
        pipeline = [
            {"$match": {"workflow_id": {"$in": unique_ids}}},
            {
                "$group": {
                    "_id": "$workflow_id",
                    "attempts": {"$sum": 1},
                    "completions": {
                        "$sum": {"$cond": [{"$eq": ["$completed", True]}, 1, 0]}
                    },
                    "last_episode_at": {"$max": "$attempt_started_at"},
                }
            },
        ]
        grouped = list(coll.aggregate(pipeline))
        for row in grouped:
            workflow_id = _safe_str(row.get("_id"))
            if workflow_id is None or workflow_id not in aggregate_map:
                continue
            attempts = int(row.get("attempts", 0))
            completions = int(row.get("completions", 0))
            completion_rate = (completions / attempts) if attempts > 0 else None
            aggregate_map[workflow_id] = {
                "attempts": attempts,
                "completions": completions,
                "completion_rate": completion_rate,
                "last_episode_at": _iso_or_none(row.get("last_episode_at")),
                "updated_at": aggregate_map[workflow_id].get("updated_at"),
            }

    return aggregate_map
