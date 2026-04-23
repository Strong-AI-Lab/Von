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
from .episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_IMPROVEMENT_SUGGESTION_CATEGORIES,
    EPISODE_EVALUATION_IMPROVEMENT_SUGGESTION_MAX_COUNT,
    EPISODE_EVALUATION_IMPROVEMENT_SUGGESTION_SCHEMA_VERSION,
    EPISODE_EVALUATION_IMPROVEMENT_TARGET_SURFACES,
    EPISODE_EVALUATION_WORKFLOW_ID,
)
from .text_value_service import upsert_singleton_text_relation
from .workflow_episode_service import get_latest_workflow_use_episode

logger = logging.getLogger(__name__)

EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION = "episode_critique_memory.v1"
EPISODE_CRITIQUE_SELF_IMPROVEMENT_SCHEMA_VERSION = "episode_critique_self_improvement.v1"
EPISODE_CRITIQUE_MEMORIES_COLLECTION = "episode_critique_memories"
EPISODE_CRITIQUE_MEMORY_TYPE_ID = "#V#episode_critique_memory"
EPISODE_CRITIQUE_MEMORY_PARENT_TYPE_ID = "#V#artifact"
EPISODE_CRITIQUE_MEMORY_SERVICE_SOURCE = "episode_critique_memory_service"

_INDEXES_READY = False

