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


def backfill_turn_execution_records_from_chat_history(
    *,
    namespace: str,
    limit_sessions: int = 500,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Backfill turn_execution_records from chat_history assistant messages.

    This scans sessions in the provided namespace and extracts
    `history[].llm_debug_data.turn_execution_record` payloads for assistant turns.
    By default this runs in dry-run mode to report potential backfill volume
    without mutating Mongo.
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
        "history.llm_debug_data.turn_execution_record": 1,
    }
    cursor = chat_history_coll.find(query, projection).limit(session_limit)

    sessions_scanned = 0
    assistant_messages_scanned = 0
    records_found = 0
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

        for message in history:
            if not isinstance(message, Mapping):
                continue
            if message.get("role") != "assistant":
                continue
            assistant_messages_scanned += 1

            llm_debug = message.get("llm_debug_data")
            if not isinstance(llm_debug, Mapping):
                continue
            record = llm_debug.get("turn_execution_record")
            if not isinstance(record, Mapping):
                continue
            records_found += 1

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
                    "llm_debug_data.turn_execution_record, so direct projection backfill "
                    "cannot populate turn_execution_records."
                ),
                "assistant_messages_scanned": assistant_messages_scanned,
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
        "sessions_scanned": sessions_scanned,
        "assistant_messages_scanned": assistant_messages_scanned,
        "records_found": records_found,
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
