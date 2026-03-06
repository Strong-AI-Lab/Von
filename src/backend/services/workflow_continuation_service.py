"""Authoritative session-scoped workflow continuation context helpers.

This module turns prior turn-execution state into deterministic continuation
context for follow-up / repair turns. The goal is to route off persisted
workflow evidence rather than loose prompt-shape guessing.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .turn_execution_record_service import get_latest_turn_execution_record_projection
from .workflow_episode_service import get_latest_workflow_use_episode

_FILE_COPY_CONCEPT_ID_PATTERN = re.compile(
    r"#V#[A-Za-z0-9][A-Za-z0-9._-]*file_copy[A-Za-z0-9._-]*",
    flags=re.IGNORECASE,
)
_CONCEPT_ID_PATTERN = re.compile(
    r"#V#[A-Za-z0-9][A-Za-z0-9._-]*",
    flags=re.IGNORECASE,
)
_SHORT_CONTINUATION_PROMPT_PATTERN = re.compile(
    r"^\s*(?:yes|yep|yeah|ok|okay|sure|do it|go ahead|proceed|continue|"
    r"please do|sounds good|looks good|that works|finish|retry|repair|fix|"
    r"verify|check|status|again)\b",
    flags=re.IGNORECASE,
)
_FOLLOW_UP_REPAIR_PROMPT_PATTERN = re.compile(
    r"\b(?:continue|proceed|finish|complete|retry|repair|fix|verify|check|"
    r"still missing|not created|not added|didn't|did not|wasn't|was not|"
    r"weren't|were not|why didn't|why did not|why wasn't|why was not|yet)\b",
    flags=re.IGNORECASE,
)
_REPRESENTATION_TERM_PATTERN = re.compile(
    r"\b(?:represent|representation|materialis(?:e|ation)|model|profile)\b",
    flags=re.IGNORECASE,
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _dedupe_strings(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(text)
    return deduped


def _normalise_required_effect(effect: Mapping[str, Any]) -> dict[str, Any] | None:
    effect_id = _safe_str(effect.get("effect_id"))
    effect_type = _safe_str(effect.get("effect_type"))
    status = _safe_str(effect.get("status"))
    if not effect_type or status not in {"not_executed", "not_satisfied"}:
        return None

    return {
        "effect_id": effect_id,
        "effect_type": effect_type,
        "status": status,
        "description": _safe_str(effect.get("description")),
        "required_tools": _dedupe_strings(effect.get("required_tools")),
        "targets": _dedupe_strings(effect.get("targets")),
        "representation_domain_id": _safe_str(effect.get("representation_domain_id")),
        "representation_profile_concept_id": _safe_str(
            effect.get("representation_profile_concept_id")
        ),
        "failure_code": _safe_str(effect.get("failure_code")),
        "status_reason": _safe_str(effect.get("status_reason")),
    }


def get_session_workflow_continuation_context(
    *,
    session_id: str | None,
    namespace: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest authoritative follow-up context for a chat session."""

    clean_session_id = _safe_str(session_id)
    if not clean_session_id:
        return None

    latest_record = get_latest_turn_execution_record_projection(
        session_id=clean_session_id,
        namespace=namespace,
        user_id=user_id,
    )
    latest_episode = get_latest_workflow_use_episode(
        namespace=namespace,
        session_id=clean_session_id,
    )

    if not isinstance(latest_record, Mapping) and not isinstance(latest_episode, Mapping):
        return None

    workflow_selection = (
        latest_record.get("workflow_selection")
        if isinstance(latest_record, Mapping)
        else None
    )
    workflow_selection = workflow_selection if isinstance(workflow_selection, Mapping) else {}
    completion_gate = (
        latest_record.get("completion_gate") if isinstance(latest_record, Mapping) else None
    )
    completion_gate = completion_gate if isinstance(completion_gate, Mapping) else {}
    execution = latest_record.get("execution") if isinstance(latest_record, Mapping) else None
    execution = execution if isinstance(execution, Mapping) else {}
    raw_required_effects = (
        latest_record.get("required_effects") if isinstance(latest_record, Mapping) else None
    )
    unresolved_required_effects = [
        item
        for item in (
            _normalise_required_effect(effect)
            for effect in (raw_required_effects if isinstance(raw_required_effects, list) else [])
            if isinstance(effect, Mapping)
        )
        if isinstance(item, dict)
    ]

    requires_follow_up = bool(completion_gate.get("requires_follow_up", False))
    safe_to_claim_completion = bool(
        completion_gate.get("safe_to_claim_completion", not requires_follow_up)
    )
    selected_workflow_id = _safe_str(workflow_selection.get("selected_workflow_id"))
    if selected_workflow_id is None and isinstance(latest_episode, Mapping):
        selected_workflow_id = _safe_str(latest_episode.get("workflow_id"))

    if (
        not unresolved_required_effects
        and not requires_follow_up
        and not isinstance(latest_episode, Mapping)
    ):
        return None

    required_effects_contract = execution.get("required_effects_contract")
    required_effects_contract = (
        dict(required_effects_contract)
        if isinstance(required_effects_contract, Mapping)
        else None
    )

    return {
        "session_id": clean_session_id,
        "source_request_id": (
            _safe_str(latest_record.get("request_id"))
            if isinstance(latest_record, Mapping)
            else None
        ),
        "selected_workflow_id": selected_workflow_id,
        "selector_verdict": _safe_str(workflow_selection.get("selector_verdict")),
        "selector_source": _safe_str(workflow_selection.get("selector_source")) or "default",
        "completion_gate_decision": _safe_str(completion_gate.get("decision")),
        "completion_gate_decision_reason": _safe_str(
            completion_gate.get("decision_reason")
        ),
        "requires_follow_up": requires_follow_up,
        "safe_to_claim_completion": safe_to_claim_completion,
        "has_unresolved_required_effects": bool(unresolved_required_effects),
        "unresolved_required_effects": unresolved_required_effects,
        "required_effects_contract": required_effects_contract,
        "latest_workflow_episode": (
            dict(latest_episode) if isinstance(latest_episode, Mapping) else None
        ),
    }


