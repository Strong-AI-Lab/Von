"""Live authority checks for scoped assertions returned from the RAG index."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from ..db.mongo_client import get_scoped_knowledge_assertions_collection
from ..db.repositories.text_value_repository import TextRelationsRepository
from ..security.access_control import (
    filter_accessible_concept_ids,
    override_current_actor,
)
from .text_relation_predicate_validation_service import (
    predicate_concept_id_for_storage,
)


def _clean_concept_id(value: Any) -> str | None:
    token = str(value or "").strip()
    return token if token.startswith("#V#") else None


def _clean_assertion_id(value: Any) -> str | None:
    token = str(value or "").strip()
    return token if token.startswith("ska_") else None


def _clean_relation_id(value: Any) -> str | None:
    token = str(value or "").strip()
    return token or None


def _candidate_kind(metadata: Mapping[str, Any]) -> str | None:
    kind = str(metadata.get("type") or "").strip()
    source = str(metadata.get("source") or "").strip()
    if kind == "scoped_knowledge_assertion" or source == (
        "scoped_knowledge_assertion"
    ):
        return "scoped_knowledge_assertion"
    if kind == "text_relation" or source == "vontology_text_relation":
        return "text_relation"
    return None


def _relation_lookup_values(relation_ids: Sequence[str]) -> list[Any]:
    values: list[Any] = []
    seen: set[tuple[type[Any], str]] = set()
    for relation_id in relation_ids:
        candidates: list[Any] = [relation_id]
        try:
            candidates.insert(0, ObjectId(relation_id))
        except (InvalidId, TypeError):
            pass
        for candidate in candidates:
            key = (type(candidate), str(candidate))
            if key in seen:
                continue
            seen.add(key)
            values.append(candidate)
    return values


def current_authorised_rag_candidate_keys(
    metadata_rows: Sequence[Mapping[str, Any]],
    *,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
) -> set[tuple[str, str]]:
    """Return live-authorised canonical keys for knowledge RAG candidates."""

    scoped_metadata: dict[str, Mapping[str, Any]] = {}
    relation_metadata: dict[str, Mapping[str, Any]] = {}
    for metadata in metadata_rows:
        kind = _candidate_kind(metadata)
        if kind == "scoped_knowledge_assertion":
            assertion_id = _clean_assertion_id(metadata.get("assertion_id"))
            if assertion_id:
                scoped_metadata[assertion_id] = metadata
        elif kind == "text_relation":
            relation_id = _clean_relation_id(metadata.get("relation_id"))
            if relation_id:
                relation_metadata[relation_id] = metadata
    if not scoped_metadata and not relation_metadata:
        return set()

    actor_user_id = _clean_concept_id(user_concept_id)
    actor_org_id = _clean_concept_id(organisation_concept_id)
    audience_keys = []
    if actor_user_id:
        audience_keys.append(f"user:{actor_user_id}")
    if actor_org_id:
        audience_keys.append(f"org:{actor_org_id}")
    if not audience_keys:
        return set()

    current_rows: dict[
        tuple[str, str],
        tuple[str, str, str],
    ] = {}
    concept_ids: set[str] = set()

    if relation_metadata:
        relations = TextRelationsRepository.find(
            {
                "_id": {
                    "$in": _relation_lookup_values(
                        list(relation_metadata)
                    )
                }
            },
            {
                "_id": 1,
                "subject_concept_id": 1,
                "predicate": 1,
            },
        )
        for relation in relations:
            if not isinstance(relation, Mapping):
                continue
            relation_id = _clean_relation_id(relation.get("_id"))
            subject_id = _clean_concept_id(
                relation.get("subject_concept_id")
            )
            predicate = str(relation.get("predicate") or "").strip()
            predicate_id = predicate_concept_id_for_storage(predicate)
            if (
                not relation_id
                or relation_id not in relation_metadata
                or not subject_id
                or not predicate
                or not predicate_id
            ):
                continue
            current_rows[("text_relation", relation_id)] = (
                subject_id,
                predicate,
                predicate_id,
            )
            concept_ids.update((subject_id, predicate_id))

    collection = (
        get_scoped_knowledge_assertions_collection()
        if scoped_metadata
        else None
    )
    if collection is not None:
        assertions = collection.find(
            {
                "assertion_id": {"$in": sorted(scoped_metadata)},
                "status": "asserted",
                "object_kind": "text",
                "scope.audience_keys": {"$in": audience_keys},
            },
            {
                "_id": 0,
                "assertion_id": 1,
                "subject_concept_id": 1,
                "predicate": 1,
            },
        )
        for assertion in assertions:
            if not isinstance(assertion, Mapping):
                continue
            assertion_id = _clean_assertion_id(
                assertion.get("assertion_id")
            )
            subject_id = _clean_concept_id(
                assertion.get("subject_concept_id")
            )
            predicate = str(assertion.get("predicate") or "").strip()
            predicate_id = predicate_concept_id_for_storage(predicate)
            if (
                not assertion_id
                or not subject_id
                or not predicate
                or not predicate_id
            ):
                continue
            current_rows[("scoped_knowledge_assertion", assertion_id)] = (
                subject_id,
                predicate,
                predicate_id,
            )
            concept_ids.update((subject_id, predicate_id))

    with override_current_actor(actor_user_id, actor_org_id):
        visible_concept_ids = filter_accessible_concept_ids(concept_ids)

    allowed: set[tuple[str, str]] = set()
    for key, (subject_id, predicate, predicate_id) in current_rows.items():
        kind, candidate_id = key
        metadata = (
            relation_metadata.get(candidate_id)
            if kind == "text_relation"
            else scoped_metadata.get(candidate_id)
        )
        if metadata is None:
            continue
        if str(metadata.get("subject_concept_id") or "").strip() != subject_id:
            continue
        if str(metadata.get("predicate") or "").strip() != predicate:
            continue
        if subject_id not in visible_concept_ids:
            continue
        if predicate_id not in visible_concept_ids:
            continue
        allowed.add(key)
    return allowed


def current_authorised_scoped_assertion_ids(
    metadata_rows: Sequence[Mapping[str, Any]],
    *,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
) -> set[str]:
    """Return candidate IDs that remain asserted, visible, and in audience.

    RAG is a derived index, so its metadata is only a candidate locator. This
    function re-reads the scoped assertion store and batches current Vontology
    visibility checks for both the assertion subject and represented predicate.
    """

    return {
        candidate_id
        for kind, candidate_id in current_authorised_rag_candidate_keys(
            metadata_rows,
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
        )
        if kind == "scoped_knowledge_assertion"
    }


__all__ = [
    "current_authorised_rag_candidate_keys",
    "current_authorised_scoped_assertion_ids",
]
