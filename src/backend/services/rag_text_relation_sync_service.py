from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from bson import ObjectId
from bson.errors import InvalidId

from src.backend.db.mongo_client import (
    get_scoped_knowledge_assertions_collection,
)
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import (
    filter_accessible_concept_ids,
    override_current_organisation,
    override_current_user,
)
from src.backend.services.rag_service import RAGBackendUnavailable, get_rag_service
from src.backend.services.namespace_service import (
    derive_actor_context_from_namespace,
    resolve_canonical_namespace,
)
from src.backend.services.text_relation_predicate_validation_service import (
    predicate_concept_id_for_storage,
)
from src.backend.services.text_relation_read_policy import (
    is_hidden_from_generic_text_reads,
)


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

    safe_limit = max(int(limit), 0)
    safe_offset = max(int(offset), 0)
    safe_scan_limit = max(int(scan_limit), 0)
    docs = collect_text_relation_docs_for_namespace(
        namespace=namespace,
        predicates=predicates,
        languages=languages,
        limit=safe_scan_limit,
    )
    total_visible = len(docs)
    selected_docs = (
        docs[safe_offset : safe_offset + safe_limit] if safe_limit else []
    )
    items: List[Dict[str, Any]] = []
    for doc in selected_docs:
        metadata = doc.metadata
        items.append(
            {
                "index_item_id": doc.doc_id,
                "row_kind": metadata.get("row_kind") or "base_text_relation",
                "relation_id": metadata.get("relation_id"),
                "assertion_id": metadata.get("assertion_id"),
                "subject_concept_id": metadata.get("subject_concept_id"),
                "predicate": metadata.get("predicate"),
                "text_value_id": metadata.get("text_value_id"),
                "lang": metadata.get("lang"),
                "preview_length": metadata.get("content_length", len(doc.text)),
                "updated_at": metadata.get("updated_at"),
                "source": metadata.get("source"),
            }
        )

    return {
        "items": items,
        "total": total_visible,
        "scanned": total_visible,
        "scanned_kind": "visible_projected_candidates",
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

    requested_id = relation_id.strip()
    scoped_assertion_id = (
        requested_id.removeprefix("scoped_assertion:")
        if requested_id.startswith("scoped_assertion:")
        else requested_id
        if requested_id.startswith("ska_")
        else None
    )
    if scoped_assertion_id:
        collection = get_scoped_knowledge_assertions_collection()
        if collection is None:
            return None
        assertion = collection.find_one(
            {
                "assertion_id": scoped_assertion_id,
                "status": "asserted",
                "object_kind": "text",
                "scope.audience_keys": {
                    "$in": [f"user:{user_id}", f"org:{org_id}"],
                },
            }
        )
        if not isinstance(assertion, dict):
            return None
        if is_hidden_from_generic_text_reads(assertion.get("predicate")):
            return None
        concept_visibility: Dict[str, bool] = {}
        _populate_concept_visibility(
            [
                str(assertion.get("subject_concept_id") or ""),
                str(
                    predicate_concept_id_for_storage(
                        assertion.get("predicate")
                    )
                    or ""
                ),
            ],
            user_id=user_id,
            org_id=org_id,
            concept_visibility=concept_visibility,
        )
        return _scoped_assertion_to_rag_doc(
            assertion,
            user_id=user_id,
            org_id=org_id,
            concept_visibility=concept_visibility,
        )

    relation_key: Any
    relation_oid = _safe_object_id(requested_id)
    relation_key = relation_oid if relation_oid is not None else requested_id

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
    if is_hidden_from_generic_text_reads(predicate):
        return None

    predicate_concept_id = predicate_concept_id_for_storage(predicate)
    if not predicate_concept_id:
        return None
    if _visible_concept_ids_in_namespace(
        [subject_concept_id, predicate_concept_id],
        user_id=user_id,
        org_id=org_id,
    ) != {subject_concept_id, predicate_concept_id}:
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
        "predicate_concept_id": predicate_concept_id,
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
    """Parse a canonical user or user-at-organisation RAG namespace."""
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("namespace is required")
    cleaned = namespace.strip()
    canonical = resolve_canonical_namespace(cleaned)
    if canonical is None or canonical != cleaned:
        raise ValueError(
            "namespace must use canonical #V#user or #V#user@organisation form"
        )
    user_id, org_id = derive_actor_context_from_namespace(canonical)
    if user_id is None:
        raise ValueError("namespace has no valid user concept ID")
    return user_id, org_id or ""


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
    return concept_id in _visible_concept_ids_in_namespace(
        [concept_id],
        user_id=user_id,
        org_id=org_id,
    )


def _visible_concept_ids_in_namespace(
    concept_ids: Iterable[str],
    *,
    user_id: str,
    org_id: str,
) -> set[str]:
    """Resolve one bounded concept-visibility batch for a namespace."""

    candidates = {
        concept_id.strip()
        for concept_id in concept_ids
        if isinstance(concept_id, str) and concept_id.strip().startswith("#V#")
    }
    if not candidates:
        return set()
    with override_current_user(user_id), override_current_organisation(org_id):
        return filter_accessible_concept_ids(candidates)


def _populate_concept_visibility(
    concept_ids: Iterable[str],
    *,
    user_id: str,
    org_id: str,
    concept_visibility: Dict[str, bool],
) -> None:
    """Populate an explicit cache with one query for all missing concept IDs."""

    missing = {
        concept_id
        for concept_id in concept_ids
        if isinstance(concept_id, str)
        and concept_id.startswith("#V#")
        and concept_id not in concept_visibility
    }
    if not missing:
        return
    visible = _visible_concept_ids_in_namespace(
        missing,
        user_id=user_id,
        org_id=org_id,
    )
    for concept_id in missing:
        concept_visibility[concept_id] = concept_id in visible


def _populate_text_value_cache(
    relations: Iterable[Any],
    *,
    text_value_cache: Dict[str, Mapping[str, Any] | None],
) -> None:
    """Hydrate one bounded relation batch with one TextValue query."""

    object_ids: dict[str, ObjectId] = {}
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        object_id = _safe_object_id(relation.get("object_text_id"))
        if object_id is None:
            continue
        cache_key = str(object_id)
        if cache_key not in text_value_cache:
            object_ids[cache_key] = object_id
    if not object_ids:
        return
    for cache_key in object_ids:
        text_value_cache[cache_key] = None
    for text_value in TextValuesRepository.find(
        {"_id": {"$in": list(object_ids.values())}},
        {
            "text": 1,
            "lang": 1,
        },
    ):
        if not isinstance(text_value, Mapping):
            continue
        cache_key = str(text_value.get("_id") or "")
        if cache_key in text_value_cache:
            text_value_cache[cache_key] = text_value


def _build_rag_text(*, concept_id: str, predicate: str, lang: str, text: str) -> str:
    # Keep the body searchable, but also add a small structured header.
    return (
        f"Concept: {concept_id}\n"
        f"Predicate: {predicate}\n"
        f"Language: {lang}\n\n"
        f"{text.strip()}"
    )


def _base_relation_to_rag_doc(
    relation: Any,
    *,
    user_id: str,
    org_id: str,
    concept_allow: set[str] | None,
    language_allow: set[str],
    concept_visibility: Dict[str, bool],
    text_value_cache: Dict[str, Mapping[str, Any] | None] | None = None,
) -> TextRelationRagDoc | None:
    if not isinstance(relation, dict):
        return None

    relation_id = relation.get("_id")
    relation_id_str = str(relation_id) if relation_id else None
    subject_concept_id = relation.get("subject_concept_id")
    predicate = relation.get("predicate")
    object_text_id = relation.get("object_text_id")
    if (
        not relation_id_str
        or not isinstance(subject_concept_id, str)
        or not predicate
    ):
        return None
    if is_hidden_from_generic_text_reads(predicate):
        return None
    if concept_allow is not None and subject_concept_id not in concept_allow:
        return None

    predicate_concept_id = predicate_concept_id_for_storage(predicate)
    if not predicate_concept_id:
        return None
    for concept_id in (subject_concept_id, predicate_concept_id):
        if concept_id not in concept_visibility:
            concept_visibility[concept_id] = _concept_visible_in_namespace(
                concept_id=concept_id,
                user_id=user_id,
                org_id=org_id,
            )
        if not concept_visibility[concept_id]:
            return None

    text_value_oid = _safe_object_id(object_text_id)
    if text_value_oid is None:
        return None
    cache_key = str(text_value_oid)
    text_value = (
        text_value_cache.get(cache_key)
        if text_value_cache is not None and cache_key in text_value_cache
        else TextValuesRepository.find_one({"_id": text_value_oid})
    )
    if not isinstance(text_value, Mapping):
        return None

    text = text_value.get("text")
    lang = str(text_value.get("lang") or "en-NZ")
    if not isinstance(text, str) or not text.strip():
        return None
    if language_allow and lang not in language_allow:
        return None

    return TextRelationRagDoc(
        doc_id=f"text_relation:{relation_id_str}",
        text=_build_rag_text(
            concept_id=subject_concept_id,
            predicate=str(predicate),
            lang=lang,
            text=text,
        ),
        metadata={
            "type": "text_relation",
            "source": "vontology_text_relation",
            "concept_id": subject_concept_id,
            "subject_concept_id": subject_concept_id,
            "predicate": str(predicate),
            "predicate_concept_id": predicate_concept_id,
            "relation_id": relation_id_str,
            "text_value_id": str(text_value_oid),
            "lang": lang,
            "language": lang,
            "content_length": len(text),
            "created_at": (
                str(relation.get("created_at"))
                if relation.get("created_at")
                else None
            ),
            "updated_at": (
                str(relation.get("updated_at"))
                if relation.get("updated_at")
                else None
            ),
            "user_id": user_id,
            "organisation_concept_id": org_id,
            "org_id": org_id,
        },
    )


def _scoped_assertion_to_rag_doc(
    assertion: Any,
    *,
    user_id: str,
    org_id: str,
    concept_visibility: Dict[str, bool],
) -> TextRelationRagDoc | None:
    if not isinstance(assertion, dict):
        return None
    if is_hidden_from_generic_text_reads(assertion.get("predicate")):
        return None
    if assertion.get("assertion_form") == "standalone_text":
        try:
            from .knowledge_assertion_rag_service import (
                build_standalone_text_assertion_rag_document,
            )

            payload = build_standalone_text_assertion_rag_document(assertion)
        except (TypeError, ValueError):
            return None
        return TextRelationRagDoc(
            doc_id=str(payload["id"]),
            text=str(payload["text"]),
            metadata=dict(payload["metadata"]),
        )
    object_text = assertion.get("object_text")
    if not isinstance(object_text, dict):
        return None

    text = object_text.get("text")
    lang = str(object_text.get("language") or "en-NZ")
    subject_concept_id = str(assertion.get("subject_concept_id") or "")
    predicate = str(assertion.get("predicate") or "")
    assertion_id = str(assertion.get("assertion_id") or "")
    if not text or not subject_concept_id or not predicate or not assertion_id:
        return None
    predicate_concept_id = predicate_concept_id_for_storage(predicate)
    if not predicate_concept_id:
        return None

    for concept_id in (subject_concept_id, predicate_concept_id):
        if concept_id not in concept_visibility:
            concept_visibility[concept_id] = _concept_visible_in_namespace(
                concept_id=concept_id,
                user_id=user_id,
                org_id=org_id,
            )
        if not concept_visibility[concept_id]:
            return None

    scope = assertion.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    provenance = assertion.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}

    return TextRelationRagDoc(
        doc_id=f"scoped_assertion:{assertion_id}",
        text=_build_rag_text(
            concept_id=subject_concept_id,
            predicate=predicate,
            lang=lang,
            text=str(text),
        ),
        metadata={
            "type": "scoped_knowledge_assertion",
            "source": "scoped_knowledge_assertion",
            "concept_id": subject_concept_id,
            "subject_concept_id": subject_concept_id,
            "predicate": predicate,
            "predicate_concept_id": predicate_concept_id,
            "assertion_id": assertion_id,
            "relation_id": None,
            "row_kind": "scoped_assertion",
            "lang": lang,
            "language": lang,
            "content_length": len(str(text)),
            "user_id": user_id,
            "organisation_concept_id": org_id,
            "org_id": org_id,
            "audience_user_concept_id": user_id,
            "audience_organisation_concept_id": org_id,
            "audience_keys": list(scope.get("audience_keys") or []),
            "assertion_scope": scope,
            "assertion_provenance": provenance,
            "asserted_by_user_concept_id": provenance.get(
                "asserted_by_user_concept_id"
            ),
            "asserted_by_organisation_concept_id": provenance.get(
                "organisation_concept_id"
            ),
            "canonical_publication": False,
            "created_at": (
                str(assertion.get("created_at"))
                if assertion.get("created_at")
                else None
            ),
            "updated_at": (
                str(assertion.get("updated_at"))
                if assertion.get("updated_at")
                else None
            ),
        },
    )


