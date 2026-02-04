"""Workflow discovery service for conversation turns.

Searches for workflows represented in Vontology that are relevant to user input.
Combines RAG vector search with Vontology concept search to find applicable
workflows during conversation turns.

Use Case: During conversation turns, surface relevant workflows in the
"Thinking" context section to enable proactive workflow assistance.

Related Issues:
    - JVNAUTOSCI-1076: Workflow discovery during conversation turn
    - JVNAUTOSCI-1075: Durable workflow system
    - JVNAUTOSCI-803: LLM Workflows

Technical Notes:
    - Workflows are instances of #V#llm_workflow (or subtypes like #V#durable_workflow)
    - Uses semantic search via concept_embedding_service for RAG-based discovery
    - Uses concept_search_service for Vontology-native search
    - Relevance threshold (default 0.70) filters noise from results
    - Search should add < 500ms to turn latency
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Workflow type concepts to search for instances
WORKFLOW_TYPE_IDS = (
    "#V#llm_workflow",
    "#V#workflow",
    "#V#durable_workflow",
)

# Default relevance threshold (0.0-1.0)
DEFAULT_RELEVANCE_THRESHOLD = 0.70

# Maximum workflows to return
DEFAULT_MAX_RESULTS = 3

# Search timeout in seconds
SEARCH_TIMEOUT_SECONDS = 0.5


@dataclass
class WorkflowMatch:
    """A matched workflow from discovery search."""

    concept_id: str
    name: str
    description: Optional[str] = None
    relevance_score: float = 0.0
    match_source: str = "unknown"  # "semantic", "vontology", "combined"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialisation."""
        return {
            "concept_id": self.concept_id,
            "name": self.name,
            "description": self.description,
            "relevance_score": round(self.relevance_score, 3),
            "match_source": self.match_source,
        }