_CONCEPT_ID_PATTERN = re.compile(r"#V#[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_JIRA_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_SNAKE_TOKEN_PATTERN = re.compile(r"[^a-z0-9]+")
_EPISODE_IMPROVEMENT_CATEGORY_SET = set(
    EPISODE_EVALUATION_IMPROVEMENT_SUGGESTION_CATEGORIES
)
_EPISODE_IMPROVEMENT_TARGET_SURFACE_SET = set(
    EPISODE_EVALUATION_IMPROVEMENT_TARGET_SURFACES
)
_EPISODE_IMPROVEMENT_CATEGORY_ALIASES = {
    "workflow_fix": "workflow_change",
    "workflow_improvement": "workflow_change",
    "workflow_repair": "workflow_change",
    "prompt_change": "prompt_improvement",
    "prompt_fix": "prompt_improvement",
    "tool_support": "tool_addition",
    "tooling_improvement": "tool_addition",
    "support_surface": "support_surface_addition",
    "tool_metadata_improvement": "tool_metadata_fix",
    "tool_contract_improvement": "tool_contract_fix",
    "telemetry_improvement": "telemetry_addition",
    "verification_fix": "verification_improvement",
    "critic_improvement": "critic_self_improvement",
    "episode_critic_self_improvement": "critic_self_improvement",
}
_EPISODE_IMPROVEMENT_TARGET_SURFACE_ALIASES = {
    "workflow_definition": "workflow",
    "tooling": "tool",
    "tool_support": "tool",
    "support": "support_surface",
    "tool_metadata_json": "tool_metadata",
    "tool_contract_json": "tool_contract",
    "prompt_concept": "prompt",
    "critic": "episode_critic",
}
_EPISODE_IMPROVEMENT_PRIORITY_VALUES = {"high", "medium", "low"}


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


def _merge_string_lists(*values: Any, limit: int = 200) -> list[str]:
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


def _normalise_concept_id(value: Any) -> str | None:
    text = _safe_str(value)
    if not text:
        return None
    return text if _CONCEPT_ID_PATTERN.fullmatch(text) else None


def _normalise_machine_token(value: Any) -> str | None:
    text = _safe_str(value)
    if not text:
        return None
    lowered = text.lower()
    token = _SNAKE_TOKEN_PATTERN.sub("_", lowered).strip("_")
    return token or None


def _coerce_episode_improvement_category(value: Any) -> str:
    token = _normalise_machine_token(value) or "workflow_change"
    token = _EPISODE_IMPROVEMENT_CATEGORY_ALIASES.get(token, token)
    if token not in _EPISODE_IMPROVEMENT_CATEGORY_SET:
        return "workflow_change"
    return token


def _coerce_episode_improvement_target_surface(value: Any, *, category: str) -> str:
    token = _normalise_machine_token(value)
    if token:
        token = _EPISODE_IMPROVEMENT_TARGET_SURFACE_ALIASES.get(token, token)
    if token in _EPISODE_IMPROVEMENT_TARGET_SURFACE_SET:
        return token
    defaults = {
        "workflow_change": "workflow",
        "prompt_improvement": "prompt",
        "tool_addition": "tool",
        "support_surface_addition": "support_surface",
        "tool_metadata_fix": "tool_metadata",
        "tool_contract_fix": "tool_contract",
        "telemetry_addition": "telemetry",
        "verification_improvement": "workflow",
        "critic_self_improvement": "episode_critic",
    }
    return defaults.get(category, "workflow")


def _coerce_episode_improvement_priority(value: Any) -> str:
    token = _normalise_machine_token(value) or "medium"
    if token not in _EPISODE_IMPROVEMENT_PRIORITY_VALUES:
        return "medium"
    return token


def _build_episode_improvement_suggestion_id(
    *,
    category: str,
    title: str,
    target_surface: str,
    target_workflow_id: str | None,
    target_tool_name: str | None,
) -> str:
    title_token = _normalise_machine_token(title) or "suggestion"
    scope_token = (
        _normalise_machine_token(target_workflow_id)
        or _normalise_machine_token(target_tool_name)
        or target_surface
    )
    seed = {
        "category": category,
        "title": title,
        "target_surface": target_surface,
        "target_workflow_id": target_workflow_id,
        "target_tool_name": target_tool_name,
    }
    digest = _hash_payload(seed)[:8]
    return f"{category}_{scope_token}_{title_token}_{digest}".strip("_")


def _normalise_episode_improvement_suggestions(
    value: Any,
    *,
    default_workflow_id: str | None,
) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        raw_items = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw_items = [item for item in value if isinstance(item, Mapping)]
    else:
        raw_items = []

    suggestions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw_item in raw_items:
        item = _mapping_or_empty(raw_item)
        category = _coerce_episode_improvement_category(item.get("category"))
        title = _safe_str(item.get("title")) or _safe_str(item.get("suggested_change"))
        rationale = _safe_str(item.get("rationale"))
        suggested_change = _safe_str(item.get("suggested_change"))
        if not title or not rationale or not suggested_change:
            continue
        target_workflow_id = (
            _normalise_concept_id(item.get("target_workflow_id"))
            or default_workflow_id
        )
        target_prompt_concept_id = _normalise_concept_id(
            item.get("target_prompt_concept_id")
        )
        target_tool_name = _safe_str(item.get("target_tool_name"))
        target_surface = _coerce_episode_improvement_target_surface(
            item.get("target_surface"),
            category=category,
        )
        priority = _coerce_episode_improvement_priority(item.get("priority"))
        recursion_level = min(max(_safe_int(item.get("recursion_level"), default=0), 0), 1)
        if category == "critic_self_improvement":
            recursion_level = 1
        evidence_refs = _normalise_strings(item.get("evidence_refs"), limit=6)
        suggestion_id = _safe_str(item.get("suggestion_id")) or _build_episode_improvement_suggestion_id(
            category=category,
            title=title,
            target_surface=target_surface,
            target_workflow_id=target_workflow_id,
            target_tool_name=target_tool_name,
        )
        dedupe_key = (
            category,
            _normalise_machine_token(title) or suggestion_id,
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        suggestions.append(
            {
                "schema_version": EPISODE_EVALUATION_IMPROVEMENT_SUGGESTION_SCHEMA_VERSION,
                "suggestion_id": suggestion_id,
                "category": category,
                "priority": priority,
                "target_surface": target_surface,
                "target_workflow_id": target_workflow_id,
                "target_prompt_concept_id": target_prompt_concept_id,
                "target_tool_name": target_tool_name,
                "title": title,
                "rationale": rationale,
                "suggested_change": suggested_change,
                "evidence_refs": evidence_refs,
                "recursion_level": recursion_level,
            }
        )
        if len(suggestions) >= EPISODE_EVALUATION_IMPROVEMENT_SUGGESTION_MAX_COUNT:
            break

    return suggestions


def _normalise_self_improvement_rows(
    value: Any,
    *,
    key_fields: Sequence[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []

    rows: list[dict[str, Any]] = []
    row_index: dict[tuple[str, ...], int] = {}
    for raw_item in value:
        item = _mapping_or_empty(raw_item)
        if not item:
            continue
        key = tuple(_safe_str(item.get(field)) or "" for field in key_fields)
        if not any(key):
            continue
        if key in row_index:
            rows[row_index[key]].update(item)
            continue
        row_index[key] = len(rows)
        rows.append(item)
    return rows


def _normalise_self_improvement_state(value: Any) -> dict[str, Any]:
    raw = _mapping_or_empty(value)
    launches = _normalise_self_improvement_rows(
        raw.get("launches"),
        key_fields=("suggestion_id", "launch_workflow_id", "target_workflow_id"),
    )
    proposals = _normalise_self_improvement_rows(
        raw.get("proposals"),
        key_fields=("proposal_id", "target_workflow_id"),
    )
    promotion_evaluations = _normalise_self_improvement_rows(
        raw.get("promotion_evaluations"),
        key_fields=("proposal_id", "target_workflow_id"),
    )
    if not launches and not proposals and not promotion_evaluations:
        return {}
    return {
        "schema_version": EPISODE_CRITIQUE_SELF_IMPROVEMENT_SCHEMA_VERSION,
        "launches": launches,
        "proposals": proposals,
        "promotion_evaluations": promotion_evaluations,
    }


def _build_memory_id(*, request_id: str, episode_id: str | None) -> str:
    seed = episode_id or request_id
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"#V#episode_critique_memory_{digest}"


def _normalise_format_over_content_diagnostic(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    status = _safe_str(value.get("status"))
    summary = _safe_str(value.get("summary"))
    reason_codes = _normalise_strings(value.get("reason_codes"), limit=20)
    confidence = _safe_float(value.get("confidence"))
    selected_model = _safe_str(value.get("selected_model"))
    selected_provider = _safe_str(value.get("selected_provider"))
    observed_stage_id = _safe_str(value.get("observed_stage_id"))
    raw_response_format = _safe_str(value.get("raw_response_format"))

    if not any(
        (
            status,
            summary,
            reason_codes,
            confidence is not None,
            selected_model,
            selected_provider,
            observed_stage_id,
            raw_response_format,
        )
    ):
        return None

    diagnostic: dict[str, Any] = {}
    if status:
        diagnostic["status"] = status
    if summary:
        diagnostic["summary"] = summary
    if confidence is not None:
        diagnostic["confidence"] = confidence
    if reason_codes:
        diagnostic["reason_codes"] = reason_codes
    if selected_model:
        diagnostic["selected_model"] = selected_model
    if selected_provider:
        diagnostic["selected_provider"] = selected_provider
    if observed_stage_id:
        diagnostic["observed_stage_id"] = observed_stage_id
    if raw_response_format:
        diagnostic["raw_response_format"] = raw_response_format
    if "structured_output_signal" in value:
        diagnostic["structured_output_signal"] = bool(value.get("structured_output_signal"))
    if "grounded_tool_path_available" in value:
        diagnostic["grounded_tool_path_available"] = bool(
            value.get("grounded_tool_path_available")
        )
    if "grounded_tool_path_unused" in value:
        diagnostic["grounded_tool_path_unused"] = bool(
            value.get("grounded_tool_path_unused")
        )
    if "tool_invocation_count" in value:
        diagnostic["tool_invocation_count"] = _safe_int(
            value.get("tool_invocation_count")
        )
    if "search_evidence_count" in value:
        diagnostic["search_evidence_count"] = _safe_int(
            value.get("search_evidence_count")
        )
    return diagnostic


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


def _extract_implicated_from_episode_bundle(
    *,
    evidence_bundle: Mapping[str, Any],
    assessment: Mapping[str, Any],
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    locator = _mapping_or_empty(evidence_bundle.get("episode_locator"))
    observed_evidence = _mapping_or_empty(evidence_bundle.get("observed_evidence"))
    tool_ledger = _mapping_or_empty(observed_evidence.get("tool_ledger"))
    tool_invocations = tool_ledger.get("tool_invocations")
    if not isinstance(tool_invocations, list):
        tool_invocations = []

    workflow_ids = _normalise_strings(
        [
            locator.get("workflow_id"),
            *(_mapping_or_empty(assessment).get("implicated_workflow_ids") or []),
        ],
        limit=20,
    )
    tool_names = _normalise_strings(
        [
            *[
                _safe_str(item.get("tool"))
                for item in tool_invocations
                if isinstance(item, Mapping)
            ],
            *(_mapping_or_empty(assessment).get("implicated_tool_names") or []),
        ],
        limit=60,
    )
    concept_ids, issue_keys = _extract_concept_ids_and_issue_keys(
        evidence_bundle,
        assessment,
    )
    task_ids = _extract_task_ids(evidence_bundle, assessment)
    return workflow_ids, tool_names, concept_ids[:120], task_ids[:40], issue_keys[:40]


def build_episode_critique_memory_state_from_episode_assessment(
    *,
    evidence_bundle: Mapping[str, Any],
    assessment: Mapping[str, Any],
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(evidence_bundle, Mapping) or not isinstance(assessment, Mapping):
        return None

    locator = _mapping_or_empty(evidence_bundle.get("episode_locator"))
    request_id = _safe_str(locator.get("request_id")) or _safe_str(locator.get("instance_id"))
    if not request_id:
        return None

    episode_id = _safe_str(locator.get("episode_id"))
    instance_id = _safe_str(locator.get("instance_id"))
    workflow_id = _safe_str(locator.get("workflow_id"))
    resolved_namespace = _safe_str(namespace) or _safe_str(locator.get("namespace"))
    resolved_user_id = _safe_str(user_id) or _safe_str(locator.get("user_id"))
    resolved_org_id = _safe_str(org_id) or _safe_str(locator.get("org_id"))
    resolved_session_id = _safe_str(locator.get("session_id"))
    memory_id = _build_memory_id(
        request_id=request_id,
        episode_id=episode_id or instance_id,
    )

    verdict = _safe_str(assessment.get("verdict")) or "inconclusive"
    unresolved_check_count = _safe_int(
        assessment.get("unresolved_check_count"),
        default=len(_normalise_strings(evidence_bundle.get("fail_closed_reason_codes"))),
    )
    confidence = _safe_float(assessment.get("confidence"))
    workflow_ids, tool_names, concept_ids, task_ids, issue_keys = (
        _extract_implicated_from_episode_bundle(
            evidence_bundle=evidence_bundle,
            assessment=assessment,
        )
    )
    observed_evidence = _mapping_or_empty(evidence_bundle.get("observed_evidence"))
    workflow_identity = _mapping_or_empty(observed_evidence.get("workflow_definition_identity"))
    recommendations = _normalise_strings(assessment.get("recommendations"), limit=8)
    improvement_suggestions = _normalise_episode_improvement_suggestions(
        assessment.get("improvement_suggestions"),
        default_workflow_id=workflow_id,
    )
    format_over_content_diagnostic = _normalise_format_over_content_diagnostic(
        assessment.get("format_over_content_diagnostic")
        or evidence_bundle.get("format_over_content_diagnostic")
    )
    summary = _safe_str(assessment.get("summary"))
    created_at_utc = _utcnow_iso()
    updated_at_utc = created_at_utc

    subject_kind = "workflow_use_episode"
    if not episode_id and instance_id:
        subject_kind = "workflow_terminal_instance"
    elif not episode_id:
        subject_kind = "turn_execution_request"

    receipt_payload = {
        "episode_bundle_receipt": _mapping_or_empty(evidence_bundle.get("bundle_receipt")),
        "section_receipts": _mapping_or_empty(evidence_bundle.get("receipts")),
        "assessment_sha256": _hash_payload(assessment),
    }

    critic_state: dict[str, Any] = {
        "workflow_id": EPISODE_EVALUATION_WORKFLOW_ID,
        "summary": {
            "summary": summary,
            "maintenance_follow_up_recommended": bool(
                assessment.get("maintenance_follow_up_recommended")
            ),
            "root_cause_count": len(
                [
                    item
                    for item in (assessment.get("root_causes") or [])
                    if isinstance(item, Mapping)
                ]
            ),
        },
        "verdict": verdict,
        "confidence": confidence,
        "unresolved_check_count": unresolved_check_count,
        "assessment": dict(assessment),
    }
    if format_over_content_diagnostic is not None:
        critic_state["format_over_content_diagnostic"] = format_over_content_diagnostic

    state: dict[str, Any] = {
        "schema_version": EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION,
        "memory_id": memory_id,
        "request_id": request_id,
        "created_at_utc": created_at_utc,
        "updated_at_utc": updated_at_utc,
        "namespace": resolved_namespace,
        "session_id": resolved_session_id,
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "subject_episode": {
            "subject_kind": subject_kind,
            "episode_id": episode_id,
            "workflow_id": workflow_id,
            "instance_id": instance_id,
            "stable_key": _safe_str(locator.get("episode_id")) or instance_id or request_id,
            "source": _safe_str(locator.get("execution_trace_id")) or subject_kind,
            "turn_id": _safe_str(locator.get("request_id")),
            "session_id": resolved_session_id,
            "namespace": resolved_namespace,
            "workflow_definition_identity": workflow_identity,
        },
        "critic": critic_state,
        "completion_gate": {},
        "implicated": {
            "workflow_ids": workflow_ids,
            "tool_names": tool_names,
            "concept_ids": concept_ids,
        },
        "remediation": {
            "task_ids": task_ids,
            "jira_issue_keys": issue_keys,
        },
        "recommendations": recommendations,
        "improvement_suggestions": improvement_suggestions,
        "self_improvement": {},
        "evidence_receipts": {
            **receipt_payload,
            "receipt_hash": _hash_payload(receipt_payload),
        },
        "dedupe_fingerprint": _hash_payload(
            {
                "request_id": request_id,
                "episode_id": episode_id,
                "instance_id": instance_id,
                "workflow_id": workflow_id,
                "verdict": verdict,
                "assessment": assessment,
            }
        ),
        "description": summary
        or _build_description(
            request_id=request_id,
            episode_id=episode_id or instance_id,
            verdict=verdict,
            unresolved_check_count=unresolved_check_count,
            workflow_id=workflow_id,
        ),
        "routing": {},
    }
    return state


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
    format_over_content_diagnostic = _normalise_format_over_content_diagnostic(
        critic.get("format_over_content_diagnostic")
    )

    critic_state: dict[str, Any] = {
        "workflow_id": _safe_str(critic.get("workflow_id")),
        "summary": critic_summary,
        "verdict": verdict,
        "confidence": confidence,
        "unresolved_check_count": unresolved_check_count,
    }
    if format_over_content_diagnostic is not None:
        critic_state["format_over_content_diagnostic"] = format_over_content_diagnostic

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
        "critic": critic_state,
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
        "routing": {},
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
    routing = _mapping_or_empty(state.get("routing"))
    critic_summary = _mapping_or_empty(critic.get("summary"))
    format_over_content_diagnostic = _mapping_or_empty(
        critic.get("format_over_content_diagnostic")
    )
    improvement_suggestions = _normalise_episode_improvement_suggestions(
        state.get("improvement_suggestions"),
        default_workflow_id=_safe_str(subject_episode.get("workflow_id")),
    )

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
        "critic_summary_text": _safe_str(critic_summary.get("summary")),
        "confidence": _safe_float(critic.get("confidence")),
        "unresolved_check_count": _safe_int(critic.get("unresolved_check_count")),
        "format_over_content_status": _safe_str(
            format_over_content_diagnostic.get("status")
        ),
        "format_over_content_summary": _safe_str(
            format_over_content_diagnostic.get("summary")
        ),
        "format_over_content_confidence": _safe_float(
            format_over_content_diagnostic.get("confidence")
        ),
        "format_over_content_reason_codes": _normalise_strings(
            format_over_content_diagnostic.get("reason_codes"),
            limit=20,
        ),
        "format_over_content_model": _safe_str(
            format_over_content_diagnostic.get("selected_model")
        ),
        "format_over_content_provider": _safe_str(
            format_over_content_diagnostic.get("selected_provider")
        ),
        "format_over_content_stage_id": _safe_str(
            format_over_content_diagnostic.get("observed_stage_id")
        ),
        "format_over_content_raw_response_format": _safe_str(
            format_over_content_diagnostic.get("raw_response_format")
        ),
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
        "routing_decision": _safe_str(routing.get("decision")),
        "routing_reason_codes": _normalise_strings(
            routing.get("reason_codes"),
            limit=20,
        ),
        "routing_fingerprint": _safe_str(routing.get("fingerprint")),
        "routing_repeat_count": _safe_int(routing.get("repeat_count")),
        "routing_task_action": _safe_str(routing.get("task_action")),
        "routing_jira_action": _safe_str(routing.get("jira_action")),
        "dedupe_fingerprint": _safe_str(state.get("dedupe_fingerprint")),
        "recommendations": _normalise_strings(state.get("recommendations"), limit=8),
        "improvement_suggestions": improvement_suggestions,
        "improvement_suggestion_count": len(improvement_suggestions),
        "improvement_suggestion_categories": _normalise_strings(
            [item.get("category") for item in improvement_suggestions],
            limit=20,
        ),
        "improvement_target_workflow_ids": _normalise_strings(
            [item.get("target_workflow_id") for item in improvement_suggestions],
            limit=20,
        ),
        "improvement_target_tool_names": _normalise_strings(
            [item.get("target_tool_name") for item in improvement_suggestions],
            limit=20,
        ),
        "self_improvement_launch_count": len(
            _normalise_self_improvement_state(state.get("self_improvement")).get("launches")
            or []
        ),
        "self_improvement_proposal_count": len(
            _normalise_self_improvement_state(state.get("self_improvement")).get(
                "proposals"
            )
            or []
        ),
        "self_improvement_target_workflow_ids": _normalise_strings(
            [
                item.get("target_workflow_id")
                for item in (
                    _normalise_self_improvement_state(state.get("self_improvement")).get(
                        "proposals"
                    )
                    or []
                )
            ],
            limit=20,
        ),
        "self_improvement_promotion_recommendations": _normalise_strings(
            [
                item.get("promotion_recommendation")
                for item in (
                    _normalise_self_improvement_state(state.get("self_improvement")).get(
                        "promotion_evaluations"
                    )
                    or []
                )
            ],
            limit=20,
        ),
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
        if "implicated_workflow_created_desc" not in existing_indexes:
            collection.create_index(
                [("implicated_workflow_ids", ASCENDING), ("created_at_utc", DESCENDING)],
                name="implicated_workflow_created_desc",
            )
        if "verdict_created_desc" not in existing_indexes:
            collection.create_index(
                [("verdict", ASCENDING), ("created_at_utc", DESCENDING)],
                name="verdict_created_desc",
            )
        if "namespace_routing_fingerprint" not in existing_indexes:
            collection.create_index(
                [("namespace", ASCENDING), ("routing_fingerprint", ASCENDING)],
                name="namespace_routing_fingerprint",
            )
        if "namespace_routing_decision_created_desc" not in existing_indexes:
            collection.create_index(
                [
                    ("namespace", ASCENDING),
                    ("routing_decision", ASCENDING),
                    ("created_at_utc", DESCENDING),
                ],
                name="namespace_routing_decision_created_desc",
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


def list_recent_workflow_improvement_suggestions(
    workflow_id: str,
    *,
    namespace: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    workflow_id_clean = _safe_str(workflow_id)
    if not workflow_id_clean:
        return []

    coll = get_episode_critique_memories_collection()
    if coll is None:
        return []

    query: dict[str, Any] = {
        "$or": [
            {"workflow_id": workflow_id_clean},
            {"implicated_workflow_ids": workflow_id_clean},
            {"improvement_target_workflow_ids": workflow_id_clean},
        ],
        "improvement_suggestion_count": {"$gt": 0},
    }
    if _safe_str(namespace):
        query["namespace"] = _safe_str(namespace)

    projection = {
        "_id": 0,
        "memory_id": 1,
        "request_id": 1,
        "episode_id": 1,
        "workflow_id": 1,
        "created_at_utc": 1,
        "updated_at_utc": 1,
        "verdict": 1,
        "critic_summary_text": 1,
        "receipt_hash": 1,
        "improvement_suggestions": 1,
    }

    try:
        cursor = coll.find(query, projection)
        if hasattr(cursor, "sort"):
            cursor = cursor.sort("created_at_utc", DESCENDING)
        if hasattr(cursor, "limit"):
            cursor = cursor.limit(max(1, min(int(limit), 20)))
        docs = list(cursor)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not list recent workflow improvement suggestions for %s: %s",
            workflow_id_clean,
            exc,
        )
        return []

    suggestions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw_doc in docs:
        doc = dict(raw_doc) if isinstance(raw_doc, Mapping) else {}
        for raw_item in doc.get("improvement_suggestions") or []:
            item = _mapping_or_empty(raw_item)
            item_workflow_id = _safe_str(item.get("target_workflow_id"))
            if item_workflow_id and item_workflow_id != workflow_id_clean:
                continue
            signature = (
                _safe_str(item.get("suggestion_id")) or "",
                _safe_str(doc.get("memory_id")) or "",
            )
            if signature in seen:
                continue
            seen.add(signature)
            suggestions.append(
                {
                    **item,
                    "memory_id": _safe_str(doc.get("memory_id")),
                    "request_id": _safe_str(doc.get("request_id")),
                    "episode_id": _safe_str(doc.get("episode_id")),
                    "source_workflow_id": _safe_str(doc.get("workflow_id")),
                    "created_at_utc": _safe_str(doc.get("created_at_utc")),
                    "updated_at_utc": _safe_str(doc.get("updated_at_utc")),
                    "verdict": _safe_str(doc.get("verdict")),
                    "critic_summary_text": _safe_str(doc.get("critic_summary_text")),
                    "receipt_hash": _safe_str(doc.get("receipt_hash")),
                }
            )
            if len(suggestions) >= max(1, min(int(limit), 20)):
                return suggestions
    return suggestions


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


def upsert_episode_critique_memory_from_episode_assessment(
    *,
    evidence_bundle: Mapping[str, Any],
    assessment: Mapping[str, Any],
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    state = build_episode_critique_memory_state_from_episode_assessment(
        evidence_bundle=evidence_bundle,
        assessment=assessment,
        namespace=namespace,
        user_id=user_id,
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


def record_episode_critique_memory_routing(
    *,
    memory_id: str,
    routing: Mapping[str, Any],
    remediation_task_ids: Sequence[str] | None = None,
    remediation_issue_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    state = get_episode_critique_memory_state(memory_id)
    if not isinstance(state, Mapping):
        return {"success": False, "reason": "memory_not_found", "memory_id": memory_id}

    updated_state = dict(state)
    updated_state["updated_at_utc"] = _utcnow_iso()
    updated_state["routing"] = dict(routing) if isinstance(routing, Mapping) else {}

    remediation = _mapping_or_empty(updated_state.get("remediation"))
    remediation["task_ids"] = _merge_string_lists(
        remediation.get("task_ids"),
        remediation_task_ids,
        limit=40,
    )
    remediation["jira_issue_keys"] = _merge_string_lists(
        remediation.get("jira_issue_keys"),
        remediation_issue_keys,
        limit=40,
    )
    updated_state["remediation"] = remediation

    persisted = _persist_episode_critique_memory_state(
        memory_id=memory_id,
        state=updated_state,
    )
    projection = upsert_episode_critique_memory_projection(
        record=persisted,
        namespace=_safe_str(updated_state.get("namespace")),
        user_id=_safe_str(updated_state.get("user_id")),
        org_id=_safe_str(updated_state.get("org_id")),
    )
    return {
        "success": True,
        "memory_id": memory_id,
        "state": persisted,
        "projection": projection,
    }


def record_episode_critique_memory_self_improvement(
    *,
    memory_id: str,
    launches: Sequence[Mapping[str, Any]] | None = None,
    proposals: Sequence[Mapping[str, Any]] | None = None,
    promotion_evaluations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    state = get_episode_critique_memory_state(memory_id)
    if not isinstance(state, Mapping):
        return {"success": False, "reason": "memory_not_found", "memory_id": memory_id}

    updated_state = dict(state)
    updated_state["updated_at_utc"] = _utcnow_iso()
    existing = _normalise_self_improvement_state(updated_state.get("self_improvement"))
    merged_state = _normalise_self_improvement_state(
        {
            "launches": [
                *(existing.get("launches") or []),
                *(
                    [dict(item) for item in launches if isinstance(item, Mapping)]
                    if isinstance(launches, Sequence)
                    else []
                ),
            ],
            "proposals": [
                *(existing.get("proposals") or []),
                *(
                    [dict(item) for item in proposals if isinstance(item, Mapping)]
                    if isinstance(proposals, Sequence)
                    else []
                ),
            ],
            "promotion_evaluations": [
                *(existing.get("promotion_evaluations") or []),
                *(
                    [
                        dict(item)
                        for item in promotion_evaluations
                        if isinstance(item, Mapping)
                    ]
                    if isinstance(promotion_evaluations, Sequence)
                    else []
                ),
            ],
        }
    )
    updated_state["self_improvement"] = merged_state

    persisted = _persist_episode_critique_memory_state(
        memory_id=memory_id,
        state=updated_state,
    )
    projection = upsert_episode_critique_memory_projection(
        record=persisted,
        namespace=_safe_str(updated_state.get("namespace")),
        user_id=_safe_str(updated_state.get("user_id")),
        org_id=_safe_str(updated_state.get("org_id")),
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
    "EPISODE_CRITIQUE_SELF_IMPROVEMENT_SCHEMA_VERSION",
    "EPISODE_CRITIQUE_MEMORY_TYPE_ID",
    "build_episode_critique_memory_state_from_episode_assessment",
    "build_episode_critique_memory_state_from_turn",
    "get_episode_critique_memories_collection",
    "get_episode_critique_memory_projection",
    "get_episode_critique_memory_state",
    "list_recent_workflow_improvement_suggestions",
    "record_episode_critique_memory_routing",
    "record_episode_critique_memory_self_improvement",
    "upsert_episode_critique_memory_from_episode_assessment",
    "upsert_episode_critique_memory_from_turn",
    "upsert_episode_critique_memory_projection",
]
