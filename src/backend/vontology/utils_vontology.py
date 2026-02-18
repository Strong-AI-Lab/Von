# --- Standard Library Imports ---
import os
import threading
from pathlib import Path
import hashlib
import json
import re
import traceback
import hashlib
from datetime import datetime, timezone
from ..utils.time_utils import utc_now, utc_iso_now
import logging
import inspect
from typing import Optional, List, Dict, Any, Union
from typing import Any, Dict, List, Optional, Set, Iterable, Union, Tuple
from collections import defaultdict

# Critical Note: Concept Files XX_Type.md are in the XX directory that contains directories containing their children
# e.g. Thing contains Thing_Type.md and UnderspecifiedLocation/  and UnderspecifiedLocation contains UnderspecifiedLocation_Type.md
# and directories for its (possibly multiple) children.
# --- Logging Configuration ---

# Initialize logger for this module
logger = logging.getLogger(__name__)

# --- Salient predicate scope constants ---
SALIENT_SCOPE_FIELD = "salient_predicate_scopes"
SALIENT_SCOPE_INSTANCE_KEY = "instance_level"
SALIENT_SCOPE_TYPE_KEY = "type_level"
SALIENT_SCOPE_UNCLASSIFIED_KEY = "unclassified"


def _ordered_unique_identifiers(values: Iterable[Any] | Any) -> List[str]:
    """Return a list of unique non-empty strings preserving original order."""
    result: List[str] = []
    seen: set[str] = set()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return result
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def normalise_salient_scope_map(scopes: Any) -> Dict[str, List[str]]:
    """Normalise raw scope input into canonical lists keyed by scope category."""
    if not isinstance(scopes, dict):
        scopes = {}

    def _fetch(*keys: str) -> List[str]:
        for key in keys:
            if key in scopes:
                return _ordered_unique_identifiers(scopes.get(key))
        return []

    instance_vals = _fetch(SALIENT_SCOPE_INSTANCE_KEY, "instance", "instances")
    type_vals = _fetch(SALIENT_SCOPE_TYPE_KEY, "type", "types")
    unclassified_vals = _fetch(SALIENT_SCOPE_UNCLASSIFIED_KEY, "unclassified", "other")

    return {
        SALIENT_SCOPE_INSTANCE_KEY: instance_vals,
        SALIENT_SCOPE_TYPE_KEY: type_vals,
        SALIENT_SCOPE_UNCLASSIFIED_KEY: unclassified_vals,
    }


def build_salient_instance_compat_list(scopes_map: Dict[str, List[str]]) -> List[str]:
    """Create compatibility list for legacy instance-level salient predicates."""
    instance_vals = scopes_map.get(SALIENT_SCOPE_INSTANCE_KEY, [])
    unclassified_vals = scopes_map.get(SALIENT_SCOPE_UNCLASSIFIED_KEY, [])
    return _ordered_unique_identifiers(list(instance_vals) + list(unclassified_vals))


