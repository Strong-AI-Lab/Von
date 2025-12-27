from __future__ import annotations

from typing import List, Optional
from bson import ObjectId

from src.backend.db.connection_manager import get_db
from src.backend.services.rag_service import get_rag_service, RAGBackendUnavailable
from datetime import datetime


def collect_indexed_sessions(limit: int = 1000) -> List[dict]:
    db = get_db()
    if db is None:
        return []
    coll = db["interaction_sessions"]
    cursor = coll.find(
        {"indexing_status": "indexed"},
        {
            "_id": 1,
            "namespace": 1,
            "embedding": 1,
            "summary": 1,
            "history": 1,
            "indexed_at": 1,
            "user_id": 1,
            "organisation_concept_id": 1,
            "role_in_org": 1,
        },
    ).limit(limit)
    items = []
    for doc in cursor:
        text_parts = []
        if isinstance(doc.get("summary"), str):
            text_parts.append(doc["summary"])
        history = doc.get("history") or []
        if isinstance(history, list):
            for h in history:
                c = h.get("content")
                if isinstance(c, str):
                    text_parts.append(c)
        text = "\n\n".join(text_parts)

        # Build metadata with organisation context if available
        metadata = {
            "indexed_at": str(doc.get("indexed_at")) if doc.get("indexed_at") else None,
            "source": "interaction_session",
        }
        if doc.get("namespace"):
            metadata["namespace"] = doc["namespace"]
        if doc.get("user_id"):
            metadata["user_id"] = doc["user_id"]
        if doc.get("organisation_concept_id"):
            metadata["organisation_concept_id"] = doc["organisation_concept_id"]
        if doc.get("role_in_org"):
            metadata["role_in_org"] = doc["role_in_org"]

        items.append(
            {
                "id": str(doc.get("_id")),
                "namespace": doc.get("namespace"),
                "text": text,
                "embedding": doc.get("embedding"),
                "metadata": metadata,
            }
        )
    return items


def sync_to_chat_store(namespace: Optional[str] = None, limit: int = 1000) -> dict:
    try:
        service = get_rag_service()
    except RAGBackendUnavailable as e:
        return {"success": False, "error": f"RAG service unavailable: {e}"}

    indexed = collect_indexed_sessions(limit=limit)
    added = 0
    failed = 0
    for item in indexed:
        target_namespace = (
            namespace
            or item.get("namespace")
            or (item.get("metadata") or {}).get("namespace")
            or "chat_history"
        )
        doc = {
            "id": item.get("id"),
            "text": item.get("text", ""),
            "metadata": item.get("metadata", {}),
        }
        if item.get("embedding"):
            doc["embedding"] = item["embedding"]

        try:
            success_count, failure_count = service.upsert_documents(
                [doc], namespace=target_namespace
            )
        except Exception:
            # Retry without embedding if backend rejects it
            doc.pop("embedding", None)
            success_count, failure_count = service.upsert_documents(
                [doc], namespace=target_namespace
            )

        added += success_count
        failed += failure_count

    return {
        "success": failed == 0,
        "added": added,
        "failed": failed,
        "total_indexed": len(indexed),
    }


def sync_one_session(session_id: str, namespace: Optional[str] = None) -> dict:
    """Synchronise a single already-indexed interaction_session into the chat RAG store."""
    db = get_db()
    if db is None:
        return {"success": False, "error": "DB unavailable"}
    coll = db["interaction_sessions"]
    doc = coll.find_one(
        {"_id": ObjectId(session_id), "indexing_status": "indexed"},
        {
            "_id": 1,
            "namespace": 1,
            "embedding": 1,
            "summary": 1,
            "history": 1,
            "indexed_at": 1,
            "user_id": 1,
            "organisation_concept_id": 1,
            "role_in_org": 1,
        },
    )
    if not doc:
        return {
            "success": False,
            "error": "Session not found or not indexed",
            "session_id": session_id,
        }
    try:
        service = get_rag_service()
    except RAGBackendUnavailable as e:
        return {
            "success": False,
            "error": f"RAG service unavailable: {e}",
            "session_id": session_id,
        }

    text_parts: List[str] = []
    if isinstance(doc.get("summary"), str) and doc.get("summary"):
        text_parts.append(doc["summary"])
    history = doc.get("history") or []
    if isinstance(history, list):
        for h in history:
            c = h.get("content")
            if isinstance(c, str):
                text_parts.append(c)
    text = "\n\n".join(text_parts)

    # Build metadata with organisation context if available
    metadata = {
        "indexed_at": str(doc.get("indexed_at")) if doc.get("indexed_at") else None,
        "source": "interaction_session",
    }
    if doc.get("namespace"):
        metadata["namespace"] = doc["namespace"]
    if doc.get("user_id"):
        metadata["user_id"] = doc["user_id"]
    if doc.get("organisation_concept_id"):
        metadata["organisation_concept_id"] = doc["organisation_concept_id"]
    if doc.get("role_in_org"):
        metadata["role_in_org"] = doc["role_in_org"]

    upsert_doc = {
        "id": str(doc["_id"]),
        "text": text,
        "metadata": metadata,
    }
    embedding = doc.get("embedding")
    if isinstance(embedding, list) and embedding:
        upsert_doc["embedding"] = embedding

    ns = namespace or doc.get("namespace") or "chat_history"
    try:
        success_count, failure_count = service.upsert_documents(
            [upsert_doc], namespace=ns
        )
    except Exception as e:
        if "embedding" in upsert_doc:
            upsert_doc.pop("embedding", None)
            try:
                success_count, failure_count = service.upsert_documents(
                    [upsert_doc], namespace=ns
                )
            except Exception as nested_e:
                return {
                    "success": False,
                    "error": f"Upsert failed: {nested_e}",
                    "session_id": session_id,
                }
        else:
            return {
                "success": False,
                "error": f"Upsert failed: {e}",
                "session_id": session_id,
            }

    if failure_count > 0 or success_count == 0:
        return {"success": False, "error": "Upsert failed", "session_id": session_id}

    return {
        "success": True,
        "upserted": success_count,
        "namespace": ns,
        "session_id": session_id,
    }
