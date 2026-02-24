"""Visibility predicate helpers.

This module centralises legacy/canonical visibility predicate aliases so write
paths can mirror values consistently and read paths can tolerate historical
storage variants.

Why this exists:
- Legacy records use ``specific_to_user`` / ``specific_to_org`` fields.
- Newer ontology usage expects predicate-concept IDs (for example
  ``#V#specific_to_user`` and ``#V#specific_to_organisation``).
- Without a shared helper, call paths drift and debugging predicate state
  becomes brittle (for example fetches for one variant while data is stored in
  another).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

# Keep read aliases broad for backwards compatibility.
SPECIFIC_TO_USER_PREDICATES: tuple[str, ...] = (
    "specific_to_user",
    "#V#specific_to_user",
)

SPECIFIC_TO_ORG_PREDICATES_READ: tuple[str, ...] = (
    "specific_to_org",
    "specific_to_organisation",
    "#V#specific_to_org",
    "#V#specific_to_organisation",
)

# Writes should favour currently supported canonical/legacy keys and avoid
# introducing new non-canonical variants unless explicitly required.
SPECIFIC_TO_ORG_PREDICATES_WRITE: tuple[str, ...] = (
    "specific_to_org",
    "specific_to_organisation",
    "#V#specific_to_organisation",
)


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
) -> Dict[str, Any]:
    updated = dict(relationships) if isinstance(relationships, dict) else {}
    normalised = _normalise_concept_ids(list(values))

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
    return _set_visibility_values(relationships, SPECIFIC_TO_USER_PREDICATES, values)


def set_specific_to_org_values(
    relationships: Dict[str, Any] | None,
    values: Iterable[str],
) -> Dict[str, Any]:
    return _set_visibility_values(
        relationships,
        SPECIFIC_TO_ORG_PREDICATES_WRITE,
        values,
    )

