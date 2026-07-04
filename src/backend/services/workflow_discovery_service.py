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
    resolve_workflow_capabilities_for_contract,
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
DISCOVERY_UNBOUNDED_SECONDARY_MIN_BUDGET_SECONDS = 1.0

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
ROUTING_EXCLUSION_EXPLICIT_WORKFLOW_CONTEXT_REQUIRED = (
    "explicit_workflow_context_required"
)
ROUTING_READINESS_WORKFLOW_ABSENT = "workflow_absent"
ROUTING_READINESS_WORKFLOW_PRESENT_NOT_INDEXED = "workflow_present_not_indexed"
ROUTING_READINESS_WORKFLOW_PRESENT_LAZY_DEFINITION = "workflow_present_lazy_definition"
ROUTING_READINESS_WORKFLOW_PRESENT_MISSING_AUTHORITATIVE_ROUTING_TEXT = (
    "workflow_present_missing_authoritative_routing_text"
)
ROUTING_READINESS_WORKFLOW_PRESENT_NOT_EXECUTABLE = "workflow_present_not_executable"
ROUTING_READINESS_WORKFLOW_PRESENT_NOT_ROUTING_ELIGIBLE = (
    "workflow_present_not_routing_eligible"
)
ROUTING_READINESS_WORKFLOW_PRESENT_CAPABILITY_INDEX_PENDING = (
    "workflow_present_capability_index_pending"
)
DISCOVERY_BLOCKER_BUDGET_EXHAUSTED_NO_CANDIDATES = (
    "workflow_discovery_budget_exhausted_no_candidates"
)
CONTRACT_PROJECTION_SCHEMA_VERSION = "workflow_discovery_contract_projection.v1"

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
    routing_index_metadata: Optional[Dict[str, Any]] = None
    routing_readiness_status: Optional[str] = None
    routing_readiness_detail: Optional[str] = None

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
            "routing_index_metadata": (
                dict(self.routing_index_metadata)
                if isinstance(self.routing_index_metadata, dict)
                else None
            ),
            "routing_readiness_status": self.routing_readiness_status,
            "routing_readiness_detail": self.routing_readiness_detail,
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
    contract_projection: Optional[Dict[str, Any]] = None
    routing_readiness_diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    blocker_reason: Optional[str] = None

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
            "contract_projection": (
                dict(self.contract_projection)
                if isinstance(self.contract_projection, dict)
                else None
            ),
            "routing_readiness_diagnostics": (
                list(self.routing_readiness_diagnostics)
                if self.routing_readiness_diagnostics
                else None
            ),
            "blocker_reason": self.blocker_reason,
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