def assess_prompt_for_workflow_continuation(
    *,
    prompt: str | None,
    continuation_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify whether the current prompt should continue prior workflow state."""

    prompt_text = _safe_str(prompt) or ""
    if not prompt_text:
        return {"applies": False, "reason": "empty_prompt"}
    if not isinstance(continuation_context, Mapping):
        return {"applies": False, "reason": "no_continuation_context"}

    has_open_work = bool(
        continuation_context.get("requires_follow_up")
        or continuation_context.get("has_unresolved_required_effects")
    )
    if not has_open_work:
        return {"applies": False, "reason": "no_open_work"}

    if len(prompt_text) <= 24 and _SHORT_CONTINUATION_PROMPT_PATTERN.search(prompt_text):
        return {"applies": True, "reason": "short_follow_up_prompt"}

    if _FOLLOW_UP_REPAIR_PROMPT_PATTERN.search(prompt_text):
        return {"applies": True, "reason": "explicit_follow_up_or_repair_prompt"}

    unresolved_effects = continuation_context.get("unresolved_required_effects")
    if isinstance(unresolved_effects, list):
        for effect in unresolved_effects:
            if not isinstance(effect, Mapping):
                continue
            effect_type = _safe_str(effect.get("effect_type")) or ""
            if "representation" in effect_type.lower() and _REPRESENTATION_TERM_PATTERN.search(
                prompt_text
            ):
                return {
                    "applies": True,
                    "reason": "representation_follow_up_prompt",
                }

    return {"applies": False, "reason": "prompt_not_continuation"}


def build_workflow_continuation_routing_prompt(
    *,
    prompt: str,
    continuation_context: Mapping[str, Any],
) -> str:
    """Return routing/planning text enriched with authoritative continuation facts."""

    prompt_text = _safe_str(prompt) or ""
    if not prompt_text or not isinstance(continuation_context, Mapping):
        return prompt_text

    summary_lines = [
        "ACTIVE WORKFLOW CONTINUATION CONTEXT",
        f"Session: {_safe_str(continuation_context.get('session_id')) or 'unknown'}",
        (
            "Selected workflow: "
            f"{_safe_str(continuation_context.get('selected_workflow_id')) or 'unknown'}"
        ),
        (
            "Completion gate: "
            f"{_safe_str(continuation_context.get('completion_gate_decision')) or 'unknown'}"
        ),
    ]

    unresolved_effects = continuation_context.get("unresolved_required_effects")
    if isinstance(unresolved_effects, list) and unresolved_effects:
        summary_lines.append("Unresolved required effects:")
        for effect in unresolved_effects[:6]:
            if not isinstance(effect, Mapping):
                continue
            effect_type = _safe_str(effect.get("effect_type")) or "unknown"
            targets = ", ".join(_dedupe_strings(effect.get("targets")))
            required_tools = ", ".join(_dedupe_strings(effect.get("required_tools")))
            description = _safe_str(effect.get("description")) or ""
            line = f"- {effect_type}"
            if targets:
                line += f" | targets: {targets}"
            if required_tools:
                line += f" | required_tools: {required_tools}"
            if description:
                line += f" | description: {description}"
            summary_lines.append(line)

    required_effects_contract = continuation_context.get("required_effects_contract")
    if isinstance(required_effects_contract, Mapping):
        artefact_context = required_effects_contract.get("artefact_context")
        if isinstance(artefact_context, Mapping):
            file_copy_ids = ", ".join(
                _dedupe_strings(artefact_context.get("file_copy_ids"))
            )
            urls = ", ".join(_dedupe_strings(artefact_context.get("urls")))
            if file_copy_ids:
                summary_lines.append(f"Artefact file_copy_ids: {file_copy_ids}")
            if urls:
                summary_lines.append(f"Artefact urls: {urls}")

    summary_lines.append("Current user turn:")
    summary_lines.append(prompt_text)
    return "\n".join(summary_lines)


def build_workflow_continuation_system_message(
    continuation_context: Mapping[str, Any] | None,
) -> str | None:
    """Return a system message for planner/tool-routing context, if available."""

    if not isinstance(continuation_context, Mapping):
        return None
    summary = build_workflow_continuation_routing_prompt(
        prompt="Use this authoritative session state when deciding continuation, repair, or verification work.",
        continuation_context=continuation_context,
    )
    if not summary:
        return None
    return summary


def extract_file_copy_targets_from_continuation_context(
    continuation_context: Mapping[str, Any] | None,
) -> list[str]:
    """Return file-copy concept targets referenced by unresolved continuation state."""

    if not isinstance(continuation_context, Mapping):
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str | None) -> None:
        text = _safe_str(candidate)
        if not text or not _FILE_COPY_CONCEPT_ID_PATTERN.fullmatch(text):
            return
        lowered = text.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        found.append(text)

    unresolved_effects = continuation_context.get("unresolved_required_effects")
    if isinstance(unresolved_effects, list):
        for effect in unresolved_effects:
            if not isinstance(effect, Mapping):
                continue
            for target in _dedupe_strings(effect.get("targets")):
                _add(target)

    required_effects_contract = continuation_context.get("required_effects_contract")
    if isinstance(required_effects_contract, Mapping):
        artefact_context = required_effects_contract.get("artefact_context")
        if isinstance(artefact_context, Mapping):
            for target in _dedupe_strings(artefact_context.get("file_copy_ids")):
                _add(target)

    return found


def extract_concept_targets_from_continuation_context(
    continuation_context: Mapping[str, Any] | None,
) -> list[str]:
    """Return concept-id targets referenced by unresolved continuation state."""

    if not isinstance(continuation_context, Mapping):
        return []
    found: list[str] = []
    seen: set[str] = set()

    unresolved_effects = continuation_context.get("unresolved_required_effects")
    if not isinstance(unresolved_effects, list):
        return found

    for effect in unresolved_effects:
        if not isinstance(effect, Mapping):
            continue
        for target in _dedupe_strings(effect.get("targets")):
            if not _CONCEPT_ID_PATTERN.fullmatch(target):
                continue
            lowered = target.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            found.append(target)

    return found
