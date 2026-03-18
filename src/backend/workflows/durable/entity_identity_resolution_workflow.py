"""Durable entity identity-resolution workflow (JVNAUTOSCI-1253)."""

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
from ...services.concept_merge_service import merge_concepts
from ...services.text_value_service import get_texts_for_concept
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
IDENTITY_POLICY_VERSION = "entity_identity_resolution_policy.v1"

DEFAULT_SCAN_LIMIT = 300
DEFAULT_TEXT_SCAN_LIMIT = 100
DEFAULT_MAX_PAIR_EVAL = 600
DEFAULT_AUTO_MERGE_THRESHOLD = 0.93
DEFAULT_REVIEW_THRESHOLD = 0.72
DEFAULT_DETAIL_LIMIT = 120

_NAME_PREDICATES = {"hasname", "vhasname"}
_SOURCE_HINTS = (
    "source",
    "url",
    "email",
    "doi",
    "orcid",
    "github",
    "jira",
    "slack",
    "arxiv",
)


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


def _source_key(value: Any) -> str:
    text = _as_text(value).casefold()
    if not text:
        return ""
    text = re.sub(r"^https?://", "", text)
    text = text.rstrip("/")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _predicate_key(value: Any) -> str:
    raw = _as_text(value).casefold()
    return raw.replace("#", "").replace("_", "").replace("-", "")


def _extract_relationship_targets(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned.startswith("#V#") else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_extract_relationship_targets(item))
        return out
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_extract_relationship_targets(item))
        return out
    return []


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


def _profile_weight(profile: Mapping[str, Any]) -> int:
    return (
        len(profile.get("name_keys") or []) * 2
        + len(profile.get("source_refs") or []) * 3
        + len(profile.get("relationship_targets") or [])
        + len(profile.get("type_ids") or [])
    )