def validate_concept_name_for_id(name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that a concept name can be safely converted to a concept ID.

    Constraints:
    - Name must be a non-empty string
    - Spaces and punctuation are permitted; they will be normalised away when
      generating the concept_id

    Args:
        name: Raw concept name from user

    Returns:
        Tuple of (is_valid, error_message)
        - (True, None) if valid
        - (False, error_message) if invalid
    """
    if not name or not isinstance(name, str):
        return False, "Concept name must be a non-empty string"

    return True, None


def extract_salient_scope_lists(
    document: Dict[str, Any] | None,
) -> Dict[str, List[str]]:
    """Extract instance/type salient predicate identifiers with legacy fallbacks."""
    if not isinstance(document, dict):
        document = {}

    scopes_raw = document.get(SALIENT_SCOPE_FIELD)
    if isinstance(scopes_raw, dict):
        normalised = normalise_salient_scope_map(scopes_raw)
        instance_values = build_salient_instance_compat_list(normalised)
        type_values = normalised.get(SALIENT_SCOPE_TYPE_KEY, [])
        unclassified_values = normalised.get(SALIENT_SCOPE_UNCLASSIFIED_KEY, [])
        return {
            "instance": instance_values,
            "type": type_values,
            "unclassified": unclassified_values,
        }

    # Legacy fallbacks
    direct_field = document.get("salient_binary_predicates_for_type")
    if isinstance(direct_field, dict):
        direct_field = direct_field.get("salient_binary_predicates_for_type")
    direct_values = _ordered_unique_identifiers(direct_field)

    relationships = document.get("relationships") or {}
    if isinstance(relationships, dict):
        rel_values = _ordered_unique_identifiers(
            relationships.get("#V#salient_binary_predicate_for_type")
        )
        for value in rel_values:
            if value not in direct_values:
                direct_values.append(value)

    return {
        "instance": direct_values,
        "type": [],
        "unclassified": [],
    }


# --- Third-Party Imports ---
import requests
import bson.json_util as json_util
from pymongo.collection import Collection
from pymongo.errors import PyMongoError, OperationFailure  # Added OperationFailure
from bson.objectid import ObjectId
from bson import errors as bson_errors  # ObjectId import corrected
from bson.errors import InvalidId  # Ensure InvalidId is imported

# --- Optional markdown import (avoid hard dependency / lint error) ---
import importlib

_markdown_mod = None
try:  # Attempt dynamic discovery first to suppress static unresolved-import warnings
    if importlib.util.find_spec("markdown") is not None:  # type: ignore[attr-defined]
        _markdown_mod = importlib.import_module("markdown")  # type: ignore[assignment]
except Exception:  # pragma: no cover - defensive
    _markdown_mod = None

if _markdown_mod is not None:
    markdown = _markdown_mod  # type: ignore
else:

    class _MarkdownFallback:  # noqa: D401 - simple passthrough fallback
        """Fallback object providing markdown(markdown_text)->html passthrough."""

        @staticmethod
        def markdown(text: str):  # noqa: D401
            return text

    markdown = _MarkdownFallback()  # type: ignore

# --- MongoDB Client Import ---
try:
    from ..db.mongo_client import (
        get_db,
        get_concepts_collection,
        CONCEPTS_COLLECTION_NAME,
    )
except ImportError:
    # Fallback for direct script usage or testing
    from ..db.mongo_client import get_db

from ..db.repositories.concepts_repository import (
    ConceptsRepository,
)  # Added repository import

from ..db.repositories.text_value_repository import TextRelationsRepository

# --- Cached preferred language to avoid repeated DB calls ---
_cached_preferred_language = None
_cache_timestamp = 0
_cache_ttl = 300  # Cache for 5 minutes (300 seconds)

# --- Deprecation Metrics ---
_DEPRECATED_CALL_COUNTER = {
    "update_vontology_node_description": 0,
    "update_vontology_node_notes": 0,  # reserved for future parity
}


def _persist_deprecation_metric(key: str):
    """Increment a persistent counter in system_metrics collection.
    Document schema: { _id: 'deprecation_counters', counters: { key: int, ... }, updated_at: ISO8601 }
    """
    try:
        db = get_db()
        if db is None:
            return
        coll = db.get_collection("system_metrics")
        now = utc_iso_now()
        coll.update_one(
            {"_id": "deprecation_counters"},
            {"$inc": {f"counters.{key}": 1}, "$set": {"updated_at": now}},
            upsert=True,
        )
    except Exception:  # pragma: no cover
        pass


# --- Performance Metrics ---
def record_tree_build_performance(build_seconds: float):
    """Persist performance statistics for ontology tree builds.

    Document schema in system_metrics collection (upsert):
      { _id: 'performance_counters',
        vontology_tree_build: {
           count: int,
           total_build_sec: float,
           max_build_sec: float,
           last_build_sec: float,
           last_built_at: ISO8601
        },
        updated_at: ISO8601 }
    Avg is computed on read (total / count).
    """
    try:
        if build_seconds is None:
            return
        db = get_db()
        if db is None:
            return
        coll = db.get_collection("system_metrics")
        now_iso = utc_iso_now()
        coll.update_one(
            {"_id": "performance_counters"},
            {
                "$inc": {
                    "vontology_tree_build.count": 1,
                    "vontology_tree_build.total_build_sec": float(build_seconds),
                },
                "$max": {"vontology_tree_build.max_build_sec": float(build_seconds)},
                "$set": {
                    "vontology_tree_build.last_build_sec": float(build_seconds),
                    "vontology_tree_build.last_built_at": now_iso,
                    "updated_at": now_iso,
                },
            },
            upsert=True,
        )
    except Exception:
        pass


def record_name_normalization(kind: str = "generic"):
    """Persist a counter for UI name normalization events.

    Document schema in system_metrics collection (upsert):
      { _id: 'ui_counters',
        name_normalizations: { total: int, by_kind: { kind: int, ... } },
        updated_at: ISO8601 }
    """
    try:
        db = get_db()
        if db is None:
            return
        coll = db.get_collection("system_metrics")
        now_iso = utc_iso_now()
        coll.update_one(
            {"_id": "ui_counters"},
            {
                "$inc": {
                    "name_normalizations.total": 1,
                    f"name_normalizations.by_kind.{kind}": 1,
                },
                "$set": {"updated_at": now_iso},
            },
            upsert=True,
        )
    except Exception:
        pass


def record_frontend_tree_load(
    load_ms: int, node_count: int | None = None, cache_hit: bool | None = None
):
    """Persist metrics for frontend Vontology tree loading performance.

    Document schema (_id: 'frontend_tree_load'):
      {
        _id: 'frontend_tree_load',
        totals: { count, total_ms, max_ms, last_ms, last_at },
        per_pid: { <pid>: { count, total_ms, max_ms, last_ms, last_at } },
        cache: { hits, misses, last_hit: bool },
        nodes: { last_count, max_count },
        updated_at: ISO8601
      }
    """
    try:
        db = get_db()
        if db is None:
            return
        coll = db.get_collection("system_metrics")
        now_iso = utc_iso_now()
        import os

        pid = str(os.getpid())
        load_ms = int(load_ms) if load_ms is not None else 0
        inc_ops = {
            "totals.count": 1,
            "totals.total_ms": load_ms,
            f"per_pid.{pid}.count": 1,
            f"per_pid.{pid}.total_ms": load_ms,
        }
        if cache_hit is not None:
            inc_ops[f'cache.{"hits" if cache_hit else "misses"}'] = 1
        set_ops = {
            "updated_at": now_iso,
            "totals.last_ms": load_ms,
            "totals.last_at": now_iso,
            f"per_pid.{pid}.last_ms": load_ms,
            f"per_pid.{pid}.last_at": now_iso,
        }
        if cache_hit is not None:
            set_ops["cache.last_hit"] = bool(cache_hit)
        if node_count is not None:
            set_ops["nodes.last_count"] = int(node_count)
        coll.update_one(
            {"_id": "frontend_tree_load"},
            {
                "$inc": inc_ops,
                "$set": set_ops,
                "$max": {
                    "totals.max_ms": load_ms,
                    f"per_pid.{pid}.max_ms": load_ms,
                    "nodes.max_count": int(node_count) if node_count is not None else 0,
                },
            },
            upsert=True,
        )
    except Exception:
        pass


# --- Vontology Base Directory Definition ---
# Version tracking for debugging
VONTOLOGY_UTILS_VERSION = "2024-08-23-v4-with-root-debugging"

# Neutral placeholder concept IDs for examples (avoid PII / real users)
EXAMPLE_PERSON_CONCEPT_ID = "#V#example_person"
EXAMPLE_USER_CONCEPT_ID = "#V#default_user"

try:
    # Assumes utils_vontology.py is in src/backend/vontology
    _current_dir = Path(__file__).resolve().parent
    VONTOLOGY_BASE_DIR = _current_dir.parent.parent / "knowledge" / "vontology"
except NameError:
    # Fallback if __file__ is not defined (e.g., interactive session)
    logger.warning(
        "Warning: __file__ not defined, attempting relative path for Vontology base."
    )  # Changed print to logger.warning
    VONTOLOGY_BASE_DIR = Path("./knowledge/vontology").resolve()  # Adjust as needed

VONTOLOGY_NODES_COLLECTION_NAME = "concepts"  # DEPRECATED: Use CONCEPTS_COLLECTION_NAME
CONCEPTS_COLLECTION_NAME = "concepts"
# --- Knowkat API URL Definition ---
KNOWKAT_API_URL = "http://127.0.0.1:11686"  # Default from docs

# --- Relationship helpers (shared) ---
VONTOLOGY_THING_IDS = {
    "#V#thing",
    "#V#Thing",
    "http://www.w3.org/2002/07/owl#Thing",
    "Thing",
}
THING_PRIMARY_ID = "#V#thing"


def is_nonempty_relationship(value) -> bool:
    """Return True if a relationship field contains any non-empty string(s).

    Accepts list[str] or str; ignores None/empty/whitespace-only values.
    """
    if isinstance(value, list):
        return any(isinstance(x, str) and x.strip() for x in value)
    if isinstance(value, str):
        return bool(value.strip())
    return False


def is_pure_instance(node: dict) -> bool:
    """A node is a pure instance if it has instance-of but no type-of relationships.

    This preserves types (which may have both is_a_type_of and is_an_instance_of e.g. a second-order or higher-order type).
    """
    rel = (node or {}).get("relationships", {})
    has_instance_of = is_nonempty_relationship(rel.get("is_an_instance_of"))
    has_type_of = is_nonempty_relationship(rel.get("is_a_type_of"))
    return has_instance_of and not has_type_of


def is_thing_concept(node: dict) -> bool:
    """Return True if the node represents the ontology root Thing.

    Checks common identifiers and title/name for an exact 'Thing'.
    """
    if not isinstance(node, dict):
        return False
    # Accept either 'concept_id' (full document) or fallback 'id' (tree node structure)
    cid = node.get("concept_id") or node.get("id")
    if isinstance(cid, str) and cid in VONTOLOGY_THING_IDS:
        return True
    title = node.get("name")
    if isinstance(title, str) and title == "Thing":
        return True
    # Backward-compat: accept legacy metadata.title strictly equal to 'Thing'
    meta_title = (node.get("metadata") or {}).get("title")
    if isinstance(meta_title, str) and meta_title == "Thing":
        logger.warning(
            "DEPRECATION: Thing recognized via legacy metadata.title == 'Thing'. "
            "Please migrate to names[] or concept_id-based detection."
        )
        return True
    return False


def _get_cached_preferred_language() -> str:
    """Get preferred language with caching to avoid repeated DB calls."""
    global _cached_preferred_language, _cache_timestamp
    import time

    current_time = time.time()

    # Check if cache is still valid
    if (
        _cached_preferred_language is not None
        and (current_time - _cache_timestamp) < _cache_ttl
    ):
        return _cached_preferred_language

    # Cache is expired or empty, fetch new value
    try:
        from ..services.settings_service import (
            get_preferred_language,
        )  # Local import to avoid circular dependency

        _cached_preferred_language = get_preferred_language()
        _cache_timestamp = current_time
        return _cached_preferred_language
    except Exception:
        # If fetching fails, return default but don't cache it
        return "en-NZ"


def clear_preferred_language_cache():
    """Clear the cached preferred language value. Useful for testing or when settings change."""
    global _cached_preferred_language, _cache_timestamp
    _cached_preferred_language = None
    _cache_timestamp = 0


def get_concept_display_name_with_names_fallback(concept: dict) -> str:
    """Resolve a human-friendly display name for a concept.

    Priority order (post-migration):
    1. First names array entry with type "NL" (Natural Language), matching user's preferred language
    2. First names array entry with type "ABBR" (Abbreviation), matching user's preferred language
    3. First names array entry with type "NL", any language
    4. First names array entry with type "ABBR", any language
    5. Top-level name field (legacy)
    6. Derive from concept_id (e.g. "#V#example_person" -> "Example Person")
    7. "Unnamed Concept"

    Note: This supports both natural language names and abbreviations/acronyms for better
    search and display functionality. metadata.title is no longer considered.
    """
    if not isinstance(concept, dict):
        return "Unnamed Concept"

    # Try names array first - look for type "NL"
    names = concept.get("names")
    if isinstance(names, list):
        # Attempt to honor preferred language first using cached value
        preferred_lang = _get_cached_preferred_language()

        if preferred_lang:
            # First priority: NL names in preferred language
            for name_entry in names:
                if (
                    isinstance(name_entry, dict)
                    and name_entry.get("type") == "NL"
                    and isinstance(name_entry.get("language"), str)
                    and name_entry.get("language") == preferred_lang
                ):
                    name_value = name_entry.get("name")
                    if isinstance(name_value, str) and name_value.strip():
                        return name_value.strip()

            # Second priority: ABBR names in preferred language
            for name_entry in names:
                if (
                    isinstance(name_entry, dict)
                    and name_entry.get("type") == "ABBR"
                    and isinstance(name_entry.get("language"), str)
                    and name_entry.get("language") == preferred_lang
                ):
                    name_value = name_entry.get("name")
                    if isinstance(name_value, str) and name_value.strip():
                        return name_value.strip()

        # Fallback: first NL name regardless of language
        for name_entry in names:
            if isinstance(name_entry, dict) and name_entry.get("type") == "NL":
                name_value = name_entry.get("name")
                if isinstance(name_value, str) and name_value.strip():
                    return name_value.strip()

        # Fallback: first ABBR name regardless of language
        for name_entry in names:
            if isinstance(name_entry, dict) and name_entry.get("type") == "ABBR":
                name_value = name_entry.get("name")
                if isinstance(name_value, str) and name_value.strip():
                    return name_value.strip()

    # Try top-level name field (legacy)
    name = concept.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    # Derive from concept_id
    concept_id = concept.get("concept_id")
    if isinstance(concept_id, str) and concept_id.startswith("#V#"):
        # Convert "#V#example_person" -> "Example Person"
        name_part = concept_id[3:]  # Remove "#V#"
        return name_part.replace("_", " ").title()

    # Final fallback
    return "Unnamed Concept"


def is_thing_id(concept_id: str | None) -> bool:
    """Return True if the given concept_id string is any recognized Thing identifier."""
    return isinstance(concept_id, str) and concept_id in VONTOLOGY_THING_IDS


def is_type(node: dict) -> bool:
    """Return True if a node is a type.

    A node is a type if:
    - It has any non-empty relationships.is_a_type_of value; or
    - It is the ontology root Thing (by known IDs or title).
    """
    if not isinstance(node, dict):
        return False
    rel = node.get("relationships", {}) or {}
    if is_nonempty_relationship(rel.get("is_a_type_of")):
        return True
    # Compatibility: treat predicate concept aliases as structural edges.
    if is_nonempty_relationship(rel.get("#V#is_a_type_of")):
        return True
    return is_thing_concept(node)


def is_predicate(node: dict) -> bool:
    """Return True if a node is a predicate.

    A node is a predicate if it's an instance of predicate-related types:
    - #V#predicate
    - #V#binary_predicate
    - #V#unary_predicate
    - #V#n_ary_predicate
    - #V#ternary_predicate
    - #V#relation
    - #V#property
    - Or any type containing "predicate" in its name (case-insensitive)
    """
    if not isinstance(node, dict):
        return False

    rel = node.get("relationships", {}) or {}
    instance_of = rel.get("is_an_instance_of")

    if not instance_of:
        return False

    # Normalize to list
    types = instance_of if isinstance(instance_of, list) else [instance_of]

    # Known predicate types
    predicate_types = {
        "#V#predicate",
        "#V#binary_predicate",
        "#V#unary_predicate",
        "#V#n_ary_predicate",
        "#V#ternary_predicate",
        "#V#relation",
        "#V#property",
    }

    for type_id in types:
        if not isinstance(type_id, str):
            continue

        # Direct match with known predicate types
        if type_id in predicate_types or any(
            type_id.startswith(pt) for pt in predicate_types
        ):
            return True

        # Check if type name contains "predicate" (case-insensitive)
        if "predicate" in type_id.lower():
            return True

    return False


def compute_most_salient_type_dynamic(concept: dict) -> Optional[str]:
    """
    Dynamically compute the most salient type for a concept based on usage patterns.

    This function computes the most salient parent type for a concept by:
    1. Getting all parent types from is_a_type_of
    2. For each parent, counting how many individuals are instances of that type
    3. Selecting the parent with the lowest usage count (most specific)

    Args:
        concept: The concept document to compute most salient type for

    Returns:
        The concept_id of the most salient parent type, or None if no parents or computation fails
    """
    try:
        concept_id = concept.get("concept_id")
        if not concept_id:
            return None

        # Get parent types
        parent_types = concept.get("relationships", {}).get("is_a_type_of", [])
        if isinstance(parent_types, str):
            parent_types = [parent_types]

        if not parent_types:
            return None

        # If only one parent, return it
        if len(parent_types) == 1:
            return parent_types[0]

        # Count usage for each parent type
        coll = ConceptsRepository.collection()
        if coll is None:
            return parent_types[0]  # Fallback to first parent

        parent_usage_counts = {}

        for parent_type in parent_types:
            try:
                # Count individuals that are instances of this type
                usage_count = coll.count_documents(
                    {"relationships.is_an_instance_of": parent_type}
                )
                parent_usage_counts[parent_type] = usage_count
                logger.debug(
                    f"Parent {parent_type} has {usage_count} individual instances"
                )
            except Exception as e:
                logger.warning(f"Error counting usage for parent {parent_type}: {e}")
                parent_usage_counts[parent_type] = float("inf")  # Penalize errors

        # Select parent with lowest usage count (most specific)
        if parent_usage_counts:
            most_salient = min(
                parent_usage_counts.keys(), key=lambda p: parent_usage_counts[p]
            )
            logger.debug(
                f"Most salient type for {concept_id}: {most_salient} (usage: {parent_usage_counts[most_salient]})"
            )
            return most_salient

        return parent_types[0]  # Fallback to first parent

    except Exception as e:
        logger.error(
            f"Error computing most salient type for {concept.get('concept_id', 'unknown')}: {e}"
        )
        # Fallback to first parent if available
        parent_types = concept.get("relationships", {}).get("is_a_type_of", [])
        if isinstance(parent_types, str):
            return parent_types
        elif isinstance(parent_types, list) and parent_types:
            return parent_types[0]
        return None


def get_most_salient_type(concept: dict, use_cache: bool = True) -> Optional[str]:
    """
    Get the most salient type for a concept, using cached value if available or computing dynamically.

    Args:
        concept: The concept document
        use_cache: Whether to use cached most_salient_type value if available

    Returns:
        The concept_id of the most salient parent type, or None if no parents
    """
    if use_cache:
        # Try cached value first
        cached_salient = concept.get("relationships", {}).get("most_salient_type")
        if cached_salient:
            return cached_salient

    # Compute dynamically
    return compute_most_salient_type_dynamic(concept)


def to_pascal_case(text_str: str) -> str:
    """
    Converts a string to PascalCase.
    Handles spaces, underscores, hyphens as separators.
    Attempts to preserve acronyms if they are fully uppercase.
    Removes other special characters like {} before processing.
    e.g., "scholarly contribution" -> "ScholarlyContribution"
          "Scholarly_Article" -> "ScholarlyArticle"
          "LLM prompt" -> "LLMPrompt"
          "my html page" -> "MyHtmlPage"
          "the union of { spatial things" -> "TheUnionOfSpatialThings"
          "air-breathing vertebrate" -> "AirBreathingVertebrate"
    """
    if not text_str:
        return ""

    # Pre-processing: Remove problematic characters like {} and @
    # Replace them with nothing, as they are not separators.
    # Hyphens and underscores will be treated as separators later.
    interim_text = text_str.replace("{", "").replace("}", "").replace("@", "")

    # Insert a space before uppercase letters that follow a lowercase letter or digit,
    # or before an uppercase letter that is followed by a lowercase letter (to split acronyms from words like "LLMPrompt").
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", interim_text)
    s2 = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", s1)
    # Replace underscores and hyphens with spaces
    s3 = s2.replace("_", " ").replace("-", " ")

    words = s3.split(" ")
    pascal_words = []
    for word in words:
        if not word:
            continue
        if word.isupper():  # Preserve acronyms
            pascal_words.append(word)
        else:
            # Ensure word is not empty before accessing word[0] or word[1:]
            if word:  # Check if word is not empty
                pascal_words.append(word[0].upper() + word[1:].lower())

    return "".join(pascal_words)


def generate_concept_id_from_name(pascal_case_name_str: str) -> str | None:
    """
    Generates a concept_id from a PascalCase name string.
    The ID itself will be in #V#snake_case format.
    e.g., "ScholarlyContribution" -> "#V#scholarly_contribution"
          "MyHTMLPage" -> "#V#my_html_page"
          "LLM" -> "#V#llm"
          "Sorbonne Université" -> "#V#sorbonne_universite" (accents normalized)
    """
    import unicodedata

    if not pascal_case_name_str:
        logger.error(
            "generate_concept_id_from_name called with empty input! Returning None."
        )
        return None

    # JVNAUTOSCI-944: Transliterate accented characters to ASCII before processing
    # NFKD decomposition splits characters like "é" into "e" + combining accent
    normalized_input = unicodedata.normalize("NFKD", pascal_case_name_str)
    # Filter out combining characters (category starts with 'M' for Mark)
    ascii_name = "".join(
        c for c in normalized_input if not unicodedata.category(c).startswith("M")
    )

    # Convert PascalCase to snake_case.
    # Insert underscore before uppercase letters, except at the start.
    # Handles sequences of uppercase letters (acronyms) correctly.
    s1 = re.sub(
        r"([A-Z]+)([A-Z][a-z])", r"\1_\2", ascii_name
    )  # Corrected: r'\\1_\\2' -> r'\1_\2'
    s2 = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s1).lower()  # Corrected regex string

    # Normalize: remove any characters that are not lowercase letters, numbers, or underscore
    normalized_name = re.sub(r"[^a-z0-9_]+", "", s2).strip("_")

    if not normalized_name:  # Handle cases where name becomes empty after normalization
        fallback_name = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_name.lower()).strip("_")
        if not fallback_name:
            logger.error(
                f"generate_concept_id_from_name: Could not generate fallback for '{pascal_case_name_str}'. Returning hash-based ID."
            )
            return f"#V#{hashlib.md5(pascal_case_name_str.encode()).hexdigest()[:8]}"  # very basic fallback
        logger.warning(
            f"generate_concept_id_from_name: Used fallback for '{pascal_case_name_str}' -> '{fallback_name}'"
        )
        return f"#V#{fallback_name}"

    return f"#V#{normalized_name}"


def canonicalize_label(label):
    """
    Convert a label to CamelCase and remove special characters (parentheses, punctuation, etc).
    E.g., 'computer scientist' -> 'ComputerScientist', 'worker (skilled)' -> 'WorkerSkilled'
    """
    # Remove anything that's not a word character or space
    label = re.sub(r"[^\\w\\s]", "", label)  # Corrected regex string
    # Split on whitespace, capitalize each part, join
    parts = label.split()
    return "".join(word.capitalize() for word in parts)


def parse_md_content(md_content, ignore_line_prefix="|"):  # Added ignore_line_prefix
    """
    Parses markdown content to extract Vontology details.
    Lines starting with `ignore_line_prefix` will be skipped.
    Returns a dictionary with extracted details, including 'subconcept_of_names' as a list.
    """
    details = {
        "name": "",
        "concept_id": None,  # Explicit concept_id from _Type.md
        "source_concept": "",
        "subconcept_of_names": [],  # List of parent names or IDs
        "description": "",
    }
    lines = md_content.splitlines()
    for i, line in enumerate(lines):
        if ignore_line_prefix and line.startswith(
            ignore_line_prefix
        ):  # Added check for prefix
            continue  # Skip this line

        line_lower = line.lower()
        if line.startswith("# "):  # Main name of the concept
            details["name"] = line[2:].strip()
        elif line_lower.startswith(
            "**conceptid**:"
        ):  # Corrected: ensure lowercase 'conceptid'
            raw_id = line.split(":", 1)[1].strip()
            if raw_id and raw_id.lower() != "none":
                details["concept_id"] = raw_id
            else:
                logger.debug(
                    f"Explicit 'None' or empty concept_id found for potential name: {details.get('name', 'Unknown (name not yet parsed)')}"
                )
                details["concept_id"] = None  # Ensure it's None if explicitly "None"
        elif line_lower.startswith("**source concept**:"):
            details["source_concept"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("**subconcept of**:"):
            parent_refs_str = line.split(":", 1)[1].strip()
            if parent_refs_str and parent_refs_str.lower() not in [
                "none (root)",
                "none",
                "",
            ]:
                details["subconcept_of_names"] = [
                    name.strip() for name in parent_refs_str.split(",") if name.strip()
                ]
            else:
                details["subconcept_of_names"] = []  # Explicitly no parents or root
        elif line_lower.startswith("**description**:"):
            desc_lines = [line.split(":", 1)[1].strip()]
            for j in range(i + 1, len(lines)):
                # Stop if another known field is encountered
                if (
                    lines[j]
                    .lower()
                    .startswith(
                        ("**conceptid**:", "**source concept**:", "**subconcept of**:")
                    )
                ):
                    break
                desc_lines.append(lines[j].strip())
            details["description"] = "\\n".join(desc_lines).strip()
            break  # Description is typically the last major field

    # Log a warning if an explicit concept_id was not found and name is present
    if not details["concept_id"] and details["name"]:
        logger.warning(
            f"No explicit 'ConceptID:' found in markdown for concept named '{details['name']}'. An ID will be generated if possible during scan_filesystem_to_mongodb."
        )
    elif not details["name"] and details["concept_id"]:
        logger.warning(
            f"Explicit 'ConceptID: {details['concept_id']}' found, but no H1 title ('# Name') found in markdown."
        )

    # If name is not set from H1, and concept_id is available and looks like a #V# id, try to infer name from it.
    # This is a fallback, ideally name is always present in H1.
    concept_id = details.get("concept_id")
    if not details["name"] and concept_id is not None and concept_id.startswith("#V#"):
        inferred_name_from_id = concept_id[3:]  # Strip #V#
        # Attempt to make it more readable: replace underscores with spaces, capitalize words
        # This inferred name will be PascalCased later in scan_filesystem_to_mongodb if used.
        details["name"] = " ".join(
            word.capitalize() for word in inferred_name_from_id.split("_")
        )

    return details


def get_concept_details_from_db(concept_name_or_id=None, collection_name="concepts"):
    """
    Fetches concept details from MongoDB.
    If concept_name_or_id is provided, fetches a specific concept by its name or concept_id.
    Otherwise, fetches all concepts.
    Returns a list of concept detail dictionaries.
    Each dict contains: name, id (concept_id), parent_ids (list of parent concept_ids or None), path (document path).
    """
    # Use repository for concepts collection; fallback to direct DB only for non-concepts
    repo = ConceptsRepository if collection_name == CONCEPTS_COLLECTION_NAME else None

    if concept_name_or_id:
        # Try matching by concept_id first, then legacy top-level name, then names[].name
        query = {
            "$or": [
                {"concept_id": concept_name_or_id},
                {"name": concept_name_or_id},
                {"names.name": concept_name_or_id},
            ]
        }
        # Ensure 'path' is included in the projection
        if repo is not None:
            doc = repo.find_one(
                query,
                {
                    "name": 1,
                    "names": 1,
                    "concept_id": 1,
                    "relationships.is_a_type_of": 1,
                    "path": 1,
                },
            )
        else:
            db = get_db()
            if db is None:
                logger.error(
                    "Database not available for collection '%s'", collection_name
                )
                return []
            coll = db[collection_name]
            doc = coll.find_one(
                query,
                {
                    "name": 1,
                    "names": 1,
                    "concept_id": 1,
                    "relationships.is_a_type_of": 1,
                    "path": 1,
                },
            )
        if doc:
            raw_subconcept_of = doc.get("relationships", {}).get("is_a_type_of")
            processed_parent_ids = None

            if isinstance(raw_subconcept_of, list):
                valid_parents = [
                    p_id
                    for p_id in raw_subconcept_of
                    if isinstance(p_id, str)
                    and p_id.strip()
                    and p_id.strip().lower() not in ["none (root)", "none"]
                ]
                if valid_parents:
                    processed_parent_ids = valid_parents
            elif isinstance(raw_subconcept_of, str):
                if (
                    raw_subconcept_of.strip()
                    and raw_subconcept_of.strip().lower()
                    not in ["", "none (root)", "none"]
                ):
                    processed_parent_ids = [raw_subconcept_of.strip()]

            resolved_name = get_concept_display_name_with_names_fallback(doc)
            return [
                {
                    "name": resolved_name,
                    "id": doc.get("concept_id"),  # Primary identifier
                    "parent_ids": processed_parent_ids,  # List of parent concept_ids or None
                    "path": doc.get("path"),  # Include the path
                }
            ]
        return []

    # Fetch all concepts if no specific one is requested
    # Ensure 'path' is included in the projection
    if repo is not None:
        all_docs_cursor = repo.find(
            {},
            {
                "name": 1,
                "names": 1,
                "concept_id": 1,
                "relationships.is_a_type_of": 1,
                "path": 1,
                "_id": 1,
            },
        )
    else:
        db = get_db()
        if db is None:
            logger.error("Database not available for collection '%s'", collection_name)
            return []
        coll = db[collection_name]
        all_docs_cursor = coll.find(
            {},
            {
                "name": 1,
                "names": 1,
                "concept_id": 1,
                "relationships.is_a_type_of": 1,
                "path": 1,
                "_id": 1,
            },
        )
    concepts_list = []
    for doc in all_docs_cursor:
        concept_id = doc.get("concept_id")
        if not concept_id:  # Skip documents that are missing a concept_id
            logger.warning(
                f"[get_concept_details_from_db] Warning: Document with _id {doc.get('_id')} is missing 'concept_id'. Skipping."
            )
            continue

        raw_subconcept_of = doc.get("relationships", {}).get("is_a_type_of")
        processed_parent_ids = None

        if isinstance(raw_subconcept_of, list):
            valid_parents = [
                p_id
                for p_id in raw_subconcept_of
                if isinstance(p_id, str)
                and p_id.strip()
                and p_id.strip().lower() not in ["none (root)", "none"]
            ]
            if valid_parents:
                processed_parent_ids = valid_parents
        elif isinstance(raw_subconcept_of, str):
            if raw_subconcept_of.strip() and raw_subconcept_of.strip().lower() not in [
                "",
                "none (root)",
                "none",
            ]:
                processed_parent_ids = [raw_subconcept_of.strip()]

        concepts_list.append(
            {
                "name": get_concept_display_name_with_names_fallback(doc),
                "id": concept_id,
                "parent_ids": processed_parent_ids,  # List of parent concept_ids or None
                "path": doc.get("path"),  # Include the path
                "mongo_id": str(doc.get("_id")),  # Add the MongoDB _id as a string
            }
        )
    return concepts_list


def update_vontology_node_description(identifier: str, description: str) -> dict:
    """DEPRECATED: Use concept_service.update_concept_description instead.

    This wrapper now resolves the provided identifier (Mongo _id or concept_id)
    to a concept_id and delegates to the canonical dual-write implementation
    in concept_service. It preserves the historical return structure:
      {"success": True} on success or {"success": False, "error": str}.

    Planned removal: once all callers use the concept_service route directly,
    this function will be deleted (see JVNAUTOSCI-532).
    """
    if not identifier:
        return {"success": False, "error": "Identifier must be provided."}

    try:
        # Resolve to concept_id
        resolved_concept_id = None
        if ObjectId.is_valid(identifier):
            try:
                doc = ConceptsRepository.find_one(
                    {"_id": ObjectId(identifier)}, {"concept_id": 1}
                )
                if doc and doc.get("concept_id"):
                    resolved_concept_id = doc["concept_id"]
                else:
                    return {
                        "success": False,
                        "error": f"Node with identifier '{identifier}' not found.",
                    }
            except Exception as e:  # pragma: no cover
                logger.error(
                    f"Failed resolving ObjectId '{identifier}' to concept_id: {e}"
                )
                return {
                    "success": False,
                    "error": "Failed resolving identifier to concept_id.",
                }
        elif identifier.startswith("#V#"):
            resolved_concept_id = identifier
        else:
            return {
                "success": False,
                "error": "Invalid identifier format (expected ObjectId or #V# concept_id).",
            }

        # Lazy import to avoid circular dependencies
        try:
            from ..services.concept_service import (
                update_concept_description as _update_concept_description,
            )
        except Exception as e:  # pragma: no cover
            logger.error(
                f"Unable to import concept_service.update_concept_description: {e}"
            )
            return {"success": False, "error": "Internal import error."}

        try:
            _DEPRECATED_CALL_COUNTER["update_vontology_node_description"] += 1
            _persist_deprecation_metric("update_vontology_node_description")
        except Exception:  # pragma: no cover
            pass
        logger.warning(
            "DEPRECATED_CALL: update_vontology_node_description -> update_concept_description | concept_id=%s | total_calls=%s",
            resolved_concept_id,
            _DEPRECATED_CALL_COUNTER.get("update_vontology_node_description"),
        )

        ok = _update_concept_description(resolved_concept_id, description)
        if not ok:
            return {
                "success": False,
                "error": f"Failed to update description for '{resolved_concept_id}'.",
            }
        return {"success": True, "concept_id": resolved_concept_id}
    except PyMongoError as e:
        logger.error(
            f"MongoDB error while updating description for node '{identifier}': {e}"
        )
        return {"success": False, "error": f"MongoDB error: {e}"}
    except Exception as e:  # pragma: no cover
        logger.error(
            f"Unexpected error while updating description for node '{identifier}': {e}"
        )
        return {"success": False, "error": f"Unexpected error: {e}"}


def update_vontology_node_in_db(
    concept_id: str,
    update_data: dict,
    collection_name: str = VONTOLOGY_NODES_COLLECTION_NAME,
) -> bool:
    """
    Updates a Vontology node in MongoDB based on its concept_id.

    Args:
        concept_id: The concept_id of the node to update.
        update_data: A dictionary containing the fields to update.
                     e.g., {"prompt_template": "new_template", "description": "new_desc"}
        collection_name: The name of the MongoDB collection.

    Returns:
        True if the update was successful (at least one document was modified), False otherwise.
    """
    if not concept_id or not update_data:
        logger.error(
            "update_vontology_node_in_db: concept_id and update_data must be provided."
        )
        return False

    try:
        db = get_db()
        if db is None:
            logger.error(
                "update_vontology_node_in_db: Database not available for collection '%s'",
                collection_name,
            )
            return False
        collection = db[collection_name]

        # Ensure that internal MongoDB fields like _id are not accidentally part of update_data
        # unless explicitly handled (which is not the case here).
        # We are only setting new values or updating existing ones.
        update_payload = {"$set": update_data}

        result = collection.update_one({"concept_id": concept_id}, update_payload)

        if result.matched_count == 0:
            logger.warning(
                f"update_vontology_node_in_db: No Vontology node found with concept_id '{concept_id}'. No update performed."
            )
            return False

        if result.modified_count == 0 and result.matched_count > 0:
            logger.info(
                f"update_vontology_node_in_db: Vontology node '{concept_id}' found, but the provided data did not change any existing values."
            )
            # Still considered a success in terms of finding and processing, though no data changed.
            # Depending on strictness, this could return False if a change was expected.
            # For now, if it matched, we'll say it's fine.

        logger.info(
            f"Successfully updated Vontology node '{concept_id}'. Matched: {result.matched_count}, Modified: {result.modified_count}"
        )
        return True

    except PyMongoError as e:
        logger.error(f"MongoDB error while updating Vontology node '{concept_id}': {e}")
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error while updating Vontology node '{concept_id}': {e}"
        )
        return False


# --- Filesystem Scanning and MongoDB Synchronization ---


def get_all_vontology_nodes_with_details(identifier: str = "Thing"):
    """
    Returns a Vontology concept and all its descendants from MongoDB.
    Uses $graphLookup to traverse "is_a_type_of" relationships.

    Robust identifier handling:
    - Prefer concept_id when provided (e.g., '#V#Foo').
    - If identifier looks like an ObjectId, try both string '_id' and ObjectId('_id').
    - As fallbacks, try 'path' then 'name' (deprecated).
    """
    # Build candidate queries in order of preference
    candidate_queries = []
    try:
        if isinstance(identifier, str) and identifier.startswith("#V#"):
            candidate_queries.append({"concept_id": identifier})
        if ObjectId.is_valid(identifier):
            # Try string _id first to support string-stored _id docs
            candidate_queries.append({"_id": identifier})
            # Then try actual ObjectId
            candidate_queries.append({"_id": ObjectId(identifier)})
        # Fallbacks (deprecated): path then name
        candidate_queries.append({"path": identifier})
        candidate_queries.append({"name": identifier})
    except bson_errors.InvalidId:
        # Not a valid ObjectId; keep non-ObjectId candidates
        candidate_queries.append({"path": identifier})
        candidate_queries.append({"name": identifier})

    last_query = None
    try:
        for start_node_query in candidate_queries:
            last_query = start_node_query
            logger.info(f"Starting subtree search with query: {start_node_query}")
            pipeline = [
                {"$match": start_node_query},
                {
                    "$graphLookup": {
                        "from": VONTOLOGY_NODES_COLLECTION_NAME,
                        "startWith": "$concept_id",
                        "connectFromField": "concept_id",
                        "connectToField": "is_a_type_of",
                        "as": "descendants",
                        "depthField": "depth",
                    }
                },
            ]
            result = list(ConceptsRepository.aggregate(pipeline))
            if result:
                # Combine start node with descendants
                start_node = result[0]
                descendants = start_node.pop("descendants", [])
                all_nodes = [start_node] + descendants
                # Convert ObjectIds to strings
                for node in all_nodes:
                    if "_id" in node:
                        node["_id"] = str(node["_id"])
                logger.info(
                    f"Query for identifier '{identifier}' yielded {len(all_nodes)} total nodes (including start node)."
                )
                return all_nodes
            else:
                logger.warning(
                    f"No node found for identifier '{identifier}' with query {start_node_query}. Trying next candidate..."
                )

        # If all candidates failed
        logger.warning(
            f"Exhausted all identifier forms for '{identifier}'. No subtree found. Last query: {last_query}"
        )
        return []

    except PyMongoError as e:
        logger.error(
            f"MongoDB error during $graphLookup for identifier '{identifier}': {e}"
        )
        return {"error": f"Database error while fetching subtree for '{identifier}'."}
    except Exception as e:
        logger.error(f"Unexpected error during subtree fetch for '{identifier}': {e}")
        return {
            "error": f"An unexpected error occurred while fetching subtree for '{identifier}'."
        }


def get_vontology_node_content(identifier: str, *, reconstruct_md: bool = True) -> dict:
    """
    Fetches a Vontology concept's details from MongoDB by its path, concept_id, or _id.
    Renders markdown content to HTML.
    """
    if not identifier:
        logger.error("Identifier cannot be empty for get_vontology_node_content.")
        return {"error": "Identifier cannot be empty."}
    # Use repository for read

    from ..utils.concept_id_utils import canonicalise_vontology_concept_id

    query = {}
    try:
        if ObjectId.is_valid(identifier):
            # Try both ObjectId and string formats since the database might store IDs as strings
            query = {"_id": identifier}  # Try as real ObjectId first
        elif identifier.startswith("#V#"):
            # Canonicalise punctuation variants (e.g. hyphen vs underscore).
            # Prefer the canonical ID if it differs; fall back to the original if needed.
            canonical_id = canonicalise_vontology_concept_id(identifier)
            if canonical_id and canonical_id != identifier:
                doc = ConceptsRepository.find_one({"concept_id": canonical_id})
                if doc is None:
                    query = {"concept_id": identifier}
                else:
                    query = {"concept_id": canonical_id}
            else:
                query = {"concept_id": identifier}
        else:
            query = {"path": identifier}
            # DEPRECATION WARNING
            caller = inspect.stack()[1]
            logger.warning(
                f"DEPRECATION WARNING: Path-based usage of get_vontology_node_content with identifier '{identifier}'. "
                f"Called from {caller.filename}:{caller.lineno} in {caller.function}"
            )
    except bson_errors.InvalidId:
        logger.warning(
            f"Received an invalid BSON ObjectId string: {identifier}. Treating as path/name."
        )
        query = {"path": identifier}  # Fallback to path if ID is invalid
        # DEPRECATION WARNING
        caller = inspect.stack()[1]
        logger.warning(
            f"DEPRECATION WARNING: Path-based usage of get_vontology_node_content with invalid ObjectId '{identifier}'. "
            f"Called from {caller.filename}:{caller.lineno} in {caller.function}"
        )

    doc = ConceptsRepository.find_one(query)  # Fetch with _id first

    if not doc:
        # If it was a valid ObjectId but not found as string, try as ObjectId object
        if ObjectId.is_valid(identifier) and "_id" in query:
            logger.info(
                f"Concept not found with string ID '{identifier}', trying as ObjectId."
            )
            query = {"_id": ObjectId(identifier)}
            doc = ConceptsRepository.find_one(query)

    if not doc:
        # If not found and it wasn't an ID-based search, try by name as a fallback
        if "path" in query:
            logger.info(f"Concept not found by path '{identifier}', trying by name.")
            doc = ConceptsRepository.find_one({"name": identifier})

    if not doc:
        logger.warning(
            f"Concept '{identifier}' not found in MongoDB (searched by id, path, and potentially name)."
        )
        # Virtual fallback: some concept-like identifiers are primarily handled in code.
        # These should still render in the UI (cartouches/tabs/tree) without requiring
        # database mutation.
        try:
            if isinstance(identifier, str) and identifier.startswith("#V#"):
                from .code_concepts_registry import build_virtual_concept_doc

                virtual_doc = build_virtual_concept_doc(identifier)
                if isinstance(virtual_doc, dict) and virtual_doc.get("concept_id"):
                    doc = virtual_doc
        except Exception:
            # Fall through to the standard not-found response.
            doc = None

        if not doc:
            return {"error": f"Concept '{identifier}' not found in MongoDB."}

    # Convert _id to string for JSON serialization before returning
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])

    # Create a copy of doc for raw_doc that has all ObjectIds converted to strings
    import copy

    raw_doc_copy = copy.deepcopy(doc)

    # Recursively convert any ObjectIds in the raw_doc to strings
    def convert_objectids_to_strings(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, ObjectId):
                    obj[key] = str(value)
                elif isinstance(value, (dict, list)):
                    convert_objectids_to_strings(value)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, ObjectId):
                    obj[i] = str(item)
                elif isinstance(item, (dict, list)):
                    convert_objectids_to_strings(item)

    convert_objectids_to_strings(raw_doc_copy)

    md_content = doc.get("md_content")

    if md_content is None and reconstruct_md:
        logger.debug(
            "Markdown content (md_content) missing for '%s'. Reconstructing basic version.",
            identifier,
        )
        # Try to get name from multiple possible locations, with human-readable fallback
        # Prefer names[] "NL" entry; do not use metadata.title anymore
        name = get_concept_display_name_with_names_fallback(doc)
        # Use accessor functions for description and notes
        description = get_concept_description(doc)
        notes = get_concept_notes(doc)

        # NOTE: Older versions reconstructed md_content with boilerplate metadata
        # (Source Concept/SubConcept Of/Instance Of). Those placeholders are noisy
        # and redundant with UI fields, so keep the fallback minimal.
        md_content = f"# {name}\n\n"

        if description:
            md_content += f"## Description\n\n{description}\n\n"

        if notes:
            md_content += f"## Notes\n\n{notes}\n"

    html = markdown.markdown(md_content or "")

    # Normalize relationship fields to always be arrays for inference simplicity
    rels = doc.get("relationships", {}) or {}
    isa_types = rels.get("is_a_type_of")
    if isa_types is None:
        isa_types_norm = []
    elif isinstance(isa_types, list):
        isa_types_norm = isa_types
    else:
        isa_types_norm = [isa_types]
    inst_of = rels.get("is_an_instance_of")
    if inst_of is None:
        inst_of_norm = []
    elif isinstance(inst_of, list):
        inst_of_norm = inst_of
    else:
        inst_of_norm = [inst_of]
    # Also normalize in raw_doc copy
    try:
        if "relationships" in raw_doc_copy:
            if not isinstance(raw_doc_copy["relationships"].get("is_a_type_of"), list):
                val = raw_doc_copy["relationships"].get("is_a_type_of")
                raw_doc_copy["relationships"]["is_a_type_of"] = (
                    [] if val is None else (val if isinstance(val, list) else [val])
                )
            if not isinstance(
                raw_doc_copy["relationships"].get("is_an_instance_of"), list
            ):
                val = raw_doc_copy["relationships"].get("is_an_instance_of")
                raw_doc_copy["relationships"]["is_an_instance_of"] = (
                    [] if val is None else (val if isinstance(val, list) else [val])
                )
    except Exception:
        pass

    # Compute kind using three-way classification: type, predicate, or individual
    # Check predicate FIRST: predicates can have is_a_type_of relationships
    # (e.g., a predicate subtype), which would incorrectly match is_type().
    if is_predicate(doc):
        computed_kind = "predicate"
    elif is_type(doc):
        computed_kind = "type"
    else:
        computed_kind = "individual"

    # NOTE (JVNAUTOSCI-335): We intentionally no longer emit a top-level 'name' field.
    # Callers must use 'display_name' (preferred) or inspect names[] inside raw_doc.
    payload = {
        "content_html": html,
        "display_name": get_concept_display_name_with_names_fallback(doc),
        "concept_id": doc.get("concept_id"),
        "path": doc.get("path"),
        # Top-level 'description' intentionally suppressed globally (JVNAUTOSCI-573 rollout)
        # Callers must resolve description via relation-aware APIs (text values with predicate 'hasDescription').
        "is_a_type_of": isa_types_norm,
        "is_an_instance_of": inst_of_norm,
        "kind": computed_kind,  # Three-way classification: type, predicate, or individual
        "computed_kind": computed_kind,  # Keep for backward compatibility
        "source_concept": doc.get("source_concept"),
        "md_content": md_content,
        "raw_doc": raw_doc_copy,
    }
    return payload


def get_vontology_node_parents(identifier: str) -> dict:
    """
    Fetches the direct parents for a given Vontology concept.
    """
    if not identifier:
        return {"error": "Identifier cannot be empty."}

    # Use repository for reads

    query = {}
    try:
        if ObjectId.is_valid(identifier):
            query = {"_id": ObjectId(identifier)}
        elif identifier.startswith("#V#"):
            query = {"concept_id": identifier}
        else:
            # Path-based lookup is not supported for this function
            return {
                "error": "Path-based parent lookup is not supported. Please provide a concept_id or _id."
            }
    except bson_errors.InvalidId:
        return {"error": f"Invalid BSON ObjectId string: {identifier}."}

    # Include names[] so display name can be resolved correctly, also include most_salient_type
    node = ConceptsRepository.find_one(
        query,
        {
            "name": 1,
            "names": 1,
            "relationships.is_a_type_of": 1,
            "relationships.most_salient_type": 1,
            "concept_id": 1,
        },
    )

    if not node:
        return {"error": f"Concept '{identifier}' not found."}

    # Get all parent relationships for context, but mark the most salient one
    relationships = node.get("relationships", {})
    parent_ids = relationships.get("is_a_type_of", [])
    most_salient_parent = relationships.get("most_salient_type")

    if isinstance(parent_ids, str):  # Handle case where it might be a single string
        parent_ids = [parent_ids]

    parents = []
    if parent_ids:
        parent_cursor = ConceptsRepository.find(
            {"concept_id": {"$in": parent_ids}},
            {"name": 1, "names": 1, "concept_id": 1},
        )
        for p in parent_cursor:
            parent_info = {
                "name": get_concept_display_name_with_names_fallback(p),
                "concept_id": p.get("concept_id"),
                "is_most_salient": p.get("concept_id") == most_salient_parent,
            }
            parents.append(parent_info)

    return {
        "node": {
            "name": get_concept_display_name_with_names_fallback(node),
            "concept_id": node.get("concept_id"),
        },
        "parents": parents,
        "most_salient_parent": most_salient_parent,
    }


def get_vontology_node_and_descendant_ids(
    identifier: str, include_descendants: bool = True
):
    """
    Fetches the concept_id of a Vontology concept and all its descendants.
    Returns a list of concept_ids (ObjectIds as strings).
    Phase 3: Updated to work with unified concepts collection.
    """
    # Use repository for aggregation

    start_node_query = {}
    try:
        if ObjectId.is_valid(identifier):
            start_node_query = {"_id": ObjectId(identifier)}
        elif identifier.startswith("#V#"):
            start_node_query = {"concept_id": identifier}
        else:
            # Try to match by name in Phase 3 structure
            start_node_query = {"name": identifier}
    except bson_errors.InvalidId:
        start_node_query = {"name": identifier}

    def _manual_descendants(start_concept_id: str) -> list[str]:
        if not include_descendants:
            return [start_concept_id]
        visited: set[str] = {start_concept_id}
        queue: list[str] = [start_concept_id]

        while queue:
            current = queue.pop(0)
            cursor = ConceptsRepository.find(
                {"relationships.is_a_type_of": current},
                {"concept_id": 1},
            )
            for doc in cursor:
                cid = doc.get("concept_id")
                if isinstance(cid, str) and cid and cid not in visited:
                    visited.add(cid)
                    queue.append(cid)

        return list(visited)

    start_doc = ConceptsRepository.find_one(start_node_query, {"concept_id": 1})
    if not start_doc or not isinstance(start_doc.get("concept_id"), str):
        return []
    start_concept_id = str(start_doc.get("concept_id"))

    # Phase 3: Updated pipeline to work with unified concepts collection
    # and relationships.is_a_type_of structure using concept_id strings.
    pipeline = [
        {"$match": {"concept_id": start_concept_id}},
        {
            "$graphLookup": {
                "from": CONCEPTS_COLLECTION_NAME,
                "startWith": "$concept_id",
                "connectFromField": "concept_id",
                "connectToField": "relationships.is_a_type_of",
                "as": "descendants",
            }
        },
        {
            "$project": {
                "all_concept_ids": {
                    "$concatArrays": [
                        ["$concept_id"],
                        "$descendants.concept_id",
                    ]
                }
            }
        },
    ]

    try:
        result = list(ConceptsRepository.aggregate(pipeline))
        if not result:
            return _manual_descendants(start_concept_id)

        raw_ids = result[0].get("all_concept_ids", [])
        all_concept_ids: list[str] = []
        for concept_id in raw_ids:
            if not concept_id:
                continue
            all_concept_ids.append(str(concept_id))

        # Mongomock (and some other test doubles) can incorrectly return literal
        # field-path strings like "$concept_id" instead of actual values.
        has_mongomock_artifacts = any(
            isinstance(cid, str) and cid.startswith("$") for cid in all_concept_ids
        )
        valid = [
            cid
            for cid in all_concept_ids
            if isinstance(cid, str) and cid.startswith("#V#")
        ]
        if has_mongomock_artifacts:
            # Mongomock $concatArrays doesn't resolve field references; the
            # start node's own concept_id is returned as the literal "$concept_id".
            # Fall back to manual BFS which doesn't rely on aggregation.
            return _manual_descendants(start_concept_id)
        if not valid:
            return _manual_descendants(start_concept_id)

        return list(set(valid))

    except Exception as e:
        logger.error(
            f"MongoDB error during get_vontology_node_and_descendant_ids for identifier '{identifier}': {e}"
        )
        return _manual_descendants(start_concept_id)


def get_vontology_node_and_ancestor_instance_ids(
    identifier: str, target_concept_id: str = "#V#von_user"
):
    """
    For access control: check if a concept is an instance of a target concept (directly or indirectly through type hierarchy).

    Logic:
    - Get the direct instance relationships of the concept (is_an_instance_of)
    - For each direct instance type, check if the target_concept_id is in its subtype hierarchy
    - This implements: A is instance of B AND B is subtype of C → A is instance of C

    Args:
        identifier: concept_id or name to check
        target_concept_id: the target concept to check for in the hierarchy (default: #V#von_user)

    Returns:
        bool: True if the concept is an instance of the target (directly or indirectly)
    """
    # First get the concept document
    start_node_query = {}
    try:
        if ObjectId.is_valid(identifier):
            start_node_query = {"_id": ObjectId(identifier)}
        elif identifier.startswith("#V#"):
            start_node_query = {"concept_id": identifier}
        else:
            # Try to match by name
            start_node_query = {"name": identifier}
    except bson_errors.InvalidId:
        start_node_query = {"name": identifier}

    concept_doc = ConceptsRepository.find_one(start_node_query)
    if not concept_doc:
        return False

    # Get direct instance relationships
    relationships = concept_doc.get("relationships", {})
    direct_instances = relationships.get("is_an_instance_of", [])

    # Normalize to list
    if isinstance(direct_instances, str):
        direct_instances = [direct_instances]
    elif not isinstance(direct_instances, list):
        direct_instances = []

    # For each direct instance type, check if target_concept_id is in its subtype hierarchy
    for instance_type in direct_instances:
        if not instance_type or not isinstance(instance_type, str):
            continue

        # Use existing function to get all descendants (subtypes) of this instance type
        try:
            descendants = get_vontology_node_and_descendant_ids(
                instance_type, include_descendants=True
            )
            if target_concept_id in descendants:
                return True
        except Exception as e:
            logger.warning(f"Error checking descendants for {instance_type}: {e}")
            continue

    return False


# --- Function to get Vontology tree structure ---
def get_vontology_tree(root_concept: str = "Thing"):
    """Return a single-root ontology tree (root = Thing) with exactly one parent per non-root type.

    Rules:
      * Exclude pure individuals (instance-of only, no type-of).
      * Parent choice order: relationships.most_salient_type -> first relationships.is_a_type_of -> (fallback) Thing.
      * Every non-Thing type must appear exactly once under its chosen parent.
      * Orphan types (no parent list) attach directly under Thing.
    """
    logger.info(f"[get_vontology_tree] START (version={VONTOLOGY_UTILS_VERSION})")
    try:
        docs = list(
            ConceptsRepository.find(
                {},
                {
                    "concept_id": 1,
                    "name": 1,
                    "names": 1,
                    "relationships.is_a_type_of": 1,
                    "relationships.most_salient_type": 1,
                    "relationships.is_an_instance_of": 1,
                    "path": 1,
                    "_id": 1,
                },
            )
        )
        # Inject virtual code concepts so they can render in the tree even when not
        # persisted as concept documents.
        try:
            from .code_concepts_registry import (
                iter_code_concepts,
                build_virtual_concept_doc,
            )

            existing_ids = {
                d.get("concept_id")
                for d in docs
                if isinstance(d, dict) and isinstance(d.get("concept_id"), str)
            }
            for cc in iter_code_concepts():
                if cc.concept_id in existing_ids:
                    continue
                vdoc = build_virtual_concept_doc(cc.concept_id)
                if isinstance(vdoc, dict) and vdoc.get("concept_id"):
                    docs.append(vdoc)
        except Exception:
            pass
        total = len(docs)
        if not total:
            return {"tree": []}

        # Separate out type docs
        types: list[dict] = []
        skipped = 0
        for d in docs:
            # Keep predicates in the tree even though they are often pure instances.
            if is_pure_instance(d) and not is_predicate(d):
                skipped += 1
                continue
            if not d.get("concept_id"):
                continue
            types.append(d)
        logger.info(
            f"[get_vontology_tree] Loaded {total} docs; types={len(types)}; skipped_pure_instances={skipped}"
        )

        # Build node shells
        id_to_node: dict[str, dict] = {}
        for d in types:
            cid = d.get("concept_id")
            if not isinstance(cid, str) or not cid:
                continue
            id_to_node[cid] = {
                "id": cid,
                "concept_id": cid,  # for is_thing_concept compatibility
                "mongo_id": str(d.get("_id")) if d.get("_id") else "",
                "name": get_concept_display_name_with_names_fallback(d),
                "path": d.get("path") or cid,
                "children": [],
            }

        # Ensure Thing node exists (even if missing in DB)
        thing_id = THING_PRIMARY_ID
        if thing_id not in id_to_node:
            id_to_node[thing_id] = {
                "id": thing_id,
                "concept_id": thing_id,
                "mongo_id": "",
                "name": "Thing",
                "path": thing_id,
                "children": [],
            }
            logger.warning(
                "[get_vontology_tree] Inserted placeholder Thing node (missing from DB)."
            )

        # Parent assignment map child->parent
        parent_of: dict[str, str] = {}
        for d in types:
            cid = d.get("concept_id")
            if not isinstance(cid, str) or not cid:
                continue
            if is_thing_id(cid):
                continue  # root
            rel = d.get("relationships", {}) or {}
            parent: str | None = None
            # 1. most_salient_type
            ms = rel.get("most_salient_type")
            parents_raw = rel.get("is_a_type_of")
            # Normalize parents list
            if isinstance(parents_raw, str):
                parents_list = [parents_raw]
            elif isinstance(parents_raw, list):
                parents_list = [
                    p for p in parents_raw if isinstance(p, str) and p.strip()
                ]
            else:
                parents_list = []

            if ms and ms in parents_list:
                parent = ms
            elif parents_list:
                parent = parents_list[0]
            else:
                parent = thing_id  # orphan fallback

            # Predicates: prefer attaching under an appropriate predicate type.
            if is_predicate(d) and not parents_list:
                inst_raw = rel.get("is_an_instance_of")
                if isinstance(inst_raw, str):
                    inst_list = [inst_raw]
                elif isinstance(inst_raw, list):
                    inst_list = [
                        x for x in inst_raw if isinstance(x, str) and x.strip()
                    ]
                else:
                    inst_list = []

                preferred_predicate_parents = [
                    "#V#predicate",
                    "#V#relation",
                    "#V#property",
                    "#V#binary_predicate",
                    "#V#unary_predicate",
                    "#V#n_ary_predicate",
                    "#V#ternary_predicate",
                ]
                for candidate in preferred_predicate_parents + inst_list:
                    if candidate != cid and candidate in id_to_node:
                        parent = candidate
                        break

            # Final fallback if chosen parent missing
            if (
                parent
                and parent not in id_to_node
                and parent not in VONTOLOGY_THING_IDS
            ):
                # create lightweight placeholder (will appear unless later populated)
                id_to_node[parent] = {
                    "id": parent,
                    "concept_id": parent,
                    "mongo_id": "",
                    "name": parent.replace("#V#", "").replace("_", " ").title(),
                    "path": parent,
                    "children": [],
                }
            if not parent or parent not in id_to_node:
                parent = thing_id
            if isinstance(cid, str) and cid:
                parent_of[cid] = parent

        # Attach children
        for child_id, parent_id in parent_of.items():
            if child_id == parent_id:
                continue  # self-loop guard
            parent_node = id_to_node.get(parent_id)
            child_node = id_to_node.get(child_id)
            if not parent_node or not child_node:
                continue
            if not any(c["id"] == child_id for c in parent_node["children"]):
                parent_node["children"].append(child_node)

        # Move any nodes without an assigned parent (excluding Thing) under Thing
        thing_node = id_to_node[thing_id]
        assigned_children = set(parent_of.keys())
        for cid, node in id_to_node.items():
            if is_thing_id(cid):
                continue
            if cid not in assigned_children:
                if not any(c["id"] == cid for c in thing_node["children"]):
                    thing_node["children"].append(node)

        # Optional: sort children alphabetically for stable UI
        def sort_rec(n: dict):
            n["children"].sort(key=lambda c: c["name"].lower())
            for ch in n["children"]:
                sort_rec(ch)

        sort_rec(thing_node)

        logger.info(
            f"[get_vontology_tree] DONE. Nodes={len(id_to_node)}; Thing children={len(thing_node['children'])}"
        )
        return {"tree": [thing_node]}
    except Exception as e:
        logger.error(f"[get_vontology_tree] ERROR: {e}\n{traceback.format_exc()}")
        return {"error": str(e), "tree": []}


def get_concept_hierarchical_paths(
    concept_ids: List[str],
    max_depth: int = 10,
    separator: str = " → ",
    include_root: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Build hierarchical paths for multiple concepts efficiently (batch operation).

    Handles MULTIPLE INHERITANCE: A concept may have multiple parent paths to root.
    Follows is_a_type_of relationships upward from each concept to root (Thing).
    Uses batch queries to minimize database round-trips.

    Args:
        concept_ids: List of concept IDs to build paths for
        max_depth: Maximum depth to traverse (prevents infinite loops)
        separator: String to join path components (default: " → ")
        include_root: If True, include #V#thing in paths (default: True)

    Returns:
        Dict mapping concept_id to:
        {
            "paths": [  # LIST of all paths (multiple inheritance)
                {
                    "path": "thing → person → student",
                    "path_ids": ["#V#thing", "#V#person", "#V#student"],
                    "path_names": ["Thing", "Person", "Student"],
                    "depth": 3
                },
                {
                    "path": "thing → agent → student",
                    "path_ids": ["#V#thing", "#V#agent", "#V#student"],
                    "path_names": ["Thing", "Agent", "Student"],
                    "depth": 3
                }
            ],
            "primary_path": "thing → person → student",  # First/shortest path
            "all_parents": ["#V#person", "#V#agent"],  # Unique direct parents
            "max_depth": 3,
            "is_root": False
        }

    Example:
        >>> get_concept_hierarchical_paths(["#V#student"])
        {
            "#V#student": {
                "paths": [
                    {"path": "thing → person → student", "path_ids": [...], "path_names": [...], "depth": 3},
                    {"path": "thing → agent → student", "path_ids": [...], "path_names": [...], "depth": 3}
                ],
                "primary_path": "thing → person → student",
                "all_parents": ["#V#person", "#V#agent"],
                "max_depth": 3,
                "is_root": False
            }
        }
    """
    if not concept_ids:
        return {}

    result: Dict[str, Dict[str, Any]] = {}

    # Special case: #V#thing is root
    thing_id = "#V#thing"
    if thing_id in concept_ids:
        result[thing_id] = {
            "paths": [
                {
                    "path": "thing" if not include_root else "thing",
                    "path_ids": [thing_id] if include_root else [],
                    "path_names": ["Thing"] if include_root else [],
                    "depth": 1 if include_root else 0,
                }
            ],
            "primary_path": "thing" if not include_root else "thing",
            "all_parents": [],
            "max_depth": 1 if include_root else 0,
            "is_root": True,
        }

    # Batch fetch all concepts we might need
    # Start with requested concepts, then iteratively fetch parents
    concepts_to_fetch = set(concept_ids) - {thing_id}
    fetched_concepts: Dict[str, Dict[str, Any]] = {}

    for _ in range(max_depth):
        if not concepts_to_fetch:
            break

        # Fetch this batch
        cursor = ConceptsRepository.find(
            {"concept_id": {"$in": list(concepts_to_fetch)}},
            {"concept_id": 1, "names": 1, "name": 1, "relationships.is_a_type_of": 1},
        )

        newly_fetched = {}
        parent_ids_to_fetch = set()

        for doc in cursor:
            cid = doc.get("concept_id")
            if not cid:
                continue

            newly_fetched[cid] = doc

            # Extract ALL parent IDs (multiple inheritance)
            parents_raw = doc.get("relationships", {}).get("is_a_type_of", [])
            if isinstance(parents_raw, str):
                parents = [parents_raw]
            elif isinstance(parents_raw, list):
                parents = [p for p in parents_raw if isinstance(p, str)]
            else:
                parents = []

            # Queue all parents for fetching if not already known
            for pid in parents:
                if (
                    pid != thing_id
                    and pid not in fetched_concepts
                    and pid not in newly_fetched
                ):
                    parent_ids_to_fetch.add(pid)

        fetched_concepts.update(newly_fetched)
        concepts_to_fetch = parent_ids_to_fetch

    # Add Thing to fetched concepts
    fetched_concepts[thing_id] = {
        "concept_id": thing_id,
        "names": [{"name": "Thing", "lang": "en"}],
        "relationships": {},
    }

    # Build ALL paths for each requested concept (handles multiple inheritance)
    for concept_id in concept_ids:
        if concept_id == thing_id:
            continue  # Already handled

        if concept_id not in fetched_concepts:
            # Concept not found or not reachable
            result[concept_id] = {
                "paths": [],
                "primary_path": None,
                "all_parents": [],
                "max_depth": 0,
                "is_root": False,
                "error": "concept_not_found",
            }
            continue

        # Get direct parents
        doc = fetched_concepts.get(concept_id)
        if doc is None:
            continue
        parents_raw = doc.get("relationships", {}).get("is_a_type_of", [])
        if isinstance(parents_raw, str):
            all_direct_parents = [parents_raw]
        elif isinstance(parents_raw, list):
            all_direct_parents = [p for p in parents_raw if isinstance(p, str)]
        else:
            all_direct_parents = []

        # Build all paths using BFS to explore multiple inheritance tree
        all_paths = []

        def build_paths_recursive(
            current_id: str, current_path_ids: List[str], visited: set
        ) -> None:
            """Recursively build all paths from current_id to root."""
            if len(current_path_ids) >= max_depth:
                return

            if current_id in visited:
                # Cycle detected - store partial path up to this point
                if current_path_ids:
                    path_names = []
                    for pid in current_path_ids:
                        pdoc = fetched_concepts.get(pid)
                        if pdoc:
                            pname = get_concept_display_name_with_names_fallback(pdoc)
                            path_names.append(pname)

                    # Reverse for consistency
                    final_path_ids = list(reversed(current_path_ids))
                    final_path_names = list(reversed(path_names))

                    path_str = (
                        separator.join(name.lower() for name in final_path_names)
                        if final_path_names
                        else None
                    )

                    all_paths.append(
                        {
                            "path": path_str,
                            "path_ids": final_path_ids,
                            "path_names": final_path_names,
                            "depth": len(final_path_ids),
                        }
                    )
                return

            visited_copy = visited.copy()
            visited_copy.add(current_id)

            current_doc = fetched_concepts.get(current_id)
            if not current_doc:
                return

            # Get display name
            display_name = get_concept_display_name_with_names_fallback(current_doc)

            # Add current to path
            new_path_ids = current_path_ids + [current_id]

            # Check if we reached root
            if current_id == thing_id:
                # Completed path to root - REVERSE to get root → leaf order
                path_names = []
                for pid in new_path_ids:
                    pdoc = fetched_concepts.get(pid)
                    if pdoc:
                        pname = get_concept_display_name_with_names_fallback(pdoc)
                        path_names.append(pname)

                # Reverse for root → leaf order
                final_path_ids = list(reversed(new_path_ids))
                final_path_names = list(reversed(path_names))

                # Optionally exclude root
                if (
                    not include_root
                    and final_path_ids
                    and final_path_ids[0] == thing_id
                ):
                    final_path_ids = final_path_ids[1:]
                    final_path_names = final_path_names[1:]

                # Build path string (lowercase for readability)
                path_str = (
                    separator.join(name.lower() for name in final_path_names)
                    if final_path_names
                    else None
                )

                all_paths.append(
                    {
                        "path": path_str,
                        "path_ids": final_path_ids,
                        "path_names": final_path_names,
                        "depth": len(final_path_ids),
                    }
                )
                return

            # Get parents and recurse
            p_raw = current_doc.get("relationships", {}).get("is_a_type_of", [])
            if isinstance(p_raw, str):
                parents = [p_raw]
            elif isinstance(p_raw, list):
                parents = [p for p in p_raw if isinstance(p, str)]
            else:
                parents = []

            if not parents:
                # Orphaned concept - create partial path anyway (reverse for consistency)
                path_names = []
                for pid in new_path_ids:
                    pdoc = fetched_concepts.get(pid)
                    if pdoc:
                        pname = get_concept_display_name_with_names_fallback(pdoc)
                        path_names.append(pname)

                # Reverse paths even for orphans for consistency
                final_path_ids = list(reversed(new_path_ids))
                final_path_names = list(reversed(path_names))

                path_str = (
                    separator.join(name.lower() for name in final_path_names)
                    if final_path_names
                    else None
                )

                all_paths.append(
                    {
                        "path": path_str,
                        "path_ids": final_path_ids,
                        "path_names": final_path_names,
                        "depth": len(final_path_ids),
                    }
                )
                return

            # Recurse for each parent (multiple inheritance)
            for parent_id in parents:
                build_paths_recursive(parent_id, new_path_ids, visited_copy)

        # Start recursion from concept
        build_paths_recursive(concept_id, [], set())

        # Sort paths by depth (shortest first) for consistency
        all_paths.sort(key=lambda p: p["depth"])

        # Determine primary path (shortest/first)
        primary_path = all_paths[0]["path"] if all_paths else None
        max_path_depth = max((p["depth"] for p in all_paths), default=0)

        result[concept_id] = {
            "paths": all_paths,
            "primary_path": primary_path,
            "all_parents": all_direct_parents,
            "max_depth": max_path_depth,
            "is_root": False,
        }

    return result


def delete_vontology_concept(concept_id: str) -> dict:
    """
    Deletes a Vontology concept from MongoDB based on its concept_id.
        Before deletion, it stitches children (types) and instances to this node's direct parents
        to preserve hierarchy integrity. Specifically:
            - For each child where relationships.is_a_type_of contains this concept_id,
                remove this concept_id and add this node's parents to the child's is_a_type_of (deduped).
            - For each instance where relationships.is_an_instance_of contains this concept_id,
                remove this concept_id and add this node's parents to the instance's is_an_instance_of (deduped).
        If the deleted node has no parents, children/instances simply have the deleted reference removed.
    """
    if not concept_id:
        logger.error("delete_vontology_concept_from_db: concept_id must be provided.")
        return {"success": False, "message": "concept_id must be provided."}

    try:
        # Use repository for CRUD
        # Find the concept to be deleted
        concept_to_delete = ConceptsRepository.find_one({"concept_id": concept_id})
        if not concept_to_delete:
            logger.warning(f"Concept with id '{concept_id}' not found.")
            return {
                "success": False,
                "message": f"Concept with id '{concept_id}' not found.",
            }

        # Gather this node's direct parents (supertypes), normalize to list[str]
        parents_raw = concept_to_delete.get("relationships", {}).get("is_a_type_of", [])
        if isinstance(parents_raw, str):
            super_parents = [parents_raw]
        elif isinstance(parents_raw, list):
            super_parents = [p for p in parents_raw if isinstance(p, str) and p.strip()]
        else:
            super_parents = []

        # Reparent children (types)
        children_cursor = ConceptsRepository.find(
            {
                "$or": [
                    {"relationships.is_a_type_of": concept_id},
                    {"relationships.is_a_type_of": {"$in": [concept_id]}},
                ]
            },
            {"_id": 1, "concept_id": 1, "relationships.is_a_type_of": 1, "name": 1},
        )

        children_reparented = 0
        for child in children_cursor:
            child_id = child.get("concept_id")
            if not isinstance(child_id, str) or not child_id:
                continue

            child_changed = False

            # Remove the deleted concept from the child's parent list
            try:
                if ConceptsRepository.mutate_relationship_edge(
                    child_id, "is_a_type_of", concept_id, action="remove"
                ):
                    child_changed = True
            except ValueError:
                pass

            # Add super parents (dedup handled by mutate helper) avoiding self-references
            for sp in super_parents:
                if not isinstance(sp, str) or not sp or sp == child_id:
                    continue
                try:
                    if ConceptsRepository.mutate_relationship_edge(
                        child_id, "is_a_type_of", sp, action="add"
                    ):
                        child_changed = True
                except ValueError:
                    continue

            if child_changed:
                children_reparented += 1

        # Re-type instances (individuals)
        instances_cursor = ConceptsRepository.find(
            {
                "$or": [
                    {"relationships.is_an_instance_of": concept_id},
                    {"relationships.is_an_instance_of": {"$in": [concept_id]}},
                ]
            },
            {
                "_id": 1,
                "concept_id": 1,
                "relationships.is_an_instance_of": 1,
                "name": 1,
            },
        )

        instances_retyped = 0
        for inst in instances_cursor:
            inst_id = inst.get("concept_id")
            if not isinstance(inst_id, str) or not inst_id:
                continue

            inst_changed = False

            try:
                if ConceptsRepository.mutate_relationship_edge(
                    inst_id, "is_an_instance_of", concept_id, action="remove"
                ):
                    inst_changed = True
            except ValueError:
                pass

            for sp in super_parents:
                if not isinstance(sp, str) or not sp:
                    continue
                try:
                    if ConceptsRepository.mutate_relationship_edge(
                        inst_id, "is_an_instance_of", sp, action="add"
                    ):
                        inst_changed = True
                except ValueError:
                    continue

            if inst_changed:
                instances_retyped += 1

        # Finally delete the concept itself
        delete_result = ConceptsRepository.delete_one({"concept_id": concept_id})
        if getattr(delete_result, "deleted_count", 0) == 0:
            logger.warning(f"Concept with id '{concept_id}' was not deleted.")
            return {
                "success": False,
                "message": f"Concept with id '{concept_id}' was not deleted.",
            }

        # Remove associated text relations (names/descriptions/etc.) so deleted concepts
        # cannot re-surface via stale text-value edges.
        try:
            rel_delete = TextRelationsRepository.delete_many(
                {"subject_concept_id": concept_id}
            )
            text_relations_deleted = int(getattr(rel_delete, "deleted_count", 0) or 0)
        except Exception:
            text_relations_deleted = 0

        logger.info(
            f"Deleted concept '{concept_id}'. Reparented {children_reparented} children and re-typed {instances_retyped} instances."
        )

        # Optional cleanups of denormalized fields (best-effort, ignored errors)
        if super_parents:
            for parent_id in super_parents:
                if not isinstance(parent_id, str) or not parent_id:
                    continue
                try:
                    ConceptsRepository.mutate_relationship_edge(
                        parent_id, "has_subtype", concept_id, action="remove"
                    )
                except ValueError:
                    continue

        return {
            "success": True,
            "message": (
                f"Successfully deleted concept '{concept_id}'. "
                f"Children reparented: {children_reparented}. Instances re-typed: {instances_retyped}."
            ),
            "deleted_count": getattr(delete_result, "deleted_count", 0),
            "children_updated": children_reparented,
            "instances_updated": instances_retyped,
            "text_relations_deleted": text_relations_deleted,
        }

    except PyMongoError as e:
        logger.error(f"MongoDB error while deleting concept '{concept_id}': {e}")
        return {"success": False, "message": f"MongoDB error: {e}"}
    except Exception as e:
        logger.error(f"Unexpected error while deleting concept '{concept_id}': {e}")
        return {"success": False, "message": f"Unexpected error: {e}"}


def simulate_or_delete_concept(concept_id: str, execute: bool = False) -> dict:
    """Unified simulate/delete helper.

    Args:
        concept_id: target concept id
        execute: when False perform read-only simulation; when True apply rewiring + delete

    Returns:
        dict with keys:
        - simulate: bool, whether this is a simulation
        - concept_id: the target concept id
        - parents: list of parent concept_ids
        - children_reparented: number of children reparented
        - children: list of child concepts with old and new parents
        - instances_retyped: number of instances re-typed
        - instances: list of instance concepts with old and new types
        - warnings: list of warning messages
        - executed: bool, whether the operation was executed
        - success: bool, whether the operation was successful
    """
    if not concept_id:
        return {
            "success": False,
            "error": "concept_id required",
            "simulate": not execute,
        }

    try:
        concept_doc = ConceptsRepository.find_one({"concept_id": concept_id})
        if not concept_doc:
            # Simulation must be read-only; only attempt cleanup when actually executing.
            if execute:
                try:
                    rel_del = TextRelationsRepository.delete_many(
                        {"subject_concept_id": concept_id}
                    )
                    cleaned = int(getattr(rel_del, "deleted_count", 0) or 0)
                except Exception:
                    cleaned = 0
            else:
                cleaned = 0

            # Return dict only (avoid tuple) to satisfy declared return type
            return {
                "success": False,
                "error": f"Concept '{concept_id}' not found",
                "simulate": not execute,
                "text_relations_deleted": cleaned,
            }

        warnings: list[str] = []
        protected = False
        if concept_id == THING_PRIMARY_ID:
            warnings.append("Cannot delete Thing root concept")
            protected = True

        # Guard for future governance: prevent deletion of code-linked concepts
        rels = concept_doc.get("relationships") or {}
        inst_of = rels.get("is_an_instance_of") or []
        if isinstance(inst_of, str):
            inst_list = [inst_of]
        elif isinstance(inst_of, list):
            inst_list = [i for i in inst_of if isinstance(i, str)]
        else:
            inst_list = []
        if "#V#mentioned_in_von_code" in inst_list:
            warnings.append(
                "Concept is instance of #V#mentioned_in_von_code and is protected"
            )
            protected = True

        # Collect parents
        parents_raw = rels.get("is_a_type_of") or []
        if isinstance(parents_raw, str):
            parents = [parents_raw]
        elif isinstance(parents_raw, list):
            parents = [p for p in parents_raw if isinstance(p, str) and p]
        else:
            parents = []

        # Children (types) referencing this as parent
        child_cursor = ConceptsRepository.find(
            {
                "$or": [
                    {"relationships.is_a_type_of": concept_id},
                    {"relationships.is_a_type_of": {"$in": [concept_id]}},
                ]
            },
            {"concept_id": 1, "relationships.is_a_type_of": 1},
        )
        children = []
        for ch in child_cursor:
            cid = ch.get("concept_id")
            ch_parents = ch.get("relationships", {}).get("is_a_type_of", [])
            if isinstance(ch_parents, str):
                ch_list = [ch_parents]
            elif isinstance(ch_parents, list):
                ch_list = [p for p in ch_parents if isinstance(p, str)]
            else:
                ch_list = []
            new_parents = [p for p in ch_list if p != concept_id]
            for sp in parents:
                if sp and sp not in new_parents and sp != cid:
                    new_parents.append(sp)
            children.append(
                {
                    "child_id": cid,
                    "old_parents": ch_list,
                    "new_parents": list(dict.fromkeys(new_parents)),
                }
            )

        # Instances referencing this as type
        inst_cursor = ConceptsRepository.find(
            {
                "$or": [
                    {"relationships.is_an_instance_of": concept_id},
                    {"relationships.is_an_instance_of": {"$in": [concept_id]}},
                ]
            },
            {"concept_id": 1, "relationships.is_an_instance_of": 1},
        )
        instances = []
        for inst in inst_cursor:
            iid = inst.get("concept_id")
            i_types = inst.get("relationships", {}).get("is_an_instance_of", [])
            if isinstance(i_types, str):
                t_list = [i_types]
            elif isinstance(i_types, list):
                t_list = [p for p in i_types if isinstance(p, str)]
            else:
                t_list = []
            new_types = [p for p in t_list if p != concept_id]
            for sp in parents:
                if sp and sp not in new_types:
                    new_types.append(sp)
            instances.append(
                {
                    "instance_id": iid,
                    "old_types": t_list,
                    "new_types": list(dict.fromkeys(new_types)),
                }
            )

        if not execute or protected:
            return {
                "success": not protected,
                "simulate": True,
                "executed": False,
                "concept_id": concept_id,
                "parents": parents,
                "children_reparented": len(children),
                "children": children,
                "instances_retyped": len(instances),
                "instances": instances,
                "warnings": warnings,
                "protected": protected,
            }

        del_result = None

        def _sync_relationships(
            source_id: Optional[str],
            rel_kind: str,
            old_values: List[str],
            new_values: List[str],
        ):
            if not source_id:
                return
            new_values_list = [v for v in new_values if isinstance(v, str) and v]
            old_values_list = [v for v in old_values if isinstance(v, str) and v]

            for target in old_values_list:
                if target not in new_values_list:
                    try:
                        ConceptsRepository.mutate_relationship_edge(
                            source_id, rel_kind, target, action="remove"
                        )
                    except ValueError:
                        continue

            for target in new_values_list:
                try:
                    ConceptsRepository.mutate_relationship_edge(
                        source_id, rel_kind, target, action="add"
                    )
                except ValueError:
                    continue

        for ch in children:
            _sync_relationships(
                ch.get("child_id"),
                "is_a_type_of",
                ch.get("old_parents", []),
                ch.get("new_parents", []),
            )

        for inst in instances:
            _sync_relationships(
                inst.get("instance_id"),
                "is_an_instance_of",
                inst.get("old_types", []),
                inst.get("new_types", []),
            )

        del_result = ConceptsRepository.delete_one({"concept_id": concept_id})

        # Remove associated text relations (names/descriptions/etc.) so deleted concepts
        # cannot re-surface via stale text-value edges.
        try:
            rel_delete = TextRelationsRepository.delete_many(
                {"subject_concept_id": concept_id}
            )
            text_relations_deleted = int(getattr(rel_delete, "deleted_count", 0) or 0)
        except Exception:
            text_relations_deleted = 0
        return {
            "success": getattr(del_result, "deleted_count", 0) == 1,
            "simulate": False,
            "executed": True,
            "concept_id": concept_id,
            "parents": parents,
            "children_reparented": len(children),
            "children": children,
            "instances_retyped": len(instances),
            "instances": instances,
            "warnings": warnings,
            "transactional": False,
            "text_relations_deleted": text_relations_deleted,
        }
    except Exception as e:  # pragma: no cover
        logger.error("simulate_or_delete_concept error: %s", e, exc_info=True)
        return {"success": False, "error": str(e), "simulate": not execute}


def is_opencyc_format(data: Union[List, Dict]) -> bool:
    """
    Detect if the input data is in OpenCyc format (nodes and edges structure).

    Args:
        data: The input data to check

    Returns:
        bool: True if data appears to be in OpenCyc format
    """
    if isinstance(data, dict):
        # Check for nodes and edges keys typical of OpenCyc format
        has_nodes = "nodes" in data and isinstance(data["nodes"], list)
        has_edges = "edges" in data and isinstance(data["edges"], list)

        if has_nodes and has_edges and len(data["nodes"]) > 0:
            # Check if nodes have OpenCyc-style structure (id, label, not concept_id)
            sample_nodes = data["nodes"][:3]  # Check first 3 nodes
            opencyc_indicators = 0

            for node in sample_nodes:
                if isinstance(node, dict):
                    has_id = "id" in node
                    has_label = "label" in node
                    no_concept_id = "concept_id" not in node

                    # Strong indicator: OpenCyc URL
                    if (
                        has_id
                        and has_label
                        and no_concept_id
                        and isinstance(node.get("id"), str)
                        and (
                            "opencyc.org" in node.get("id", "")
                            or node.get("id", "").startswith("http://")
                        )
                    ):
                        opencyc_indicators += 1
                    # Weaker indicator: has id/label but no concept_id (Von format always has concept_id)
                    elif has_id and has_label and no_concept_id:
                        opencyc_indicators += 1

            # Consider it OpenCyc if at least one node shows OpenCyc characteristics
            return opencyc_indicators > 0

    return False


def cyc_id_to_von_concept_id(cyc_id: str, label: str) -> str:
    """
    Convert OpenCyc ID to Von concept ID format.

    Args:
        cyc_id: The original OpenCyc ID
        label: The human-readable label

    Returns:
        str: Von-formatted concept ID
    """
    # Create a clean concept ID from the label
    # Remove special characters and convert to snake_case
    clean_label = re.sub(r"[^\w\s-]", "", label.lower())
    clean_label = re.sub(r"[-\s]+", "_", clean_label)
    clean_label = clean_label.strip("_")

    # Ensure it starts with #V#
    return f"#V#{clean_label}"


def convert_opencyc_to_von_format(cyc_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert OpenCyc format data to Von vontology format.

    Args:
        cyc_data: Dictionary with 'nodes' and 'edges' keys in OpenCyc format

    Returns:
        List of dictionaries in Von format
    """
    nodes = cyc_data.get("nodes", [])
    edges = cyc_data.get("edges", [])

    # Create a mapping from cyc_id to label for parent lookup
    id_to_label = {
        node["id"]: node["label"] for node in nodes if "id" in node and "label" in node
    }

    von_nodes = []

    for cyc_node in nodes:
        if not isinstance(cyc_node, dict):
            continue

        cyc_id = cyc_node.get("id")
        label = cyc_node.get("label")
        comment = cyc_node.get("comment", "")

        if not cyc_id or not label:
            logger.warning(
                f"Skipping OpenCyc node with missing id or label: {cyc_node}"
            )
            continue

        concept_id = cyc_id_to_von_concept_id(cyc_id, label)

        # Find parent relationships from edges
        parent_ids = []
        for edge in edges:
            if edge.get("to") == cyc_id:
                parent_cyc_id = edge.get("from")
                if parent_cyc_id and parent_cyc_id in id_to_label:
                    parent_label = id_to_label[parent_cyc_id]
                    parent_concept_id = cyc_id_to_von_concept_id(
                        parent_cyc_id, parent_label
                    )
                    parent_ids.append(parent_concept_id)

        # Create Von format node in unified format (JVNAUTOSCI-315 fix)
        now = datetime.now(timezone.utc)
        von_node = {
            "concept_id": concept_id,
            "names": [
                {"name": label.title(), "language": "en-US", "type": "NL"},
                {"name": cyc_id, "language": "cycL", "type": "CODE"},
            ],
            "description": comment if comment else f"Concept representing {label}",
            "relationships": {
                "is_a_type_of": parent_ids,
                "has_subtype": [],
                "is_an_instance_of": [],
                "has_instance": [],
                "related_to": [],
            },
            "metadata": {
                "description": comment if comment else f"Concept representing {label}",
                "concept_type": "collection",
                "tags": [],
                "classifications": [],
            },
            "concept_data": {
                "axioms": [],
                "constraints": [],
                "properties": {"md_content_hash": None},
                "source_attribution": {
                    "source_id": concept_id,
                    "source_type": "OpenCyc",
                    "source_metadata": {"original_cyc_id": cyc_id},
                },
            },
            "timestamps": {"created_at": now, "updated_at": now, "accessed_at": None},
            "created_by": "opencyc_import",
            "source": "OpenCyc",
            "original_cyc_id": cyc_id,
            "created_at": now.isoformat(),
            "last_db_update_timestamp": now,
            "version": 1,
        }

        von_nodes.append(von_node)

    logger.info(f"Converted {len(von_nodes)} OpenCyc nodes to Von format")
    return von_nodes


def normalize_node_to_unified_format(node: dict) -> dict:
    """
    Converts a node from legacy format to unified format.

    Legacy format has top-level is_a_type_of, unified format uses relationships.
    This ensures all imported nodes use the unified concepts collection format.

    Args:
        node: Node dictionary that may be in legacy format

    Returns:
        Node dictionary in unified format
    """
    # Make a copy to avoid modifying the original
    unified_node = node.copy()

    # Check if this is legacy format (has top-level is_a_type_of but no relationships)
    if "is_a_type_of" in unified_node and "relationships" not in unified_node:
        logger.info(
            f"Converting node {unified_node.get('concept_id', 'Unknown')} from legacy format to unified format"
        )

        # Extract legacy relationship data
        legacy_is_a_type_of = unified_node.pop("is_a_type_of", [])

        # Create unified relationships structure
        unified_node["relationships"] = {
            "is_a_type_of": legacy_is_a_type_of,
            "has_subtype": [],
            "is_an_instance_of": [],
            "has_instance": [],
            "related_to": [],
        }
    elif "relationships" not in unified_node:
        # Ensure we have the minimum unified format structure
        unified_node["relationships"] = {
            "is_a_type_of": [],
            "has_subtype": [],
            "is_an_instance_of": [],
            "has_instance": [],
            "related_to": [],
        }

    # Add metadata structure if missing
    if "metadata" not in unified_node:
        unified_node["metadata"] = {
            "description": unified_node.get("description", ""),
            "concept_type": "collection",  # Default type
            "tags": [],
            "classifications": [],
        }

    # Add concept_data structure if missing
    if "concept_data" not in unified_node:
        unified_node["concept_data"] = {
            "axioms": [],
            "constraints": [],
            "properties": {"md_content_hash": None},
            "source_attribution": {
                "source_id": unified_node.get("concept_id", "unknown"),
                "source_type": unified_node.get("source", "import"),
                "source_metadata": None,
            },
        }

    # Add timestamps if missing
    if "timestamps" not in unified_node:
        now = datetime.now(timezone.utc)
        unified_node["timestamps"] = {
            "created_at": unified_node.get("created_at", now),
            "updated_at": now,
            "accessed_at": None,
        }

    # Add standard fields if missing
    if "last_db_update_timestamp" not in unified_node:
        unified_node["last_db_update_timestamp"] = datetime.now(timezone.utc)

    if "version" not in unified_node:
        unified_node["version"] = 1

    # Convert legacy 'name' field to 'names' array if needed
    if "name" in unified_node and "names" not in unified_node:
        logger.info(
            f"Converting legacy 'name' field to 'names' array for {unified_node.get('concept_id', 'Unknown')}"
        )
        old_name = unified_node.pop("name")
        unified_node["names"] = [{"name": old_name, "language": "en-US", "type": "NL"}]

    return unified_node


def detect_circular_references_in_import(nodes: list) -> dict:
    """Detect potential circular references in nodes to be imported.

    Returns dict with has_cycles, cycles, safe_nodes, counts.
    """
    node_map: dict[str, dict] = {}
    graph: dict[str, set] = defaultdict(set)  # child -> parents

    for node in nodes:
        concept_id = node.get("concept_id")
        if not concept_id:
            continue
        node_map[concept_id] = node
        parent_ids = []
        if "relationships" in node:
            parent_ids = node["relationships"].get("is_a_type_of", [])
        elif "is_a_type_of" in node:
            parent_ids = node.get("is_a_type_of", [])
        if isinstance(parent_ids, str):
            parent_ids = [parent_ids]
        for parent_id in parent_ids:
            if parent_id:
                graph[concept_id].add(parent_id)

    try:
        existing_concepts = list(
            ConceptsRepository.find(
                {}, {"concept_id": 1, "relationships.is_a_type_of": 1}
            )
        )
        for concept in existing_concepts:
            concept_id = concept.get("concept_id")
            if not concept_id or concept_id in node_map:
                continue
            parent_ids = concept.get("relationships", {}).get("is_a_type_of", [])
            if isinstance(parent_ids, str):
                parent_ids = [parent_ids]
            for parent_id in parent_ids:
                if parent_id:
                    graph[concept_id].add(parent_id)
    except Exception as e:  # pragma: no cover
        logger.warning(
            f"Could not load existing relationships for cycle detection: {e}"
        )

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    cycles_found: list[list[str]] = []

    def dfs_visit(node: str, path: list[str]):  # noqa: ANN001
        if color[node] == GRAY:
            cycle_start = path.index(node)
            cycle = path[cycle_start:] + [node]
            cycles_found.append(cycle)
            return
        if color[node] == BLACK:
            return
        color[node] = GRAY
        path.append(node)
        for parent in graph[node]:
            dfs_visit(parent, path)
        path.pop()
        color[node] = BLACK

    for node in list(graph.keys()):
        if color[node] == WHITE:
            dfs_visit(node, [])

    unique_cycles: list[list[str]] = []
    seen_cycles: set[tuple[str, ...]] = set()
    for cycle in cycles_found:
        if len(cycle) > 1:
            min_idx = cycle[:-1].index(min(cycle[:-1]))
            normalized = tuple(cycle[min_idx:-1] + cycle[:min_idx])
            if normalized not in seen_cycles:
                seen_cycles.add(normalized)
                unique_cycles.append(cycle)

    nodes_in_cycles: set[str] = set()
    for cycle in unique_cycles:
        nodes_in_cycles.update(cycle[:-1])

    safe_nodes = [n for n in nodes if n.get("concept_id") not in nodes_in_cycles]

    return {
        "has_cycles": bool(unique_cycles),
        "cycles": unique_cycles,
        "safe_nodes": safe_nodes,
        "total_nodes": len(nodes),
        "safe_count": len(safe_nodes),
        "cycle_count": len(unique_cycles),
    }


def break_cycles_in_import_nodes(nodes: list[dict]) -> dict:
    """Attempt to break parent-link cycles among nodes to be imported by removing minimal edges.

    Strategy:
      * Build combined graph of existing + imported nodes (child -> parents).
      * Identify strongly connected components (SCCs) with size > 1 or self-loops.
      * Iteratively remove one edge per SCC using heuristic until graph is acyclic or no removable edges remain.
      * Only edges belonging to imported nodes are removed (we do not mutate existing stored documents).

    Heuristic priority (higher wins):
      1. Parent id starts with '#V#the_union_of_'
      2. Edge did not previously exist in DB (new import edge)
      3. Child currently has >1 parents
      4. Lexicographically maximal (child, parent)

    Returns dict:
      {
        'had_cycles': bool,
        'original_cycle_count': int,
        'removed_edge_count': int,
        'removed_edges': [ {child, parent, reason, rank_tuple} ],
        'unresolved_cycle_count': int,
        'nodes_modified': <list reference to modified nodes>
      }
    """
    try:  # Fail-safe – never let import crash due to breaker
        # Map concept_id->node for imported nodes
        imported_map: dict[str, dict] = {}
        for n in nodes:
            cid = n.get("concept_id")
            if isinstance(cid, str):
                imported_map[cid] = n

        if not imported_map:
            return {
                "had_cycles": False,
                "original_cycle_count": 0,
                "removed_edge_count": 0,
                "removed_edges": [],
                "unresolved_cycle_count": 0,
                "nodes_modified": nodes,
            }

        # Fetch existing docs for imported concept_ids present already to classify existing edges
        existing_docs: dict[str, dict] = {}
        try:
            existing_cursor = ConceptsRepository.find(
                {"concept_id": {"$in": list(imported_map.keys())}},
                {"concept_id": 1, "relationships.is_a_type_of": 1},
            )
            for doc in existing_cursor:
                dcid = doc.get("concept_id")
                if isinstance(dcid, str):
                    parents_raw = (doc.get("relationships") or {}).get(
                        "is_a_type_of", []
                    )
                    if isinstance(parents_raw, str):
                        parents_list = [parents_raw]
                    elif isinstance(parents_raw, list):
                        parents_list = [
                            p for p in parents_raw if isinstance(p, str) and p
                        ]
                    else:
                        parents_list = []
                    existing_docs[dcid] = {"parents": set(parents_list)}
        except Exception:
            existing_docs = {}

        # Helper to get current parent list for a node (handles legacy format)
        def get_parent_list(node: dict) -> list[str]:
            if "relationships" in node:
                parents = node.get("relationships", {}).get("is_a_type_of", [])
            else:
                parents = node.get("is_a_type_of", [])
            if isinstance(parents, str):
                return [parents]
            if isinstance(parents, list):
                return [p for p in parents if isinstance(p, str) and p]
            return []

        # Graph build function (child -> parents)
        def build_graph() -> dict[str, set[str]]:
            g: dict[str, set[str]] = defaultdict(set)
            # Imported nodes edges (modifiable)
            for cid, node in imported_map.items():
                for p in get_parent_list(node):
                    g[cid].add(p)
            # Existing nodes outside import – only needed if they participate in cycles via imported edges
            try:
                # Traverse parent chain breadth-first so cycles among existing nodes remain visible
                initial_parents = {
                    p
                    for n in imported_map.values()
                    for p in get_parent_list(n)
                    if isinstance(p, str) and p
                }
                pending: list[str] = [p for p in initial_parents]
                seen: set[str] = set()
                while pending:
                    # Process in reasonably small batches to avoid oversized $in queries
                    batch = pending[:50]
                    pending = pending[50:]
                    if not batch:
                        continue
                    parent_cursor = ConceptsRepository.find(
                        {"concept_id": {"$in": batch}},
                        {"concept_id": 1, "relationships.is_a_type_of": 1},
                    )
                    for doc in parent_cursor:
                        pcid = doc.get("concept_id")
                        if not isinstance(pcid, str):
                            continue
                        if pcid in seen:
                            # Even if seen, ensure node exists in graph for completeness
                            g.setdefault(pcid, set())
                        else:
                            seen.add(pcid)
                            g.setdefault(pcid, set())
                        rel_parents = (doc.get("relationships") or {}).get(
                            "is_a_type_of", []
                        )
                        if isinstance(rel_parents, str):
                            rel_parents_list = [rel_parents]
                        elif isinstance(rel_parents, list):
                            rel_parents_list = [
                                p for p in rel_parents if isinstance(p, str) and p
                            ]
                        else:
                            rel_parents_list = []
                        for pp in rel_parents_list:
                            g[pcid].add(pp)
                            if pp not in seen and pp not in pending:
                                pending.append(pp)
            except Exception:
                pass
            return g

        # Tarjan SCC
        def tarjan_scc(graph: dict[str, set[str]]):
            index = 0
            indices: dict[str, int] = {}
            lowlink: dict[str, int] = {}
            stack: list[str] = []
            on_stack: set[str] = set()
            sccs: list[list[str]] = []

            def strongconnect(v: str):
                nonlocal index
                indices[v] = index
                lowlink[v] = index
                index += 1
                stack.append(v)
                on_stack.add(v)
                for w in graph.get(v, ()):  # traverse parents
                    if w not in indices:
                        strongconnect(w)
                        lowlink[v] = min(lowlink[v], lowlink[w])
                    elif w in on_stack:
                        lowlink[v] = min(lowlink[v], indices[w])
                # If v is root of SCC
                if lowlink[v] == indices[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.remove(w)
                        comp.append(w)
                        if w == v:
                            break
                    sccs.append(comp)

            for v in list(graph.keys()):
                if v not in indices:
                    strongconnect(v)
            return sccs

        removed_edges: list[dict] = []

        # Helper to classify edge & produce heuristic rank tuple
        def edge_rank(child: str, parent: str) -> tuple:
            parent_is_union = 1 if parent.startswith("#V#the_union_of_") else 0
            existing_edge = 0
            if child in existing_docs and parent in existing_docs[child]["parents"]:
                existing_edge = (
                    1  # existing edge -> we prefer NOT to remove; invert later
                )
            # We want new edges preferred, so is_new_edge = 1 when existing_edge == 0
            is_new_edge = 1 - existing_edge
            child_parents_count = len(get_parent_list(imported_map.get(child, {})))
            # Rank tuple ordered by heuristic priority
            return (
                parent_is_union,
                is_new_edge,
                1 if child_parents_count > 1 else 0,
                child,
                parent,
            )

        # Apply removals iteratively
        iteration_guard = 0
        graph = build_graph()
        # Detect original cycles using DFS style from existing function for reference
        orig_cycle_detection = detect_circular_references_in_import(
            list(imported_map.values())
        )
        original_cycle_count = orig_cycle_detection.get("cycle_count", 0)
        had_cycles = bool(original_cycle_count)
        if not had_cycles:
            return {
                "had_cycles": False,
                "original_cycle_count": 0,
                "removed_edge_count": 0,
                "removed_edges": [],
                "unresolved_cycle_count": 0,
                "nodes_modified": nodes,
            }

        while iteration_guard < 100:  # safety cap
            iteration_guard += 1
            graph = build_graph()
            sccs = tarjan_scc(graph)
            # Collect problematic SCCs (size>1) and self-loops
            problem_sccs: list[list[str]] = []
            for comp in sccs:
                if len(comp) > 1:
                    problem_sccs.append(comp)
                else:
                    v = comp[0]
                    if v in graph and v in graph[v]:  # self loop
                        problem_sccs.append(comp)
            if not problem_sccs:
                break  # no more cycles

            progress_this_round = False
            for comp in problem_sccs:
                # Candidate removable edges inside this component where child is imported
                cand_edges: list[tuple[tuple, str, str]] = (
                    []
                )  # (rank_tuple, child, parent)
                comp_set = set(comp)
                for child in comp:
                    if child not in imported_map:
                        continue  # cannot modify existing-only node
                    for parent in get_parent_list(imported_map[child]):
                        if (
                            parent in comp_set
                        ):  # only edges internal to SCC are relevant
                            rank = edge_rank(child, parent)
                            cand_edges.append((rank, child, parent))
                if not cand_edges:
                    continue  # cannot resolve this SCC
                # Pick best edge (max rank tuple)
                cand_edges.sort(key=lambda x: x[0], reverse=True)
                best_rank, best_child, best_parent = cand_edges[0]
                # Remove the edge from imported node structure
                node_ref = imported_map.get(best_child)
                if node_ref:
                    # Modify both unified and legacy representations if present
                    if "relationships" in node_ref:
                        parents_field = node_ref["relationships"].get(
                            "is_a_type_of", []
                        )
                        if isinstance(parents_field, list):
                            node_ref["relationships"]["is_a_type_of"] = [
                                p for p in parents_field if p != best_parent
                            ]
                        elif (
                            isinstance(parents_field, str)
                            and parents_field == best_parent
                        ):
                            node_ref["relationships"]["is_a_type_of"] = []
                    if "is_a_type_of" in node_ref:  # legacy parallel retention
                        legacy_parents = node_ref.get("is_a_type_of")
                        if isinstance(legacy_parents, list):
                            node_ref["is_a_type_of"] = [
                                p for p in legacy_parents if p != best_parent
                            ]
                        elif (
                            isinstance(legacy_parents, str)
                            and legacy_parents == best_parent
                        ):
                            node_ref["is_a_type_of"] = []

                removed_edges.append(
                    {
                        "child": best_child,
                        "parent": best_parent,
                        "reason": "Cycle resolution",
                        "rank_tuple": best_rank,
                    }
                )

            # Double-check: if we removed an edge that was also an existing edge, we may have introduced a new cycle.
            # If so, we can either revert this removal or apply a different strategy.
            graph = build_graph()  # rebuild graph after removals
            sccs = tarjan_scc(graph)
            for comp in sccs:
                if len(comp) > 1:
                    # If we find a cycle again, we may need to revert the last removal or take additional action.
                    logger.warning(f"Cycle still exists after removal attempt: {comp}")
                    # Strategy: remove the last added edge from removed_edges (if any)
                    if removed_edges:
                        last_removal = removed_edges.pop()
                        logger.info(
                            f"Reverting removal of edge {last_removal['child']} -> {last_removal['parent']}"
                        )
                        # Re-add the edge to the node
                        node_ref = imported_map.get(last_removal["child"])
                        if node_ref:
                            # Modify both unified and legacy representations if present
                            if "relationships" in node_ref:
                                parents_field = node_ref["relationships"].get(
                                    "is_a_type_of", []
                                )
                                if isinstance(parents_field, list):
                                    node_ref["relationships"]["is_a_type_of"].append(
                                        last_removal["parent"]
                                    )
                                elif isinstance(parents_field, str):
                                    node_ref["relationships"]["is_a_type_of"] = [
                                        parents_field,
                                        last_removal["parent"],
                                    ]
                            if "is_a_type_of" in node_ref:  # legacy parallel retention
                                legacy_parents = node_ref.get("is_a_type_of")
                                if isinstance(legacy_parents, list):
                                    node_ref["is_a_type_of"].append(
                                        last_removal["parent"]
                                    )
                                elif isinstance(legacy_parents, str):
                                    node_ref["is_a_type_of"] = [
                                        legacy_parents,
                                        last_removal["parent"],
                                    ]
                        # Rebuild graph and check cycles again
                        graph = build_graph()
                        sccs = tarjan_scc(graph)
                        if any(len(comp) > 1 for comp in sccs):
                            logger.error(
                                "Reverted removal introduced new cycle! Manual intervention required."
                            )
                            # In case of failure, we could either stop here or attempt a different strategy.
                            # For now, let's break to avoid infinite loop.
                            break

            # If we made it here, we successfully broke the cycles
            progress_this_round = True

            if progress_this_round:
                logger.info("Cycle breaking progress: ")
                for comp in problem_sccs:
                    logger.info(f"  - Component: {comp}")

        # Final check: if cycles still exist, we may need to report or handle unresolved cycles
        graph = build_graph()  # rebuild graph after all removals
        sccs = tarjan_scc(graph)
        unresolved_cycles = [comp for comp in sccs if len(comp) > 1]
        if unresolved_cycles:
            logger.warning(
                f"Unresolved cycles remain after breaking attempt: {unresolved_cycles}"
            )
        else:
            logger.info("No unresolved cycles remain.")

        return {
            "had_cycles": had_cycles,
            "original_cycle_count": original_cycle_count,
            "removed_edge_count": len(removed_edges),
            "removed_edges": removed_edges,
            "unresolved_cycle_count": len(unresolved_cycles),
            "nodes_modified": nodes,
        }
    except Exception as e:
        logger.error(f"Error in break_cycles_in_import_nodes: {e}")
        return {
            "had_cycles": False,
            "original_cycle_count": 0,
            "removed_edge_count": 0,
            "removed_edges": [],
            "unresolved_cycle_count": 0,
            "nodes_modified": nodes,
        }


# --- Concept Field Accessors ---
# These functions provide a consistent interface for accessing concept fields
# that may be stored in different locations (legacy vs. new schema)


def get_concept_description(concept: Dict[str, Any]) -> Optional[str]:
    """Get concept description from canonical text relations (fallback legacy fields)."""
    if not concept or not isinstance(concept, dict):
        return None

    concept_id = concept.get("concept_id")
    if isinstance(concept_id, str) and concept_id.strip():
        try:
            from ..services.text_value_service import get_preferred_text_for_concept

            best = get_preferred_text_for_concept(
                concept_id,
                predicate_precedence=(
                    ("hasDescription", "#V#hasDescription"),
                    ("hasContent", "#V#hasContent"),
                ),
                preferred_languages=("en-NZ", "en"),
                limit=50,
            )
            text_value = best.get("text") if isinstance(best, dict) else None
            if isinstance(text_value, str) and text_value.strip():
                return text_value.strip()
        except Exception:
            pass

    # Check legacy top-level field
    if "description" in concept and isinstance(concept["description"], str):
        description_value = concept["description"].strip()
        if description_value:
            return description_value

    # Check vontology node attributes
    attributes = concept.get("attributes", {})
    if "description" in attributes and isinstance(attributes["description"], str):
        description_value = attributes["description"].strip()
        if description_value:
            return description_value

    return None


def _extract_comment_text(concept: Dict[str, Any]) -> Optional[str]:
    """Return a trimmed comment field if present and non-empty."""
    comment = concept.get("comment") if isinstance(concept, dict) else None
    if isinstance(comment, str):
        stripped = comment.strip()
        if stripped:
            return stripped
    return None


def resolve_description_text_for_import(node: Dict[str, Any]) -> Optional[str]:
    """Prefer explicit description fallbacks (description then comment)."""
    description = get_concept_description(node)
    if isinstance(description, str):
        stripped = description.strip()
        if stripped:
            return stripped
    return _extract_comment_text(node)


def get_concept_notes(concept: Dict[str, Any]) -> Optional[str]:
    """Get concept notes from canonical text relations (fallback legacy fields)."""
    if not concept or not isinstance(concept, dict):
        return None

    concept_id = concept.get("concept_id")
    if isinstance(concept_id, str) and concept_id.strip():
        try:
            from ..services.text_value_service import get_preferred_text_for_concept

            best = get_preferred_text_for_concept(
                concept_id,
                predicate_precedence=(("hasNote", "#V#hasNote"),),
                preferred_languages=("en-NZ", "en"),
                limit=50,
            )
            text_value = best.get("text") if isinstance(best, dict) else None
            if isinstance(text_value, str) and text_value.strip():
                return text_value.strip()
        except Exception:
            pass

    # Check legacy top-level field
    if "notes" in concept and isinstance(concept["notes"], str):
        notes_value = concept["notes"].strip()
        if notes_value:
            return notes_value

    # Check vontology node attributes
    attributes = concept.get("attributes", {})
    if "notes" in attributes and isinstance(attributes["notes"], str):
        notes_value = attributes["notes"].strip()
        if notes_value:
            return notes_value

    return None


def set_concept_description(concept: Dict[str, Any], description: str) -> None:
    """Set in-memory description convenience value (not persistent storage)."""
    if not concept or not isinstance(concept, dict):
        return

    concept["description"] = description


def set_concept_notes(concept: Dict[str, Any], notes: str) -> None:
    """Set in-memory notes convenience value (not persistent storage)."""
    if not concept or not isinstance(concept, dict):
        return

    concept["notes"] = notes


def create_vontology_concept(
    parent_id: str,
    new_concept_name: str,
    create_as_instance: bool = False,
    notes: Optional[str] = None,
    description: Optional[str] = None,
    instance_of_type: Optional[str] = None,
    created_by_concept_id: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
    event_namespace: Optional[str] = None,
    visibility_scope_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Creates a new concept in the Vontology.

    Args:
        parent_id: The concept_id of the parent concept (e.g., "#V#person")
        new_concept_name: The name for the new concept (will be used to generate concept_id)
        create_as_instance: If True, creates an instance of the parent type.
                           If False, creates a subtype of the parent.
        notes: Optional notes for the new concept
        description: Optional description for the new concept
        instance_of_type: Optional concept_id to create an is_an_instance_of relationship.
                         When provided, the concept will be BOTH a subtype of parent_id
                         AND an instance of instance_of_type. This is useful for predicates
                         that need to be instances of a specific predicate type.
        created_by_concept_id: Optional actor override for event-triggered workflows.
        organisation_concept_id: Optional organisation override for event-triggered workflows.
        event_namespace: Optional namespace override for event-triggered workflows.
        visibility_scope_mode: Optional visibility override ("organisation_general",
            "global_general", or default authenticated scoping).

    Returns:
        Dict with keys:
        - success: bool indicating if creation succeeded
        - message: str with success/error message
        - concept: dict with the created concept data (if successful)
    """
    try:
        # Local import to avoid circular dependency at module import time
        from ..services.concept_service import create_concept  # type: ignore
        from ..utils.concept_id_utils import canonicalise_vontology_concept_id

        # Validate concept name constraints upfront
        is_valid, error_msg = validate_concept_name_for_id(new_concept_name)
        if not is_valid:
            return {"success": False, "message": error_msg, "concept": None}

        # Generate a canonical concept_id from the provided name (slug-like input).
        # This prevents punctuation variants (e.g. hyphen vs underscore) creating distinct concepts.
        canonical_id = canonicalise_vontology_concept_id(new_concept_name)
        if not canonical_id:
            return {
                "success": False,
                "message": "New concept name is empty after normalisation.",
                "concept": None,
            }

        base_slug = canonical_id[3:]

        # If creating an INSTANCE we allow duplicate display names by disambiguating the concept_id
        # (Users commonly create multiple instances sharing a natural language name.)
        # For TYPES we retain strict uniqueness to avoid hierarchy ambiguity.
        from ..db.repositories.concepts_repository import ConceptsRepository

        candidate_concept_id = canonical_id
        if create_as_instance:
            # Preflight existence check and append incremental suffix until free
            counter = 2
            while ConceptsRepository.find_one({"concept_id": candidate_concept_id}):
                candidate_concept_id = f"#V#{base_slug}_{counter}"
                counter += 1
                if counter > 50:  # safety stop to avoid pathological loops
                    return {
                        "success": False,
                        "message": "Unable to allocate unique concept_id after 49 retries.",
                        "concept": None,
                    }
        else:
            # For type creation, fail fast if the concept_id already exists
            if ConceptsRepository.find_one({"concept_id": candidate_concept_id}):
                return {
                    "success": False,
                    "message": f"Type with id '{candidate_concept_id}' already exists.",
                    "concept": None,
                    "error_code": "already_exists",
                    "existing_concept_id": candidate_concept_id,
                    "input_name": new_concept_name,
                    "suggestion": "Use fetch_concept_content to examine the existing concept, or update_concept to modify it.",
                }

        parent_concept_ids = [parent_id] if parent_id else []

        created_concept = create_concept(
            name=new_concept_name,
            concept_id=candidate_concept_id,
            parent_concept_ids=parent_concept_ids,
            create_as_instance=create_as_instance,
            description=description,
            notes=notes,
            instance_of_type=instance_of_type,
            created_by_concept_id=created_by_concept_id,
            organisation_concept_id=organisation_concept_id,
            event_namespace=event_namespace,
            visibility_scope_mode=visibility_scope_mode,
        )

        if created_concept:
            return {
                "success": True,
                "message": f"Successfully created concept '{new_concept_name}'",
                "concept": created_concept,
                "canonical_concept_id": candidate_concept_id,
                "input_name": new_concept_name,
            }
        return {
            "success": False,
            "message": f"Failed to create concept '{new_concept_name}'",
            "concept": None,
            "input_name": new_concept_name,
        }
    except Exception as e:  # pragma: no cover - defensive catch
        logger.error(f"Error creating vontology concept '{new_concept_name}': {e}")
        return {
            "success": False,
            "message": f"Error creating concept: {str(e)}",
            "concept": None,
        }


def ensure_thing_exists_and_link_orphans() -> Dict[str, Any]:
    """
    Ensures that the Thing root concept exists in the database.
    If it doesn't exist, creates it.
    If it exists but there are orphan concepts (types with no parent), links them to Thing.

    Returns:
        Dict with keys:
        - thing_created: bool indicating if Thing was newly created
        - thing_concept_id: str with Thing's concept_id
        - orphans_linked: int count of orphan concepts linked to Thing
        - orphan_ids: list of concept_ids that were linked
    """
    from ..services.concept_service import create_concept
    from ..db.repositories.concepts_repository import ConceptsRepository

    logger.info("[ensure_thing_exists] Checking for Thing root concept")

    # Check if Thing already exists
    thing_concept = ConceptsRepository.find_one({"concept_id": THING_PRIMARY_ID})
    thing_created = False

    if not thing_concept:
        # Create Thing as root concept with rich metadata following OWL conventions
        logger.info("[ensure_thing_exists] Thing not found, creating root concept")
        try:
            thing_concept = create_concept(
                name="Thing",
                concept_id=THING_PRIMARY_ID,
                parent_concept_ids=[],  # No parent - it's the root
                create_as_instance=False,  # It's a type
                description="Thing is the root concept in an ontology, encompassing all entities, whether physical or abstract, real or conceptual. It serves as the broadest possible category, providing a common ancestor for every concept in the ontology.",
                notes="This is the universal root following OWL (Web Ontology Language) conventions. All concepts without explicit parent relationships are children of Thing. This prevents orphaned concepts in the hierarchy.",
                attributes={"domain": "Ontology", "role": "root", "standard": "OWL"},
                system_tags=["root", "ontology", "foundational", "owl"],
                user_tags=[],
            )
            thing_created = True
            logger.info(
                f"[ensure_thing_exists] Created Thing with rich metadata: {thing_concept.get('concept_id')}"
            )
        except Exception as e:
            logger.error(f"[ensure_thing_exists] Failed to create Thing: {e}")
            return {
                "thing_created": False,
                "thing_concept_id": None,
                "orphans_linked": 0,
                "orphan_ids": [],
                "error": str(e),
            }
    else:
        logger.info("[ensure_thing_exists] Thing already exists")

    # Find orphan concepts (types with empty or missing is_a_type_of)
    orphan_concepts = list(
        ConceptsRepository.find(
            {
                "$and": [
                    # Has no instance-of (it's not an instance)
                    {
                        "$or": [
                            {"relationships.is_an_instance_of": {"$exists": False}},
                            {"relationships.is_an_instance_of": []},
                            {"relationships.is_an_instance_of": ""},
                        ]
                    },
                    # Has empty or missing is_a_type_of (it's an orphan type)
                    {
                        "$or": [
                            {"relationships.is_a_type_of": {"$exists": False}},
                            {"relationships.is_a_type_of": []},
                            {"relationships.is_a_type_of": ""},
                        ]
                    },
                    # Not Thing itself
                    {"concept_id": {"$ne": THING_PRIMARY_ID}},
                ]
            }
        )
    )

    orphans_linked = 0
    orphan_ids = []

    if orphan_concepts:
        logger.info(
            f"[ensure_thing_exists] Found {len(orphan_concepts)} orphan concepts, linking to Thing"
        )

        for orphan in orphan_concepts:
            try:
                orphan_id = orphan.get("concept_id")
                if not orphan_id:
                    continue

                # Update orphan to have Thing as parent
                ConceptsRepository.update_one(
                    {"concept_id": orphan_id},
                    {"$set": {"relationships.is_a_type_of": [THING_PRIMARY_ID]}},
                )

                # Update Thing to have this orphan as child (reciprocal relationship)
                ConceptsRepository.update_one(
                    {"concept_id": THING_PRIMARY_ID},
                    {"$addToSet": {"relationships.has_subtype": orphan_id}},
                )

                orphans_linked += 1
                orphan_ids.append(orphan_id)
                logger.info(f"[ensure_thing_exists] Linked orphan {orphan_id} to Thing")

            except Exception as e:
                logger.warning(
                    f"[ensure_thing_exists] Failed to link orphan {orphan_id}: {e}"
                )

    logger.info(
        f"[ensure_thing_exists] Complete - created={thing_created}, orphans_linked={orphans_linked}"
    )

    return {
        "thing_created": thing_created,
        "thing_concept_id": THING_PRIMARY_ID,
        "orphans_linked": orphans_linked,
        "orphan_ids": orphan_ids,
    }


# Process-level cache: once modality concepts are verified, skip re-checking.
_modality_concepts_ensured = False
_modality_concepts_lock = threading.Lock()


def ensure_conversation_modality_concepts(force: bool = False) -> Dict[str, Any]:
    """Ensure core meeting/conversation modality concepts exist.

    Creates (if missing):
    - Type: Conversation Modality (#V#conversation_modality)
    - Instances: Zoom, Microsoft Teams, Google Meet

    Returns summary dict with created/exists counts.

    Args:
        force: If True, bypass the process-level cache and re-check DB.
    """
    global _modality_concepts_ensured

    # Fast path (outside lock): already verified this process lifetime
    if _modality_concepts_ensured and not force:
        return {
            "cached": True,
            "type_created": False,
            "instances_created": [],
            "instances_skipped": [],
            "errors": [],
        }

    # Acquire lock to prevent race conditions - other threads wait here
    with _modality_concepts_lock:
        # Double-check inside lock (another thread might have completed while we waited)
        if _modality_concepts_ensured and not force:
            logger.info("[ensure_modality] Cache HIT (after lock) - skipping DB checks")
            return {
                "cached": True,
                "type_created": False,
                "instances_created": [],
                "instances_skipped": [],
                "errors": [],
            }

        logger.info("[ensure_modality] Cache MISS - proceeding with DB checks")

        from ..services.concept_service import create_concept
        from ..utils.concept_id_utils import canonicalise_vontology_concept_id

        summary: Dict[str, Any] = {
            "type_created": False,
            "instances_created": [],
            "instances_skipped": [],
            "errors": [],
        }

        try:
            if not ConceptsRepository.find_one({"concept_id": THING_PRIMARY_ID}):
                ensure_thing_exists_and_link_orphans()
        except Exception:
            pass

        modality_type_id = "#V#conversation_modality"
        try:
            if not ConceptsRepository.find_one({"concept_id": modality_type_id}):
                created = create_vontology_concept(
                    parent_id=THING_PRIMARY_ID,
                    new_concept_name="Conversation Modality",
                    create_as_instance=False,
                    description=(
                        "A conversation modality is the medium/platform through which a conversation or meeting occurs, "
                        "such as Zoom, Microsoft Teams, or Google Meet."
                    ),
                    notes=(
                        "Used to tag conversations/meetings with the platform they occurred on. "
                        "This is a non-exclusive (many-to-many) categorisation; sessions may have multiple modalities."
                    ),
                )
                summary["type_created"] = bool(created.get("success"))
        except Exception as exc:
            summary["errors"].append(f"type_create_failed: {exc}")

        instances = [
            ("Zoom", "Video meeting platform."),
            ("Microsoft Teams", "Video meeting and collaboration platform."),
            ("Google Meet", "Video meeting platform (Google)."),
        ]

        for display_name, description in instances:
            try:
                concept_id = canonicalise_vontology_concept_id(display_name)
                if not concept_id:
                    summary["errors"].append(f"invalid_name: {display_name}")
                    continue

                if ConceptsRepository.find_one({"concept_id": concept_id}):
                    summary["instances_skipped"].append(concept_id)
                    continue

                create_concept(
                    name=display_name,
                    concept_id=concept_id,
                    parent_concept_ids=[modality_type_id],
                    create_as_instance=True,
                    description=description,
                    notes="Created by Von to support conversation metadata.",
                    system_tags=["conversation", "modality", "meeting"],
                )
                summary["instances_created"].append(concept_id)
            except Exception as exc:
                summary["errors"].append(f"instance_create_failed:{display_name}:{exc}")

        # Mark as ensured for this process lifetime (skip future checks)
        _modality_concepts_ensured = True
        return summary


# Note: add_upward_closure_nodes defined later (duplicate removed - keeping complete implementation at line 2949)
def _invalidate_vontology_caches_duplicate_removed(
    affected_concepts: list, correlation_id: str
) -> None:
    """
    Invalidate in-memory vontology caches for affected concepts.

    This function clears relevant cache entries when concepts are modified
    to ensure cache consistency.

    Args:
        affected_concepts: List of concept_ids that were affected by the operation
        correlation_id: Unique identifier for the operation (for logging)
    """
    try:
        logger.info(
            f"[cache_invalidation] Invalidating caches for {len(affected_concepts)} concepts (correlation: {correlation_id})"
        )

        # Note: The actual cache variables (_TREE_CACHE, _INSTANCE_COUNTS_CACHE, _SALIENT_CACHE)
        # are defined in vontology_routes.py, not here. In a full implementation, this function
        # would need to coordinate with the route module to clear those caches.
        # For now, we'll just log the operation to resolve the import error.

        for concept_id in affected_concepts:
            logger.debug(f"[cache_invalidation] Affected concept: {concept_id}")

    except Exception as e:
        logger.error(
            f"Error in invalidate_vontology_caches (correlation: {correlation_id}): {e}"
        )


def _ensure_import_description_relation(
    concept_id: str, description_text: Optional[str]
) -> None:
    """Create a hasDescription text relation when importing concept nodes."""
    if not concept_id or not isinstance(concept_id, str):
        return
    if not isinstance(description_text, str):
        return

    # Preserve formatting (newlines/indentation) for Markdown, but use a
    # whitespace-collapsed comparison to avoid creating duplicates.
    stored_text = description_text.replace("\r\n", "\n").strip()
    normalised = re.sub(r"\s+", " ", stored_text).strip().lower()
    if not normalised:
        return

    try:
        from ..services import text_value_service as text_service
        from ..security.access_control import bypass_access_control

        # Import jobs may run without a request context/user session. These
        # descriptions are part of the shared ontology import, so bypass access
        # control checks for this internal operation.
        with bypass_access_control():
            existing = text_service.get_texts_for_concept(
                concept_id, predicate="hasDescription", limit=10
            )
            for relation in existing:
                text = relation.get("text")
                if (
                    isinstance(text, str)
                    and re.sub(r"\s+", " ", text.strip()).strip().lower() == normalised
                ):
                    return  # Already present, nothing to do

            text_service.upsert_text_for_concept(
                subject_concept_id=concept_id,
                predicate="hasDescription",
                text=stored_text,
                lang="en-NZ",
            )
    except Exception as exc:  # pragma: no cover - guard import path during import jobs
        logger.warning(
            "[import_ontology_nodes] Failed to upsert hasDescription relation for %s: %s",
            concept_id,
            exc,
        )


def import_ontology_nodes(nodes: list, progress_callback=None) -> Dict[str, Any]:
    """
    Import a list of ontology nodes into the database.

    This function processes a list of ontology nodes, handles format conversion,
    cycle detection, and bulk insertion/updating.

    Args:
        nodes: List of node dictionaries to import
        progress_callback: Optional callback function for progress updates

    Returns:
        Dict with keys:
        - success: bool indicating if import succeeded
        - imported_count: number of nodes imported
        - updated_count: number of nodes updated
        - skipped_count: number of nodes skipped
        - converted_from_opencyc: bool indicating if OpenCyc conversion occurred
        - cycle_breaking: dict with cycle detection/breaking information
        - error: error message (if unsuccessful)
    """
    try:
        from ..db.repositories.concepts_repository import ConceptsRepository

        if not nodes:
            return {
                "success": True,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "converted_from_opencyc": False,
                "cycle_breaking": {
                    "had_cycles": False,
                    "original_cycle_count": 0,
                    "removed_edge_count": 0,
                    "removed_edges": [],
                    "unresolved_cycle_count": 0,
                },
            }

        logger.info(f"[import_ontology_nodes] Starting import of {len(nodes)} nodes")

        # Progress reporting
        if progress_callback:
            progress_callback(
                {"stage": "starting", "processed": 0, "total": len(nodes)}
            )

        # Convert OpenCyc format if needed
        converted_from_opencyc = False
        processed_nodes = []
        for i, node in enumerate(nodes):
            if is_opencyc_format(node):
                converted_node = convert_opencyc_to_von_format(node)
                processed_nodes.append(converted_node)
                converted_from_opencyc = True
            else:
                processed_nodes.append(node)

            if progress_callback and i % 10 == 0:
                progress_callback(
                    {"stage": "converting", "processed": i, "total": len(nodes)}
                )

        # Detect cycles before optionally breaking them (env strict mode may bail early)
        cycle_detection = detect_circular_references_in_import(processed_nodes)

        strict_env = os.getenv("VON_IMPORT_STRICT_CYCLES", "").lower() in {
            "1",
            "true",
            "yes",
        }
        if cycle_detection.get("has_cycles") and strict_env:
            logger.warning(
                "[import_ontology_nodes] Strict cycle mode enabled; aborting import due to detected cycles"  # noqa: E501
            )
            return {
                "success": False,
                "strict_mode": True,
                "cycles_detected": cycle_detection,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": len(processed_nodes),
                "converted_from_opencyc": converted_from_opencyc,
                "cycle_breaking": {
                    "had_cycles": True,
                    "original_cycle_count": cycle_detection.get("cycle_count", 0),
                    "removed_edge_count": 0,
                    "removed_edges": [],
                    "unresolved_cycle_count": cycle_detection.get("cycle_count", 0),
                },
            }

        cycle_breaking = break_cycles_in_import_nodes(processed_nodes)

        if progress_callback:
            progress_callback(
                {
                    "stage": "cycle_detection",
                    "cycles_found": cycle_breaking["original_cycle_count"],
                }
            )

        # Process nodes for import
        imported_count = 0
        updated_count = 0
        skipped_count = 0
        reconciliation_plan: list[tuple[str, dict, dict]] = []

        for i, node in enumerate(processed_nodes):
            try:
                concept_id = node.get("concept_id")
                if not concept_id:
                    skipped_count += 1
                    continue

                # Check if concept already exists
                existing = ConceptsRepository.find_one({"concept_id": concept_id})
                previous_relationships = (existing or {}).get("relationships") or {}

                # Normalize the node format
                unified_node = normalize_node_to_unified_format(node)
                unified_node.pop("_id", None)

                if existing:
                    # Update existing concept
                    ConceptsRepository.update_one(
                        {"concept_id": concept_id}, {"$set": unified_node}
                    )
                    updated_count += 1
                else:
                    # Insert new concept
                    ConceptsRepository.insert_one(unified_node)
                    imported_count += 1

                description_text = resolve_description_text_for_import(node)
                if description_text:
                    _ensure_import_description_relation(concept_id, description_text)

                # JVNAUTOSCI-316: Automatically register vonID and GUID as names
                try:
                    from ..services.text_value_service import upsert_text_for_concept

                    # Register vonID
                    upsert_text_for_concept(
                        subject_concept_id=concept_id,
                        predicate="hasName",
                        text=concept_id,
                        lang="en-NZ",
                        context={"name_type": "CODE"},
                    )

                    # Register GUID
                    guid = unified_node.get("_id") or (
                        existing.get("_id") if existing else None
                    )
                    if guid:
                        upsert_text_for_concept(
                            subject_concept_id=concept_id,
                            predicate="hasName",
                            text=str(guid),
                            lang="en-NZ",
                            context={"name_type": "CODE"},
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to register auto-names for imported concept {concept_id}: {e}"
                    )

                reconciliation_plan.append(
                    (
                        concept_id,
                        unified_node.get("relationships", {}),
                        previous_relationships,
                    )
                )

                if progress_callback and i % 5 == 0:
                    progress_callback(
                        {
                            "stage": "importing",
                            "processed": i,
                            "total": len(processed_nodes),
                            "imported": imported_count,
                            "updated": updated_count,
                        }
                    )

            except Exception as e:
                logger.warning(
                    f"Failed to import node {node.get('concept_id', 'unknown')}: {e}"
                )
                skipped_count += 1

        if progress_callback:
            progress_callback(
                {
                    "stage": "completed",
                    "processed": len(processed_nodes),
                    "total": len(processed_nodes),
                    "imported": imported_count,
                    "updated": updated_count,
                    "skipped": skipped_count,
                }
            )

        logger.info(
            f"[import_ontology_nodes] Completed: imported={imported_count}, updated={updated_count}, skipped={skipped_count}"
        )

        # Second pass to enforce relationship invariants once all nodes exist
        for concept_id, relationships, previous_relationships in reconciliation_plan:
            try:
                ConceptsRepository.reconcile_relationships(
                    concept_id,
                    relationships,
                    previous_relationships=previous_relationships,
                )
            except Exception as reconcile_err:
                logger.warning(
                    "[import_ontology_nodes] Relationship reconciliation best-effort failure for %s: %s",
                    concept_id,
                    reconcile_err,
                )

        return {
            "success": True,
            "imported_count": imported_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "converted_from_opencyc": converted_from_opencyc,
            "cycle_breaking": cycle_breaking,
        }

    except Exception as e:
        logger.error(f"Error in import_ontology_nodes: {e}")
        return {
            "success": False,
            "error": str(e),
            "imported_count": 0,
            "updated_count": 0,
            "skipped_count": 0,
            "converted_from_opencyc": False,
            "cycle_breaking": {
                "had_cycles": False,
                "original_cycle_count": 0,
                "removed_edge_count": 0,
                "removed_edges": [],
                "unresolved_cycle_count": 0,
            },
        }


def add_upward_closure_nodes(node_path: str) -> None:
    """
    Add upward closure nodes for transitive relationships.

    This function recomputes the inherited_salient_binary_predicates for the given node_path
    and its descendants, similar to the logic in recompute_inherited_salient.py but for a specific subtree.

    Args:
        node_path: The concept_id of the node to start recomputation from
    """
    try:
        from ..db.repositories.concepts_repository import ConceptsRepository
        from ..server.routes.vontology_routes import _recompute_inherited_for_subtree

        # Get the Flask app context to access the repository
        from flask import current_app

        repo = current_app.config.get("concepts_repo") or ConceptsRepository

        # Recompute inherited salient predicates for the subtree starting from node_path
        # This will update the inherited_salient_binary_predicates field for the node and its descendants
        summary = _recompute_inherited_for_subtree(
            repo, node_path, limit=None, dry_run=False
        )

        logger.info(f"Recomputed upward closure for {node_path}: {summary}")

    except Exception as e:
        logger.error(f"Error adding upward closure nodes for {node_path}: {e}")
        raise


def invalidate_vontology_caches(
    affected_concepts: list[str], correlation_id: str
) -> None:
    """
    Invalidate in-memory caches that may be affected by changes to the given concepts.

    This clears the tree cache, instance counts cache, and salient cache to ensure
    fresh data is loaded on next access.

    Args:
        affected_concepts: List of concept_ids that were affected by the change
        correlation_id: Correlation ID for logging/tracing
    """
    try:
        # Import the cache clearing functions from vontology_routes
        # We need to access the global cache variables defined there
        import sys
        import os

        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "server", "routes")
        )

        # Clear tree cache
        try:
            from ..server.routes.vontology_routes import _TREE_CACHE

            _TREE_CACHE.clear()
            logger.info(f"Cleared tree cache for correlation_id {correlation_id}")
        except (ImportError, NameError):
            logger.debug("Tree cache not available for clearing")

        # Clear instance counts cache
        try:
            from ..server.routes.vontology_routes import _INSTANCE_COUNTS_CACHE

            _INSTANCE_COUNTS_CACHE.clear()
            logger.info(
                f"Cleared instance counts cache for correlation_id {correlation_id}"
            )
        except (ImportError, NameError):
            logger.debug("Instance counts cache not available for clearing")

        # Clear salient cache
        try:
            from ..server.routes.vontology_routes import _SALIENT_CACHE

            _SALIENT_CACHE.clear()
            logger.info(f"Cleared salient cache for correlation_id {correlation_id}")
        except (ImportError, NameError):
            logger.debug("Salient cache not available for clearing")

        # Invalidate JVNAUTOSCI-959 concept stats cache (versioned stale marker).
        try:
            from ..services.vontology_concept_stats_service import (
                invalidate_vontology_concept_stats_cache,
            )

            invalidate_vontology_concept_stats_cache(
                reason=f"vontology_cache_invalidation:{correlation_id}",
                affected_concepts=affected_concepts,
            )
        except Exception:
            logger.debug("Concept stats cache not available for invalidation")

        logger.info(
            f"Invalidated vontology caches for {len(affected_concepts)} affected concepts, correlation_id: {correlation_id}"
        )

    except Exception as e:
        logger.error(f"Error invalidating vontology caches: {e}")
        raise
