"""Unified concept search service.

Provides flexible concept discovery for MCP gateway, UI routes, and concept tabs.
Consolidates 4 previous search implementations into single well-tested service.

Current Features:
- Exact matching by concept_id
- Substring and prefix matching on names, descriptions, and concept_id
- Two-pass search strategy (prefix → substring fallback)
- Similarity matching with configurable threshold (Phase 2 ✅)
- Hierarchical path inclusion with multiple inheritance support (Phase 3 ✅)
- Kind filtering (type, individual, predicate)
- Scope filtering (restrict to subtree)
- Instance filtering (instances of type with descendants)
- Description search (canonical text relations plus compatibility fallbacks)
- Tag filtering (system_tags, user_tags)
- Pagination support (page/per_page)
- Result ranking by relevance
- Dual schema search (legacy + text_relations)

Future phases:
- Embedding-based semantic search (Phase 4)

See: docs/engineering/concept_search_analysis.md for architecture details.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from bson import ObjectId

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from .text_value_service import get_preferred_texts_for_concepts
from ..vontology.utils_vontology import (
    get_vontology_node_and_descendant_ids,
    get_concept_display_name_with_names_fallback,
    get_concept_hierarchical_paths,
    is_type,
    is_predicate,
)

logger = logging.getLogger(__name__)

SEARCH_RESULT_PROJECTION: Dict[str, int] = {
    "concept_id": 1,
    "names": 1,
    "name": 1,
    "description": 1,
    "relationships": 1,
    "attributes": 1,
    "system_tags": 1,
    "user_tags": 1,
}
DESCRIPTION_TEXT_PREDICATE_PRECEDENCE = (
    ("hasDescription", "#V#hasDescription"),
    ("hasContent", "#V#hasContent"),
)


class ConceptSearchError(Exception):
    """Base exception for concept search errors."""

    pass


class InvalidSearchParameters(ConceptSearchError):
    """Raised when search parameters are invalid."""

    pass


def _determine_concept_kind(concept_doc: Dict[str, Any]) -> str:
    """Determine if a concept is a type, predicate, or individual.

    Args:
        concept_doc: The concept document from MongoDB

    Returns:
        One of: "type", "predicate", "individual"
    """
    try:
        # Check predicate FIRST: predicates can have is_a_type_of relationships
        # (e.g., a predicate subtype), which would incorrectly match is_type().
        if is_predicate(concept_doc):
            return "predicate"
        elif is_type(concept_doc):
            return "type"
        else:
            return "individual"
    except Exception as e:
        logger.warning(
            f"Error determining concept kind: {e}, defaulting to 'individual'"
        )
        return "individual"


def _build_name_query(
    query: str,
    exact: bool = False,
    prefix: bool = False,
    include_description: bool = False,
) -> Dict[str, Any]:
    """Build MongoDB query for name/description matching.

    Searches convenience fields already present on concept documents.
    Canonical text-relations search is layered on separately via
    ``_search_text_relations()``.

    Args:
        query: The search string
        exact: If True, require exact match
        prefix: If True, match query at start of field (prefix match)
        include_description: If True, search in description fields too

    Returns:
        MongoDB query dict for $or condition
    """
    if exact:
        # Exact match on concept_id or any name
        base_or = [
            {"concept_id": query},
            {"names.name": query},
            {"name": query},  # Legacy field
        ]
    elif prefix:
        # Prefix match (starts with query)
        regex = {"$regex": f"^{re.escape(query)}", "$options": "i"}
        base_or = [
            {"concept_id": regex},
            {"names.name": regex},
            {"name": regex},  # Legacy field
        ]
    else:
        # Substring match (contains query anywhere)
        regex = {"$regex": re.escape(query), "$options": "i"}
        base_or = [
            {"concept_id": regex},
            {"names.name": regex},
            {"name": regex},  # Legacy field
        ]

    # Add description fields if requested
    if include_description:
        regex = {"$regex": re.escape(query), "$options": "i"}
        base_or.extend(
            [  # type: ignore[arg-type]
                {"description": regex},
                {"attributes.description": regex},
                {"names.text": regex},  # Legacy field in names array
            ]
        )

    return {"$or": base_or}


def _extract_loaded_description_fallback(concept_doc: Dict[str, Any]) -> Optional[str]:
    """Return non-relation description text already present on a loaded document."""
    description = concept_doc.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()

    attributes = concept_doc.get("attributes")
    if isinstance(attributes, dict):
        attribute_description = attributes.get("description")
        if (
            isinstance(attribute_description, str)
            and attribute_description.strip()
        ):
            return attribute_description.strip()

    names = concept_doc.get("names")
    if isinstance(names, list):
        for name_obj in names:
            if not isinstance(name_obj, dict):
                continue
            text = name_obj.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

    return None


def _build_preferred_description_lookup(
    concept_docs: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Resolve canonical description text for a batch of concept docs."""
    description_lookup: Dict[str, str] = {}
    concept_ids = [
        concept_id
        for concept_doc in concept_docs
        if isinstance(concept_doc, dict)
        for concept_id in [concept_doc.get("concept_id")]
        if isinstance(concept_id, str) and concept_id.strip()
    ]
    if concept_ids:
        try:
            preferred_rows = get_preferred_texts_for_concepts(
                concept_ids,
                predicate_precedence=DESCRIPTION_TEXT_PREDICATE_PRECEDENCE,
                preferred_languages=("en-NZ", "en"),
                limit_per_concept=20,
            )
            for concept_id, row in preferred_rows.items():
                text = row.get("text")
                if isinstance(text, str) and text.strip():
                    description_lookup[concept_id] = text.strip()
        except Exception as exc:
            logger.warning(
                "Failed to fetch preferred concept descriptions: %s",
                exc,
                exc_info=True,
            )

    for concept_doc in concept_docs:
        concept_id = concept_doc.get("concept_id")
        if (
            not isinstance(concept_id, str)
            or not concept_id.strip()
            or concept_id in description_lookup
        ):
            continue
        fallback_description = _extract_loaded_description_fallback(concept_doc)
        if fallback_description:
            description_lookup[concept_id] = fallback_description

    return description_lookup


