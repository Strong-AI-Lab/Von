from __future__ import annotations
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Iterable, Literal, Mapping, Set
from pymongo.collection import Collection
from pymongo.database import Database
import logging

from ..mongo_client import get_db, get_concepts_collection
from ...security.access_control import (
    apply_concept_query_filter,
    apply_pipeline_filter,
    prewarm_concept_relationship_access,
    sanitize_concept_document,
)


# --- Legacy Field Guard -------------------------------------------------
def _sanitize_update_payload(update: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any attempt to write legacy top-level 'description'.

    Canonical descriptive text must be stored via text value relations (predicate hasDescription).
    This guard prevents silent reintroduction of deprecated schema fields which can confuse
    accessor logic and downstream tooling.
    """
    try:
        if not update:
            return update
        # Modifier style ($set etc.)
        if any(k.startswith("$") for k in update.keys()):
            set_ops = update.get("$set")
            if isinstance(set_ops, dict) and "description" in set_ops:
                logger.warning(
                    "[legacy_field_guard] Stripping top-level description from $set update; use relation hasDescription instead"
                )
                set_ops.pop("description", None)
                if not set_ops:
                    update.pop("$set", None)
            return update
        # Replacement style (full document)
        if "description" in update:
            logger.warning(
                "[legacy_field_guard] Stripping top-level description from replacement document; use relation hasDescription instead"
            )
            update = dict(update)
            update.pop("description", None)
        return update
    except Exception as e:  # pragma: no cover
        logger.error(f"[legacy_field_guard] Failed to sanitize update payload: {e}")
        return update


logger = logging.getLogger(__name__)


RELATIONSHIP_KINDS: tuple[str, ...] = (
    "is_a_type_of",
    "has_subtype",
    "is_an_instance_of",
    "has_instance",
    "related_to",
    "#V#authored_by",
    "#V#has_author",
)

_EMBEDDING_STATUS_PENDING = "pending"
_EMBEDDING_STATUS_STALE = "stale"
_EMBEDDING_INPUT_FIELD_PREFIXES: tuple[str, ...] = (
    "concept_id",
    "guid",
    "name",
    "names",
    "description",
    "notes",
    "relationships",
    "system_tags",
    "attributes.description",
    "attributes.notes",
    # Preserve the former updated_at > embedding_updated_at fallback without
    # making every queue poll perform a collection scan.
    "updated_at",
)


def _field_affects_concept_embedding(field_name: Any) -> bool:
    if not isinstance(field_name, str) or not field_name:
        return False
    return any(
        field_name == prefix or field_name.startswith(f"{prefix}.")
        for prefix in _EMBEDDING_INPUT_FIELD_PREFIXES
    )


def _prepare_concept_embedding_status(update: Dict[str, Any]) -> Dict[str, Any]:
    """Materialise embedding queue state for supported concept mutations.

    The concept embedding worker must be able to select work through an index.
    Semantic updates and updates that advance ``updated_at`` therefore mark the
    concept stale at write time. Explicit embedding-status mutations remain
    authoritative so the worker can publish ``indexed`` without immediately
    turning its own write back into work.
    """

    if not update:
        return update

    modifier_update = any(str(key).startswith("$") for key in update)
    if modifier_update:
        modified_fields: set[str] = set()
        for payload in update.values():
            if isinstance(payload, Mapping):
                modified_fields.update(
                    field for field in payload if isinstance(field, str)
                )

        if any(
            field == "embedding_status" or field.startswith("embedding_status.")
            for field in modified_fields
        ):
            return update
        if not any(
            _field_affects_concept_embedding(field) for field in modified_fields
        ):
            return update

        prepared = dict(update)
        set_payload = dict(prepared.get("$set") or {})
        set_payload["embedding_status"] = _EMBEDDING_STATUS_STALE
        prepared["$set"] = set_payload
        return prepared

    if "embedding_status" in update:
        return update
    if not any(_field_affects_concept_embedding(field) for field in update):
        return update
    prepared = dict(update)
    prepared["embedding_status"] = _EMBEDDING_STATUS_STALE
    return prepared


class _AccessControlledCursor:
    """Wrap a PyMongo cursor to sanitise concept documents on iteration."""

    _SANITISATION_BATCH_SIZE = 64

    def __init__(self, cursor, *, sanitisation_batch_size: int | None = None):
        self._cursor = cursor
        self._buffer: Deque[Dict[str, Any]] = deque()
        self._exhausted = False
        requested_batch_size = (
            self._SANITISATION_BATCH_SIZE
            if sanitisation_batch_size is None
            else sanitisation_batch_size
        )
        self._sanitisation_batch_size = max(1, requested_batch_size)

    def __iter__(self):
        return self

    def __next__(self):
        while not self._buffer:
            if self._exhausted:
                raise StopIteration
            batch: List[Dict[str, Any]] = []
            for _ in range(self._sanitisation_batch_size):
                try:
                    batch.append(next(self._cursor))
                except StopIteration:
                    self._exhausted = True
                    break
            prewarm_concept_relationship_access(batch)
            for doc in batch:
                sanitised = sanitize_concept_document(doc)
                if sanitised is not None:
                    self._buffer.append(sanitised)
        return self._buffer.popleft()

    def __getattr__(self, item):  # pragma: no cover - simple proxy
        return getattr(self._cursor, item)


class ConceptsRepository:
    """Single point of access for the 'concepts' collection.
    All direct DB operations on concepts should go through this class.
    """

    @staticmethod
    def collection() -> Collection | None:
        return get_concepts_collection()

    @staticmethod
    def db() -> Database | None:
        return get_db()

    # Basic wrappers
    @staticmethod
    def find_one(
        filter: Dict[str, Any], projection: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        coll = ConceptsRepository.collection()
        if coll is None:
            return None
        query = apply_concept_query_filter(filter or {})
        result = coll.find_one(query, projection)
        return sanitize_concept_document(result)

    @staticmethod
    def find(
        filter: Dict[str, Any],
        projection: Optional[Dict[str, Any]] = None,
        sort: Optional[List] = None,
        skip: int = 0,
        limit: int = 0,
        max_time_ms: Optional[int] = None,
        sanitisation_batch_size: Optional[int] = None,
        cursor_batch_size: Optional[int] = None,
    ):
        coll = ConceptsRepository.collection()
        if coll is None:
            return []
        query = apply_concept_query_filter(filter or {})
        cursor = coll.find(query, projection)
        if max_time_ms and max_time_ms > 0:
            cursor = cursor.max_time_ms(max_time_ms)
        if cursor_batch_size and cursor_batch_size > 0:
            cursor = cursor.batch_size(cursor_batch_size)
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit:
            cursor = cursor.limit(limit)
        return _AccessControlledCursor(
            cursor,
            sanitisation_batch_size=sanitisation_batch_size,
        )

    @staticmethod
    def insert_one(document: Dict[str, Any]):
        coll = ConceptsRepository.collection()
        if coll is None:
            raise RuntimeError("Concepts collection not available")
        prepared = dict(document)
        prepared.setdefault("embedding_status", _EMBEDDING_STATUS_PENDING)
        return coll.insert_one(prepared)

    @staticmethod
    def update_one(
        filter: Dict[str, Any], update: Dict[str, Any], upsert: bool = False
    ):
        coll = ConceptsRepository.collection()
        if coll is None:
            raise RuntimeError("Concepts collection not available")
        update = _sanitize_update_payload(update)
        update = _prepare_concept_embedding_status(update)
        query = apply_concept_query_filter(filter or {})
        return coll.update_one(query, update, upsert=upsert)

    @staticmethod
    def update_many(filter: Dict[str, Any], update: Dict[str, Any]):
        coll = ConceptsRepository.collection()
        if coll is None:
            raise RuntimeError("Concepts collection not available")
        update = _sanitize_update_payload(update)
        update = _prepare_concept_embedding_status(update)
        query = apply_concept_query_filter(filter or {})
        return coll.update_many(query, update)

    @staticmethod
    def find_one_and_update(
        filter: Dict[str, Any], update: Dict[str, Any], return_document: bool = False
    ):
        coll = ConceptsRepository.collection()
        if coll is None:
            return None
        update = _sanitize_update_payload(update)
        update = _prepare_concept_embedding_status(update)
        query = apply_concept_query_filter(filter or {})
        result = coll.find_one_and_update(
            query, update, return_document=return_document
        )
        return sanitize_concept_document(result)

    @staticmethod
    def delete_one(filter: Dict[str, Any]):
        coll = ConceptsRepository.collection()
        if coll is None:
            raise RuntimeError("Concepts collection not available")
        query = apply_concept_query_filter(filter or {})
        return coll.delete_one(query)

    @staticmethod
    def delete_many(filter: Dict[str, Any]):
        coll = ConceptsRepository.collection()
        if coll is None:
            raise RuntimeError("Concepts collection not available")
        query = apply_concept_query_filter(filter or {})
        return coll.delete_many(query)

    @staticmethod
    def count_documents(filter: Dict[str, Any]) -> int:
        coll = ConceptsRepository.collection()
        if coll is None:
            return 0
        query = apply_concept_query_filter(filter or {})
        return coll.count_documents(query)

    @staticmethod
    def aggregate(pipeline: List[Dict[str, Any]]):
        coll = ConceptsRepository.collection()
        if coll is None:
            return []
        pipeline_with_access = apply_pipeline_filter(pipeline or [])
        cursor = coll.aggregate(pipeline_with_access)
        return _AccessControlledCursor(cursor)

    @staticmethod
    def distinct(field: str, filter: Optional[Dict[str, Any]] = None) -> List[Any]:
        """Return distinct values for a field while preserving concept access control."""

        coll = ConceptsRepository.collection()
        if coll is None:
            return []
        query = apply_concept_query_filter(filter or {})
        return list(coll.distinct(field, query))

    # Relationship helpers
    @staticmethod
    def _ensure_relationship_array(concept_id: str, rel_kind: str) -> bool:
        """Ensure relationships.<rel_kind> is stored as a list for the concept."""
        doc = ConceptsRepository.find_one(
            {"concept_id": concept_id},
            {f"relationships.{rel_kind}": 1, "concept_id": 1},
        )
        if not doc:
            raise ValueError(
                f"Concept '{concept_id}' not found while normalising relationships"
            )

        rels = doc.get("relationships") or {}
        curr = rels.get(rel_kind)
        if isinstance(curr, list):
            return False

        if isinstance(curr, str) and curr.strip():
            ConceptsRepository.update_one(
                {"concept_id": concept_id},
                {"$set": {f"relationships.{rel_kind}": [curr]}},
            )
            return True

        ConceptsRepository.update_one(
            {"concept_id": concept_id}, {"$set": {f"relationships.{rel_kind}": []}}
        )
        return True

    @staticmethod
    def mutate_relationship_edge(
        source_id: str,
        kind: str,
        target_id: str,
        *,
        action: Literal["add", "remove"],
        maintain_inverse: bool = True,
    ) -> bool:
        """Mutate a relationship edge while optionally maintaining its structural inverse."""

        if action not in {"add", "remove"}:
            raise ValueError(f"Unsupported relationship action '{action}'")

        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source_id must be a non-empty string")
        if not isinstance(target_id, str) or not target_id:
            raise ValueError("target_id must be a non-empty string")

        normalised = ConceptsRepository._ensure_relationship_array(source_id, kind)

        if action == "add":
            result = ConceptsRepository.update_one(
                {"concept_id": source_id},
                {"$addToSet": {f"relationships.{kind}": target_id}},
            )
        else:
            result = ConceptsRepository.update_one(
                {"concept_id": source_id},
                {"$pull": {f"relationships.{kind}": target_id}},
            )

        changed = normalised or (result.modified_count > 0)

        if maintain_inverse:
            inverse_map = {
                "is_a_type_of": ("has_subtype", target_id, source_id),
                "has_subtype": ("is_a_type_of", target_id, source_id),
                "is_an_instance_of": ("has_instance", target_id, source_id),
                "has_instance": ("is_an_instance_of", target_id, source_id),
                "related_to": ("related_to", target_id, source_id),
            }
            inverse_spec = inverse_map.get(kind)
            if inverse_spec:
                inv_kind, inv_source, inv_target = inverse_spec
                inverse_changed = ConceptsRepository.mutate_relationship_edge(
                    inv_source,
                    inv_kind,
                    inv_target,
                    action=action,
                    maintain_inverse=False,
                )
                changed = changed or inverse_changed

        if changed:
            try:
                from ...services.relationship_extent_index_service import (
                    sync_relationship_extent_index_for_concept_id,
                )

                sync_relationship_extent_index_for_concept_id(source_id)
            except Exception:
                logger.debug(
                    "[relationship_extent_index] Best-effort sync failed for %s",
                    source_id,
                    exc_info=True,
                )

        return changed

    @staticmethod
    def reconcile_relationships(
        concept_id: str,
        new_relationships: Mapping[str, Iterable[str]] | None,
        *,
        previous_relationships: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        """Ensure stored relationship edges have their inverses kept in sync.

        This helper compares the relationships assigned to ``concept_id`` with any
        previously persisted values. It adds newly declared edges and removes
        edges that have been dropped so that inverse structures stay coherent.
        Missing target concepts are ignored to keep imports resilient.
        """

        if not isinstance(concept_id, str) or not concept_id:
            raise ValueError("concept_id must be a non-empty string")

        def _collect_targets(
            source: Mapping[str, Iterable[str]] | None,
        ) -> Dict[str, Set[str]]:
            collected: Dict[str, Set[str]] = {}
            if not source:
                return collected
            try:
                from ...services.concept_predicate_metadata_service import get_relationship_kinds
                kinds: tuple[str, ...] = get_relationship_kinds()
            except Exception:
                kinds = RELATIONSHIP_KINDS
            for rel_kind in kinds:
                values = source.get(rel_kind)  # type: ignore[index]
                if values is None:
                    continue
                targets: Set[str] = set()
                if isinstance(values, str):
                    if values.strip():
                        targets.add(values)
                else:
                    for candidate in values:
                        if isinstance(candidate, str) and candidate.strip():
                            targets.add(candidate)
                if targets:
                    collected[rel_kind] = targets
            return collected

        latest = _collect_targets(new_relationships)
        previous = _collect_targets(previous_relationships)

        for rel_kind, targets in latest.items():
            for target in targets:
                try:
                    ConceptsRepository.mutate_relationship_edge(
                        concept_id,
                        rel_kind,
                        target,
                        action="add",
                    )
                except ValueError:
                    logger.debug(
                        "[relationship_reconcile] Skipped adding %s -> %s (%s missing)",
                        concept_id,
                        target,
                        rel_kind,
                    )

        for rel_kind, previous_targets in previous.items():
            removals = previous_targets - latest.get(rel_kind, set())
            for target in removals:
                try:
                    ConceptsRepository.mutate_relationship_edge(
                        concept_id,
                        rel_kind,
                        target,
                        action="remove",
                    )
                except ValueError:
                    logger.debug(
                        "[relationship_reconcile] Skipped removing %s -> %s (%s missing)",
                        concept_id,
                        target,
                        rel_kind,
                    )