def _agent_test_registry_capability_search_should_block(
    *,
    workflow_registry: Any | None,
) -> bool:
    if workflow_registry is None:
        return False
    return str(os.getenv("VON_AGENT_TEST_INSTANCE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "y",
    }


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


def _get_latency_sensitive_capability_index_runtime_state() -> Mapping[str, Any]:
    try:
        return get_workflow_capability_index_runtime_state(latency_sensitive=True)
    except TypeError:
        return get_workflow_capability_index_runtime_state()


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


def _query_may_name_workflow_directly(query_phrase: str) -> bool:
    """Return True only for exact workflow-artefact identity resolution.

    This guards a registry ID/name scan. It should stay about workflow identity,
    not task semantics or routing preference.
    """

    return " workflow " in f" {str(query_phrase or '').strip()} "


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


def _dedupe_text_values(values: Sequence[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(text)
    return ordered


def _iter_contract_scalar_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, Mapping):
        texts: list[str] = []
        for item in value.values():
            texts.extend(_iter_contract_scalar_text(item))
        return texts
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        texts = []
        for item in value:
            texts.extend(_iter_contract_scalar_text(item))
        return texts
    return []


def _contract_state_field_payload(
    expected_outcome_contract: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if not isinstance(expected_outcome_contract, Mapping):
        return {}
    if isinstance(expected_outcome_contract.get("fields"), Mapping):
        return expected_outcome_contract.get("fields") or {}
    return expected_outcome_contract


def _contract_sequence_values(
    *,
    expected_outcome_contract: Mapping[str, Any],
    field_payload: Mapping[str, Any],
    field_names: Sequence[str],
) -> list[str]:
    values: list[Any] = []
    for source in (expected_outcome_contract, field_payload):
        for field_name in field_names:
            if field_name in source:
                raw_value = source.get(field_name)
                if isinstance(raw_value, Sequence) and not isinstance(
                    raw_value,
                    (str, bytes, bytearray),
                ):
                    values.extend(raw_value)
                else:
                    values.append(raw_value)
    return _dedupe_text_values(values)


def _extract_contract_workflow_concept_ids(
    expected_outcome_contract: Mapping[str, Any] | None,
) -> list[str]:
    if not isinstance(expected_outcome_contract, Mapping):
        return []

    direct_field_names = (
        "workflow_id",
        "workflow_ids",
        "workflow_concept_id",
        "workflow_concept_ids",
        "target_workflow_id",
        "target_workflow_ids",
        "preferred_workflow_id",
        "preferred_workflow_ids",
        "selected_workflow_id",
        "selected_workflow_ids",
        "workflow_execute_target",
        "workflow_execute_targets",
    )
    field_payload = _contract_state_field_payload(expected_outcome_contract)
    ordered: list[str] = []
    ordered.extend(
        _contract_sequence_values(
            expected_outcome_contract=expected_outcome_contract,
            field_payload=field_payload,
            field_names=direct_field_names,
        )
    )
    for text in _iter_contract_scalar_text(expected_outcome_contract):
        ordered.extend(_extract_direct_workflow_concept_ids(text))
    return _dedupe_text_values(ordered)


def _build_expected_outcome_contract_projection(
    expected_outcome_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project represented turn-contract fields into discovery support telemetry."""

    if not isinstance(expected_outcome_contract, Mapping):
        return {
            "schema_version": CONTRACT_PROJECTION_SCHEMA_VERSION,
            "fields_used": [],
            "text": "",
            "query_text": "",
            "workflow_concept_ids": [],
            "required_tools": [],
            "required_actions": [],
            "target_concept_ids": [],
            "target_type_ids": [],
        }

    field_payload = _contract_state_field_payload(expected_outcome_contract)
    lines: list[str] = []
    query_lines: list[str] = []
    fields_used: list[str] = []

    labelled_text_fields: tuple[tuple[str, str], ...] = (
        ("selector_guidance", "Routing guidance"),
        ("grounding_requirement", "Grounding requirement"),
        ("summary", "Success target"),
        ("precision_policy", "Precision policy"),
        ("answering_guidance", "Answering guidance"),
        ("reasoning", "Contract reasoning"),
    )
    context_aliases: dict[str, tuple[str, ...]] = {
        "summary": ("turn_expected_outcome_summary",),
        "grounding_requirement": ("turn_expected_grounding_requirement",),
        "precision_policy": ("turn_expected_precision_policy",),
        "selector_guidance": ("turn_selector_guidance",),
        "answering_guidance": ("turn_answering_guidance",),
        "reasoning": ("turn_expected_outcome_reasoning",),
    }

    for field_name, label in labelled_text_fields:
        value = field_payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            for alias in context_aliases.get(field_name, ()):
                value = field_payload.get(alias)
                if isinstance(value, str) and value.strip():
                    break
        if isinstance(value, str) and value.strip():
            lines.append(f"- {label}: {value.strip()}")
            fields_used.append(field_name)

    required_tools = _contract_sequence_values(
        expected_outcome_contract=expected_outcome_contract,
        field_payload=field_payload,
        field_names=("required_tools", "turn_expected_required_tools"),
    )
    if required_tools:
        line = "- Required tools: " + ", ".join(required_tools)
        lines.append(line)
        query_lines.append(line)
        fields_used.append("required_tools")

    required_actions = _contract_sequence_values(
        expected_outcome_contract=expected_outcome_contract,
        field_payload=field_payload,
        field_names=(
            "required_actions",
            "required_action_ids",
            "required_workflow_actions",
            "required_workflow_action_ids",
            "required_obligations",
            "postconditions",
        ),
    )
    if required_actions:
        line = "- Required workflow actions: " + ", ".join(required_actions)
        lines.append(line)
        query_lines.append(line)
        fields_used.append("required_actions")

    target_concept_ids = _contract_sequence_values(
        expected_outcome_contract=expected_outcome_contract,
        field_payload=field_payload,
        field_names=(
            "target_concept_ids",
            "turn_expected_target_concept_ids",
            "target_concept_id",
            "target_concepts",
        ),
    )
    if target_concept_ids:
        line = "- Target concept IDs: " + ", ".join(target_concept_ids)
        lines.append(line)
        query_lines.append(line)
        fields_used.append("target_concept_ids")

    target_type_ids = _contract_sequence_values(
        expected_outcome_contract=expected_outcome_contract,
        field_payload=field_payload,
        field_names=(
            "target_type_ids",
            "turn_expected_target_type_ids",
            "target_type_id",
            "target_types",
            "requested_type_ids",
        ),
    )
    if target_type_ids:
        line = "- Target type IDs: " + ", ".join(target_type_ids)
        lines.append(line)
        query_lines.append(line)
        fields_used.append("target_type_ids")

    workflow_concept_ids = _extract_contract_workflow_concept_ids(
        expected_outcome_contract
    )
    if workflow_concept_ids:
        line = "- Workflow concept IDs: " + ", ".join(workflow_concept_ids)
        lines.append(line)
        query_lines.append(line)
        fields_used.append("workflow_concept_ids")

    text = ""
    if lines:
        text = "\n".join(["Turn-intent routing guidance:", *lines])
    query_text = ""
    if query_lines:
        query_text = "\n".join(["Turn-intent routing guidance:", *query_lines])

    return {
        "schema_version": CONTRACT_PROJECTION_SCHEMA_VERSION,
        "fields_used": _dedupe_text_values(fields_used),
        "text": text,
        "query_text": query_text,
        "workflow_concept_ids": workflow_concept_ids,
        "required_tools": required_tools,
        "required_actions": required_actions,
        "target_concept_ids": target_concept_ids,
        "target_type_ids": target_type_ids,
    }


def _apply_contract_projection_to_query(
    query: str,
    *,
    projection: Mapping[str, Any] | None,
) -> str:
    projection_text = (
        str(projection.get("query_text") or "").strip()
        if isinstance(projection, Mapping)
        else ""
    )
    clean_query = str(query or "").strip()
    if not projection_text:
        return clean_query
    if projection_text in clean_query:
        return clean_query
    if "Turn-intent routing guidance:" in clean_query:
        return "\n".join([clean_query, *projection_text.splitlines()[1:]]).strip()
    if not clean_query:
        return projection_text
    return f"{clean_query}\n\n{projection_text}"


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
    label_query: str | None = None,
    contract_projection: Mapping[str, Any] | None = None,
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
    if isinstance(contract_projection, Mapping):
        raw_workflow_ids = contract_projection.get("workflow_concept_ids")
        if isinstance(raw_workflow_ids, Sequence) and not isinstance(
            raw_workflow_ids,
            (str, bytes, bytearray),
        ):
            direct_ids = _dedupe_text_values([*direct_ids, *raw_workflow_ids])
    direct_lookup = {item.lower(): item for item in direct_ids}
    required_tools = (
        contract_projection.get("required_tools")
        if isinstance(contract_projection, Mapping)
        else ()
    )
    contract_requires_workflow_execute = any(
        str(item or "").strip().lower() == "workflow_execute"
        for item in (
            required_tools
            if isinstance(required_tools, Sequence)
            and not isinstance(required_tools, (str, bytes, bytearray))
            else ()
        )
    )
    identity_query = label_query if isinstance(label_query, str) else query
    query_phrase = f" {_normalise_workflow_phrase(identity_query)} "
    registry_identity_scan_allowed = bool(
        workflow_registry is not None
        and (
            _query_has_workflow_execute_contract(query)
            or contract_requires_workflow_execute
            or _query_may_name_workflow_directly(query_phrase)
        )
    )
    direct_identity_scan_allowed = bool(direct_ids or registry_identity_scan_allowed)
    if not direct_identity_scan_allowed:
        return []

    candidate_ids: list[str] = list(direct_ids)
    seen = {item.lower() for item in candidate_ids}
    if registry_identity_scan_allowed:
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


def _derive_candidate_routing_readiness_status(
    match: WorkflowMatch,
) -> tuple[str, str | None]:
    if not match.is_executable:
        detail = str(match.executability_detail or "").strip() or None
        if detail and (
            "lazy" in detail.lower() or "definition_loaded=false" in detail.lower()
        ):
            return ROUTING_READINESS_WORKFLOW_PRESENT_LAZY_DEFINITION, detail
        return ROUTING_READINESS_WORKFLOW_PRESENT_NOT_EXECUTABLE, (
            str(match.executability_reason or "").strip() or detail
        )
    if (
        match.routing_exclusion_reason
        == ROUTING_EXCLUSION_MISSING_AUTHORITATIVE_PURPOSE
    ):
        return (
            ROUTING_READINESS_WORKFLOW_PRESENT_MISSING_AUTHORITATIVE_ROUTING_TEXT,
            match.routing_exclusion_reason,
        )
    if match.routing_exclusion_reason:
        return (
            ROUTING_READINESS_WORKFLOW_PRESENT_NOT_ROUTING_ELIGIBLE,
            match.routing_exclusion_reason,
        )
    if match.routing_eligible:
        return "workflow_present_routing_ready", None
    return ROUTING_READINESS_WORKFLOW_PRESENT_NOT_ROUTING_ELIGIBLE, None


def _query_explicitly_names_workflow_candidate(
    query: str,
    match: WorkflowMatch,
) -> bool:
    """Return whether the turn directly names this workflow artefact.

    This is a support guard for represented routing profiles that declare
    ``explicit_workflow_context_required``. It only recognises explicit workflow
    identity, not domain semantics.
    """

    direct_ids = {
        item.lower()
        for item in _extract_direct_workflow_concept_ids(query)
        if isinstance(item, str) and item.strip()
    }
    concept_id = str(match.concept_id or "").strip()
    if concept_id and concept_id.lower() in direct_ids:
        return True

    query_phrase = f" {_normalise_workflow_phrase(query)} "
    if not _query_may_name_workflow_directly(query_phrase):
        return False

    candidate_phrases = {
        _normalise_workflow_phrase(concept_id),
        _normalise_workflow_phrase(match.name),
    }
    return any(
        bool(phrase) and f" {phrase} " in query_phrase
        for phrase in candidate_phrases
    )


def _routing_profile_exclusion_reason(
    match: WorkflowMatch,
    *,
    query: str,
) -> str | None:
    profile = match.routing_profile
    if not isinstance(profile, Mapping):
        return None
    if profile.get("routing_eligible") is False:
        return ROUTING_EXCLUSION_EXPLICITLY_DISABLED
    if (
        profile.get("explicit_workflow_context_required") is True
        and not _query_explicitly_names_workflow_candidate(query, match)
    ):
        return ROUTING_EXCLUSION_EXPLICIT_WORKFLOW_CONTEXT_REQUIRED
    return None


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


def _routing_index_metadata(match: WorkflowMatch) -> Mapping[str, Any] | None:
    metadata = getattr(match, "routing_index_metadata", None)
    if isinstance(metadata, Mapping):
        schema = str(metadata.get("routing_index_schema_version") or "").strip()
        if schema == "workflow_routing_index_entry.v1":
            return metadata
    return None


def _compact_executability_from_routing_index(
    match: WorkflowMatch,
) -> Tuple[bool, str, Optional[str]] | None:
    metadata = _routing_index_metadata(match)
    if metadata is None:
        return None
    compact = metadata.get("compact_executability")
    if not isinstance(compact, Mapping):
        return None
    source = str(compact.get("source") or "").strip()
    if source != "vontology_workflow_graph_shape":
        return None
    is_executable = bool(compact.get("is_executable"))
    if is_executable:
        detail_parts = [f"source={source}"]
        step_count = compact.get("step_count")
        if isinstance(step_count, int):
            detail_parts.append(f"step_count={step_count}")
        if bool(compact.get("has_initial_step")):
            detail_parts.append("has_initial_step=true")
        return (
            True,
            EXECUTABILITY_EXECUTABLE_NOW,
            "compact_routing_index:" + ",".join(detail_parts),
        )
    reason = str(compact.get("reason") or "").strip()
    if reason == "workflow_has_no_steps":
        return (
            False,
            EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT,
            "compact_routing_index:workflow_has_no_steps",
        )
    return (
        False,
        EXECUTABILITY_GRAPH_INCOMPLETE,
        f"compact_routing_index:{reason or 'graph_incomplete'}",
    )


def _has_authoritative_routing_text_from_index(match: WorkflowMatch) -> bool | None:
    metadata = _routing_index_metadata(match)
    if metadata is None:
        return None
    if isinstance(metadata.get("has_authoritative_routing_text"), bool):
        return bool(metadata.get("has_authoritative_routing_text"))
    description_source = str(metadata.get("description_source") or "").strip()
    if description_source:
        return description_source.startswith("text_relation:")
    return None


def _routing_profile_from_index(
    match: WorkflowMatch,
) -> tuple[dict[str, Any] | None, str | None] | None:
    metadata = _routing_index_metadata(match)
    if metadata is None:
        return None
    profile = metadata.get("routing_profile")
    source = str(metadata.get("routing_profile_source") or "").strip() or None
    if isinstance(profile, Mapping):
        return dict(profile), source
    return None, source


def _publication_lifecycle_from_index(
    match: WorkflowMatch,
) -> tuple[dict[str, Any] | None, str | None] | None:
    metadata = _routing_index_metadata(match)
    if metadata is None:
        return None
    lifecycle = metadata.get("publication_lifecycle")
    source = str(metadata.get("publication_lifecycle_source") or "").strip() or None
    if isinstance(lifecycle, Mapping):
        return dict(lifecycle), source
    return None, source


def _annotate_and_rank_candidates(
    matches: List[WorkflowMatch],
    *,
    max_results: int,
    workflow_registry: Any | None = None,
    query: str = "",
) -> List[WorkflowMatch]:
    """Attach executability/confidence metadata and rank candidates for routing."""
    annotated: list[WorkflowMatch] = []
    annotation_cap = max(max_results * 2, max_results)
    required_routing_candidates = max(1, int(max_results))

    for match in matches[:annotation_cap]:
        compact_executability = _compact_executability_from_routing_index(match)
        if compact_executability is not None:
            is_executable, reason, detail = compact_executability
        else:
            is_executable, reason, detail = _classify_workflow_candidate_executability(
                match.concept_id,
                workflow_registry=workflow_registry,
            )
        indexed_authoritative_text = _has_authoritative_routing_text_from_index(match)
        has_authoritative_text = (
            indexed_authoritative_text
            if indexed_authoritative_text is not None
            else _has_authoritative_routing_text(match.concept_id)
        )
        indexed_routing_profile = _routing_profile_from_index(match)
        if indexed_routing_profile is not None:
            routing_profile, _routing_profile_source = indexed_routing_profile
        else:
            routing_profile, _routing_profile_source = (
                _resolve_workflow_routing_profile_data(match.concept_id)
            )
        indexed_publication_lifecycle = _publication_lifecycle_from_index(match)
        if indexed_publication_lifecycle is not None:
            publication_lifecycle, _publication_lifecycle_source = (
                indexed_publication_lifecycle
            )
        else:
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
        profile_exclusion_reason = _routing_profile_exclusion_reason(
            match,
            query=query,
        )
        if profile_exclusion_reason:
            match.routing_eligible = False
            match.is_policy_safe = False
            match.routing_exclusion_reason = profile_exclusion_reason
        readiness_status, readiness_detail = _derive_candidate_routing_readiness_status(
            match
        )
        match.routing_readiness_status = readiness_status
        match.routing_readiness_detail = readiness_detail
        match.confidence_score = _compute_candidate_confidence(match)
        annotated.append(match)

        routing_eligible_count = sum(1 for item in annotated if item.routing_eligible)
        if routing_eligible_count >= required_routing_candidates:
            break

    annotated.sort(
        key=lambda m: (
            -_EXECUTABILITY_REASON_PRIORITY.get(m.executability_reason, -1),
            -int(bool(m.routing_eligible)),
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
    _resolve_workflow_publication_lifecycle_data.cache_clear()


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
            non_blocking=not _agent_test_registry_capability_search_should_block(
                workflow_registry=workflow_registry,
            ),
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
                    routing_index_metadata=(
                        dict(cap.metadata) if isinstance(cap.metadata, dict) else None
                    ),
                )
            )
        return results
    except Exception as exc:
        logger.warning("Workflow capability index search failed: %s", exc)
        return []


def _resolve_contract_capability_metadata_matches(
    *,
    contract_projection: Mapping[str, Any] | None,
    workflow_registry: Any | None,
    limit: int,
    exclude_ids: set[str] | None = None,
) -> list[WorkflowMatch]:
    if not isinstance(contract_projection, Mapping):
        return []
    required_tools = contract_projection.get("required_tools")
    required_actions = contract_projection.get("required_actions")
    if (
        not isinstance(required_tools, Sequence)
        or isinstance(required_tools, (str, bytes, bytearray))
    ) and (
        not isinstance(required_actions, Sequence)
        or isinstance(required_actions, (str, bytes, bytearray))
    ):
        return []
    try:
        cap_matches = resolve_workflow_capabilities_for_contract(
            required_tools=required_tools,
            required_actions=required_actions,
            max_results=limit,
            workflow_registry=workflow_registry,
            exclude_ids=exclude_ids,
        )
    except Exception as exc:
        logger.warning("Workflow contract capability resolution failed: %s", exc)
        return []
    results: list[WorkflowMatch] = []
    for cap in cap_matches:
        results.append(
            WorkflowMatch(
                concept_id=cap.workflow_id,
                name=cap.name,
                description=cap.description,
                relevance_score=cap.relevance_score,
                match_source=cap.source,
                routing_index_metadata=(
                    dict(cap.metadata) if isinstance(cap.metadata, dict) else None
                ),
            )
        )
    return results


def discover_workflows(
    query: str,
    *,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
    allow_non_executable: bool = False,
    workflow_registry: Any | None = None,
    expected_outcome_contract: Mapping[str, Any] | None = None,
    requested_query: str | None = None,
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
    requested_query_text = (
        requested_query.strip() if isinstance(requested_query, str) else ""
    ) or query
    if not query:
        return WorkflowDiscoveryResult(
            query="",
            threshold=relevance_threshold,
            errors=["Empty query after trimming"],
            requested_query=requested_query_text,
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
    contract_projection = _build_expected_outcome_contract_projection(
        expected_outcome_contract
    )
    budget_exhausted = False
    budget_exhaustion_stage: str | None = None
    budget_exhaustion_detail: str | None = None
    file_context_started_at = time.perf_counter()
    file_copy_contexts, context_errors = _resolve_query_file_copy_contexts(query)
    errors.extend(context_errors)
    contract_enriched_query = _apply_contract_projection_to_query(
        query,
        projection=contract_projection,
    )
    search_query = _augment_query_with_file_copy_context(
        contract_enriched_query,
        file_copy_contexts,
    )
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
        routing_readiness_diagnostics = [
            {
                "workflow_id": match.concept_id,
                "status": match.routing_readiness_status,
                "detail": match.routing_readiness_detail,
                "match_source": match.match_source,
                "routing_exclusion_reason": match.routing_exclusion_reason,
                "executability_reason": match.executability_reason,
            }
            for match in ranked_matches
            if match.routing_readiness_status
        ]
        blocker_reason = (
            DISCOVERY_BLOCKER_BUDGET_EXHAUSTED_NO_CANDIDATES
            if budget_exhausted and not ranked_matches
            else match_absence_reason
        )
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
            requested_query=requested_query_text,
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
            contract_projection=contract_projection,
            routing_readiness_diagnostics=routing_readiness_diagnostics,
            blocker_reason=blocker_reason,
        )

    # Cheap exact-ID / represented-contract resolution runs before retrieval so
    # a structured workflow contract is not hidden by a cold capability index or
    # a later semantic-search budget cutoff.
    direct_resolution_started_at = time.perf_counter()
    direct_matches = _resolve_contract_direct_workflow_candidates(
        search_query,
        label_query=requested_query_text,
        contract_projection=contract_projection,
        workflow_registry=workflow_registry,
        limit=max_results * 2,
    )
    direct_matches_sufficient = _has_enough_capability_matches(
        direct_matches,
        threshold=relevance_threshold,
        max_results=max_results,
    )
    if direct_matches:
        search_sources.append("contract_direct_workflow_resolution")
        all_matches.extend(direct_matches)
    _record_discovery_stage_timing(
        stage_timings,
        stage="contract_direct_workflow_resolution",
        started_at=direct_resolution_started_at,
        status="completed",
        match_count=len(direct_matches),
        sufficient=direct_matches_sufficient,
        field_count=len(contract_projection.get("fields_used") or []),
        workflow_concept_id_count=len(
            contract_projection.get("workflow_concept_ids") or []
        ),
    )

    # JVNAUTOSCI-1424 Phase 2: Search the dedicated capability index FIRST.
    # This covers all registered workflows (built-in + Vontology) with rich
    # capability descriptions.  Built-in workflows like chat_assistant and
    # tool_calling are discoverable on equal footing with Vontology workflows.
    capability_index_state: Mapping[str, Any] = {}
    capability_matches_sufficient = direct_matches_sufficient
    if direct_matches_sufficient:
        _record_discovery_stage_timing(
            stage_timings,
            stage="capability_index_search",
            started_at=time.perf_counter(),
            status="skipped_contract_direct_sufficient",
        )
    else:
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
            capability_index_state = (
                _get_latency_sensitive_capability_index_runtime_state()
            )
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
                    _record_discovery_stage_timing(
                        stage_timings,
                        stage="capability_index_unavailable",
                        started_at=capability_started_at,
                        status="completed",
                        errors=capability_unavailability_errors,
                    )
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

    if not capability_matches_sufficient:
        contract_metadata_started_at = time.perf_counter()
        existing_candidate_ids = {
            str(match.concept_id or "").strip()
            for match in all_matches
            if str(match.concept_id or "").strip()
        }
        contract_metadata_matches = _resolve_contract_capability_metadata_matches(
            contract_projection=contract_projection,
            workflow_registry=workflow_registry,
            limit=max_results * 3,
            exclude_ids=existing_candidate_ids,
        )
        contract_metadata_sufficient = _has_enough_capability_matches(
            contract_metadata_matches,
            threshold=relevance_threshold,
            max_results=max_results,
        )
        if contract_metadata_matches:
            search_sources.append("contract_capability_metadata")
            all_matches.extend(contract_metadata_matches)
            capability_matches_sufficient = contract_metadata_sufficient
        _record_discovery_stage_timing(
            stage_timings,
            stage="contract_capability_metadata_resolution",
            started_at=contract_metadata_started_at,
            status="completed",
            match_count=len(contract_metadata_matches),
            sufficient=contract_metadata_sufficient,
            field_count=len(contract_projection.get("fields_used") or []),
            required_tool_count=len(contract_projection.get("required_tools") or []),
            required_action_count=len(contract_projection.get("required_actions") or []),
            entry_source=(
                (
                    contract_metadata_matches[0].routing_index_metadata
                    if contract_metadata_matches
                    else {}
                )
                or {}
            )
            .get("contract_capability_match", {})
            .get("entry_source"),
        )

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
    elif (
        not capability_matches_sufficient
        and semantic_budget_remaining < DISCOVERY_UNBOUNDED_SECONDARY_MIN_BUDGET_SECONDS
    ):
        _record_budget_exhaustion(
            "semantic_search",
            (
                "Discovery skipped semantic_search because the remaining budget "
                "was too small for the unbounded secondary search substrate "
                f"(remaining={semantic_budget_remaining:.3f}s, "
                f"minimum={DISCOVERY_UNBOUNDED_SECONDARY_MIN_BUDGET_SECONDS:.3f}s)."
            ),
        )
        _record_discovery_stage_timing(
            stage_timings,
            stage="semantic_search",
            started_at=time.perf_counter(),
            status="skipped_budget_insufficient",
            budget_seconds=round(semantic_budget_remaining, 3),
            minimum_budget_seconds=round(
                DISCOVERY_UNBOUNDED_SECONDARY_MIN_BUDGET_SECONDS, 3
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
    elif (
        not capability_matches_sufficient
        and vontology_budget_remaining
        < DISCOVERY_UNBOUNDED_SECONDARY_MIN_BUDGET_SECONDS
    ):
        _record_budget_exhaustion(
            "vontology_search",
            (
                "Discovery skipped vontology_search because the remaining budget "
                "was too small for the unbounded secondary search substrate "
                f"(remaining={vontology_budget_remaining:.3f}s, "
                f"minimum={DISCOVERY_UNBOUNDED_SECONDARY_MIN_BUDGET_SECONDS:.3f}s)."
            ),
        )
        _record_discovery_stage_timing(
            stage_timings,
            stage="vontology_search",
            started_at=time.perf_counter(),
            status="skipped_budget_insufficient",
            budget_seconds=round(vontology_budget_remaining, 3),
            minimum_budget_seconds=round(
                DISCOVERY_UNBOUNDED_SECONDARY_MIN_BUDGET_SECONDS, 3
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
        query=requested_query_text,
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


SELECTOR_FAST_PATH_POLICY_CONCEPT_ID = "#V#selector_fast_path_policy"
SELECTOR_FAST_PATH_POLICY_SCHEMA = "selector_fast_path_policy.v1"
_SELECTOR_FAST_PATH_SUPPORTED_RULES = frozenset(
    {
        "single_unique_executable_candidate",
        "unique_executable_candidate",
        "unique_candidate_covers_contract",
    }
)


def _resolve_selector_fast_path_policy() -> (
    tuple[Optional[Dict[str, Any]], Dict[str, Any]]
):
    """Resolve the represented selector fast-path policy (JVNAUTOSCI-2406).

    The policy is authored on the Vontology concept
    ``#V#selector_fast_path_policy`` as ``hasContent`` JSON. Python validates
    schema and rule vocabulary and fails closed: absence or invalidity means
    no fast-path policy is stamped and the selector LLM remains the routing
    authority.
    """

    import json as _json

    diagnostics: Dict[str, Any] = {
        "policy_concept_id": SELECTOR_FAST_PATH_POLICY_CONCEPT_ID,
        "status": "policy_unavailable",
        "error": None,
    }
    try:
        from .text_value_service import get_texts_for_concept

        rows = get_texts_for_concept(
            SELECTOR_FAST_PATH_POLICY_CONCEPT_ID, predicate="hasContent", limit=1
        )
    except Exception as exc:
        diagnostics["error"] = f"selector_fast_path_policy_lookup_failed:{exc}"
        return None, diagnostics

    raw_text = None
    if rows and isinstance(rows[0], Mapping):
        raw_text = rows[0].get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        diagnostics["error"] = "selector_fast_path_policy_content_missing"
        return None, diagnostics

    try:
        payload = _json.loads(raw_text)
    except Exception as exc:
        diagnostics["error"] = f"selector_fast_path_policy_json_invalid:{exc}"
        diagnostics["status"] = "policy_invalid"
        return None, diagnostics
    if not isinstance(payload, Mapping):
        diagnostics["error"] = "selector_fast_path_policy_payload_not_object"
        diagnostics["status"] = "policy_invalid"
        return None, diagnostics

    schema = str(payload.get("schema") or payload.get("schema_version") or "").strip()
    if schema != SELECTOR_FAST_PATH_POLICY_SCHEMA:
        diagnostics["error"] = "selector_fast_path_policy_schema_invalid"
        diagnostics["schema"] = schema or None
        diagnostics["status"] = "policy_invalid"
        return None, diagnostics

    if payload.get("enabled") is not True:
        diagnostics["error"] = "selector_fast_path_policy_disabled"
        diagnostics["status"] = "policy_disabled"
        return None, diagnostics

    rule = str(payload.get("rule") or payload.get("rule_id") or "").strip()
    if rule not in _SELECTOR_FAST_PATH_SUPPORTED_RULES:
        diagnostics["error"] = "selector_fast_path_policy_rule_unsupported"
        diagnostics["rule"] = rule or None
        diagnostics["status"] = "policy_invalid"
        return None, diagnostics

    policy = {
        "enabled": True,
        "rule": rule,
        "require_contract_coverage": bool(
            payload.get("require_contract_coverage", True)
        ),
        "treat_missing_required_actions_as_covered": bool(
            payload.get("treat_missing_required_actions_as_covered", False)
        ),
        "authority_concept_id": SELECTOR_FAST_PATH_POLICY_CONCEPT_ID,
        "authority_source": "text_relation:hasContent",
    }
    diagnostics["status"] = "policy_resolved"
    diagnostics["rule"] = rule
    return policy, diagnostics


def _annotate_selector_fast_path_coverage(
    payload: Dict[str, Any],
    *,
    policy: Mapping[str, Any],
) -> None:
    """Annotate candidate dicts with structural contract-coverage flags.

    Coverage is a structural subset comparison between the turn contract's
    represented required tools/actions (from the discovery contract
    projection) and each candidate's represented capability metadata
    (``routing_index_metadata.required_tools`` / ``workflow_action_ids``).
    Candidates without represented capability metadata are left unannotated,
    which the selector fast path treats as not covered (fail closed).
    """

    projection = payload.get("contract_projection")
    projection = projection if isinstance(projection, Mapping) else {}
    contract_tools = {
        str(item).strip()
        for item in (projection.get("required_tools") or [])
        if isinstance(item, str) and str(item).strip()
    }
    contract_actions = {
        str(item).strip()
        for item in (projection.get("required_actions") or [])
        if isinstance(item, str) and str(item).strip()
    }
    if not contract_tools:
        payload["selector_fast_path_coverage_status"] = "no_contract_required_tools"
        return

    actions_covered_by_absence = bool(
        not contract_actions
        and policy.get("treat_missing_required_actions_as_covered") is True
    )

    def _annotate(entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        metadata = entry.get("routing_index_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        declared_tools_raw = metadata.get("required_tools")
        if not isinstance(declared_tools_raw, (list, tuple)):
            return
        declared_tools = {
            str(item).strip()
            for item in declared_tools_raw
            if isinstance(item, str) and str(item).strip()
        }
        covers_tools = contract_tools.issubset(declared_tools)
        entry["covers_expected_tool_set"] = covers_tools

        coverage_provenance: Dict[str, Any] = {
            "policy_concept_id": policy.get("authority_concept_id"),
            "tool_coverage_source": "routing_index_metadata.required_tools",
            "contract_required_tools": sorted(contract_tools),
            "candidate_declared_tools": sorted(declared_tools),
        }

        declared_actions_raw = metadata.get("workflow_action_ids")
        if contract_actions:
            if isinstance(declared_actions_raw, (list, tuple)):
                declared_actions = {
                    str(item).strip()
                    for item in declared_actions_raw
                    if isinstance(item, str) and str(item).strip()
                }
                entry["covers_success_contract"] = contract_actions.issubset(
                    declared_actions
                )
                coverage_provenance["action_coverage_source"] = (
                    "routing_index_metadata.workflow_action_ids"
                )
                coverage_provenance["contract_required_actions"] = sorted(
                    contract_actions
                )
        elif actions_covered_by_absence:
            entry["covers_success_contract"] = True
            coverage_provenance["action_coverage_source"] = (
                "represented_policy:treat_missing_required_actions_as_covered"
            )
        entry["selector_fast_path_coverage"] = coverage_provenance

    for key in ("matches", "routing_matches", "candidates"):
        entries = payload.get(key)
        if isinstance(entries, list):
            for entry in entries:
                _annotate(entry)
    payload["selector_fast_path_coverage_status"] = "annotated"


def _attach_selector_fast_path_metadata(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    policy, diagnostics = _resolve_selector_fast_path_policy()
    payload["selector_fast_path_policy_diagnostics"] = diagnostics
    if policy is None:
        return payload
    payload["selector_fast_path_policy"] = policy
    _annotate_selector_fast_path_coverage(payload, policy=policy)
    return payload


def discover_workflows_for_turn(
    user_input: str,
    *,
    namespace: Optional[str] = None,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout_seconds: float | str | None = None,
    allow_non_executable: Optional[bool] = None,
    workflow_registry: Any | None = None,
    expected_outcome_contract: Mapping[str, Any] | None = None,
    requested_query: str | None = None,
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
            expected_outcome_contract=expected_outcome_contract,
            requested_query=requested_query,
        )
        payload = result.to_dict()
        # Self-describing telemetry: every discovery payload records where it
        # came from so downstream diagnostics can explain empty results.
        payload.setdefault("discovery_payload_origin", "discover_workflows_for_turn")
        # Represented selector fast-path metadata (JVNAUTOSCI-2406): stamp the
        # Vontology-authored policy and structural contract-coverage flags so
        # the selector's represented fast path can apply; absence fails closed
        # to the selector LLM.
        payload = _attach_selector_fast_path_metadata(payload)
        return payload

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
            requested_query=(
                requested_query.strip()
                if isinstance(requested_query, str) and requested_query.strip()
                else user_input.strip()
            ),
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
        ).to_dict() | {
            "discovery_payload_origin": (
                "discover_workflows_for_turn_budget_exhausted"
                if budget_exhausted
                else "discover_workflows_for_turn_error"
            ),
        }
