from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from bson import ObjectId
from bson.errors import InvalidId

from src.backend.db.mongo_client import get_concepts_collection
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import (
    apply_concept_query_filter,
    override_current_organisation,
    override_current_user,
)
from src.backend.services.rag_service import RAGBackendUnavailable, get_rag_service


@dataclass(frozen=True)
class TextRelationRagDoc:
    doc_id: str
    text: str
    metadata: Dict[str, Any]


def list_text_relation_index_items(
    *,
    namespace: str,
    limit: int = 20,
    offset: int = 0,
    scan_limit: int = 5000,
    predicates: Optional[Sequence[str]] = None,
    languages: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Return lightweight previews of text relations visible in a namespace.

    This is intended for inspection ("what would be indexed?") rather than bulk export.
    Results are filtered by concept visibility for the user/org encoded in the namespace.

    Note: `total` is the number of *visible* relations within `scan_limit`.
    """

    user_id, org_id = _parse_namespace(namespace)
    rel_filter: Dict[str, Any] = {}
    if predicates:
        rel_filter["predicate"] = {"$in": list(predicates)}

    safe_limit = max(int(limit), 0)
    safe_offset = max(int(offset), 0)
    safe_scan_limit = max(int(scan_limit), 0)

    relations = TextRelationsRepository.find(
        rel_filter,
        projection={
            "_id": 1,
            "subject_concept_id": 1,
            "predicate": 1,
            "object_text_id": 1,
            "updated_at": 1,
        },
        sort=[("updated_at", -1)],
        limit=safe_scan_limit,
    )

    items: List[Dict[str, Any]] = []
    concept_visibility: Dict[str, bool] = {}

    total_visible = 0
    scanned = 0

    for rel in relations:
        scanned += 1
        if not isinstance(rel, dict):
            continue

        relation_id = rel.get("_id")
        relation_id_str = str(relation_id) if relation_id else None
        subject_concept_id = rel.get("subject_concept_id")
        predicate = rel.get("predicate")
        object_text_id = rel.get("object_text_id")

        if (
            not relation_id_str
            or not isinstance(subject_concept_id, str)
            or not predicate
            or not object_text_id
        ):
            continue

        if subject_concept_id not in concept_visibility:
            concept_visibility[subject_concept_id] = _concept_visible_in_namespace(
                concept_id=subject_concept_id, user_id=user_id, org_id=org_id
            )
        if not concept_visibility[subject_concept_id]:
            continue

        text_value_oid = _safe_object_id(object_text_id)
        if text_value_oid is None:
            continue

        tv = TextValuesRepository.find_one(
            {"_id": text_value_oid}, {"text": 1, "lang": 1}
        )
        if not isinstance(tv, dict):
            continue

        text = tv.get("text")
        lang = tv.get("lang") or "en-NZ"
        if not isinstance(text, str) or not text.strip():
            continue
        if languages and isinstance(lang, str) and lang not in set(languages):
            continue

        visible_index = total_visible
        total_visible += 1
        if safe_limit and visible_index < safe_offset:
            continue
        if safe_limit and len(items) >= safe_limit:
            continue

        items.append(
            {
                "relation_id": relation_id_str,
                "subject_concept_id": subject_concept_id,
                "predicate": str(predicate),
                "text_value_id": str(text_value_oid),
                "lang": str(lang),
                "preview_length": len(text),
                "updated_at": (
                    str(rel.get("updated_at")) if rel.get("updated_at") else None
                ),
            }
        )

    return {
        "items": items,
        "total": total_visible,
        "scanned": scanned,
        "limit": safe_limit,
        "offset": safe_offset,
        "scan_limit": safe_scan_limit,
    }


def get_text_relation_preview(
    *,
    namespace: str,
    relation_id: str,
) -> Optional[TextRelationRagDoc]:
    """Fetch one visible text relation and return its full RAG document representation."""

    user_id, org_id = _parse_namespace(namespace)

    if not isinstance(relation_id, str) or not relation_id.strip():
        return None

    relation_key: Any
    relation_oid = _safe_object_id(relation_id.strip())
    relation_key = relation_oid if relation_oid is not None else relation_id.strip()

    rel = TextRelationsRepository.find_one(
        {"_id": relation_key},
        {
            "_id": 1,
            "subject_concept_id": 1,
            "predicate": 1,
            "object_text_id": 1,
            "context": 1,
            "created_at": 1,
            "updated_at": 1,
        },
    )
    if not isinstance(rel, dict):
        return None

    subject_concept_id = rel.get("subject_concept_id")
    predicate = rel.get("predicate")
    object_text_id = rel.get("object_text_id")

    if not isinstance(subject_concept_id, str) or not predicate or not object_text_id:
        return None

    if not _concept_visible_in_namespace(
        concept_id=subject_concept_id, user_id=user_id, org_id=org_id
    ):
        return None

    text_value_oid = _safe_object_id(object_text_id)
    if text_value_oid is None:
        return None

    tv = TextValuesRepository.find_one({"_id": text_value_oid})
    if not isinstance(tv, dict):
        return None

    text = tv.get("text")
    lang = tv.get("lang") or "en-NZ"
    if not isinstance(text, str) or not text.strip():
        return None

    rag_text = _build_rag_text(
        concept_id=subject_concept_id,
        predicate=str(predicate),
        lang=str(lang),
        text=text,
    )

    relation_id_str = str(rel.get("_id"))
    metadata = {
        "source": "vontology_text_relation",
        "subject_concept_id": subject_concept_id,
        "predicate": str(predicate),
        "relation_id": relation_id_str,
        "text_value_id": str(text_value_oid),
        "lang": str(lang),
        "user_id": user_id,
        "organisation_concept_id": org_id,
        "created_at": str(rel.get("created_at")) if rel.get("created_at") else None,
        "updated_at": str(rel.get("updated_at")) if rel.get("updated_at") else None,
    }

    return TextRelationRagDoc(
        doc_id=f"text_relation:{relation_id_str}",
        text=rag_text,
        metadata=metadata,
    )


def _parse_namespace(namespace: str) -> Tuple[str, str]:
    """Parse a namespace of the form '#V#user@org' into (user_id, organisation_id)."""
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("namespace is required")

    cleaned = namespace.strip()
    if "@" not in cleaned:
        raise ValueError(
            "namespace must be of the form '#V#user@org' (missing '@' separator)"
        )

    user_part, org_part = cleaned.split("@", 1)
    user_id = user_part.strip()
    org_id = org_part.strip()

    if not user_id:
        raise ValueError("namespace user part is empty")
    if not org_id:
        raise ValueError("namespace organisation part is empty")

    # Ensure org is a concept id.
    if not org_id.startswith("#V#"):
        org_id = f"#V#{org_id}"

    return user_id, org_id


def _safe_object_id(value: Any) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str):
        try:
            return ObjectId(value)
        except (InvalidId, TypeError):
            return None
    return None


def _concept_visible_in_namespace(
    *, concept_id: str, user_id: str, org_id: str
) -> bool:
    coll = get_concepts_collection()
    if coll is None:
        # Fail-open for dev tooling when DB is not available.
        return True

    with override_current_user(user_id), override_current_organisation(org_id):
        query = apply_concept_query_filter({"concept_id": concept_id})
        doc = coll.find_one(query, {"_id": 1})
        return bool(doc)


def _build_rag_text(*, concept_id: str, predicate: str, lang: str, text: str) -> str:
    # Keep the body searchable, but also add a small structured header.
    return (
        f"Concept: {concept_id}\n"
        f"Predicate: {predicate}\n"
        f"Language: {lang}\n\n"
        f"{text.strip()}"
    )


def collect_text_relation_docs_for_namespace(
    *,
    namespace: str,
    predicates: Optional[Sequence[str]] = None,
    languages: Optional[Sequence[str]] = None,
    concept_ids: Optional[Sequence[str]] = None,
    updated_since: Optional[datetime] = None,
    skip: int = 0,
    sort: Optional[List] = None,
    limit: int = 5000,
) -> List[TextRelationRagDoc]:
    """Collect text-relations as RAG documents for a single namespace.

    Notes:
    - Filters by concept visibility for the (user, organisation) implied by the namespace.
    - Stamps metadata user/org so LlamaIndex permission filtering works.
    """

    user_id, org_id = _parse_namespace(namespace)

    rel_filter: Dict[str, Any] = {}
    concept_allow: Optional[set[str]] = None
    if concept_ids:
        concept_allow = {c for c in concept_ids if isinstance(c, str) and c.strip()}
        if concept_allow:
            rel_filter["subject_concept_id"] = {"$in": sorted(concept_allow)}

    if predicates:
        rel_filter["predicate"] = {"$in": list(predicates)}

    if updated_since is not None:
        rel_filter["updated_at"] = {"$gte": updated_since}

    safe_skip = max(int(skip), 0)
    safe_limit = max(int(limit), 0)
    safe_sort = sort if sort is not None else [("_id", 1)]

    relations = list(
        TextRelationsRepository.find(
            rel_filter,
            sort=safe_sort,
            skip=safe_skip,
            limit=safe_limit,
        )
    )
    if not relations:
        return []

    docs: List[TextRelationRagDoc] = []

    # Small cache to avoid repeated concept lookups.
    concept_visibility: Dict[str, bool] = {}

    for rel in relations:
        if not isinstance(rel, dict):
            continue

        relation_id = rel.get("_id")
        relation_id_str = str(relation_id) if relation_id else None
        subject_concept_id = rel.get("subject_concept_id")
        predicate = rel.get("predicate")
        object_text_id = rel.get("object_text_id")

        if (
            not relation_id_str
            or not isinstance(subject_concept_id, str)
            or not predicate
        ):
            continue

        if concept_allow is not None and subject_concept_id not in concept_allow:
            continue

        if subject_concept_id not in concept_visibility:
            concept_visibility[subject_concept_id] = _concept_visible_in_namespace(
                concept_id=subject_concept_id, user_id=user_id, org_id=org_id
            )
        if not concept_visibility[subject_concept_id]:
            continue

        text_value_oid = _safe_object_id(object_text_id)
        if text_value_oid is None:
            continue

        tv = TextValuesRepository.find_one({"_id": text_value_oid})
        if not isinstance(tv, dict):
            continue

        text = tv.get("text")
        lang = tv.get("lang") or "en-NZ"
        if not isinstance(text, str) or not text.strip():
            continue
        if languages and isinstance(lang, str) and lang not in set(languages):
            continue

        rag_text = _build_rag_text(
            concept_id=subject_concept_id,
            predicate=str(predicate),
            lang=str(lang),
            text=text,
        )

        metadata = {
            # Distinguish from chat history docs.
            "type": "text_relation",
            "source": "vontology_text_relation",
            # Preferred canonical key for concept.
            "concept_id": subject_concept_id,
            "subject_concept_id": subject_concept_id,
            "predicate": str(predicate),
            "relation_id": relation_id_str,
            "text_value_id": str(text_value_oid),
            "lang": str(lang),
            "language": str(lang),
            # Required for query-time filtering.
            "user_id": user_id,
            "organisation_concept_id": org_id,
            # Alias to match Jira ticket naming.
            "org_id": org_id,
        }

        docs.append(
            TextRelationRagDoc(
                doc_id=f"text_relation:{relation_id_str}",
                text=rag_text,
                metadata=metadata,
            )
        )

    return docs


def sync_text_relations_to_rag(
    *,
    namespace: str,
    predicates: Optional[Sequence[str]] = None,
    languages: Optional[Sequence[str]] = None,
    concept_ids: Optional[Sequence[str]] = None,
    updated_since: Optional[datetime] = None,
    skip: int = 0,
    sort: Optional[List] = None,
    limit: int = 5000,
    batch_size: int = 200,
) -> Dict[str, Any]:
    """Upsert text relation docs into the RAG store for a namespace."""

    try:
        rag = get_rag_service()
    except RAGBackendUnavailable as exc:
        return {"success": False, "error": f"RAG service unavailable: {exc}"}

    docs = collect_text_relation_docs_for_namespace(
        namespace=namespace,
        predicates=predicates,
        languages=languages,
        concept_ids=concept_ids,
        updated_since=updated_since,
        skip=skip,
        sort=sort,
        limit=limit,
    )

    added = 0
    failed = 0

    def _chunks(
        items: List[TextRelationRagDoc], n: int
    ) -> Iterable[List[TextRelationRagDoc]]:
        step = max(int(n), 1)
        for i in range(0, len(items), step):
            yield items[i : i + step]

    for batch in _chunks(docs, batch_size):
        payload = [
            {"id": d.doc_id, "text": d.text, "metadata": d.metadata} for d in batch
        ]
        ok, bad = rag.upsert_documents(payload, namespace=namespace)
        added += int(ok)
        failed += int(bad)

    return {
        "success": failed == 0,
        "namespace": namespace,
        "total_candidates": len(docs),
        "added": added,
        "failed": failed,
    }
