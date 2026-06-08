from __future__ import annotations

import logging
from typing import Any, List, Optional

from bson import ObjectId

from src.backend.db.mongo_client import get_interaction_sessions_collection
from src.backend.services.rag_service import RAGBackendUnavailable, get_rag_service

logger = logging.getLogger(__name__)


def _normalise_namespace(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#v#"):
        return "#V#" + cleaned[3:]
    if cleaned.startswith("#V#"):
        return cleaned
    return f"#V#{cleaned.lstrip('#')}"


def _normalise_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#v#"):
        return "#V#" + cleaned[3:]
    if cleaned.startswith("#V#"):
        return cleaned
    return f"#V#{cleaned.lstrip('#')}"


def _parse_namespace_components(namespace: str | None) -> tuple[str | None, str | None]:
    if not isinstance(namespace, str) or not namespace.strip():
        return None, None
    try:
        from src.backend.services.namespace_service import parse_namespace

        parsed = parse_namespace(namespace)
    except Exception:
        return None, None

    parsed_user = parsed.get("user_id")
    parsed_org = parsed.get("org_id")
    user_concept_id = _normalise_concept_id(parsed_user) if parsed_user else None
    organisation_concept_id = _normalise_concept_id(parsed_org) if parsed_org else None
    return user_concept_id, organisation_concept_id


def _resolve_sync_namespace_provenance(
    *,
    requested_namespace: str | None,
    requested_user_concept_id: str | None,
    requested_organisation_concept_id: str | None,
    item_namespace: str | None = None,
    item_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_ns = _normalise_namespace(requested_namespace)

    item_ns = _normalise_namespace(item_namespace)
    item_namespace_source = "item.namespace" if item_ns else None
    if item_ns is None and isinstance(item_metadata, dict):
        item_ns = _normalise_namespace(item_metadata.get("namespace"))
        if item_ns is not None:
            item_namespace_source = "item.metadata.namespace"

    if requested_ns is not None:
        namespace = requested_ns
        namespace_source = "request.namespace"
    elif item_ns is not None:
        namespace = item_ns
        namespace_source = item_namespace_source or "item.namespace"
    else:
        namespace = "chat_history"
        namespace_source = "fallback.chat_history"

    user_concept_id = _normalise_concept_id(requested_user_concept_id)
    organisation_concept_id = _normalise_concept_id(requested_organisation_concept_id)
    parsed_user, parsed_org = _parse_namespace_components(namespace)
    if user_concept_id is None:
        user_concept_id = parsed_user
    if organisation_concept_id is None:
        organisation_concept_id = parsed_org

    namespace_mismatch = bool(
        requested_ns is not None and item_ns is not None and requested_ns != item_ns
    )

    return {
        "namespace": namespace,
        "namespace_source": namespace_source,
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "namespace_mismatch": namespace_mismatch,
        "requested_namespace": requested_ns,
        "item_namespace": item_ns,
    }


def _record_namespace_sync_observation(
    *,
    flow: str,
    namespace: str | None,
    namespace_source: str | None,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    mismatch_detected: bool,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        from src.backend.services.namespace_isolation_diagnostics_service import (
            record_namespace_context_observation,
        )

        record_namespace_context_observation(
            flow=flow,
            namespace=namespace,
            namespace_source=namespace_source,
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            mismatch_detected=mismatch_detected,
            details=details,
        )
    except Exception:
        # Diagnostics are best-effort and must not block RAG sync.
        return


def collect_indexed_sessions(limit: int = 1000) -> List[dict]:
    coll = get_interaction_sessions_collection()
    if coll is None:
        return []
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

        # Build metadata with organisation context if available.
        metadata = {
            "indexed_at": str(doc.get("indexed_at")) if doc.get("indexed_at") else None,
            "source": "interaction_session",
        }
        if doc.get("namespace"):
            metadata["namespace"] = doc["namespace"]
        if doc.get("user_id"):
            metadata["user_id"] = doc["user_id"]
            metadata["user_concept_id"] = doc["user_id"]
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


def sync_to_chat_store(
    namespace: Optional[str] = None,
    limit: int = 1000,
    user_concept_id: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
) -> dict:
    try:
        service = get_rag_service()
    except RAGBackendUnavailable as e:
        return {"success": False, "error": f"RAG service unavailable: {e}"}

    indexed = collect_indexed_sessions(limit=limit)
    added = 0
    failed = 0
    mismatch_count = 0
    mismatch_session_ids: list[str] = []
    target_namespaces: set[str] = set()

    for item in indexed:
        item_metadata = item.get("metadata")
        if not isinstance(item_metadata, dict):
            item_metadata = {}
        provenance = _resolve_sync_namespace_provenance(
            requested_namespace=namespace,
            requested_user_concept_id=user_concept_id,
            requested_organisation_concept_id=organisation_concept_id,
            item_namespace=item.get("namespace"),
            item_metadata=item_metadata,
        )

        target_namespace = provenance.get("namespace") or "chat_history"
        target_namespaces.add(str(target_namespace))
        if provenance.get("namespace_mismatch"):
            mismatch_count += 1
            if len(mismatch_session_ids) < 20:
                mismatch_session_ids.append(str(item.get("id")))

        doc_metadata = dict(item_metadata)
        doc_metadata["namespace"] = target_namespace
        doc_metadata["namespace_source"] = provenance.get("namespace_source")
        if provenance.get("user_concept_id"):
            doc_metadata["user_concept_id"] = provenance.get("user_concept_id")
        if provenance.get("organisation_concept_id"):
            doc_metadata["organisation_concept_id"] = provenance.get(
                "organisation_concept_id"
            )

        doc = {
            "id": item.get("id"),
            "text": item.get("text", ""),
            "metadata": doc_metadata,
        }
        if item.get("embedding"):
            doc["embedding"] = item["embedding"]

        try:
            success_count, failure_count = service.upsert_documents(
                [doc], namespace=target_namespace
            )
        except Exception:
            # Retry without embedding if backend rejects it.
            doc.pop("embedding", None)
            success_count, failure_count = service.upsert_documents(
                [doc], namespace=target_namespace
            )

        added += success_count
        failed += failure_count

    resolved_namespace = _normalise_namespace(namespace)
    namespace_source = (
        "request.namespace" if resolved_namespace else "mixed.item.namespace"
    )
    if resolved_namespace is None:
        if len(target_namespaces) == 1:
            resolved_namespace = next(iter(target_namespaces))
            namespace_source = "item.namespace"
        elif len(target_namespaces) == 0:
            resolved_namespace = "chat_history"
            namespace_source = "fallback.chat_history"

    resolved_user = _normalise_concept_id(user_concept_id)
    resolved_org = _normalise_concept_id(organisation_concept_id)
    parsed_user, parsed_org = _parse_namespace_components(resolved_namespace)
    if resolved_user is None:
        resolved_user = parsed_user
    if resolved_org is None:
        resolved_org = parsed_org

    if mismatch_count > 0:
        logger.warning(
            "[NAMESPACE] RAG sync mismatch_count=%s requested=%s sample_session_ids=%s",
            mismatch_count,
            _normalise_namespace(namespace),
            mismatch_session_ids[:5],
        )

    _record_namespace_sync_observation(
        flow="rag_sync.sync_to_chat_store",
        namespace=resolved_namespace,
        namespace_source=namespace_source,
        user_concept_id=resolved_user,
        organisation_concept_id=resolved_org,
        mismatch_detected=mismatch_count > 0,
        details={
            "total_indexed": len(indexed),
            "added": added,
            "failed": failed,
            "mismatch_count": mismatch_count,
            "mismatch_session_ids": mismatch_session_ids[:5],
            "indexed_namespaces": sorted(target_namespaces)[:20],
        },
    )

    return {
        "success": failed == 0,
        "added": added,
        "failed": failed,
        "total_indexed": len(indexed),
        "namespace": resolved_namespace,
        "namespace_source": namespace_source,
        "user_concept_id": resolved_user,
        "organisation_concept_id": resolved_org,
        "namespace_mismatch": mismatch_count > 0,
        "mismatch_count": mismatch_count,
        "mismatch_session_ids": mismatch_session_ids,
    }


def sync_one_session(
    session_id: str,
    namespace: Optional[str] = None,
    user_concept_id: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
) -> dict:
    """Synchronise one already-indexed interaction_session into the chat RAG store."""
    coll = get_interaction_sessions_collection()
    if coll is None:
        return {"success": False, "error": "DB unavailable"}
    try:
        query_id: ObjectId | str = ObjectId(session_id)
    except Exception:
        query_id = session_id

    doc = coll.find_one(
        {"_id": query_id, "indexing_status": "indexed"},
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

    metadata = {
        "indexed_at": str(doc.get("indexed_at")) if doc.get("indexed_at") else None,
        "source": "interaction_session",
    }
    if doc.get("namespace"):
        metadata["namespace"] = doc["namespace"]
    if doc.get("user_id"):
        metadata["user_id"] = doc["user_id"]
        metadata["user_concept_id"] = doc["user_id"]
    if doc.get("organisation_concept_id"):
        metadata["organisation_concept_id"] = doc["organisation_concept_id"]
    if doc.get("role_in_org"):
        metadata["role_in_org"] = doc["role_in_org"]

    provenance = _resolve_sync_namespace_provenance(
        requested_namespace=namespace,
        requested_user_concept_id=user_concept_id,
        requested_organisation_concept_id=organisation_concept_id,
        item_namespace=doc.get("namespace"),
        item_metadata=metadata,
    )
    ns = provenance.get("namespace") or "chat_history"

    metadata["namespace"] = ns
    metadata["namespace_source"] = provenance.get("namespace_source")
    if provenance.get("user_concept_id"):
        metadata["user_concept_id"] = provenance.get("user_concept_id")
    if provenance.get("organisation_concept_id"):
        metadata["organisation_concept_id"] = provenance.get("organisation_concept_id")

    upsert_doc = {
        "id": str(doc["_id"]),
        "text": text,
        "metadata": metadata,
    }
    embedding = doc.get("embedding")
    if isinstance(embedding, list) and embedding:
        upsert_doc["embedding"] = embedding

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

    if provenance.get("namespace_mismatch"):
        logger.warning(
            "[NAMESPACE] RAG sync one-session mismatch requested=%s session_namespace=%s session_id=%s",
            provenance.get("requested_namespace"),
            provenance.get("item_namespace"),
            session_id,
        )

    _record_namespace_sync_observation(
        flow="rag_sync.sync_one_session",
        namespace=ns,
        namespace_source=provenance.get("namespace_source"),
        user_concept_id=provenance.get("user_concept_id"),
        organisation_concept_id=provenance.get("organisation_concept_id"),
        mismatch_detected=bool(provenance.get("namespace_mismatch")),
        details={
            "session_id": session_id,
            "requested_namespace": provenance.get("requested_namespace"),
            "item_namespace": provenance.get("item_namespace"),
        },
    )

    return {
        "success": True,
        "upserted": success_count,
        "namespace": ns,
        "namespace_source": provenance.get("namespace_source"),
        "user_concept_id": provenance.get("user_concept_id"),
        "organisation_concept_id": provenance.get("organisation_concept_id"),
        "namespace_mismatch": bool(provenance.get("namespace_mismatch")),
        "session_id": session_id,
    }
