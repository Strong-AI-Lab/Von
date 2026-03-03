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
    "remove_relationships_bulk",
    "undo_relationship_removal",
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

_VERIFICATION_READ_TOOL_NAMES = {
    "count",
    "fetch_concept",
    "fetch_concept_content",
    "find_concepts_by_name",
    "find_relations_with_argument",
    "find_subconcepts",
    "get_context",
    "get_file_contents",
    "get_paper_metadata",
    "get_task",
    "get_team_members",
    "get_teams",
    "get_text_relations",
    "get_text_relations_summary",
    "get_tree",
    "issue_read",
    "jira_get_issue",
    "jira_get_myself",
    "jira_get_transitions",
    "jira_search",
    "list_my_tasks",
    "list_pull_requests",
    "resolve_concept_by_name",
    "search_code",
    "search_concepts",
    "search_issues",
    "search_pull_requests",
    "search_repositories",
    "search_users",
    "search_knowledge_base",
    "vontology_concept_search",
    "workflow_get_instance",
    "workflow_get_schedule",
    "workflow_list_definitions",
    "workflow_list_event_bindings",
    "workflow_list_instances",
    "workflow_list_schedules",
}

_VERIFICATION_READ_TOOL_PREFIXES = (
    "count_",
    "fetch_",
    "find_",
    "get_",
    "list_",
    "read_",
    "resolve_",
    "search_",
)

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

_WRITE_OBJECT_TERMS = (
    "attachment",
    "branch",
    "comment",
    "concept",
    "file",
    "issue",
    "jira",
    "predicate",
    "pull request",
    "relation",
    "relationship",
    "schedule",
    "subtask",
    "task",
    "ticket",
    "todo",
    "to-do",
    "to do",
    "workflow",
)

_DIAGNOSTIC_ONLY_PHRASES = (
    "didn't actually",
    "did not actually",
    "don't program",
    "do not program",
    "explain why",
    "inspect the telemetry",
    "what went wrong",
    "why did not",
    "why didn't",
    "why was not",
    "why wasn't",
)

_PAPER_REPRESENTATION_INTENT_PATTERN = re.compile(
    r"\b("
    r"represent(?:ation|ing)?\s+(?:the\s+)?(?:corresponding\s+)?paper"
    r"|paper\s+representation"
    r"|represent\s+the\s+paper\s+not\s+the\s+file"
    r"|represent\s+the\s+paper\s+rather\s+than\s+the\s+file"
    r"|fully\s+represent\s+(?:the\s+)?(?:corresponding\s+)?paper"
    r"|scholarly\s+paper\s+representation"
    r")\b",
    flags=re.IGNORECASE,
)
_FILE_COPY_CONCEPT_ID_PATTERN = re.compile(
    r"#V#[A-Za-z0-9][A-Za-z0-9._-]*file_copy[A-Za-z0-9._-]*",
    flags=re.IGNORECASE,
)

_ACTION_REQUEST_MUTATION_PATTERN = re.compile(
    r"\b(?:can you|could you|go ahead(?: and)?|please|would you)\s+"
    r"(?:add|apply|attach|create|delete|link|modify|remove|rename|set|update)\b"
)

_NEGATED_MUTATION_PREFIX_PATTERN = re.compile(
    r"(?:did(?:n't| not)|was(?:n't| not)|were(?:n't| not)|not)\s+(?:actually\s+)?$"
)

_TOOL_CALLING_SELECTOR_VERDICTS = {"tool_seeking", "tool_calling"}

_TOOL_EXECUTION_FAILURE_REASON_MAP = {
    "worker_unavailable_zero_execution": "Tool execution was blocked while workers were unavailable.",
    "missing_tool_call_parse_error": "Tool call parsing failed before any tool execution occurred.",
    "missing_tool_call_retry_exhausted": "Tool-call recovery exhausted retries without executing a tool.",
    "missing_tool_call_unresolved": "Tool-calling was selected but no executable tool call was produced.",
}


