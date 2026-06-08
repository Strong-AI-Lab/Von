"""Workflow-discovery gap recovery and candidate workflow test workflows.

These workflows provide a workflow-first recovery path after chat-turn routing
fails to find an applicable discovered workflow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Mapping, Sequence

from ...languagemodels.llm_interface import get_llm_client
from ...security.visibility_predicates import (
    CANONICAL_VISIBILITY_PREDICATES,
    LEGACY_VISIBILITY_PREDICATES,
)
from ...services.concept_service import (
    ConceptNotFoundError,
    get_concept_by_concept_id,
)
from ...services.settings_service import (
    INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT,
)
from ...services.namespace_service import derive_actor_context_from_namespace
from ...services.text_value_service import upsert_singleton_text_relation
from ...services.testing_workflow_contracts import (
    CAPABILITY_TEST_EXECUTION_WORKFLOW_ID,
)
from ...services.workflow_gap_vontology_service import (
    render_workflow_gap_candidate_prompt,
    render_workflow_gap_analysis_prompt,
    render_workflow_gap_test_prompt,
)
from ...utils.concept_id_utils import canonicalise_vontology_concept_id
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
    build_subworkflow_contract,
)
from ..workflow_template_profile_service import (
    WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID,
    resolve_workflow_spec_template,
)
from ..workflow_authoring_service import (
    serialise_workflow_definition_to_authoring_spec,
    strip_transient_execution_defaults_from_authoring_spec,
)
from ..workflow_definition_identity_service import build_workflow_definition_identity
from ..vontology_loader import load_workflow_definition_from_vontology
from ..workflow_gap_workflow_contracts import (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID,
    WORKFLOW_GAP_CANDIDATE_PROMPT_LINK_PREDICATE,
    WORKFLOW_GAP_COLLECT_CONTEXT_ACTION_ID,
    WORKFLOW_GAP_CREATE_TEST_INPUTS_MAPPING_ID,
    WORKFLOW_GAP_CREATE_TOOL_OUTPUT_MAPPINGS,
    WORKFLOW_GAP_CREATE_WORKFLOW_SPEC_MAPPING_ID,
    WORKFLOW_GAP_CREATE_WRITES_CONTEXT_KEYS,
    WORKFLOW_GAP_CREATION_FAILURE_MODE_BINDINGS,
    WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
    WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
    WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
    WORKFLOW_GAP_MAX_REPAIR_ATTEMPTS,
    WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID,
    WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
    WORKFLOW_GAP_TEST_ACCEPTANCE_MAPPING_ID,
    WORKFLOW_GAP_TEST_BASE_RESPONSE_MAPPING_ID,
    WORKFLOW_GAP_TEST_FAILURE_MODE_BINDINGS,
    WORKFLOW_GAP_TEST_ORG_CONCEPT_MAPPING_ID,
    WORKFLOW_GAP_TEST_PROMPT_MAPPING_ID,
    WORKFLOW_GAP_TEST_RECENT_TURNS_MAPPING_ID,
    WORKFLOW_GAP_TEST_SESSION_ID_MAPPING_ID,
    WORKFLOW_GAP_TEST_TOOL_OUTPUT_MAPPINGS,
    WORKFLOW_GAP_TEST_TURN_ID_MAPPING_ID,
    WORKFLOW_GAP_TEST_USER_CONCEPT_MAPPING_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID_MAPPING_ID,
    WORKFLOW_GAP_TEST_WRITES_CONTEXT_KEYS,
)
from ..workflow_registry import WorkflowRegistration

logger = logging.getLogger(__name__)

WORKFLOW_CREATION_WORKFLOW_ID = "#V#von_workflow_creation_workflow"
DEFAULT_RECENT_TURN_LIMIT = 6
DEFAULT_RECENT_CONTEXT_LIMIT = 8
DEFAULT_TEXT_LIMIT = 2_000
DEFAULT_ANALYSIS_CONFIDENCE_THRESHOLD = 0.9
DEFAULT_TEST_CONFIDENCE_THRESHOLD = 0.85

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(?P<body>\{.*\})\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _coerce_float(
    value: Any,
    *,
    default: float,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_string_list(value: Any, *, max_items: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _clean_text(item)
        lowered = cleaned.lower()
        if not cleaned or lowered in seen:
            continue
        seen.add(lowered)
        items.append(cleaned)
        if len(items) >= max_items:
            break
    return items


def _coerce_message_sequence(
    value: Any,
    *,
    max_items: int = 20,
    include_timestamps: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        role = _clean_text(item.get("role")) or "assistant"
        content = _clean_text(item.get("content"))
        if not content:
            continue
        row: dict[str, Any] = {
            "role": role,
            "content": content[:DEFAULT_TEXT_LIMIT],
        }
        if include_timestamps:
            timestamp = _clean_text(item.get("timestamp"))
            if timestamp:
                row["timestamp"] = timestamp
        messages.append(row)
        if len(messages) >= max_items:
            break
    return messages


def _coerce_mapping_list(value: Any, *, max_items: int = 40) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            rows.append(dict(item))
        if len(rows) >= max_items:
            break
    return rows


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)


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


def _safe_json_load(value: Any, *, fallback: Any) -> Any:
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except Exception:
        return fallback


def _slugify(value: str, *, fallback: str) -> str:
    raw = _clean_text(value).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return slug or fallback


def _titleise_slug(value: str) -> str:
    slug = _clean_text(value)
    if slug.startswith("#V#"):
        slug = slug[3:]
    words = [part for part in slug.replace("-", "_").split("_") if part]
    if not words:
        return "Workflow"
    return " ".join(word.capitalize() for word in words)


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    except Exception:
        return None
    return concept if isinstance(concept, Mapping) else None


def _load_runtime_workflow_definition(workflow_id: str) -> WorkflowDefinition | None:
    workflow_id_clean = _clean_text(workflow_id)
    if not workflow_id_clean:
        return None

    try:
        from .registry_factory import build_durable_workflow_registry_read_only

        registry = build_durable_workflow_registry_read_only(defer_parity_work=True)
        definition = registry.get(workflow_id_clean)
        if isinstance(definition, WorkflowDefinition):
            return definition
    except Exception:
        logger.debug(
            "workflow gap recovery registry lookup failed for %s",
            workflow_id_clean,
            exc_info=True,
        )

    try:
        definition = load_workflow_definition_from_vontology(workflow_id_clean)
    except Exception:
        logger.debug(
            "workflow gap recovery Vontology definition lookup failed for %s",
            workflow_id_clean,
            exc_info=True,
        )
        return None
    return definition if isinstance(definition, WorkflowDefinition) else None


def _extract_capability_execution_outputs(
    payload: Mapping[str, Any] | None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    workflow_execution = _mapping_or_empty((payload or {}).get("workflow_execution"))
    outputs = _mapping_or_empty(workflow_execution.get("outputs"))
    response_text = _clean_text(outputs.get("response_text"))
    tool_invocations = _coerce_mapping_list(outputs.get("tool_invocations"), max_items=32)
    tool_messages = _coerce_mapping_list(outputs.get("extra_messages"), max_items=32)
    return response_text, tool_invocations, tool_messages


def _build_capability_test_summary(
    *,
    payload: Mapping[str, Any] | None,
    action_error: str | None,
) -> dict[str, Any]:
    raw = _mapping_or_empty(payload)
    workflow_execution = _mapping_or_empty(raw.get("workflow_execution"))
    candidate_validation = _mapping_or_empty(raw.get("candidate_validation"))
    side_effect_audit = _mapping_or_empty(raw.get("side_effect_audit"))
    trace_summary = _mapping_or_empty(raw.get("trace_summary"))
    quality_signals = _mapping_or_empty(raw.get("quality_signals"))
    promotion_recommendation = _mapping_or_empty(raw.get("promotion_recommendation"))
    verdict_summary = _mapping_or_empty(raw.get("verdict_summary"))
    repair_hints = _coerce_mapping_list(
        raw.get("repair_hints") or candidate_validation.get("repair_hints"),
        max_items=16,
    )
    final_status = _clean_text(raw.get("final_status") or workflow_execution.get("final_status"))
    verdict = _clean_text(raw.get("verdict"))
    blocked_event_count = int(side_effect_audit.get("blocked_event_count") or 0)
    candidate_validation_valid = candidate_validation.get("valid")
    timed_out = bool(raw.get("timed_out"))
    error_text = (
        _clean_text(action_error)
        or _clean_text(raw.get("error"))
        or _clean_text(workflow_execution.get("error"))
        or _clean_text(trace_summary.get("last_error"))
    )
    passed = (
        final_status == "completed"
        and not timed_out
        and blocked_event_count == 0
        and candidate_validation_valid is not False
        and verdict not in {"fail", "inconclusive"}
    )
    return {
        "schema_version": "workflow_gap_capability_test_summary.v1",
        "passed": passed,
        "final_status": final_status or None,
        "timed_out": timed_out,
        "error": error_text or None,
        "verdict": verdict or None,
        "verdict_summary": verdict_summary or None,
        "promotion_recommendation": promotion_recommendation or None,
        "candidate_validation": candidate_validation or None,
        "repair_hints": repair_hints,
        "quality_signals": quality_signals or None,
        "side_effect_audit": side_effect_audit or None,
        "trace_summary": trace_summary or None,
        "workflow_execution": workflow_execution or None,
    }


def _build_actionable_escalation_message(
    *,
    attempt_count: int,
    capability_summary: Mapping[str, Any] | None,
    acceptance_report: Mapping[str, Any] | None,
    creation_error: str | None = None,
) -> str | None:
    reasons: list[str] = []
    summary = _mapping_or_empty(capability_summary)
    acceptance = _mapping_or_empty(acceptance_report)

    creation_error_clean = _clean_text(creation_error)
    if creation_error_clean:
        reasons.append(f"creation failure: {creation_error_clean}")

    capability_error = _clean_text(summary.get("error"))
    if capability_error:
        reasons.append(f"capability test failure: {capability_error}")

    candidate_validation = _mapping_or_empty(summary.get("candidate_validation"))
    if candidate_validation.get("valid") is False:
        reasons.append("candidate validation failed before safe execution")

    blocked_event_count = int(
        _mapping_or_empty(summary.get("side_effect_audit")).get("blocked_event_count")
        or 0
    )
    if blocked_event_count > 0:
        reasons.append(
            f"side-effect policy blocked {blocked_event_count} event"
            f"{'' if blocked_event_count == 1 else 's'}"
        )

    trace_error = _clean_text(_mapping_or_empty(summary.get("trace_summary")).get("last_error"))
    if trace_error:
        reasons.append(f"execution trace error: {trace_error}")

    acceptance_reason = _clean_text(acceptance.get("reason"))
    if acceptance_reason:
        reasons.append(f"acceptance failure: {acceptance_reason}")

    missing_requirements = _coerce_string_list(
        acceptance.get("missing_requirements"),
        max_items=8,
    )
    if missing_requirements:
        reasons.append(
            "missing acceptance requirements: "
            + "; ".join(missing_requirements[:3])
        )

    repair_hints = _coerce_mapping_list(summary.get("repair_hints"), max_items=3)
    hint_texts = [
        _clean_text(item.get("repair_hint") or item.get("reason_code"))
        for item in repair_hints
        if _clean_text(item.get("repair_hint") or item.get("reason_code"))
    ]
    if hint_texts:
        reasons.append("suggested repairs: " + "; ".join(hint_texts))

    if not reasons:
        return None
    joined = " | ".join(reasons)
    return (
        f"Repair loop exhausted after {attempt_count} attempt"
        f"{'' if attempt_count == 1 else 's'}. "
        f"{joined}"
    )


def _normalise_candidate_workflow_id(
    *,
    requested_workflow_id: str | None,
    workflow_name: str,
    request_text: str,
    gap_summary: str,
) -> str:
    requested = _clean_text(requested_workflow_id)
    if requested:
        canonical = canonicalise_vontology_concept_id(requested) or requested
        if canonical.startswith("#V#"):
            return canonical

    base_slug = _slugify(
        workflow_name or "workflow_gap_candidate",
        fallback="workflow_gap_candidate",
    )
    if not base_slug.endswith("workflow"):
        base_slug = f"{base_slug}_workflow"
    suffix = hashlib.sha1(
        f"{request_text}|{workflow_name}|{gap_summary}".encode("utf-8")
    ).hexdigest()[:10]
    return f"#V#{base_slug}_{suffix}"


def _workflow_gap_step_concept_id(workflow_id: str, state_id: str) -> str:
    workflow_slug = _slugify(
        workflow_id[3:] if workflow_id.startswith("#V#") else workflow_id,
        fallback="workflow_gap",
    )
    state_slug = _slugify(state_id, fallback="step")
    return f"#V#workflow_step_{workflow_slug}_{state_slug}"


def _build_recent_turns_from_context(
    context: Any,
    *,
    limit: int = DEFAULT_RECENT_CONTEXT_LIMIT,
) -> list[dict[str, Any]]:
    messages = _coerce_message_sequence(context, max_items=max(1, limit))
    if not messages:
        return []
    return messages[-limit:]


def _build_recent_turns_from_history(
    *,
    user_concept_id: str,
    conversation_session_id: str,
    limit: int = DEFAULT_RECENT_TURN_LIMIT,
) -> tuple[list[dict[str, Any]], str | None]:
    from ...services.chat_history_service import ChatHistoryServiceError, get_chat_history

    try:
        history = get_chat_history(user_concept_id, conversation_session_id)
    except ChatHistoryServiceError as exc:
        return [], f"chat_history_unavailable:{exc}"
    except Exception as exc:
        return [], f"chat_history_failed:{exc}"

    rows = _coerce_message_sequence(
        history,
        max_items=max(1, limit * 3),
        include_timestamps=True,
    )
    if not rows:
        return [], None
    return rows[-limit:], None


def _normalise_analysis_result(
    payload: Mapping[str, Any] | None,
    *,
    fallback_request_text: str,
) -> dict[str, Any]:
    raw = dict(payload or {})
    decision = _clean_text(raw.get("decision")).lower()
    if decision not in {"no_gap", "ask_user", "create_and_retry"}:
        decision = "no_gap"
    workflow_name = _clean_text(raw.get("workflow_name")) or _titleise_slug(
        _slugify(fallback_request_text, fallback="workflow_gap_candidate")
    )
    result = {
        "decision": decision,
        "confidence": _coerce_float(raw.get("confidence"), default=0.0),
        "intent_summary": _clean_text(raw.get("intent_summary"))
        or _clean_text(fallback_request_text)[:160],
        "gap_summary": _clean_text(raw.get("gap_summary"))
        or "A reusable workflow gap may exist for this intent.",
        "workflow_name": workflow_name,
        "workflow_id": _clean_text(raw.get("workflow_id")) or None,
        "workflow_description": _clean_text(raw.get("workflow_description"))
        or f"Candidate workflow to satisfy '{workflow_name}'.",
        "workflow_guidance": _coerce_string_list(
            raw.get("workflow_guidance"),
            max_items=16,
        ),
        "acceptance_requirements": _coerce_string_list(
            raw.get("acceptance_requirements"),
            max_items=16,
        ),
        "requires_write_tools": _coerce_bool(raw.get("requires_write_tools")),
        "reasons": _coerce_string_list(raw.get("reasons"), max_items=16),
        "user_confirmation_prompt": _clean_text(raw.get("user_confirmation_prompt")),
    }
    if not result["acceptance_requirements"]:
        result["acceptance_requirements"] = [
            "The user-facing result directly addresses the requested intent.",
            "The response does not claim completion without evidence.",
        ]
    if not result["workflow_guidance"]:
        result["workflow_guidance"] = [
            "Prefer reusable workflow structure over one-off procedural behaviour.",
            "Use internal tools when they are the clearest route to satisfying the request.",
        ]
    if not result["user_confirmation_prompt"] and result["decision"] == "ask_user":
        result["user_confirmation_prompt"] = (
            "I created a candidate workflow for this gap. Do you agree with the gap analysis and want me to use it?"
        )
    return result


def _link_candidate_prompt_to_workflow(
    *,
    workflow_id: str,
    prompt_concept_id: str,
) -> str | None:
    if _safe_get_concept(workflow_id) is None:
        return "candidate_workflow_concept_missing_for_prompt_link"
    try:
        upsert_singleton_text_relation(
            subject_concept_id=workflow_id,
            predicate=WORKFLOW_GAP_CANDIDATE_PROMPT_LINK_PREDICATE,
            text=prompt_concept_id,
            lang="en-NZ",
            policy="replace_others",
            garbage_collect=True,
        )
    except Exception as exc:
        return f"candidate_prompt_link_failed:{exc}"
    return None


def _candidate_recent_turns(
    request: WorkflowActionRequest,
    *,
    default_recent_turns_json: str,
) -> tuple[list[dict[str, Any]], str]:
    live_recent_turns = request.data.get("workflow_gap_recent_turns")
    if isinstance(live_recent_turns, list):
        recent_turns = _coerce_message_sequence(live_recent_turns, max_items=12)
        if recent_turns:
            return recent_turns, _json_text(recent_turns)

    for context_key in ("context", "augmented_context"):
        messages = _build_recent_turns_from_context(request.data.get(context_key))
        if messages:
            return messages, _json_text(messages)

    fallback_turns = _safe_json_load(default_recent_turns_json, fallback=[])
    recent_turns = _coerce_message_sequence(fallback_turns, max_items=12)
    return recent_turns, _json_text(recent_turns)


def _coerce_string_list_from_json(value: Any, *, max_items: int = 12) -> list[str]:
    if isinstance(value, str):
        parsed = _safe_json_load(value, fallback=None)
        if isinstance(parsed, list):
            return _coerce_string_list(parsed, max_items=max_items)
        cleaned = _clean_text(value)
        return [cleaned] if cleaned else []
    return _coerce_string_list(value, max_items=max_items)


def _resolve_candidate_string_list(
    *,
    inputs: Mapping[str, Any],
    request_data: Mapping[str, Any],
    direct_key: str,
    json_key: str,
    analysis_key: str,
    max_items: int = 16,
) -> list[str]:
    for source in (
        inputs.get(direct_key),
        request_data.get(direct_key),
        inputs.get(json_key),
        request_data.get(json_key),
        (
            (request_data.get("workflow_gap_analysis_result") or {}).get(analysis_key)
            if isinstance(request_data.get("workflow_gap_analysis_result"), Mapping)
            else None
        ),
    ):
        values = (
            _coerce_string_list_from_json(source, max_items=max_items)
            if isinstance(source, str)
            else _coerce_string_list(source, max_items=max_items)
        )
        if values:
            return values
    return []


def _build_workflow_gap_prefetch_tool_invocation(
    *,
    tool_name: str,
    payload: Mapping[str, Any],
    result_payload: Mapping[str, Any] | None,
    duration_ms: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if isinstance(result_payload, Mapping):
        summary = {
            "concept_id": _clean_text(result_payload.get("concept_id")),
            "name": _clean_text(result_payload.get("name")),
            "relations_found": (
                int((result_payload.get("relations") or {}).get("relations_found") or 0)
                if isinstance(result_payload.get("relations"), Mapping)
                else 0
            ),
        }
    invocation = {
        "tool": tool_name,
        "effective_payload": dict(payload),
        "status": "failed" if error else "success",
        "source": "workflow_gap_prefetch",
        "result_summary": summary,
    }
    if duration_ms is not None:
        invocation["duration_ms"] = duration_ms
    if error:
        invocation["error"] = error
    return invocation


def _build_authenticated_concept_grounding_text(payload: Mapping[str, Any]) -> str:
    concept_id = _clean_text(payload.get("concept_id"))
    concept_name = _clean_text(payload.get("name")) or concept_id or "Authenticated concept"
    lines = [f"Authenticated focal concept: {concept_name}{f' ({concept_id})' if concept_id else ''}"]

    relations_payload = payload.get("relations")
    relations = (
        list(relations_payload.get("relations") or [])
        if isinstance(relations_payload, Mapping)
        else []
    )

    descriptions: list[str] = []
    relation_lines: list[str] = []
    seen_relation_keys: set[str] = set()

    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        predicate_id = _clean_text(relation.get("predicate_id"))
        relation_kind = _clean_text(relation.get("relation_kind"))
        if relation_kind == "text" and predicate_id == "hasDescription":
            text_value = relation.get("text_value")
            text = (
                _clean_text(text_value.get("text"))
                if isinstance(text_value, Mapping)
                else ""
            )
            if text:
                descriptions.append(text)
            continue
        if relation_kind != "binary":
            continue
        target_previews = relation.get("target_previews")
        if isinstance(target_previews, Mapping):
            target_names = [
                _clean_text((preview or {}).get("name"))
                for preview in target_previews.values()
                if isinstance(preview, Mapping) and _clean_text((preview or {}).get("name"))
            ]
        else:
            target_names = _coerce_string_list(relation.get("target_values"), max_items=4)
        target_names = [name for name in target_names if name]
        if not target_names or len(target_names) > 4:
            continue
        if predicate_id in {
            *CANONICAL_VISIBILITY_PREDICATES,
            *LEGACY_VISIBILITY_PREDICATES,
            "#V#has_paper_recommendation_assertion",
            "#V#preferred_language",
            "#V#has_email",
        }:
            continue
        relation_key = f"{predicate_id.lower()}|{'|'.join(name.lower() for name in target_names)}"
        if relation_key in seen_relation_keys:
            continue
        seen_relation_keys.add(relation_key)
        relation_lines.append(
            f"- {_titleise_slug(predicate_id)}: {', '.join(target_names[:4])}"
        )
        if len(relation_lines) >= 6:
            break

    if descriptions:
        lines.append("Explicit description evidence:")
        for description in descriptions[:2]:
            compact = re.sub(r"\s+", " ", description).strip()
            if compact:
                lines.append(f"- {compact[:700]}")
    if relation_lines:
        lines.append("Explicit relation evidence:")
        lines.extend(relation_lines)
    return "\n".join(lines)


def _prefetch_candidate_grounding(
    *,
    request: WorkflowActionRequest,
    inputs: Mapping[str, Any],
    user_concept_id: str,
) -> tuple[
    str,
    list[dict[str, Any]],
    dict[str, Any] | None,
    Mapping[str, Any] | None,
]:
    profile = (
        _clean_text(inputs.get("workflow_gap_prefetch_profile"))
        or _clean_text(request.data.get("workflow_gap_prefetch_profile"))
    ).lower()
    if profile != "authenticated_concept_relations":
        return "", [], None, None
    if not user_concept_id:
        return "", [], {
            "profile": profile,
            "executed": False,
            "reason": "prefetch_not_applicable",
        }, None

    gateway = request.environment.gateway
    if gateway is None or not getattr(gateway, "enabled", False):
        return "", [], {
            "profile": profile,
            "executed": False,
            "reason": "gateway_unavailable",
        }, None

    payload: dict[str, Any] = {
        "concept_id": user_concept_id,
        "include_relations_arg1": True,
        "include_text_relations_arg1": True,
        "include_relations_any_arg": False,
        "limit": 80,
    }
    user_namespace = _clean_text(request.environment.user_namespace)
    if user_namespace:
        payload["namespace"] = user_namespace

    try:
        result = gateway.invoke("fetch_concept", payload)
        result_payload = (
            dict(getattr(result, "payload", {}))
            if isinstance(getattr(result, "payload", {}), Mapping)
            else {}
        )
        evidence_text = _build_authenticated_concept_grounding_text(result_payload)
        invocation = _build_workflow_gap_prefetch_tool_invocation(
            tool_name="fetch_concept",
            payload=payload,
            result_payload=result_payload,
            duration_ms=getattr(result, "duration_ms", None),
        )
        diagnostics = {
            "profile": profile,
            "executed": True,
            "tool": "fetch_concept",
            "concept_id": user_concept_id,
            "evidence_present": bool(evidence_text),
        }
        return evidence_text, [invocation], diagnostics, result_payload
    except Exception as exc:
        invocation = _build_workflow_gap_prefetch_tool_invocation(
            tool_name="fetch_concept",
            payload=payload,
            result_payload=None,
            error=str(exc),
        )
        diagnostics = {
            "profile": profile,
            "executed": False,
            "tool": "fetch_concept",
            "concept_id": user_concept_id,
            "error": str(exc),
        }
        return "", [invocation], diagnostics, None


def _normalise_candidate_execution_inputs(
    request: WorkflowActionRequest,
) -> dict[str, Any]:
    merged = dict(request.inputs or {})
    for key, value in (request.data or {}).items():
        if key in {
            "prompt",
            "workflow_gap_request_text",
            "workflow_gap_recent_turns",
            "workflow_gap_acceptance_requirements",
            "workflow_gap_acceptance_requirements_json",
            "workflow_gap_base_response_text",
            "workflow_gap_guidance",
            "workflow_gap_guidance_json",
            "workflow_gap_prefetch_profile",
            "workflow_gap_dry_run",
            "conversation_session_id",
            "turn_id",
            "user_concept_id",
            "org_concept_id",
            "context",
            "augmented_context",
        }:
            merged[key] = value
    return merged

def _handle_collect_context(request: WorkflowActionRequest) -> WorkflowActionResult:
    prompt = (
        _clean_text(request.data.get("workflow_gap_request_text"))
        or _clean_text(request.data.get("prompt"))
    )
    base_response_text = (
        _clean_text(request.data.get("workflow_gap_base_response_text"))
        or _clean_text(request.data.get("response_text"))
    )
    namespace_user_concept_id, namespace_org_concept_id = (
        derive_actor_context_from_namespace(request.environment.user_namespace)
    )
    user_concept_id = (
        _clean_text(request.data.get("user_concept_id"))
        or _clean_text(request.environment.user_concept_id)
        or _clean_text(namespace_user_concept_id)
    )
    org_concept_id = (
        _clean_text(request.data.get("org_concept_id"))
        or _clean_text(request.environment.org_concept_id)
        or _clean_text(namespace_org_concept_id)
    )
    conversation_session_id = _clean_text(
        request.data.get("conversation_session_id") or request.data.get("session_id")
    )

    recent_turns: list[dict[str, Any]] = []
    recent_turns_source = "context"
    recent_turns_error: str | None = None
    if user_concept_id and conversation_session_id:
        recent_turns, recent_turns_error = _build_recent_turns_from_history(
            user_concept_id=user_concept_id,
            conversation_session_id=conversation_session_id,
        )
        if recent_turns:
            recent_turns_source = "chat_history"
    if not recent_turns:
        recent_turns = _build_recent_turns_from_context(
            request.data.get("context") or request.data.get("augmented_context")
        )

    tool_invocations = _coerce_mapping_list(
        request.data.get("workflow_gap_base_tool_invocations"),
        max_items=24,
    )
    tool_messages = _coerce_mapping_list(
        request.data.get("workflow_gap_base_extra_messages"),
        max_items=24,
    )
    raw_routing = request.data.get("workflow_routing")
    routing = dict(raw_routing) if isinstance(raw_routing, Mapping) else {}
    raw_discovery_result = request.data.get("workflow_discovery_result")
    discovery_result = (
        dict(raw_discovery_result)
        if isinstance(raw_discovery_result, Mapping)
        else {}
    )

    outputs = {
        "workflow_gap_request_text": prompt,
        "workflow_gap_base_response_text": base_response_text,
        "workflow_gap_recent_turns": recent_turns,
        "workflow_gap_recent_turns_json": _json_text(recent_turns),
        "workflow_gap_recent_turns_source": recent_turns_source,
        "workflow_gap_base_tool_invocations": tool_invocations,
        "workflow_gap_base_extra_messages": tool_messages,
        "workflow_gap_routing_snapshot": routing,
        "workflow_gap_discovery_snapshot": discovery_result,
        "user_concept_id": user_concept_id or None,
        "org_concept_id": org_concept_id or None,
        "conversation_session_id": conversation_session_id or None,
        "turn_id": _clean_text(request.data.get("turn_id")) or None,
        "workflow_gap_context_collected": True,
    }
    if recent_turns_error:
        outputs["workflow_gap_recent_turns_error"] = recent_turns_error
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_analyse_recovery(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    request_text = _clean_text(context.get("workflow_gap_request_text"))
    analysis_payload = {
        "workflow_id": WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
        "request_text": request_text,
        "recent_turns": list(context.get("workflow_gap_recent_turns") or []),
        "base_response_text": _clean_text(context.get("workflow_gap_base_response_text")),
        "routing": dict(context.get("workflow_gap_routing_snapshot") or {}),
        "workflow_discovery": dict(context.get("workflow_gap_discovery_snapshot") or {}),
        "base_tool_invocations": list(context.get("workflow_gap_base_tool_invocations") or []),
        "base_tool_messages": list(context.get("workflow_gap_base_extra_messages") or []),
    }
    rendered_prompt, prompt_diagnostics = render_workflow_gap_analysis_prompt(
        workflow_id=WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
        prompt_concept_id=(
            _clean_text(context.get("workflow_gap_analysis_prompt_concept_id")) or None
        ),
        variables={"analysis_payload_json": _json_text(analysis_payload)},
        max_chars=24_000,
    )
    if rendered_prompt is None:
        analysis_result = _normalise_analysis_result(
            {
                "decision": "no_gap",
                "confidence": 0.0,
                "gap_summary": "Workflow-gap analysis prompt unavailable.",
                "reasons": ["workflow_gap_analysis_prompt_unavailable"],
            },
            fallback_request_text=request_text,
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "workflow_gap_analysis_status": "prompt_unavailable",
                "workflow_gap_prompt_diagnostics": prompt_diagnostics,
                "workflow_gap_analysis_result": analysis_result,
                "workflow_gap_should_create_candidate": False,
                "workflow_gap_should_test_candidate": False,
                "workflow_gap_needs_user_confirmation": False,
            },
        )

    llm = request.environment.llm_client or get_llm_client()
    raw_response = llm.generate(
        rendered_prompt.text,
        llm_params={"max_tokens": 1800},
    )
    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    if not isinstance(parsed_payload, Mapping):
        analysis_result = _normalise_analysis_result(
            {
                "decision": "no_gap",
                "confidence": 0.0,
                "gap_summary": "Workflow-gap analysis response was not valid JSON.",
                "reasons": [f"workflow_gap_analysis_json_parse_failed:{parse_mode}"],
            },
            fallback_request_text=request_text,
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "workflow_gap_analysis_status": "invalid_json",
                "workflow_gap_prompt_diagnostics": prompt_diagnostics,
                "workflow_gap_analysis_result": analysis_result,
                "workflow_gap_analysis_parse_mode": parse_mode,
                "workflow_gap_should_create_candidate": False,
                "workflow_gap_should_test_candidate": False,
                "workflow_gap_needs_user_confirmation": False,
            },
        )

    analysis_result = _normalise_analysis_result(
        parsed_payload,
        fallback_request_text=request_text,
    )
    decision = str(analysis_result.get("decision") or "no_gap")
    confidence = _coerce_float(analysis_result.get("confidence"), default=0.0)
    should_create_candidate = decision in {"ask_user", "create_and_retry"}
    should_test_candidate = (
        decision == "create_and_retry"
        and confidence >= DEFAULT_ANALYSIS_CONFIDENCE_THRESHOLD
    )
    needs_user_confirmation = decision == "ask_user" or (
        should_create_candidate and not should_test_candidate
    )
    return WorkflowActionResult(
        status="success",
        outputs={
            "workflow_gap_analysis_status": "analysed",
            "workflow_gap_prompt_diagnostics": prompt_diagnostics,
            "workflow_gap_analysis_result": analysis_result,
            "workflow_gap_analysis_parse_mode": parse_mode,
            "workflow_gap_should_create_candidate": should_create_candidate,
            "workflow_gap_should_test_candidate": should_test_candidate,
            "workflow_gap_needs_user_confirmation": needs_user_confirmation,
        },
    )


def _handle_prepare_candidate_spec(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    analysis_result = (
        dict(context.get("workflow_gap_analysis_result") or {})
        if isinstance(context.get("workflow_gap_analysis_result"), Mapping)
        else {}
    )
    if not bool(context.get("workflow_gap_should_create_candidate")):
        return WorkflowActionResult(
            status="success",
            outputs={"workflow_gap_candidate_prepared": False},
        )

    previous_attempt_count = 0
    try:
        previous_attempt_count = int(context.get("workflow_gap_repair_attempt_count") or 0)
    except Exception:
        previous_attempt_count = 0
    attempt_count = max(1, previous_attempt_count + 1)

    request_text = _clean_text(context.get("workflow_gap_request_text"))
    workflow_name = _clean_text(analysis_result.get("workflow_name")) or "Workflow Gap Candidate"
    workflow_description = (
        _clean_text(analysis_result.get("workflow_description"))
        or "Candidate workflow created from workflow-gap recovery."
    )
    gap_summary = _clean_text(analysis_result.get("gap_summary"))
    intent_summary = _clean_text(analysis_result.get("intent_summary"))
    workflow_guidance = _coerce_string_list(
        analysis_result.get("workflow_guidance"),
        max_items=16,
    )
    acceptance_requirements = _coerce_string_list(
        analysis_result.get("acceptance_requirements"),
        max_items=16,
    )
    candidate_workflow_id = _normalise_candidate_workflow_id(
        requested_workflow_id=_clean_text(analysis_result.get("workflow_id")) or None,
        workflow_name=workflow_name,
        request_text=request_text,
        gap_summary=gap_summary,
    )
    prompt_concept_id = WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID
    candidate_prompt_variables = {
        "workflow_name": workflow_name,
        "workflow_description": workflow_description,
        "intent_summary": intent_summary,
        "gap_summary": gap_summary,
        "workflow_guidance_json": _json_text(list(workflow_guidance)),
        "acceptance_requirements_json": _json_text(list(acceptance_requirements)),
        "request_text": request_text,
        "recent_turns_json": _clean_text(context.get("workflow_gap_recent_turns_json")),
        "base_response_text": _clean_text(context.get("workflow_gap_base_response_text")),
    }
    rendered_candidate_prompt, candidate_prompt_diagnostics = (
        render_workflow_gap_candidate_prompt(
            prompt_concept_id=prompt_concept_id,
            variables=candidate_prompt_variables,
            max_chars=24_000,
        )
    )
    if rendered_candidate_prompt is None:
        return WorkflowActionResult(
            status="failed",
            error="workflow_gap_candidate_prompt_unavailable",
            outputs={
                "candidate_prompt_concept_id": prompt_concept_id,
                "candidate_workflow_id": candidate_workflow_id,
                "workflow_gap_candidate_prompt_diagnostics": (
                    candidate_prompt_diagnostics
                ),
            },
        )

    recent_turns_json = _clean_text(context.get("workflow_gap_recent_turns_json"))
    base_response_text = _clean_text(context.get("workflow_gap_base_response_text"))
    try:
        candidate_workflow_spec, template_resolution = resolve_workflow_spec_template(
            request_text=request_text,
            explicit_template_id=WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID,
            variables={
                "workflow_id": candidate_workflow_id,
                "workflow_name": workflow_name,
                "workflow_description": workflow_description,
                "prompt_concept_id": prompt_concept_id,
                "request_text": request_text,
                "recent_turns_json": recent_turns_json,
                "base_response_text": base_response_text,
                "workflow_guidance_json": _json_text(list(workflow_guidance)),
                "acceptance_requirements_json": _json_text(
                    list(acceptance_requirements)
                ),
                "intent_summary": intent_summary,
                "gap_summary": gap_summary,
            },
        )
        candidate_workflow_spec = strip_transient_execution_defaults_from_authoring_spec(
            candidate_workflow_spec
        )
    except Exception as exc:
        return WorkflowActionResult(
            status="failed",
            error=f"workflow_gap_candidate_template_render_failed:{exc}",
            outputs={
                "candidate_workflow_id": candidate_workflow_id,
                "candidate_prompt_concept_id": prompt_concept_id,
            },
        )
    return WorkflowActionResult(
        status="success",
        outputs={
            "candidate_workflow_spec": candidate_workflow_spec,
            "candidate_workflow_creation_test_inputs": {
                "workflow_gap_dry_run": True,
            },
            "candidate_workflow_id": candidate_workflow_id,
            "candidate_workflow_name": workflow_name,
            "candidate_prompt_concept_id": prompt_concept_id,
            "candidate_prompt_created": False,
            "workflow_gap_repair_attempt_count": attempt_count,
            "workflow_gap_repair_attempts_remaining": max(
                0,
                WORKFLOW_GAP_MAX_REPAIR_ATTEMPTS - attempt_count,
            ),
            "workflow_gap_candidate_prompt_diagnostics": candidate_prompt_diagnostics,
            "workflow_gap_candidate_prepared": True,
            "workflow_gap_acceptance_requirements": acceptance_requirements,
            "workflow_gap_acceptance_requirements_json": _json_text(
                acceptance_requirements
            ),
            "workflow_gap_guidance": workflow_guidance,
            "workflow_gap_guidance_json": _json_text(workflow_guidance),
            "workflow_gap_candidate_template_resolution": template_resolution,
            "workflow_gap_repair_loop_active": attempt_count
            < WORKFLOW_GAP_MAX_REPAIR_ATTEMPTS,
        },
    )


def _handle_decide_test(request: WorkflowActionRequest) -> WorkflowActionResult:
    should_test = bool(request.data.get("workflow_gap_should_test_candidate"))
    candidate_workflow_id = _clean_text(
        request.data.get("created_candidate_workflow_id")
        or request.data.get("candidate_workflow_id")
    )
    creation_failed = bool(request.data.get("candidate_creation_child_failed")) or bool(
        _clean_text(request.data.get("candidate_creation_error"))
    )
    try:
        attempt_count = int(request.data.get("workflow_gap_repair_attempt_count") or 0)
    except Exception:
        attempt_count = 0
    try:
        last_test_attempt_count = int(
            request.data.get("workflow_gap_last_test_attempt_count") or 0
        )
    except Exception:
        last_test_attempt_count = 0
    attempts_remaining = max(0, WORKFLOW_GAP_MAX_REPAIR_ATTEMPTS - attempt_count)
    test_passed = bool(request.data.get("workflow_gap_test_passed"))
    test_failed = (
        bool(_clean_text(request.data.get("workflow_gap_test_error")))
        or bool(request.data.get("workflow_gap_test_child_failed"))
        or (last_test_attempt_count == attempt_count and attempt_count > 0 and not test_passed)
    )
    should_test = (
        should_test
        and bool(candidate_workflow_id)
        and not creation_failed
        and attempt_count > last_test_attempt_count
    )
    should_repair = False
    repair_reason = ""
    if creation_failed and attempt_count < WORKFLOW_GAP_MAX_REPAIR_ATTEMPTS:
        should_repair = True
        repair_reason = "candidate_creation_failed"
    elif test_failed and attempt_count < WORKFLOW_GAP_MAX_REPAIR_ATTEMPTS:
        should_repair = True
        repair_reason = "candidate_test_failed"
    repair_attempts_exhausted = bool(
        (creation_failed or test_failed)
        and attempt_count >= WORKFLOW_GAP_MAX_REPAIR_ATTEMPTS
    )
    return WorkflowActionResult(
        status="success",
        outputs={
            "workflow_gap_should_test_candidate_now": should_test,
            "workflow_gap_should_repair_candidate_now": should_repair,
            "workflow_gap_repair_reason": repair_reason or None,
            "workflow_gap_repair_attempts_remaining": attempts_remaining,
            "workflow_gap_repair_attempts_exhausted": repair_attempts_exhausted,
        },
    )


def _handle_execute_candidate(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = _normalise_candidate_execution_inputs(request)
    prompt_concept_id = _clean_text(inputs.get("prompt_concept_id"))
    if not prompt_concept_id:
        return WorkflowActionResult(
            status="failed",
            error="workflow_gap_candidate_prompt_missing",
        )

    request_text = (
        _clean_text(inputs.get("prompt"))
        or _clean_text(inputs.get("workflow_gap_request_text"))
        or _clean_text(inputs.get("default_request_text"))
    )
    recent_turns, recent_turns_json = _candidate_recent_turns(
        request,
        default_recent_turns_json=_clean_text(inputs.get("default_recent_turns_json")),
    )
    base_response_text = (
        _clean_text(inputs.get("workflow_gap_base_response_text"))
        or _clean_text(inputs.get("default_base_response_text"))
    )
    workflow_guidance = _resolve_candidate_string_list(
        inputs=inputs,
        request_data=request.data,
        direct_key="workflow_gap_guidance",
        json_key="workflow_guidance_json",
        analysis_key="workflow_guidance",
        max_items=16,
    )
    acceptance_requirements = _resolve_candidate_string_list(
        inputs=inputs,
        request_data=request.data,
        direct_key="workflow_gap_acceptance_requirements",
        json_key="acceptance_requirements_json",
        analysis_key="acceptance_requirements",
        max_items=16,
    )
    rendered_prompt, prompt_diagnostics = render_workflow_gap_candidate_prompt(
        prompt_concept_id=prompt_concept_id,
        variables={
            "workflow_name": (
                _clean_text(inputs.get("workflow_name"))
                or _clean_text(request.data.get("candidate_workflow_name"))
                or "Workflow gap candidate"
            ),
            "workflow_description": (
                _clean_text(inputs.get("workflow_description"))
                or _clean_text(
                    (
                        request.data.get("workflow_gap_analysis_result") or {}
                    ).get("workflow_description")
                )
                or "Candidate workflow to satisfy the detected workflow gap."
            ),
            "intent_summary": (
                _clean_text(inputs.get("intent_summary"))
                or _clean_text(
                    (
                        request.data.get("workflow_gap_analysis_result") or {}
                    ).get("intent_summary")
                )
                or request_text
            ),
            "gap_summary": (
                _clean_text(inputs.get("gap_summary"))
                or _clean_text(
                    ((request.data.get("workflow_gap_analysis_result") or {}).get("gap_summary"))
                )
                or "A reusable workflow gap may exist for this request."
            ),
            "workflow_guidance_json": _json_text(
                list(workflow_guidance)
            ),
            "acceptance_requirements_json": _json_text(
                list(acceptance_requirements)
            ),
            "request_text": request_text,
            "recent_turns_json": recent_turns_json,
            "base_response_text": base_response_text,
        },
        max_chars=24_000,
    )
    if rendered_prompt is None:
        return WorkflowActionResult(
            status="failed",
            error="workflow_gap_candidate_prompt_unavailable",
            outputs={
                "candidate_prompt_concept_id": prompt_concept_id,
                "workflow_gap_candidate_prompt_diagnostics": prompt_diagnostics,
            },
        )

    llm_client = request.environment.llm_client or get_llm_client()
    gateway = request.environment.gateway
    if gateway is None:
        return WorkflowActionResult(
            status="failed",
            error="workflow_gap_candidate_gateway_unavailable",
        )

    dry_run = _coerce_bool(inputs.get("workflow_gap_dry_run"))
    if dry_run:
        configured_max_tool_invocations = 0
    else:
        raw_max_tool_invocations = request.environment.max_tool_invocations
        try:
            configured_max_tool_invocations = (
                INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT
                if raw_max_tool_invocations is None
                else int(raw_max_tool_invocations)
            )
        except Exception:
            configured_max_tool_invocations = (
                INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT
            )
    auxiliary_system_prompt = _clean_text(request.environment.auxiliary_system_prompt)
    candidate_system_prompt = rendered_prompt.text
    combined_auxiliary_prompt = (
        f"{auxiliary_system_prompt}\n\n{candidate_system_prompt}"
        if auxiliary_system_prompt
        else candidate_system_prompt
    )
    context_messages = _coerce_message_sequence(
        request.data.get("context") or request.data.get("augmented_context"),
        max_items=20,
    )
    if not context_messages:
        context_messages = list(recent_turns)

    namespace_user_concept_id, namespace_org_concept_id = (
        derive_actor_context_from_namespace(request.environment.user_namespace)
    )
    resolved_user_concept_id = (
        _clean_text(request.data.get("user_concept_id"))
        or _clean_text(request.environment.user_concept_id)
        or _clean_text(namespace_user_concept_id)
    )
    resolved_org_concept_id = (
        _clean_text(request.data.get("org_concept_id"))
        or _clean_text(request.environment.org_concept_id)
        or _clean_text(namespace_org_concept_id)
    )
    (
        grounding_text,
        prefetched_tool_invocations,
        prefetch_diagnostics,
        _,
    ) = (
        _prefetch_candidate_grounding(
            request=request,
            inputs=inputs,
            user_concept_id=resolved_user_concept_id,
        )
    )
    if grounding_text:
        combined_auxiliary_prompt = (
            f"{combined_auxiliary_prompt}\n\n"
            "Prefetched represented evidence for the authored workflow:\n"
            f"{grounding_text}"
        )
    from ...integrations.internal_mcp.orchestrator import InternalMCPChatOrchestrator

    max_tool_invocations = (
        0 if dry_run else configured_max_tool_invocations
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=max_tool_invocations,
        default_gmail_profile=request.environment.default_gmail_profile,
    )

    nested_result = orchestrator.run(
        prompt=request_text,
        context=context_messages,
        llm_client=llm_client,
        model=request.environment.model,
        user_namespace=request.environment.user_namespace,
        auxiliary_system_prompt=combined_auxiliary_prompt,
        conversation_session_id=_clean_text(inputs.get("conversation_session_id")) or None,
        turn_id=_clean_text(inputs.get("turn_id")) or None,
        workflow_discovery_result=None,
        workflow_continuation_context=None,
        workflow_gap_recovery_enabled=False,
        user_concept_id=resolved_user_concept_id or None,
        org_concept_id=resolved_org_concept_id or None,
    )
    response_text = _clean_text(nested_result.response_text)
    return WorkflowActionResult(
        status="success",
        outputs={
            "candidate_response_present": bool(response_text),
            "response_text": response_text,
            "extra_messages": list(nested_result.extra_messages),
            "tool_invocations": [
                *prefetched_tool_invocations,
                *list(nested_result.tool_invocations),
            ],
            "aux_llm_calls": list(nested_result.aux_llm_calls),
            "llm_calls": list(nested_result.llm_calls),
            "candidate_workflow_routing": (
                {
                    "workflow_id": nested_result.workflow_routing.workflow_id,
                    "verdict": nested_result.workflow_routing.verdict,
                    "prompt_id": nested_result.workflow_routing.prompt_id,
                    "source": nested_result.workflow_routing.source,
                }
                if nested_result.workflow_routing is not None
                else None
            ),
            "candidate_render_plan": dict(nested_result.render_plan or {})
            if isinstance(nested_result.render_plan, Mapping)
            else nested_result.render_plan,
            "workflow_gap_dry_run": dry_run,
            "workflow_gap_prefetch_diagnostics": prefetch_diagnostics,
            "workflow_gap_prefetched_grounding_present": bool(grounding_text),
            "workflow_gap_prefetched_response_used": False,
            "workflow_gap_prefetched_response_source": None,
        },
    )


def _handle_run_candidate_test(request: WorkflowActionRequest) -> WorkflowActionResult:
    candidate_workflow_id = _clean_text(
        request.inputs.get("candidate_workflow_id")
        if isinstance(request.inputs, Mapping)
        else None
    ) or _clean_text(request.data.get("candidate_workflow_id"))
    if not candidate_workflow_id:
        return WorkflowActionResult(
            status="failed",
            error="workflow_gap_test_candidate_workflow_missing",
        )

    definition = _load_runtime_workflow_definition(candidate_workflow_id)
    if definition is None:
        return WorkflowActionResult(
            status="failed",
            error=f"workflow_gap_test_definition_not_loadable:{candidate_workflow_id}",
        )

    capability_test_definition = _load_runtime_workflow_definition(
        CAPABILITY_TEST_EXECUTION_WORKFLOW_ID
    )
    if capability_test_definition is None:
        return WorkflowActionResult(
            status="failed",
            error=(
                "workflow_gap_capability_test_definition_not_loadable:"
                f"{CAPABILITY_TEST_EXECUTION_WORKFLOW_ID}"
            ),
        )

    from .registry_factory import build_durable_action_registry

    executor = WorkflowExecutor(
        registry=build_durable_action_registry(),
        max_transitions=80,
    )
    recent_turns = _coerce_message_sequence(
        request.data.get("workflow_gap_recent_turns"),
        max_items=12,
    )
    acceptance_requirements = _coerce_string_list(
        request.data.get("workflow_gap_acceptance_requirements"),
        max_items=16,
    )
    test_context = {
        "prompt": _clean_text(
            request.data.get("workflow_gap_request_text") or request.data.get("prompt")
        ),
        "workflow_gap_request_text": _clean_text(
            request.data.get("workflow_gap_request_text") or request.data.get("prompt")
        ),
        "workflow_gap_recent_turns": recent_turns,
        "workflow_gap_acceptance_requirements": acceptance_requirements,
        "workflow_gap_base_response_text": _clean_text(
            request.data.get("workflow_gap_base_response_text")
        ),
        "conversation_session_id": _clean_text(request.data.get("conversation_session_id"))
        or None,
        "turn_id": _clean_text(request.data.get("turn_id")) or None,
        "user_concept_id": _clean_text(request.data.get("user_concept_id")) or None,
        "org_concept_id": _clean_text(request.data.get("org_concept_id")) or None,
        "workflow_gap_dry_run": _coerce_bool(request.data.get("workflow_gap_dry_run")),
    }
    definition_identity = build_workflow_definition_identity(
        workflow_id=candidate_workflow_id,
        source="workflow_gap_candidate_runtime",
        definition=definition,
        authoritative_definition=definition,
    )
    capability_test_inputs = {
        "workflow_id": candidate_workflow_id,
        "candidate_workflow_ids": [candidate_workflow_id],
        "authoring_spec": serialise_workflow_definition_to_authoring_spec(definition),
        "base_definition_hash": _clean_text(definition_identity.get("definition_hash"))
        or None,
        "workflow_inputs": test_context,
        "target_capability_ids": [candidate_workflow_id],
        "expected_outcomes": [
            {"label": "target_workflow_execution", "expected": "completed"}
        ],
        "allowed_side_effects": [],
        "forbidden_side_effects": [],
    }
    capability_result = executor.run(
        capability_test_definition,
        environment=request.environment,
        data=capability_test_inputs,
    )
    capability_summary = _build_capability_test_summary(
        payload=capability_result.data,
        action_error=capability_result.error,
    )
    candidate_response_text, candidate_tool_invocations, candidate_tool_messages = (
        _extract_capability_execution_outputs(capability_result.data)
    )

    test_payload = {
        "candidate_workflow_id": candidate_workflow_id,
        "candidate_completed": bool(capability_result.completed),
        "candidate_final_state": _clean_text(capability_result.final_state),
        "candidate_error": _clean_text(capability_result.error),
        "candidate_response_text": candidate_response_text,
        "candidate_tool_invocations": candidate_tool_invocations,
        "candidate_tool_messages": candidate_tool_messages,
        "acceptance_requirements": acceptance_requirements,
        "request_text": _clean_text(request.data.get("workflow_gap_request_text")),
        "recent_turns": recent_turns,
        "base_response_text": _clean_text(request.data.get("workflow_gap_base_response_text")),
        "capability_test": capability_summary,
    }
    rendered_prompt, prompt_diagnostics = render_workflow_gap_test_prompt(
        workflow_id=WORKFLOW_GAP_TEST_WORKFLOW_ID,
        prompt_concept_id=(
            _clean_text(request.data.get("workflow_gap_test_prompt_concept_id")) or None
        ),
        variables={"test_payload_json": _json_text(test_payload)},
        max_chars=24_000,
    )
    if rendered_prompt is None:
        report = {
            "pass": False,
            "confidence": 0.0,
            "satisfied_requirements": [],
            "missing_requirements": list(acceptance_requirements),
            "reason": "Workflow-gap test prompt unavailable.",
            "prompt_diagnostics": prompt_diagnostics,
        }
        actionable_escalation = _build_actionable_escalation_message(
            attempt_count=int(request.data.get("workflow_gap_repair_attempt_count") or 0),
            capability_summary=capability_summary,
            acceptance_report=report,
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "workflow_gap_test_passed": False,
                "workflow_gap_test_error": "workflow_gap_test_prompt_unavailable",
                "workflow_gap_capability_test": capability_summary,
                "workflow_gap_failure_evidence": {
                    "schema_version": "workflow_gap_failure_evidence.v1",
                    "capability_test": capability_summary,
                    "acceptance_test": report,
                },
                "workflow_gap_actionable_escalation": actionable_escalation,
                "workflow_gap_test_response_text": candidate_response_text,
                "workflow_gap_test_tool_invocations": candidate_tool_invocations,
                "workflow_gap_test_tool_messages": candidate_tool_messages,
                "workflow_gap_test_report": report,
            },
        )

    llm = request.environment.llm_client or get_llm_client()
    raw_response = llm.generate(
        rendered_prompt.text,
        llm_params={"max_tokens": 1400},
    )
    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    report = dict(parsed_payload) if isinstance(parsed_payload, Mapping) else {}
    report_pass = _coerce_bool(report.get("pass"))
    report_confidence = _coerce_float(report.get("confidence"), default=0.0)
    satisfied_requirements = _coerce_string_list(
        report.get("satisfied_requirements"),
        max_items=16,
    )
    missing_requirements = _coerce_string_list(
        report.get("missing_requirements"),
        max_items=16,
    )
    reason = _clean_text(report.get("reason")) or (
        f"workflow_gap_test_json_parse_failed:{parse_mode}"
        if not isinstance(parsed_payload, Mapping)
        else "workflow-gap test report completed."
    )
    test_report = {
        "pass": bool(
            report_pass and report_confidence >= DEFAULT_TEST_CONFIDENCE_THRESHOLD
        ),
        "confidence": report_confidence,
        "satisfied_requirements": satisfied_requirements,
        "missing_requirements": missing_requirements,
        "reason": reason,
        "parse_mode": parse_mode,
        "prompt_diagnostics": prompt_diagnostics,
    }
    capability_passed = bool(capability_summary.get("passed"))
    overall_test_passed = bool(test_report["pass"]) and capability_passed
    if not overall_test_passed and not _clean_text(test_report.get("reason")):
        test_report["reason"] = (
            "Candidate failed capability execution workflow validation."
            if not capability_passed
            else "Candidate failed workflow-gap acceptance evaluation."
        )
    failure_evidence = {
        "schema_version": "workflow_gap_failure_evidence.v1",
        "capability_test": capability_summary,
        "acceptance_test": test_report,
    }
    actionable_escalation = _build_actionable_escalation_message(
        attempt_count=int(request.data.get("workflow_gap_repair_attempt_count") or 0),
        capability_summary=capability_summary,
        acceptance_report=test_report,
    )
    return WorkflowActionResult(
        status="success",
        outputs={
            "workflow_gap_test_passed": overall_test_passed,
            "workflow_gap_last_test_attempt_count": int(
                request.data.get("workflow_gap_repair_attempt_count") or 0
            ),
            "workflow_gap_test_error": (
                _clean_text(capability_summary.get("error"))
                or (
                    _clean_text(test_report.get("reason"))
                    if not overall_test_passed
                    else None
                )
            ),
            "workflow_gap_capability_test": capability_summary,
            "workflow_gap_failure_evidence": failure_evidence,
            "workflow_gap_actionable_escalation": actionable_escalation,
            "workflow_gap_test_response_text": candidate_response_text,
            "workflow_gap_test_tool_invocations": candidate_tool_invocations,
            "workflow_gap_test_tool_messages": candidate_tool_messages,
            "workflow_gap_test_report": test_report,
        },
    )


def _handle_finalise_recovery(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    analysis_result = (
        dict(context.get("workflow_gap_analysis_result") or {})
        if isinstance(context.get("workflow_gap_analysis_result"), Mapping)
        else {}
    )
    base_response_text = _clean_text(context.get("workflow_gap_base_response_text"))
    candidate_workflow_id = _clean_text(
        context.get("created_candidate_workflow_id") or context.get("candidate_workflow_id")
    )
    candidate_workflow_name = _clean_text(context.get("candidate_workflow_name"))
    candidate_prompt_concept_id = _clean_text(context.get("candidate_prompt_concept_id"))
    prompt_link_error = None
    if candidate_workflow_id and candidate_prompt_concept_id:
        prompt_link_error = _link_candidate_prompt_to_workflow(
            workflow_id=candidate_workflow_id,
            prompt_concept_id=candidate_prompt_concept_id,
        )

    decision = _clean_text(analysis_result.get("decision")).lower() or "no_gap"
    final_response_text = base_response_text
    final_extra_messages = _coerce_mapping_list(
        context.get("workflow_gap_base_extra_messages"),
        max_items=24,
    )
    final_tool_invocations = _coerce_mapping_list(
        context.get("workflow_gap_base_tool_invocations"),
        max_items=24,
    )
    outcome = "no_gap"
    try:
        attempt_count = int(context.get("workflow_gap_repair_attempt_count") or 0)
    except Exception:
        attempt_count = 0
    attempts_exhausted = bool(context.get("workflow_gap_repair_attempts_exhausted"))
    actionable_escalation = _clean_text(
        context.get("workflow_gap_actionable_escalation")
    ) or None
    failure_evidence = _mapping_or_empty(context.get("workflow_gap_failure_evidence"))
    if decision == "create_and_retry" and bool(context.get("workflow_gap_test_passed")):
        final_response_text = _clean_text(context.get("workflow_gap_test_response_text"))
        final_extra_messages = _coerce_mapping_list(
            context.get("workflow_gap_test_tool_messages"),
            max_items=32,
        )
        final_tool_invocations = _coerce_mapping_list(
            context.get("workflow_gap_test_tool_invocations"),
            max_items=32,
        )
        outcome = "candidate_retried_successfully"
    elif decision == "create_and_retry" and attempts_exhausted:
        test_report = (
            dict(context.get("workflow_gap_test_report") or {})
            if isinstance(context.get("workflow_gap_test_report"), Mapping)
            else {}
        )
        failure_reason = _clean_text(test_report.get("reason")) or _clean_text(
            context.get("candidate_creation_error")
        )
        candidate_label = candidate_workflow_name or candidate_workflow_id or "candidate workflow"
        final_response_text = (
            f"{base_response_text}\n\n"
            f"I exhausted the bounded workflow-gap repair loop after {attempt_count} attempts."
            f" Candidate: {candidate_label}."
            f"{f' Latest failure: {failure_reason}.' if failure_reason else ''}"
            f"{f' {actionable_escalation}.' if actionable_escalation else ''}"
            " Please review the proposal/evidence before promoting or retrying manually."
        ).strip()
        outcome = "candidate_repair_exhausted"
    elif decision == "ask_user" and candidate_workflow_id:
        summary = _clean_text(analysis_result.get("gap_summary"))
        confirmation_prompt = _clean_text(analysis_result.get("user_confirmation_prompt"))
        candidate_label = candidate_workflow_name or candidate_workflow_id
        final_response_text = (
            f"{base_response_text}\n\n"
            f"Potential workflow gap identified: {summary}\n"
            f"Candidate workflow created: {candidate_label} ({candidate_workflow_id}).\n"
            f"{confirmation_prompt or 'Do you want me to use this candidate workflow?'}"
        ).strip()
        outcome = "candidate_created_awaiting_confirmation"
    elif decision == "create_and_retry" and candidate_workflow_id:
        test_report = (
            dict(context.get("workflow_gap_test_report") or {})
            if isinstance(context.get("workflow_gap_test_report"), Mapping)
            else {}
        )
        failure_reason = _clean_text(test_report.get("reason"))
        candidate_label = candidate_workflow_name or candidate_workflow_id
        final_response_text = (
            f"{base_response_text}\n\n"
            f"I created candidate workflow {candidate_label} ({candidate_workflow_id}), "
            "but the immediate test did not satisfy the explicit acceptance requirements."
            f"{f' Reason: {failure_reason}' if failure_reason else ''}"
            f"{f' {actionable_escalation}' if actionable_escalation else ''}"
        ).strip()
        outcome = "candidate_created_test_failed"

    outputs = {
        "workflow_gap_recovery_outcome": outcome,
        "workflow_gap_final_response_text": final_response_text or base_response_text,
        "workflow_gap_final_extra_messages": final_extra_messages,
        "workflow_gap_final_tool_invocations": final_tool_invocations,
        "workflow_gap_candidate_workflow_id": candidate_workflow_id or None,
        "workflow_gap_candidate_prompt_concept_id": candidate_prompt_concept_id or None,
        "workflow_gap_repair_attempt_count": attempt_count,
        "workflow_gap_repair_attempts_exhausted": attempts_exhausted,
        "workflow_gap_actionable_escalation": actionable_escalation,
        "workflow_gap_failure_evidence": failure_evidence or None,
    }
    if prompt_link_error:
        outputs["workflow_gap_candidate_prompt_link_error"] = prompt_link_error
    return WorkflowActionResult(status="success", outputs=outputs)


def build_workflow_discovery_gap_recovery_workflow_test_definition() -> WorkflowDefinition:
    collect_context = WorkflowStateSpec(
        state_id="collect_context",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_GAP_COLLECT_CONTEXT_ACTION_ID,
                description="Collect recent turn context and fallback execution evidence.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="analyse_gap",
                condition=lambda _ctx: True,
                reason="context_collected",
            ),
        ),
    )

    analyse_gap = WorkflowStateSpec(
        state_id="analyse_gap",
        actions=(
            WorkflowActionInvocation(
                action_id="workflow_gap.analyse_recovery",
                description="Analyse the discovery miss and decide whether a candidate workflow should be created.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="prepare_candidate",
                condition=lambda ctx: bool(ctx.get("workflow_gap_should_create_candidate")),
                reason="candidate_needed",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="no_candidate_needed",
            ),
        ),
    )

    prepare_candidate = WorkflowStateSpec(
        state_id="prepare_candidate",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID,
                description="Build and persist the candidate workflow specification and prompt support.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="create_candidate",
                condition=lambda _ctx: True,
                reason="candidate_prepared",
            ),
        ),
    )

    create_candidate = WorkflowStateSpec(
        state_id="create_candidate",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                inputs={
                    "workflow_id": WORKFLOW_CREATION_WORKFLOW_ID,
                    **dict(WORKFLOW_GAP_CREATION_FAILURE_MODE_BINDINGS),
                    "workflow_spec": {
                        "$context_key": "candidate_workflow_spec",
                        "$mapping_concept_id": WORKFLOW_GAP_CREATE_WORKFLOW_SPEC_MAPPING_ID,
                    },
                    "test_run_inputs": {
                        "$context_key": "candidate_workflow_creation_test_inputs",
                        "$mapping_concept_id": WORKFLOW_GAP_CREATE_TEST_INPUTS_MAPPING_ID,
                    },
                    "__parent_workflow_id": WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
                    "__parent_state_id": _workflow_gap_step_concept_id(
                        WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
                        "create_candidate",
                    ),
                },
                description="Create the candidate workflow in Vontology and verify its structural executability.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="decide_test",
                condition=lambda _ctx: True,
                reason="candidate_created_or_failed_closed",
            ),
        ),
        metadata={
            "subworkflow_contract": build_subworkflow_contract(
                workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
                input_mappings=[
                    {
                        "child_input_key": "workflow_spec",
                        "parent_context_key": "candidate_workflow_spec",
                        "mapping_concept_id": WORKFLOW_GAP_CREATE_WORKFLOW_SPEC_MAPPING_ID,
                    },
                    {
                        "child_input_key": "test_run_inputs",
                        "parent_context_key": "candidate_workflow_creation_test_inputs",
                        "mapping_concept_id": WORKFLOW_GAP_CREATE_TEST_INPUTS_MAPPING_ID,
                    },
                ],
                output_mappings=[
                    {
                        "child_output_field": item.child_output_field,
                        "parent_context_key": item.context_key,
                        "mapping_concept_id": item.concept_id,
                    }
                    for item in WORKFLOW_GAP_CREATE_TOOL_OUTPUT_MAPPINGS
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
                for item in WORKFLOW_GAP_CREATE_TOOL_OUTPUT_MAPPINGS
            ],
            "writes_context_keys": list(WORKFLOW_GAP_CREATE_WRITES_CONTEXT_KEYS),
        },
    )

    decide_test = WorkflowStateSpec(
        state_id="decide_test",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
                description="Decide whether the newly created candidate workflow should be tested immediately.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="prepare_candidate",
                condition=lambda ctx: bool(ctx.get("workflow_gap_should_repair_candidate_now")),
                reason="repair_candidate_again",
            ),
            WorkflowTransitionSpec(
                to_state="test_candidate",
                condition=lambda ctx: bool(ctx.get("workflow_gap_should_test_candidate_now")),
                reason="test_candidate_now",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="skip_test",
            ),
        ),
    )

    test_candidate = WorkflowStateSpec(
        state_id="test_candidate",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                inputs={
                    "workflow_id": WORKFLOW_GAP_TEST_WORKFLOW_ID,
                    **dict(WORKFLOW_GAP_TEST_FAILURE_MODE_BINDINGS),
                    "candidate_workflow_id": {
                        "$context_key": "created_candidate_workflow_id",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_WORKFLOW_ID_MAPPING_ID,
                    },
                    "prompt": {
                        "$context_key": "workflow_gap_request_text",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_PROMPT_MAPPING_ID,
                    },
                    "workflow_gap_recent_turns": {
                        "$context_key": "workflow_gap_recent_turns",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_RECENT_TURNS_MAPPING_ID,
                    },
                    "workflow_gap_acceptance_requirements": {
                        "$context_key": "workflow_gap_acceptance_requirements",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_ACCEPTANCE_MAPPING_ID,
                    },
                    "workflow_gap_base_response_text": {
                        "$context_key": "workflow_gap_base_response_text",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_BASE_RESPONSE_MAPPING_ID,
                    },
                    "user_concept_id": {
                        "$context_key": "user_concept_id",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_USER_CONCEPT_MAPPING_ID,
                    },
                    "org_concept_id": {
                        "$context_key": "org_concept_id",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_ORG_CONCEPT_MAPPING_ID,
                    },
                    "conversation_session_id": {
                        "$context_key": "conversation_session_id",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_SESSION_ID_MAPPING_ID,
                    },
                    "turn_id": {
                        "$context_key": "turn_id",
                        "$mapping_concept_id": WORKFLOW_GAP_TEST_TURN_ID_MAPPING_ID,
                    },
                    "__parent_workflow_id": WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
                    "__parent_state_id": _workflow_gap_step_concept_id(
                        WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
                        "test_candidate",
                    ),
                },
                description="Run the candidate workflow inside the dedicated test workflow and evaluate the acceptance requirements.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="decide_test",
                condition=lambda ctx: not bool(ctx.get("workflow_gap_test_passed")),
                reason="candidate_test_requires_repair_decision",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="candidate_test_finished",
            ),
        ),
        metadata={
            "subworkflow_contract": build_subworkflow_contract(
                workflow_id=WORKFLOW_GAP_TEST_WORKFLOW_ID,
                input_mappings=[
                    {
                        "child_input_key": "candidate_workflow_id",
                        "parent_context_key": "created_candidate_workflow_id",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_WORKFLOW_ID_MAPPING_ID,
                    },
                    {
                        "child_input_key": "prompt",
                        "parent_context_key": "workflow_gap_request_text",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_PROMPT_MAPPING_ID,
                    },
                    {
                        "child_input_key": "workflow_gap_recent_turns",
                        "parent_context_key": "workflow_gap_recent_turns",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_RECENT_TURNS_MAPPING_ID,
                    },
                    {
                        "child_input_key": "workflow_gap_acceptance_requirements",
                        "parent_context_key": "workflow_gap_acceptance_requirements",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_ACCEPTANCE_MAPPING_ID,
                    },
                    {
                        "child_input_key": "workflow_gap_base_response_text",
                        "parent_context_key": "workflow_gap_base_response_text",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_BASE_RESPONSE_MAPPING_ID,
                    },
                    {
                        "child_input_key": "user_concept_id",
                        "parent_context_key": "user_concept_id",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_USER_CONCEPT_MAPPING_ID,
                    },
                    {
                        "child_input_key": "org_concept_id",
                        "parent_context_key": "org_concept_id",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_ORG_CONCEPT_MAPPING_ID,
                    },
                    {
                        "child_input_key": "conversation_session_id",
                        "parent_context_key": "conversation_session_id",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_SESSION_ID_MAPPING_ID,
                    },
                    {
                        "child_input_key": "turn_id",
                        "parent_context_key": "turn_id",
                        "mapping_concept_id": WORKFLOW_GAP_TEST_TURN_ID_MAPPING_ID,
                    },
                ],
                output_mappings=[
                    {
                        "child_output_field": item.child_output_field,
                        "parent_context_key": item.context_key,
                        "mapping_concept_id": item.concept_id,
                    }
                    for item in WORKFLOW_GAP_TEST_TOOL_OUTPUT_MAPPINGS
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
                for item in WORKFLOW_GAP_TEST_TOOL_OUTPUT_MAPPINGS
            ],
            "writes_context_keys": list(WORKFLOW_GAP_TEST_WRITES_CONTEXT_KEYS),
        },
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
                description="Finalise workflow-gap recovery and compute the final user-facing result.",
            ),
        ),
        terminal=True,
    )
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
        initial_state="collect_context",
        states={
            "collect_context": collect_context,
            "analyse_gap": analyse_gap,
            "prepare_candidate": prepare_candidate,
            "create_candidate": create_candidate,
            "decide_test": decide_test,
            "test_candidate": test_candidate,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Recover from workflow-discovery misses by creating and optionally testing a candidate workflow."
        ),
    )


def build_workflow_gap_test_definition() -> WorkflowDefinition:
    run_test = WorkflowStateSpec(
        state_id="run_test",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
                description="Execute the candidate workflow and evaluate the explicit acceptance requirements.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="test_evaluated",
            ),
        ),
    )
    complete = WorkflowStateSpec(state_id="complete", terminal=True)
    failed = WorkflowStateSpec(state_id="failed", terminal=True)
    return WorkflowDefinition(
        workflow_id=WORKFLOW_GAP_TEST_WORKFLOW_ID,
        initial_state="run_test",
        states={
            "run_test": run_test,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose="Evaluate whether a newly created candidate workflow satisfies workflow-gap acceptance requirements.",
    )


def build_workflow_discovery_gap_recovery_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
        definition=build_workflow_discovery_gap_recovery_workflow_test_definition(),
        purpose=(
            "Analyse workflow-discovery misses, create candidate workflows in Vontology, and retry them when confidence is high enough."
        ),
        source="built_in",
    )


def build_workflow_gap_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=WORKFLOW_GAP_TEST_WORKFLOW_ID,
        definition=build_workflow_gap_test_definition(),
        purpose=(
            "Run and evaluate candidate workflows against explicit acceptance requirements before replacing a fallback response."
        ),
        source="built_in",
    )


def register_workflow_gap_recovery_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_GAP_COLLECT_CONTEXT_ACTION_ID,
            handler=_handle_collect_context,
            description="Collect recent turn context and fallback routing evidence for workflow-gap recovery.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id="workflow_gap.analyse_recovery",
            handler=_handle_analyse_recovery,
            description="Analyse a workflow-discovery miss and decide whether a candidate workflow should be created.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_GAP_PREPARE_CANDIDATE_ACTION_ID,
            handler=_handle_prepare_candidate_spec,
            description="Prepare the candidate workflow specification and prompt support for workflow-gap recovery.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
            handler=_handle_decide_test,
            description="Decide whether the newly created candidate workflow should be tested immediately.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
            handler=_handle_execute_candidate,
            description="Execute a candidate workflow via a nested orchestrator run using the candidate prompt concept.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
            handler=_handle_run_candidate_test,
            description="Run and evaluate a candidate workflow against explicit acceptance requirements.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
            handler=_handle_finalise_recovery,
            description="Finalise workflow-gap recovery and compute the replacement or follow-up response.",
        )
    )


__all__ = [
    "build_workflow_discovery_gap_recovery_workflow_test_definition",
    "build_workflow_gap_test_definition",
    "build_workflow_discovery_gap_recovery_workflow_test_registration",
    "build_workflow_gap_test_registration",
    "register_workflow_gap_recovery_actions",
]
