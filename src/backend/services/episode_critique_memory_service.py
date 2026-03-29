"""Persist episode-critic judgements as first-class memory artefacts."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure, PyMongoError

from ..db.mongo_client import get_db
from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id_exact
from .text_value_service import upsert_singleton_text_relation
from .workflow_episode_service import get_latest_workflow_use_episode

logger = logging.getLogger(__name__)

EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION = "episode_critique_memory.v1"
EPISODE_CRITIQUE_MEMORIES_COLLECTION = "episode_critique_memories"
EPISODE_CRITIQUE_MEMORY_TYPE_ID = "#V#episode_critique_memory"
EPISODE_CRITIQUE_MEMORY_PARENT_TYPE_ID = "#V#artifact"
EPISODE_CRITIQUE_MEMORY_SERVICE_SOURCE = "episode_critique_memory_service"

_INDEXES_READY = False

_CONCEPT_ID_PATTERN = re.compile(r"#V#[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_JIRA_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clone_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return _clone_mapping(value)


def _normalise_strings(values: Any, *, limit: int = 200) -> list[str]:
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


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def _payload_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
    except Exception:
        try:
            return str(value)
        except Exception:
            return ""


def _hash_payload(value: Any) -> str:
    text = _payload_text(value)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_memory_id(*, request_id: str, episode_id: str | None) -> str:
    seed = episode_id or request_id
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"#V#episode_critique_memory_{digest}"


def _get_concept_or_none(concept_id: str) -> dict[str, Any] | None:
    try:
        concept = get_concept_by_concept_id_exact(concept_id)
    except ConceptNotFoundError:
        return None
    return dict(concept) if isinstance(concept, Mapping) else None


def _ensure_episode_critique_memory_type() -> tuple[dict[str, Any], bool]:
    existing = _get_concept_or_none(EPISODE_CRITIQUE_MEMORY_TYPE_ID)
    if isinstance(existing, Mapping):
        return dict(existing), False

    created = concept_service.create_concept(
        name="Episode Critique Memory",
        concept_id=EPISODE_CRITIQUE_MEMORY_TYPE_ID,
        description=(
            "Type for episodic-memory artefacts that persist critic judgements over "
            "completed workflow or conversation-turn episodes."
        ),
        parent_concept_ids=[EPISODE_CRITIQUE_MEMORY_PARENT_TYPE_ID],
        create_as_instance=False,
    )
    upsert_singleton_text_relation(
        subject_concept_id=EPISODE_CRITIQUE_MEMORY_TYPE_ID,
        predicate="hasDescription",
        text=(
            "Type for episodic-memory artefacts that persist critic verdicts, "
            "confidence, evidence receipts, implicated workflows/tools/concepts, "
            "and remediation links for completed episodes."
        ),
        lang="en-NZ",
        context={
            "source": EPISODE_CRITIQUE_MEMORY_SERVICE_SOURCE,
            "reason": "ensure_episode_critique_memory_type",
        },
        garbage_collect=True,
    )
    return created, True


def _ensure_episode_critique_memory_concept(
    *,
    memory_id: str,
    name: str,
    description: str,
    user_id: str | None,
    org_id: str | None,
    namespace: str | None,
) -> tuple[dict[str, Any], bool]:
    existing = _get_concept_or_none(memory_id)
    if isinstance(existing, Mapping):
        return dict(existing), False

    created = concept_service.create_concept(
        name=name,
        concept_id=memory_id,
        description=description,
        parent_concept_ids=[EPISODE_CRITIQUE_MEMORY_TYPE_ID],
        create_as_instance=True,
        created_by_concept_id=user_id,
        organisation_concept_id=org_id,
        event_namespace=namespace,
    )
    upsert_singleton_text_relation(
        subject_concept_id=memory_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        context={
            "source": EPISODE_CRITIQUE_MEMORY_SERVICE_SOURCE,
            "reason": "create_episode_critique_memory",
        },
        garbage_collect=True,
    )
    return created, True


def _extract_concept_ids_and_issue_keys(*values: Any) -> tuple[list[str], list[str]]:
    concept_ids: list[str] = []
    issue_keys: list[str] = []
    seen_concepts: set[str] = set()
    seen_issues: set[str] = set()

    stack: list[Any] = list(values)
    visited_nodes = 0
    max_nodes = 3000

    while stack and visited_nodes < max_nodes:
        current = stack.pop()
        visited_nodes += 1

        if isinstance(current, Mapping):
            for value in current.values():
                stack.append(value)
            continue

        if isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            for value in current:
                stack.append(value)
            continue

        if not isinstance(current, str):
            continue

        for concept_id in _CONCEPT_ID_PATTERN.findall(current):
            lowered = concept_id.lower()
            if lowered in seen_concepts:
                continue
            seen_concepts.add(lowered)
            concept_ids.append(concept_id)

        for match in _JIRA_ISSUE_KEY_PATTERN.findall(current.upper()):
            if match in seen_issues:
                continue
            seen_issues.add(match)
            issue_keys.append(match)

    return concept_ids, issue_keys


def _extract_task_ids(*values: Any) -> list[str]:
    task_ids: list[str] = []
    seen: set[str] = set()

    stack: list[Any] = list(values)
    visited_nodes = 0
    max_nodes = 2000

    while stack and visited_nodes < max_nodes:
        current = stack.pop()
        visited_nodes += 1

        if isinstance(current, Mapping):
            for key, value in current.items():
                key_text = str(key).strip().lower() if isinstance(key, str) else ""
                if key_text in {"task_concept_id", "task_id"}:
                    task_value = _safe_str(value)
                    if task_value:
                        lowered = task_value.lower()
                        if lowered not in seen:
                            seen.add(lowered)
                            task_ids.append(task_value)
                stack.append(value)
            continue

        if isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            for value in current:
                stack.append(value)

    return task_ids


def _extract_implicated_tool_names(
    *,
    record: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any] | None,
) -> list[str]:
    tool_names: list[str] = []
    seen: set[str] = set()

    execution = _mapping_or_empty(record.get("execution"))
    for item in execution.get("tool_invocations") or []:
        if not isinstance(item, Mapping):
            continue
        tool = _safe_str(item.get("tool"))
        if not tool:
            continue
        lowered = tool.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        tool_names.append(tool)

    if isinstance(llm_debug_data, Mapping):
        for item in llm_debug_data.get("tool_invocations") or []:
            if not isinstance(item, Mapping):
                continue
            tool = _safe_str(item.get("tool")) or _safe_str(item.get("method"))
            if not tool:
                continue
            lowered = tool.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            tool_names.append(tool)

    return tool_names[:60]


def _extract_implicated_workflow_ids(
    *,
    record: Mapping[str, Any],
    episode: Mapping[str, Any] | None,
) -> list[str]:
    workflow_ids: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        workflow_id = _safe_str(value)
        if not workflow_id:
            return
        lowered = workflow_id.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        workflow_ids.append(workflow_id)

    workflow_selection = _mapping_or_empty(record.get("workflow_selection"))
    _add(workflow_selection.get("selected_workflow_id"))

    routing = _mapping_or_empty(record.get("workflow_routing_diagnostics"))
    dispatch = _mapping_or_empty(routing.get("dispatch"))
    _add(dispatch.get("dispatch_workflow_id"))
    _add(routing.get("selected_workflow_id"))

    if isinstance(episode, Mapping):
        _add(episode.get("workflow_id"))

    return workflow_ids[:20]


def _extract_implicated_concept_ids(
    *,
    record: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any] | None,
) -> list[str]:
    values: list[Any] = [record]
    if isinstance(llm_debug_data, Mapping):
        values.append(llm_debug_data.get("search_evidence"))
        values.append(llm_debug_data.get("tool_invocations"))

    concept_ids, _ = _extract_concept_ids_and_issue_keys(*values)
    return concept_ids[:120]


def _extract_remediation_links(
    *,
    record: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any] | None,
) -> tuple[list[str], list[str]]:
    values: list[Any] = []
    if isinstance(llm_debug_data, Mapping):
        values.append(llm_debug_data.get("tool_invocations"))
        values.append(llm_debug_data.get("response"))
    execution = _mapping_or_empty(record.get("execution"))
    values.append(execution.get("tool_invocations"))

    task_ids = _extract_task_ids(*values)
    _, issue_keys = _extract_concept_ids_and_issue_keys(*values)
    return task_ids[:40], issue_keys[:40]


def _derive_verdict_and_confidence(
    *,
    critic_summary: Mapping[str, Any],
    completion_gate: Mapping[str, Any],
) -> tuple[str, float | None, int]:
    verified_count = max(0, _safe_int(critic_summary.get("verified_count")))
    not_verified_count = max(0, _safe_int(critic_summary.get("not_verified_count")))
    inconclusive_count = max(0, _safe_int(critic_summary.get("inconclusive_count")))
    error_count = max(0, _safe_int(critic_summary.get("error_count")))
    total_checks = verified_count + not_verified_count + inconclusive_count + error_count
    unresolved_check_count = not_verified_count + inconclusive_count + error_count

    requires_follow_up = bool(completion_gate.get("requires_follow_up"))
    safe_to_claim_completion = bool(completion_gate.get("safe_to_claim_completion"))
    decision = (_safe_str(completion_gate.get("decision")) or "").lower()

    if requires_follow_up:
        verdict = "follow_up_required"
    elif total_checks > 0 and unresolved_check_count == 0 and safe_to_claim_completion:
        verdict = "pass"
    elif total_checks > 0 and verified_count == 0 and unresolved_check_count > 0:
        verdict = "fail"
    elif safe_to_claim_completion and decision == "completed" and total_checks == 0:
        verdict = "pass"
    else:
        verdict = "inconclusive"

    confidence: float | None
    if total_checks > 0:
        confidence = round(max(0.0, min(1.0, verified_count / float(total_checks))), 3)
        if requires_follow_up:
            confidence = round(min(confidence, 0.49), 3)
    elif verdict == "pass":
        confidence = 0.25
    else:
        confidence = None

    return verdict, confidence, unresolved_check_count


def _build_recommendations(
    *,
    verdict: str,
    critic_summary: Mapping[str, Any],
    completion_gate: Mapping[str, Any],
    implicated_tool_names: Sequence[str],
) -> list[str]:
    recommendations: list[str] = []

    if bool(completion_gate.get("requires_follow_up")):
        recommendations.append(
            "Resolve the blocked or incomplete required effects before claiming completion."
        )

    if _safe_int(critic_summary.get("error_count")) > 0:
        recommendations.append(
            "Inspect the retained tool and critic receipts for runtime or verification errors."
        )

    if _safe_int(critic_summary.get("inconclusive_count")) > 0:
        recommendations.append(
            "Strengthen verification for the unresolved effects so future critiques are less ambiguous."
        )

    if _safe_int(critic_summary.get("not_verified_count")) > 0:
        recommendations.append(
            "Re-run or add the verification reads needed to confirm the intended mutation actually landed."
        )

    blocking_failure_codes = completion_gate.get("blocking_failure_codes")
    if isinstance(blocking_failure_codes, list) and blocking_failure_codes:
        recommendations.append(
            "Review the blocking failure codes recorded by the completion gate."
        )

    if verdict == "pass" and not recommendations:
        recommendations.append(
            "No remediation is currently indicated by the recorded critic evidence."
        )

    if implicated_tool_names and verdict != "pass":
        recommendations.append(
            f"Check the implicated tools first: {', '.join(implicated_tool_names[:6])}."
        )

    deduped: list[str] = []
    seen: set[str] = set()
    for item in recommendations:
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(item)
    return deduped[:8]


def _build_evidence_receipts(
    *,
    record: Mapping[str, Any],
    episode: Mapping[str, Any] | None,
    llm_debug_data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    execution = _mapping_or_empty(record.get("execution"))
    search_evidence = execution.get("search_evidence")
    if not isinstance(search_evidence, list) and isinstance(llm_debug_data, Mapping):
        search_evidence = llm_debug_data.get("search_evidence")

    search_receipts: list[dict[str, Any]] = []
    if isinstance(search_evidence, list):
        for item in search_evidence:
            if not isinstance(item, Mapping):
                continue
            search_receipts.append(
                {
                    "tool": _safe_str(item.get("tool")),
                    "call_id": _safe_str(item.get("call_id")),
                    "query": _safe_str(item.get("query")),
                    "status": _safe_str(item.get("status")),
                    "arguments_sha256": _safe_str(item.get("arguments_sha256")),
                    "result_sha256": _safe_str(item.get("result_sha256")),
                    "result_truncated": bool(item.get("result_truncated")),
                }
            )
            if len(search_receipts) >= 20:
                break

    tool_receipts: list[dict[str, Any]] = []
    for item in execution.get("tool_invocations") or []:
        if not isinstance(item, Mapping):
            continue
        tool_receipts.append(
            {
                "tool": _safe_str(item.get("tool")),
                "status": _safe_str(item.get("status")),
                "payload_fingerprint": _safe_str(item.get("payload_fingerprint")),
                "result_summary": _safe_str(item.get("result_summary")),
            }
        )
        if len(tool_receipts) >= 40:
            break

    receipt = {
        "turn_execution_record": {
            "request_id": _safe_str(record.get("request_id")),
            "sha256": _hash_payload(record),
        },
        "workflow_episode": {
            "episode_id": _safe_str(episode.get("episode_id"))
            if isinstance(episode, Mapping)
            else None,
            "stable_key": _safe_str(episode.get("stable_key"))
            if isinstance(episode, Mapping)
            else None,
            "workflow_id": _safe_str(episode.get("workflow_id"))
            if isinstance(episode, Mapping)
            else None,
        },
        "search_tools": search_receipts,
        "tool_invocations": tool_receipts,
    }
    receipt["receipt_hash"] = _hash_payload(receipt)
    return receipt


def _build_description(
    *,
    request_id: str,
    episode_id: str | None,
    verdict: str,
    unresolved_check_count: int,
    workflow_id: str | None,
) -> str:
    subject = episode_id or request_id
    workflow_suffix = f" for {workflow_id}" if workflow_id else ""
    return (
        f"Episode critique memory{workflow_suffix} over {subject}: verdict "
        f"{verdict.replace('_', ' ')}, unresolved checks {int(unresolved_check_count)}."
    )


def build_episode_critique_memory_state_from_turn(
    *,
    record: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    namespace: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None

    request_id = _safe_str(record.get("request_id"))
    if not request_id:
        return None

    resolved_user_id = _safe_str(user_id) or _safe_str(record.get("user_id"))
    resolved_session_id = _safe_str(session_id) or _safe_str(record.get("session_id"))
    resolved_namespace = _safe_str(namespace) or _safe_str(record.get("namespace"))
    resolved_org_id = _safe_str(org_id) or _safe_str(record.get("org_id"))

    workflow_selection = _mapping_or_empty(record.get("workflow_selection"))
    selected_workflow_id = _safe_str(workflow_selection.get("selected_workflow_id"))

    episode = None
    if selected_workflow_id:
        try:
            episode = get_latest_workflow_use_episode(
                workflow_id=selected_workflow_id,
                namespace=resolved_namespace,
                session_id=resolved_session_id,
                turn_id=request_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[episode_critique_memory] could not resolve workflow episode for %s: %s",
                request_id,
                exc,
            )
            episode = None

    episode_payload = dict(episode) if isinstance(episode, Mapping) else {}
    episode_id = _safe_str(episode_payload.get("episode_id"))
    workflow_id = (
        selected_workflow_id
        or _safe_str(episode_payload.get("workflow_id"))
        or _safe_str(record.get("workflow_id"))
    )
    memory_id = _build_memory_id(request_id=request_id, episode_id=episode_id)

    critic = _mapping_or_empty(record.get("critic"))
    critic_summary = _mapping_or_empty(critic.get("summary"))
    completion_gate = _mapping_or_empty(record.get("completion_gate"))
    verdict, confidence, unresolved_check_count = _derive_verdict_and_confidence(
        critic_summary=critic_summary,
        completion_gate=completion_gate,
    )

    implicated_tool_names = _extract_implicated_tool_names(
        record=record,
        llm_debug_data=llm_debug_data,
    )
    implicated_workflow_ids = _extract_implicated_workflow_ids(
        record=record,
        episode=episode_payload if episode_payload else None,
    )
    implicated_concept_ids = _extract_implicated_concept_ids(
        record=record,
        llm_debug_data=llm_debug_data,
    )
    remediation_task_ids, remediation_issue_keys = _extract_remediation_links(
        record=record,
        llm_debug_data=llm_debug_data,
    )

    description = _build_description(
        request_id=request_id,
        episode_id=episode_id,
        verdict=verdict,
        unresolved_check_count=unresolved_check_count,
        workflow_id=workflow_id,
    )
    now_iso = _utcnow_iso()
    created_at_utc = _safe_str(record.get("created_at_utc")) or now_iso

    state: dict[str, Any] = {
        "schema_version": EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION,
        "memory_id": memory_id,
        "request_id": request_id,
        "created_at_utc": created_at_utc,
        "updated_at_utc": now_iso,
        "namespace": resolved_namespace,
        "session_id": resolved_session_id,
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "subject_episode": {
            "subject_kind": "workflow_use_episode"
            if episode_id
            else "turn_execution_request",
            "episode_id": episode_id,
            "workflow_id": workflow_id,
            "instance_id": _safe_str(episode_payload.get("instance_id")),
            "stable_key": _safe_str(episode_payload.get("stable_key")),
            "source": _safe_str(episode_payload.get("source")),
            "turn_id": request_id,
            "session_id": resolved_session_id,
            "namespace": resolved_namespace,
            "workflow_definition_identity": _mapping_or_empty(
                episode_payload.get("workflow_definition_identity")
            ),
        },
        "critic": {
            "workflow_id": _safe_str(critic.get("workflow_id")),
            "summary": critic_summary,
            "verdict": verdict,
            "confidence": confidence,
            "unresolved_check_count": unresolved_check_count,
        },
        "completion_gate": completion_gate,
        "implicated": {
            "workflow_ids": implicated_workflow_ids,
            "tool_names": implicated_tool_names,
            "concept_ids": implicated_concept_ids,
        },
        "remediation": {
            "task_ids": remediation_task_ids,
            "jira_issue_keys": remediation_issue_keys,
        },
        "recommendations": _build_recommendations(
            verdict=verdict,
            critic_summary=critic_summary,
            completion_gate=completion_gate,
            implicated_tool_names=implicated_tool_names,
        ),
        "evidence_receipts": _build_evidence_receipts(
            record=record,
            episode=episode_payload if episode_payload else None,
            llm_debug_data=llm_debug_data,
        ),
        "dedupe_fingerprint": _hash_payload(
            {
                "request_id": request_id,
                "episode_id": episode_id,
                "workflow_id": workflow_id,
                "critic_summary": critic_summary,
                "completion_gate": completion_gate,
            }
        ),
        "description": description,
    }
    return state


def _persist_episode_critique_memory_state(
    *,
    memory_id: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    concept_service.update_concept(
        memory_id,
        {
            "concept_data.episode_critique_memory": dict(state),
            "attributes.episode_critique_memory.request_id": state.get("request_id"),
            "attributes.episode_critique_memory.verdict": _mapping_or_empty(
                state.get("critic")
            ).get("verdict"),
            "attributes.episode_critique_memory.updated_at_utc": state.get(
                "updated_at_utc"
            ),
            "attributes.episode_critique_memory.workflow_id": _mapping_or_empty(
                state.get("subject_episode")
            ).get("workflow_id"),
            "attributes.episode_critique_memory.episode_id": _mapping_or_empty(
                state.get("subject_episode")
            ).get("episode_id"),
        },
    )
    return dict(state)


def _build_episode_critique_memory_projection(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    critic = _mapping_or_empty(state.get("critic"))
    subject_episode = _mapping_or_empty(state.get("subject_episode"))
    implicated = _mapping_or_empty(state.get("implicated"))
    remediation = _mapping_or_empty(state.get("remediation"))
    evidence_receipts = _mapping_or_empty(state.get("evidence_receipts"))

    return {
        "schema_version": EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION,
        "memory_id": _safe_str(state.get("memory_id")),
        "request_id": _safe_str(state.get("request_id")),
        "episode_id": _safe_str(subject_episode.get("episode_id")),
        "workflow_id": _safe_str(subject_episode.get("workflow_id")),
        "session_id": _safe_str(state.get("session_id")),
        "namespace": _safe_str(state.get("namespace")),
        "user_id": _safe_str(state.get("user_id")),
        "org_id": _safe_str(state.get("org_id")),
        "created_at_utc": _safe_str(state.get("created_at_utc")),
        "updated_at_utc": _safe_str(state.get("updated_at_utc")),
        "verdict": _safe_str(critic.get("verdict")),
        "confidence": _safe_float(critic.get("confidence")),
        "unresolved_check_count": _safe_int(critic.get("unresolved_check_count")),
        "implicated_workflow_ids": _normalise_strings(
            implicated.get("workflow_ids"),
            limit=30,
        ),
        "implicated_tool_names": _normalise_strings(
            implicated.get("tool_names"),
            limit=60,
        ),
        "implicated_concept_ids": _normalise_strings(
            implicated.get("concept_ids"),
            limit=120,
        ),
        "remediation_task_ids": _normalise_strings(
            remediation.get("task_ids"),
            limit=40,
        ),
        "remediation_issue_keys": _normalise_strings(
            remediation.get("jira_issue_keys"),
            limit=40,
        ),
        "dedupe_fingerprint": _safe_str(state.get("dedupe_fingerprint")),
        "recommendations": _normalise_strings(state.get("recommendations"), limit=8),
        "receipt_hash": _safe_str(evidence_receipts.get("receipt_hash")),
    }


def _ensure_indexes(collection: Any) -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return

    try:
        existing_indexes = [idx.get("name") for idx in collection.list_indexes()]
        if "memory_id_unique" not in existing_indexes:
            collection.create_index(
                [("memory_id", ASCENDING)],
                unique=True,
                name="memory_id_unique",
            )
        if "namespace_created_desc" not in existing_indexes:
            collection.create_index(
                [("namespace", ASCENDING), ("created_at_utc", DESCENDING)],
                name="namespace_created_desc",
            )
        if "request_created_desc" not in existing_indexes:
            collection.create_index(
                [("request_id", ASCENDING), ("created_at_utc", DESCENDING)],
                name="request_created_desc",
            )
        if "episode_created_desc" not in existing_indexes:
            collection.create_index(
                [("episode_id", ASCENDING), ("created_at_utc", DESCENDING)],
                name="episode_created_desc",
            )
        if "workflow_created_desc" not in existing_indexes:
            collection.create_index(
                [("workflow_id", ASCENDING), ("created_at_utc", DESCENDING)],
                name="workflow_created_desc",
            )
        if "verdict_created_desc" not in existing_indexes:
            collection.create_index(
                [("verdict", ASCENDING), ("created_at_utc", DESCENDING)],
                name="verdict_created_desc",
            )
    except OperationFailure as exc:
        logger.warning(
            "Index creation partially failed for episode_critique_memories: %s",
            exc,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not ensure episode_critique_memories indexes: %s",
            exc,
        )
    _INDEXES_READY = True


def get_episode_critique_memories_collection():
    db = get_db()
    if db is None:
        return None
    coll = db[EPISODE_CRITIQUE_MEMORIES_COLLECTION]
    _ensure_indexes(coll)
    return coll


def upsert_episode_critique_memory_projection(
    *,
    record: Mapping[str, Any],
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {"updated": False, "reason": "invalid_record"}

    memory_id = _safe_str(record.get("memory_id"))
    if not memory_id:
        return {"updated": False, "reason": "missing_memory_id"}

    coll = get_episode_critique_memories_collection()
    if coll is None:
        return {"updated": False, "reason": "collection_unavailable"}

    now = _utcnow()
    payload = _build_episode_critique_memory_projection(record)
    payload["memory_id"] = memory_id
    if _safe_str(namespace):
        payload.setdefault("namespace", _safe_str(namespace))
    if _safe_str(user_id):
        payload.setdefault("user_id", _safe_str(user_id))
    if _safe_str(org_id):
        payload.setdefault("org_id", _safe_str(org_id))
    payload.setdefault("schema_version", EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION)
    payload.setdefault("created_at_utc", now.isoformat())
    payload["updated_at_utc"] = _safe_str(record.get("updated_at_utc")) or now.isoformat()

    try:
        result = coll.update_one(
            {"memory_id": memory_id},
            {"$set": payload, "$setOnInsert": {"inserted_at": now}},
            upsert=True,
        )
    except PyMongoError as exc:
        logger.warning(
            "Failed to upsert episode_critique_memories projection for memory_id=%s: %s",
            memory_id,
            exc,
        )
        return {"updated": False, "reason": "mongo_error", "memory_id": memory_id}

    updated = bool(getattr(result, "modified_count", 0) > 0)
    inserted = getattr(result, "upserted_id", None) is not None
    matched = bool(getattr(result, "matched_count", 0) > 0)
    return {
        "updated": updated or inserted,
        "inserted": inserted,
        "matched": matched,
        "memory_id": memory_id,
    }


def get_episode_critique_memory_state(memory_id: str) -> dict[str, Any] | None:
    resolved_memory_id = _safe_str(memory_id)
    if not resolved_memory_id:
        return None
    concept = _get_concept_or_none(resolved_memory_id)
    if not isinstance(concept, Mapping):
        return None
    concept_data = concept.get("concept_data")
    if not isinstance(concept_data, Mapping):
        return None
    state = concept_data.get("episode_critique_memory")
    if not isinstance(state, Mapping):
        return None
    return dict(state)


def get_episode_critique_memory_projection(memory_id: str) -> dict[str, Any] | None:
    resolved_memory_id = _safe_str(memory_id)
    if not resolved_memory_id:
        return None
    coll = get_episode_critique_memories_collection()
    if coll is None:
        return None
    doc = coll.find_one({"memory_id": resolved_memory_id}, {"_id": 0})
    if isinstance(doc, Mapping):
        return dict(doc)
    state = get_episode_critique_memory_state(resolved_memory_id)
    if not isinstance(state, Mapping):
        return None
    return _build_episode_critique_memory_projection(state)


def upsert_episode_critique_memory_from_turn(
    *,
    record: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    namespace: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    state = build_episode_critique_memory_state_from_turn(
        record=record,
        llm_debug_data=llm_debug_data,
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        org_id=org_id,
    )
    if not isinstance(state, Mapping):
        return {"success": False, "reason": "state_unavailable"}

    memory_id = _safe_str(state.get("memory_id"))
    if not memory_id:
        return {"success": False, "reason": "missing_memory_id"}

    _ensure_episode_critique_memory_type()
    _ensure_episode_critique_memory_concept(
        memory_id=memory_id,
        name=f"Episode critique memory {memory_id.replace('#V#', '')}",
        description=_safe_str(state.get("description")) or "Episode critique memory",
        user_id=_safe_str(user_id) or _safe_str(state.get("user_id")),
        org_id=_safe_str(org_id) or _safe_str(state.get("org_id")),
        namespace=_safe_str(namespace) or _safe_str(state.get("namespace")),
    )

    persisted = _persist_episode_critique_memory_state(memory_id=memory_id, state=state)
    projection = upsert_episode_critique_memory_projection(
        record=persisted,
        namespace=_safe_str(namespace) or _safe_str(state.get("namespace")),
        user_id=_safe_str(user_id) or _safe_str(state.get("user_id")),
        org_id=_safe_str(org_id) or _safe_str(state.get("org_id")),
    )

    return {
        "success": True,
        "memory_id": memory_id,
        "state": persisted,
        "projection": projection,
    }


__all__ = [
    "EPISODE_CRITIQUE_MEMORIES_COLLECTION",
    "EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION",
    "EPISODE_CRITIQUE_MEMORY_TYPE_ID",
    "build_episode_critique_memory_state_from_turn",
    "get_episode_critique_memories_collection",
    "get_episode_critique_memory_projection",
    "get_episode_critique_memory_state",
    "upsert_episode_critique_memory_from_turn",
    "upsert_episode_critique_memory_projection",
]