def _merge_direction(a: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[str, str]:
    a_id = str(a.get("concept_id") or "")
    b_id = str(b.get("concept_id") or "")
    if _profile_weight(a) > _profile_weight(b):
        return b_id, a_id
    if _profile_weight(b) > _profile_weight(a):
        return a_id, b_id
    return (b_id, a_id) if a_id <= b_id else (a_id, b_id)


def _build_profile(
    concept_doc: Mapping[str, Any],
    *,
    text_limit: int,
) -> dict[str, Any] | None:
    concept_id = _as_text(concept_doc.get("concept_id"))
    if not concept_id:
        return None

    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        relationships = {}

    if _infer_kind(concept_doc) != "instance":
        return None

    type_ids = sorted(
        {
            type_id
            for type_id in _as_string_list(relationships.get("is_an_instance_of"))
            if type_id and type_id != "#V#predicate"
        }
    )
    if not type_ids:
        return None

    names: set[str] = set()
    display_name = get_concept_display_name_with_names_fallback(dict(concept_doc))
    if display_name:
        names.add(display_name)
    top_name = _as_text(concept_doc.get("name"))
    if top_name:
        names.add(top_name)

    source_refs: set[str] = set()
    try:
        text_rows = get_texts_for_concept(concept_id, limit=text_limit)
    except Exception as exc:
        logger.debug("[identity_resolution] text scan failed for %s: %s", concept_id, exc)
        text_rows = []

    for row in text_rows:
        if not isinstance(row, Mapping):
            continue
        text_value = _as_text(row.get("text"))
        if not text_value:
            continue
        pred_raw = str(row.get("predicate") or "").casefold()
        pred_key = _predicate_key(row.get("predicate"))
        if pred_key in _NAME_PREDICATES:
            names.add(text_value)
        if any(token in pred_raw for token in _SOURCE_HINTS):
            source_value = _source_key(text_value)
            if source_value:
                source_refs.add(source_value)

    for predicate, raw_val in relationships.items():
        if any(token in str(predicate or "").casefold() for token in _SOURCE_HINTS):
            for candidate in _as_string_list(raw_val):
                source_value = _source_key(candidate)
                if source_value:
                    source_refs.add(source_value)

    name_keys = sorted({k for k in (_name_key(name) for name in names) if k and len(k) >= 3})
    if not name_keys:
        return None

    rel_targets = sorted(
        {
            value
            for value in _extract_relationship_targets(relationships)
            if value and value != concept_id
        }
    )

    return {
        "concept_id": concept_id,
        "display_name": display_name or top_name or concept_id,
        "type_ids": type_ids,
        "scope_key": _scope_key(relationships),
        "name_keys": name_keys,
        "source_refs": sorted(source_refs),
        "relationship_targets": rel_targets,
    }


def _scan_profiles(*, scan_limit: int, text_limit: int) -> list[dict[str, Any]]:
    cursor = ConceptsRepository.find(
        {"relationships.is_an_instance_of": {"$exists": True, "$ne": []}},
        projection={
            "concept_id": 1,
            "name": 1,
            "names": 1,
            "computed_kind": 1,
            "relationships": 1,
        },
        sort=[("concept_id", 1)],
        limit=scan_limit,
    )
    profiles: list[dict[str, Any]] = []
    for concept_doc in cursor:
        if not isinstance(concept_doc, Mapping):
            continue
        profile = _build_profile(concept_doc, text_limit=text_limit)
        if profile is not None:
            profiles.append(profile)
    return profiles


def _score_pair(
    *,
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    name_key: str,
    auto_threshold: float,
    review_threshold: float,
) -> dict[str, Any] | None:
    a_id = str(a.get("concept_id") or "")
    b_id = str(b.get("concept_id") or "")
    if not a_id or not b_id or a_id == b_id:
        return None
    if str(a.get("scope_key") or "") != str(b.get("scope_key") or ""):
        return None

    shared_types = sorted(
        set(str(v) for v in (a.get("type_ids") or [])).intersection(
            str(v) for v in (b.get("type_ids") or [])
        )
    )
    shared_sources = sorted(
        set(str(v) for v in (a.get("source_refs") or [])).intersection(
            str(v) for v in (b.get("source_refs") or [])
        )
    )
    shared_targets = sorted(
        set(str(v) for v in (a.get("relationship_targets") or [])).intersection(
            str(v) for v in (b.get("relationship_targets") or [])
        )
    )

    score = 0.62
    rationale = [f"Shared normalised name key '{name_key}'."]

    if shared_types:
        score += min(0.14, 0.08 + 0.01 * len(shared_types))
        rationale.append("Shared type context.")
    if shared_sources:
        score += min(0.24, 0.12 + 0.04 * len(shared_sources))
        rationale.append("Shared source references.")
    if shared_targets:
        score += min(0.14, 0.04 + 0.02 * len(shared_targets))
        rationale.append("Shared relationship neighbourhood.")
    if not shared_sources and not shared_targets:
        score -= 0.08
        rationale.append("No shared source/relationship evidence; applying caution.")

    # Short person-name pairs are high-collision; require stronger support.
    if "#V#person" in shared_types and len(name_key.split()) <= 2 and not shared_sources:
        score -= 0.08
        rationale.append("Common short person-name collision risk.")

    score = max(0.0, min(0.999, score))
    evidence_count = (
        int(bool(shared_types)) + int(bool(shared_sources)) + int(bool(shared_targets))
    )

    if score >= auto_threshold and evidence_count >= 1:
        action = "auto_merge"
    elif score >= review_threshold:
        action = "queue_review"
    else:
        action = "ignore"

    source_id, target_id = _merge_direction(a, b)
    return {
        "source_id": source_id,
        "target_id": target_id,
        "pair_ids": sorted([a_id, b_id]),
        "name_key": name_key,
        "confidence_score": round(score, 4),
        "action": action,
        "evidence_count": evidence_count,
        "shared_type_ids": shared_types[:8],
        "shared_source_references": shared_sources[:8],
        "shared_relationship_targets": shared_targets[:8],
        "rationale": rationale[:6],
        "policy_version": IDENTITY_POLICY_VERSION,
        "generated_at_utc": _utc_now_iso(),
    }


def _build_recommendations(
    *,
    profiles: Sequence[Mapping[str, Any]],
    max_pair_eval: int,
    auto_threshold: float,
    review_threshold: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id = {
        str(profile.get("concept_id") or ""): profile
        for profile in profiles
        if str(profile.get("concept_id") or "")
    }

    name_index: dict[str, list[str]] = {}
    for concept_id, profile in by_id.items():
        for key in profile.get("name_keys") or []:
            if isinstance(key, str) and key:
                name_index.setdefault(key, []).append(concept_id)

    pair_map: dict[tuple[str, str], dict[str, Any]] = {}
    evaluated = 0
    truncated = False

    for name_key in sorted(name_index):
        ids = sorted(set(name_index[name_key]))
        if len(ids) < 2:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pair_key = (ids[i], ids[j])
                if pair_key not in pair_map and evaluated >= max_pair_eval:
                    truncated = True
                    break
                candidate = _score_pair(
                    a=by_id[pair_key[0]],
                    b=by_id[pair_key[1]],
                    name_key=name_key,
                    auto_threshold=auto_threshold,
                    review_threshold=review_threshold,
                )
                evaluated += 1
                if candidate is None:
                    continue
                current = pair_map.get(pair_key)
                if current is None or float(candidate.get("confidence_score") or 0.0) > float(
                    current.get("confidence_score") or 0.0
                ):
                    pair_map[pair_key] = candidate
            if truncated:
                break
        if truncated:
            break

    recommendations = sorted(
        pair_map.values(),
        key=lambda row: (
            -float(row.get("confidence_score") or 0.0),
            str(row.get("source_id") or ""),
            str(row.get("target_id") or ""),
        ),
    )
    diagnostics = {
        "profile_count": len(by_id),
        "name_key_count": len(name_index),
        "evaluated_pairs": evaluated,
        "max_pair_evaluations": max_pair_eval,
        "truncated": truncated,
    }
    return recommendations, diagnostics


def _build_clusters(
    recommendations: Sequence[Mapping[str, Any]],
    *,
    min_confidence: float,
) -> list[dict[str, Any]]:
    parent: dict[str, str] = {}
    rank: dict[str, int] = {}
    edges: list[dict[str, Any]] = []

    def _find(node: str) -> str:
        root = parent.setdefault(node, node)
        if root != node:
            parent[node] = _find(root)
        return parent[node]

    def _union(a: str, b: str) -> None:
        ra = _find(a)
        rb = _find(b)
        if ra == rb:
            return
        wa = rank.get(ra, 0)
        wb = rank.get(rb, 0)
        if wa < wb:
            parent[ra] = rb
            return
        if wa > wb:
            parent[rb] = ra
            return
        parent[rb] = ra
        rank[ra] = wa + 1

    for rec in recommendations:
        confidence = float(rec.get("confidence_score") or 0.0)
        if confidence < min_confidence:
            continue
        if str(rec.get("action") or "") == "ignore":
            continue
        source_id = _as_text(rec.get("source_id"))
        target_id = _as_text(rec.get("target_id"))
        if not source_id or not target_id or source_id == target_id:
            continue
        _union(source_id, target_id)
        edges.append(
            {
                "source_id": source_id,
                "target_id": target_id,
                "confidence_score": round(confidence, 4),
                "name_key": _as_text(rec.get("name_key")),
                "action": str(rec.get("action") or ""),
            }
        )

    nodes_by_root: dict[str, set[str]] = {}
    for node in parent:
        nodes_by_root.setdefault(_find(node), set()).add(node)

    clusters: list[dict[str, Any]] = []
    for root, nodes in sorted(nodes_by_root.items(), key=lambda item: sorted(item[1])):
        if len(nodes) < 2:
            continue
        cluster_edges = [
            edge
            for edge in edges
            if edge["source_id"] in nodes and edge["target_id"] in nodes
        ]
        max_confidence = max(
            (float(edge["confidence_score"]) for edge in cluster_edges),
            default=0.0,
        )
        clusters.append(
            {
                "cluster_id": f"dup_cluster:{root}",
                "concept_ids": sorted(nodes),
                "pair_count": len(cluster_edges),
                "max_confidence_score": round(max_confidence, 4),
                "edges": cluster_edges[:20],
            }
        )
    return clusters


def _queue_uncertain(
    source_id: str,
    target_id: str,
    recommendation: Mapping[str, Any],
) -> dict[str, Any]:
    return upsert_uncertain_relationship_assertion(
        source_id=source_id,
        predicate=IDENTITY_REVIEW_PREDICATE,
        target=target_id,
        confidence_score=float(recommendation.get("confidence_score") or 0.0),
        provenance={
            "source": "entity_identity_resolution_workflow",
            "workflow_id": ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
            "policy_version": recommendation.get("policy_version")
            or IDENTITY_POLICY_VERSION,
            "name_key": recommendation.get("name_key"),
            "rationale": list(recommendation.get("rationale") or [])[:6],
            "generated_at_utc": recommendation.get("generated_at_utc")
            or _utc_now_iso(),
        },
        status="proposed",
        evidence_count=_coerce_int(
            recommendation.get("evidence_count"),
            default=1,
            minimum=1,
            maximum=100,
        ),
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
        "confidence_score": float(recommendation.get("confidence_score") or 0.0),
        "policy_version": recommendation.get("policy_version") or IDENTITY_POLICY_VERSION,
        "rationale": list(recommendation.get("rationale") or [])[:6],
        "shared_source_references": list(
            recommendation.get("shared_source_references") or []
        )[:8],
        "shared_relationship_targets": list(
            recommendation.get("shared_relationship_targets") or []
        )[:8],
        "merge_success": bool(merge_result.get("success")),
        "merge_operations_count": len(list(merge_result.get("operations") or [])),
    }
    ConceptsRepository.update_one(
        {"concept_id": target_id},
        {
            "$push": {
                "identity_resolution_audit": {"$each": [event], "$slice": -400}
            }
        },
    )


def _coerce_recommendations(value: Any, *, max_items: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value[:max_items]:
        if isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _cluster_count_after_apply(
    *,
    scan_limit: int,
    text_limit: int,
    max_pair_eval: int,
    auto_threshold: float,
    review_threshold: float,
) -> int:
    profiles = _scan_profiles(scan_limit=scan_limit, text_limit=text_limit)
    recommendations, _diagnostics = _build_recommendations(
        profiles=profiles,
        max_pair_eval=max_pair_eval,
        auto_threshold=auto_threshold,
        review_threshold=review_threshold,
    )
    clusters = _build_clusters(recommendations, min_confidence=review_threshold)
    return len(clusters)


def build_entity_identity_resolution_workflow_test_definition() -> WorkflowDefinition:
    scan = WorkflowStateSpec(
        state_id="scan",
        actions=(
            WorkflowActionInvocation(
                action_id="identity_resolution.scan_candidates",
                description=(
                    "Detect duplicate clusters and confidence-scored recommendations."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="apply",
                condition=lambda ctx: bool(ctx.get("duplicate_recommendations")),
                reason="recommendations_ready",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="nothing_to_apply",
            ),
        ),
    )

    apply = WorkflowStateSpec(
        state_id="apply",
        actions=(
            WorkflowActionInvocation(
                action_id="identity_resolution.apply_resolutions",
                description=(
                    "Auto-merge high-confidence duplicates and queue uncertain cases."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: True,
                reason="apply_complete",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="identity_resolution.finalise",
                description="Summarise outcomes and reduction metrics.",
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
            "apply": apply,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Background duplicate-entity detection, confidence-scored resolution, "
            "and provenance maintenance."
        ),
    )


def _handle_scan_candidates(request: WorkflowActionRequest) -> WorkflowActionResult:
    ctx = request.data
    scan_limit = _coerce_int(
        ctx.get("scan_limit"),
        default=DEFAULT_SCAN_LIMIT,
        minimum=20,
        maximum=2000,
    )
    text_limit = _coerce_int(
        ctx.get("text_relation_limit"),
        default=DEFAULT_TEXT_SCAN_LIMIT,
        minimum=20,
        maximum=500,
    )
    max_pair_eval = _coerce_int(
        ctx.get("max_pair_evaluations"),
        default=DEFAULT_MAX_PAIR_EVAL,
        minimum=20,
        maximum=5000,
    )
    auto_threshold = _coerce_float(
        ctx.get("auto_apply_confidence_threshold"),
        default=DEFAULT_AUTO_MERGE_THRESHOLD,
        minimum=0.5,
        maximum=0.999,
    )
    review_threshold = _coerce_float(
        ctx.get("review_confidence_threshold"),
        default=DEFAULT_REVIEW_THRESHOLD,
        minimum=0.3,
        maximum=0.999,
    )
    if review_threshold > auto_threshold:
        review_threshold = auto_threshold

    profiles = _scan_profiles(scan_limit=scan_limit, text_limit=text_limit)
    recommendations, diagnostics = _build_recommendations(
        profiles=profiles,
        max_pair_eval=max_pair_eval,
        auto_threshold=auto_threshold,
        review_threshold=review_threshold,
    )
    clusters = _build_clusters(recommendations, min_confidence=review_threshold)
    actionable = [r for r in recommendations if str(r.get("action") or "") != "ignore"]

    summary = {
        "scanned_profiles": len(profiles),
        "recommendation_count": len(recommendations),
        "actionable_recommendation_count": len(actionable),
        "auto_merge_candidate_count": len(
            [r for r in actionable if str(r.get("action")) == "auto_merge"]
        ),
        "review_queue_candidate_count": len(
            [r for r in actionable if str(r.get("action")) == "queue_review"]
        ),
        "duplicate_cluster_count": len(clusters),
        "auto_apply_confidence_threshold": auto_threshold,
        "review_confidence_threshold": review_threshold,
        "policy_version": IDENTITY_POLICY_VERSION,
        "generated_at_utc": _utc_now_iso(),
    }

    logger.info(
        "[identity_resolution] scan complete: profiles=%d recommendations=%d actionable=%d clusters=%d",
        len(profiles),
        len(recommendations),
        len(actionable),
        len(clusters),
    )

    return WorkflowActionResult(
        outputs={
            "identity_resolution_scan_summary": summary,
            "identity_resolution_scan_diagnostics": diagnostics,
            "duplicate_recommendations": recommendations[:DEFAULT_DETAIL_LIMIT],
            "duplicate_clusters": clusters[:DEFAULT_DETAIL_LIMIT],
            "duplicate_cluster_count_before": len(clusters),
            "scan_limit_used": scan_limit,
            "text_relation_limit_used": text_limit,
            "max_pair_evaluations_used": max_pair_eval,
            "auto_apply_confidence_threshold": auto_threshold,
            "review_confidence_threshold": review_threshold,
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

    scan_limit = _coerce_int(
        ctx.get("scan_limit_used"),
        default=DEFAULT_SCAN_LIMIT,
        minimum=20,
        maximum=2000,
    )
    text_limit = _coerce_int(
        ctx.get("text_relation_limit_used"),
        default=DEFAULT_TEXT_SCAN_LIMIT,
        minimum=20,
        maximum=500,
    )
    max_pair_eval = _coerce_int(
        ctx.get("max_pair_evaluations_used"),
        default=DEFAULT_MAX_PAIR_EVAL,
        minimum=20,
        maximum=5000,
    )
    auto_threshold = _coerce_float(
        ctx.get("auto_apply_confidence_threshold"),
        default=DEFAULT_AUTO_MERGE_THRESHOLD,
        minimum=0.5,
        maximum=0.999,
    )
    review_threshold = _coerce_float(
        ctx.get("review_confidence_threshold"),
        default=DEFAULT_REVIEW_THRESHOLD,
        minimum=0.3,
        maximum=0.999,
    )
    if review_threshold > auto_threshold:
        review_threshold = auto_threshold

    recommendations = _coerce_recommendations(
        ctx.get("duplicate_recommendations"),
        max_items=max_pair_eval,
    )
    recommendations.sort(
        key=lambda row: (
            -float(row.get("confidence_score") or 0.0),
            str(row.get("source_id") or ""),
            str(row.get("target_id") or ""),
        )
    )

    merged_count = 0
    queued_count = 0
    failed_count = 0
    skipped_count = 0
    would_merge_count = 0
    would_queue_count = 0
    consumed_sources: set[str] = set()
    details: list[dict[str, Any]] = []

    for rec in recommendations:
        source_id = _as_text(rec.get("source_id"))
        target_id = _as_text(rec.get("target_id"))
        if not source_id or not target_id or source_id == target_id:
            skipped_count += 1
            continue
        if source_id in consumed_sources:
            skipped_count += 1
            continue

        confidence = _coerce_float(
            rec.get("confidence_score"),
            default=0.0,
            minimum=0.0,
            maximum=1.0,
        )
        action = str(rec.get("action") or "").strip().lower()
        if not action:
            if confidence >= auto_threshold:
                action = "auto_merge"
            elif confidence >= review_threshold:
                action = "queue_review"
            else:
                action = "ignore"

        if action == "ignore":
            skipped_count += 1
            continue

        detail = {
            "source_id": source_id,
            "target_id": target_id,
            "confidence_score": round(confidence, 4),
            "action": action,
            "dry_run": dry_run,
        }

        if action == "auto_merge":
            if dry_run:
                would_merge_count += 1
                detail["outcome"] = "would_merge"
            else:
                merge_result = merge_concepts(source_id, target_id, simulate=False)
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
                    detail["merge_error"] = merge_result.get("errors") or merge_result.get(
                        "error"
                    )
                    queue_result = _queue_uncertain(source_id, target_id, rec)
                    if bool(queue_result.get("success")):
                        queued_count += 1
                        detail["fallback_queue_outcome"] = "queued_for_review"
                    else:
                        detail["fallback_queue_outcome"] = "queue_failed"
                        detail["fallback_queue_error"] = queue_result.get("error")

        elif action == "queue_review":
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
        else:
            skipped_count += 1
            continue

        if len(details) < detail_limit:
            details.append(detail)

    cluster_before = _coerce_int(
        ctx.get("duplicate_cluster_count_before"),
        default=0,
        minimum=0,
        maximum=1_000_000,
    )
    cluster_after = cluster_before
    if not dry_run and merged_count > 0:
        try:
            cluster_after = _cluster_count_after_apply(
                scan_limit=scan_limit,
                text_limit=text_limit,
                max_pair_eval=max_pair_eval,
                auto_threshold=auto_threshold,
                review_threshold=review_threshold,
            )
        except Exception as exc:
            logger.warning("[identity_resolution] post-apply rescan failed: %s", exc)

    reduction = max(0, cluster_before - cluster_after)
    summary = {
        "dry_run": dry_run,
        "merged_count": merged_count,
        "queued_count": queued_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "would_merge_count": would_merge_count,
        "would_queue_count": would_queue_count,
        "duplicate_cluster_count_before": cluster_before,
        "duplicate_cluster_count_after": cluster_after,
        "duplicate_cluster_reduction": reduction,
        "completed_at_utc": _utc_now_iso(),
        "policy_version": IDENTITY_POLICY_VERSION,
    }

    logger.info(
        "[identity_resolution] apply complete: merged=%d queued=%d failed=%d reduction=%d",
        merged_count,
        queued_count,
        failed_count,
        reduction,
    )

    return WorkflowActionResult(
        outputs={
            "identity_resolution_apply_summary": summary,
            "identity_resolution_apply_details": details,
            "duplicate_cluster_count_after": cluster_after,
            "duplicate_cluster_reduction": reduction,
        }
    )


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    ctx = request.data
    scan_summary = dict(ctx.get("identity_resolution_scan_summary") or {})
    apply_summary = dict(ctx.get("identity_resolution_apply_summary") or {})
    result = {
        "workflow_id": ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        "policy_version": IDENTITY_POLICY_VERSION,
        "scan_summary": scan_summary,
        "apply_summary": apply_summary,
        "duplicate_cluster_count_before": apply_summary.get(
            "duplicate_cluster_count_before",
            scan_summary.get("duplicate_cluster_count", 0),
        ),
        "duplicate_cluster_count_after": apply_summary.get(
            "duplicate_cluster_count_after",
            scan_summary.get("duplicate_cluster_count", 0),
        ),
        "duplicate_cluster_reduction": apply_summary.get(
            "duplicate_cluster_reduction",
            0,
        ),
        "generated_at_utc": _utc_now_iso(),
    }
    return WorkflowActionResult(outputs={"identity_resolution_result": result})


def build_entity_identity_resolution_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        definition=build_entity_identity_resolution_workflow_test_definition(),
        purpose=(
            "Background duplicate-entity detection, confidence-scored resolution, "
            "and provenance maintenance."
        ),
        source="built_in",
    )


def register_entity_identity_resolution_actions(registry: ActionRegistry) -> None:
    actions = [
        ActionSpec(
            action_id="identity_resolution.scan_candidates",
            handler=_handle_scan_candidates,
            description=(
                "Detect duplicate clusters and confidence-scored recommendations."
            ),
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="identity_resolution.apply_resolutions",
            handler=_handle_apply_resolutions,
            description=(
                "Auto-merge high-confidence duplicates and queue uncertain cases."
            ),
            side_effects="write",
        ),
        ActionSpec(
            action_id="identity_resolution.finalise",
            handler=_handle_finalise,
            description="Summarise identity-resolution outcomes and trend metrics.",
            side_effects="none",
        ),
    ]
    for spec in actions:
        try:
            registry.register(spec)
        except ValueError:
            pass
