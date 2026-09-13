"""Semantic task retrieval over canonical, actor-visible snapshots.

The SQLite index is disposable derived storage, never an authority for task
text or visibility. It stores only vectors and revision hashes. Every search
reads and filters canonical tasks before consulting the index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from ..security import access_control
from ..db.repositories.concepts_repository import ConceptsRepository
from .rag_service import get_rag_service

logger = logging.getLogger(__name__)
DOCUMENT_VERSION = "task_document.v1"
DOCUMENT_FIELDS = (
    "task_concept_id",
    "title",
    "description",
    "status",
    "priority",
    "assignee_concept_id",
    "created_by_concept_id",
    "organisation_concept_id",
    "project_concept_id",
    "collection_concept_ids",
    "labels",
    "components",
    "task_source_id",
    "external_references",
    "reference_code",
    "created_at",
    "updated_at",
    "start_date",
    "due_date",
)
BATCH_SIZE = 32


def build_task_document(task: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve task identity, context and provenance without promoting text to policy."""
    if not task.get("task_concept_id"):
        raise ValueError("Task identity is required")
    fields = {key: task.get(key) for key in DOCUMENT_FIELDS}
    text = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    revision = hashlib.sha256((DOCUMENT_VERSION + text).encode()).hexdigest()
    return {
        "id": task["task_concept_id"],
        "text": text,
        "revision": revision,
        "schema_version": DOCUMENT_VERSION,
        "source": "canonical_task_service",
    }


def _scope() -> str:
    # Scope is derived from the server actor, never query filter identifiers.
    return json.dumps(
        [
            access_control.get_effective_user_concept_id(),
            access_control.get_effective_organisation_concept_id(),
        ]
    )


def _index_path() -> Path:
    return Path(
        os.environ.get(
            "VON_TASK_SEMANTIC_INDEX_PATH", "data/task_semantic_index.sqlite3"
        )
    )


def _connect() -> sqlite3.Connection:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Vectors are private derived data even though raw task text is not stored.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS task_vectors ("
            "scope TEXT NOT NULL, task_id TEXT NOT NULL, revision TEXT NOT NULL, "
            "model TEXT NOT NULL, vector TEXT NOT NULL, touched REAL NOT NULL, "
            "PRIMARY KEY(scope, task_id))"
        )
    except Exception:
        connection.close()
        raise
    return connection


def clear_task_semantic_index() -> int:
    """Discard the current trusted actor's derived cache; next search rebuilds it."""
    connection = _connect()
    try:
        with connection:
            return connection.execute(
                "DELETE FROM task_vectors WHERE scope = ?", (_scope(),)
            ).rowcount
    finally:
        connection.close()


def _normalise_vector(value: Any) -> list[float]:
    vector = [float(component) for component in value]
    if not vector or not all(math.isfinite(component) for component in vector):
        raise ValueError("Invalid embedding")
    magnitude = math.sqrt(sum(component * component for component in vector))
    if not math.isfinite(magnitude) or magnitude == 0:
        raise ValueError("Invalid embedding norm")
    return [component / magnitude for component in vector]