def _has_affirmative_mutation_term(prompt_text: str) -> bool:
    if not prompt_text:
        return False
    for term in _MUTATION_INTENT_TERMS:
        start = 0
        while True:
            index = prompt_text.find(term, start)
            if index < 0:
                break

            before = prompt_text[index - 1] if index > 0 else " "
            end_index = index + len(term)
            after = prompt_text[end_index] if end_index < len(prompt_text) else " "
            if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
                start = index + len(term)
                continue

            window_start = max(0, index - 48)
            prefix = prompt_text[window_start:index]
            if _NEGATED_MUTATION_PREFIX_PATTERN.search(prefix):
                start = index + len(term)
                continue

            return True
    return False


def _looks_like_diagnostic_only_prompt(prompt_text: str) -> bool:
    if not prompt_text:
        return False
    if not any(phrase in prompt_text for phrase in _DIAGNOSTIC_ONLY_PHRASES):
        return False
    if _ACTION_REQUEST_MUTATION_PATTERN.search(prompt_text):
        return False
    if re.match(
        r"^\s*(?:add|apply|attach|create|delete|link|modify|remove|rename|set|update)\b",
        prompt_text,
    ):
        return False
    return True


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


def _safe_non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


def _normalise_failure_codes(raw_codes: Any) -> list[str]:
    if not isinstance(raw_codes, list):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw_codes:
        code = _safe_str(item)
        if not code:
            continue
        lowered = code.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(code)
    return ordered


def _derive_actor_concept_from_namespace(namespace: Any) -> str | None:
    namespace_value = _safe_str(namespace)
    if not namespace_value or not namespace_value.startswith("#V#"):
        return None
    namespace_body = namespace_value[3:].strip()
    if not namespace_body:
        return None
    user_slug = namespace_body.split("@", 1)[0].strip()
    if not user_slug:
        return None
    return f"#V#{user_slug}"


def _resolve_actor_concept_identity(
    *,
    actor_concept_id: Any,
    user_id: Any,
    namespace: Any,
) -> tuple[str | None, str]:
    explicit_actor = _safe_str(actor_concept_id)
    if explicit_actor and "@" not in explicit_actor:
        return explicit_actor, "actor_concept_id"

    explicit_user = _safe_str(user_id)
    if explicit_user and "@" not in explicit_user:
        return explicit_user, "user_id"

    namespace_actor = _derive_actor_concept_from_namespace(namespace)
    if namespace_actor:
        return namespace_actor, "namespace_user_component"

    return None, "missing"


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


def _is_verification_read_tool(tool_name: str | None) -> bool:
    if not isinstance(tool_name, str):
        return False
    cleaned = tool_name.strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if lowered.startswith("__"):
        return False
    if _is_write_tool(cleaned):
        return False
    if lowered in _VERIFICATION_READ_TOOL_NAMES:
        return True
    return any(
        lowered.startswith(prefix) for prefix in _VERIFICATION_READ_TOOL_PREFIXES
    )


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
) -> tuple[
    list[dict[str, Any]],
    list[str],
    list[str],
    list[str],
    list[str],
    list[str],
]:
    serialised: list[dict[str, Any]] = []
    successful_tools: list[str] = []
    failed_tools: list[str] = []
    blocked_tools: list[str] = []
    successful_write_tools: list[str] = []
    successful_verification_tools: list[str] = []

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
            if _is_verification_read_tool(tool_name) and lowered not in {
                name.lower() for name in successful_verification_tools
            }:
                successful_verification_tools.append(tool_name)
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
        successful_verification_tools,
    )