def _normalize_text_for_match(text: str) -> str:
    """Normalise text for case/whitespace-insensitive matching.

    Mirrors text_value_service._normalize_text behaviour so fingerprint
    matching and manual scan use consistent normalisation rules.
    """
    cleaned = text.strip()
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[\t\f\v ]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.casefold()


def _find_text_values_by_fingerprint(
    normalized_query: str, limit: int = 500
) -> list[dict]:
    coll = TextValuesRepository.collection()
    if coll is None:
        return []
    try:
        langs = [
            lang
            for lang in coll.distinct("lang")
            if isinstance(lang, str) and lang.strip()
        ]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to fetch distinct text_value langs: %s", exc)
        langs = []

    if not langs:
        langs = ["en"]

    fingerprints = [f"{normalized_query}||{lang.lower()}" for lang in langs]
    if not fingerprints:
        return []

    return list(
        TextValuesRepository.find({"fingerprint": {"$in": fingerprints}}, limit=limit)
    )


def _scan_text_values_for_match(
    normalized_query: str,
    predicates: list[str],
    *,
    exact: bool,
    prefix: bool,
    limit: int = 5000,
) -> list[dict]:
    if not normalized_query:
        return []

    relations = list(
        TextRelationsRepository.find(
            {"predicate": {"$in": predicates}},
            projection={"object_text_id": 1},
            limit=limit,
        )
    )

    if not relations:
        return []

    candidate_ids: list[object] = []
    for rel in relations:
        obj_id = rel.get("object_text_id")
        if not obj_id:
            continue
        candidate_ids.append(obj_id)
        if isinstance(obj_id, str) and ObjectId.is_valid(obj_id):
            candidate_ids.append(ObjectId(obj_id))

    if not candidate_ids:
        return []

    text_values = list(
        TextValuesRepository.find({"_id": {"$in": candidate_ids}}, limit=limit)
    )

    matches: list[dict] = []
    for tv in text_values:
        raw_text = tv.get("text")
        if not isinstance(raw_text, str):
            continue
        normalized_text = _normalize_text_for_match(raw_text)
        if exact:
            is_match = normalized_text == normalized_query
        elif prefix:
            is_match = normalized_text.startswith(normalized_query)
        else:
            is_match = normalized_query in normalized_text
        if is_match:
            matches.append(tv)

    return matches


def _search_text_relations(
    query: str,
    exact: bool = False,
    prefix: bool = False,
    include_description: bool = False,
) -> set[str]:
    """Search modern text_relations schema for matching concept IDs.

    Queries text_values collection for matching text, then joins with
    text_relations to find associated concept IDs. Searches hasName
    and optionally hasDescription predicates.

    Args:
        query: The search string
        exact: If True, require exact match
        prefix: If True, match query at start of text (prefix match)
        include_description: If True, search hasDescription predicate too

    Returns:
        Set of concept_ids that have matching text relations

    Example:
        >>> _search_text_relations("university", include_description=True)
        {'#V#university', '#V#university_of_melbourne', ...}
    """
    # Normalize whitespace in query (collapse multiple spaces to single space)
    collapsed_query = re.sub(r"\s+", " ", query.strip())
    normalized_query = _normalize_text_for_match(query)

    # Build predicates list FIRST - only search text_values linked via these predicates
    predicates = ["hasName"]
    if include_description:
        predicates.extend(["hasDescription", "hasContent"])

    # Strategy: search text_values directly, then join to text_relations by predicate.
    matching_texts: list[dict] = []
    if exact or prefix:
        flexible_pattern = re.escape(collapsed_query).replace(" ", r"\s+")
        if exact:
            text_query = {"text": {"$regex": f"^{flexible_pattern}$", "$options": "i"}}
        else:
            text_query = {"text": {"$regex": f"^{flexible_pattern}", "$options": "i"}}
        matching_texts = list(TextValuesRepository.find(text_query, limit=500))
    else:
        flexible_exact_pattern = re.escape(collapsed_query).replace(" ", r"\s+")
        exact_query = {
            "text": {"$regex": f"^{flexible_exact_pattern}$", "$options": "i"}
        }
        matching_texts = list(TextValuesRepository.find(exact_query, limit=500))

        if not matching_texts:
            try:
                text_search_query = {"$text": {"$search": collapsed_query}}
                matching_texts = list(
                    TextValuesRepository.find(text_search_query, limit=500)
                )
            except Exception as e:
                logger.debug(f"Text search failed: {e}")
                substring_query = {
                    "text": {"$regex": re.escape(collapsed_query), "$options": "i"}
                }
                matching_texts = list(
                    TextValuesRepository.find(substring_query, limit=500)
                )

    if not matching_texts:
        if exact:
            matching_texts = _find_text_values_by_fingerprint(normalized_query)
        if not matching_texts:
            matching_texts = _scan_text_values_for_match(
                normalized_query,
                predicates,
                exact=exact,
                prefix=prefix,
            )

    if not matching_texts:
        return set()

    # Step 1: Extract text_value IDs from matching texts
    text_value_ids = []
    for tv in matching_texts:
        raw_id = tv.get("_id")
        if raw_id is None:
            continue
        text_value_ids.append(raw_id)
        text_value_ids.append(str(raw_id))

    # Step 2: Find text_relations linking these text_values to concepts
    relations = list(
        TextRelationsRepository.find(
            {
                "object_text_id": {"$in": text_value_ids},
                "predicate": {"$in": predicates},
            },
            limit=1000,
        )
    )

    # Extract unique concept IDs
    concept_ids = {
        rel["subject_concept_id"] for rel in relations if rel.get("subject_concept_id")
    }

    logger.info(
        f"[text_relations_search] query='{query}' exact={exact} prefix={prefix} "
        f"include_desc={include_description} found {len(matching_texts)} text_values, "
        f"{len(relations)} text_relations, {len(concept_ids)} unique concepts | "
        f"text_value_ids={text_value_ids[:5]} ..."
    )

    return concept_ids


