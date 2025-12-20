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
- Description search (metadata.description, names.text)
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
from ..vontology.utils_vontology import (
    get_vontology_node_and_descendant_ids,
    get_concept_display_name_with_names_fallback,
    get_concept_hierarchical_paths,
    is_type,
    is_predicate,
)

logger = logging.getLogger(__name__)


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
        if is_type(concept_doc):
            return "type"
        elif is_predicate(concept_doc):
            return "predicate"
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

    Searches LEGACY fields (names[], metadata.description).
    For modern text_relations search, use _search_text_relations().

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
                {"metadata.description": regex},
                {"names.text": regex},  # Legacy field in names array
            ]
        )

    return {"$or": base_or}


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
    normalized_query = re.sub(r"\s+", " ", query.strip())

    # Build predicates list FIRST - only search text_values linked via these predicates
    predicates = ["hasName"]
    if include_description:
        predicates.append("hasDescription")

    # Strategy: Find text_relations by predicate FIRST, get their text_value_ids,
    # THEN search only within those specific text_values
    # This prevents finding "New Zealand" in descriptions/notes when we only want names

    # Step 1: Get ALL text_relations for the specified predicates (hasName by default)
    all_relations = list(
        TextRelationsRepository.find({"predicate": {"$in": predicates}}, limit=10000)
    )

    if not all_relations:
        logger.debug(f"No text_relations found for predicates {predicates}")
        return set()

    # Step 2: Extract the text_value_ids that are actually used as names (or descriptions if enabled)
    valid_text_value_ids = {
        rel["object_text_id"] for rel in all_relations if rel.get("object_text_id")
    }

    if not valid_text_value_ids:
        logger.debug(
            f"No text_value_ids found in relations for predicates {predicates}"
        )
        return set()

    # Step 3: Build text search query, but restrict to valid_text_value_ids
    if exact:
        text_query = {
            "_id": {
                "$in": [
                    ObjectId(tv_id)
                    for tv_id in valid_text_value_ids
                    if ObjectId.is_valid(tv_id)
                ]
            },
            "text": normalized_query,
        }
    elif prefix:
        text_query = {
            "_id": {
                "$in": [
                    ObjectId(tv_id)
                    for tv_id in valid_text_value_ids
                    if ObjectId.is_valid(tv_id)
                ]
            },
            "text": {"$regex": f"^{re.escape(normalized_query)}", "$options": "i"},
        }
    else:
        # For substring search, try exact match first, then broader text search
        # All searches restricted to valid_text_value_ids
        object_ids = [
            ObjectId(tv_id)
            for tv_id in valid_text_value_ids
            if ObjectId.is_valid(tv_id)
        ]

        # Try exact match with flexible whitespace
        flexible_exact_pattern = re.escape(normalized_query).replace(" ", r"\s+")
        exact_query = {
            "_id": {"$in": object_ids},
            "text": {"$regex": f"^{flexible_exact_pattern}$", "$options": "i"},
        }
        matching_texts = list(TextValuesRepository.find(exact_query, limit=500))

        # Fallback to text search within valid IDs only
        if not matching_texts:
            try:
                text_search_query = {
                    "_id": {"$in": object_ids},
                    "$text": {"$search": normalized_query},
                }
                matching_texts = list(
                    TextValuesRepository.find(text_search_query, limit=500)
                )
            except Exception as e:
                logger.debug(f"Text search failed: {e}")
                # Final fallback: substring anywhere in text
                substring_query = {
                    "_id": {"$in": object_ids},
                    "text": {"$regex": re.escape(normalized_query), "$options": "i"},
                }
                matching_texts = list(
                    TextValuesRepository.find(substring_query, limit=500)
                )

    # Step 4: Execute query for exact/prefix
    if exact or prefix:
        matching_texts = list(TextValuesRepository.find(text_query, limit=500))

    if not matching_texts:
        return set()

    # Step 5: Extract text_value IDs from matching texts
    text_value_ids = [str(tv["_id"]) for tv in matching_texts]

    # Step 6: Find text_relations linking these text_values to concepts
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
) -> List[tuple[str, float, Dict[str, Any]]]:
    """Find concepts with similarity score above threshold.

    Args:
        query: Search string
        candidates: List of concept documents to score
        min_similarity: Minimum similarity threshold (0.0-1.0)
        include_description: If True, also score against descriptions

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
            description = concept_doc.get("metadata", {}).get("description", "")
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


def search_concepts(
    query: str = "",
    filter_kind: Optional[List[str]] = None,
    scope_root: Optional[str] = None,
    instance_of: Optional[str] = None,
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
        instance_of: Optional concept_id to filter instances of type (RECURSIVE: includes descendants/subtypes automatically)
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
    valid_match_types = {"exact", "substring", "similarity", "all"}
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

        # Add instance_of filter if specified (includes descendants)
        if instance_of:
            descendant_ids = get_vontology_node_and_descendant_ids(instance_of)
            if descendant_ids:
                base_query["relationships.is_an_instance_of"] = {"$in": descendant_ids}
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
                projection={
                    "concept_id": 1,
                    "names": 1,
                    "name": 1,
                    "metadata.description": 1,
                    "relationships": 1,
                    "attributes": 1,
                    "system_tags": 1,
                    "user_tags": 1,
                },
                limit=limit,
            )

            for concept_doc in all_instances_cursor:
                concept_id = concept_doc.get("concept_id")
                if concept_id and concept_id not in seen_ids:
                    results.append(concept_doc)
                    seen_ids.add(concept_id)

            match_types_used.append("instance_filter")

        # Only search text_relations for non-similarity matching (similarity handles it differently)
        elif match_type != "similarity":
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
                projection={
                    "concept_id": 1,
                    "names": 1,
                    "name": 1,
                    "metadata.description": 1,
                    "relationships": 1,
                    "attributes": 1,
                    "system_tags": 1,
                    "user_tags": 1,
                },
                limit=1000,  # Broader fetch for similarity scoring
            )

            candidates = list(candidates_cursor)
            similarity_results = _similarity_match(
                query, candidates, min_similarity, include_description
            )

            for concept_id, score, concept_doc in similarity_results:
                if concept_id not in seen_ids:
                    concept_doc["_similarity_score"] = score
                    results.append(concept_doc)
                    seen_ids.add(concept_id)

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
                    projection={
                        "concept_id": 1,
                        "names": 1,
                        "name": 1,
                        "metadata.description": 1,
                        "relationships": 1,
                        "attributes": 1,
                        "system_tags": 1,
                        "user_tags": 1,
                    },
                    limit=limit,
                )

                substring_added = False
                for concept_doc in substring_cursor:
                    concept_id = concept_doc.get("concept_id")
                    if concept_id and concept_id not in seen_ids:
                        results.append(concept_doc)
                        seen_ids.add(concept_id)
                        substring_added = True

                if substring_added:
                    match_types_used.append("substring")

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
                    projection={
                        "concept_id": 1,
                        "names": 1,
                        "name": 1,
                        "metadata.description": 1,
                        "relationships": 1,
                        "attributes": 1,
                        "system_tags": 1,
                        "user_tags": 1,
                    },
                    limit=limit * 2,
                )

                for concept_doc in prefix_cursor:
                    concept_id = concept_doc.get("concept_id")
                    if concept_id and concept_id not in seen_ids:
                        results.append(concept_doc)
                        seen_ids.add(concept_id)

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
                        projection={
                            "concept_id": 1,
                            "names": 1,
                            "name": 1,
                            "metadata.description": 1,
                            "relationships": 1,
                            "attributes": 1,
                            "system_tags": 1,
                            "user_tags": 1,
                        },
                        limit=remaining,
                    )

                    for concept_doc in substring_cursor:
                        concept_id = concept_doc.get("concept_id")
                        if concept_id and concept_id not in seen_ids:
                            results.append(concept_doc)
                            seen_ids.add(concept_id)
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
                    projection={
                        "concept_id": 1,
                        "names": 1,
                        "name": 1,
                        "metadata.description": 1,
                        "relationships": 1,
                        "attributes": 1,
                        "system_tags": 1,
                        "user_tags": 1,
                    },
                    limit=limit * 2,
                )

                for concept_doc in substring_cursor:
                    concept_id = concept_doc.get("concept_id")
                    if concept_id not in seen_ids:
                        results.append(concept_doc)
                        seen_ids.add(concept_id)

        else:
            # Single-pass search (exact or substring)
            use_exact = match_type == "exact"
            name_query = _build_name_query(
                query, exact=use_exact, include_description=include_description
            )
            combined_query = {**base_query, **name_query} if base_query else name_query

            concepts_cursor = ConceptsRepository.find(
                combined_query,
                projection={
                    "concept_id": 1,
                    "names": 1,
                    "name": 1,
                    "metadata.description": 1,
                    "relationships": 1,
                    "attributes": 1,
                    "system_tags": 1,
                    "user_tags": 1,
                },
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
                    projection={
                        "concept_id": 1,
                        "names": 1,
                        "name": 1,
                        "metadata.description": 1,
                        "relationships": 1,
                        "attributes": 1,
                        "system_tags": 1,
                        "user_tags": 1,
                    },
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
                            # First name becomes primary
                            if cid not in text_relations_names:
                                text_relations_names[cid] = name
                            # Store all names with types
                            if cid not in text_relations_all_names:
                                text_relations_all_names[cid] = []
                            text_relations_all_names[cid].append((name, name_type))

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

                    # Apply name_type bonus: NL (natural language) gets slight boost over ABBR/CODE
                    if candidate_score > 0:
                        if name_type == "NL":
                            candidate_score += 2.0  # Natural language bonus
                        elif name_type == "ABBR":
                            candidate_score += 0.0  # No bonus for abbreviations
                        elif name_type == "CODE":
                            candidate_score -= 1.0  # Slight penalty for technical codes

                    # Update best match if this is better
                    if candidate_score > best_match_score:
                        best_match_score = candidate_score
                        best_match_name = alt_name
                        best_match_type = name_type

                score = best_match_score

                # If no good name match, check description
                if score < 60.0:
                    description = concept_doc.get("metadata", {}).get("description", "")
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

        return {
            "results": scored_results,
            "total_count": total_count,
            "match_types_used": match_types_used if match_types_used else [match_type],
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