def _summarise_tool_execution_context(
    *,
    workflow_routing: Mapping[str, Any] | None,
    turn_execution_diagnostics: Mapping[str, Any] | None,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    serialised_invocations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    selected_workflow_id = (
        _safe_str(workflow_routing.get("workflow_id"))
        if isinstance(workflow_routing, Mapping)
        else None
    )
    selector_verdict = (
        _safe_str(workflow_routing.get("verdict"))
        if isinstance(workflow_routing, Mapping)
        else None
    )
    selector_verdict_lower = (selector_verdict or "").lower()
    selected_workflow_id_lower = (selected_workflow_id or "").lower()

    tool_route_selected = bool(
        selector_verdict_lower in _TOOL_CALLING_SELECTOR_VERDICTS
        or "tool_calling_workflow" in selected_workflow_id_lower
    )

    latest_progress = (
        turn_execution_diagnostics.get("latest_progress")
        if isinstance(turn_execution_diagnostics, Mapping)
        else None
    )
    counters = (
        latest_progress.get("counters")
        if isinstance(latest_progress, Mapping)
        else None
    )
    progress_tools_started = (
        _safe_non_negative_int(counters.get("tools_started"))
        if isinstance(counters, Mapping)
        else 0
    )
    progress_tools_completed = (
        _safe_non_negative_int(counters.get("tools_completed"))
        if isinstance(counters, Mapping)
        else 0
    )

    diagnostic_events = []
    if isinstance(latest_progress, Mapping):
        raw_events = latest_progress.get("diagnostic_events")
        if isinstance(raw_events, list):
            diagnostic_events = [
                event for event in raw_events if isinstance(event, Mapping)
            ]

    worker_unavailable_event_count = 0
    tool_call_start_event_count = 0
    tool_plan_stage_event_count = 0
    tool_execute_stage_event_count = 0
    for event in diagnostic_events:
        liveness_reason = (_safe_str(event.get("liveness_reason")) or "").lower()
        if liveness_reason == "worker_unavailable":
            worker_unavailable_event_count += 1
        event_kind = (_safe_str(event.get("event_kind")) or "").lower()
        if event_kind == "tool_call_start":
            tool_call_start_event_count += 1
        stage = (_safe_str(event.get("stage")) or "").lower()
        phase = (_safe_str(event.get("phase")) or "").lower()
        if "tool_plan" in {stage, phase}:
            tool_plan_stage_event_count += 1
        if "tool_execute" in {stage, phase}:
            tool_execute_stage_event_count += 1

    parse_error_invocation_count = 0
    validation_error_invocation_count = 0
    for invocation in serialised_invocations:
        if not isinstance(invocation, Mapping):
            continue
        tool_name = (_safe_str(invocation.get("tool")) or "").lower()
        if tool_name == "__tool_call_parse_error__":
            parse_error_invocation_count += 1
        elif tool_name == "__tool_call_validation_error__":
            validation_error_invocation_count += 1

    missing_tool_parse_error_count = 0
    missing_tool_retry_exhausted_count = 0
    missing_tool_unresolved_count = 0
    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        entry_type = (_safe_str(entry.get("type")) or "").lower()
        if entry_type == "missing_tool_call_detection":
            parse_error = _safe_str(entry.get("parse_error"))
            retry_reason = _safe_str(entry.get("retry_reason"))
            if parse_error:
                missing_tool_parse_error_count += 1
            if retry_reason:
                missing_tool_unresolved_count += 1
        elif entry_type == "missing_tool_call_retry":
            retry_reason = _safe_str(entry.get("retry_reason"))
            if not retry_reason:
                continue
            stage = (_safe_str(entry.get("stage")) or "").lower()
            retries_remaining = _safe_non_negative_int(entry.get("retries_remaining"))
            if stage == "skipped" or retries_remaining <= 0:
                missing_tool_retry_exhausted_count += 1

    invocation_count = len(serialised_invocations)
    observed_started_count = max(progress_tools_started, tool_call_start_event_count)
    observed_executed_count = max(progress_tools_completed, invocation_count)

    failure_codes: list[str] = []
    worker_unavailable_with_tool_expectation = (
        tool_plan_stage_event_count > 0
        or tool_execute_stage_event_count > 0
        or tool_call_start_event_count > 0
        or parse_error_invocation_count > 0
        or validation_error_invocation_count > 0
        or missing_tool_parse_error_count > 0
        or missing_tool_retry_exhausted_count > 0
        or missing_tool_unresolved_count > 0
    )
    if (
        tool_route_selected
        and observed_executed_count <= 0
        and worker_unavailable_event_count > 0
        and worker_unavailable_with_tool_expectation
    ):
        failure_codes.append("worker_unavailable_zero_execution")
    if missing_tool_parse_error_count > 0 or parse_error_invocation_count > 0:
        failure_codes.append("missing_tool_call_parse_error")
    if missing_tool_retry_exhausted_count > 0:
        failure_codes.append("missing_tool_call_retry_exhausted")
    if (
        observed_executed_count <= 0
        and missing_tool_unresolved_count > 0
        and tool_plan_stage_event_count > 0
        and tool_execute_stage_event_count <= 0
    ):
        failure_codes.append("missing_tool_call_unresolved")

    planned_count = max(
        observed_started_count,
        invocation_count,
        1 if (tool_route_selected and failure_codes) else 0,
    )

    return {
        "tool_route_selected": tool_route_selected,
        "selected_workflow_id": selected_workflow_id,
        "selector_verdict": selector_verdict,
        "planned_count": planned_count,
        "started_count": observed_started_count,
        "executed_count": observed_executed_count,
        "invocation_count": invocation_count,
        "worker_unavailable_event_count": worker_unavailable_event_count,
        "tool_plan_stage_event_count": tool_plan_stage_event_count,
        "tool_execute_stage_event_count": tool_execute_stage_event_count,
        "failure_codes": list(failure_codes),
        "zero_tools_executed": observed_executed_count <= 0,
        "parse_error_invocation_count": parse_error_invocation_count,
        "validation_error_invocation_count": validation_error_invocation_count,
    }


def _infer_tool_execution_required_effect(
    *,
    execution_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(execution_summary, Mapping):
        return None
    if not bool(execution_summary.get("tool_route_selected")):
        return None

    executed_count = _safe_non_negative_int(execution_summary.get("executed_count"))
    if executed_count > 0:
        return None

    failure_codes = _normalise_failure_codes(execution_summary.get("failure_codes"))
    if not failure_codes:
        return None

    primary_failure_code = failure_codes[0]
    status_reason = _TOOL_EXECUTION_FAILURE_REASON_MAP.get(
        primary_failure_code,
        "Tool-calling workflow selected but no tool execution was observed.",
    )

    return {
        "effect_id": "effect_tool_execution_1",
        "intent_origin": "workflow_contract",
        "effect_type": "tool_execution",
        "description": "Run at least one tool call for this tool-calling turn.",
        "required_tools": [],
        "targets": [],
        "required_predicates": [],
        "postcondition_required": True,
        "postcondition_strategy": "execution_observed",
        "status": "not_executed",
        "status_reason": status_reason,
        "failure_code": primary_failure_code,
        "failure_codes": list(failure_codes),
    }


def _extract_file_copy_concept_ids_from_text(prompt_text: Any) -> list[str]:
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        return []
    matches = _FILE_COPY_CONCEPT_ID_PATTERN.findall(prompt_text)
    concept_ids: list[str] = []
    seen: set[str] = set()
    for raw in matches:
        concept_id = _safe_str(raw)
        if not concept_id:
            continue
        lowered = concept_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        concept_ids.append(concept_id)
    return concept_ids


def _extract_required_scholarly_file_copy_ids_from_aux(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    concept_ids: list[str] = []
    seen: set[str] = set()
    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        entry_type = (_safe_str(entry.get("type")) or "").strip().lower()
        if entry_type not in {"prompt_tool_requirements", "workflow_selector_override"}:
            continue
        raw_values = entry.get("required_scholarly_representation_for_file_copy_ids")
        if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
            continue
        for raw in raw_values:
            concept_id = _safe_str(raw)
            if not concept_id:
                continue
            lowered = concept_id.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            concept_ids.append(concept_id)
    return concept_ids


def _infer_scholarly_representation_required_effect(
    *,
    prompt_text: Any,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
    successful_tools: Sequence[str],
    failed_tools: Sequence[str],
    blocked_tools: Sequence[str],
) -> dict[str, Any] | None:
    prompt_clean = prompt_text if isinstance(prompt_text, str) else ""
    if not _PAPER_REPRESENTATION_INTENT_PATTERN.search(prompt_clean):
        return None

    required_targets = _extract_required_scholarly_file_copy_ids_from_aux(aux_llm_calls)
    if not required_targets:
        required_targets = _extract_file_copy_concept_ids_from_text(prompt_text)
    if not required_targets:
        return None

    successful_lookup = {name.lower() for name in successful_tools if isinstance(name, str)}
    failed_lookup = {name.lower() for name in failed_tools if isinstance(name, str)}
    blocked_lookup = {name.lower() for name in blocked_tools if isinstance(name, str)}

    effect_status = "not_executed"
    status_reason = "No interpret_file_copy execution was observed."
    failure_code = "scholarly_representation_not_executed"
    if "interpret_file_copy" in successful_lookup:
        effect_status = "satisfied"
        status_reason = (
            "Observed scholarly representation tool invocation: interpret_file_copy."
        )
        failure_code = None
    elif "interpret_file_copy" in failed_lookup or "interpret_file_copy" in blocked_lookup:
        effect_status = "not_satisfied"
        status_reason = (
            "interpret_file_copy failed or was blocked for required scholarly representation."
        )
        failure_code = "scholarly_representation_tool_failed"

    effect: dict[str, Any] = {
        "effect_id": "effect_scholarly_representation_1",
        "intent_origin": "workflow_contract",
        "effect_type": "scholarly_representation",
        "description": (
            "Ensure the corresponding scholarly-paper concept is materialised from the "
            "file-copy context before final response completion."
        ),
        "required_tools": ["interpret_file_copy"],
        "targets": list(required_targets),
        "required_predicates": [
            "#V#computer_file_for_propositional_information_thing",
            "#V#propositional_information_thing_has_computer_file",
        ],
        "postcondition_required": True,
        "postcondition_strategy": "execution_observed",
        "status": effect_status,
        "status_reason": status_reason,
    }
    if failure_code:
        effect["failure_code"] = failure_code
        effect["failure_codes"] = [failure_code]
    else:
        effect["failure_codes"] = []
    return effect


def _infer_mutation_required_effect(
    *,
    prompt_text: Any,
    successful_write_tools: Sequence[str],
    failed_tools: Sequence[str],
    blocked_tools: Sequence[str],
) -> dict[str, Any] | None:
    prompt_clean = prompt_text if isinstance(prompt_text, str) else ""
    lowered = prompt_clean.lower()

    has_mutation_term = _has_affirmative_mutation_term(lowered)
    has_kb_term = any(token in lowered for token in _KB_TERMS)
    has_write_object_term = any(token in lowered for token in _WRITE_OBJECT_TERMS)
    diagnostic_only_prompt = _looks_like_diagnostic_only_prompt(lowered)
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
        has_mutation_term and (has_kb_term or has_write_object_term)
    )
    if diagnostic_only_prompt and not explicit_missing_relation_phrase:
        mutation_intent = False

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
    successful_verification_tools: Sequence[str],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for effect in required_effects:
        effect_id = _safe_str(effect.get("effect_id")) or "effect_1"
        effect_status = _safe_str(effect.get("status")) or "pending"
        effect_type = _safe_str(effect.get("effect_type")) or "kb_mutation"
        if effect_type == "tool_execution":
            if effect_status == "satisfied":
                check_status = "verified"
                evidence = "Required tool execution was observed."
                verification_mode = "execution_observed"
            elif effect_status in {"not_satisfied", "not_executed"}:
                check_status = "not_verified"
                evidence = (
                    _safe_str(effect.get("status_reason"))
                    or "Required tool execution was not observed."
                )
                verification_mode = "execution_missing"
            else:
                check_status = "inconclusive"
                evidence = "Tool execution verification outcome is inconclusive."
                verification_mode = "execution_inconclusive"
        elif effect_type == "scholarly_representation":
            if effect_status == "satisfied":
                check_status = "verified"
                evidence = (
                    _safe_str(effect.get("status_reason"))
                    or "Scholarly paper representation execution was observed."
                )
                verification_mode = "execution_observed"
            elif effect_status in {"not_satisfied", "not_executed"}:
                check_status = "not_verified"
                evidence = (
                    _safe_str(effect.get("status_reason"))
                    or "Scholarly paper representation execution was not observed."
                )
                verification_mode = "execution_missing"
            else:
                check_status = "inconclusive"
                evidence = "Scholarly paper representation verification is inconclusive."
                verification_mode = "execution_inconclusive"
        else:
            if effect_status == "satisfied" and successful_write_tools:
                if successful_verification_tools:
                    check_status = "verified"
                    evidence = (
                        "Observed postcondition verification read/check tool(s): "
                        + ", ".join(successful_verification_tools[:3])
                    )
                    verification_mode = "state_requery_observed"
                else:
                    check_status = "inconclusive"
                    evidence = (
                        "Write tool invocation succeeded but explicit state "
                        "re-query/check tool invocation was not observed."
                    )
                    verification_mode = "state_requery_missing"
            elif effect_status in {"not_satisfied", "not_executed"}:
                check_status = "not_verified"
                evidence = "Required mutation effect is unresolved."
                verification_mode = "state_requery_blocked"
            else:
                check_status = "inconclusive"
                evidence = "Mutation verification outcome is inconclusive."
                verification_mode = "state_requery_inconclusive"

        checks.append(
            {
                "check_id": f"check_{effect_id}",
                "effect_id": effect_id,
                "check_type": (
                    "tool_execution_observed"
                    if effect_type == "tool_execution"
                    else "scholarly_representation_observed"
                    if effect_type == "scholarly_representation"
                    else "predicate_exists"
                ),
                "check_tool": "derived.turn_execution",
                "check_payload": {},
                "observed": {
                    "successful_write_tools": list(successful_write_tools),
                    "successful_verification_tools": list(
                        successful_verification_tools
                    ),
                    "effect_status": effect_status,
                    "effect_type": effect_type,
                },
                "status": check_status,
                "verification_mode": verification_mode,
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
    blocking_failure_codes: list[str] = []

    unresolved_effect_ids: list[str] = []
    unresolved_effect_types: set[str] = set()
    unresolved_failure_codes: list[str] = []
    for effect in required_effects:
        effect_status = _safe_str(effect.get("status")) or ""
        effect_id = _safe_str(effect.get("effect_id")) or "effect_1"
        if effect_status in {"not_satisfied", "not_executed"}:
            unresolved_effect_ids.append(effect_id)
            effect_type = _safe_str(effect.get("effect_type"))
            if effect_type:
                unresolved_effect_types.add(effect_type)
            unresolved_failure_codes.extend(
                _normalise_failure_codes(effect.get("failure_codes"))
            )
            single_failure_code = _safe_str(effect.get("failure_code"))
            if single_failure_code:
                unresolved_failure_codes.append(single_failure_code)

    unresolved_check_ids: list[str] = []
    verified_check_effect_ids: list[str] = []
    unresolved_postcondition_checks: list[dict[str, Any]] = []
    for check in postcondition_checks:
        check_status = _safe_str(check.get("status")) or ""
        check_effect_id = _safe_str(check.get("effect_id")) or "effect_1"
        if check_status == "verified":
            verified_check_effect_ids.append(check_effect_id)
            continue
        if check_status in {"not_verified", "inconclusive", "error"}:
            unresolved_check_ids.append(check_effect_id)
            unresolved_postcondition_checks.append(
                {
                    "effect_id": check_effect_id,
                    "check_id": _safe_str(check.get("check_id")),
                    "check_type": _safe_str(check.get("check_type")),
                    "status": check_status,
                    "verification_mode": _safe_str(check.get("verification_mode")),
                    "evidence": _safe_str(check.get("evidence")),
                }
            )

    unresolved_preconditions: list[dict[str, Any]] = []
    for effect in required_effects:
        effect_status = _safe_str(effect.get("status")) or ""
        if effect_status not in {"not_satisfied", "not_executed"}:
            continue
        failure_codes = _normalise_failure_codes(effect.get("failure_codes"))
        single_failure_code = _safe_str(effect.get("failure_code"))
        if single_failure_code and single_failure_code not in failure_codes:
            failure_codes.append(single_failure_code)
        unresolved_preconditions.append(
            {
                "effect_id": _safe_str(effect.get("effect_id")) or "effect_1",
                "effect_type": _safe_str(effect.get("effect_type")) or "kb_mutation",
                "status": effect_status,
                "status_reason": _safe_str(effect.get("status_reason")),
                "failure_codes": failure_codes,
            }
        )

    if unresolved_effect_ids:
        blocking_effect_ids = sorted(set(unresolved_effect_ids))
        if unresolved_failure_codes:
            blocking_failure_codes = sorted(
                {
                    code
                    for code in unresolved_failure_codes
                    if isinstance(code, str) and code.strip()
                }
            )
        if any(
            (_safe_str(effect.get("status")) or "") == "not_satisfied"
            for effect in required_effects
        ):
            decision = "failed"
            decision_reason = "Mutation attempt failed or was blocked."
        else:
            decision = "escalation_required"
            if unresolved_effect_types == {"tool_execution"}:
                decision_reason = "Required tool execution was not observed."
            elif unresolved_effect_types == {"scholarly_representation"}:
                decision_reason = (
                    "Required scholarly paper representation was not executed."
                )
            else:
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
    evidence_payload = {
        "evaluation_basis": "required_effects_and_postcondition_checks",
        "required_effect_count": len(required_effects),
        "postcondition_check_count": len(postcondition_checks),
        "postcondition_summary": _summarise_check_counts(postcondition_checks),
        "verified_effect_ids": sorted(set(verified_check_effect_ids)),
        "unresolved_effect_ids": sorted(set(unresolved_check_ids)),
        "unresolved_preconditions": unresolved_preconditions,
        "unresolved_postcondition_checks": unresolved_postcondition_checks,
        "completion_outcome": (
            "success"
            if decision == "completed"
            else "inconclusive"
            if decision == "partial"
            else "failure"
        ),
    }
    return {
        "workflow_id": "#V#turn_completion_gate_workflow",
        "decision": decision,
        "decision_reason": decision_reason,
        "blocking_effect_ids": blocking_effect_ids,
        "blocking_failure_codes": blocking_failure_codes,
        "safe_to_claim_completion": safe_to_claim,
        "requires_follow_up": not safe_to_claim,
        "evidence_payload": evidence_payload,
    }


def build_turn_execution_record(
    *,
    request_id: Any,
    session_id: Any,
    namespace: Any,
    actor_concept_id: Any = None,
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
    resolved_actor_concept_id, actor_identity_source = _resolve_actor_concept_identity(
        actor_concept_id=actor_concept_id,
        user_id=user_id,
        namespace=namespace,
    )
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
        successful_verification_tools,
    ) = _summarise_tool_invocations(tool_invocations)
    execution_summary = _summarise_tool_execution_context(
        workflow_routing=workflow_routing,
        turn_execution_diagnostics=(
            turn_execution_diagnostics
            if isinstance(turn_execution_diagnostics, Mapping)
            else None
        ),
        aux_llm_calls=aux_llm_calls,
        serialised_invocations=serialised_invocations,
    )

    required_effects: list[dict[str, Any]] = []
    scholarly_representation_effect = _infer_scholarly_representation_required_effect(
        prompt_text=prompt_text,
        aux_llm_calls=aux_llm_calls,
        successful_tools=successful_tools,
        failed_tools=failed_tools,
        blocked_tools=blocked_tools,
    )
    if scholarly_representation_effect is not None:
        required_effects.append(scholarly_representation_effect)
    mutation_effect = _infer_mutation_required_effect(
        prompt_text=prompt_text,
        successful_write_tools=successful_write_tools,
        failed_tools=failed_tools,
        blocked_tools=blocked_tools,
    )
    if mutation_effect is not None:
        required_effects.append(mutation_effect)
    elif not successful_write_tools and scholarly_representation_effect is None:
        tool_execution_effect = _infer_tool_execution_required_effect(
            execution_summary=execution_summary
        )
        if tool_execution_effect is not None:
            required_effects.append(tool_execution_effect)

    postcondition_checks = _build_postcondition_checks(
        required_effects=required_effects,
        successful_write_tools=successful_write_tools,
        successful_verification_tools=successful_verification_tools,
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
        "actor_concept_id": resolved_actor_concept_id,
        "actor_identity_source": actor_identity_source,
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
            "summary": execution_summary,
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
                    actor_concept_id = _safe_str(llm_debug.get("actor_concept_id"))
                    record = build_turn_execution_record(
                        request_id=debug_request_id,
                        session_id=session_id,
                        namespace=namespace_value,
                        actor_concept_id=actor_concept_id,
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
