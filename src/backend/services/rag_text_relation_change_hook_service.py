from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def maybe_sync_concept_text_relations_to_rag(
    *,
    namespace: Optional[str],
    concept_id: str,
    predicate: Optional[str] = None,
) -> Dict[str, Any]:
    """Best-effort: reindex a concept's text relations into RAG.

    This is intentionally fail-closed: if `namespace` is missing, we do nothing.
    """

    if not namespace or not isinstance(namespace, str) or not namespace.strip():
        return {"success": False, "skipped": True, "reason": "namespace_required"}

    if not isinstance(concept_id, str) or not concept_id.strip():
        return {"success": False, "skipped": True, "reason": "concept_id_required"}

    predicates = None
    if isinstance(predicate, str) and predicate.strip():
        predicates = [predicate.strip()]

    try:
        from src.backend.services.rag_text_relation_sync_service import (
            sync_text_relations_to_rag,
        )

        return sync_text_relations_to_rag(
            namespace=namespace.strip(),
            concept_ids=[concept_id.strip()],
            predicates=predicates,
            limit=5000,
        )
    except Exception as exc:
        logger.warning(
            "[rag_text_relations] RAG sync failed concept_id=%s namespace=%s: %s",
            concept_id,
            namespace,
            exc,
        )
        return {
            "success": False,
            "skipped": False,
            "error": f"RAG sync failed: {exc}",
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