@dataclass
class WorkflowDiscoveryResult:
    """Result of workflow discovery search."""

    matches: List[WorkflowMatch] = field(default_factory=list)
    search_time_ms: float = 0.0
    query: str = ""
    threshold: float = DEFAULT_RELEVANCE_THRESHOLD
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialisation."""
        return {
            "matches": [m.to_dict() for m in self.matches],
            "search_time_ms": round(self.search_time_ms, 2),
            "query": self.query,
            "threshold": self.threshold,
            "match_count": len(self.matches),
            "errors": self.errors if self.errors else None,
        }


def _get_workflow_description(concept_doc: Dict[str, Any]) -> Optional[str]:
    """Extract description from concept document.

    Checks modern text_relations first (hasDescription), then legacy fields.
    """
    # Try legacy fields first (faster)
    metadata = concept_doc.get("metadata", {})
    if isinstance(metadata, dict):
        desc = metadata.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip()

    # Try names array text field (legacy)
    names = concept_doc.get("names", [])
    if isinstance(names, list):
        for name_obj in names:
            if isinstance(name_obj, dict):
                text = name_obj.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()

    return None


def _get_workflow_name(concept_doc: Dict[str, Any]) -> str:
    """Extract display name from concept document."""
    try:
        from ..vontology.utils_vontology import (
            get_concept_display_name_with_names_fallback,
        )

        return get_concept_display_name_with_names_fallback(concept_doc)
    except Exception:
        # Fallback: try common fields
        name = concept_doc.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()

        names = concept_doc.get("names", [])
        if isinstance(names, list) and names:
            for name_obj in names:
                if isinstance(name_obj, dict):
                    n = name_obj.get("name")
                    if isinstance(n, str) and n.strip():
                        return n.strip()

        return concept_doc.get("concept_id", "Unknown Workflow")


def _search_workflows_semantic(
    query: str,
    *,
    limit: int = DEFAULT_MAX_RESULTS * 2,
) -> List[WorkflowMatch]:
    """Search for workflows using semantic (embedding-based) search.

    Uses the concept embedding index with instance_of filter for workflow types.
    """
    try:
        from .concept_search_service import search_concepts

        # Use semantic search with instance_of filter for workflows
        for workflow_type in WORKFLOW_TYPE_IDS:
            result = search_concepts(
                query=query,
                match_type="semantic",
                instance_of=workflow_type,
                include_description=True,
                limit=limit,
            )

            matches = []
            for concept in result.get("results", []):
                score = concept.get("similarity_score", 0.0)
                if score <= 0:
                    # Fallback to relevance_score if similarity_score not present
                    score = concept.get("relevance_score", 0.0) / 100.0

                matches.append(
                    WorkflowMatch(
                        concept_id=concept.get("concept_id", ""),
                        name=concept.get("name", "Unknown"),
                        description=None,  # Will be enriched later if needed
                        relevance_score=score,
                        match_source="semantic",
                    )
                )

            if matches:
                return matches

    except Exception as e:
        logger.warning(f"Semantic workflow search failed: {e}")

    return []


def _search_workflows_vontology(
    query: str,
    *,
    limit: int = DEFAULT_MAX_RESULTS * 2,
) -> List[WorkflowMatch]:
    """Search for workflows using Vontology concept search.

    Uses substring/similarity matching on workflow instances.
    """
    try:
        from .concept_search_service import search_concepts

        # Try similarity matching first (fuzzy)
        for workflow_type in WORKFLOW_TYPE_IDS:
            result = search_concepts(
                query=query,
                match_type="similarity",
                min_similarity=0.5,  # Lower threshold, will filter later
                instance_of=workflow_type,
                include_description=True,
                limit=limit,
            )

            matches = []
            for concept in result.get("results", []):
                score = concept.get("similarity_score", 0.0)
                if score <= 0:
                    score = concept.get("relevance_score", 0.0) / 100.0

                matches.append(
                    WorkflowMatch(
                        concept_id=concept.get("concept_id", ""),
                        name=concept.get("name", "Unknown"),
                        description=None,
                        relevance_score=score,
                        match_source="vontology",
                    )
                )

            if matches:
                return matches

    except Exception as e:
        logger.warning(f"Vontology workflow search failed: {e}")

    return []


def _enrich_workflow_matches(matches: List[WorkflowMatch]) -> List[WorkflowMatch]:
    """Enrich workflow matches with descriptions from Vontology."""
    if not matches:
        return matches

    try:
        from ..db.repositories.concepts_repository import ConceptsRepository

        concept_ids = [m.concept_id for m in matches]
        concepts = list(
            ConceptsRepository.find(
                {"concept_id": {"$in": concept_ids}},
                projection={
                    "concept_id": 1,
                    "names": 1,
                    "name": 1,
                    "metadata.description": 1,
                },
            )
        )

        concept_map = {c.get("concept_id"): c for c in concepts}

        for match in matches:
            concept_doc = concept_map.get(match.concept_id)
            if concept_doc:
                if not match.description:
                    match.description = _get_workflow_description(concept_doc)
                if match.name == "Unknown":
                    match.name = _get_workflow_name(concept_doc)

    except Exception as e:
        logger.warning(f"Failed to enrich workflow matches: {e}")

    return matches


def _deduplicate_and_rank(
    matches: List[WorkflowMatch],
    *,
    threshold: float,
    max_results: int,
) -> List[WorkflowMatch]:
    """Deduplicate matches by concept_id and rank by relevance."""
    seen_ids: set[str] = set()
    unique_matches: List[WorkflowMatch] = []

    # Sort by score descending
    sorted_matches = sorted(matches, key=lambda m: -m.relevance_score)

    for match in sorted_matches:
        if match.concept_id in seen_ids:
            continue
        if match.relevance_score < threshold:
            continue
        seen_ids.add(match.concept_id)
        unique_matches.append(match)
        if len(unique_matches) >= max_results:
            break

    return unique_matches


def discover_workflows(
    query: str,
    *,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
) -> WorkflowDiscoveryResult:
    """Discover workflows relevant to user input.

    Performs combined RAG and Vontology search to find workflow concepts
    matching the user's query. Returns ranked results above the relevance
    threshold.

    Args:
        query: User input text to match against workflows
        relevance_threshold: Minimum relevance score (0.0-1.0) for inclusion
        max_results: Maximum number of workflows to return
        timeout_seconds: Maximum time to spend searching (soft limit)

    Returns:
        WorkflowDiscoveryResult with matched workflows and search metadata

    Example:
        >>> result = discover_workflows("sync RAG text relations")
        >>> for match in result.matches:
        ...     print(f"{match.name}: {match.relevance_score:.2f}")
        RAG Text Relation Sync: 0.92
    """
    if not query or not isinstance(query, str):
        return WorkflowDiscoveryResult(
            query=query or "",
            threshold=relevance_threshold,
            errors=["Invalid or empty query"],
        )

    query = query.strip()
    if not query:
        return WorkflowDiscoveryResult(
            query="",
            threshold=relevance_threshold,
            errors=["Empty query after trimming"],
        )

    start_time = time.perf_counter()
    all_matches: List[WorkflowMatch] = []
    errors: List[str] = []

    # Search both sources
    try:
        semantic_matches = _search_workflows_semantic(query, limit=max_results * 2)
        all_matches.extend(semantic_matches)
    except Exception as e:
        errors.append(f"semantic_search_error: {e}")
        logger.warning(f"Semantic workflow discovery failed: {e}")

    # Check timeout
    elapsed = time.perf_counter() - start_time
    if elapsed < timeout_seconds:
        try:
            vontology_matches = _search_workflows_vontology(
                query, limit=max_results * 2
            )
            all_matches.extend(vontology_matches)
        except Exception as e:
            errors.append(f"vontology_search_error: {e}")
            logger.warning(f"Vontology workflow discovery failed: {e}")

    # Deduplicate and rank
    ranked_matches = _deduplicate_and_rank(
        all_matches,
        threshold=relevance_threshold,
        max_results=max_results,
    )

    # Enrich with descriptions
    ranked_matches = _enrich_workflow_matches(ranked_matches)

    elapsed_ms = (time.perf_counter() - start_time) * 1000

    logger.info(
        f"[workflow_discovery] query='{query[:50]}...' "
        f"found={len(ranked_matches)} elapsed_ms={elapsed_ms:.1f}"
    )

    return WorkflowDiscoveryResult(
        matches=ranked_matches,
        search_time_ms=elapsed_ms,
        query=query,
        threshold=relevance_threshold,
        errors=errors if errors else [],
    )


def discover_workflows_for_turn(
    user_input: str,
    *,
    namespace: Optional[str] = None,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> Optional[Dict[str, Any]]:
    """Convenience wrapper for workflow discovery during conversation turns.

    Returns None if no relevant workflows found, otherwise returns a dict
    suitable for inclusion in response metadata.

    Args:
        user_input: The user's prompt text
        namespace: Optional namespace for scoped search (future use)
        relevance_threshold: Minimum relevance score
        max_results: Maximum workflows to return

    Returns:
        Dict with workflow suggestions or None if no matches
    """
    if not user_input or len(user_input.strip()) < 5:
        # Skip very short inputs
        return None

    try:
        result = discover_workflows(
            user_input,
            relevance_threshold=relevance_threshold,
            max_results=max_results,
        )

        if not result.matches:
            return None

        return result.to_dict()

    except Exception as e:
        logger.warning(f"Workflow discovery for turn failed: {e}")
        return None
