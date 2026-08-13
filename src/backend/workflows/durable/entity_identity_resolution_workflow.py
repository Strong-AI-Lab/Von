"""Durable entity identity-resolution workflow.

The reasoning policy lives in Vontology: the workflow definition itself
(``#V#entity_identity_resolution_workflow``) routes through an LLM rumination
state that consumes the prompt concept
``#V#entity_duplicate_reasoning_prompt``. Python here provides only support
primitives: candidate enumeration, evidence assembly dispatch, and execution
of the LLM's ``identity_recommendations`` (merge / queue-for-review).

This module deliberately performs NO scoring, ranking, classification, or
merge-direction policy. Earlier versions encoded those policies in Python
(``_score_pair``, ``_build_recommendations``, ``_build_clusters``,
``_merge_direction``); they were retired under JVNAUTOSCI-2148 because they
were a textbook AGENTS.md section 4.2 violation - code-side semantic steering for
durable classification and ranking that should be authored.

History: JVNAUTOSCI-1253 (original creation), JVNAUTOSCI-2148 (rumination
authority migration), JVNAUTOSCI-2153 (heuristic retirement).
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
import unicodedata
from typing import Any, Mapping, Sequence

from ...db.repositories.concepts_repository import ConceptsRepository
from ...security.visibility_predicates import (
    get_specific_to_org_values,
    get_specific_to_user_values,
)
from ...services.entity_identity_evidence_service import (
    build_candidate_evidence_pairs,
    DEFAULT_AUTHORED_PAPER_LIMIT,
    DEFAULT_TEXT_RELATION_LIMIT,
)
from ...services.uncertain_relationship_service import (
    upsert_uncertain_relationship_assertion,
)
from ...vontology.utils_vontology import get_concept_display_name_with_names_fallback
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..workflow_registry import WorkflowRegistration

logger = logging.getLogger(__name__)

ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID = "#V#entity_identity_resolution_workflow"
IDENTITY_REVIEW_PREDICATE = "#V#potential_duplicate_of"
IDENTITY_POLICY_VERSION = "entity_identity_resolution_policy.v2_llm_rumination"

ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID = "#V#entity_duplicate_reasoning_prompt"
ENTITY_DUPLICATE_REASONING_PROMPT_LINK_PREDICATE = (
    "#V#hasEntityDuplicateReasoningPrompt"
)

DEFAULT_SCAN_LIMIT = 300
DEFAULT_FOCUSED_SCAN_LIMIT = 1000
DEFAULT_MAX_CANDIDATE_PAIRS = 600
DEFAULT_DETAIL_LIMIT = 120
DEFAULT_MIN_NAME_KEY_LENGTH = 3

VALID_LLM_ACTIONS = {
    "auto_merge",
    "queue_review",
    "leave_distinct",
    "insufficient_evidence",
}
ACTIONABLE_LLM_ACTIONS = {"auto_merge", "queue_review"}


def merge_concepts(
    source_id: str,
    target_id: str,
    *,
    simulate: bool,
    request: WorkflowActionRequest | None = None,
) -> dict[str, Any]:
    """Enter identity consolidation through the governed MCP command boundary."""

    # Import lazily: the catalogue also discovers durable workflow actions at
    # startup. The decorated handler binds the exact plan, delegation, receipt,
    # locks and canonical read-back that a direct primitive call would bypass.
    from ...integrations.internal_mcp.catalogue import _merge_concepts
    from ...services.ontology_publication_authority_service import (
        bind_ontology_invocation,
    )

    if request is None:
        # Compatibility callers and tests still enter the governed handler, but
        # a durable auto-merge needs the request below so its workflow-agent
        # provenance and exact server-issued delegation are bound.
        return _merge_concepts(
            source_id=source_id,
            target_id=target_id,
            simulate=simulate,
        )
    workflow_id = _as_text(request.workflow_id) or (
        ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
    )
    state_id = _as_text(request.workflow_state_id) or "apply"
    effect_id = f"workflow:{workflow_id}:{state_id}:merge_concepts"
    with bind_ontology_invocation(
        surface="workflow",
        executing_agent_concept_id="#V#von_system",
        audience="workflow",
        delegation_id=request.environment.ontology_delegation_id,
        effect_id=effect_id,
        workflow_id=workflow_id,
    ):
        return _merge_concepts(
            source_id=source_id,
            target_id=target_id,
            simulate=simulate,
        )


# ---------------------------------------------------------------------------
# Coercion helpers (kept; primitive - not policy)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [
            str(item).strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
    return []


def _normalise_concept_ids(value: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in _as_string_list(value):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _name_key(value: Any) -> str:
    text = _as_text(value)
    if not text:
        return ""
    stripped = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )
    lowered = stripped.casefold()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    return " ".join(collapsed.split())


def _scope_key(relationships: Mapping[str, Any]) -> str:
    users = sorted(get_specific_to_user_values(dict(relationships)))
    orgs = sorted(get_specific_to_org_values(dict(relationships)))
    return (
        f"u:{','.join(users) if users else 'global'}"
        f"|o:{','.join(orgs) if orgs else 'global'}"
    )


def _infer_kind(concept_doc: Mapping[str, Any]) -> str:
    computed = concept_doc.get("computed_kind")
    if isinstance(computed, str):
        cleaned = computed.strip().lower()
        if cleaned == "individual":
            return "instance"
        if cleaned in {"type", "instance", "predicate"}:
            return cleaned
    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return "unknown"
    instance_of = _as_string_list(relationships.get("is_an_instance_of"))
    if "#V#predicate" in instance_of:
        return "predicate"
    if _as_string_list(relationships.get("is_a_type_of")):
        return "type"
    if instance_of:
        return "instance"
    return "unknown"


# ---------------------------------------------------------------------------
# Candidate enumeration (no scoring - that is the LLM's job)
# ---------------------------------------------------------------------------


def _collect_candidate_name_keys(concept_doc: Mapping[str, Any]) -> list[str]:
    keys: set[str] = set()
    candidates: list[str] = []
    display_name = get_concept_display_name_with_names_fallback(dict(concept_doc))
    if display_name:
        candidates.append(display_name)
    top_name = _as_text(concept_doc.get("name"))
    if top_name:
        candidates.append(top_name)
    for raw in concept_doc.get("names") or []:
        if isinstance(raw, str) and raw.strip():
            candidates.append(raw.strip())
    for candidate in candidates:
        normalised = _name_key(candidate)
        if normalised and len(normalised) >= DEFAULT_MIN_NAME_KEY_LENGTH:
            keys.add(normalised)
    return sorted(keys)


def _enumerate_candidate_pairs(
    *,
    scan_limit: int,
    candidate_concept_ids: Sequence[str],
    candidate_names: Sequence[str],
    max_candidate_pairs: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Walk concepts and emit unordered same-name-key pairs.

    No scoring or decision logic - every emitted pair is a question for the
    LLM rumination stage. The only mechanical filters are: same scope, kind
    must be ``instance``, name keys must be reasonably long.
    """

    focused_concept_ids = _normalise_concept_ids(candidate_concept_ids)
    focused_concept_id_set = set(focused_concept_ids)
    focused_name_keys: set[str] = {
        key
        for key in (_name_key(name) for name in candidate_names)
        if key and len(key) >= DEFAULT_MIN_NAME_KEY_LENGTH
    }

    base_query: dict[str, Any] = {
        "relationships.is_an_instance_of": {"$exists": True, "$ne": []}
    }
    projection = {
        "concept_id": 1,
        "name": 1,
        "names": 1,
        "computed_kind": 1,
        "relationships": 1,
    }

    effective_scan_limit = scan_limit
    if focused_concept_ids:
        effective_scan_limit = max(
            scan_limit,
            min(DEFAULT_FOCUSED_SCAN_LIMIT, len(focused_concept_ids) * 500),
        )

    name_key_index: dict[str, list[dict[str, Any]]] = {}
    indexed_concept_ids: set[str] = set()
    docs_kept = 0
    focused_docs_preloaded = 0

    def _index_candidate_doc(
        concept_id: str,
        keys: Sequence[str],
        relationships: Mapping[str, Any],
    ) -> bool:
        if concept_id in indexed_concept_ids:
            return False
        scope_key = _scope_key(relationships)
        for key in keys:
            name_key_index.setdefault(key, []).append(
                {"concept_id": concept_id, "scope_key": scope_key}
            )
        indexed_concept_ids.add(concept_id)
        return True

    # Pass 1: focused docs. These must be indexed even when their concept IDs
    # fall outside the bounded full-corpus cursor; paper ingest uses this path
    # to ask about newly materialised author concepts.
    if focused_concept_ids:
        focus_cursor = ConceptsRepository.find(
            {"concept_id": {"$in": focused_concept_ids}},
            projection=projection,
        )
        for doc in focus_cursor:
            if isinstance(doc, Mapping) and _infer_kind(doc) == "instance":
                concept_id = _as_text(doc.get("concept_id"))
                keys = _collect_candidate_name_keys(doc)
                if not concept_id or not keys:
                    continue
                for key in keys:
                    focused_name_keys.add(key)
                relationships = doc.get("relationships")
                if not isinstance(relationships, Mapping):
                    relationships = {}
                if _index_candidate_doc(concept_id, keys, relationships):
                    docs_kept += 1
                    focused_docs_preloaded += 1

    # Pass 2: full corpus (bounded). For focused mode we only retain rows
    # whose name keys overlap focused_name_keys (or are themselves the focus).
    docs_scanned = 0
    cursor = ConceptsRepository.find(
        base_query,
        projection=projection,
        sort=[("concept_id", 1)],
        limit=effective_scan_limit,
    )
    for doc in cursor:
        docs_scanned += 1
        if not isinstance(doc, Mapping):
            continue
        if _infer_kind(doc) != "instance":
            continue
        concept_id = _as_text(doc.get("concept_id"))
        if not concept_id:
            continue
        if concept_id in indexed_concept_ids:
            continue

        keys = _collect_candidate_name_keys(doc)
        if not keys:
            continue

        if focused_concept_ids or focused_name_keys:
            # In focused mode require the doc to be either a focus concept or
            # share a name key with a focus.
            is_focus_concept = concept_id in focused_concept_id_set
            shares_focus_name = bool(set(keys) & focused_name_keys)
            if not is_focus_concept and not shares_focus_name:
                continue

        relationships = doc.get("relationships")
        if not isinstance(relationships, Mapping):
            relationships = {}

        if _index_candidate_doc(concept_id, keys, relationships):
            docs_kept += 1

    pairs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    truncated = False

    for key in sorted(name_key_index):
        bucket = name_key_index[key]
        if len(bucket) < 2:
            continue
        bucket_sorted = sorted(bucket, key=lambda row: row["concept_id"])
        for i in range(len(bucket_sorted)):
            for j in range(i + 1, len(bucket_sorted)):
                a = bucket_sorted[i]
                b = bucket_sorted[j]
                if a["concept_id"] == b["concept_id"]:
                    continue
                if a["scope_key"] != b["scope_key"]:
                    continue
                ordered = tuple(sorted([a["concept_id"], b["concept_id"]]))
                if ordered in seen_pairs:
                    continue
                seen_pairs.add(ordered)
                if len(pairs) >= max_candidate_pairs:
                    truncated = True
                    break
                pairs.append(
                    {
                        "a_concept_id": ordered[0],
                        "b_concept_id": ordered[1],
                        "name_key": key,
                        "scope_key": a["scope_key"],
                    }
                )
            if truncated:
                break
        if truncated:
            break

    diagnostics = {
        "docs_scanned": docs_scanned,
        "docs_kept_for_indexing": docs_kept,
        "focused_docs_preloaded": focused_docs_preloaded,
        "name_key_count": len(name_key_index),
        "candidate_pair_count": len(pairs),
        "max_candidate_pairs": max_candidate_pairs,
        "truncated": truncated,
        "focused_mode": bool(focused_concept_ids or focused_name_keys),
        "focused_concept_count": len(focused_concept_ids),
        "focused_name_key_count": len(focused_name_keys),
        "scan_limit_used": effective_scan_limit,
    }
    return pairs, diagnostics


