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
    coll = db['interaction_sessions']
    cursor = coll.find({"indexing_status": "indexed"}, {"_id": 1, "embedding": 1, "summary": 1, "history": 1, "indexed_at": 1}).limit(limit)
    items = []
    for doc in cursor:
        text_parts = []
        if isinstance(doc.get('summary'), str):
            text_parts.append(doc['summary'])
        history = doc.get('history') or []
        if isinstance(history, list):
            for h in history:
                c = h.get('content')
                if isinstance(c, str):
                    text_parts.append(c)
        text = "\n\n".join(text_parts)
        items.append({
            "id": str(doc.get('_id')),
            "text": text,
            "embedding": doc.get('embedding'),
            "metadata": {
                "indexed_at": str(doc.get('indexed_at')) if doc.get('indexed_at') else None,
                "source": "interaction_session",
            }
        })
    return items


def sync_to_chat_store(namespace: Optional[str] = None, limit: int = 1000) -> dict:
    try:
        service = get_rag_service()
    except RAGBackendUnavailable as e:
        return {"success": False, "error": f"RAG service unavailable: {e}"}

    indexed = collect_indexed_sessions(limit=limit)
    added = 0
    for item in indexed:
        try:
            service.upsert(id=item['id'], text=item['text'], metadata=item['metadata'], namespace=namespace, embedding=item.get('embedding'))
            added += 1
        except Exception:
            # Fallback: if embedding is not compatible, let backend compute it
            try:
                service.upsert(id=item['id'], text=item['text'], metadata=item['metadata'], namespace=namespace)
                added += 1
            except Exception:
                pass
    return {"success": True, "added": added, "total_indexed": len(indexed)}

def sync_one_session(session_id: str, namespace: Optional[str] = None) -> dict:
    """Synchronise a single already-indexed interaction_session into the chat RAG store."""
    db = get_db()
    if db is None:
        return {"success": False, "error": "DB unavailable"}
    coll = db['interaction_sessions']
    doc = coll.find_one({"_id": ObjectId(session_id), "indexing_status": "indexed"}, {"_id": 1, "embedding": 1, "summary": 1, "history": 1, "indexed_at": 1})
    if not doc:
        return {"success": False, "error": "Session not found or not indexed", "session_id": session_id}
    try:
        service = get_rag_service()
    except RAGBackendUnavailable as e:
        return {"success": False, "error": f"RAG service unavailable: {e}", "session_id": session_id}

    text_parts: List[str] = []
    if isinstance(doc.get('summary'), str) and doc.get('summary'):
        text_parts.append(doc['summary'])
    history = doc.get('history') or []
    if isinstance(history, list):
        for h in history:
            c = h.get('content')
            if isinstance(c, str):
                text_parts.append(c)
    text = "\n\n".join(text_parts)

    upsert_doc = {
        "id": str(doc['_id']),
        "text": text,
        "metadata": {
            "indexed_at": str(doc.get('indexed_at')) if doc.get('indexed_at') else None,
            "source": "interaction_session",
        }
    }
    embedding = doc.get('embedding')
    ns = namespace or "chat_history"
    try:
        if isinstance(embedding, list) and embedding:
            service.upsert(id=upsert_doc['id'], text=upsert_doc['text'], metadata=upsert_doc['metadata'], namespace=ns, embedding=embedding)
        else:
            service.upsert(id=upsert_doc['id'], text=upsert_doc['text'], metadata=upsert_doc['metadata'], namespace=ns)
    except Exception as e:
        return {"success": False, "error": f"Upsert failed: {e}", "session_id": session_id}
    return {"success": True, "upserted": 1, "namespace": ns, "session_id": session_id}