def _compute_similarity(query: str, target: str) -> float:
    """Compute string similarity score between query and target.

    Uses SequenceMatcher (Ratcliff-Obershelp algorithm) for simple,
    dependency-free similarity scoring.

    Args:
        query: The search string
        target: The target string to compare against

    Returns:
        Similarity score between 0.0 (no match) and 1.0 (perfect match)

    Example:
        >>> _compute_similarity("university", "University")
        1.0
        >>> _compute_similarity("univercity", "university")
        0.9
        >>> _compute_similarity("person", "personal")
        0.75
    """
    return SequenceMatcher(None, query.lower(), target.lower()).ratio()


def _similarity_match(
    query: str,
    candidates: List[Dict[str, Any]],
    min_similarity: float = 0.6,
    include_description: bool = False,
    description_lookup: Optional[Dict[str, str]] = None,
) -> List[tuple[str, float, Dict[str, Any]]]:
    """Find concepts with similarity score above threshold.

    Args:
        query: Search string
        candidates: List of concept documents to score
        min_similarity: Minimum similarity threshold (0.0-1.0)
        include_description: If True, also score against descriptions
        description_lookup: Optional canonical description text by concept_id

    Returns:
        List of tuples: (concept_id, best_score, concept_doc)
        Sorted by score descending
    """
    scored = []

    for concept_doc in candidates:
        concept_id = concept_doc.get("concept_id", "")
        best_score = 0.0

        # Check concept_id
        if concept_id:
            score = _compute_similarity(query, concept_id)
            best_score = max(best_score, score)

        # Check all names
        names_list = concept_doc.get("names", [])
        for name_obj in names_list:
            name = name_obj.get("name", "")
            if name:
                score = _compute_similarity(query, name)
                best_score = max(best_score, score)

        # Check legacy name field
        legacy_name = concept_doc.get("name")
        if legacy_name:
            score = _compute_similarity(query, legacy_name)
            best_score = max(best_score, score)

        # Check description if requested
        if include_description:
            description = (
                description_lookup.get(concept_id)
                if description_lookup is not None
                else None
            ) or _extract_loaded_description_fallback(concept_doc)
            if description:
                score = _compute_similarity(query, description)
                best_score = max(best_score, score)

            # Check legacy text field in names
            for name_obj in names_list:
                text = name_obj.get("text", "")
                if text:
                    score = _compute_similarity(query, text)
                    best_score = max(best_score, score)

        # Include if above threshold
        if best_score >= min_similarity:
            scored.append((concept_id, best_score, concept_doc))

    # Sort by score descending, then by name
    scored.sort(key=lambda x: (-x[1], x[0].lower()))
    return scored


