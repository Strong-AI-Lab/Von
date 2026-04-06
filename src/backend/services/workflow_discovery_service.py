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
    - Workflows are instances of #V#ai_workflow (including #V#durable_workflow)
    - Uses semantic search via concept_embedding_service for RAG-based discovery
    - Uses concept_search_service for Vontology-native search
    - Relevance threshold (default 0.70) filters noise from results
    - Search should add < 500ms to turn latency
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from ..vontology.utils_vontology import get_concept_description
from .file_copy_reference_service import extract_file_copy_concept_ids_from_text
from .file_copy_typing_service import build_file_copy_typing_context
from .arxiv_paper_link_service import extract_arxiv_id_candidates
from .workflow_capability_service import (
    get_workflow_capability_index_runtime_state,
    search_workflow_capabilities,
)

logger = logging.getLogger(__name__)

# Workflow type concepts to search for instances.
WORKFLOW_TYPE_IDS = (
    "#V#ai_workflow",
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
DISCOVERY_CAPABILITY_INDEX_MAX_WAIT_SECONDS = 0.0

# Executability reason codes (JVNAUTOSCI-1088).
EXECUTABILITY_EXECUTABLE_NOW = "executable_now"
EXECUTABILITY_GRAPH_INCOMPLETE = "graph_incomplete"
EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT = "non_executable_design_artifact"
EXECUTABILITY_DRAFT_NOT_PUBLISHED = "draft_not_published"
EXECUTABILITY_WORKFLOW_STEP_INTEGRITY = "workflow_step_integrity_issue"
EXECUTABILITY_WORKFLOW_STEP_PARTIALLY_VACUOUS = "workflow_step_partially_vacuous"
EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS = (
    "workflow_step_completely_vacuous"
)
ROUTING_EXCLUSION_MISSING_AUTHORITATIVE_PURPOSE = (
    "missing_authoritative_purpose"
)
ROUTING_EXCLUSION_EXPLICITLY_DISABLED = "routing_explicitly_disabled"

_EXECUTABILITY_REASON_PRIORITY = {
    EXECUTABILITY_EXECUTABLE_NOW: 2,
    EXECUTABILITY_GRAPH_INCOMPLETE: 1,
    EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT: 0,
    EXECUTABILITY_DRAFT_NOT_PUBLISHED: 1,
    EXECUTABILITY_WORKFLOW_STEP_INTEGRITY: 1,
    EXECUTABILITY_WORKFLOW_STEP_PARTIALLY_VACUOUS: 1,
    EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS: 1,
}


def _count_workflow_steps(graph: Optional[Dict[str, Any]]) -> int:
    """Count concrete step nodes in a workflow graph payload."""
    steps = graph.get("steps") if isinstance(graph, dict) else None
    if not isinstance(steps, list):
        return 0
    count = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id", "") or "").strip()
        if step_id:
            count += 1
    return count


def _classify_registry_workflow_executability(
    concept_id: str,
) -> Tuple[bool, str, Optional[str]] | None:
    """Classify executability from registry definitions when graph data is absent.

    JVNAUTOSCI-803 follow-on: built-in workflow registrations can be executable
    without a Vontology process graph mirror. This fallback closes monitor/
    discovery parity for registry-backed (non-Vontology) workflows.
    """

    try:
        from ..workflows.durable.registry_factory import (
            build_durable_workflow_registry_read_only,
        )
    except Exception:
        return None

    try:
        registry = build_durable_workflow_registry_read_only()
        registration = registry.get_registration(concept_id)
    except Exception:
        return None

    source = ""
    definition = None
    if registration is not None:
        source = str(getattr(registration, "source", "") or "").strip().lower()
        definition = getattr(registration, "definition", None)
    else:
        try:
            definition = registry.get(concept_id)
        except Exception:
            definition = None

    # Preserve Vontology graph authority for Vontology-sourced registrations.
    if source == "vontology":
        return None
    if definition is None:
        return None

    initial_state = str(getattr(definition, "initial_state", "") or "").strip()
    if not initial_state:
        return (
            False,
            EXECUTABILITY_GRAPH_INCOMPLETE,
            "registry_initial_state_missing",
        )

    states = getattr(definition, "states", None)
    if not isinstance(states, Mapping) or not states:
        return (
            False,
            EXECUTABILITY_GRAPH_INCOMPLETE,
            "registry_states_missing",
        )
    if initial_state not in states:
        return (
            False,
            EXECUTABILITY_GRAPH_INCOMPLETE,
            "registry_initial_state_unresolved",
        )

    return (True, EXECUTABILITY_EXECUTABLE_NOW, None)


@dataclass
class WorkflowMatch:
    """A matched workflow from discovery search."""

    concept_id: str
    name: str
    description: Optional[str] = None
    relevance_score: float = 0.0
    match_source: str = "unknown"  # "semantic", "vontology", "combined"
    is_executable: bool = False
    executability_reason: str = EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT
    executability_detail: Optional[str] = None
    confidence_score: float = 0.0
    is_policy_safe: bool = False
    routing_eligible: bool = False
    routing_exclusion_reason: Optional[str] = None
    routing_profile: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialisation."""
        return {
            "concept_id": self.concept_id,
            "name": self.name,
            "description": self.description,
            "relevance_score": round(self.relevance_score, 3),
            "match_source": self.match_source,
            "is_executable": self.is_executable,
            "executability_reason": self.executability_reason,
            "executability_detail": self.executability_detail,
            "confidence_score": round(self.confidence_score, 3),
            "is_policy_safe": self.is_policy_safe,
            "routing_eligible": self.routing_eligible,
            "routing_exclusion_reason": self.routing_exclusion_reason,
            "routing_profile": (
                dict(self.routing_profile)
                if isinstance(self.routing_profile, dict)
                else None
            ),
        }


@dataclass
class WorkflowDiscoveryResult:
    """Result of workflow discovery search."""

    matches: List[WorkflowMatch] = field(default_factory=list)
    routing_matches: Optional[List[WorkflowMatch]] = None
    search_time_ms: float = 0.0
    query: str = ""
    requested_query: str = ""
    threshold: float = DEFAULT_RELEVANCE_THRESHOLD
    errors: List[str] = field(default_factory=list)
    search_sources: List[str] = field(default_factory=list)
    keyword_fallback_queries: List[str] = field(default_factory=list)
    allow_non_executable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialisation."""
        candidate_payload = [m.to_dict() for m in self.matches]
        # Backward compatibility: if routing_matches is omitted by callers/tests,
        # treat candidates as routing matches.
        routing_source = (
            self.routing_matches if isinstance(self.routing_matches, list) else self.matches
        )
        routing_payload = [m.to_dict() for m in routing_source]
        return {
            # Backwards-compatible key used by selector/orchestrator pathways:
            # "matches" now means routing-eligible candidates by default.
            "matches": routing_payload,
            # Full candidate set for traceability.
            "candidates": candidate_payload,
            "routing_matches": routing_payload,
            "search_time_ms": round(self.search_time_ms, 2),
            "query": self.query,
            "requested_query": self.requested_query or self.query,
            "threshold": self.threshold,
            "match_count": len(routing_payload),
            "candidate_count": len(candidate_payload),
            "search_sources": list(self.search_sources),
            "keyword_fallback_queries": list(self.keyword_fallback_queries),
            "allow_non_executable": self.allow_non_executable,
            "errors": self.errors if self.errors else None,
        }


def _get_workflow_description(concept_doc: Dict[str, Any]) -> Optional[str]:
    """Resolve workflow description via the shared relation-first accessor."""
    return get_concept_description(concept_doc)


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


def _resolve_query_file_copy_contexts(query: str) -> tuple[list[dict[str, Any]], list[str]]:
    contexts: list[dict[str, Any]] = []
    errors: list[str] = []
    for concept_id in extract_file_copy_concept_ids_from_text(query):
        try:
            context = build_file_copy_typing_context(file_copy_concept_id=concept_id)
        except Exception as exc:
            errors.append(
                f"file_copy_context_error:{concept_id}:{type(exc).__name__}"
            )
            continue
        if isinstance(context, Mapping):
            contexts.append(dict(context))
    return contexts, errors


def _augment_query_with_file_copy_context(
    query: str,
    contexts: List[dict[str, Any]],
) -> str:
    if not contexts:
        return query

    lines = [query, "", "Artefact typing context:"]
    for context in contexts[:3]:
        parts: list[str] = []
        file_copy_concept_id = str(context.get("file_copy_concept_id") or "").strip()
        if file_copy_concept_id:
            parts.append(f"file_copy={file_copy_concept_id}")
        route_hint = str(context.get("route_hint") or "").strip()
        if route_hint:
            parts.append(f"route_hint={route_hint}")
        type_names = [
            str(item).strip()
            for item in (context.get("type_display_names") or [])
            if isinstance(item, str) and str(item).strip()
        ]
        if type_names:
            parts.append("types=" + ", ".join(type_names[:4]))
        original_filename = str(context.get("original_filename") or "").strip()
        if original_filename:
            parts.append(f"filename={original_filename}")
        content_type = str(context.get("content_type") or "").strip()
        if content_type:
            parts.append(f"content_type={content_type}")
        if parts:
            lines.append("- " + "; ".join(parts))
    return "\n".join(lines)


def _build_keyword_fallback_queries(
    query: str,
    contexts: List[dict[str, Any]],
) -> list[str]:
    candidates: list[str] = [query]
    for context in contexts:
        route_hint = str(context.get("route_hint") or "").strip()
        if route_hint:
            candidates.append(f"{route_hint} workflow")
            candidates.append(f"{route_hint} representation workflow")
        for type_name in context.get("type_display_names") or []:
            if isinstance(type_name, str) and type_name.strip():
                candidates.append(type_name.strip())

    query_lower = str(query or "").strip().lower()
    talk_keywords = (
        "talk",
        "presentation",
        "seminar",
        "academic talk",
        "scientific talk",
        "technical talk",
    )
    if any(keyword in query_lower for keyword in talk_keywords):
        candidates.extend(
            [
                "talk workflow",
                "talk representation workflow",
                "presentation workflow",
                "presentation representation workflow",
                "academic presentation workflow",
                "technical scientific talk representation workflow",
                "scientific presentation workflow",
                "seminar workflow",
                "seminar representation workflow",
            ]
        )

    arxiv_ids = extract_arxiv_id_candidates(
        query,
        *[
            context.get("original_filename")
            for context in contexts
            if isinstance(context, Mapping)
        ],
    )
    if arxiv_ids:
        primary_arxiv_id = arxiv_ids[0]
        candidates.extend(
            [
                "arxiv workflow",
                "arxiv paper workflow",
                "arxiv paper representation workflow",
                "scholarly paper workflow",
                "scholarly paper representation workflow",
                f"arxiv {primary_arxiv_id}",
            ]
        )

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        token = str(item or "").strip()
        if not token or len(token) < 3:
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(token)
    return deduped


def _search_workflows_name_fallback(
    queries: List[str],
    *,
    limit: int = DEFAULT_MAX_RESULTS * 2,
) -> List[WorkflowMatch]:
    try:
        from .concept_search_service import search_concepts
    except Exception as exc:
        logger.warning("Keyword workflow fallback unavailable: %s", exc)
        return []

    matches: list[WorkflowMatch] = []
    seen_keys: set[tuple[str, str]] = set()
    for search_query in queries:
        for match_type, source_name, score_floor in (
            ("exact", "exact_name", 0.99),
            ("substring", "substring_name", 0.82),
        ):
            for workflow_type in WORKFLOW_TYPE_IDS:
                try:
                    result = search_concepts(
                        query=search_query,
                        match_type=match_type,
                        instance_of=workflow_type,
                        include_description=True,
                        limit=limit,
                    )
                except Exception as exc:
                    logger.warning(
                        "Workflow name fallback search failed for %s (%s): %s",
                        search_query,
                        match_type,
                        exc,
                    )
                    continue

                for concept in result.get("results", []):
                    concept_id = str(concept.get("concept_id") or "").strip()
                    if not concept_id:
                        continue
                    dedupe_key = (concept_id.lower(), source_name)
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    score = concept.get("similarity_score", 0.0)
                    if score <= 0:
                        score = concept.get("relevance_score", 0.0) / 100.0
                    score = max(float(score or 0.0), score_floor)
                    matches.append(
                        WorkflowMatch(
                            concept_id=concept_id,
                            name=concept.get("name", "Unknown"),
                            description=None,
                            relevance_score=score,
                            match_source=source_name,
                        )
                    )
                if matches:
                    break
            if matches:
                break
    return matches


def _enrich_workflow_matches(matches: List[WorkflowMatch]) -> List[WorkflowMatch]:
    """Enrich workflow matches with descriptions from Vontology."""
    if not matches:
        return matches

    try:
        from ..db.repositories.concepts_repository import ConceptsRepository
        from ..workflows.vontology_loader import batch_fetch_workflow_purposes

        concept_ids = [m.concept_id for m in matches]
        authoritative_descriptions = batch_fetch_workflow_purposes(concept_ids)
        concepts = list(
            ConceptsRepository.find(
                {"concept_id": {"$in": concept_ids}},
                projection={
                    "concept_id": 1,
                    "names": 1,
                    "name": 1,
                },
            )
        )

        concept_map = {c.get("concept_id"): c for c in concepts}

        for match in matches:
            concept_doc = concept_map.get(match.concept_id)
            authoritative_description = authoritative_descriptions.get(match.concept_id)
            if concept_doc:
                if not match.description:
                    match.description = authoritative_description or _get_workflow_description(
                        concept_doc
                    )
                if match.name == "Unknown":
                    match.name = _get_workflow_name(concept_doc)
            elif authoritative_description and not match.description:
                match.description = authoritative_description

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
    unique_by_id: dict[str, WorkflowMatch] = {}
    ordered_ids: list[str] = []

    # Sort by score descending so first insert is highest score for each concept.
    sorted_matches = sorted(matches, key=lambda m: -m.relevance_score)

    for match in sorted_matches:
        cid = match.concept_id.strip() if isinstance(match.concept_id, str) else ""
        if not cid or match.relevance_score < threshold:
            continue

        existing = unique_by_id.get(cid)
        if existing is None:
            # Normalise concept_id value to avoid duplicate keys with whitespace.
            match.concept_id = cid
            unique_by_id[cid] = match
            ordered_ids.append(cid)
        else:
            # Merge source confidence across search pathways.
            if existing.match_source != match.match_source:
                existing.match_source = "combined"
            if match.relevance_score > existing.relevance_score:
                existing.relevance_score = match.relevance_score
            if not existing.description and match.description:
                existing.description = match.description
            if existing.name == "Unknown" and match.name and match.name != "Unknown":
                existing.name = match.name

    deduped = [unique_by_id[cid] for cid in ordered_ids]
    deduped.sort(key=lambda m: -m.relevance_score)
    return deduped[: max(1, int(max_results))]


@lru_cache(maxsize=1024)
def _classify_workflow_concept_executability(
    concept_id: str,
) -> Tuple[bool, str, Optional[str]]:
    """Classify whether a concept is executable and provide a reason code."""
    if not isinstance(concept_id, str) or not concept_id.strip():
        return (
            False,
            EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT,
            "invalid_concept_id",
        )

    try:
        from ..workflows.vontology_loader import (
            build_workflow_process_graph,
            detect_vacuous_workflow_steps,
            load_workflow_definition_from_vontology,
            resolve_workflow_publication_lifecycle,
        )

        publication_lifecycle, publication_lifecycle_source = (
            resolve_workflow_publication_lifecycle(concept_id)
        )
        if (
            isinstance(publication_lifecycle, Mapping)
            and publication_lifecycle.get("published") is False
        ):
            phase = str(publication_lifecycle.get("phase") or "draft").strip() or "draft"
            detail = f"workflow_not_published:phase={phase}"
            if (
                isinstance(publication_lifecycle_source, str)
                and publication_lifecycle_source
            ):
                detail = f"{detail}:source={publication_lifecycle_source}"
            return (False, EXECUTABILITY_DRAFT_NOT_PUBLISHED, detail)

        graph, warnings = build_workflow_process_graph(concept_id)
        warning_items = [
            str(item).strip()
            for item in (warnings or [])
            if isinstance(item, str) and item.strip()
        ]
        total_steps = _count_workflow_steps(graph)

        definition = load_workflow_definition_from_vontology(concept_id)
        if definition is not None:
            integrity_issues = detect_vacuous_workflow_steps(
                workflow_id=concept_id,
                graph=graph,
            )
            if integrity_issues:
                sample_issue = integrity_issues[0]
                issue_step = sample_issue.get("step_id")
                issue_count = len(integrity_issues)
                if total_steps > 0 and issue_count >= total_steps:
                    reason = EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS
                elif issue_count:
                    reason = EXECUTABILITY_WORKFLOW_STEP_PARTIALLY_VACUOUS
                else:
                    reason = EXECUTABILITY_WORKFLOW_STEP_INTEGRITY
                return (
                    False,
                    reason,
                    (
                        "workflow_step_contract_issue:"
                        f"count={issue_count},total={total_steps},"
                        f"first_step={issue_step}"
                    ),
                )
            return (True, EXECUTABILITY_EXECUTABLE_NOW, None)

        if not isinstance(graph, dict):
            registry_fallback = _classify_registry_workflow_executability(concept_id)
            if registry_fallback is not None:
                return registry_fallback

        if isinstance(graph, dict):
            detail = warning_items[0] if warning_items else "workflow_graph_not_loadable"
            return (False, EXECUTABILITY_GRAPH_INCOMPLETE, detail)

        if "workflow_has_no_steps" in warning_items:
            return (
                False,
                EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT,
                "workflow_has_no_steps",
            )

        if "workflow_concept_not_found" in warning_items:
            return (False, EXECUTABILITY_GRAPH_INCOMPLETE, "workflow_concept_not_found")

        detail = warning_items[0] if warning_items else "no_workflow_graph_structure"
        return (False, EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT, detail)
    except Exception as exc:
        return (
            False,
            EXECUTABILITY_GRAPH_INCOMPLETE,
            f"classification_error:{type(exc).__name__}",
        )


def classify_workflow_concept_executability(
    concept_id: str,
) -> Tuple[bool, str, Optional[str]]:
    """Expose workflow executability classification for API consumers.

    Returns a tuple:
    - is_executable: whether the workflow should be callable now
    - executability_reason: machine-readable reason code
    - executability_detail: optional human-readable detail
    """
    return _classify_workflow_concept_executability(concept_id)


def _compute_candidate_confidence(match: WorkflowMatch) -> float:
    """Compute a bounded confidence score from relevance + evidence signals."""
    score = max(0.0, min(1.0, float(match.relevance_score)))
    source = (match.match_source or "").strip().lower()
    if source == "combined":
        score += 0.08
    elif source == "semantic":
        score += 0.03
    if isinstance(match.description, str) and match.description.strip():
        score += 0.02
    if match.is_executable:
        score += 0.05
    return min(1.0, round(score, 3))


def _has_authoritative_routing_text(concept_id: str) -> bool:
    """Return whether the workflow exposes authoritative Vontology description text."""

    if not isinstance(concept_id, str) or not concept_id.strip():
        return False

    try:
        from ..workflows.vontology_loader import resolve_workflow_description

        description_text, description_source = resolve_workflow_description(
            concept_id,
            workflow_source="vontology",
        )
    except Exception:
        return False

    return (
        isinstance(description_text, str)
        and bool(description_text.strip())
        and isinstance(description_source, str)
        and description_source.startswith("text_relation:")
    )


@lru_cache(maxsize=1024)
def _resolve_workflow_routing_profile_data(
    concept_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return authoritative routing-profile metadata for a workflow concept."""

    if not isinstance(concept_id, str) or not concept_id.strip():
        return None, None

    try:
        from ..workflows.vontology_loader import resolve_workflow_routing_profile

        profile, source = resolve_workflow_routing_profile(concept_id)
    except Exception:
        return None, None

    if not isinstance(profile, Mapping):
        return None, None
    return dict(profile), str(source or "").strip() or None


@lru_cache(maxsize=1024)
def _resolve_workflow_publication_lifecycle_data(
    concept_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(concept_id, str) or not concept_id.strip():
        return None, None
    try:
        from ..workflows.vontology_loader import resolve_workflow_publication_lifecycle

        lifecycle, source = resolve_workflow_publication_lifecycle(concept_id)
    except Exception:
        return None, None
    if not isinstance(lifecycle, Mapping):
        return None, None
    return dict(lifecycle), str(source or "").strip() or None


def _lifecycle_allows_routing(
    lifecycle: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    if not isinstance(lifecycle, Mapping):
        return True, None
    if lifecycle.get("routing_eligible") is False:
        return False, ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    phase = str(lifecycle.get("phase") or "").strip().lower()
    review_state = str(lifecycle.get("review_state") or "").strip().lower()
    if phase in {"superseded", "demoted"}:
        return False, ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    if review_state in {"pending_review", "rejected", "superseded"}:
        return False, ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    return True, None


def _annotate_and_rank_candidates(
    matches: List[WorkflowMatch],
    *,
    max_results: int,
) -> List[WorkflowMatch]:
    """Attach executability/confidence metadata and rank candidates for routing."""
    annotated: list[WorkflowMatch] = []
    annotation_cap = max(max_results * 2, max_results)
    required_routing_candidates = max(1, int(max_results))

    for match in matches[:annotation_cap]:
        is_executable, reason, detail = _classify_workflow_concept_executability(
            match.concept_id
        )
        has_authoritative_text = _has_authoritative_routing_text(match.concept_id)
        routing_profile, _routing_profile_source = _resolve_workflow_routing_profile_data(
            match.concept_id
        )
        publication_lifecycle, _publication_lifecycle_source = (
            _resolve_workflow_publication_lifecycle_data(match.concept_id)
        )
        lifecycle_allows_routing, lifecycle_exclusion_reason = (
            _lifecycle_allows_routing(publication_lifecycle)
        )
        match.is_executable = bool(is_executable)
        match.executability_reason = reason
        match.executability_detail = detail
        # Discovery-level policy baseline: only executable candidates are
        # considered safe before orchestrator-level policy checks are applied.
        match.is_policy_safe = bool(
            is_executable and has_authoritative_text and lifecycle_allows_routing
        )
        match.routing_eligible = bool(
            is_executable and has_authoritative_text and lifecycle_allows_routing
        )
        if not is_executable:
            match.routing_exclusion_reason = reason
        elif not has_authoritative_text:
            match.routing_exclusion_reason = (
                ROUTING_EXCLUSION_MISSING_AUTHORITATIVE_PURPOSE
            )
        elif lifecycle_exclusion_reason:
            match.routing_exclusion_reason = lifecycle_exclusion_reason
        else:
            match.routing_exclusion_reason = None
        if isinstance(routing_profile, dict):
            match.routing_profile = routing_profile
        match.confidence_score = _compute_candidate_confidence(match)
        annotated.append(match)

        routing_eligible_count = sum(1 for item in annotated if item.routing_eligible)
        if routing_eligible_count >= required_routing_candidates:
            break

    annotated.sort(
        key=lambda m: (
            -_EXECUTABILITY_REASON_PRIORITY.get(m.executability_reason, -1),
            -m.confidence_score,
            -m.relevance_score,
            m.concept_id,
        )
    )
    return annotated[: max(1, int(max_results))]


def _filter_routing_candidates(
    matches: List[WorkflowMatch], *, allow_non_executable: bool
) -> List[WorkflowMatch]:
    """Return candidates eligible for selector context by policy defaults."""
    if allow_non_executable:
        for match in matches:
            if not match.routing_eligible:
                match.routing_eligible = True
                match.routing_exclusion_reason = None
        return list(matches)
    return [m for m in matches if m.routing_eligible]


def _env_allow_non_executable_default() -> bool:
    value = os.getenv("VON_WORKFLOW_SELECTOR_ALLOW_NON_EXECUTABLE", "0")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _has_enough_capability_matches(
    matches: List[WorkflowMatch],
    *,
    threshold: float,
    max_results: int,
) -> bool:
    seen_ids: set[str] = set()
    qualifying_count = 0
    for match in matches:
        concept_id = str(match.concept_id or "").strip()
        if not concept_id or concept_id in seen_ids:
            continue
        seen_ids.add(concept_id)
        if float(match.relevance_score or 0.0) < threshold:
            continue
        qualifying_count += 1
        if qualifying_count >= max(1, int(max_results)):
            return True
    return False


@lru_cache(maxsize=512)
def _is_executable_workflow_concept(concept_id: str) -> bool:
    """Return whether a workflow concept can be loaded into an executable definition."""
    is_executable, _reason, _detail = _classify_workflow_concept_executability(
        concept_id
    )
    return is_executable


def invalidate_workflow_discovery_executability_caches() -> None:
    """Clear cached workflow executability classifications.

    Workflow executability depends on live Vontology graph state. Any concept
    mutation that edits workflow/step structure should clear these caches so
    discovery and selector routing can react without process restarts.
    """

    _classify_workflow_concept_executability.cache_clear()
    _is_executable_workflow_concept.cache_clear()
    _resolve_workflow_routing_profile_data.cache_clear()


def _count_executable_matches(matches: List[WorkflowMatch]) -> int:
    return sum(1 for match in matches if bool(match.is_executable))


def _search_workflow_capabilities(
    query: str,
    *,
    limit: int = DEFAULT_MAX_RESULTS * 3,
) -> List[WorkflowMatch]:
    """Search the dedicated workflow capability index (primary search path).

    JVNAUTOSCI-1424 Phase 2: The capability index covers ALL registered
    workflows (built-in + Vontology) with rich capability descriptions.
    This is the first search strategy tried, before semantic/vontology/name
    fallback paths.
    """
    try:
        cap_matches = search_workflow_capabilities(
            query,
            max_results=limit,
            min_score=0.01,
            non_blocking=True,
            max_wait_seconds=DISCOVERY_CAPABILITY_INDEX_MAX_WAIT_SECONDS,
        )
        results: list[WorkflowMatch] = []
        for cap in cap_matches:
            results.append(
                WorkflowMatch(
                    concept_id=cap.workflow_id,
                    name=cap.name,
                    description=cap.description,
                    relevance_score=cap.relevance_score,
                    match_source="capability_index",
                )
            )
        return results
    except Exception as exc:
        logger.warning("Workflow capability index search failed: %s", exc)
        return []


def discover_workflows(
    query: str,
    *,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
    allow_non_executable: bool = False,
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
    search_sources: List[str] = []
    file_copy_contexts, context_errors = _resolve_query_file_copy_contexts(query)
    errors.extend(context_errors)
    search_query = _augment_query_with_file_copy_context(query, file_copy_contexts)
    keyword_fallback_queries = _build_keyword_fallback_queries(query, file_copy_contexts)

    # JVNAUTOSCI-1424 Phase 2: Search the dedicated capability index FIRST.
    # This covers all registered workflows (built-in + Vontology) with rich
    # capability descriptions.  Built-in workflows like chat_assistant and
    # tool_calling are discoverable on equal footing with Vontology workflows.
    try:
        search_sources.append("capability_index")
        capability_matches = _search_workflow_capabilities(
            search_query, limit=max_results * 3
        )
        capability_index_state = get_workflow_capability_index_runtime_state()
        all_matches.extend(capability_matches)
        capability_matches_sufficient = _has_enough_capability_matches(
            capability_matches,
            threshold=relevance_threshold,
            max_results=max_results,
        )
        if not capability_matches:
            if bool(capability_index_state.get("build_in_progress", False)):
                errors.append("capability_index_build_in_progress")
            elif not bool(capability_index_state.get("ready", False)):
                errors.append("capability_index_not_ready")
            last_error = capability_index_state.get("last_error")
            if isinstance(last_error, str) and last_error.strip():
                errors.append(f"capability_index_build_error:{last_error.strip()}")
    except Exception as e:
        capability_matches_sufficient = False
        errors.append(f"capability_index_error: {e}")
        logger.warning("Workflow capability index search failed: %s", e)

    # Secondary: existing search sources fill gaps the capability index misses.
    if (
        not capability_matches_sufficient
        and (time.perf_counter() - start_time) < timeout_seconds
    ):
        try:
            search_sources.append("semantic")
            semantic_matches = _search_workflows_semantic(
                search_query, limit=max_results * 2
            )
            all_matches.extend(semantic_matches)
        except Exception as e:
            errors.append(f"semantic_search_error: {e}")
            logger.warning(f"Semantic workflow discovery failed: {e}")

    if (
        not capability_matches_sufficient
        and (time.perf_counter() - start_time) < timeout_seconds
    ):
        try:
            search_sources.append("vontology")
            vontology_matches = _search_workflows_vontology(
                search_query, limit=max_results * 2
            )
            all_matches.extend(vontology_matches)
        except Exception as e:
            errors.append(f"vontology_search_error: {e}")
            logger.warning(f"Vontology workflow discovery failed: {e}")

    if (
        not capability_matches_sufficient
        and keyword_fallback_queries
        and (time.perf_counter() - start_time) < timeout_seconds
    ):
        try:
            search_sources.append("name_fallback")
            fallback_matches = _search_workflows_name_fallback(
                keyword_fallback_queries,
                limit=max_results * 2,
            )
            all_matches.extend(fallback_matches)
        except Exception as e:
            errors.append(f"workflow_name_fallback_error: {e}")
            logger.warning("Workflow name fallback discovery failed: %s", e)

    # Deduplicate and rank (keep a larger pre-limit for executability-aware
    # ranking to avoid early relevance-only truncation).
    ranked_matches = _deduplicate_and_rank(
        all_matches,
        threshold=relevance_threshold,
        max_results=max(max_results * 4, max_results),
    )

    # Enrich with descriptions
    ranked_matches = _enrich_workflow_matches(ranked_matches)
    ranked_matches = _annotate_and_rank_candidates(
        ranked_matches,
        max_results=max_results,
    )
    routing_matches = _filter_routing_candidates(
        ranked_matches,
        allow_non_executable=allow_non_executable,
    )

    executable_match_count = _count_executable_matches(ranked_matches)
    try:
        from ..workflows.workflow_baseline_telemetry import (
            record_workflow_discovery_observation,
        )

        record_workflow_discovery_observation(
            discovered_match_count=len(ranked_matches),
            executable_match_count=executable_match_count,
        )
    except Exception:
        pass

    elapsed_ms = (time.perf_counter() - start_time) * 1000

    logger.info(
        f"[workflow_discovery] query='{query[:50]}...' "
        f"candidates={len(ranked_matches)} eligible={len(routing_matches)} "
        f"executable={executable_match_count} "
        f"elapsed_ms={elapsed_ms:.1f}"
    )

    return WorkflowDiscoveryResult(
        matches=ranked_matches,
        routing_matches=routing_matches,
        search_time_ms=elapsed_ms,
        query=search_query,
        requested_query=query,
        threshold=relevance_threshold,
        errors=errors if errors else [],
        search_sources=search_sources,
        keyword_fallback_queries=keyword_fallback_queries,
        allow_non_executable=allow_non_executable,
    )


def discover_workflows_for_turn(
    user_input: str,
    *,
    namespace: Optional[str] = None,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    max_results: int = DEFAULT_MAX_RESULTS,
    allow_non_executable: Optional[bool] = None,
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
        Dict with workflow suggestions or None if the input is too short
    """
    if not user_input or len(user_input.strip()) < 5:
        # Skip very short inputs
        return None

    try:
        effective_allow_non_executable = (
            _env_allow_non_executable_default()
            if allow_non_executable is None
            else bool(allow_non_executable)
        )
        result = discover_workflows(
            user_input,
            relevance_threshold=relevance_threshold,
            max_results=max_results,
            allow_non_executable=effective_allow_non_executable,
        )
        return result.to_dict()

    except Exception as e:
        logger.warning(f"Workflow discovery for turn failed: {e}")
        return None