def rank_tasks(
    tasks: list[dict[str, Any]], query: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Index current filtered tasks and rank by cosine similarity.

    No cached text or cached candidate IDs may enter the result. Replacing or
    pruning a concurrent cache entry affects cache efficiency only: each search
    retains the vectors matching its own canonical snapshot in memory.
    """
    started = time.monotonic()
    documents = [build_task_document(task) for task in tasks]
    diagnostics: dict[str, Any] = {
        "status": "ready",
        "document_version": DOCUMENT_VERSION,
        "candidates": len(tasks),
        "cache_hits": 0,
        "embedded": 0,
        "ranking": "cosine",
        "source": "canonical_task_service",
    }
    if not tasks:
        clear_task_semantic_index()
        diagnostics["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        return [], diagnostics

    service = get_rag_service()
    # Pin the actual embedder for this operation; a runtime setting change must
    # not mix vector spaces between the query and successive document batches.
    capture = getattr(service, "_capture_embedding_runtime", None)
    if not callable(capture):
        raise RuntimeError("Task indexing requires a versioned embedding runtime")
    summary, model, _ = capture("task_semantic_index")
    model_key = json.dumps(summary["embedding_signature"], sort_keys=True)
    diagnostics["embedding_version"] = hashlib.sha256(model_key.encode()).hexdigest()
    query_vector = _normalise_vector(model.get_query_embedding(query))
    vectors: dict[str, list[float]] = {}
    scope = _scope()
    connection = _connect()
    try:
        cached = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT task_id, revision, model, vector FROM task_vectors WHERE scope = ?",
                (scope,),
            )
        }
        pending = []
        for document in documents:
            row = cached.get(document["id"])
            if row and row[0] == document["revision"] and row[1] == model_key:
                try:
                    vector = _normalise_vector(json.loads(row[2]))
                    if len(vector) != len(query_vector):
                        raise ValueError("Embedding dimension changed")
                    vectors[document["id"]] = vector
                    diagnostics["cache_hits"] += 1
                    continue
                except (ValueError, TypeError):
                    pass  # Corrupt individual rows are recoverable cache misses.
            pending.append(document)
        for start in range(0, len(pending), BATCH_SIZE):
            batch = pending[start : start + BATCH_SIZE]
            embeddings = model.get_text_embedding_batch([doc["text"] for doc in batch])
            if len(embeddings) != len(batch):
                raise ValueError("Embedding batch incomplete")
            with connection:
                for document, embedding in zip(batch, embeddings):
                    vector = _normalise_vector(embedding)
                    if len(vector) != len(query_vector):
                        raise ValueError("Embedding dimension mismatch")
                    vectors[document["id"]] = vector
                    connection.execute(
                        "INSERT OR REPLACE INTO task_vectors VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            scope,
                            document["id"],
                            document["revision"],
                            model_key,
                            json.dumps(vector),
                            time.time(),
                        ),
                    )
                    diagnostics["embedded"] += 1
        # The cache represents the current filtered snapshot. Filter changes may
        # evict reusable rows, but deleted/revoked tasks cannot remain candidates.
        access_control.invalidate_current_access_evaluator()
        if ConceptsRepository.collection() is None:
            raise RuntimeError(
                "Canonical task storage unavailable during access recheck"
            )
        current_ids = set()
        candidate_ids = list(vectors)
        for start in range(0, len(candidate_ids), 200):
            current_ids.update(
                row["concept_id"]
                for row in ConceptsRepository.find(
                    {"concept_id": {"$in": candidate_ids[start : start + 200]}},
                    projection={"concept_id": 1, "_id": 0},
                )
            )
        diagnostics["access_recheck_excluded"] = len(vectors) - len(current_ids)
        with connection:
            connection.executemany(
                "DELETE FROM task_vectors WHERE scope = ? AND task_id = ?",
                [
                    (scope, task_id)
                    for task_id in set(cached) | set(vectors)
                    if task_id not in current_ids
                ],
            )
        results = []
        for task, document in zip(tasks, documents):
            if document["id"] not in current_ids:
                continue
            score = sum(a * b for a, b in zip(query_vector, vectors[document["id"]]))
            results.append(
                {
                    **task,
                    "semantic_score": round(score, 8),
                    "retrieval_text": document["text"],
                    "indexed_revision": document["revision"],
                }
            )
        results.sort(
            key=lambda task: (-task["semantic_score"], task["task_concept_id"])
        )
        diagnostics["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        logger.info(
            "Task semantic retrieval candidates=%d embedded=%d cache_hits=%d elapsed_ms=%s",
            len(tasks),
            diagnostics["embedded"],
            diagnostics["cache_hits"],
            diagnostics["elapsed_ms"],
        )
        return results, diagnostics
    finally:
        connection.close()


def search_semantic_tasks(
    *, query: str | None, filters: Mapping[str, Any]
) -> dict[str, Any]:
    from .task_management_service import InvalidTaskDataError, _search_tasks

    if not isinstance(query, str) or not query.strip():
        raise InvalidTaskDataError("Semantic search requires a non-empty query")
    if any(key.startswith("_") for key in filters):
        raise InvalidTaskDataError("Internal search parameters are not accepted")
    actor, source = access_control.get_effective_user_concept_id_with_source()
    if access_control.is_bypass_enabled() or (
        actor
        and source
        not in {
            access_control.AUTHENTICATED_SESSION_ACTOR_SOURCE,
            access_control.TRUSTED_IN_PROCESS_ACTOR_SOURCE,
        }
    ):
        raise InvalidTaskDataError("Semantic task search requires trusted actor scope")
    organisation = (
        access_control.get_effective_organisation_concept_id() if actor else None
    )
    with (
        access_control.override_current_actor(actor, organisation),
        access_control.force_access_control_enforcement(),
    ):
        try:
            if ConceptsRepository.collection() is None:
                raise RuntimeError("Canonical task storage unavailable")
            return _search_tasks(query=None, _semantic_query=query.strip(), **filters)
        except InvalidTaskDataError:
            raise
        except Exception as exc:
            # Never log provider exception bodies: they may echo private input.
            logger.warning(
                "Task semantic retrieval unavailable (%s)", type(exc).__name__
            )
            result = _search_tasks(query=query, **filters)
            result["semantic_retrieval"] = {
                "status": "degraded",
                "ranking": "lexical",
                "error_code": "task_semantic_index_unavailable",
                "recovery": "Retry semantic search; an operator can rebuild the disposable local index.",
            }
            return result


def reindex_semantic_tasks() -> dict[str, Any]:
    """Rebuild all currently visible tasks through the normal canonical read path.

    Internal maintenance entry point for a trusted actor-bound operator. No
    canonical task or shared vector namespace is mutated.
    """
    if (
        not access_control.get_effective_user_concept_id()
        or access_control.is_bypass_enabled()
    ):
        raise ValueError("Reindexing requires a bound actor")
    clear_task_semantic_index()
    result = search_semantic_tasks(
        query="task index recovery", filters={"limit": 1, "bulk_visibility": "include"}
    )
    return dict(result["semantic_retrieval"])


def assemble_task_context(
    tasks: list[dict[str, Any]], *, character_budget: int = 12000
) -> list[dict[str, Any]]:
    """Bound RAG context while preserving exact citation identity and truncation."""
    context = []
    remaining = max(0, character_budget)
    for task in tasks:
        if remaining == 0:
            break
        text = task["retrieval_text"]
        excerpt = text[:remaining]
        context.append(
            {
                "citation": task["task_concept_id"],
                "text": excerpt,
                "score": task["semantic_score"],
                "source": "canonical_task_service",
                "truncated": len(excerpt) < len(text),
                "content_role": "retrieved_data",
            }
        )
        remaining -= len(excerpt)
    return context