def _iter_bounded_batches(
    items: Iterable[Any],
    *,
    batch_size: int,
) -> Iterable[List[Any]]:
    """Yield bounded in-memory batches from one already-open source cursor."""

    batch: List[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _emit_stale_doc_batch(
    stale_batch: Sequence[str],
    *,
    stale_doc_ids: List[str] | None,
    stale_doc_sink: Callable[[Sequence[str]], None] | None,
) -> None:
    """Publish one bounded stale-document batch to compatibility consumers."""

    if not stale_batch:
        return
    deduplicated = list(dict.fromkeys(stale_batch))
    if stale_doc_ids is not None:
        stale_doc_ids.extend(deduplicated)
    if stale_doc_sink is not None:
        stale_doc_sink(deduplicated)


def _base_relation_should_prune(
    relation: Any,
    *,
    concept_allow: set[str] | None,
    language_allow: set[str],
    concept_visibility: Mapping[str, bool],
    text_value_cache: Mapping[str, Mapping[str, Any] | None],
) -> bool:
    """Return whether a scanned base relation is now ineligible for this index."""

    if not isinstance(relation, Mapping) or not relation.get("_id"):
        return False
    subject_id = str(relation.get("subject_concept_id") or "")
    if concept_allow is not None and subject_id not in concept_allow:
        return False
    if is_hidden_from_generic_text_reads(relation.get("predicate")):
        return True
    predicate_id = predicate_concept_id_for_storage(relation.get("predicate"))
    if (
        not subject_id
        or not predicate_id
        or not concept_visibility.get(subject_id, False)
        or not concept_visibility.get(predicate_id, False)
    ):
        return True
    text_value_id = _safe_object_id(relation.get("object_text_id"))
    if text_value_id is None:
        return True
    text_value = text_value_cache.get(str(text_value_id))
    if not isinstance(text_value, Mapping):
        return True
    lang = str(text_value.get("lang") or "en-NZ")
    if language_allow and lang not in language_allow:
        return False
    text = text_value.get("text")
    return not isinstance(text, str) or not text.strip()


def iter_text_relation_docs_for_namespace(
    *,
    namespace: str,
    predicates: Optional[Sequence[str]] = None,
    languages: Optional[Sequence[str]] = None,
    concept_ids: Optional[Sequence[str]] = None,
    updated_since: Optional[datetime] = None,
    limit: int | None = None,
    source_batch_size: int = 1000,
    stale_doc_ids: List[str] | None = None,
    stale_doc_sink: Callable[[Sequence[str]], None] | None = None,
) -> Iterable[TextRelationRagDoc]:
    """Stream one stable base-then-scoped namespace scan.

    Each backing store is opened once and traversed in immutable ``_id`` order.
    Bounded in-memory batches provide batched visibility checks without
    repeatedly rescanning from zero or paging on mutable visible offsets.
    The scan is deliberately limited to the one supplied namespace; it does not
    fan out to other users or organisations.
    """

    user_id, org_id = _parse_namespace(namespace)
    safe_limit = None if limit is None else max(int(limit), 0)
    if safe_limit == 0:
        return
    safe_batch_size = max(1, min(int(source_batch_size), 5000))

    rel_filter: Dict[str, Any] = {}
    concept_allow: Optional[set[str]] = None
    if concept_ids:
        concept_allow = {
            concept_id.strip()
            for concept_id in concept_ids
            if isinstance(concept_id, str) and concept_id.strip()
        }
        if concept_allow:
            rel_filter["subject_concept_id"] = {"$in": sorted(concept_allow)}
    if predicates:
        rel_filter["predicate"] = {"$in": list(predicates)}
    if updated_since is not None:
        rel_filter["updated_at"] = {"$gte": updated_since}

    language_allow = set(languages or ())
    prune_stale = stale_doc_ids is not None or stale_doc_sink is not None
    yielded = 0
    base_sort = (
        [("updated_at", 1), ("_id", 1)]
        if updated_since is not None and not concept_allow and not predicates
        else [("_id", 1)]
    )
    base_cursor = TextRelationsRepository.find(
        rel_filter,
        sort=base_sort,
    )
    set_batch_size = getattr(base_cursor, "batch_size", None)
    if callable(set_batch_size):
        base_cursor = set_batch_size(safe_batch_size)
    for relations in _iter_bounded_batches(
        base_cursor,
        batch_size=safe_batch_size,
    ):
        concept_visibility: Dict[str, bool] = {}
        text_value_cache: Dict[str, Mapping[str, Any] | None] = {}
        _populate_concept_visibility(
            (
                concept_id
                for relation in relations
                if isinstance(relation, Mapping)
                for concept_id in (
                    str(relation.get("subject_concept_id") or ""),
                    str(
                        predicate_concept_id_for_storage(
                            relation.get("predicate")
                        )
                        or ""
                    ),
                )
            ),
            user_id=user_id,
            org_id=org_id,
            concept_visibility=concept_visibility,
        )
        _populate_text_value_cache(
            relations,
            text_value_cache=text_value_cache,
        )
        batch_docs: List[TextRelationRagDoc] = []
        stale_batch: List[str] = []
        remaining = (
            None if safe_limit is None else max(safe_limit - yielded, 0)
        )
        for relation in relations:
            doc = _base_relation_to_rag_doc(
                relation,
                user_id=user_id,
                org_id=org_id,
                concept_allow=concept_allow,
                language_allow=language_allow,
                concept_visibility=concept_visibility,
                text_value_cache=text_value_cache,
            )
            if doc is None:
                if (
                    prune_stale
                    and _base_relation_should_prune(
                        relation,
                        concept_allow=concept_allow,
                        language_allow=language_allow,
                        concept_visibility=concept_visibility,
                        text_value_cache=text_value_cache,
                    )
                ):
                    stale_batch.append(
                        f"text_relation:{relation.get('_id')}"
                    )
                continue
            batch_docs.append(doc)
            if remaining is not None and len(batch_docs) >= remaining:
                break
        _emit_stale_doc_batch(
            stale_batch,
            stale_doc_ids=stale_doc_ids,
            stale_doc_sink=stale_doc_sink,
        )
        for doc in batch_docs:
            yield doc
            yielded += 1
            if safe_limit is not None and yielded >= safe_limit:
                return

    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        return
    scoped_query: Dict[str, Any] = {
        "scope.audience_keys": {
            "$in": [f"user:{user_id}", f"org:{org_id}"],
        },
        "status": (
            {"$in": ["asserted", "retracted"]}
            if prune_stale
            else "asserted"
        ),
        "object_kind": "text",
    }
    if updated_since is not None:
        scoped_query["updated_at"] = {"$gte": updated_since}
    if concept_allow:
        scoped_query["subject_concept_id"] = {"$in": sorted(concept_allow)}
    if predicates:
        scoped_query["predicate"] = {"$in": list(predicates)}
    if languages:
        scoped_query["object_text.language"] = {"$in": list(languages)}

    scoped_cursor = collection.find(scoped_query).sort([("_id", 1)])
    set_batch_size = getattr(scoped_cursor, "batch_size", None)
    if callable(set_batch_size):
        scoped_cursor = set_batch_size(safe_batch_size)
    for assertions in _iter_bounded_batches(
        scoped_cursor,
        batch_size=safe_batch_size,
    ):
        concept_visibility: Dict[str, bool] = {}
        batch_concept_ids: set[str] = set()
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            subject_id = str(assertion.get("subject_concept_id") or "")
            predicate_id = predicate_concept_id_for_storage(
                assertion.get("predicate")
            )
            if subject_id:
                batch_concept_ids.add(subject_id)
            if predicate_id:
                batch_concept_ids.add(predicate_id)
        _populate_concept_visibility(
            batch_concept_ids,
            user_id=user_id,
            org_id=org_id,
            concept_visibility=concept_visibility,
        )
        batch_docs = []
        stale_batch = []
        remaining = (
            None if safe_limit is None else max(safe_limit - yielded, 0)
        )
        for assertion in assertions:
            if (
                prune_stale
                and isinstance(assertion, Mapping)
                and assertion.get("status") != "asserted"
            ):
                assertion_id = str(assertion.get("assertion_id") or "").strip()
                if assertion_id:
                    stale_batch.append(
                        f"scoped_assertion:{assertion_id}"
                    )
                continue
            doc = _scoped_assertion_to_rag_doc(
                assertion,
                user_id=user_id,
                org_id=org_id,
                concept_visibility=concept_visibility,
            )
            if doc is None:
                if prune_stale and isinstance(
                    assertion,
                    Mapping,
                ):
                    assertion_id = str(
                        assertion.get("assertion_id") or ""
                    ).strip()
                    if assertion_id:
                        stale_batch.append(
                            f"scoped_assertion:{assertion_id}"
                        )
                continue
            batch_docs.append(doc)
            if remaining is not None and len(batch_docs) >= remaining:
                break
        _emit_stale_doc_batch(
            stale_batch,
            stale_doc_ids=stale_doc_ids,
            stale_doc_sink=stale_doc_sink,
        )
        for doc in batch_docs:
            yield doc
            yielded += 1
            if safe_limit is not None and yielded >= safe_limit:
                return


def _collect_scoped_text_assertion_docs(
    *,
    user_id: str,
    org_id: str,
    predicates: Optional[Sequence[str]],
    languages: Optional[Sequence[str]],
    concept_allow: set[str] | None,
    updated_since: datetime | None,
    skip: int,
    limit: int,
) -> List[TextRelationRagDoc]:
    """Collect one stable visible page while advancing through raw candidates.

    The public offset is in projected, currently visible rows. Raw Mongo
    offsets are advanced independently so revoked subjects or predicates do
    not shorten a non-terminal page or cause the next visible page to repeat.
    """

    if limit <= 0:
        return []
    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        return []

    query: Dict[str, Any] = {
        "scope.audience_keys": {
            "$in": [f"user:{user_id}", f"org:{org_id}"],
        },
        "status": "asserted",
        "object_kind": "text",
    }
    if updated_since is not None:
        query["updated_at"] = {"$gte": updated_since}
    if concept_allow:
        query["subject_concept_id"] = {"$in": sorted(concept_allow)}
    if predicates:
        query["predicate"] = {"$in": list(predicates)}
    if languages:
        query["object_text.language"] = {"$in": list(languages)}

    visible_skip = max(int(skip), 0)
    target_end = visible_skip + max(int(limit), 0)
    raw_batch_size = max(1, min(target_end, 1000))
    raw_offset = 0
    raw_exhausted = False
    visible_docs: List[TextRelationRagDoc] = []
    while len(visible_docs) < target_end and not raw_exhausted:
        raw_candidates = list(
            collection.find(query)
            .sort([("updated_at", -1), ("assertion_id", 1)])
            .skip(raw_offset)
            .limit(raw_batch_size)
        )
        raw_offset += len(raw_candidates)
        raw_exhausted = len(raw_candidates) < raw_batch_size
        if not raw_candidates:
            break
        concept_visibility: Dict[str, bool] = {}
        batch_concept_ids: set[str] = set()
        for assertion in raw_candidates:
            if not isinstance(assertion, dict):
                continue
            subject_id = str(assertion.get("subject_concept_id") or "")
            predicate_id = predicate_concept_id_for_storage(
                assertion.get("predicate")
            )
            if subject_id:
                batch_concept_ids.add(subject_id)
            if predicate_id:
                batch_concept_ids.add(predicate_id)
        _populate_concept_visibility(
            batch_concept_ids,
            user_id=user_id,
            org_id=org_id,
            concept_visibility=concept_visibility,
        )
        for assertion in raw_candidates:
            doc = _scoped_assertion_to_rag_doc(
                assertion,
                user_id=user_id,
                org_id=org_id,
                concept_visibility=concept_visibility,
            )
            if doc is not None:
                visible_docs.append(doc)
                if len(visible_docs) >= target_end:
                    break
    return visible_docs[visible_skip:target_end]


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

    if safe_limit == 0:
        return []

    # Combined paging is base-publication rows followed by actor-visible scoped
    # rows. Scan base rows until the requested page is full or base storage is
    # exhausted; only then map the remaining offset/budget into scoped storage.
    target_end = safe_skip + safe_limit
    scan_batch_size = max(1, min(target_end, 1000))
    raw_base_skip = 0
    base_exhausted = False
    base_docs: List[TextRelationRagDoc] = []
    language_allow = set(languages or ())

    while len(base_docs) < target_end and not base_exhausted:
        relations = list(
            TextRelationsRepository.find(
                rel_filter,
                sort=safe_sort,
                skip=raw_base_skip,
                limit=scan_batch_size,
            )
        )
        raw_base_skip += len(relations)
        base_exhausted = len(relations) < scan_batch_size
        if not relations:
            break
        concept_visibility: Dict[str, bool] = {}
        text_value_cache: Dict[str, Mapping[str, Any] | None] = {}
        _populate_concept_visibility(
            (
                concept_id
                for relation in relations
                if isinstance(relation, Mapping)
                for concept_id in (
                    str(relation.get("subject_concept_id") or ""),
                    str(
                        predicate_concept_id_for_storage(
                            relation.get("predicate")
                        )
                        or ""
                    ),
                )
            ),
            user_id=user_id,
            org_id=org_id,
            concept_visibility=concept_visibility,
        )
        _populate_text_value_cache(
            relations,
            text_value_cache=text_value_cache,
        )
        for relation in relations:
            doc = _base_relation_to_rag_doc(
                relation,
                user_id=user_id,
                org_id=org_id,
                concept_allow=concept_allow,
                language_allow=language_allow,
                concept_visibility=concept_visibility,
                text_value_cache=text_value_cache,
            )
            if doc is not None:
                base_docs.append(doc)
                if len(base_docs) >= target_end:
                    break

    page = base_docs[safe_skip:target_end]
    remaining = safe_limit - len(page)
    if remaining <= 0 or not base_exhausted:
        return page

    scoped_skip = max(safe_skip - len(base_docs), 0)
    scoped_docs = _collect_scoped_text_assertion_docs(
        user_id=user_id,
        org_id=org_id,
        predicates=predicates,
        languages=languages,
        concept_allow=concept_allow,
        updated_since=updated_since,
        skip=scoped_skip,
        limit=remaining,
    )
    page.extend(scoped_docs[:remaining])
    return page


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

    delete_batch_size = max(int(batch_size), 1)
    deleted = 0

    def _delete_stale_batch(stale_ids: Sequence[str]) -> None:
        nonlocal deleted
        deduplicated = list(dict.fromkeys(stale_ids))
        for start in range(0, len(deduplicated), delete_batch_size):
            deleted += int(
                rag.delete_documents(
                    deduplicated[start : start + delete_batch_size],
                    namespace=namespace,
                )
            )

    if skip == 0 and sort in (None, [("_id", 1)]):
        docs = list(
            iter_text_relation_docs_for_namespace(
                namespace=namespace,
                predicates=predicates,
                languages=languages,
                concept_ids=concept_ids,
                updated_since=updated_since,
                limit=limit,
                stale_doc_sink=_delete_stale_batch,
            )
        )
    else:
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
        **({"deleted": int(deleted)} if deleted else {}),
    }


