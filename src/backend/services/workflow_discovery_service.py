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
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..vontology.utils_vontology import get_concept_description
from .file_copy_reference_service import extract_file_copy_concept_ids_from_text
from .file_copy_typing_service import build_file_copy_typing_context
from .workflow_capability_service import (
    get_workflow_capability_index_runtime_state,
    search_workflow_capabilities,
)
from ..workflows.workflow_definition_identity_service import assess_workflow_id_hygiene

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
def _coerce_discovery_timeout_seconds(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return max(0.0, float(default))
    if parsed <= 0.0:
        return max(0.0, float(default))
    return parsed


SEARCH_TIMEOUT_SECONDS = _coerce_discovery_timeout_seconds(
    os.getenv("VON_WORKFLOW_DISCOVERY_TIMEOUT_SECONDS"),
    default=10.0,
)
# Cold-start capability-index waits should be bounded and should not consume the
# full discovery budget. Discovery still needs time for semantic and Vontology
# search when the authoritative primary substrate is not yet sufficient.
DISCOVERY_CAPABILITY_INDEX_MAX_WAIT_SECONDS = 0.75
DISCOVERY_CAPABILITY_INDEX_WAIT_TIMEOUT_FRACTION = 0.5

# Executability reason codes (JVNAUTOSCI-1088).
EXECUTABILITY_EXECUTABLE_NOW = "executable_now"
EXECUTABILITY_GRAPH_INCOMPLETE = "graph_incomplete"
EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT = "non_executable_design_artifact"
EXECUTABILITY_DRAFT_NOT_PUBLISHED = "draft_not_published"
EXECUTABILITY_WORKFLOW_STEP_INTEGRITY = "workflow_step_integrity_issue"
EXECUTABILITY_WORKFLOW_STEP_PARTIALLY_VACUOUS = "workflow_step_partially_vacuous"
EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS = "workflow_step_completely_vacuous"
ROUTING_EXCLUSION_MISSING_AUTHORITATIVE_PURPOSE = "missing_authoritative_purpose"
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


def _remaining_search_timeout_seconds(
    *,
    started_at: float,
    timeout_seconds: float,
) -> float:
    return max(0.0, float(timeout_seconds) - (time.perf_counter() - started_at))


def _is_discovery_budget_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError)


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
    *,
    workflow_registry: Any | None = None,
) -> Tuple[bool, str, Optional[str]] | None:
    """Classify executability from registry definitions when graph data is absent.

    JVNAUTOSCI-803 follow-on: built-in workflow registrations can be executable
    without a Vontology process graph mirror. This fallback closes monitor/
    discovery parity for registry-backed (non-Vontology) workflows.
    """

    try:
        registry = workflow_registry
        if registry is None:
            from ..workflows.durable.registry_factory import (
                get_shared_workflow_registry_read_only,
            )

            registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
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


