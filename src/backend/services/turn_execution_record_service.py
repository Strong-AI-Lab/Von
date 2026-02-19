"""Turn execution record builder and projection storage utilities.

This service provides a canonical `turn_execution_record.v1` payload for each
assistant turn and a query-optimised Mongo projection collection
(`turn_execution_records`) for cross-conversation failure analysis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure, PyMongoError

from ..db.mongo_client import get_db
from ..workflows.conversation_turn_stage_model import (
    build_conversation_turn_stage_model_snapshot,
    build_conversation_turn_stage_path,
)

logger = logging.getLogger(__name__)

TURN_EXECUTION_RECORD_SCHEMA_VERSION = "turn_execution_record.v1"
TURN_EXECUTION_RECORDS_COLLECTION = "turn_execution_records"

_TURN_EXECUTION_INDEXES_READY = False
_TURN_EXECUTION_INDEXES_LOCK = threading.Lock()

_WRITE_TOOL_NAMES = {
    "add_issue_comment",
    "add_names",
    "add_relationship",
    "assign_copilot_to_issue",
    "assign_task",
    "create_branch",
    "create_concepts",
    "create_or_update_file",
    "create_pull_request",
    "create_repository",
    "create_task",
    "delete_concept",
    "delete_file",
    "delete_text_relation",
    "download_paper",
    "finalise_cached_paper",
    "gmail_modify_labels",
    "issue_write",
    "jira_add_attachment",
    "jira_add_comment",
    "jira_create_issue",
    "jira_link_issue",
    "jira_transition",
    "jira_update_issue",
    "merge_concepts",
    "merge_pull_request",
    "mcp__github__add_issue_comment",
    "mcp__github__create_pull_request",
    "mcp__github__update_pull_request",
    "pull_request_review_write",
    "push_files",
    "remove_relationship",
    "sub_issue_write",
    "upsert_renderer_profile",
    "upsert_singleton_text_relation",
    "upsert_text_relation",
    "update_concept",
    "update_pull_request",
    "update_pull_request_branch",
    "update_task_status",
    "update_text_relation",
    "workflow_bind_event",
    "workflow_create_instance",
    "workflow_create_schedule",
    "workflow_delete_schedule",
    "workflow_set_schedule_enabled",
}

_READ_ONLY_JIRA_TOOLS = {
    "jira_get_auth_config",
    "jira_get_issue",
    "jira_get_myself",
    "jira_get_transitions",
    "jira_search",
}

_MUTATION_INTENT_TERMS = (
    "add",
    "added",
    "apply",
    "create",
    "created",
    "delete",
    "link",
    "modify",
    "modified",
    "proceed with",
    "remove",
    "rename",
    "set",
    "update",
    "updated",
)

_KB_TERMS = (
    "concept",
    "kb",
    "knowledge base",
    "ontology",
    "predicate",
    "predicates",
    "relation",
    "relations",
    "relationship",
    "relationships",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime | None = None) -> str:
    dt = value or _now_utc()
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return _iso_utc()


def _hash_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_payload(value: Any) -> str | None:
    if value is None:
        return None
    try:
        payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_write_tool(tool_name: str | None) -> bool:
    if not isinstance(tool_name, str):
        return False
    cleaned = tool_name.strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if lowered.startswith("__"):
        return False
    if lowered in _WRITE_TOOL_NAMES:
        return True
    if lowered.startswith("jira_"):
        return lowered not in _READ_ONLY_JIRA_TOOLS
    if lowered.startswith("workflow_"):
        return lowered not in {
            "workflow_get_instance",
            "workflow_get_schedule",
            "workflow_list_definitions",
            "workflow_list_event_bindings",
            "workflow_list_instances",
            "workflow_list_schedules",
            "workflow_mcp_health_check",
            "workflow_trigger_schedule",
        }
    return False


def _extract_workflow_ids(raw_items: Any) -> list[str]:
    if not isinstance(raw_items, list):
        return []
    collected: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        workflow_id: str | None = None
        if isinstance(item, str):
            workflow_id = _safe_str(item)
        elif isinstance(item, Mapping):
            for key in ("concept_id", "workflow_id", "id"):
                workflow_id = _safe_str(item.get(key))
                if workflow_id:
                    break
        if not workflow_id:
            continue
        lowered = workflow_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        collected.append(workflow_id)
    return collected


def _extract_workflow_discovery(
    workflow_discovery: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    if not isinstance(workflow_discovery, Mapping):
        return {"candidate_ids": [], "excluded_candidate_ids": []}

    candidate_ids = _extract_workflow_ids(workflow_discovery.get("candidate_ids"))
    if not candidate_ids:
        candidate_ids = _extract_workflow_ids(workflow_discovery.get("candidates"))
    if not candidate_ids:
        candidate_ids = _extract_workflow_ids(workflow_discovery.get("matches"))

    excluded_ids = _extract_workflow_ids(
        workflow_discovery.get("excluded_candidate_ids")
    )
    if not excluded_ids:
        excluded_ids = _extract_workflow_ids(workflow_discovery.get("excluded"))
    if not excluded_ids:
        excluded_ids = _extract_workflow_ids(
            workflow_discovery.get("excluded_candidates")
        )

    return {
        "candidate_ids": candidate_ids,
        "excluded_candidate_ids": excluded_ids,
    }


def _extract_completion_claim_signals(
    *,
    response_text: Any,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    detected_count = 0
    validation_seen = False
    verified_count = 0
    not_verified_count = 0

    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        entry_type = _safe_str(entry.get("type"))
        if entry_type == "completion_claim_detection":
            try:
                detected_count = max(
                    detected_count, int(entry.get("claim_count") or 0)
                )
            except Exception:
                continue
        if entry_type == "completion_claim_validation":
            validation_seen = True
            try:
                verified_count = max(verified_count, int(entry.get("verified_count") or 0))
            except Exception:
                pass
            try:
                not_verified_count = max(
                    not_verified_count, int(entry.get("not_verified_count") or 0)
                )
            except Exception:
                pass

    if detected_count <= 0 and isinstance(response_text, str):
        # Conservative fallback for non-orchestrator paths where aux telemetry
        # may be absent.
        lowered = response_text.lower()
        if re.search(r"\b(completed|done|finished|applied|updated|added)\b", lowered):
            detected_count = 1

    detected = detected_count > 0
    if not detected:
        validated = True
    elif validation_seen:
        validated = not_verified_count == 0
    else:
        validated = False

    return {
        "detected": detected,
        "detected_count": detected_count,
        "validation_seen": validation_seen,
        "verified_count": verified_count,
        "not_verified_count": not_verified_count,
        "validated": validated,
    }


def _summarise_tool_invocations(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str], list[str]]:
    serialised: list[dict[str, Any]] = []
    successful_tools: list[str] = []
    failed_tools: list[str] = []
    blocked_tools: list[str] = []
    successful_write_tools: list[str] = []

    for invocation in tool_invocations or ():
        if not isinstance(invocation, Mapping):
            continue

        tool_name = _safe_str(invocation.get("tool")) or _safe_str(
            invocation.get("method")
        )
        if not tool_name:
            continue

        payload_value = invocation.get("effective_payload")
        if not isinstance(payload_value, Mapping):
            payload_value = invocation.get("payload")

        error_value = _safe_str(invocation.get("error"))
        blocked = bool(invocation.get("blocked"))
        payload_status = ""
        if isinstance(payload_value, Mapping):
            payload_status = (
                _safe_str(payload_value.get("status")) or ""
            ).lower()

        status = "ok"
        if blocked:
            status = "blocked"
        elif error_value or payload_status in {"error", "failed", "failure"}:
            status = "error"

        result_summary = _safe_str(invocation.get("result_summary"))
        if not result_summary and isinstance(payload_value, Mapping):
            result_summary = _safe_str(payload_value.get("result_summary")) or _safe_str(
                payload_value.get("summary")
            )

        serialised.append(
            {
                "tool": tool_name,
                "status": status,
                "started_at_utc": None,
                "completed_at_utc": None,
                "error": error_value,
                "result_summary": result_summary,
                "payload_fingerprint": _hash_payload(payload_value),
            }
        )

        lowered = tool_name.lower()
        if status == "ok":
            if lowered not in {name.lower() for name in successful_tools}:
                successful_tools.append(tool_name)
            if _is_write_tool(tool_name) and lowered not in {
                name.lower() for name in successful_write_tools
            }:
                successful_write_tools.append(tool_name)
        elif status == "blocked":
            if lowered not in {name.lower() for name in blocked_tools}:
                blocked_tools.append(tool_name)
        else:
            if lowered not in {name.lower() for name in failed_tools}:
                failed_tools.append(tool_name)

    return (
        serialised,
        successful_tools,
        failed_tools,
        blocked_tools,
        successful_write_tools,
    )


def _infer_mutation_required_effect(
    *,
    prompt_text: Any,
    successful_write_tools: Sequence[str],
    failed_tools: Sequence[str],
    blocked_tools: Sequence[str],
) -> dict[str, Any] | None:
    prompt_clean = prompt_text if isinstance(prompt_text, str) else ""
    lowered = prompt_clean.lower()

    has_mutation_term = any(token in lowered for token in _MUTATION_INTENT_TERMS)
    has_kb_term = any(token in lowered for token in _KB_TERMS)
    explicit_missing_relation_phrase = any(
        phrase in lowered
        for phrase in (
            "relations were not added",
            "relation was not added",
            "predicates were not added",
            "proceed with the predicates",
            "proceed with predicates",
        )
    )

    mutation_intent = explicit_missing_relation_phrase or (
        has_mutation_term and has_kb_term
    )
    if not mutation_intent and not successful_write_tools and not failed_tools and not blocked_tools:
        return None

    effect_status = "pending"
    status_reason: str | None = None
    if successful_write_tools:
        effect_status = "satisfied"
        status_reason = (
            "Observed successful write tool invocation(s): "
            + ", ".join(successful_write_tools[:3])
        )
    elif failed_tools or blocked_tools:
        effect_status = "not_satisfied"
        names = [*failed_tools, *blocked_tools]
        status_reason = (
            "Write attempt failed or was blocked: " + ", ".join(names[:3])
            if names
            else "Write attempt failed or was blocked."
        )
    else:
        effect_status = "not_executed"
        status_reason = "No write-capable tool invocation was observed."

    required_tools = list(successful_write_tools[:3]) or ["add_relationship"]
    return {
        "effect_id": "effect_1",
        "intent_origin": "implicit" if mutation_intent else "explicit",
        "effect_type": "kb_mutation",
        "description": "Apply or repair requested knowledge-base relation/predicate changes.",
        "required_tools": required_tools,
        "targets": [],
        "required_predicates": [],
        "postcondition_required": True,
        "postcondition_strategy": "state_requery",
        "status": effect_status,
        "status_reason": status_reason,
    }


def _build_postcondition_checks(
    *,
    required_effects: Sequence[Mapping[str, Any]],
    successful_write_tools: Sequence[str],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for effect in required_effects:
        effect_id = _safe_str(effect.get("effect_id")) or "effect_1"
        effect_status = _safe_str(effect.get("status")) or "pending"
        if effect_status == "satisfied" and successful_write_tools:
            check_status = "inconclusive"
            evidence = (
                "Write tool invocation succeeded but explicit state re-query was not run."
            )
        elif effect_status in {"not_satisfied", "not_executed"}:
            check_status = "not_verified"
            evidence = "Required mutation effect is unresolved."
        else:
            check_status = "inconclusive"
            evidence = "Mutation verification outcome is inconclusive."

        checks.append(
            {
                "check_id": f"check_{effect_id}",
                "effect_id": effect_id,
                "check_type": "predicate_exists",
                "check_tool": "derived.turn_execution",
                "check_payload": {},
                "observed": {
                    "successful_write_tools": list(successful_write_tools),
                    "effect_status": effect_status,
                },
                "status": check_status,
                "evidence": evidence,
                "error": None,
            }
        )
    return checks


def _summarise_check_counts(
    checks: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    summary = {
        "verified_count": 0,
        "not_verified_count": 0,
        "inconclusive_count": 0,
        "error_count": 0,
    }
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        status = (_safe_str(check.get("status")) or "").lower()
        if status == "verified":
            summary["verified_count"] += 1
        elif status == "not_verified":
            summary["not_verified_count"] += 1
        elif status == "inconclusive":
            summary["inconclusive_count"] += 1
        elif status == "error":
            summary["error_count"] += 1
    return summary


def _derive_completion_gate(
    *,
    required_effects: Sequence[Mapping[str, Any]],
    postcondition_checks: Sequence[Mapping[str, Any]],
    completion_claim_validated: bool,
) -> dict[str, Any]:
    decision = "completed"
    decision_reason = "No blocking effect detected."
    blocking_effect_ids: list[str] = []

    unresolved_effect_ids: list[str] = []
    for effect in required_effects:
        effect_status = _safe_str(effect.get("status")) or ""
        effect_id = _safe_str(effect.get("effect_id")) or "effect_1"
        if effect_status in {"not_satisfied", "not_executed"}:
            unresolved_effect_ids.append(effect_id)

    unresolved_check_ids: list[str] = []
    for check in postcondition_checks:
        check_status = _safe_str(check.get("status")) or ""
        if check_status in {"not_verified", "inconclusive", "error"}:
            unresolved_check_ids.append(_safe_str(check.get("effect_id")) or "effect_1")

    if unresolved_effect_ids:
        blocking_effect_ids = sorted(set(unresolved_effect_ids))
        if any(
            (_safe_str(effect.get("status")) or "") == "not_satisfied"
            for effect in required_effects
        ):
            decision = "failed"
            decision_reason = "Mutation attempt failed or was blocked."
        else:
            decision = "escalation_required"
            decision_reason = "Required mutation was not executed."
    elif unresolved_check_ids:
        blocking_effect_ids = sorted(set(unresolved_check_ids))
        decision = "partial"
        decision_reason = "Mutation execution observed but postcondition verification is inconclusive."

    if decision == "completed" and not completion_claim_validated:
        decision = "partial"
        decision_reason = (
            "Completion claim was detected but could not be fully validated."
        )

    safe_to_claim = decision == "completed"
    return {
        "workflow_id": "#V#turn_completion_gate_workflow",
        "decision": decision,
        "decision_reason": decision_reason,
        "blocking_effect_ids": blocking_effect_ids,
        "safe_to_claim_completion": safe_to_claim,
        "requires_follow_up": not safe_to_claim,
    }


def build_turn_execution_record(
    *,
    request_id: Any,
    session_id: Any,
    namespace: Any,
    user_id: Any,
    org_id: Any,
    prompt_text: Any,
    response_text: Any,
    interaction_timestamp_utc: Any,
    workflow_discovery: Mapping[str, Any] | None = None,
    workflow_routing: Mapping[str, Any] | None = None,
    tool_invocations: Sequence[Mapping[str, Any]] | None = None,
    turn_execution_diagnostics: Mapping[str, Any] | None = None,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    workflow_discovery_normalised = _extract_workflow_discovery(workflow_discovery)
    selected_workflow_id = None
    selector_verdict = None
    selector_source = "default"

    if isinstance(workflow_routing, Mapping):
        selected_workflow_id = _safe_str(workflow_routing.get("workflow_id"))
        selector_verdict = _safe_str(workflow_routing.get("verdict"))
        raw_source = (_safe_str(workflow_routing.get("source")) or "default").lower()
        selector_source = "workflow_selector" if raw_source == "selector" else raw_source

    (
        serialised_invocations,
        successful_tools,
        failed_tools,
        blocked_tools,
        successful_write_tools,
    ) = _summarise_tool_invocations(tool_invocations)

    required_effects: list[dict[str, Any]] = []
    mutation_effect = _infer_mutation_required_effect(
        prompt_text=prompt_text,
        successful_write_tools=successful_write_tools,
        failed_tools=failed_tools,
        blocked_tools=blocked_tools,
    )
    if mutation_effect is not None:
        required_effects.append(mutation_effect)

    postcondition_checks = _build_postcondition_checks(
        required_effects=required_effects,
        successful_write_tools=successful_write_tools,
    )
    critic_summary = _summarise_check_counts(postcondition_checks)

    completion_claim = _extract_completion_claim_signals(
        response_text=response_text,
        aux_llm_calls=aux_llm_calls,
    )

    completion_gate = _derive_completion_gate(
        required_effects=required_effects,
        postcondition_checks=postcondition_checks,
        completion_claim_validated=bool(completion_claim["validated"]),
    )

    latest_progress = (
        turn_execution_diagnostics.get("latest_progress")
        if isinstance(turn_execution_diagnostics, Mapping)
        else None
    )
    diagnostic_events = []
    retry = {"attempts": 0, "budget": 0, "reason": None}
    if isinstance(latest_progress, Mapping):
        raw_events = latest_progress.get("diagnostic_events")
        if isinstance(raw_events, list):
            diagnostic_events = [item for item in raw_events if isinstance(item, Mapping)]
        try:
            retry["attempts"] = int(latest_progress.get("retry_attempts") or 0)
        except Exception:
            retry["attempts"] = 0
        try:
            retry["budget"] = int(latest_progress.get("retry_budget") or 0)
        except Exception:
            retry["budget"] = 0
        retry["reason"] = _safe_str(latest_progress.get("retry_reason"))

    runtime_stages: list[str] = []
    for event in diagnostic_events:
        if not isinstance(event, Mapping):
            continue
        phase = _safe_str(event.get("phase"))
        stage = _safe_str(event.get("stage"))
        if phase:
            runtime_stages.append(phase)
        if stage:
            runtime_stages.append(stage)

    phase_history = (
        turn_execution_diagnostics.get("phase_history")
        if isinstance(turn_execution_diagnostics, Mapping)
        else None
    )
    if isinstance(phase_history, list):
        for entry in phase_history:
            if not isinstance(entry, Mapping):
                continue
            phase = _safe_str(entry.get("phase"))
            if phase:
                runtime_stages.append(phase)

    workflow_stage_path = (
        turn_execution_diagnostics.get("workflow_stage_path")
        if isinstance(turn_execution_diagnostics, Mapping)
        else None
    )
    if not isinstance(workflow_stage_path, Mapping):
        workflow_stage_path = build_conversation_turn_stage_path(
            runtime_stages=runtime_stages,
            workflow_id=selected_workflow_id,
        )

    return {
        "schema_version": TURN_EXECUTION_RECORD_SCHEMA_VERSION,
        "request_id": _safe_str(request_id),
        "session_id": _safe_str(session_id),
        "namespace": _safe_str(namespace),
        "user_id": _safe_str(user_id),
        "org_id": _safe_str(org_id),
        "created_at_utc": _normalise_iso_timestamp(interaction_timestamp_utc),
        "prompt": {
            "preview": (
                prompt_text[:1000] if isinstance(prompt_text, str) else _safe_str(prompt_text)
            ),
            "sha256": _hash_text(prompt_text),
            "source": "user_message",
        },
        "workflow_selection": {
            "selected_workflow_id": selected_workflow_id,
            "selector_verdict": selector_verdict,
            "selector_source": selector_source,
            "workflow_discovery": workflow_discovery_normalised,
        },
        "required_effects": required_effects,
        "execution": {
            "tool_invocations": serialised_invocations,
            "diagnostic_events": diagnostic_events,
            "retry": retry,
            "workflow_stage_model": build_conversation_turn_stage_model_snapshot(),
            "workflow_stage_path": workflow_stage_path,
        },
        "postcondition_checks": postcondition_checks,
        "critic": {
            "enabled": True,
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
            "summary": critic_summary,
        },
        "completion_gate": completion_gate,
        "final_response": {
            "response_sha256": _hash_text(response_text),
            "completion_claim_detected": bool(completion_claim["detected"]),
            "completion_claim_validated": bool(completion_claim["validated"]),
        },
    }


def _ensure_turn_execution_indexes(collection) -> None:
    global _TURN_EXECUTION_INDEXES_READY
    if _TURN_EXECUTION_INDEXES_READY:
        return

    with _TURN_EXECUTION_INDEXES_LOCK:
        if _TURN_EXECUTION_INDEXES_READY:
            return
        try:
            existing_indexes = [idx.get("name") for idx in collection.list_indexes()]
            if "request_id_unique" not in existing_indexes:
                collection.create_index(
                    [("request_id", ASCENDING)],
                    unique=True,
                    name="request_id_unique",
                )
            if "namespace_created_desc" not in existing_indexes:
                collection.create_index(
                    [("namespace", ASCENDING), ("created_at_utc", DESCENDING)],
                    name="namespace_created_desc",
                )
            if "decision_created_desc" not in existing_indexes:
                collection.create_index(
                    [
                        ("completion_gate.decision", ASCENDING),
                        ("created_at_utc", DESCENDING),
                    ],
                    name="decision_created_desc",
                )
            if "effect_status_created_desc" not in existing_indexes:
                collection.create_index(
                    [("required_effects.status", ASCENDING), ("created_at_utc", DESCENDING)],
                    name="effect_status_created_desc",
                )
            if "workflow_created_desc" not in existing_indexes:
                collection.create_index(
                    [
                        ("workflow_selection.selected_workflow_id", ASCENDING),
                        ("created_at_utc", DESCENDING),
                    ],
                    name="workflow_created_desc",
                )
        except OperationFailure as exc:
            logger.warning(
                "Index creation partially failed for turn_execution_records: %s",
                exc,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Could not ensure turn_execution_records indexes: %s",
                exc,
            )
        _TURN_EXECUTION_INDEXES_READY = True


def get_turn_execution_records_collection():
    db = get_db()
    if db is None:
        return None
    coll = db[TURN_EXECUTION_RECORDS_COLLECTION]
    _ensure_turn_execution_indexes(coll)
    return coll


def upsert_turn_execution_record_projection(
    *,
    record: Mapping[str, Any],
    user_id: str | None = None,
    session_id: str | None = None,
    namespace: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {"updated": False, "reason": "invalid_record"}

    request_id = _safe_str(record.get("request_id"))
    if not request_id:
        return {"updated": False, "reason": "missing_request_id"}

    coll = get_turn_execution_records_collection()
    if coll is None:
        return {"updated": False, "reason": "collection_unavailable"}

    now = _now_utc()
    payload = dict(record)
    payload["request_id"] = request_id
    if _safe_str(user_id):
        payload.setdefault("user_id", _safe_str(user_id))
    if _safe_str(session_id):
        payload.setdefault("session_id", _safe_str(session_id))
    if _safe_str(namespace):
        payload.setdefault("namespace", _safe_str(namespace))
    if _safe_str(org_id):
        payload.setdefault("org_id", _safe_str(org_id))
    payload.setdefault("schema_version", TURN_EXECUTION_RECORD_SCHEMA_VERSION)
    payload.setdefault("created_at_utc", _iso_utc(now))
    payload["updated_at_utc"] = _iso_utc(now)

    try:
        result = coll.update_one(
            {"request_id": request_id},
            {
                "$set": payload,
                "$setOnInsert": {"inserted_at": now},
            },
            upsert=True,
        )
        updated = bool(getattr(result, "modified_count", 0) > 0)
        inserted = getattr(result, "upserted_id", None) is not None
        matched = bool(getattr(result, "matched_count", 0) > 0)
        return {
            "updated": updated or inserted,
            "inserted": inserted,
            "matched": matched,
            "request_id": request_id,
        }
    except PyMongoError as exc:
        logger.warning(
            "Failed to upsert turn_execution_records projection for request_id=%s: %s",
            request_id,
            exc,
        )
        return {
            "updated": False,
            "reason": "mongo_error",
            "request_id": request_id,
        }


def _normalise_positive_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _get_nested_value(payload: Any, path: str) -> Any:
    if not isinstance(payload, Mapping):
        return None
    current: Any = payload
    for raw_part in path.split("."):
        part = raw_part.strip()
        if not part:
            continue
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _safe_percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _safe_count_documents(collection: Any, query: Mapping[str, Any]) -> int:
    if collection is None:
        return 0
    if hasattr(collection, "count_documents"):
        try:
            return int(collection.count_documents(dict(query)))
        except Exception:
            pass
    try:
        return sum(1 for _ in collection.find(dict(query), {"_id": 1}))
    except Exception:
        return 0


def _normalise_timestamp_for_range(value: Any) -> str | None:
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return None


def _append_unique_text(
    target: list[str],
    seen: set[str],
    *,
    value: Any,
    limit: int,
) -> None:
    if len(target) >= limit:
        return
    text = _safe_str(value)
    if not text:
        return
    lowered = text.lower()
    if lowered in seen:
        return
    seen.add(lowered)
    target.append(text)


def _collect_namespace_candidates(
    *,
    chat_history_coll: Any,
    turn_records_coll: Any,
    limit_namespaces: int,
) -> list[str]:
    namespace_items: list[str] = []
    seen: set[str] = set()

    def _collect_from_distinct(collection: Any, field: str) -> None:
        if collection is None or len(namespace_items) >= limit_namespaces:
            return
        if hasattr(collection, "distinct"):
            try:
                values = collection.distinct(field)
            except TypeError:
                try:
                    values = collection.distinct(field, {})
                except Exception:
                    values = None
            except Exception:
                values = None
            if isinstance(values, list):
                for raw_value in values:
                    _append_unique_text(
                        namespace_items,
                        seen,
                        value=raw_value,
                        limit=limit_namespaces,
                    )
                    if len(namespace_items) >= limit_namespaces:
                        return

    def _collect_from_find(collection: Any, field: str) -> None:
        if collection is None or len(namespace_items) >= limit_namespaces:
            return
        try:
            cursor = collection.find({}, {field: 1})
        except Exception:
            return
        for doc in cursor:
            value = _get_nested_value(doc, field)
            _append_unique_text(
                namespace_items,
                seen,
                value=value,
                limit=limit_namespaces,
            )
            if len(namespace_items) >= limit_namespaces:
                return

    _collect_from_distinct(chat_history_coll, "namespace")
    _collect_from_distinct(turn_records_coll, "namespace")
    if not namespace_items:
        _collect_from_find(chat_history_coll, "namespace")
        _collect_from_find(turn_records_coll, "namespace")
    namespace_items.sort()
    return namespace_items


def build_turn_execution_namespace_coverage_report(
    *,
    namespace: str | None = None,
    limit_namespaces: int = 25,
    limit_sessions_per_namespace: int = 200,
    limit_projected_records_per_namespace: int = 10000,
) -> dict[str, Any]:
    """Build namespace-level turn execution coverage metrics.

    This report compares assistant-message instrumentation in chat history
    against projected turn_execution_records so reliability gaps can be
    quantified before failure-mining benchmarks are interpreted.
    """

    namespace_filter = _safe_str(namespace)
    namespaces_limit = _normalise_positive_int(
        limit_namespaces,
        default=25,
        minimum=1,
        maximum=500,
    )
    session_limit = _normalise_positive_int(
        limit_sessions_per_namespace,
        default=200,
        minimum=1,
        maximum=10000,
    )
    projected_limit = _normalise_positive_int(
        limit_projected_records_per_namespace,
        default=10000,
        minimum=1,
        maximum=200000,
    )

    db = get_db()
    if db is None:
        return {"success": False, "error": "db_unavailable"}

    try:
        chat_history_coll = db["chat_history"]
        turn_records_coll = db[TURN_EXECUTION_RECORDS_COLLECTION]
    except Exception:
        return {"success": False, "error": "collection_unavailable"}

    if namespace_filter:
        namespaces = [namespace_filter]
    else:
        namespaces = _collect_namespace_candidates(
            chat_history_coll=chat_history_coll,
            turn_records_coll=turn_records_coll,
            limit_namespaces=namespaces_limit,
        )

    namespace_reports: list[dict[str, Any]] = []
    aggregate_assistant = 0
    aggregate_embedded = 0
    aggregate_debug = 0
    aggregate_projected = 0
    aggregate_overlap = 0
    aggregate_history_request_ids = 0
    capability_gap_index: dict[str, dict[str, Any]] = {}

    for namespace_value in namespaces[:namespaces_limit]:
        session_query = {"namespace": namespace_value}
        session_projection = {
            "user_id": 1,
            "session_id": 1,
            "history.role": 1,
            "history.timestamp": 1,
            "history.llm_debug_data": 1,
        }
        try:
            session_cursor = chat_history_coll.find(session_query, session_projection).limit(
                session_limit
            )
        except Exception:
            session_cursor = []

        sessions_scanned = 0
        assistant_messages_scanned = 0
        assistant_messages_with_llm_debug_data = 0
        assistant_messages_with_request_id = 0
        assistant_messages_with_turn_execution_record = 0
        history_request_ids: set[str] = set()
        earliest_assistant_timestamp: str | None = None
        latest_assistant_timestamp: str | None = None

        for session_doc in session_cursor:
            if not isinstance(session_doc, Mapping):
                continue
            sessions_scanned += 1
            history_items = session_doc.get("history")
            if not isinstance(history_items, list):
                continue

            for message in history_items:
                if not isinstance(message, Mapping):
                    continue
                if message.get("role") != "assistant":
                    continue
                assistant_messages_scanned += 1

                timestamp_value = _normalise_timestamp_for_range(
                    message.get("timestamp")
                )
                if timestamp_value:
                    if (
                        earliest_assistant_timestamp is None
                        or timestamp_value < earliest_assistant_timestamp
                    ):
                        earliest_assistant_timestamp = timestamp_value
                    if (
                        latest_assistant_timestamp is None
                        or timestamp_value > latest_assistant_timestamp
                    ):
                        latest_assistant_timestamp = timestamp_value

                llm_debug_data = message.get("llm_debug_data")
                if not isinstance(llm_debug_data, Mapping):
                    continue
                assistant_messages_with_llm_debug_data += 1

                debug_request_id = _safe_str(llm_debug_data.get("request_id"))
                if debug_request_id:
                    assistant_messages_with_request_id += 1
                    history_request_ids.add(debug_request_id)

                turn_record = llm_debug_data.get("turn_execution_record")
                if not isinstance(turn_record, Mapping):
                    continue
                assistant_messages_with_turn_execution_record += 1

                record_request_id = _safe_str(turn_record.get("request_id"))
                if record_request_id:
                    history_request_ids.add(record_request_id)

        projected_query = {"namespace": namespace_value}
        projected_records_total = _safe_count_documents(turn_records_coll, projected_query)
        projected_projection = {
            "request_id": 1,
            "created_at_utc": 1,
            "completion_gate.decision": 1,
            "workflow_selection.selected_workflow_id": 1,
        }
        try:
            projected_cursor = turn_records_coll.find(
                projected_query, projected_projection
            ).limit(projected_limit)
        except Exception:
            projected_cursor = []

        projected_records_scanned = 0
        projected_records_missing_request_id = 0
        projected_records_missing_decision = 0
        projected_records_missing_workflow = 0
        projected_request_ids: set[str] = set()
        projected_created_min: str | None = None
        projected_created_max: str | None = None

        for projected_doc in projected_cursor:
            if not isinstance(projected_doc, Mapping):
                continue
            projected_records_scanned += 1

            request_id = _safe_str(projected_doc.get("request_id"))
            if request_id:
                projected_request_ids.add(request_id)
            else:
                projected_records_missing_request_id += 1

            decision = _safe_str(
                _get_nested_value(projected_doc, "completion_gate.decision")
            )
            if not decision:
                projected_records_missing_decision += 1

            workflow_id = _safe_str(
                _get_nested_value(
                    projected_doc,
                    "workflow_selection.selected_workflow_id",
                )
            )
            if not workflow_id:
                projected_records_missing_workflow += 1

            created_at_utc = _normalise_timestamp_for_range(
                projected_doc.get("created_at_utc")
            )
            if created_at_utc:
                if projected_created_min is None or created_at_utc < projected_created_min:
                    projected_created_min = created_at_utc
                if projected_created_max is None or created_at_utc > projected_created_max:
                    projected_created_max = created_at_utc

        overlap_count = len(history_request_ids.intersection(projected_request_ids))
        history_request_id_count = len(history_request_ids)

        namespace_gaps: list[dict[str, Any]] = []
        if assistant_messages_scanned > 0 and assistant_messages_with_turn_execution_record == 0:
            namespace_gaps.append(
                {
                    "gap_id": "no_embedded_turn_execution_record_in_history",
                    "severity": "high",
                    "description": (
                        "Assistant turns exist but none include llm_debug_data.turn_execution_record."
                    ),
                }
            )
        if assistant_messages_scanned > 0 and projected_records_total == 0:
            namespace_gaps.append(
                {
                    "gap_id": "no_turn_execution_records_projection",
                    "severity": "high",
                    "description": (
                        "No projected turn_execution_records were found for this namespace."
                    ),
                }
            )
        if history_request_id_count > 0 and overlap_count == 0:
            namespace_gaps.append(
                {
                    "gap_id": "no_request_id_overlap_between_history_and_projection",
                    "severity": "medium",
                    "description": (
                        "History request IDs and projected request IDs did not overlap in the sampled window."
                    ),
                }
            )
        if projected_records_scanned > 0 and projected_records_missing_decision > 0:
            namespace_gaps.append(
                {
                    "gap_id": "projection_missing_completion_decision",
                    "severity": "medium",
                    "description": (
                        "Some projected records are missing completion_gate.decision."
                    ),
                    "missing_count": projected_records_missing_decision,
                }
            )
        if projected_records_scanned > 0 and projected_records_missing_workflow > 0:
            namespace_gaps.append(
                {
                    "gap_id": "projection_missing_selected_workflow_id",
                    "severity": "low",
                    "description": (
                        "Some projected records are missing workflow_selection.selected_workflow_id."
                    ),
                    "missing_count": projected_records_missing_workflow,
                }
            )

        for gap in namespace_gaps:
            gap_id = _safe_str(gap.get("gap_id"))
            if not gap_id:
                continue
            bucket = capability_gap_index.setdefault(
                gap_id,
                {
                    "gap_id": gap_id,
                    "severity": _safe_str(gap.get("severity")) or "medium",
                    "title": gap_id.replace("_", " "),
                    "description": _safe_str(gap.get("description")) or "",
                    "evidence_count": 0,
                    "namespaces": [],
                },
            )
            bucket["evidence_count"] = int(bucket.get("evidence_count", 0)) + 1
            namespaces_with_gap = bucket.get("namespaces")
            if isinstance(namespaces_with_gap, list):
                if namespace_value not in namespaces_with_gap:
                    namespaces_with_gap.append(namespace_value)

        namespace_report = {
            "namespace": namespace_value,
            "sessions_scanned": sessions_scanned,
            "assistant_messages_scanned": assistant_messages_scanned,
            "assistant_messages_with_llm_debug_data": assistant_messages_with_llm_debug_data,
            "assistant_messages_with_request_id": assistant_messages_with_request_id,
            "assistant_messages_with_turn_execution_record": (
                assistant_messages_with_turn_execution_record
            ),
            "history_request_id_count": history_request_id_count,
            "projected_records_total": projected_records_total,
            "projected_records_scanned": projected_records_scanned,
            "projected_records_scan_truncated": projected_records_total
            > projected_records_scanned,
            "projected_records_missing_request_id": projected_records_missing_request_id,
            "projected_records_missing_completion_decision": projected_records_missing_decision,
            "projected_records_missing_selected_workflow_id": projected_records_missing_workflow,
            "request_id_overlap_count": overlap_count,
            "assistant_embedded_record_rate_pct": _safe_percentage(
                assistant_messages_with_turn_execution_record,
                assistant_messages_scanned,
            ),
            "assistant_llm_debug_rate_pct": _safe_percentage(
                assistant_messages_with_llm_debug_data,
                assistant_messages_scanned,
            ),
            "request_id_overlap_rate_pct": _safe_percentage(
                overlap_count,
                history_request_id_count,
            ),
            "assistant_message_timestamp_range": {
                "earliest": earliest_assistant_timestamp,
                "latest": latest_assistant_timestamp,
            },
            "projected_created_at_utc_range": {
                "earliest": projected_created_min,
                "latest": projected_created_max,
            },
            "gap_signals": namespace_gaps,
        }
        namespace_reports.append(namespace_report)

        aggregate_assistant += assistant_messages_scanned
        aggregate_embedded += assistant_messages_with_turn_execution_record
        aggregate_debug += assistant_messages_with_llm_debug_data
        aggregate_projected += projected_records_total
        aggregate_overlap += overlap_count
        aggregate_history_request_ids += history_request_id_count

    capability_gaps = sorted(
        capability_gap_index.values(),
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(
                _safe_str(item.get("severity")) or "medium", 3
            ),
            -int(item.get("evidence_count", 0)),
            _safe_str(item.get("gap_id")) or "",
        ),
    )

    if not namespaces and not namespace_filter:
        capability_gaps.append(
            {
                "gap_id": "no_namespaces_found",
                "severity": "high",
                "title": "No namespaces found",
                "description": (
                    "No namespaces were discovered in chat_history or turn_execution_records."
                ),
                "evidence_count": 0,
                "namespaces": [],
            }
        )

    return {
        "success": True,
        "generated_at_utc": _iso_utc(),
        "namespace_filter": namespace_filter,
        "limit_namespaces": namespaces_limit,
        "limit_sessions_per_namespace": session_limit,
        "limit_projected_records_per_namespace": projected_limit,
        "namespaces_scanned": len(namespace_reports),
        "coverage_by_namespace": namespace_reports,
        "aggregate": {
            "assistant_messages_scanned": aggregate_assistant,
            "assistant_messages_with_llm_debug_data": aggregate_debug,
            "assistant_messages_with_turn_execution_record": aggregate_embedded,
            "projected_records_total": aggregate_projected,
            "history_request_id_count": aggregate_history_request_ids,
            "request_id_overlap_count": aggregate_overlap,
            "assistant_embedded_record_rate_pct": _safe_percentage(
                aggregate_embedded, aggregate_assistant
            ),
            "assistant_llm_debug_rate_pct": _safe_percentage(
                aggregate_debug, aggregate_assistant
            ),
            "request_id_overlap_rate_pct": _safe_percentage(
                aggregate_overlap, aggregate_history_request_ids
            ),
        },
        "capability_gaps": capability_gaps,
    }


def infer_turn_execution_workflow_routing_from_debug(
    *, llm_debug: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if not isinstance(llm_debug, Mapping):
        return None
    existing = llm_debug.get("workflow_routing")
    if isinstance(existing, Mapping):
        cleaned: dict[str, Any] = {}
        for key in ("workflow_id", "verdict", "source"):
            value = _safe_str(existing.get(key))
            if value:
                cleaned[key] = value
        if cleaned:
            return cleaned

    tool_invocations = (
        llm_debug.get("tool_invocations")
        if isinstance(llm_debug.get("tool_invocations"), list)
        else []
    )
    if tool_invocations:
        has_write_tools = False
        for raw_invocation in tool_invocations:
            if not isinstance(raw_invocation, Mapping):
                continue
            if _is_write_tool(_safe_str(raw_invocation.get("name"))):
                has_write_tools = True
                break
        return {
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "mutation_tooling" if has_write_tools else "tool_seeking",
            "source": "synthesised",
        }

    return {
        "workflow_id": "#V#chat_assistant_workflow",
        "verdict": "plain_response",
        "source": "synthesised",
    }


def backfill_turn_execution_records_from_chat_history(
    *,
    namespace: str,
    limit_sessions: int = 500,
    dry_run: bool = True,
    synthesise_missing_records: bool = True,
) -> dict[str, Any]:
    """Backfill turn_execution_records from chat_history assistant messages.

    This scans sessions in the provided namespace and extracts
    `history[].llm_debug_data.turn_execution_record` payloads for assistant turns.
    When embedded records are missing, optional synthesis can reconstruct records
    from legacy llm_debug_data + message context. By default this runs in dry-run
    mode to report potential backfill volume without mutating Mongo.
    """

    namespace_value = _safe_str(namespace)
    if not namespace_value:
        return {"success": False, "error": "namespace_required"}

    session_limit = _normalise_positive_int(
        limit_sessions,
        default=500,
        minimum=1,
        maximum=5000,
    )
    run_dry = bool(dry_run)
    run_synthesis = bool(synthesise_missing_records)

    db = get_db()
    if db is None:
        return {"success": False, "error": "db_unavailable"}

    try:
        chat_history_coll = db["chat_history"]
    except Exception:
        return {"success": False, "error": "chat_history_collection_unavailable"}

    query = {"namespace": namespace_value}
    projection = {
        "user_id": 1,
        "session_id": 1,
        "organisation_concept_id": 1,
        "history.role": 1,
        "history.content": 1,
        "history.timestamp": 1,
        "history.llm_debug_data": 1,
    }
    cursor = chat_history_coll.find(query, projection).limit(session_limit)

    sessions_scanned = 0
    assistant_messages_scanned = 0
    assistant_messages_with_llm_debug_data = 0
    assistant_messages_with_request_id = 0
    records_found = 0
    embedded_records_found = 0
    synthesised_records_found = 0
    candidate_records = 0
    upserted_count = 0
    inserted_count = 0
    skipped_missing_request_id = 0
    skipped_invalid_record = 0
    skipped_missing_user_or_session = 0
    failure_count = 0
    skipped_reasons: dict[str, int] = {}
    example_request_ids: list[str] = []

    for session_doc in cursor:
        if not isinstance(session_doc, Mapping):
            continue
        sessions_scanned += 1
        user_id = _safe_str(session_doc.get("user_id"))
        session_id = _safe_str(session_doc.get("session_id"))
        org_id = _safe_str(session_doc.get("organisation_concept_id"))
        history = session_doc.get("history")
        if not isinstance(history, list):
            continue

        latest_user_prompt: str | None = None
        for message in history:
            if not isinstance(message, Mapping):
                continue
            role = message.get("role")
            if role == "user":
                latest_user_prompt = _safe_str(message.get("content")) or latest_user_prompt
                continue
            if role != "assistant":
                continue
            assistant_messages_scanned += 1

            llm_debug = message.get("llm_debug_data")
            if not isinstance(llm_debug, Mapping):
                continue
            assistant_messages_with_llm_debug_data += 1

            debug_request_id = _safe_str(llm_debug.get("request_id"))
            if debug_request_id:
                assistant_messages_with_request_id += 1
            tool_invocations = (
                llm_debug.get("tool_invocations")
                if isinstance(llm_debug.get("tool_invocations"), list)
                else []
            )
            workflow_routing = infer_turn_execution_workflow_routing_from_debug(
                llm_debug=llm_debug
            )

            record = llm_debug.get("turn_execution_record")
            record_source = "embedded"
            if not isinstance(record, Mapping):
                if not run_synthesis or not debug_request_id:
                    continue
                try:
                    record = build_turn_execution_record(
                        request_id=debug_request_id,
                        session_id=session_id,
                        namespace=namespace_value,
                        user_id=user_id,
                        org_id=org_id,
                        prompt_text=latest_user_prompt,
                        response_text=_safe_str(message.get("content")),
                        interaction_timestamp_utc=(
                            llm_debug.get("interaction_timestamp_utc")
                            or message.get("timestamp")
                        ),
                        workflow_discovery=(
                            llm_debug.get("workflow_discovery")
                            if isinstance(llm_debug.get("workflow_discovery"), Mapping)
                            else None
                        ),
                        workflow_routing=workflow_routing,
                        tool_invocations=tool_invocations,
                        turn_execution_diagnostics=(
                            llm_debug.get("turn_execution_diagnostics")
                            if isinstance(
                                llm_debug.get("turn_execution_diagnostics"), Mapping
                            )
                            else None
                        ),
                        aux_llm_calls=(
                            llm_debug.get("aux_llm_calls")
                            if isinstance(llm_debug.get("aux_llm_calls"), list)
                            else []
                        ),
                    )
                    if isinstance(record, dict):
                        record["reconstruction"] = {
                            "source": "chat_history.llm_debug_data",
                            "method": "build_turn_execution_record",
                            "synthesised_at_utc": _iso_utc(),
                        }
                    record_source = "synthesised_from_llm_debug_data"
                except Exception:
                    skipped_reasons["record_synthesis_failed"] = (
                        skipped_reasons.get("record_synthesis_failed", 0) + 1
                    )
                    failure_count += 1
                    continue

            if not isinstance(record, Mapping):
                continue
            if isinstance(record, dict):
                workflow_selection = record.get("workflow_selection")
                if not isinstance(workflow_selection, dict):
                    workflow_selection = {}
                    record["workflow_selection"] = workflow_selection
                if (
                    not _safe_str(workflow_selection.get("selected_workflow_id"))
                    and isinstance(workflow_routing, Mapping)
                ):
                    inferred_workflow_id = _safe_str(workflow_routing.get("workflow_id"))
                    inferred_verdict = _safe_str(workflow_routing.get("verdict"))
                    inferred_source = _safe_str(workflow_routing.get("source"))
                    if inferred_workflow_id:
                        workflow_selection["selected_workflow_id"] = inferred_workflow_id
                    if inferred_verdict:
                        workflow_selection["selector_verdict"] = inferred_verdict
                    if inferred_source:
                        workflow_selection["selector_source"] = inferred_source
            records_found += 1
            if record_source == "embedded":
                embedded_records_found += 1
            else:
                synthesised_records_found += 1

            request_id = _safe_str(record.get("request_id"))
            if not request_id:
                skipped_missing_request_id += 1
                skipped_reasons["missing_request_id"] = (
                    skipped_reasons.get("missing_request_id", 0) + 1
                )
                continue

            if request_id not in example_request_ids:
                example_request_ids.append(request_id)

            if not user_id or not session_id:
                skipped_missing_user_or_session += 1
                skipped_reasons["missing_user_or_session"] = (
                    skipped_reasons.get("missing_user_or_session", 0) + 1
                )
                continue

            candidate_records += 1
            if run_dry:
                continue

            outcome = upsert_turn_execution_record_projection(
                record=record,
                user_id=user_id,
                session_id=session_id,
                namespace=namespace_value,
                org_id=org_id,
            )
            if bool(outcome.get("updated", False)):
                upserted_count += 1
                if bool(outcome.get("inserted", False)):
                    inserted_count += 1
                continue

            reason = _safe_str(outcome.get("reason")) or "unknown"
            if reason == "invalid_record":
                skipped_invalid_record += 1
            elif reason == "missing_request_id":
                skipped_missing_request_id += 1
            elif reason in {"mongo_error", "collection_unavailable"}:
                failure_count += 1
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1

    gap_signals: list[dict[str, Any]] = []
    if sessions_scanned > 0 and assistant_messages_scanned > 0 and records_found == 0:
        gap_signals.append(
            {
                "gap_id": "no_embedded_turn_execution_record_in_history",
                "severity": "high",
                "description": (
                    "Assistant messages were present but none contained "
                    "recoverable turn execution records in llm_debug_data."
                ),
                "assistant_messages_scanned": assistant_messages_scanned,
            }
        )
    if embedded_records_found == 0 and synthesised_records_found > 0:
        gap_signals.append(
            {
                "gap_id": "embedded_turn_execution_records_missing_recovered_by_synthesis",
                "severity": "medium",
                "description": (
                    "Embedded turn_execution_record payloads were absent, but record "
                    "synthesis from llm_debug_data recovered candidate records."
                ),
                "synthesised_records_found": synthesised_records_found,
            }
        )
    if records_found > 0 and candidate_records == 0:
        gap_signals.append(
            {
                "gap_id": "no_backfill_candidates_after_validation",
                "severity": "medium",
                "description": (
                    "Turn execution records were found in history but none qualified for "
                    "upsert (likely missing request_id or user/session context)."
                ),
                "records_found": records_found,
            }
        )

    return {
        "success": True,
        "namespace": namespace_value,
        "dry_run": run_dry,
        "limit_sessions": session_limit,
        "synthesise_missing_records": run_synthesis,
        "sessions_scanned": sessions_scanned,
        "assistant_messages_scanned": assistant_messages_scanned,
        "assistant_messages_with_llm_debug_data": assistant_messages_with_llm_debug_data,
        "assistant_messages_with_request_id": assistant_messages_with_request_id,
        "records_found": records_found,
        "embedded_records_found": embedded_records_found,
        "synthesised_records_found": synthesised_records_found,
        "candidate_records": candidate_records,
        "upserted_count": upserted_count,
        "inserted_count": inserted_count,
        "skipped_missing_request_id": skipped_missing_request_id,
        "skipped_invalid_record": skipped_invalid_record,
        "skipped_missing_user_or_session": skipped_missing_user_or_session,
        "failure_count": failure_count,
        "skipped_reasons": skipped_reasons,
        "example_request_ids": example_request_ids[:20],
        "gap_signals": gap_signals,
    }