def sync_scoped_assertions_to_rag(
    *,
    namespace: str,
    assertion_ids: Sequence[str],
) -> Dict[str, Any]:
    """Refresh exact scoped assertions without a base-relation scan budget."""

    user_id, org_id = _parse_namespace(namespace)
    requested_ids = sorted(
        {
            str(assertion_id).strip()
            for assertion_id in assertion_ids
            if isinstance(assertion_id, str)
            and str(assertion_id).strip().startswith("ska_")
        }
    )[:100]
    if not requested_ids:
        return {
            "success": False,
            "namespace": namespace,
            "error": "assertion_ids_required",
        }
    try:
        rag = get_rag_service()
    except RAGBackendUnavailable as exc:
        return {"success": False, "error": f"RAG service unavailable: {exc}"}

    collection = get_scoped_knowledge_assertions_collection()
    docs: List[TextRelationRagDoc] = []
    if collection is not None:
        assertions = list(
            collection.find(
                {
                    "assertion_id": {"$in": requested_ids},
                    "status": "asserted",
                    "object_kind": "text",
                    "scope.audience_keys": {
                        "$in": [f"user:{user_id}", f"org:{org_id}"],
                    },
                }
            )
        )
        concept_visibility: Dict[str, bool] = {}
        assertion_concept_ids: set[str] = set()
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            subject_id = str(assertion.get("subject_concept_id") or "")
            predicate_id = predicate_concept_id_for_storage(
                assertion.get("predicate")
            )
            if subject_id:
                assertion_concept_ids.add(subject_id)
            if predicate_id:
                assertion_concept_ids.add(predicate_id)
        _populate_concept_visibility(
            assertion_concept_ids,
            user_id=user_id,
            org_id=org_id,
            concept_visibility=concept_visibility,
        )
        for assertion in assertions:
            doc = _scoped_assertion_to_rag_doc(
                assertion,
                user_id=user_id,
                org_id=org_id,
                concept_visibility=concept_visibility,
            )
            if doc is not None:
                docs.append(doc)

    payload = [
        {"id": doc.doc_id, "text": doc.text, "metadata": doc.metadata}
        for doc in docs
    ]
    added = 0
    failed = 0
    if payload:
        ok, bad = rag.upsert_documents(payload, namespace=namespace)
        added = int(ok)
        failed = int(bad)

    indexed_ids = {
        str(doc.metadata.get("assertion_id") or "").strip() for doc in docs
    }
    stale_ids = [
        f"scoped_assertion:{assertion_id}"
        for assertion_id in requested_ids
        if assertion_id not in indexed_ids
    ]
    deleted = rag.delete_documents(stale_ids, namespace=namespace) if stale_ids else 0
    return {
        "success": failed == 0,
        "namespace": namespace,
        "requested": len(requested_ids),
        "added": added,
        "failed": failed,
        "deleted": int(deleted),
        "sync_scope": "exact_scoped_assertions",
    }