# ---------------------------------------------------------------------------
# LLM-recommendation execution (no policy invented; we only execute what the
# LLM authored)
# ---------------------------------------------------------------------------


def _coerce_recommendations(value: Any, *, max_items: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value[:max_items]:
        if isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _extract_recommendations_from_payload(payload: Any) -> Any:
    """Accept either a top-level list, a dict containing ``identity_recommendations``,
    or the engine's ``validated_json`` envelope.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        if "identity_recommendations" in payload:
            return payload.get("identity_recommendations")
        validated = payload.get("validated_json")
        if isinstance(validated, Mapping):
            return validated.get("identity_recommendations")
        if isinstance(validated, list):
            return validated
    return None


def _queue_uncertain(
    source_id: str,
    target_id: str,
    recommendation: Mapping[str, Any],
) -> dict[str, Any]:
    return upsert_uncertain_relationship_assertion(
        source_id=source_id,
        predicate=IDENTITY_REVIEW_PREDICATE,
        target=target_id,
        confidence_score=_coerce_float(
            recommendation.get("confidence"),
            default=0.0,
            minimum=0.0,
            maximum=1.0,
        ),
        provenance={
            "source": "entity_identity_resolution_workflow",
            "workflow_id": ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
            "policy_version": IDENTITY_POLICY_VERSION,
            "rumination_prompt": ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
            "rationale": _as_text(recommendation.get("rationale"))[:1500],
            "evidence_refs": list(recommendation.get("evidence_refs") or [])[:20],
            "name_key": recommendation.get("name_key"),
            "generated_at_utc": _utc_now_iso(),
        },
        status="proposed",
        evidence_count=max(1, len(list(recommendation.get("evidence_refs") or []))),
    )


def _record_merge_audit(
    *,
    source_id: str,
    target_id: str,
    recommendation: Mapping[str, Any],
    merge_result: Mapping[str, Any],
) -> None:
    event = {
        "event_type": "identity_resolution_auto_merge",
        "recorded_at_utc": _utc_now_iso(),
        "source_id": source_id,
        "target_id": target_id,
        "confidence": _coerce_float(
            recommendation.get("confidence"),
            default=0.0,
            minimum=0.0,
            maximum=1.0,
        ),
        "policy_version": IDENTITY_POLICY_VERSION,
        "rumination_prompt": ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
        "rationale": _as_text(recommendation.get("rationale"))[:2000],
        "evidence_refs": list(recommendation.get("evidence_refs") or [])[:20],
        "merge_success": bool(merge_result.get("success")),
        "merge_operations_count": len(list(merge_result.get("operations") or [])),
    }
    ConceptsRepository.update_one(
        {"concept_id": target_id},
        {"$push": {"identity_resolution_audit": {"$each": [event], "$slice": -400}}},
    )


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------


def build_entity_identity_resolution_workflow_test_definition() -> WorkflowDefinition:
    """Build the workflow definition used by tests and as a built-in fallback.

    The authoritative definition lives in
    ``canonical_workflow_publication_seed_bundle.json`` and is published into
    Vontology at backend startup. This Python definition mirrors the same
    structure so that the in-process registry exposes the workflow even if
    Vontology bootstrap has not yet run.
    """

    scan = WorkflowStateSpec(
        state_id="scan",
        actions=(
            WorkflowActionInvocation(
                action_id="identity_resolution.scan_candidates",
                description=(
                    "Enumerate same-name candidate pairs (no scoring; LLM owns reasoning)."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="gather_evidence",
                condition=lambda ctx: bool(ctx.get("candidate_pairs")),
                reason="candidate_pairs_present",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="no_candidate_pairs",
            ),
        ),
    )

    gather_evidence = WorkflowStateSpec(
        state_id="gather_evidence",
        actions=(
            WorkflowActionInvocation(
                action_id="identity_resolution.gather_evidence",
                description=(
                    "Assemble per-pair evidence profiles for the LLM rumination stage."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="reason_about_identity",
                condition=lambda ctx: bool(ctx.get("candidate_evidence_pairs")),
                reason="evidence_ready",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="evidence_unavailable",
            ),
        ),
    )

    reason_about_identity = WorkflowStateSpec(
        state_id="reason_about_identity",
        actions=(
            WorkflowActionInvocation(
                action_id="llm.action",
                execution_mode="llm",
                prompt_contract={
                    "requested_prompt_concept_ids": [
                        ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
                    ],
                },
                llm_policy={
                    "policy_stage": "entity_duplicate_reasoning",
                    "tool_mode": "disallowed",
                    "context_fields": [
                        "candidate_evidence_pairs",
                        "candidate_evidence_diagnostics",
                        "identity_resolution_scan_summary",
                    ],
                    "response_contract_text": (
                        "Return JSON of the form "
                        '{"identity_recommendations": [...]}, where each '
                        "recommendation has pair_ids, action (auto_merge | "
                        "queue_review | leave_distinct | insufficient_evidence), "
                        "source_id, target_id, confidence (0..1), rationale, "
                        "evidence_refs."
                    ),
                },
                validation_policy={"output_format": "json_value"},
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="apply",
                condition=lambda _ctx: True,
                reason="reasoning_complete",
            ),
        ),
        metadata={
            "tool_output_context_mappings": [
                {
                    "tool_output_field": "validated_json",
                    "context_key": "identity_reasoning_payload",
                }
            ],
            "writes_context_keys": ["identity_reasoning_payload"],
        },
    )

    apply = WorkflowStateSpec(
        state_id="apply",
        actions=(
            WorkflowActionInvocation(
                action_id="identity_resolution.apply_resolutions",
                description=(
                    "Execute the LLM's identity recommendations: merge or queue for review."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="apply_complete",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="identity_resolution.finalise",
                description="Summarise outcomes.",
            ),
        ),
        terminal=True,
    )

    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        initial_state="scan",
        states={
            "scan": scan,
            "gather_evidence": gather_evidence,
            "reason_about_identity": reason_about_identity,
            "apply": apply,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Background duplicate-entity detection with LLM-authored "
            "rumination over assembled evidence; provenance maintenance."
        ),
    )


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _handle_scan_candidates(request: WorkflowActionRequest) -> WorkflowActionResult:
    ctx = request.data
    scan_limit = _coerce_int(
        ctx.get("scan_limit"),
        default=DEFAULT_SCAN_LIMIT,
        minimum=20,
        maximum=2000,
    )
    max_candidate_pairs = _coerce_int(
        ctx.get("max_candidate_pairs"),
        default=DEFAULT_MAX_CANDIDATE_PAIRS,
        minimum=10,
        maximum=5000,
    )

    candidate_concept_ids = _normalise_concept_ids(ctx.get("candidate_concepts"))
    candidate_names = _as_string_list(ctx.get("candidate_names"))
    if not candidate_concept_ids:
        candidate_concept_ids = _normalise_concept_ids(ctx.get("candidate_concept_ids"))

    pairs, diagnostics = _enumerate_candidate_pairs(
        scan_limit=scan_limit,
        candidate_concept_ids=candidate_concept_ids,
        candidate_names=candidate_names,
        max_candidate_pairs=max_candidate_pairs,
    )

    summary = {
        "candidate_pair_count": len(pairs),
        "focused_mode": diagnostics["focused_mode"],
        "candidate_concept_count": len(candidate_concept_ids),
        "candidate_name_hint_count": len(candidate_names),
        "policy_version": IDENTITY_POLICY_VERSION,
        "generated_at_utc": _utc_now_iso(),
    }

    logger.info(
        "[identity_resolution] scan complete: pairs=%d focused=%s",
        len(pairs),
        diagnostics["focused_mode"],
    )

    return WorkflowActionResult(
        outputs={
            "candidate_pairs": pairs,
            "identity_resolution_scan_summary": summary,
            "identity_resolution_scan_diagnostics": diagnostics,
            "scan_limit_used": diagnostics["scan_limit_used"],
            "max_candidate_pairs_used": max_candidate_pairs,
        }
    )


def _handle_gather_evidence(request: WorkflowActionRequest) -> WorkflowActionResult:
    ctx = request.data
    text_relation_limit = _coerce_int(
        ctx.get("text_relation_limit"),
        default=DEFAULT_TEXT_RELATION_LIMIT,
        minimum=10,
        maximum=500,
    )
    authored_paper_limit = _coerce_int(
        ctx.get("authored_paper_limit"),
        default=DEFAULT_AUTHORED_PAPER_LIMIT,
        minimum=5,
        maximum=200,
    )
    candidate_pairs_raw = ctx.get("candidate_pairs") or []
    if not isinstance(candidate_pairs_raw, list):
        candidate_pairs_raw = []

    enriched_pairs, diagnostics = build_candidate_evidence_pairs(
        candidate_pairs_raw,
        text_relation_limit=text_relation_limit,
        authored_paper_limit=authored_paper_limit,
    )

    logger.info(
        "[identity_resolution] gather_evidence complete: pairs_in=%d pairs_out=%d concepts_profiled=%d",
        diagnostics["candidate_pair_count_in"],
        diagnostics["evidence_pair_count_out"],
        diagnostics["unique_concepts_profiled"],
    )

    return WorkflowActionResult(
        outputs={
            "candidate_evidence_pairs": enriched_pairs,
            "candidate_evidence_diagnostics": diagnostics,
        }
    )


def _handle_apply_resolutions(request: WorkflowActionRequest) -> WorkflowActionResult:
    ctx = request.data
    dry_run = _coerce_bool(ctx.get("dry_run"), default=False)
    detail_limit = _coerce_int(
        ctx.get("detail_limit"),
        default=DEFAULT_DETAIL_LIMIT,
        minimum=20,
        maximum=400,
    )

    payload = ctx.get("identity_reasoning_payload")
    raw_recommendations = _extract_recommendations_from_payload(payload)
    recommendations = _coerce_recommendations(
        raw_recommendations, max_items=DEFAULT_MAX_CANDIDATE_PAIRS
    )

    summary_base = {
        "policy_version": IDENTITY_POLICY_VERSION,
        "rumination_prompt": ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
        "completed_at_utc": _utc_now_iso(),
        "dry_run": dry_run,
    }

    # Fail closed if the LLM stage produced no usable output. We do NOT fall
    # back to Python heuristics - the rumination stage IS the policy.
    if raw_recommendations is None and not isinstance(payload, (Mapping, list)):
        logger.warning(
            "[identity_resolution] apply: no identity_reasoning_payload present; failing closed."
        )
        return WorkflowActionResult(
            outputs={
                "identity_resolution_apply_summary": {
                    **summary_base,
                    "merged_count": 0,
                    "queued_count": 0,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "would_merge_count": 0,
                    "would_queue_count": 0,
                    "fail_closed_reason": "missing_identity_reasoning_payload",
                },
                "identity_resolution_apply_details": [],
            }
        )

    merged_count = 0
    queued_count = 0
    failed_count = 0
    skipped_count = 0
    would_merge_count = 0
    would_queue_count = 0
    malformed_count = 0
    consumed_sources: set[str] = set()
    details: list[dict[str, Any]] = []

    for rec in recommendations:
        action = _as_text(rec.get("action")).lower()
        if action not in VALID_LLM_ACTIONS:
            malformed_count += 1
            continue
        if action not in ACTIONABLE_LLM_ACTIONS:
            skipped_count += 1
            continue

        source_id = _as_text(rec.get("source_id"))
        target_id = _as_text(rec.get("target_id"))
        if not source_id or not target_id or source_id == target_id:
            malformed_count += 1
            continue
        if source_id in consumed_sources:
            skipped_count += 1
            continue

        confidence = _coerce_float(
            rec.get("confidence"), default=0.0, minimum=0.0, maximum=1.0
        )
        detail = {
            "source_id": source_id,
            "target_id": target_id,
            "confidence": round(confidence, 4),
            "action": action,
            "dry_run": dry_run,
        }

        if action == "auto_merge":
            if dry_run:
                would_merge_count += 1
                detail["outcome"] = "would_merge"
            else:
                merge_result = merge_concepts(
                    source_id,
                    target_id,
                    simulate=False,
                    request=request,
                )
                if bool(merge_result.get("success")):
                    merged_count += 1
                    consumed_sources.add(source_id)
                    detail["outcome"] = "merged"
                    detail["merge_operations_count"] = len(
                        list(merge_result.get("operations") or [])
                    )
                    try:
                        _record_merge_audit(
                            source_id=source_id,
                            target_id=target_id,
                            recommendation=rec,
                            merge_result=merge_result,
                        )
                    except Exception as audit_exc:
                        detail["audit_error"] = str(audit_exc)
                else:
                    failed_count += 1
                    detail["outcome"] = "merge_failed"
                    detail["merge_error"] = merge_result.get(
                        "errors"
                    ) or merge_result.get("error")
                    queue_result = _queue_uncertain(source_id, target_id, rec)
                    if bool(queue_result.get("success")):
                        queued_count += 1
                        detail["fallback_queue_outcome"] = "queued_for_review"
                    else:
                        detail["fallback_queue_outcome"] = "queue_failed"
                        detail["fallback_queue_error"] = queue_result.get("error")
        else:  # queue_review
            if dry_run:
                would_queue_count += 1
                detail["outcome"] = "would_queue_review"
            else:
                queue_result = _queue_uncertain(source_id, target_id, rec)
                if bool(queue_result.get("success")):
                    queued_count += 1
                    detail["outcome"] = "queued_for_review"
                    assertion = queue_result.get("assertion")
                    if isinstance(assertion, Mapping):
                        detail["assertion_id"] = assertion.get("assertion_id")
                else:
                    failed_count += 1
                    detail["outcome"] = "queue_failed"
                    detail["queue_error"] = queue_result.get("error")

        if len(details) < detail_limit:
            details.append(detail)

    summary = {
        **summary_base,
        "merged_count": merged_count,
        "queued_count": queued_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "would_merge_count": would_merge_count,
        "would_queue_count": would_queue_count,
        "malformed_recommendation_count": malformed_count,
        "recommendation_count": len(recommendations),
    }

    logger.info(
        "[identity_resolution] apply complete: merged=%d queued=%d failed=%d malformed=%d",
        merged_count,
        queued_count,
        failed_count,
        malformed_count,
    )

    return WorkflowActionResult(
        outputs={
            "identity_resolution_apply_summary": summary,
            "identity_resolution_apply_details": details,
        }
    )


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    ctx = request.data
    scan_summary = dict(ctx.get("identity_resolution_scan_summary") or {})
    apply_summary = dict(ctx.get("identity_resolution_apply_summary") or {})
    result = {
        "workflow_id": ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        "policy_version": IDENTITY_POLICY_VERSION,
        "rumination_prompt": ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
        "scan_summary": scan_summary,
        "apply_summary": apply_summary,
        "merged_count": apply_summary.get("merged_count", 0),
        "queued_count": apply_summary.get("queued_count", 0),
        "candidate_pair_count": scan_summary.get("candidate_pair_count", 0),
        "generated_at_utc": _utc_now_iso(),
    }
    return WorkflowActionResult(outputs={"identity_resolution_result": result})


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


def build_entity_identity_resolution_workflow_test_registration() -> (
    WorkflowRegistration
):
    return WorkflowRegistration(
        workflow_id=ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        definition=build_entity_identity_resolution_workflow_test_definition(),
        purpose=(
            "Background duplicate-entity detection with LLM-authored "
            "rumination over assembled evidence; provenance maintenance."
        ),
        source="built_in",
    )


def register_entity_identity_resolution_actions(registry: ActionRegistry) -> None:
    actions = [
        ActionSpec(
            action_id="identity_resolution.scan_candidates",
            handler=_handle_scan_candidates,
            description=(
                "Enumerate same-name candidate pairs (no scoring; LLM owns reasoning)."
            ),
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="identity_resolution.gather_evidence",
            handler=_handle_gather_evidence,
            description=(
                "Assemble per-pair evidence profiles for the LLM rumination stage."
            ),
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="identity_resolution.apply_resolutions",
            handler=_handle_apply_resolutions,
            description=(
                "Execute LLM-authored identity recommendations (merge or queue)."
            ),
            side_effects="write",
        ),
        ActionSpec(
            action_id="identity_resolution.finalise",
            handler=_handle_finalise,
            description="Summarise identity-resolution outcomes.",
            side_effects="none",
        ),
    ]
    for spec in actions:
        try:
            registry.register(spec)
        except ValueError:
            pass