def _semantic_search(
    query: str,
    limit: int = 50,
    filter_kind: Optional[List[str]] = None,
    instance_of: Optional[str] = None,
) -> List[tuple]:
    """Perform semantic (embedding-based) search on concepts.

    Uses LlamaIndex vector store with pre-computed concept embeddings.

    Args:
        query: Natural language search query
        limit: Maximum results to return
        filter_kind: Optional filter by kind (type, predicate, individual)
        instance_of: Optional filter by instance_of relationship

    Returns:
        List of (concept_id, score, concept_doc) tuples sorted by relevance
    """
    results: List[tuple] = []

    try:
        from .rag_backends.llamaindex_backend import LlamaIndexRAGService
        from .concept_embedding_service import CONCEPT_EMBEDDING_NAMESPACE

        rag_service = LlamaIndexRAGService()

        # Query the concept vector index
        rag_results = rag_service.query(
            query_text=query,
            top_k=limit * 2,  # Fetch extra for post-filtering
            namespace=CONCEPT_EMBEDDING_NAMESPACE,
        )

        if not rag_results:
            logger.info("[semantic_search] No results from RAG query")
            return results

        # Extract concept IDs from results
        concept_ids = []
        score_map = {}
        for result in rag_results:
            metadata = result.get("metadata", {})
            concept_id = metadata.get("concept_id")
            score = result.get("score", 0.0)
            if concept_id:
                concept_ids.append(concept_id)
                score_map[concept_id] = score

        if not concept_ids:
            return results

        # Fetch full concept documents
        concepts_cursor = ConceptsRepository.find(
            {"concept_id": {"$in": concept_ids}},
            projection=SEARCH_RESULT_PROJECTION,
        )

        # Post-filter and build results
        for concept_doc in concepts_cursor:
            concept_id = concept_doc.get("concept_id")
            if not concept_id:
                continue

            # Apply kind filter
            if filter_kind:
                kind = _determine_concept_kind(concept_doc)
                if kind not in filter_kind:
                    continue

            # Apply instance_of filter
            if instance_of:
                relationships = concept_doc.get("relationships", {})
                instance_of_list = relationships.get("is_an_instance_of", [])
                if isinstance(instance_of_list, str):
                    instance_of_list = [instance_of_list]

                # Check if this concept is an instance of the target type
                # (or any subtype - for full recursive filtering would need descendant IDs)
                if instance_of not in instance_of_list:
                    continue

            score = score_map.get(concept_id, 0.0)
            results.append((concept_id, score, concept_doc))

        # Sort by score descending
        results.sort(key=lambda x: -x[1])

        # Limit results
        results = results[:limit]

    except ImportError as e:
        logger.warning(f"[semantic_search] LlamaIndex not available: {e}")
    except Exception as e:
        logger.error(f"[semantic_search] Error: {e}", exc_info=True)

    return results