def submit_durable_rag_sync(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    predicates: Optional[Sequence[str]] = None,
    languages: Optional[Sequence[str]] = None,
    concept_ids: Optional[Sequence[str]] = None,
    limit: int = 5000,
    batch_size: int = 200,
) -> Dict[str, Any]:
    """Submit a durable workflow to sync text relations to RAG.

    This is the non-blocking alternative to sync_text_relations_to_rag().
    The workflow executes in the background with checkpointing support.

    Args:
        namespace: The namespace to sync (e.g., "#V#user@org").
        user_id: User concept ID initiating the sync.
        org_id: Organisation concept ID for context.
        predicates: Optional list of predicate IDs to filter.
        languages: Optional list of language codes to filter.
        concept_ids: Optional list of concept IDs to filter.
        limit: Maximum documents to sync (default: 5000).
        batch_size: Documents per batch (default: 200).

    Returns:
        Dict with:
            - success: True if workflow was submitted
            - instance_id: The workflow instance ID for status queries
            - error: Error message if submission failed
    """
    # Check if durable workflows are enabled
    from .feature_flags import get_durable_workflows_enabled

    enabled = get_durable_workflows_enabled(default=False)
    if not enabled:
        return {
            "success": False,
            "error": "durable_workflows_disabled",
            "hint": "Set VON_DURABLE_WORKFLOWS_ENABLE=1 to enable durable workflows",
        }

    try:
        from ..workflows.durable.startup import get_instance_manager
        from ..workflows.durable.rag_sync_workflow import (
            RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
        )
        from ..workflows.durable.workflow_instance_submission_service import (
            submit_verified_workflow_instance,
        )

        instance_manager = get_instance_manager()

        # Build workflow inputs
        inputs: Dict[str, Any] = {
            "namespace": namespace,
            "limit": limit,
            "batch_size": batch_size,
        }
        if predicates:
            inputs["predicates"] = list(predicates)
        if languages:
            inputs["languages"] = list(languages)
        if concept_ids:
            inputs["concept_ids"] = list(concept_ids)

        submission = submit_verified_workflow_instance(
            manager=instance_manager,
            workflow_id=RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=inputs,
        )
        if not submission.success:
            return {
                "success": False,
                "workflow_id": RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
                "namespace": namespace,
                "error": str(submission.error or "workflow_submission_failed"),
                "error_code": submission.error_code,
                "verification": dict(submission.verification),
                "submission_status": submission.status,
                "instance_id": submission.instance_id,
            }

        return {
            "success": True,
            "instance_id": submission.instance_id,
            "workflow_id": RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
            "namespace": namespace,
            "verification": dict(submission.verification),
            "submission_status": submission.status,
            "created_new": submission.created_new,
        }

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }
