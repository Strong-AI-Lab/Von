"""Visibility predicate helpers.

Visibility predicates are represented Vontology relationships. The canonical
storage predicates are ``#V#specific_to_user`` and
``#V#specific_to_organisation``. Read helpers deliberately keep legacy aliases
for the migration window, but write helpers emit canonical predicates only and
clear legacy variants from the relationship dict they update.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

CANONICAL_SPECIFIC_TO_USER_PREDICATE = "#V#specific_to_user"
CANONICAL_SPECIFIC_TO_ORG_PREDICATE = "#V#specific_to_organisation"

LEGACY_SPECIFIC_TO_USER_PREDICATES: tuple[str, ...] = ("specific_to_user",)

LEGACY_SPECIFIC_TO_ORG_PREDICATES: tuple[str, ...] = (
    "specific_to_org",
    "specific_to_organisation",
    "#V#specific_to_org",
)

SPECIFIC_TO_USER_PREDICATES: tuple[str, ...] = (
    *LEGACY_SPECIFIC_TO_USER_PREDICATES,
    CANONICAL_SPECIFIC_TO_USER_PREDICATE,
)

SPECIFIC_TO_ORG_PREDICATES_READ: tuple[str, ...] = (
    *LEGACY_SPECIFIC_TO_ORG_PREDICATES,
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
)

SPECIFIC_TO_USER_PREDICATES_WRITE: tuple[str, ...] = (
    CANONICAL_SPECIFIC_TO_USER_PREDICATE,
)

SPECIFIC_TO_ORG_PREDICATES_WRITE: tuple[str, ...] = (
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
)

LEGACY_VISIBILITY_PREDICATES: tuple[str, ...] = (
    *LEGACY_SPECIFIC_TO_USER_PREDICATES,
    *LEGACY_SPECIFIC_TO_ORG_PREDICATES,
)

CANONICAL_VISIBILITY_PREDICATES: tuple[str, ...] = (
    CANONICAL_SPECIFIC_TO_USER_PREDICATE,
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
)

VISIBILITY_PREDICATE_ALIAS_TO_CANONICAL: Dict[str, str] = {
    **{
        predicate: CANONICAL_SPECIFIC_TO_USER_PREDICATE
        for predicate in SPECIFIC_TO_USER_PREDICATES
    },
    **{
        predicate: CANONICAL_SPECIFIC_TO_ORG_PREDICATE
        for predicate in SPECIFIC_TO_ORG_PREDICATES_READ
    },
}


def _normalise_concept_ids(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []

    ordered: List[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if not candidate.startswith("#V#"):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def collect_visibility_values(
    relationships: Dict[str, Any] | None,
    predicates: Sequence[str],
) -> List[str]:
    if not isinstance(relationships, dict):
        return []

    gathered: List[str] = []
    seen: set[str] = set()
    for predicate in predicates:
        raw = relationships.get(predicate)
        for candidate in _normalise_concept_ids(raw):
            if candidate in seen:
                continue
            seen.add(candidate)
            gathered.append(candidate)
    return gathered


def _set_visibility_values(
    relationships: Dict[str, Any] | None,
    predicates: Sequence[str],
    values: Iterable[str],
    *,
    clear_predicates: Sequence[str] = (),
) -> Dict[str, Any]:
    updated = dict(relationships) if isinstance(relationships, dict) else {}
    normalised = _normalise_concept_ids(list(values))

    for predicate in clear_predicates:
        if predicate not in predicates:
            updated.pop(predicate, None)

    for predicate in predicates:
        if normalised:
            updated[predicate] = list(normalised)
        else:
            updated.pop(predicate, None)
    return updated


def get_specific_to_user_values(relationships: Dict[str, Any] | None) -> List[str]:
    return collect_visibility_values(relationships, SPECIFIC_TO_USER_PREDICATES)


def get_specific_to_org_values(relationships: Dict[str, Any] | None) -> List[str]:
    return collect_visibility_values(relationships, SPECIFIC_TO_ORG_PREDICATES_READ)


def set_specific_to_user_values(
    relationships: Dict[str, Any] | None,
    values: Iterable[str],
) -> Dict[str, Any]:
    return _set_visibility_values(
        relationships,
        SPECIFIC_TO_USER_PREDICATES_WRITE,
        values,
        clear_predicates=SPECIFIC_TO_USER_PREDICATES,
    )


def set_specific_to_org_values(
    relationships: Dict[str, Any] | None,
    values: Iterable[str],
) -> Dict[str, Any]:
    return _set_visibility_values(
        relationships,
        SPECIFIC_TO_ORG_PREDICATES_WRITE,
        values,
        clear_predicates=SPECIFIC_TO_ORG_PREDICATES_READ,
    )


def visibility_relationship_field(predicate: str) -> str:
    """Return the Mongo relationship field path for a visibility predicate."""

    return f"relationships.{predicate}"