def _classify_workflow_candidate_executability(
    concept_id: str,
    *,
    workflow_registry: Any | None = None,
) -> Tuple[bool, str, Optional[str]]:
    """Classify candidate executability, reusing a live registry when available."""

    if workflow_registry is not None:
        registry_fallback = _classify_registry_workflow_executability(
            concept_id,
            workflow_registry=workflow_registry,
        )
        if registry_fallback is not None:
            return registry_fallback

    return _classify_workflow_concept_executability(concept_id)


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
    allow_non_executable: bool = False
    match_absence_reason: Optional[str] = None
    timeout_budget_seconds: float = SEARCH_TIMEOUT_SECONDS
    budget_exhausted: bool = False
    budget_exhaustion_stage: Optional[str] = None
    budget_exhaustion_detail: Optional[str] = None
    stage_timings: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialisation."""
        candidate_payload = [m.to_dict() for m in self.matches]
        # Backward compatibility: if routing_matches is omitted by callers/tests,
        # treat candidates as routing matches.
        routing_source = (
            self.routing_matches
            if isinstance(self.routing_matches, list)
            else self.matches
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
            "allow_non_executable": self.allow_non_executable,
            "match_absence_reason": self.match_absence_reason,
            "timeout_budget_seconds": round(self.timeout_budget_seconds, 3),
            "budget_exhausted": bool(self.budget_exhausted),
            "budget_exhaustion_stage": self.budget_exhaustion_stage,
            "budget_exhaustion_detail": self.budget_exhaustion_detail,
            "stage_timings": list(self.stage_timings),
            "stage_timing_count": len(self.stage_timings),
            "errors": self.errors if self.errors else None,
        }


def _record_discovery_stage_timing(
    stage_timings: List[Dict[str, Any]],
    *,
    stage: str,
    started_at: float,
    status: str,
    **details: Any,
) -> None:
    """Append safe, bounded timing metadata for one discovery stage."""

    stage_name = str(stage or "").strip() or "workflow_discovery"
    status_token = str(status or "").strip() or "unknown"
    payload: Dict[str, Any] = {
        "stage": stage_name,
        "status": status_token,
        "elapsed_ms": round(max(0.0, (time.perf_counter() - started_at) * 1000), 2),
    }
    for key, value in details.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key_text] = value
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            payload[key_text] = [str(item)[:240] for item in list(value)[:20]]
        elif isinstance(value, Mapping):
            scalar_map: Dict[str, Any] = {}
            for map_key, map_value in value.items():
                if isinstance(map_value, (str, int, float, bool)) or map_value is None:
                    scalar_map[str(map_key)[:120]] = map_value
            if scalar_map:
                payload[key_text] = scalar_map
    stage_timings.append(payload)


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


def _compute_capability_index_wait_seconds(timeout_seconds: float) -> float:
    """Return the bounded wait budget for a cold capability-index build."""
    try:
        timeout_budget = max(0.0, float(timeout_seconds))
    except (TypeError, ValueError):
        timeout_budget = 0.0
    if timeout_budget <= 0.0:
        return 0.0
    return min(
        DISCOVERY_CAPABILITY_INDEX_MAX_WAIT_SECONDS,
        timeout_budget * DISCOVERY_CAPABILITY_INDEX_WAIT_TIMEOUT_FRACTION,
    )


def _derive_capability_index_unavailability_errors(
    *,
    runtime_state: Mapping[str, Any],
    wait_seconds: float,
) -> list[str]:
    errors: list[str] = []
    ready = bool(runtime_state.get("ready", False))
    build_in_progress = bool(runtime_state.get("build_in_progress", False))
    if wait_seconds > 0.0 and build_in_progress and not ready:
        errors.append("capability_index_wait_timed_out")
    if build_in_progress:
        errors.append("capability_index_build_in_progress")
    elif not ready:
        errors.append("capability_index_not_ready")
    last_error = runtime_state.get("last_error")
    if isinstance(last_error, str) and last_error.strip():
        errors.append(f"capability_index_build_error:{last_error.strip()}")
    return errors


def _derive_discovery_result_match_absence_reason(
    *,
    candidates: Sequence[WorkflowMatch],
    routing_matches: Sequence[WorkflowMatch],
    errors: Sequence[str],
) -> str | None:
    if routing_matches:
        return None

    if not candidates:
        error_set = {str(item).strip() for item in errors if str(item).strip()}
        if (
            "capability_index_wait_timed_out" in error_set
            and "capability_index_build_in_progress" in error_set
        ):
            return "capability_index_wait_timed_out_build_in_progress"
        if "capability_index_build_in_progress" in error_set:
            return "capability_index_build_in_progress"
        if "capability_index_not_ready" in error_set:
            return "capability_index_not_ready"
        if any(
            error.startswith("capability_index_build_error:") for error in error_set
        ):
            return "capability_index_build_error"
        return "no_discovery_candidates"

    excluded_candidates = [
        match
        for match in candidates
        if isinstance(match.routing_exclusion_reason, str)
        and match.routing_exclusion_reason.strip()
    ]
    if excluded_candidates and len(excluded_candidates) >= len(candidates):
        exclusion_reasons = {
            str(match.routing_exclusion_reason).strip()
            for match in excluded_candidates
            if str(match.routing_exclusion_reason).strip()
        }
        if len(exclusion_reasons) == 1:
            return next(iter(exclusion_reasons))
        return "all_discovery_candidates_excluded"
    if excluded_candidates:
        return "no_routing_match_after_exclusions"
    return "no_routing_match_above_threshold"


def _resolve_query_file_copy_contexts(
    query: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    contexts: list[dict[str, Any]] = []
    errors: list[str] = []
    for concept_id in extract_file_copy_concept_ids_from_text(query):
        try:
            context = build_file_copy_typing_context(file_copy_concept_id=concept_id)
        except Exception as exc:
            errors.append(f"file_copy_context_error:{concept_id}:{type(exc).__name__}")
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


_DIRECT_WORKFLOW_CONCEPT_PATTERN = re.compile(
    r"#V#[A-Za-z0-9_:-]*workflow[A-Za-z0-9_:-]*",
    re.IGNORECASE,
)


def _normalise_workflow_phrase(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("#v#"):
        text = text[3:]
    text = text.replace("_", " ")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _query_has_workflow_execute_contract(query: str) -> bool:
    lowered = str(query or "").lower()
    return "workflow_execute" in lowered and (
        "required tool" in lowered
        or "required tools" in lowered
        or "required_tools" in lowered
        or "success requires" in lowered
        or "grounding requirement" in lowered
    )


def _extract_direct_workflow_concept_ids(query: str) -> list[str]:
    seen: set[str] = set()
    concept_ids: list[str] = []
    for match in _DIRECT_WORKFLOW_CONCEPT_PATTERN.finditer(str(query or "")):
        concept_id = match.group(0).strip().rstrip(".,;:)]}")
        lowered = concept_id.lower()
        if not concept_id or lowered in seen:
            continue
        seen.add(lowered)
        concept_ids.append(concept_id)
    return concept_ids


def _iter_registry_workflow_ids(workflow_registry: Any | None) -> list[str]:
    registry = workflow_registry
    if registry is None:
        try:
            from ..workflows.durable.registry_factory import (
                get_shared_workflow_registry_read_only,
            )

            registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
        except Exception:
            registry = None
    if registry is None:
        return []

    for method_name in ("all_workflow_ids", "workflow_ids", "list_workflow_ids"):
        method = getattr(registry, method_name, None)
        if not callable(method):
            continue
        try:
            raw_ids = method()
        except Exception:
            continue
        if isinstance(raw_ids, Sequence) and not isinstance(raw_ids, (str, bytes)):
            return [
                str(item).strip()
                for item in raw_ids
                if isinstance(item, str) and str(item).strip()
            ]
    return []


def _peek_registry_workflow_metadata(
    workflow_id: str,
    *,
    workflow_registry: Any | None,
) -> tuple[str | None, str | None]:
    registry = workflow_registry
    if registry is None:
        try:
            from ..workflows.durable.registry_factory import (
                get_shared_workflow_registry_read_only,
            )

            registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
        except Exception:
            registry = None
    if registry is None:
        return None, None

    registration = None
    for method_name in ("peek_registration", "get_registration"):
        method = getattr(registry, method_name, None)
        if not callable(method):
            continue
        try:
            registration = method(workflow_id)
        except Exception:
            registration = None
        if registration is not None:
            break

    purpose = ""
    source = ""
    definition = None
    if registration is not None:
        purpose = str(getattr(registration, "purpose", "") or "").strip()
        source = str(getattr(registration, "source", "") or "").strip()
        definition = getattr(registration, "definition", None)
    if definition is None:
        method = getattr(registry, "get", None)
        if callable(method):
            try:
                definition = method(workflow_id)
            except Exception:
                definition = None
    if definition is not None and not purpose:
        purpose = str(getattr(definition, "purpose", "") or "").strip()
    return purpose or None, source or None


def _workflow_display_name_from_id(workflow_id: str) -> str:
    phrase = _normalise_workflow_phrase(workflow_id)
    if not phrase:
        return workflow_id
    return " ".join(part.capitalize() for part in phrase.split())


def _resolve_contract_direct_workflow_candidates(
    query: str,
    *,
    workflow_registry: Any | None,
    limit: int,
) -> list[WorkflowMatch]:
    """Resolve directly grounded workflow candidates during index cold starts.

    This is intentionally narrower than secondary semantic search: it only
    acts when the turn already contains a structured workflow-execution
    contract or an explicit workflow concept ID, and it only accepts direct ID
    or exact label containment.
    """

    direct_ids = _extract_direct_workflow_concept_ids(query)
    direct_lookup = {item.lower(): item for item in direct_ids}
    if not direct_ids and not _query_has_workflow_execute_contract(query):
        return []

    query_phrase = f" {_normalise_workflow_phrase(query)} "
    candidate_ids: list[str] = list(direct_ids)
    seen = {item.lower() for item in candidate_ids}
    for workflow_id in _iter_registry_workflow_ids(workflow_registry):
        lowered = workflow_id.lower()
        if lowered in seen:
            continue
        workflow_phrase = _normalise_workflow_phrase(workflow_id)
        if not workflow_phrase:
            continue
        if f" {workflow_phrase} " not in query_phrase:
            continue
        seen.add(lowered)
        candidate_ids.append(workflow_id)
        if len(candidate_ids) >= max(1, int(limit)):
            break

    matches: list[WorkflowMatch] = []
    for workflow_id in candidate_ids[: max(1, int(limit))]:
        purpose, _source = _peek_registry_workflow_metadata(
            workflow_id,
            workflow_registry=workflow_registry,
        )
        explicit = workflow_id.lower() in direct_lookup
        matches.append(
            WorkflowMatch(
                concept_id=workflow_id,
                name=_workflow_display_name_from_id(workflow_id),
                description=purpose,
                relevance_score=1.0 if explicit else 0.97,
                match_source="contract_direct_workflow_resolution",
            )
        )
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
                    match.description = (
                        authoritative_description
                        or _get_workflow_description(concept_doc)
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

    workflow_id_hygiene = assess_workflow_id_hygiene(concept_id)
    if not bool(workflow_id_hygiene.get("valid")):
        reason_codes = [
            str(item.get("reason_code") or "").strip()
            for item in (workflow_id_hygiene.get("issues") or [])
            if isinstance(item, Mapping) and str(item.get("reason_code") or "").strip()
        ]
        detail = "workflow_id_invalid"
        if reason_codes:
            detail = f"{detail}:{','.join(reason_codes)}"
        return (
            False,
            EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT,
            detail,
        )

    registry_fallback = _classify_registry_workflow_executability(concept_id)
    if registry_fallback is not None:
        return registry_fallback

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
            phase = (
                str(publication_lifecycle.get("phase") or "draft").strip() or "draft"
            )
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

        if isinstance(graph, dict):
            detail = (
                warning_items[0] if warning_items else "workflow_graph_not_loadable"
            )
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
    published = lifecycle.get("published")
    if isinstance(published, bool) and not published:
        return False, ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    if phase in {
        "draft",
        "validated",
        "validation_failed",
        "draft_failed_completion_gate",
        "pending_review",
        "rejected",
        "superseded",
        "rolled_back",
        "demoted",
    }:
        return False, ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    rollout_state = str(lifecycle.get("rollout_state") or "").strip().lower()
    if rollout_state in {"disabled", "superseded", "rolled_back", "demoted"}:
        return False, ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    # Review state describes the active authoring proposal. A pending proposal
    # must not disable the currently published workflow version.
    return True, None


def _annotate_and_rank_candidates(
    matches: List[WorkflowMatch],
    *,
    max_results: int,
    workflow_registry: Any | None = None,
) -> List[WorkflowMatch]:
    """Attach executability/confidence metadata and rank candidates for routing."""
    annotated: list[WorkflowMatch] = []
    annotation_cap = max(max_results * 2, max_results)
    required_routing_candidates = max(1, int(max_results))

    for match in matches[:annotation_cap]:
        is_executable, reason, detail = _classify_workflow_candidate_executability(
            match.concept_id,
            workflow_registry=workflow_registry,
        )
        has_authoritative_text = _has_authoritative_routing_text(match.concept_id)
        routing_profile, _routing_profile_source = (
            _resolve_workflow_routing_profile_data(match.concept_id)
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
    # The capability index is the authoritative primary routing substrate.
    # Once it produces at least one threshold-qualified workflow, discovery
    # should not keep spending turn budget on slower secondary searches just to
    # fill the remaining result slots.
    required_qualifying_count = 1 if max(1, int(max_results)) >= 1 else 1
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
        if qualifying_count >= required_qualifying_count:
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
    max_wait_seconds: float | None = None,
    workflow_registry: Any | None = None,
) -> List[WorkflowMatch]:
    """Search the dedicated workflow capability index (primary search path).

    JVNAUTOSCI-1424 Phase 2: The capability index covers ALL registered
    workflows (built-in + Vontology) with rich capability descriptions.
    This is the first search strategy tried, before semantic/vontology/name
    fallback paths.
    """
    try:
        effective_max_wait_seconds = (
            DISCOVERY_CAPABILITY_INDEX_MAX_WAIT_SECONDS
            if max_wait_seconds is None
            else max(0.0, float(max_wait_seconds))
        )
        cap_matches = search_workflow_capabilities(
            query,
            max_results=limit,
            min_score=0.01,
            non_blocking=True,
            max_wait_seconds=effective_max_wait_seconds,
            workflow_registry=workflow_registry,
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
    workflow_registry: Any | None = None,
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
    effective_timeout_seconds = _coerce_discovery_timeout_seconds(
        timeout_seconds,
        default=SEARCH_TIMEOUT_SECONDS,
    )
    all_matches: List[WorkflowMatch] = []
    errors: List[str] = []
    search_sources: List[str] = []
    stage_timings: List[Dict[str, Any]] = []
    budget_exhausted = False
    budget_exhaustion_stage: str | None = None
    budget_exhaustion_detail: str | None = None
    file_context_started_at = time.perf_counter()
    file_copy_contexts, context_errors = _resolve_query_file_copy_contexts(query)
    errors.extend(context_errors)
    search_query = _augment_query_with_file_copy_context(query, file_copy_contexts)
    _record_discovery_stage_timing(
        stage_timings,
        stage="file_copy_context",
        started_at=file_context_started_at,
        status="completed",
        context_count=len(file_copy_contexts),
        error_count=len(context_errors),
    )
    capability_index_wait_seconds = _compute_capability_index_wait_seconds(
        effective_timeout_seconds
    )

    def _record_budget_exhaustion(stage: str, detail: str) -> None:
        nonlocal budget_exhausted
        nonlocal budget_exhaustion_stage
        nonlocal budget_exhaustion_detail
        budget_exhausted = True
        if budget_exhaustion_stage:
            return
        budget_exhaustion_stage = str(stage or "").strip() or "workflow_discovery"
        budget_exhaustion_detail = str(detail or "").strip() or None

    def _finalise_result(
        *,
        ranked_matches: List[WorkflowMatch],
        routing_matches: List[WorkflowMatch],
    ) -> WorkflowDiscoveryResult:
        match_absence_reason = _derive_discovery_result_match_absence_reason(
            candidates=ranked_matches,
            routing_matches=routing_matches or [],
            errors=errors,
        )

        executable_match_count = _count_executable_matches(ranked_matches)
        try:
            from ..workflows.workflow_baseline_telemetry import (
                record_workflow_discovery_observation,
            )

            record_workflow_discovery_observation(
                discovered_match_count=len(ranked_matches),
                executable_match_count=executable_match_count,
                budget_exhausted=budget_exhausted,
                budget_exhaustion_stage=budget_exhaustion_stage,
                timeout_budget_seconds=effective_timeout_seconds,
            )
        except Exception:
            pass

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        _record_discovery_stage_timing(
            stage_timings,
            stage="finalise_result",
            started_at=start_time,
            status="completed",
            candidate_count=len(ranked_matches),
            routing_match_count=len(routing_matches),
            budget_exhausted=budget_exhausted,
        )

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
            allow_non_executable=allow_non_executable,
            match_absence_reason=match_absence_reason,
            timeout_budget_seconds=effective_timeout_seconds,
            budget_exhausted=budget_exhausted,
            budget_exhaustion_stage=budget_exhaustion_stage,
            budget_exhaustion_detail=budget_exhaustion_detail,
            stage_timings=stage_timings,
        )

    # JVNAUTOSCI-1424 Phase 2: Search the dedicated capability index FIRST.
    # This covers all registered workflows (built-in + Vontology) with rich
    # capability descriptions.  Built-in workflows like chat_assistant and
    # tool_calling are discoverable on equal footing with Vontology workflows.
    capability_index_state: Mapping[str, Any] = {}
    try:
        search_sources.append("capability_index")
        capability_started_at = time.perf_counter()
        capability_timeout_remaining = _remaining_search_timeout_seconds(
            started_at=start_time,
            timeout_seconds=effective_timeout_seconds,
        )
        if capability_timeout_remaining <= 0.0:
            raise TimeoutError(
                "Discovery timeout budget was exhausted before "
                "capability_index_search could start "
                f"(budget={effective_timeout_seconds:.3f}s)."
            )
        capability_matches = _search_workflow_capabilities(
            search_query,
            limit=max_results * 3,
            max_wait_seconds=min(
                capability_index_wait_seconds,
                capability_timeout_remaining,
            ),
            workflow_registry=workflow_registry,
        )
        capability_index_state = get_workflow_capability_index_runtime_state()
        capability_elapsed = time.perf_counter() - capability_started_at
        all_matches.extend(capability_matches)
        capability_matches_sufficient = _has_enough_capability_matches(
            capability_matches,
            threshold=relevance_threshold,
            max_results=max_results,
        )
        _record_discovery_stage_timing(
            stage_timings,
            stage="capability_index_search",
            started_at=capability_started_at,
            status="completed",
            match_count=len(capability_matches),
            sufficient=capability_matches_sufficient,
            runtime_ready=bool(capability_index_state.get("ready")),
            build_in_progress=bool(capability_index_state.get("build_in_progress")),
        )
        if capability_elapsed > capability_timeout_remaining:
            _record_budget_exhaustion(
                "capability_index_search",
                (
                    "capability_index_search exceeded remaining discovery budget "
                    f"(elapsed={capability_elapsed:.3f}s, "
                    f"budget={capability_timeout_remaining:.3f}s)."
                ),
            )
        if not capability_matches:
            capability_unavailability_errors = (
                _derive_capability_index_unavailability_errors(
                    runtime_state=capability_index_state,
                    wait_seconds=min(
                        capability_index_wait_seconds,
                        capability_timeout_remaining,
                    ),
                )
            )
            errors.extend(capability_unavailability_errors)
            if capability_unavailability_errors:
                direct_resolution_started_at = time.perf_counter()
                direct_matches = _resolve_contract_direct_workflow_candidates(
                    search_query,
                    workflow_registry=workflow_registry,
                    limit=max_results * 2,
                )
                if direct_matches:
                    search_sources.append("contract_direct_workflow_resolution")
                    all_matches.extend(direct_matches)
                    capability_matches_sufficient = _has_enough_capability_matches(
                        direct_matches,
                        threshold=relevance_threshold,
                        max_results=max_results,
                    )
                    _record_discovery_stage_timing(
                        stage_timings,
                        stage="contract_direct_workflow_resolution",
                        started_at=direct_resolution_started_at,
                        status="completed",
                        match_count=len(direct_matches),
                        sufficient=capability_matches_sufficient,
                        reason="capability_index_unavailable",
                    )
                else:
                    return _finalise_result(ranked_matches=[], routing_matches=[])
    except Exception as e:
        capability_matches_sufficient = False
        errors.append(f"capability_index_error: {e}")
        if _is_discovery_budget_timeout(e):
            _record_budget_exhaustion("capability_index_search", str(e))
        _record_discovery_stage_timing(
            stage_timings,
            stage="capability_index_search",
            started_at=locals().get("capability_started_at", start_time),
            status="error",
            error=type(e).__name__,
        )
        logger.warning("Workflow capability index search failed: %s", e)

    # Secondary authoritative search sources fill gaps the capability index misses.
    semantic_budget_remaining = _remaining_search_timeout_seconds(
        started_at=start_time,
        timeout_seconds=effective_timeout_seconds,
    )
    if not capability_matches_sufficient and semantic_budget_remaining <= 0.0:
        _record_budget_exhaustion(
            "semantic_search",
            (
                "Discovery timeout budget was exhausted before semantic_search "
                f"could start (budget={effective_timeout_seconds:.3f}s)."
            ),
        )
    elif not capability_matches_sufficient:
        try:
            search_sources.append("semantic")
            semantic_started_at = time.perf_counter()
            semantic_matches = _search_workflows_semantic(
                search_query,
                limit=max_results * 2,
            )
            all_matches.extend(semantic_matches)
            semantic_elapsed = time.perf_counter() - semantic_started_at
            _record_discovery_stage_timing(
                stage_timings,
                stage="semantic_search",
                started_at=semantic_started_at,
                status="completed",
                match_count=len(semantic_matches),
                budget_seconds=round(semantic_budget_remaining, 3),
            )
            if semantic_elapsed > semantic_budget_remaining:
                _record_budget_exhaustion(
                    "semantic_search",
                    (
                        "semantic_search exceeded remaining discovery budget "
                        f"(elapsed={semantic_elapsed:.3f}s, "
                        f"budget={semantic_budget_remaining:.3f}s)."
                    ),
                )
        except Exception as e:
            errors.append(f"semantic_search_error: {e}")
            if _is_discovery_budget_timeout(e):
                _record_budget_exhaustion("semantic_search", str(e))
            _record_discovery_stage_timing(
                stage_timings,
                stage="semantic_search",
                started_at=locals().get("semantic_started_at", start_time),
                status="error",
                error=type(e).__name__,
            )
            logger.warning(f"Semantic workflow discovery failed: {e}")

    vontology_budget_remaining = _remaining_search_timeout_seconds(
        started_at=start_time,
        timeout_seconds=effective_timeout_seconds,
    )
    if not capability_matches_sufficient and vontology_budget_remaining <= 0.0:
        _record_budget_exhaustion(
            "vontology_search",
            (
                "Discovery timeout budget was exhausted before vontology_search "
                f"could start (budget={effective_timeout_seconds:.3f}s)."
            ),
        )
    elif not capability_matches_sufficient:
        try:
            search_sources.append("vontology")
            vontology_started_at = time.perf_counter()
            vontology_matches = _search_workflows_vontology(
                search_query,
                limit=max_results * 2,
            )
            all_matches.extend(vontology_matches)
            vontology_elapsed = time.perf_counter() - vontology_started_at
            _record_discovery_stage_timing(
                stage_timings,
                stage="vontology_search",
                started_at=vontology_started_at,
                status="completed",
                match_count=len(vontology_matches),
                budget_seconds=round(vontology_budget_remaining, 3),
            )
            if vontology_elapsed > vontology_budget_remaining:
                _record_budget_exhaustion(
                    "vontology_search",
                    (
                        "vontology_search exceeded remaining discovery budget "
                        f"(elapsed={vontology_elapsed:.3f}s, "
                        f"budget={vontology_budget_remaining:.3f}s)."
                    ),
                )
        except Exception as e:
            errors.append(f"vontology_search_error: {e}")
            if _is_discovery_budget_timeout(e):
                _record_budget_exhaustion("vontology_search", str(e))
            _record_discovery_stage_timing(
                stage_timings,
                stage="vontology_search",
                started_at=locals().get("vontology_started_at", start_time),
                status="error",
                error=type(e).__name__,
            )
            logger.warning(f"Vontology workflow discovery failed: {e}")

    # Deduplicate and rank (keep a larger pre-limit for executability-aware
    # ranking to avoid early relevance-only truncation).
    dedupe_started_at = time.perf_counter()
    ranked_matches = _deduplicate_and_rank(
        all_matches,
        threshold=relevance_threshold,
        max_results=max(max_results * 4, max_results),
    )
    _record_discovery_stage_timing(
        stage_timings,
        stage="deduplicate_and_rank",
        started_at=dedupe_started_at,
        status="completed",
        input_count=len(all_matches),
        output_count=len(ranked_matches),
    )

    # Enrich with descriptions
    enrich_started_at = time.perf_counter()
    ranked_matches = _enrich_workflow_matches(ranked_matches)
    _record_discovery_stage_timing(
        stage_timings,
        stage="enrich_matches",
        started_at=enrich_started_at,
        status="completed",
        candidate_count=len(ranked_matches),
    )
    annotate_started_at = time.perf_counter()
    ranked_matches = _annotate_and_rank_candidates(
        ranked_matches,
        max_results=max_results,
        workflow_registry=workflow_registry,
    )
    _record_discovery_stage_timing(
        stage_timings,
        stage="annotate_and_rank",
        started_at=annotate_started_at,
        status="completed",
        candidate_count=len(ranked_matches),
        routing_eligible_count=sum(
            1 for item in ranked_matches if item.routing_eligible
        ),
    )
    filter_started_at = time.perf_counter()
    routing_matches = _filter_routing_candidates(
        ranked_matches,
        allow_non_executable=allow_non_executable,
    )
    _record_discovery_stage_timing(
        stage_timings,
        stage="filter_routing_candidates",
        started_at=filter_started_at,
        status="completed",
        routing_match_count=len(routing_matches),
    )
    return _finalise_result(
        ranked_matches=ranked_matches,
        routing_matches=routing_matches,
    )


def discover_workflows_for_turn(
    user_input: str,
    *,
    namespace: Optional[str] = None,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout_seconds: float | str | None = None,
    allow_non_executable: Optional[bool] = None,
    workflow_registry: Any | None = None,
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

    effective_timeout_seconds = _coerce_discovery_timeout_seconds(
        timeout_seconds,
        default=SEARCH_TIMEOUT_SECONDS,
    )

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
            timeout_seconds=effective_timeout_seconds,
            allow_non_executable=effective_allow_non_executable,
            workflow_registry=workflow_registry,
        )
        return result.to_dict()

    except Exception as e:
        logger.warning(f"Workflow discovery for turn failed: {e}")
        budget_exhausted = _is_discovery_budget_timeout(e)
        try:
            from ..workflows.workflow_baseline_telemetry import (
                record_workflow_discovery_observation,
            )

            record_workflow_discovery_observation(
                discovered_match_count=0,
                executable_match_count=0,
                budget_exhausted=budget_exhausted,
                budget_exhaustion_stage=(
                    "workflow_discovery_for_turn" if budget_exhausted else None
                ),
                timeout_budget_seconds=effective_timeout_seconds,
            )
        except Exception:
            pass
        return WorkflowDiscoveryResult(
            query=user_input.strip(),
            requested_query=user_input.strip(),
            threshold=relevance_threshold,
            errors=[
                (
                    f"workflow_discovery_budget_exhausted: {e}"
                    if budget_exhausted
                    else f"workflow_discovery_for_turn_error: {e}"
                )
            ],
            match_absence_reason=(
                "workflow_discovery_budget_exhausted"
                if budget_exhausted
                else "workflow_discovery_for_turn_error"
            ),
            allow_non_executable=(
                _env_allow_non_executable_default()
                if allow_non_executable is None
                else bool(allow_non_executable)
            ),
            timeout_budget_seconds=effective_timeout_seconds,
            budget_exhausted=budget_exhausted,
            budget_exhaustion_stage=(
                "workflow_discovery_for_turn" if budget_exhausted else None
            ),
            budget_exhaustion_detail=str(e) if budget_exhausted else None,
            stage_timings=[
                {
                    "stage": "workflow_discovery_for_turn",
                    "status": "error",
                    "elapsed_ms": 0.0,
                    "error": type(e).__name__,
                }
            ],
        ).to_dict()