def search_concepts(
    query: str = "",
    filter_kind: Optional[List[str]] = None,
    scope_root: Optional[str] = None,
    instance_of: Optional[str] = None,
    direct_instances_only: bool = False,
    match_type: str = "substring",
    exact_match: bool = False,
    min_similarity: float = 0.6,
    include_description: bool = False,
    system_tags: Optional[List[str]] = None,
    user_tags: Optional[List[str]] = None,
    limit: int = 50,
    page: int = 1,
    per_page: Optional[int] = None,
    use_two_pass: bool = False,
    include_hierarchy_path: bool = False,
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """Search for concepts with flexible matching and filtering.

    Unified implementation for MCP gateway, UI routes, and concept tabs.

    Args:
        query: Search string (concept_id or partial name)
        filter_kind: Optional list of kinds to include ["type", "individual", "predicate"]
        scope_root: Optional concept_id to restrict search to subtree (e.g., "#V#person")
        instance_of: Optional concept_id to filter instances of type (RECURSIVE by default: includes descendants/subtypes)
        direct_instances_only: If True, only return direct instances of instance_of (not instances of subtypes)
        match_type: Matching strategy: "exact", "substring", "similarity", or "all" (default: "substring")
        exact_match: DEPRECATED - use match_type="exact" instead
        min_similarity: Minimum similarity score for similarity matching (0.0-1.0, default: 0.6)
        include_description: If True, search in description fields too
        system_tags: Optional list of system tags (all must match)
        user_tags: Optional list of user tags (all must match)
        limit: Maximum number of results to return (default: 50, max: 200)
        page: Page number for pagination (default: 1)
        per_page: Results per page (overrides limit if provided)
        use_two_pass: If True, try prefix match first, fallback to substring
        include_hierarchy_path: If True, include hierarchical paths in results (Phase 3)

    Returns:
        Dict with:
            - results: List of matching concept dicts (concept_id, name, kind, score)
            - total_count: Total number of matches before pagination
            - query_info: Information about the search performed

    Raises:
        InvalidSearchParameters: If parameters are invalid
        ConceptSearchError: If search fails

    Examples:
        Substring match:
        >>> search_concepts("person", filter_kind=["type"], limit=10)
        {"results": [{"concept_id": "#V#person", "name": "Person", ...}], ...}

        Similarity match (fuzzy):
        >>> search_concepts("univercity", match_type="similarity", min_similarity=0.7)
        {"results": [{"concept_id": "#V#university", "similarity_score": 0.9, ...}], ...}
    """
    # Validate parameters
    # Allow empty query when using instance_of filter (matches all instances)
    if query is None or not isinstance(query, str):
        raise InvalidSearchParameters(
            "Query must be a string (use empty string '' to match all)"
        )

    # Empty query is valid when using filters like instance_of
    if not query and not instance_of:
        raise InvalidSearchParameters(
            "Query cannot be empty unless using instance_of filter"
        )

    # Handle backward compatibility for exact_match parameter
    if exact_match:
        match_type = "exact"

    # Validate match_type
    valid_match_types = {"exact", "substring", "similarity", "semantic", "all"}
    if match_type not in valid_match_types:
        raise InvalidSearchParameters(
            f"Invalid match_type: {match_type}. Must be one of: {valid_match_types}"
        )

    # Validate min_similarity
    if not (0.0 <= min_similarity <= 1.0):
        raise InvalidSearchParameters(
            f"min_similarity must be between 0.0 and 1.0, got {min_similarity}"
        )

    # Handle pagination vs limit
    if per_page is not None:
        limit = per_page

    if limit <= 0:
        raise InvalidSearchParameters("Limit must be positive")

    if limit > 200:
        logger.warning(f"Requested limit {limit} exceeds maximum 200, capping to 200")
        limit = 200

    if page < 1:
        raise InvalidSearchParameters("Page must be >= 1")

    valid_kinds = {"type", "individual", "predicate"}
    if filter_kind:
        invalid_kinds = set(filter_kind) - valid_kinds
        if invalid_kinds:
            raise InvalidSearchParameters(
                f"Invalid kind(s): {invalid_kinds}. Must be one of: {valid_kinds}"
            )

    logger.info(
        f"[concept_search] query='{query}' match_type={match_type} min_sim={min_similarity} "
        f"use_two_pass={use_two_pass} filter_kind={filter_kind} scope_root={scope_root} "
        f"instance_of={instance_of} include_desc={include_description} page={page} limit={limit}"
    )

    try:
        # Build base MongoDB query
        base_query = {}

        # Add instance_of filter if specified
        # By default includes descendants (subtypes), unless direct_instances_only=True
        if instance_of:
            if direct_instances_only:
                # Only direct instances of the specified type
                base_query["relationships.is_an_instance_of"] = instance_of
            else:
                # Include instances of subtypes (recursive)
                descendant_ids = get_vontology_node_and_descendant_ids(instance_of)
                if descendant_ids:
                    base_query["relationships.is_an_instance_of"] = {
                        "$in": descendant_ids
                    }
                else:
                    # Fallback to direct instances only
                    base_query["relationships.is_an_instance_of"] = instance_of

        # Add tag filters
        if system_tags:
            base_query["system_tags"] = {"$all": system_tags}
        if user_tags:
            base_query["user_tags"] = {"$all": user_tags}

        # Two-pass search strategy: prefix first, then substring fallback
        results = []
        seen_ids = set()
        duplicates_encountered = 0  # JVNAUTOSCI-690: Track duplicates for metadata
        match_types_used = []

        # DUAL SCHEMA SUPPORT: Search modern text_relations in parallel with legacy fields
        # This ensures we find concepts regardless of migration status
        text_relations_concept_ids = set()

        # Special case: empty query with instance_of means "match all instances"
        if not query and instance_of:
            logger.info(
                f"[concept_search] Empty query with instance_of={instance_of}, fetching all instances"
            )
            # Just fetch all concepts matching base_query (which has instance_of filter)
            all_instances_cursor = ConceptsRepository.find(
                base_query,
                projection=SEARCH_RESULT_PROJECTION,
                limit=limit,
            )

            for concept_doc in all_instances_cursor:
                concept_id = concept_doc.get("concept_id")
                if concept_id and concept_id not in seen_ids:
                    results.append(concept_doc)
                    seen_ids.add(concept_id)
                elif concept_id in seen_ids:
                    duplicates_encountered += 1

            match_types_used.append("instance_filter")

        # Only search text_relations for non-similarity/non-semantic matching
        elif match_type not in ("similarity", "semantic"):
            try:
                use_exact = match_type == "exact"
                use_prefix = (
                    use_two_pass
                    and not re.search(r"\W", query)
                    and match_type != "exact"
                )

                text_relations_concept_ids = _search_text_relations(
                    query,
                    exact=use_exact,
                    prefix=use_prefix,
                    include_description=include_description,
                )

                if text_relations_concept_ids:
                    logger.info(
                        f"[concept_search] Found {len(text_relations_concept_ids)} concepts "
                        f"via text_relations (modern schema)"
                    )
            except Exception as e:
                logger.warning(
                    f"Text relations search failed (non-fatal): {e}", exc_info=True
                )

        # For similarity matching, fetch broader candidate set
        if match_type == "similarity" or match_type == "all":
            # Get all concepts (or filtered by base_query)
            # We'll score them all with similarity algorithm
            candidates_cursor = ConceptsRepository.find(
                base_query,
                projection=SEARCH_RESULT_PROJECTION,
                limit=1000,  # Broader fetch for similarity scoring
            )

            candidates = list(candidates_cursor)
            description_lookup = (
                _build_preferred_description_lookup(candidates)
                if include_description
                else None
            )
            similarity_results = _similarity_match(
                query,
                candidates,
                min_similarity,
                include_description,
                description_lookup,
            )

            for concept_id, score, concept_doc in similarity_results:
                if concept_id not in seen_ids:
                    concept_doc["_similarity_score"] = score
                    results.append(concept_doc)
                    seen_ids.add(concept_id)
                else:
                    duplicates_encountered += 1

            if similarity_results:
                match_types_used.append("similarity")

            # If match_type is "all", also do substring search and merge
            if match_type == "all":
                substring_query = _build_name_query(
                    query, prefix=False, include_description=include_description
                )
                combined_query = (
                    {**base_query, **substring_query} if base_query else substring_query
                )

                if seen_ids:
                    combined_query["concept_id"] = {"$nin": list(seen_ids)}

                substring_cursor = ConceptsRepository.find(
                    combined_query,
                    projection=SEARCH_RESULT_PROJECTION,
                    limit=limit,
                )

                substring_added = False
                for concept_doc in substring_cursor:
                    concept_id = concept_doc.get("concept_id")
                    if concept_id and concept_id not in seen_ids:
                        results.append(concept_doc)
                        seen_ids.add(concept_id)
                        substring_added = True
                    elif concept_id in seen_ids:
                        duplicates_encountered += 1

                if substring_added:
                    match_types_used.append("substring")

        elif match_type == "semantic":
            # Semantic embedding-based search via LlamaIndex
            semantic_results = _semantic_search(
                query=query,
                filter_kind=filter_kind,
                instance_of=instance_of,
                limit=limit,
            )

            for concept_id, score, concept_doc in semantic_results:
                if concept_id and concept_id not in seen_ids:
                    concept_doc["_similarity_score"] = score
                    results.append(concept_doc)
                    seen_ids.add(concept_id)
                elif concept_id in seen_ids:
                    duplicates_encountered += 1

            if semantic_results:
                match_types_used.append("semantic")

        elif use_two_pass and match_type != "exact":
            # Check if query contains non-word chars (skip prefix if so)
            has_special_chars = bool(re.search(r"\W", query))

            if not has_special_chars:
                # Pass 1: Prefix match (fast, high-quality results)
                prefix_query = _build_name_query(
                    query, prefix=True, include_description=include_description
                )
                combined_query = (
                    {**base_query, **prefix_query} if base_query else prefix_query
                )

                prefix_cursor = ConceptsRepository.find(
                    combined_query,
                    projection=SEARCH_RESULT_PROJECTION,
                    limit=limit * 2,
                )

                for concept_doc in prefix_cursor:
                    concept_id = concept_doc.get("concept_id")
                    if concept_id and concept_id not in seen_ids:
                        results.append(concept_doc)
                        seen_ids.add(concept_id)
                    elif concept_id in seen_ids:
                        duplicates_encountered += 1

                # Pass 2: Substring fallback if few results
                fallback_threshold = max(3, limit // 2)
                if len(results) < fallback_threshold:
                    remaining = limit * 2 - len(results)
                    substring_query = _build_name_query(
                        query, prefix=False, include_description=include_description
                    )
                    combined_query = (
                        {**base_query, **substring_query}
                        if base_query
                        else substring_query
                    )

                    # Exclude already found IDs
                    if seen_ids:
                        combined_query["concept_id"] = {"$nin": list(seen_ids)}

                    substring_cursor = ConceptsRepository.find(
                        combined_query,
                        projection=SEARCH_RESULT_PROJECTION,
                        limit=remaining,
                    )

                    for concept_doc in substring_cursor:
                        concept_id = concept_doc.get("concept_id")
                        if concept_id and concept_id not in seen_ids:
                            results.append(concept_doc)
                            seen_ids.add(concept_id)
                        elif concept_id in seen_ids:
                            duplicates_encountered += 1
            else:
                # Special chars detected, skip directly to substring
                substring_query = _build_name_query(
                    query, prefix=False, include_description=include_description
                )
                combined_query = (
                    {**base_query, **substring_query} if base_query else substring_query
                )

                substring_cursor = ConceptsRepository.find(
                    combined_query,
                    projection=SEARCH_RESULT_PROJECTION,
                    limit=limit * 2,
                )

                for concept_doc in substring_cursor:
                    concept_id = concept_doc.get("concept_id")
                    if concept_id not in seen_ids:
                        results.append(concept_doc)
                        seen_ids.add(concept_id)
                    else:
                        duplicates_encountered += 1

        else:
            # Single-pass search (exact or substring)
            use_exact = match_type == "exact"
            name_query = _build_name_query(
                query, exact=use_exact, include_description=include_description
            )
            combined_query = {**base_query, **name_query} if base_query else name_query

            concepts_cursor = ConceptsRepository.find(
                combined_query,
                projection=SEARCH_RESULT_PROJECTION,
                limit=limit * 2,
            )

            for concept_doc in concepts_cursor:
                results.append(concept_doc)

            match_types_used.append(match_type)

        # MERGE TEXT_RELATIONS RESULTS (modern schema)
        # Add any concepts found via text_relations that weren't found via legacy fields
        if text_relations_concept_ids:
            # Get concept IDs already found via legacy search
            legacy_concept_ids = {doc.get("concept_id") for doc in results}

            # Find concepts that are only in text_relations results
            additional_concept_ids = text_relations_concept_ids - legacy_concept_ids

            if additional_concept_ids:
                # Fetch full concept documents for these IDs
                additional_query = {"concept_id": {"$in": list(additional_concept_ids)}}

                # Apply base_query filters if any
                if base_query:
                    additional_query = {**base_query, **additional_query}

                additional_cursor = ConceptsRepository.find(
                    additional_query,
                    projection=SEARCH_RESULT_PROJECTION,
                    limit=limit * 2,
                )

                additional_found = 0
                additional_concepts = []
                for concept_doc in additional_cursor:
                    additional_concepts.append(concept_doc)
                    additional_found += 1

                if additional_found > 0:
                    # IMPORTANT: Prepend text_relations results to the front of the list
                    # These are high-quality matches (query matched in hasName predicate)
                    # and should be prioritized over legacy field matches
                    results = additional_concepts + results
                    logger.info(
                        f"[concept_search] Added {additional_found} concepts found only in "
                        f"modern schema (text_relations) - prepended to prioritize them"
                    )

        logger.info(f"[concept_search] Total results before filtering: {len(results)}")

        # Determine kind for each concept and apply filter_kind
        filtered_results = []
        concepts_to_check = []

        for concept_doc in results:
            concept_id = concept_doc.get("concept_id")
            kind = _determine_concept_kind(concept_doc)

            if filter_kind and kind not in filter_kind:
                continue

            concepts_to_check.append((concept_id, kind, concept_doc))

        # Apply scope_root filtering if specified
        if scope_root:
            # Get all descendants of scope_root
            scope_ids = get_vontology_node_and_descendant_ids(scope_root)

            for concept_id, kind, concept_doc in concepts_to_check:
                if concept_id in scope_ids:
                    filtered_results.append((concept_id, kind, concept_doc))
        else:
            filtered_results = concepts_to_check

        # Calculate total count before pagination
        total_count = len(filtered_results)

        # Apply pagination
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paginated_results = filtered_results[start_idx:end_idx]

        # Enrich with hierarchical paths if requested (Phase 3)
        path_data = {}
        if include_hierarchy_path and paginated_results:
            concept_ids_for_paths = [cid for cid, _, _ in paginated_results]
            try:
                path_data = get_concept_hierarchical_paths(concept_ids_for_paths)
                logger.debug(
                    f"[concept_search] Enriched {len(path_data)} concepts with hierarchy paths"
                )
            except Exception as e:
                logger.warning(f"Failed to build hierarchy paths: {e}", exc_info=True)
                path_data = {}

        # Score results by relevance (exact matches score higher)
        scored_results = []

        logger.info(
            f"[concept_search] Scoring {len(paginated_results)} paginated results for query='{query}'"
        )

        # MODERN SCHEMA: Fetch ALL names from text_relations for ALL concepts
        # This is the primary source of names; concept document names/name fields are legacy only
        concept_ids = [concept_id for concept_id, _, _ in paginated_results]
        description_lookup = (
            _build_preferred_description_lookup(
                [concept_doc for _, _, concept_doc in paginated_results]
            )
            if include_description
            else {}
        )
        text_relations_names = {}  # concept_id -> primary_name (first name)
        text_relations_all_names = {}  # concept_id -> list of (name, name_type) tuples

        if concept_ids:
            try:
                relations = list(
                    TextRelationsRepository.find(
                        {
                            "subject_concept_id": {"$in": concept_ids},
                            "predicate": "hasName",
                        },
                        limit=1000,
                    )
                )

                logger.info(
                    f"[concept_search] Found {len(relations)} hasName relations for {len(concept_ids)} concepts"
                )

                text_value_ids = [
                    rel["object_text_id"]
                    for rel in relations
                    if rel.get("object_text_id")
                ]
                logger.info(
                    f"[concept_search] Extracted {len(text_value_ids)} text_value_ids from relations"
                )

                if text_value_ids:
                    text_values = list(
                        TextValuesRepository.find(
                            {
                                "_id": {
                                    "$in": [
                                        ObjectId(tv_id)
                                        for tv_id in text_value_ids
                                        if ObjectId.is_valid(tv_id)
                                    ]
                                }
                            },
                            limit=1000,
                        )
                    )

                    # Build map of text_value_id -> text (just the text content)
                    tv_map = {}
                    for tv in text_values:
                        text = tv.get("text", "")
                        tv_map[str(tv["_id"])] = text

                    # Build maps: concept_id -> primary name AND concept_id -> all names with types
                    # Note: name_type is stored in text_relation's context field, not in text_value
                    for rel in relations:
                        cid = rel.get("subject_concept_id")
                        tv_id = rel.get("object_text_id")
                        if cid and tv_id and tv_id in tv_map:
                            name = tv_map[tv_id]
                            # Get name_type from relation's context field (singular, not plural)
                            context = rel.get("context", {})
                            name_type = (
                                context.get("name_type", "NL")
                                if isinstance(context, dict)
                                else "NL"
                            )
                            # Store all names with types
                            if cid not in text_relations_all_names:
                                text_relations_all_names[cid] = []
                            text_relations_all_names[cid].append((name, name_type))

                    # After collecting all names, select primary name with preference for NL > ABBR > CODE
                    for cid, names_list in text_relations_all_names.items():
                        # Sort by name_type priority: NL first, then ABBR, then CODE
                        def name_type_priority(item: tuple) -> tuple:
                            name, name_type = item
                            priority_map = {"NL": 0, "ABBR": 1, "CODE": 2}
                            return (priority_map.get(name_type, 3), name)

                        sorted_names = sorted(names_list, key=name_type_priority)
                        if sorted_names:
                            text_relations_names[cid] = sorted_names[0][
                                0
                            ]  # Take the best name

                logger.info(
                    f"[concept_search] Fetched names from text_relations for {len(text_relations_names)} concepts"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to fetch names from text_relations: {e}", exc_info=True
                )

        for concept_id, kind, concept_doc in paginated_results:
            try:
                # MODERN SCHEMA: Use text_relations name first, fall back to legacy fields only if not found
                if (
                    concept_id in text_relations_names
                    and text_relations_names[concept_id]
                ):
                    primary_name = text_relations_names[concept_id]
                else:
                    # Legacy fallback
                    primary_name = get_concept_display_name_with_names_fallback(
                        concept_doc
                    )
            except Exception as e:
                logger.warning(f"Error getting display name: {e}")
                primary_name = text_relations_names.get(concept_id) or concept_doc.get(
                    "concept_id", "Unknown"
                )

            # Calculate relevance score
            score = 0.0
            similarity_score = concept_doc.get("_similarity_score")
            query_lower = query.lower()
            name_lower = primary_name.lower()

            if similarity_score is not None:
                # Similarity match - use similarity score scaled to 0-100
                score = similarity_score * 100.0
            elif match_type == "exact":
                score = 100.0  # Exact match
            else:
                # Check ALL names (including abbreviations and alternate forms) for best match
                all_names_for_concept = text_relations_all_names.get(concept_id, [])
                best_match_score = 0.0
                best_match_name = primary_name
                best_match_type = "NL"  # Default to natural language

                # Check primary name first
                if name_lower == query_lower:
                    best_match_score = 100.0
                    best_match_name = primary_name
                elif name_lower.startswith(query_lower):
                    match_ratio = (
                        len(query_lower) / len(name_lower) if len(name_lower) > 0 else 0
                    )
                    best_match_score = 90.0 + (match_ratio * 5.0)
                    best_match_name = primary_name
                elif query_lower in name_lower:
                    match_ratio = (
                        len(query_lower) / len(name_lower) if len(name_lower) > 0 else 0
                    )
                    best_match_score = 70.0 + (match_ratio * 15.0)
                    best_match_name = primary_name

                # Check all alternate names (abbreviations, codes, other languages)
                for alt_name, name_type in all_names_for_concept:
                    alt_lower = alt_name.lower()
                    candidate_score = 0.0

                    if alt_lower == query_lower:
                        # Exact match on alternate name
                        candidate_score = 100.0
                    elif alt_lower.startswith(query_lower):
                        # Prefix match on alternate name
                        match_ratio = (
                            len(query_lower) / len(alt_lower)
                            if len(alt_lower) > 0
                            else 0
                        )
                        candidate_score = 90.0 + (match_ratio * 5.0)
                    elif query_lower in alt_lower:
                        # Substring match on alternate name
                        match_ratio = (
                            len(query_lower) / len(alt_lower)
                            if len(alt_lower) > 0
                            else 0
                        )
                        candidate_score = 70.0 + (match_ratio * 15.0)

                    # Apply name_type bonus: NL (natural language) gets highest score over ABBR/CODE
                    if candidate_score > 0:
                        if name_type == "NL":
                            candidate_score += 3.0  # Natural language bonus
                        elif name_type == "ABBR":
                            candidate_score += 1.0  # Small bonus for abbreviations
                        elif name_type in ("CODE", "vonGUID"):
                            # Significant penalty for technical codes and GUIDs
                            # This deprioritizes them unless they're the only match
                            candidate_score -= 10.0  # Major penalty for codes/GUIDs

                    # Update best match if this is better
                    if candidate_score > best_match_score:
                        best_match_score = candidate_score
                        best_match_name = alt_name
                        best_match_type = name_type

                score = best_match_score

                # If no good name match, check description
                if score < 60.0:
                    description = description_lookup.get(
                        concept_id
                    ) or _extract_loaded_description_fallback(concept_doc)
                    if description and query_lower in description.lower():
                        score = 60.0  # Description match
                    elif score == 0.0:
                        score = 50.0  # Other match

            result_obj = {
                "concept_id": concept_id,
                "name": primary_name,
                "kind": kind,
                "relevance_score": score,
            }

            # Include similarity_score if it was computed
            if similarity_score is not None:
                result_obj["similarity_score"] = round(similarity_score, 3)

            # Add hierarchy path data if available (Phase 3)
            if concept_id in path_data:
                hierarchy_info = path_data[concept_id]
                result_obj["hierarchy"] = {
                    "primary_path": hierarchy_info.get("primary_path"),
                    "all_parents": hierarchy_info.get("all_parents", []),
                    "depth": hierarchy_info.get("max_depth", 0),
                    "paths": hierarchy_info.get(
                        "paths", []
                    ),  # All paths for multiple inheritance
                }

            scored_results.append(result_obj)

        # Sort by relevance score descending
        scored_results.sort(key=lambda x: (-x["relevance_score"], x["name"].lower()))

        logger.info(
            f"[concept_search] Found {total_count} results, returning {len(scored_results)}"
        )

        # JVNAUTOSCI-690: Calculate deduplication stats
        total_with_duplicates = len(scored_results) + duplicates_encountered

        return {
            "results": scored_results,
            "total_count": total_count,
            "match_types_used": match_types_used if match_types_used else [match_type],
            "deduplication": {
                "deduplicated": True,
                "duplicates_removed": duplicates_encountered,
                "total_before_dedup": total_with_duplicates,
                "total_after_dedup": len(scored_results),
            },
            "query_info": {
                "query": query,
                "match_type": match_type,
                "min_similarity": (
                    min_similarity if match_type in ("similarity", "all") else None
                ),
                "use_two_pass": use_two_pass,
                "filter_kind": filter_kind,
                "scope_root": scope_root,
                "instance_of": instance_of,
                "direct_instances_only": direct_instances_only,
                "include_description": include_description,
                "include_hierarchy_path": include_hierarchy_path,
                "page": page,
                "per_page": limit,
                "has_more": (page * limit) < total_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }

    except InvalidSearchParameters:
        raise
    except Exception as e:
        logger.error(f"Error searching concepts: {e}", exc_info=True)
        raise ConceptSearchError(f"Concept search failed: {e}")
