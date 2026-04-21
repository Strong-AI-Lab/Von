"""Vontology-backed structural predicate metadata service.

Replaces hardcoded RELATIONSHIP_KINDS, STRUCTURAL_PREDICATE_ALIASES, and
STRUCTURAL_INVERSE_MAP with a Vontology-authoritative source.

JVNAUTOSCI-1957: Migrate structural relationship predicate registry from
hardcoded Python dicts to Vontology-backed metadata.

Authoritative predicates stored on each structural predicate concept in Vontology:
  - #V#structural_relationship_field_name: the MongoDB field/key used for storage
  - #V#structural_inverse_field: the inverse field name or concept ID

New structural predicates are added by adding these text relations in Vontology —
no Python code changes are required.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, FrozenSet, Optional, Tuple

_logger = logging.getLogger(__name__)

# TTL for cached predicate metadata (5 minutes)
_CACHE_TTL_SECONDS: float = 300.0

# Text relation predicates that encode structural metadata in Vontology
_FIELD_NAME_PREDICATE = "#V#structural_relationship_field_name"
_INVERSE_FIELD_PREDICATE = "#V#structural_inverse_field"

# Canonical structural predicate concept IDs to query
_STRUCTURAL_PREDICATE_CONCEPT_IDS: Tuple[str, ...] = (
    "#V#is_a_type_of",
    "#V#has_subtype",
    "#V#is_an_instance_of",
    "#V#has_instance",
    "#V#related_to",
    "#V#authored_by",
    "#V#has_author",
)

# --- Hardcoded fallback defaults (used when Vontology is unavailable) ---

_DEFAULT_RELATIONSHIP_KINDS: Tuple[str, ...] = (
    "is_a_type_of",
    "has_subtype",
    "is_an_instance_of",
    "has_instance",
    "related_to",
    "#V#authored_by",
    "#V#has_author",
)

_DEFAULT_STRUCTURAL_PREDICATE_ALIASES: Dict[str, str] = {
    "#V#is_a_type_of": "is_a_type_of",
    "#V#has_subtype": "has_subtype",
    "#V#is_an_instance_of": "is_an_instance_of",
    "#V#has_instance": "has_instance",
    "#V#related_to": "related_to",
}

_DEFAULT_STRUCTURAL_INVERSE_MAP: Dict[str, str] = {
    "is_a_type_of": "has_subtype",
    "has_subtype": "is_a_type_of",
    "is_an_instance_of": "has_instance",
    "has_instance": "is_an_instance_of",
    "related_to": "related_to",
}

# --- Cache state ---

_cache: Optional[Dict] = None
_cache_loaded_at: float = 0.0


def _is_cache_stale() -> bool:
    return _cache is None or (time.time() - _cache_loaded_at) > _CACHE_TTL_SECONDS


def _build_defaults() -> Dict:
    return {
        "relationship_kinds": _DEFAULT_RELATIONSHIP_KINDS,
        "structural_predicate_aliases": dict(_DEFAULT_STRUCTURAL_PREDICATE_ALIASES),
        "structural_inverse_map": dict(_DEFAULT_STRUCTURAL_INVERSE_MAP),
    }


def _load_from_vontology() -> Dict:
    """Query Vontology for structural predicate metadata.

    Returns a dict with keys: relationship_kinds, structural_predicate_aliases,
    structural_inverse_map.  Falls back to defaults on any error, or when
    Vontology returns no data (e.g. cold-start before migration).
    """
    try:
        from .text_value_service import get_texts_for_concept
    except Exception as exc:
        _logger.warning(
            "[predicate_metadata] text_value_service unavailable, using defaults: %s", exc
        )
        return _build_defaults()

    relationship_kinds: list[str] = []
    structural_predicate_aliases: Dict[str, str] = {}
    structural_inverse_map: Dict[str, str] = {}
    loaded_count = 0

    for concept_id in _STRUCTURAL_PREDICATE_CONCEPT_IDS:
        try:
            field_rows = get_texts_for_concept(
                concept_id, predicate=_FIELD_NAME_PREDICATE, limit=1
            )
            inverse_rows = get_texts_for_concept(
                concept_id, predicate=_INVERSE_FIELD_PREDICATE, limit=1
            )
        except Exception as exc:
            _logger.debug(
                "[predicate_metadata] Failed to query %s: %s", concept_id, exc
            )
            continue

        if not field_rows:
            _logger.debug(
                "[predicate_metadata] No structural_relationship_field_name for %s",
                concept_id,
            )
            continue

        field_name = (field_rows[0].get("text") or "").strip()
        if not field_name:
            continue

        relationship_kinds.append(field_name)
        loaded_count += 1

        # Structural alias: maps #V# concept ID → field name
        # Only needed when concept_id != field_name (e.g. #V#is_a_type_of → "is_a_type_of")
        if concept_id != field_name:
            structural_predicate_aliases[concept_id] = field_name

        # Inverse mapping: field_name → inverse field_name/concept_id
        if inverse_rows:
            inverse_field = (inverse_rows[0].get("text") or "").strip()
            if inverse_field:
                structural_inverse_map[field_name] = inverse_field

    if loaded_count == 0:
        _logger.warning(
            "[predicate_metadata] No structural predicate metadata found in Vontology; "
            "using defaults. Populate #V#structural_relationship_field_name text relations "
            "on structural predicate concepts to enable Vontology authority."
        )
        return _build_defaults()

    if loaded_count < len(_STRUCTURAL_PREDICATE_CONCEPT_IDS):
        _logger.warning(
            "[predicate_metadata] Loaded %d/%d structural predicates from Vontology; "
            "merging with defaults for missing entries.",
            loaded_count,
            len(_STRUCTURAL_PREDICATE_CONCEPT_IDS),
        )
        defaults = _build_defaults()
        for kind in defaults["relationship_kinds"]:
            if kind not in relationship_kinds:
                relationship_kinds.append(kind)
        for k, v in defaults["structural_predicate_aliases"].items():
            if k not in structural_predicate_aliases:
                structural_predicate_aliases[k] = v
        for k, v in defaults["structural_inverse_map"].items():
            if k not in structural_inverse_map:
                structural_inverse_map[k] = v

    _logger.debug(
        "[predicate_metadata] Loaded %d structural predicates from Vontology.",
        loaded_count,
    )
    return {
        "relationship_kinds": tuple(relationship_kinds),
        "structural_predicate_aliases": structural_predicate_aliases,
        "structural_inverse_map": structural_inverse_map,
    }


def _ensure_cache() -> Dict:
    global _cache, _cache_loaded_at
    if not _is_cache_stale():
        assert _cache is not None
        return _cache
    loaded = _load_from_vontology()
    _cache = loaded
    _cache_loaded_at = time.time()
    return loaded


def get_relationship_kinds() -> Tuple[str, ...]:
    """Return the set of structural relationship field keys (Vontology-backed).

    These are the field names used as keys in ``relationships`` subdocuments in
    MongoDB concept documents.  The returned tuple includes both plain field
    names (e.g. "is_a_type_of") and predicate concept IDs used directly as
    keys (e.g. "#V#authored_by").
    """
    return _ensure_cache()["relationship_kinds"]


def get_relationship_kinds_set() -> FrozenSet[str]:
    """Return relationship kinds as a frozen set for O(1) membership tests."""
    return frozenset(_ensure_cache()["relationship_kinds"])


def get_structural_predicate_aliases() -> Dict[str, str]:
    """Return mapping of #V# predicate concept IDs to structural field names.

    e.g. {"#V#is_a_type_of": "is_a_type_of", ...}
    Only includes predicates where the concept ID differs from the field name.
    """
    return dict(_ensure_cache()["structural_predicate_aliases"])


def get_structural_inverse_map() -> Dict[str, str]:
    """Return mapping of structural field names to their inverse field names.

    e.g. {"is_a_type_of": "has_subtype", "related_to": "related_to", ...}
    """
    return dict(_ensure_cache()["structural_inverse_map"])


def invalidate_cache() -> None:
    """Invalidate the predicate metadata cache.

    Call after Vontology mutations that affect structural predicate configuration,
    so the next access re-queries the authoritative source.
    """
    global _cache, _cache_loaded_at
    _cache = None
    _cache_loaded_at = 0.0
