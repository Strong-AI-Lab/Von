"""Active rumination workflow for stronger parent/type specificity.

JVNAUTOSCI-217:
- scan the ontology for concepts that have enough multilingual/relational
  evidence to support tighter typing,
- assemble a reusable dossier via subworkflow composition,
- analyse the dossier with a Vontology-governed prompt, and
- when confidence is high enough, add a stronger existing parent/type or create
  a missing intervening subtype before attaching the subject to it.

The workflow is intentionally conservative: it fails closed on missing prompt
support, treats ambiguous analysis as no-change, and avoids destructive removal
of broader existing parents.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import Any, Mapping, Sequence

from ...db.repositories.concepts_repository import ConceptsRepository
from ...languagemodels.llm_interface import get_llm_client
from ...services.concept_service import (
    ConceptNotFoundError,
    create_concept,
    get_concept_by_concept_id,
)
from ...services.parent_specificity_vontology_service import (
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
    render_parent_specificity_prompt,
)
from ...services.relationship_write_service import add_relationship
from ...services.text_value_service import get_texts_for_concept
from ...utils.concept_id_utils import canonicalise_vontology_concept_id
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
from ..parent_specificity_workflow_contracts import (
    PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID,
    PARENT_SPECIFICITY_DOSSIER_FAILURE_MODE_BINDINGS,
    PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS,
    PARENT_SPECIFICITY_DOSSIER_WRITES_CONTEXT_KEYS,
)
from ..subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
    build_subworkflow_contract,
)
from ..workflow_registry import WorkflowRegistration
from .parent_specificity_concept_dossier_workflow import (
    PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
)

logger = logging.getLogger(__name__)

DEFAULT_SCAN_LIMIT = 24
DEFAULT_SCAN_TEXT_LIMIT = 40
DEFAULT_DOSSIER_TEXT_LIMIT = 120
DEFAULT_DOSSIER_RELATION_LIMIT = 160
DEFAULT_MAX_MUTATIONS_PER_RUN = 4
DEFAULT_MIN_CONFIDENCE = 0.92
DEFAULT_REANALYSE_AFTER_HOURS = 168
PARENT_SPECIFICITY_AUDIT_FIELD = "parent_specificity_rumination_audit"

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(?P<body>\{.*\})\s*```",
    re.DOTALL | re.IGNORECASE,
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


def _normalise_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_string_list(value: Any, *, max_items: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    output: list[str] = []
    for item in value:
        cleaned = _normalise_text(item)
        if not cleaned or cleaned in output:
            continue
        output.append(cleaned)
        if len(output) >= max_items:
            break
    return output


def _coerce_concept_id_list(value: Any, *, max_items: int = 6) -> list[str]:
    output: list[str] = []
    for item in _coerce_string_list(value, max_items=max_items):
        canonical = canonicalise_vontology_concept_id(item) or item
        if canonical.startswith("#V#") and canonical not in output:
            output.append(canonical)
    return output


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_predicate(value: Any) -> str:
    text = _normalise_text(value)
    if text.startswith("#V#"):
        text = text[3:]
    return text.lower()


def _parent_specificity_step_concept_id(state_id: str) -> str:
    return f"#V#workflow_step_parent_specificity_rumination_workflow_{state_id}"


def _infer_subject_kind(concept_doc: Mapping[str, Any]) -> str:
    computed = _normalise_text(concept_doc.get("computed_kind")).lower()
    if computed == "individual":
        return "instance"
    if computed in {"instance", "type", "predicate"}:
        return computed

    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return "unknown"
    if relationships.get("is_a_type_of"):
        return "type"
    if relationships.get("is_an_instance_of"):
        return "instance"
    return "unknown"


def _structural_target_count(relationships: Mapping[str, Any]) -> int:
    total = 0
    for predicate, raw_value in relationships.items():
        if predicate in {
            "is_a_type_of",
            "is_an_instance_of",
            "has_subtype",
            "has_instance",
        }:
            continue
        if isinstance(raw_value, str):
            total += 1 if raw_value.strip() else 0
        elif isinstance(raw_value, list):
            total += sum(
                1 for item in raw_value if isinstance(item, str) and item.strip()
            )
    return total


def _review_is_recent(
    concept_doc: Mapping[str, Any],
    *,
    reanalyse_after_hours: int,
) -> bool:
    audit = concept_doc.get(PARENT_SPECIFICITY_AUDIT_FIELD)
    if not isinstance(audit, Mapping):
        return False
    last_reviewed = _parse_datetime(audit.get("last_reviewed_at_utc"))
    if last_reviewed is None:
        return False
    window_start = datetime.now(timezone.utc) - timedelta(hours=reanalyse_after_hours)
    if last_reviewed < window_start:
        return False

    updated_at = _parse_datetime(concept_doc.get("updated_at"))
    if updated_at and updated_at > last_reviewed:
        return False
    return True


def _build_candidate_summary(
    concept_doc: Mapping[str, Any],
    *,
    text_limit: int,
    reanalyse_after_hours: int,
) -> dict[str, Any] | None:
    concept_id = _normalise_text(concept_doc.get("concept_id"))
    if not concept_id:
        return None

    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        relationships = {}

    subject_kind = _infer_subject_kind(concept_doc)
    if subject_kind not in {"type", "instance"}:
        return None

    try:
        text_rows = list(get_texts_for_concept(concept_id, limit=text_limit) or [])
    except Exception as exc:
        logger.debug(
            "[parent_specificity] candidate text scan failed for %s: %s",
            concept_id,
            exc,
        )
        text_rows = []

    description_rows = [
        row
        for row in text_rows
        if isinstance(row, Mapping)
        and _normalise_predicate(row.get("predicate")) == "hasdescription"
    ]
    description_languages = sorted(
        {
            _normalise_text(row.get("lang"))
            for row in description_rows
            if _normalise_text(row.get("lang"))
        }
    )
    non_hierarchy_relation_count = _structural_target_count(relationships)
    hierarchy_relation_count = len(relationships.get("is_a_type_of") or []) + len(
        relationships.get("is_an_instance_of") or []
    )

    evidence_score = min(len(description_rows), 4) + min(
        non_hierarchy_relation_count, 6
    )
    if len(description_languages) > 1:
        evidence_score += 2
    if hierarchy_relation_count > 0:
        evidence_score += 1
    if evidence_score < 3:
        return None

    if _review_is_recent(
        concept_doc,
        reanalyse_after_hours=reanalyse_after_hours,
    ):
        return None

    try:
        display_name = get_concept_display_name_with_names_fallback(dict(concept_doc))
    except Exception:
        display_name = None

    return {
        "concept_id": concept_id,
        "display_name": display_name or concept_id,
        "subject_kind": subject_kind,
        "description_count": len(description_rows),
        "description_languages": description_languages,
        "non_hierarchy_relation_count": non_hierarchy_relation_count,
        "hierarchy_relation_count": hierarchy_relation_count,
        "evidence_score": evidence_score,
        "last_reviewed_at_utc": (
            (concept_doc.get(PARENT_SPECIFICITY_AUDIT_FIELD) or {}).get(
                "last_reviewed_at_utc"
            )
            if isinstance(concept_doc.get(PARENT_SPECIFICITY_AUDIT_FIELD), Mapping)
            else None
        ),
    }


def _extract_json_payload(raw_text: str) -> tuple[Any, str]:
    cleaned = str(raw_text or "").strip()
    if not cleaned:
        return None, "empty"
    try:
        return json.loads(cleaned), "strict_json"
    except Exception:
        pass

    fence_match = _JSON_FENCE_RE.search(cleaned)
    if fence_match:
        candidate = fence_match.group("body")
        try:
            return json.loads(candidate), "fenced_json"
        except Exception:
            pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate), "braced_json"
        except Exception:
            pass

    return None, "unparsed"


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    except Exception:
        return None
    return concept if isinstance(concept, Mapping) else None


def _normalise_new_type_payload(
    value: Any,
    *,
    fallback_parent_id: str | None,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    name = _normalise_text(value.get("name"))
    if not name:
        return None
    concept_id = canonicalise_vontology_concept_id(value.get("concept_id") or name) or ""
    parent_ids = _coerce_concept_id_list(value.get("parent_ids"))
    if not parent_ids and fallback_parent_id:
        parent_ids = [fallback_parent_id]
    if not parent_ids:
        return None
    return {
        "name": name,
        "concept_id": concept_id,
        "description": _normalise_text(value.get("description")),
        "parent_ids": parent_ids,
    }


def _normalise_analysis_result(
    payload: Mapping[str, Any],
    *,
    concept_id: str,
    dossier: Mapping[str, Any],
) -> dict[str, Any]:
    decision = _normalise_text(payload.get("decision")).lower()
    if decision not in {"no_change", "add_existing_parent", "create_intervening_type"}:
        decision = "no_change"

    subject_kind = _normalise_text(payload.get("subject_kind")).lower()
    if subject_kind not in {"type", "instance"}:
        subject_kind = _normalise_text(dossier.get("subject_kind")).lower() or "unknown"

    recommended_parent_id = canonicalise_vontology_concept_id(
        payload.get("recommended_parent_id")
    )
    confidence = _coerce_float(
        payload.get("confidence"),
        default=0.0,
        minimum=0.0,
        maximum=1.0,
    )
    new_type = _normalise_new_type_payload(
        payload.get("new_type"),
        fallback_parent_id=recommended_parent_id,
    )

    return {
        "concept_id": concept_id,
        "decision": decision,
        "subject_kind": subject_kind,
        "recommended_parent_id": recommended_parent_id,
        "confidence": confidence,
        "reasons": _coerce_string_list(payload.get("reasons"), max_items=8),
        "evidence": _coerce_string_list(payload.get("evidence"), max_items=10),
        "language_signals": _coerce_string_list(
            payload.get("language_signals"),
            max_items=12,
        ),
        "new_type": new_type,
    }


def _record_parent_specificity_audit(
    *,
    concept_id: str,
    payload: Mapping[str, Any],
) -> None:
    ConceptsRepository.update_one(
        {"concept_id": concept_id},
        {
            "$set": {
                PARENT_SPECIFICITY_AUDIT_FIELD: {
                    **dict(payload),
                    "last_reviewed_at_utc": _utc_now_iso(),
                }
            }
        },
    )


def _append_record(
    existing: Any,
    record: Mapping[str, Any],
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in (existing if isinstance(existing, list) else []):
        if isinstance(item, Mapping):
            rows.append(dict(item))
    rows.append(dict(record))
    return rows[-limit:]


def _ensure_intervening_type(
    *,
    new_type_payload: Mapping[str, Any],
) -> dict[str, Any]:
    concept_id = _normalise_text(new_type_payload.get("concept_id"))
    name = _normalise_text(new_type_payload.get("name"))
    description = _normalise_text(new_type_payload.get("description"))
    parent_ids = _coerce_concept_id_list(new_type_payload.get("parent_ids"))
    if not concept_id or not name or not parent_ids:
        return {
            "success": False,
            "error": "intervening_type_payload_incomplete",
        }

    existing = _safe_get_concept(concept_id)
    created = False
    if existing is None:
        create_concept(
            name=name,
            concept_id=concept_id,
            description=description or None,
            parent_concept_ids=parent_ids,
            create_as_instance=False,
            visibility_scope_mode="global_general",
        )
        created = True
    else:
        kind = _infer_subject_kind(existing)
        if kind not in {"type", "unknown"}:
            return {
                "success": False,
                "error": f"intervening_concept_not_type:{concept_id}",
            }
        for parent_id in parent_ids:
            add_relationship(concept_id, "is_a_type_of", parent_id)

    return {
        "success": True,
        "intervening_type_id": concept_id,
        "created": created,
        "parent_ids": parent_ids,
    }


def build_parent_specificity_rumination_workflow() -> WorkflowDefinition:
    assess = WorkflowStateSpec(
        state_id="assess",
        actions=(
            WorkflowActionInvocation(
                action_id="parent_specificity.assess_candidates",
                description="Scan for concepts with enough evidence for stronger typing.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="prepare_candidate",
                condition=lambda ctx: bool(ctx.get("candidate_ids")),
                reason="candidates_found",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="no_candidates",
            ),
        ),
    )

    prepare_candidate = WorkflowStateSpec(
        state_id="prepare_candidate",
        actions=(
            WorkflowActionInvocation(
                action_id="parent_specificity.prepare_candidate",
                description="Select the next concept for parent-specificity review.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="gather_dossier",
                condition=lambda ctx: bool(ctx.get("current_candidate_id")),
                reason="candidate_selected",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="no_remaining_candidates",
            ),
        ),
    )

    gather_dossier = WorkflowStateSpec(
        state_id="gather_dossier",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                inputs={
                    "workflow_id": PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
                    **dict(PARENT_SPECIFICITY_DOSSIER_FAILURE_MODE_BINDINGS),
                    "concept_id": {
                        "$context_key": "current_candidate_id",
                        "$mapping_concept_id": (
                            PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID
                        ),
                    },
                    "__parent_workflow_id": PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
                    "__parent_state_id": _parent_specificity_step_concept_id(
                        "gather_dossier"
                    ),
                },
                description=(
                    "Invoke reusable dossier subworkflow before parent-specificity analysis."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="analyse_candidate",
                condition=lambda _ctx: True,
                reason="dossier_ready_or_failed_closed",
            ),
        ),
        metadata={
            "subworkflow_contract": build_subworkflow_contract(
                workflow_id=PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
                input_mappings=[
                    {
                        "child_input_key": "concept_id",
                        "parent_context_key": "current_candidate_id",
                        "mapping_concept_id": (
                            PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID
                        ),
                    },
                ],
                output_mappings=[
                    {
                        "child_output_field": item.child_output_field,
                        "parent_context_key": item.context_key,
                        "mapping_concept_id": item.concept_id,
                    }
                    for item in PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS
                    if isinstance(item.child_output_field, str)
                ],
                failure_mode=WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
            ),
            "tool_output_context_mappings": [
                {
                    "tool_output_field": item.tool_output_field,
                    "context_key": item.context_key,
                    "mapping_concept_id": item.concept_id,
                }
                for item in PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS
            ],
            "writes_context_keys": list(PARENT_SPECIFICITY_DOSSIER_WRITES_CONTEXT_KEYS),
        },
    )

    analyse_candidate = WorkflowStateSpec(
        state_id="analyse_candidate",
        actions=(
            WorkflowActionInvocation(
                action_id="parent_specificity.analyse_candidate",
                description=(
                    "Analyse multilingual dossier evidence for stronger parent/type specificity."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="apply_candidate",
                condition=lambda _ctx: True,
                reason="analysis_complete",
            ),
        ),
    )

    apply_candidate = WorkflowStateSpec(
        state_id="apply_candidate",
        actions=(
            WorkflowActionInvocation(
                action_id="parent_specificity.apply_candidate",
                description=(
                    "Apply only high-confidence stronger typing or missing intervening type creation."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="prepare_candidate",
                condition=lambda ctx: bool(ctx.get("has_remaining_candidates")),
                reason="more_candidates",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="review_budget_exhausted",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="parent_specificity.finalise",
                description="Summarise parent-specificity rumination results.",
            ),
        ),
        terminal=True,
    )
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
        initial_state="assess",
        states={
            "assess": assess,
            "prepare_candidate": prepare_candidate,
            "gather_dossier": gather_dossier,
            "analyse_candidate": analyse_candidate,
            "apply_candidate": apply_candidate,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Actively improve parent/type specificity from multilingual descriptions "
            "and relationship evidence."
        ),
    )


def _handle_assess_candidates(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    scan_limit = _coerce_int(
        context.get("scan_limit"),
        default=DEFAULT_SCAN_LIMIT,
        minimum=1,
        maximum=200,
    )
    scan_text_limit = _coerce_int(
        context.get("scan_text_limit"),
        default=DEFAULT_SCAN_TEXT_LIMIT,
        minimum=5,
        maximum=120,
    )
    dossier_text_limit = _coerce_int(
        context.get("dossier_text_limit"),
        default=DEFAULT_DOSSIER_TEXT_LIMIT,
        minimum=20,
        maximum=400,
    )
    dossier_relation_limit = _coerce_int(
        context.get("dossier_relation_limit"),
        default=DEFAULT_DOSSIER_RELATION_LIMIT,
        minimum=20,
        maximum=400,
    )
    max_mutations = _coerce_int(
        context.get("max_mutations_per_run"),
        default=DEFAULT_MAX_MUTATIONS_PER_RUN,
        minimum=1,
        maximum=50,
    )
    reanalyse_after_hours = _coerce_int(
        context.get("reanalyse_after_hours"),
        default=DEFAULT_REANALYSE_AFTER_HOURS,
        minimum=1,
        maximum=24 * 365,
    )

    candidate_docs = ConceptsRepository.find(
        {
            "$or": [
                {"relationships.is_a_type_of": {"$exists": True, "$ne": []}},
                {"relationships.is_an_instance_of": {"$exists": True, "$ne": []}},
            ]
        },
        projection={
            "concept_id": 1,
            "name": 1,
            "names": 1,
            "computed_kind": 1,
            "relationships": 1,
            "updated_at": 1,
            PARENT_SPECIFICITY_AUDIT_FIELD: 1,
        },
        sort=[("updated_at", -1), ("concept_id", 1)],
        limit=max(scan_limit * 6, scan_limit),
    )

    candidate_summaries: list[dict[str, Any]] = []
    for concept_doc in candidate_docs:
        if not isinstance(concept_doc, Mapping):
            continue
        summary = _build_candidate_summary(
            concept_doc,
            text_limit=scan_text_limit,
            reanalyse_after_hours=reanalyse_after_hours,
        )
        if summary is not None:
            candidate_summaries.append(summary)

    candidate_summaries.sort(
        key=lambda item: (
            -int(item.get("evidence_score", 0)),
            -int(item.get("non_hierarchy_relation_count", 0)),
            str(item.get("concept_id", "")),
        )
    )
    candidate_summaries = candidate_summaries[:scan_limit]
    candidate_ids = [str(item["concept_id"]) for item in candidate_summaries]

    return WorkflowActionResult(
        outputs={
            "candidate_ids": candidate_ids,
            "candidate_summaries": candidate_summaries,
            "candidate_index": 0,
            "scan_limit_used": scan_limit,
            "scan_text_limit_used": scan_text_limit,
            "dossier_text_limit": dossier_text_limit,
            "dossier_relation_limit": dossier_relation_limit,
            "max_mutations_per_run": max_mutations,
            "reanalyse_after_hours": reanalyse_after_hours,
            "reviewed_count": 0,
            "applied_count": 0,
            "would_apply_count": 0,
            "failed_apply_count": 0,
            "no_change_count": 0,
            "created_intervening_type_count": 0,
            "analysis_records": [],
        }
    )


def _handle_prepare_candidate(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    candidate_ids = [
        str(item).strip()
        for item in (context.get("candidate_ids") or [])
        if isinstance(item, str) and str(item).strip()
    ]
    candidate_index = _coerce_int(
        context.get("candidate_index"),
        default=0,
        minimum=0,
        maximum=max(len(candidate_ids), 0),
    )
    if candidate_index >= len(candidate_ids):
        return WorkflowActionResult(
            outputs={
                "current_candidate_id": None,
                "current_candidate_summary": None,
                "has_remaining_candidates": False,
            }
        )

    current_candidate_id = candidate_ids[candidate_index]
    summaries = context.get("candidate_summaries") or []
    current_summary = next(
        (
            item
            for item in summaries
            if isinstance(item, Mapping)
            and str(item.get("concept_id") or "").strip() == current_candidate_id
        ),
        None,
    )

    return WorkflowActionResult(
        outputs={
            "candidate_index": candidate_index + 1,
            "current_candidate_id": current_candidate_id,
            "current_candidate_summary": dict(current_summary or {}),
            "has_remaining_candidates": candidate_index + 1 < len(candidate_ids),
            "concept_dossier": None,
            "concept_dossier_summary": None,
            "dossier_child_failed": False,
            "dossier_error": None,
            "analysis_result": None,
            "analysis_status": None,
            "analysis_actionable": False,
            "analysis_prompt_diagnostics": None,
            "analysis_parse_mode": None,
        }
    )


def _handle_analyse_candidate(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    current_candidate_id = _normalise_text(context.get("current_candidate_id"))
    dossier = context.get("concept_dossier")
    if not current_candidate_id:
        return WorkflowActionResult(
            outputs={
                "analysis_status": "candidate_missing",
                "analysis_actionable": False,
                "analysis_result": {"decision": "no_change", "confidence": 0.0},
            }
        )

    if bool(context.get("dossier_child_failed")) or not isinstance(dossier, Mapping):
        return WorkflowActionResult(
            outputs={
                "analysis_status": "dossier_unavailable",
                "analysis_actionable": False,
                "analysis_result": {
                    "concept_id": current_candidate_id,
                    "decision": "no_change",
                    "confidence": 0.0,
                    "reasons": [
                        _normalise_text(context.get("dossier_error"))
                        or "concept dossier unavailable"
                    ],
                },
            }
        )

    min_confidence = _coerce_float(
        context.get("min_confidence"),
        default=DEFAULT_MIN_CONFIDENCE,
        minimum=0.5,
        maximum=0.999,
    )
    analysis_payload = {
        "workflow_id": PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
        "concept_id": current_candidate_id,
        "candidate_summary": context.get("current_candidate_summary") or {},
        "concept_dossier": dict(dossier),
        "minimum_apply_confidence": min_confidence,
        "mutation_budget_remaining": max(
            0,
            int(context.get("max_mutations_per_run", DEFAULT_MAX_MUTATIONS_PER_RUN))
            - int(context.get("applied_count", 0)),
        ),
    }

    rendered_prompt, prompt_diagnostics = render_parent_specificity_prompt(
        workflow_id=PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
        prompt_concept_id=_normalise_text(context.get("prompt_concept_id")) or None,
        variables={
            "analysis_payload_json": json.dumps(
                analysis_payload,
                ensure_ascii=True,
                indent=2,
            )
        },
        max_chars=24000,
    )
    if rendered_prompt is None:
        return WorkflowActionResult(
            outputs={
                "analysis_status": "prompt_unavailable",
                "analysis_actionable": False,
                "analysis_prompt_diagnostics": prompt_diagnostics,
                "analysis_result": {
                    "concept_id": current_candidate_id,
                    "decision": "no_change",
                    "confidence": 0.0,
                    "reasons": ["parent-specificity prompt unavailable"],
                },
            }
        )

    llm = request.environment.llm_client or get_llm_client()
    raw_response = llm.generate(
        rendered_prompt.text,
        llm_params={"max_tokens": 1400},
    )
    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    if not isinstance(parsed_payload, Mapping):
        return WorkflowActionResult(
            outputs={
                "analysis_status": "invalid_json",
                "analysis_actionable": False,
                "analysis_prompt_diagnostics": prompt_diagnostics,
                "analysis_parse_mode": parse_mode,
                "analysis_result": {
                    "concept_id": current_candidate_id,
                    "decision": "no_change",
                    "confidence": 0.0,
                    "reasons": [f"analysis_json_parse_failed:{parse_mode}"],
                },
            }
        )

    analysis_result = _normalise_analysis_result(
        parsed_payload,
        concept_id=current_candidate_id,
        dossier=dossier,
    )
    decision = str(analysis_result.get("decision") or "")
    actionable = decision in {"add_existing_parent", "create_intervening_type"}
    status = "no_change"

    if decision == "add_existing_parent":
        actionable = actionable and bool(analysis_result.get("recommended_parent_id"))
    elif decision == "create_intervening_type":
        actionable = actionable and isinstance(analysis_result.get("new_type"), Mapping)

    if actionable and float(analysis_result.get("confidence") or 0.0) < min_confidence:
        actionable = False
        status = "low_confidence"
    elif actionable:
        status = "actionable"
    elif decision != "no_change":
        status = "invalid_recommendation"

    return WorkflowActionResult(
        outputs={
            "analysis_result": analysis_result,
            "analysis_status": status,
            "analysis_actionable": actionable,
            "analysis_prompt_diagnostics": prompt_diagnostics,
            "analysis_parse_mode": parse_mode,
        }
    )


def _handle_apply_candidate(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    current_candidate_id = _normalise_text(context.get("current_candidate_id"))
    analysis_result = (
        dict(context.get("analysis_result") or {})
        if isinstance(context.get("analysis_result"), Mapping)
        else {}
    )
    analysis_status = _normalise_text(context.get("analysis_status")) or "no_change"
    analysis_actionable = bool(context.get("analysis_actionable"))
    dry_run = bool(context.get("dry_run", False))
    max_mutations = _coerce_int(
        context.get("max_mutations_per_run"),
        default=DEFAULT_MAX_MUTATIONS_PER_RUN,
        minimum=1,
        maximum=50,
    )
    applied_count = int(context.get("applied_count", 0) or 0)

    reviewed_count = int(context.get("reviewed_count", 0) or 0) + 1
    would_apply_count = int(context.get("would_apply_count", 0) or 0)
    failed_apply_count = int(context.get("failed_apply_count", 0) or 0)
    no_change_count = int(context.get("no_change_count", 0) or 0)
    created_intervening_type_count = int(
        context.get("created_intervening_type_count", 0) or 0
    )
    analysis_records = list(context.get("analysis_records") or [])

    decision = _normalise_text(analysis_result.get("decision")) or "no_change"
    confidence = float(analysis_result.get("confidence") or 0.0)
    record: dict[str, Any] = {
        "concept_id": current_candidate_id,
        "decision": decision,
        "confidence": confidence,
        "status": analysis_status,
        "dry_run": dry_run,
    }

    if not current_candidate_id:
        return WorkflowActionResult(
            outputs={
                "reviewed_count": reviewed_count,
                "no_change_count": no_change_count + 1,
                "analysis_records": _append_record(analysis_records, record),
            }
        )

    if applied_count >= max_mutations:
        record["status"] = "mutation_budget_exhausted"
        no_change_count += 1
        return WorkflowActionResult(
            outputs={
                "reviewed_count": reviewed_count,
                "applied_count": applied_count,
                "would_apply_count": would_apply_count,
                "failed_apply_count": failed_apply_count,
                "no_change_count": no_change_count,
                "created_intervening_type_count": created_intervening_type_count,
                "analysis_records": _append_record(analysis_records, record),
            }
        )

    if not analysis_actionable:
        record["status"] = analysis_status or "no_change"
        no_change_count += 1
        if not dry_run:
            try:
                _record_parent_specificity_audit(
                    concept_id=current_candidate_id,
                    payload={
                        "last_decision": decision,
                        "last_status": record["status"],
                        "last_confidence": confidence,
                    },
                )
            except Exception as exc:
                logger.debug(
                    "[parent_specificity] audit write failed for %s: %s",
                    current_candidate_id,
                    exc,
                )
        return WorkflowActionResult(
            outputs={
                "reviewed_count": reviewed_count,
                "applied_count": applied_count,
                "would_apply_count": would_apply_count,
                "failed_apply_count": failed_apply_count,
                "no_change_count": no_change_count,
                "created_intervening_type_count": created_intervening_type_count,
                "analysis_records": _append_record(analysis_records, record),
                "latest_parent_specificity_action": record,
            }
        )

    if dry_run:
        would_apply_count += 1
        record["status"] = "would_apply"
        return WorkflowActionResult(
            outputs={
                "reviewed_count": reviewed_count,
                "applied_count": applied_count,
                "would_apply_count": would_apply_count,
                "failed_apply_count": failed_apply_count,
                "no_change_count": no_change_count,
                "created_intervening_type_count": created_intervening_type_count,
                "analysis_records": _append_record(analysis_records, record),
                "latest_parent_specificity_action": record,
            }
        )

    try:
        mutation_result: dict[str, Any]
        if decision == "add_existing_parent":
            predicate = (
                "is_an_instance_of"
                if _normalise_text(analysis_result.get("subject_kind")) == "instance"
                else "is_a_type_of"
            )
            target_id = _normalise_text(analysis_result.get("recommended_parent_id"))
            write_result = add_relationship(current_candidate_id, predicate, target_id)
            mutation_result = {
                "success": bool(write_result.get("success")),
                "action": "add_existing_parent",
                "predicate": predicate,
                "target_id": target_id,
                "write_result": write_result,
            }
        else:
            new_type_payload = analysis_result.get("new_type")
            type_result = _ensure_intervening_type(
                new_type_payload=dict(new_type_payload or {}),
            )
            if not bool(type_result.get("success")):
                mutation_result = {
                    "success": False,
                    "action": "create_intervening_type",
                    "error": type_result.get("error") or "intervening_type_create_failed",
                    "type_result": type_result,
                }
            else:
                subject_predicate = (
                    "is_an_instance_of"
                    if _normalise_text(analysis_result.get("subject_kind")) == "instance"
                    else "is_a_type_of"
                )
                attach_result = add_relationship(
                    current_candidate_id,
                    subject_predicate,
                    str(type_result.get("intervening_type_id") or ""),
                )
                mutation_result = {
                    "success": bool(type_result.get("success"))
                    and bool(attach_result.get("success")),
                    "action": "create_intervening_type",
                    "subject_predicate": subject_predicate,
                    "intervening_type_id": type_result.get("intervening_type_id"),
                    "created_intervening_type": bool(type_result.get("created")),
                    "type_result": type_result,
                    "attach_result": attach_result,
                }
                if bool(type_result.get("created")):
                    created_intervening_type_count += 1

        if bool(mutation_result.get("success")):
            applied_count += 1
            record["status"] = "applied"
        else:
            failed_apply_count += 1
            record["status"] = "apply_failed"
        record["mutation_result"] = mutation_result

        _record_parent_specificity_audit(
            concept_id=current_candidate_id,
            payload={
                "last_decision": decision,
                "last_status": record["status"],
                "last_confidence": confidence,
                "last_applied_at_utc": _utc_now_iso()
                if record["status"] == "applied"
                else None,
                "last_mutation_result": mutation_result,
            },
        )
    except Exception as exc:
        logger.exception(
            "[parent_specificity] apply failed for %s",
            current_candidate_id,
        )
        failed_apply_count += 1
        record["status"] = "apply_failed"
        record["error"] = str(exc)

    return WorkflowActionResult(
        outputs={
            "reviewed_count": reviewed_count,
            "applied_count": applied_count,
            "would_apply_count": would_apply_count,
            "failed_apply_count": failed_apply_count,
            "no_change_count": no_change_count,
            "created_intervening_type_count": created_intervening_type_count,
            "analysis_records": _append_record(analysis_records, record),
            "latest_parent_specificity_action": record,
        }
    )


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    candidate_ids = list(context.get("candidate_ids") or [])
    result = {
        "success": int(context.get("failed_apply_count", 0) or 0) == 0,
        "candidate_count": len(candidate_ids),
        "reviewed_count": int(context.get("reviewed_count", 0) or 0),
        "applied_count": int(context.get("applied_count", 0) or 0),
        "would_apply_count": int(context.get("would_apply_count", 0) or 0),
        "failed_apply_count": int(context.get("failed_apply_count", 0) or 0),
        "no_change_count": int(context.get("no_change_count", 0) or 0),
        "created_intervening_type_count": int(
            context.get("created_intervening_type_count", 0) or 0
        ),
        "analysis_records": list(context.get("analysis_records") or []),
        "scan_limit_used": context.get("scan_limit_used"),
        "scan_text_limit_used": context.get("scan_text_limit_used"),
    }
    return WorkflowActionResult(
        outputs={"parent_specificity_rumination_result": result}
    )


def get_parent_specificity_rumination_workflow_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
        definition=build_parent_specificity_rumination_workflow(),
        purpose=(
            "Actively improve parent/type specificity from multilingual descriptions "
            "and relationship evidence."
        ),
        source="built_in",
    )


def register_parent_specificity_rumination_actions(registry: ActionRegistry) -> None:
    actions = [
        ActionSpec(
            action_id="parent_specificity.assess_candidates",
            handler=_handle_assess_candidates,
            description="Scan for concepts with enough evidence for stronger typing.",
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="parent_specificity.prepare_candidate",
            handler=_handle_prepare_candidate,
            description="Select the next concept for parent-specificity review.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="parent_specificity.analyse_candidate",
            handler=_handle_analyse_candidate,
            description="Analyse multilingual dossier evidence for stronger typing.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="parent_specificity.apply_candidate",
            handler=_handle_apply_candidate,
            description="Apply only high-confidence stronger typing changes.",
            side_effects="write",
        ),
        ActionSpec(
            action_id="parent_specificity.finalise",
            handler=_handle_finalise,
            description="Summarise parent-specificity rumination results.",
            side_effects="none",
        ),
    ]
    for action in actions:
        registry.register_if_absent(action)


__all__ = [
    "DEFAULT_DOSSIER_RELATION_LIMIT",
    "DEFAULT_DOSSIER_TEXT_LIMIT",
    "DEFAULT_MAX_MUTATIONS_PER_RUN",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_REANALYSE_AFTER_HOURS",
    "DEFAULT_SCAN_LIMIT",
    "DEFAULT_SCAN_TEXT_LIMIT",
    "PARENT_SPECIFICITY_AUDIT_FIELD",
    "build_parent_specificity_rumination_workflow",
    "get_parent_specificity_rumination_workflow_registration",
    "register_parent_specificity_rumination_actions",
]
