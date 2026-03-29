"""Deterministic routing from episode critiques to bounded remediation work."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import logging
from typing import Any, Mapping, Sequence

from .episode_critique_memory_service import (
    get_episode_critique_memories_collection,
    get_episode_critique_memory_state,
    record_episode_critique_memory_routing,
)
from .jira_deduplicated_issue_service import upsert_deduplicated_jira_issue
from .task_management_service import (
    create_task,
    find_task_by_external_reference,
    upsert_task_external_reference,
)

logger = logging.getLogger(__name__)

EPISODE_CRITIQUE_REMEDIATION_SOURCE_SYSTEM = "episode_critique_remediation"
EPISODE_CRITIQUE_REMEDIATION_SEARCH_LABEL = "episode-critique-remediation"

ROUTING_DECISION_MEMORY_ONLY = "memory_only"
ROUTING_DECISION_TASK_ONLY = "task_only"
ROUTING_DECISION_TASK_AND_JIRA = "task_and_jira"
ROUTING_DECISION_ROUTING_FAILED = "routing_failed"

_TASK_CONFIDENCE_THRESHOLD = 0.55
_JIRA_CONFIDENCE_THRESHOLD = 0.75
_TASK_REPEAT_THRESHOLD = 2
_JIRA_REPEAT_THRESHOLD = 3


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _normalise_strings(values: Any, *, limit: int = 60) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _merge_string_lists(*values: Any, limit: int = 60) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for items in values:
        for item in _normalise_strings(items, limit=limit):
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _hash_payload(value: Any, *, length: int = 24) -> str:
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()[:length]


def _root_causes_from_state(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    critic = _mapping_or_empty(state.get("critic"))
    assessment = _mapping_or_empty(critic.get("assessment"))
    rows = assessment.get("root_causes")
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for item in rows[:20]:
        if isinstance(item, Mapping):
            result.append(dict(item))
    return result


def _cause_tokens(state: Mapping[str, Any]) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for row in _root_causes_from_state(state):
        for key in ("cause_id", "rationale"):
            token = _safe_str(row.get(key))
            if not token:
                continue
            lowered = token.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            tokens.append(token)
            if len(tokens) >= 8:
                return tokens
    return tokens


def _severity_rank(state: Mapping[str, Any]) -> int:
    severity_order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    rank = 0
    for row in _root_causes_from_state(state):
        severity = (_safe_str(row.get("severity")) or "").lower()
        rank = max(rank, severity_order.get(severity, 0))
    critic = _mapping_or_empty(state.get("critic"))
    assessment = _mapping_or_empty(critic.get("assessment"))
    verdict = (_safe_str(critic.get("verdict")) or "").lower()
    unresolved = _safe_int(critic.get("unresolved_check_count"))
    if verdict == "follow_up_required":
        rank = max(rank, 3)
    elif verdict == "fail":
        rank = max(rank, 2)
    if unresolved >= 2:
        rank = max(rank, 2)
    if bool(assessment.get("maintenance_follow_up_recommended")):
        rank = max(rank, 3)
    return rank


def build_episode_critique_routing_fingerprint(state: Mapping[str, Any]) -> str:
    subject_episode = _mapping_or_empty(state.get("subject_episode"))
    critic = _mapping_or_empty(state.get("critic"))
    assessment = _mapping_or_empty(critic.get("assessment"))
    implicated = _mapping_or_empty(state.get("implicated"))
    payload = {
        "workflow_id": _safe_str(subject_episode.get("workflow_id")),
        "verdict": _safe_str(critic.get("verdict")),
        "maintenance_follow_up_reason": _safe_str(
            assessment.get("maintenance_follow_up_reason")
        ),
        "root_causes": sorted(_cause_tokens(state)),
        "implicated_workflow_ids": sorted(
            _normalise_strings(implicated.get("workflow_ids"), limit=10)
        ),
        "implicated_tool_names": sorted(
            _normalise_strings(implicated.get("tool_names"), limit=10)
        ),
    }
    return _hash_payload(payload)


def _count_prior_matches(
    *,
    namespace: str | None,
    routing_fingerprint: str,
    memory_id: str,
) -> int:
    coll = get_episode_critique_memories_collection()
    if coll is None or not namespace or not routing_fingerprint:
        return 0
    query = {
        "namespace": namespace,
        "routing_fingerprint": routing_fingerprint,
        "memory_id": {"$ne": memory_id},
    }
    try:
        return int(coll.count_documents(query, limit=200))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[episode_critique_routing] could not count prior matches for %s: %s",
            routing_fingerprint,
            exc,
        )
        return 0


def _existing_reference_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    external_references = task.get("external_references")
    if not isinstance(external_references, Mapping):
        return {}
    payload = external_references.get(EPISODE_CRITIQUE_REMEDIATION_SOURCE_SYSTEM)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _workflow_label(state: Mapping[str, Any]) -> str:
    subject_episode = _mapping_or_empty(state.get("subject_episode"))
    identity = _mapping_or_empty(subject_episode.get("workflow_definition_identity"))
    return (
        _safe_str(identity.get("workflow_name"))
        or _safe_str(identity.get("name"))
        or _safe_str(subject_episode.get("workflow_id"))
        or "workflow episode"
    )


def _cause_fragment(state: Mapping[str, Any]) -> str:
    causes = _cause_tokens(state)
    if causes:
        return ", ".join(causes[:2])
    critic = _mapping_or_empty(state.get("critic"))
    assessment = _mapping_or_empty(critic.get("assessment"))
    summary = _safe_str(assessment.get("summary")) or _safe_str(state.get("description"))
    if not summary:
        return "critic finding"
    return summary[:80]


def _priority_from_severity(rank: int) -> str:
    if rank >= 4:
        return "critical"
    if rank >= 3:
        return "high"
    if rank >= 2:
        return "medium"
    return "low"


def _build_task_title(state: Mapping[str, Any]) -> str:
    title = f"Episode critique remediation: {_workflow_label(state)} - {_cause_fragment(state)}"
    return title[:160]


def _build_task_description(
    *,
    state: Mapping[str, Any],
    routing_fingerprint: str,
    repeat_count: int,
    decision: str,
) -> str:
    subject_episode = _mapping_or_empty(state.get("subject_episode"))
    critic = _mapping_or_empty(state.get("critic"))
    assessment = _mapping_or_empty(critic.get("assessment"))
    evidence_receipts = _mapping_or_empty(state.get("evidence_receipts"))
    recommendations = _normalise_strings(state.get("recommendations"), limit=6)
    lines = [
        "Generated by Codex on behalf of Michael Witbrock.",
        "",
        f"Routing fingerprint: {routing_fingerprint}",
        f"Critique memory: {_safe_str(state.get('memory_id')) or 'unknown'}",
        f"Request ID: {_safe_str(state.get('request_id')) or 'unknown'}",
        f"Workflow: {_safe_str(subject_episode.get('workflow_id')) or 'unknown'}",
        f"Routing decision: {decision}",
        f"Repeat count: {repeat_count}",
        f"Verdict: {_safe_str(critic.get('verdict')) or 'unknown'}",
        f"Confidence: {_safe_float(critic.get('confidence'))}",
        f"Summary: {_safe_str(assessment.get('summary')) or _safe_str(state.get('description')) or 'none'}",
        f"Receipt hash: {_safe_str(evidence_receipts.get('receipt_hash')) or 'none'}",
        "",
        "Recommendations:",
    ]
    if recommendations:
        lines.extend(f"- {item}" for item in recommendations)
    else:
        lines.append("- No explicit recommendations were recorded.")
    return "\n".join(lines)


def _build_jira_summary(state: Mapping[str, Any]) -> str:
    return f"Episode critique remediation: {_cause_fragment(state)} ({_workflow_label(state)})"[:140]


def _build_jira_description(
    *,
    state: Mapping[str, Any],
    routing_fingerprint: str,
    repeat_count: int,
    task_concept_id: str | None,
) -> str:
    subject_episode = _mapping_or_empty(state.get("subject_episode"))
    critic = _mapping_or_empty(state.get("critic"))
    assessment = _mapping_or_empty(critic.get("assessment"))
    lines = [
        "Generated by Codex on behalf of Michael Witbrock.",
        "",
        f"Deduplication fingerprint: {routing_fingerprint}",
        f"Critique memory: {_safe_str(state.get('memory_id')) or 'unknown'}",
        f"Linked Von task: {task_concept_id or 'none'}",
        f"Workflow: {_safe_str(subject_episode.get('workflow_id')) or 'unknown'}",
        f"Repeat count: {repeat_count}",
        f"Verdict: {_safe_str(critic.get('verdict')) or 'unknown'}",
        f"Confidence: {_safe_float(critic.get('confidence'))}",
        "",
        "Critique summary:",
        _safe_str(assessment.get("summary"))
        or _safe_str(state.get("description"))
        or "No summary recorded.",
    ]
    return "\n".join(lines)


def _build_existing_issue_comment(
    *,
    state: Mapping[str, Any],
    routing_fingerprint: str,
    repeat_count: int,
) -> str:
    summary = (
        _safe_str(_mapping_or_empty(_mapping_or_empty(state.get("critic")).get("assessment")).get("summary"))
        or _safe_str(state.get("description"))
        or "No summary recorded."
    )
    return (
        "Generated by Codex on behalf of Michael Witbrock.\n\n"
        "Episode critique rerun matched existing remediation fingerprint "
        f"`{routing_fingerprint}`.\n\n"
        f"Repeat count is now {repeat_count}.\n\n"
        f"Latest critique summary:\n{summary[:1200]}"
    )


def _build_task_reference_payload(
    *,
    state: Mapping[str, Any],
    existing_payload: Mapping[str, Any] | None,
    routing_fingerprint: str,
    repeat_count: int,
    decision: str,
    task_concept_id: str,
    jira_issue_key: str | None,
) -> dict[str, Any]:
    memory_id = _safe_str(state.get("memory_id"))
    critic = _mapping_or_empty(state.get("critic"))
    assessment = _mapping_or_empty(critic.get("assessment"))
    return {
        "routing_fingerprint": routing_fingerprint,
        "memory_ids": _merge_string_lists(
            _mapping_or_empty(existing_payload).get("memory_ids"),
            [memory_id] if memory_id else [],
            limit=20,
        ),
        "task_concept_id": task_concept_id,
        "workflow_id": _safe_str(_mapping_or_empty(state.get("subject_episode")).get("workflow_id")),
        "decision": decision,
        "repeat_count": repeat_count,
        "verdict": _safe_str(critic.get("verdict")),
        "confidence": _safe_float(critic.get("confidence")),
        "summary": _safe_str(assessment.get("summary")) or _safe_str(state.get("description")),
        "jira_issue_keys": _merge_string_lists(
            _mapping_or_empty(existing_payload).get("jira_issue_keys"),
            [jira_issue_key] if jira_issue_key else [],
            limit=10,
        ),
        "last_routed_at_utc": _utcnow_iso(),
    }


def _determine_decision(
    *,
    state: Mapping[str, Any],
    repeat_count: int,
    existing_task: Mapping[str, Any] | None,
    existing_issue_keys: Sequence[str],
) -> tuple[str, list[str], bool, bool]:
    critic = _mapping_or_empty(state.get("critic"))
    verdict = (_safe_str(critic.get("verdict")) or "").lower()
    confidence = _safe_float(critic.get("confidence")) or 0.0
    severity_rank = _severity_rank(state)
    reason_codes: list[str] = []

    if verdict == "pass":
        return ROUTING_DECISION_MEMORY_ONLY, ["verdict_pass"], False, False

    has_existing_task = isinstance(existing_task, Mapping)
    has_existing_issue = bool(existing_issue_keys)
    if has_existing_task:
        reason_codes.append("existing_task_reused")
    if has_existing_issue:
        reason_codes.append("existing_jira_reused")

    repeated = repeat_count >= _TASK_REPEAT_THRESHOLD
    if repeated:
        reason_codes.append("repeat_threshold_met")
    else:
        reason_codes.append("repeat_threshold_not_met")

    if severity_rank >= 3:
        reason_codes.append("high_severity_signal")
    elif severity_rank >= 2:
        reason_codes.append("medium_severity_signal")
    else:
        reason_codes.append("low_severity_signal")

    if confidence < _TASK_CONFIDENCE_THRESHOLD and not has_existing_task and not has_existing_issue:
        return ROUTING_DECISION_MEMORY_ONLY, reason_codes + ["confidence_below_task_threshold"], False, False

    should_task = has_existing_task or has_existing_issue or repeated or severity_rank >= 3
    if not should_task:
        return ROUTING_DECISION_MEMORY_ONLY, reason_codes + ["novel_signal_retained_in_memory"], False, False

    should_jira = has_existing_issue or (
        confidence >= _JIRA_CONFIDENCE_THRESHOLD
        and (
            repeat_count >= _JIRA_REPEAT_THRESHOLD
            or (severity_rank >= 3 and repeat_count >= _TASK_REPEAT_THRESHOLD)
        )
    )
    if should_jira:
        return ROUTING_DECISION_TASK_AND_JIRA, reason_codes + ["jira_escalation_threshold_met"], True, True
    return ROUTING_DECISION_TASK_ONLY, reason_codes + ["task_routing_threshold_met"], True, False


def route_episode_critique_memory(
    *,
    memory_id: str | None = None,
    memory_state: Mapping[str, Any] | None = None,
    actor_concept_id: str | None = None,
) -> dict[str, Any]:
    state = (
        dict(memory_state)
        if isinstance(memory_state, Mapping)
        else get_episode_critique_memory_state(str(memory_id or ""))
    )
    if not isinstance(state, Mapping):
        return {"success": False, "reason": "memory_state_unavailable"}

    resolved_memory_id = _safe_str(state.get("memory_id")) or _safe_str(memory_id)
    if not resolved_memory_id:
        return {"success": False, "reason": "missing_memory_id"}
    state_dict = dict(state)

    try:
        routing_fingerprint = build_episode_critique_routing_fingerprint(state_dict)
        namespace = _safe_str(state_dict.get("namespace"))
        org_id = _safe_str(state_dict.get("org_id"))
        user_id = _safe_str(state_dict.get("user_id")) or _safe_str(actor_concept_id)
        prior_repeat_count = _count_prior_matches(
            namespace=namespace,
            routing_fingerprint=routing_fingerprint,
            memory_id=resolved_memory_id,
        )
        repeat_count = prior_repeat_count + 1

        existing_task = find_task_by_external_reference(
            source_system=EPISODE_CRITIQUE_REMEDIATION_SOURCE_SYSTEM,
            external_id=routing_fingerprint,
            organisation_concept_id=org_id,
        )
        existing_reference_payload = (
            _existing_reference_payload(existing_task)
            if isinstance(existing_task, Mapping)
            else {}
        )
        remediation = _mapping_or_empty(state_dict.get("remediation"))
        existing_issue_keys = _normalise_strings(
            _merge_string_lists(
                remediation.get("jira_issue_keys"),
                existing_reference_payload.get("jira_issue_keys"),
            ),
            limit=10,
        )

        decision, reason_codes, should_task, should_jira = _determine_decision(
            state=state_dict,
            repeat_count=repeat_count,
            existing_task=existing_task,
            existing_issue_keys=existing_issue_keys,
        )

        task_action = "not_needed"
        jira_action = "not_needed"
        task_concept_id: str | None = None
        jira_issue_key: str | None = existing_issue_keys[0] if existing_issue_keys else None
        task_reference_payload: dict[str, Any] = {}

        if should_task:
            existing_payload = existing_reference_payload
            if isinstance(existing_task, Mapping):
                task_concept_id = _safe_str(existing_task.get("task_concept_id"))
                task_action = "reused_existing" if task_concept_id else "not_needed"
            else:
                task = create_task(
                    title=_build_task_title(state_dict),
                    description=_build_task_description(
                        state=state_dict,
                        routing_fingerprint=routing_fingerprint,
                        repeat_count=repeat_count,
                        decision=decision,
                    ),
                    assignee_concept_id=user_id,
                    created_by_concept_id=user_id,
                    originating_session_id=_safe_str(state_dict.get("session_id")),
                    priority=_priority_from_severity(_severity_rank(state_dict)),
                    organisation_concept_id=org_id,
                )
                task_concept_id = _safe_str(task.get("task_concept_id"))
                task_action = "created_new" if task_concept_id else "not_needed"
            if task_concept_id:
                task_reference_payload = _build_task_reference_payload(
                    state=state_dict,
                    existing_payload=existing_payload,
                    routing_fingerprint=routing_fingerprint,
                    repeat_count=repeat_count,
                    decision=decision,
                    task_concept_id=task_concept_id,
                    jira_issue_key=jira_issue_key,
                )
                upsert_task_external_reference(
                    task_concept_id,
                    source_system=EPISODE_CRITIQUE_REMEDIATION_SOURCE_SYSTEM,
                    external_id=routing_fingerprint,
                    actor_concept_id=user_id,
                    reference_payload=task_reference_payload,
                )

        if should_jira:
            jira_result = upsert_deduplicated_jira_issue(
                project_key="JVNAUTOSCI",
                issue_type="Task",
                summary=_build_jira_summary(state_dict),
                description=_build_jira_description(
                    state=state_dict,
                    routing_fingerprint=routing_fingerprint,
                    repeat_count=repeat_count,
                    task_concept_id=task_concept_id,
                ),
                fingerprint=routing_fingerprint,
                search_label=EPISODE_CRITIQUE_REMEDIATION_SEARCH_LABEL,
                labels=[
                    EPISODE_CRITIQUE_REMEDIATION_SEARCH_LABEL,
                    "actor-critic",
                    "self-improvement",
                ],
                request_id=_safe_str(state_dict.get("request_id")) or resolved_memory_id,
                existing_comment=_build_existing_issue_comment(
                    state=state_dict,
                    routing_fingerprint=routing_fingerprint,
                    repeat_count=repeat_count,
                ),
            )
            if not bool(jira_result.get("success")):
                raise RuntimeError(
                    jira_result.get("error") or jira_result.get("error_code") or "jira_upsert_failed"
                )
            jira_issue_key = _safe_str(jira_result.get("issue_key"))
            jira_action = _safe_str(jira_result.get("mode")) or "updated_existing"
            if task_concept_id:
                task_reference_payload = _build_task_reference_payload(
                    state=state_dict,
                    existing_payload=task_reference_payload,
                    routing_fingerprint=routing_fingerprint,
                    repeat_count=repeat_count,
                    decision=decision,
                    task_concept_id=task_concept_id,
                    jira_issue_key=jira_issue_key,
                )
                upsert_task_external_reference(
                    task_concept_id,
                    source_system=EPISODE_CRITIQUE_REMEDIATION_SOURCE_SYSTEM,
                    external_id=routing_fingerprint,
                    actor_concept_id=user_id,
                    reference_payload=task_reference_payload,
                )

        routing = {
            "schema_version": "episode_critique_routing.v1",
            "decision": decision,
            "reason_codes": reason_codes,
            "fingerprint": routing_fingerprint,
            "repeat_count": repeat_count,
            "task_action": task_action,
            "jira_action": jira_action,
            "task_concept_id": task_concept_id,
            "jira_issue_key": jira_issue_key,
            "updated_at_utc": _utcnow_iso(),
        }
        record_result = record_episode_critique_memory_routing(
            memory_id=resolved_memory_id,
            routing=routing,
            remediation_task_ids=[task_concept_id] if task_concept_id else [],
            remediation_issue_keys=[jira_issue_key] if jira_issue_key else [],
        )
        updated_state = _mapping_or_empty(record_result.get("state"))
        if not updated_state:
            updated_state = dict(state_dict)
        return {
            "success": True,
            "memory_id": resolved_memory_id,
            "decision": decision,
            "reason_codes": reason_codes,
            "routing_fingerprint": routing_fingerprint,
            "repeat_count": repeat_count,
            "remediation_task_id": task_concept_id,
            "remediation_issue_key": jira_issue_key,
            "remediation_task_ids": _normalise_strings(
                _mapping_or_empty(updated_state.get("remediation")).get("task_ids"),
                limit=40,
            ),
            "remediation_issue_keys": _normalise_strings(
                _mapping_or_empty(updated_state.get("remediation")).get("jira_issue_keys"),
                limit=40,
            ),
            "task_action": task_action,
            "jira_action": jira_action,
            "state": updated_state,
        }
    except Exception as exc:
        logger.warning(
            "[episode_critique_routing] routing failed for %s: %s",
            resolved_memory_id,
            exc,
        )
        routing = {
            "schema_version": "episode_critique_routing.v1",
            "decision": ROUTING_DECISION_ROUTING_FAILED,
            "reason_codes": ["routing_failed"],
            "fingerprint": build_episode_critique_routing_fingerprint(state_dict),
            "repeat_count": 0,
            "task_action": "not_needed",
            "jira_action": "not_needed",
            "error": str(exc),
            "updated_at_utc": _utcnow_iso(),
        }
        record_episode_critique_memory_routing(
            memory_id=resolved_memory_id,
            routing=routing,
        )
        return {
            "success": False,
            "memory_id": resolved_memory_id,
            "decision": ROUTING_DECISION_ROUTING_FAILED,
            "reason": "routing_failed",
            "error": str(exc),
            "routing_fingerprint": routing.get("fingerprint"),
            "reason_codes": ["routing_failed"],
            "repeat_count": 0,
            "remediation_task_ids": _normalise_strings(
                _mapping_or_empty(state_dict.get("remediation")).get("task_ids"),
                limit=40,
            ),
            "remediation_issue_keys": _normalise_strings(
                _mapping_or_empty(state_dict.get("remediation")).get("jira_issue_keys"),
                limit=40,
            ),
        }


__all__ = [
    "EPISODE_CRITIQUE_REMEDIATION_SOURCE_SYSTEM",
    "ROUTING_DECISION_MEMORY_ONLY",
    "ROUTING_DECISION_TASK_AND_JIRA",
    "ROUTING_DECISION_TASK_ONLY",
    "build_episode_critique_routing_fingerprint",
    "route_episode_critique_memory",
]
