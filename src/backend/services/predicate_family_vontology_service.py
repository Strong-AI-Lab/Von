"""Read represented relation-predicate family metadata from Vontology."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from .relationship_extent_index_service import query_relationship_extent_index

INVERSE_LINK_PREDICATE_IDS = (
    "#V#predicate_is_inverse_of_predicate",
    "#V#inverse_predicates",
)
FAMILY_MEMBER_PREDICATE_ID = "#V#has_member_predicate"
FOCAL_ROLE_PREDICATE_ID = "#V#has_focal_role_predicate"
RELATED_ROLE_PREDICATE_ID = "#V#has_related_role_predicate"
REIFIED_RELATION_TYPE_PREDICATE_ID = "#V#has_reified_relation_type"
PREDICATE_SPECIALISATION_PREDICATE_ID = "#V#predicate_specialises_predicate"
_MAX_EXPANDED_PREDICATES = 100


def _canonical_ids(value: Any) -> list[str]:
    values = (
        value
        if isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        else [value]
    )
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        concept_id = item.strip()
        if not concept_id.startswith("#V#") or concept_id in result:
            continue
        result.append(concept_id)
    return result


def _append_unique(target: list[str], values: Any, *, limit: int) -> None:
    for concept_id in _canonical_ids(values):
        if concept_id not in target:
            target.append(concept_id)
        if len(target) >= limit:
            return


def _relationships(document: Mapping[str, Any]) -> Mapping[str, Any]:
    relationships = document.get("relationships")
    return relationships if isinstance(relationships, Mapping) else {}


def _empty_expansion(seeds: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "relation_predicate_family_expansion.v1",
        "seed_predicate_ids": seeds,
        "predicate_ids": list(seeds),
        "family_concept_ids": [],
        "focal_role_predicate_ids": [],
        "related_role_predicate_ids": [],
        "reified_relation_type_ids": [],
        "entailing_specialisation_predicate_ids": [],
        "source": "vontology_predicate_schema",
    }


def expand_relation_predicate_family(
    predicate_ids: Sequence[str],
    *,
    max_predicates: int = 32,
) -> dict[str, Any]:
    """Expand predicate IDs through represented inverse and family metadata.

    Predicate documents are resolved by their exact indexed IDs. Family
    membership is discovered through the target-first relationship extent
    index, avoiding a scan across dynamic ``relationships`` keys. If that
    derived index is unavailable, the supplied predicate IDs and any directly
    represented inverse links remain usable.
    """

    try:
        requested_limit = int(max_predicates or 32)
    except (TypeError, ValueError):
        requested_limit = 32
    resolved_limit = max(1, min(requested_limit, _MAX_EXPANDED_PREDICATES))
    seeds = _canonical_ids(predicate_ids)[:resolved_limit]
    result = _empty_expansion(seeds)
    if not seeds:
        result["status"] = "unchanged"
        return result

    expanded = list(seeds)
    family_ids: list[str] = []
    focal_role_predicate_ids: list[str] = []
    related_role_predicate_ids: list[str] = []
    reified_relation_type_ids: list[str] = []
    index_status = "available"
    family_discovery_source = "relationship_extent_index"
    specialisation_discovery_source = "relationship_extent_index"
    entailing_specialisations: list[str] = []

    try:
        predicate_projection: dict[str, int] = {
            "_id": 0,
            "concept_id": 1,
        }
        for inverse_predicate_id in INVERSE_LINK_PREDICATE_IDS:
            predicate_projection[f"relationships.{inverse_predicate_id}"] = 1
        predicate_documents = ConceptsRepository.find(
            {"concept_id": {"$in": list(seeds)}},
            projection=predicate_projection,
        )
        for document in predicate_documents:
            relationships = _relationships(document)
            for inverse_predicate_id in INVERSE_LINK_PREDICATE_IDS:
                _append_unique(
                    expanded,
                    relationships.get(inverse_predicate_id),
                    limit=resolved_limit,
                )

        # Directional entailment expansion: when a query asks for a general
        # predicate, include facts stated with predicates that specialise it.
        # Never follow the edge from a specialisation to its generalisation,
        # because that would make a generic fact satisfy a narrow query (and,
        # for memberOfVonOrg, would turn description into authority).
        frontier = list(expanded)
        while frontier and len(expanded) < resolved_limit:
            specialisation_rows, specialisation_count = query_relationship_extent_index(
                predicate_id=PREDICATE_SPECIALISATION_PREDICATE_ID,
                target_values=frontier,
                count_total=False,
                projection={
                    "_id": 0,
                    "source_concept_id": 1,
                    "predicate_id": 1,
                    "target_value": 1,
                },
                limit=resolved_limit * 4,
            )
            if specialisation_count < 0:
                specialisation_discovery_source = "canonical_exact_predicate_query"
                specialisation_documents = ConceptsRepository.find(
                    {
                        f"relationships.{PREDICATE_SPECIALISATION_PREDICATE_ID}": {
                            "$in": frontier
                        }
                    },
                    projection={"_id": 0, "concept_id": 1},
                    limit=resolved_limit,
                )
                candidates = [
                    document.get("concept_id")
                    for document in specialisation_documents
                    if isinstance(document, Mapping)
                ]
            else:
                candidates = [
                    row.get("source_concept_id")
                    for row in specialisation_rows
                    if isinstance(row, Mapping)
                ]
            next_frontier: list[str] = []
            for candidate in _canonical_ids(candidates):
                if candidate in expanded:
                    continue
                expanded.append(candidate)
                entailing_specialisations.append(candidate)
                next_frontier.append(candidate)
                if len(expanded) >= resolved_limit:
                    break
            frontier = next_frontier

        family_rows, family_row_count = query_relationship_extent_index(
            predicate_id=FAMILY_MEMBER_PREDICATE_ID,
            target_values=expanded,
            count_total=False,
            projection={
                "_id": 0,
                "source_concept_id": 1,
                "predicate_id": 1,
                "target_value": 1,
            },
            limit=resolved_limit * 4,
        )
        if family_row_count < 0:
            index_status = "unavailable"
            family_discovery_source = "canonical_exact_predicate_query"
            family_projection = {
                "_id": 0,
                "concept_id": 1,
                f"relationships.{FAMILY_MEMBER_PREDICATE_ID}": 1,
                f"relationships.{FOCAL_ROLE_PREDICATE_ID}": 1,
                f"relationships.{RELATED_ROLE_PREDICATE_ID}": 1,
                f"relationships.{REIFIED_RELATION_TYPE_PREDICATE_ID}": 1,
            }
            family_documents = list(
                ConceptsRepository.find(
                    {
                        f"relationships.{FAMILY_MEMBER_PREDICATE_ID}": {
                            "$in": list(expanded)
                        }
                    },
                    projection=family_projection,
                    limit=resolved_limit,
                )
            )
            for document in family_documents:
                family_id = document.get("concept_id")
                if isinstance(family_id, str):
                    _append_unique(
                        family_ids,
                        [family_id],
                        limit=resolved_limit,
                    )
        else:
            family_documents = []
            for row in family_rows:
                family_id = row.get("source_concept_id")
                if isinstance(family_id, str):
                    _append_unique(family_ids, [family_id], limit=resolved_limit)

        if family_ids:
            if not family_documents:
                family_projection = {
                    "_id": 0,
                    "concept_id": 1,
                    f"relationships.{FAMILY_MEMBER_PREDICATE_ID}": 1,
                    f"relationships.{FOCAL_ROLE_PREDICATE_ID}": 1,
                    f"relationships.{RELATED_ROLE_PREDICATE_ID}": 1,
                    f"relationships.{REIFIED_RELATION_TYPE_PREDICATE_ID}": 1,
                }
                family_documents = ConceptsRepository.find(
                    {"concept_id": {"$in": family_ids}},
                    projection=family_projection,
                )
            accessible_family_ids: list[str] = []
            for document in family_documents:
                family_id = document.get("concept_id")
                if isinstance(family_id, str):
                    _append_unique(
                        accessible_family_ids,
                        [family_id],
                        limit=resolved_limit,
                    )
                relationships = _relationships(document)
                _append_unique(
                    expanded,
                    relationships.get(FAMILY_MEMBER_PREDICATE_ID),
                    limit=resolved_limit,
                )
                _append_unique(
                    focal_role_predicate_ids,
                    relationships.get(FOCAL_ROLE_PREDICATE_ID),
                    limit=resolved_limit,
                )
                _append_unique(
                    related_role_predicate_ids,
                    relationships.get(RELATED_ROLE_PREDICATE_ID),
                    limit=resolved_limit,
                )
                _append_unique(
                    reified_relation_type_ids,
                    relationships.get(REIFIED_RELATION_TYPE_PREDICATE_ID),
                    limit=resolved_limit,
                )
            family_ids = accessible_family_ids
    except Exception as exc:
        result.update(
            {
                "status": "schema_read_failed",
                "error_class": type(exc).__name__,
                "relationship_extent_index_status": index_status,
            }
        )
        return result

    result.update(
        {
            "predicate_ids": expanded[:resolved_limit],
            "family_concept_ids": family_ids,
            "focal_role_predicate_ids": focal_role_predicate_ids,
            "related_role_predicate_ids": related_role_predicate_ids,
            "reified_relation_type_ids": reified_relation_type_ids,
            "entailing_specialisation_predicate_ids": entailing_specialisations,
            "status": "expanded" if expanded != seeds else "unchanged",
            "relationship_extent_index_status": index_status,
            "family_discovery_source": family_discovery_source,
            "specialisation_discovery_source": specialisation_discovery_source,
        }
    )
    return result


__all__ = [
    "FAMILY_MEMBER_PREDICATE_ID",
    "FOCAL_ROLE_PREDICATE_ID",
    "INVERSE_LINK_PREDICATE_IDS",
    "PREDICATE_SPECIALISATION_PREDICATE_ID",
    "RELATED_ROLE_PREDICATE_ID",
    "REIFIED_RELATION_TYPE_PREDICATE_ID",
    "expand_relation_predicate_family",
]
