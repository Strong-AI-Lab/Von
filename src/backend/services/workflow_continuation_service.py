"""Authoritative session-scoped workflow continuation context helpers.

This module turns prior turn-execution state into deterministic continuation
context for follow-up / repair turns. The goal is to route off persisted
workflow evidence rather than loose prompt-shape guessing.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .arxiv_paper_link_service import extract_arxiv_id_candidates
from .file_copy_reference_service import is_file_copy_concept_id
from .turn_execution_record_service import get_latest_turn_execution_record_projection
from .workflow_discovery_service import classify_workflow_concept_executability
from .workflow_episode_service import get_latest_workflow_use_episode

_CONCEPT_ID_PATTERN = re.compile(
    r"#V#[A-Za-z0-9][A-Za-z0-9._-]*",
    flags=re.IGNORECASE,
)
_URL_TARGET_PATTERN = re.compile(r"(?i)^https?://[^\s]+$")

# The instruction that frames continuation context for the selector/planner,
# including the user's licence to diverge, is represented prompt authority
# (JVNAUTOSCI-2500). Python no longer detects prompt-level divergence with
# lexical patterns; the represented selector stage reads the user's message
# alongside this framing and owns that judgement.
WORKFLOW_CONTINUATION_FRAMING_PROMPT_CONCEPT_ID = (
    "#V#workflow_continuation_framing_prompt"
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


def _copy_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): nested for key, nested in value.items() if isinstance(key, str)}


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


def _classify_selected_workflow_executability(
    selected_workflow_id: str | None,
) -> dict[str, Any] | None:
    workflow_id = _safe_str(selected_workflow_id)
    if not workflow_id:
        return None
    try:
        is_executable, reason, detail = classify_workflow_concept_executability(
            workflow_id
        )
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "selected_workflow_id": workflow_id,
            "selected_workflow_is_executable": False,
            "selected_workflow_executability_reason": "classification_error",
            "selected_workflow_executability_detail": type(exc).__name__,
        }

    return {
        "selected_workflow_id": workflow_id,
        "selected_workflow_is_executable": bool(is_executable),
        "selected_workflow_executability_reason": _safe_str(reason),
        "selected_workflow_executability_detail": _safe_str(detail),
    }


def resolve_workflow_continuation_framing_instruction(
    *,
    prompt_concept_id: str | None = None,
    max_chars: int = 4000,
) -> tuple[str | None, dict[str, Any]]:
    """Resolve the represented continuation-framing instruction text.

    The instruction (including the user's explicit licence to diverge from the
    prior workflow) is authored as a Vontology prompt concept. When the
    represented instruction is unavailable, this fails closed: callers inject
    only the factual continuation summary, never a Python-authored
    instruction.
    """

    from .prompt_template_service import PromptTemplateService

    requested_prompt_id = (
        _safe_str(prompt_concept_id) or WORKFLOW_CONTINUATION_FRAMING_PROMPT_CONCEPT_ID
    )
    diagnostics: dict[str, Any] = {
        "requested_prompt_concept_id": requested_prompt_id,
        "loaded_prompt_concept_id": None,
        "error": None,
    }
    try:
        prompt_service = PromptTemplateService(default_max_chars=max(1000, max_chars))
        loaded_prompt_id, prompt_text = prompt_service.resolve_prompt_text(
            [requested_prompt_id],
            fallback=None,
            max_chars=max_chars,
        )
    except Exception as exc:
        diagnostics["error"] = (
            f"workflow_continuation_framing_prompt_resolve_failed:{exc}"
        )
        return None, diagnostics

    instruction = _safe_str(prompt_text)
    if not instruction:
        diagnostics["error"] = "workflow_continuation_framing_prompt_missing_or_empty"
        return None, diagnostics
    diagnostics["loaded_prompt_concept_id"] = _safe_str(loaded_prompt_id)
    return instruction, diagnostics


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
    selected_workflow_executability = _classify_selected_workflow_executability(
        selected_workflow_id
    )

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
    workflow_required_effects_contract = execution.get(
        "workflow_required_effects_contract"
    )
    workflow_required_effects_contract = (
        dict(workflow_required_effects_contract)
        if isinstance(workflow_required_effects_contract, Mapping)
        else None
    )
    execution_summary = execution.get("summary")
    execution_summary = (
        dict(execution_summary) if isinstance(execution_summary, Mapping) else {}
    )
    latest_episode_payload = (
        dict(latest_episode) if isinstance(latest_episode, Mapping) else None
    )
    workflow_definition_identity = (
        _copy_mapping(latest_episode_payload.get("workflow_definition_identity"))
        if isinstance(latest_episode_payload, Mapping)
        else None
    )
    resolved_contract_identifiers = {
        "required_effects_contract_profile_selected_id": _safe_str(
            execution_summary.get("required_effects_contract_profile_selected_id")
        ),
        "required_effects_contract_domain": _safe_str(
            execution_summary.get("required_effects_contract_domain")
        ),
        "required_effects_contract_domain_concept_id": _safe_str(
            execution_summary.get("required_effects_contract_domain_concept_id")
        ),
        "required_effects_contract_profile_source": _safe_str(
            execution_summary.get("required_effects_contract_profile_source")
        ),
        "workflow_required_effects_contract_id": _safe_str(
            execution_summary.get("workflow_required_effects_contract_id")
        ),
        "workflow_required_effects_contract_source": _safe_str(
            execution_summary.get("workflow_required_effects_contract_source")
        ),
        "selected_execution_mode": _safe_str(
            execution_summary.get("selected_execution_mode")
        ),
        "dispatch_workflow_id": _safe_str(execution_summary.get("dispatch_workflow_id")),
        "dispatch_terminal_status": _safe_str(
            execution_summary.get("dispatch_terminal_status")
        ),
    }
    resolved_contract_identifiers = {
        key: value
        for key, value in resolved_contract_identifiers.items()
        if isinstance(value, str) and value
    }

    return {
        "session_id": clean_session_id,
        "source_request_id": (
            _safe_str(latest_record.get("request_id"))
            if isinstance(latest_record, Mapping)
            else None
        ),
        "selected_workflow_id": selected_workflow_id,
        "selected_workflow_is_executable": (
            selected_workflow_executability.get("selected_workflow_is_executable")
            if isinstance(selected_workflow_executability, Mapping)
            else None
        ),
        "selected_workflow_executability_reason": (
            _safe_str(
                selected_workflow_executability.get(
                    "selected_workflow_executability_reason"
                )
            )
            if isinstance(selected_workflow_executability, Mapping)
            else None
        ),
        "selected_workflow_executability_detail": (
            _safe_str(
                selected_workflow_executability.get(
                    "selected_workflow_executability_detail"
                )
            )
            if isinstance(selected_workflow_executability, Mapping)
            else None
        ),
        "selector_verdict": _safe_str(workflow_selection.get("selector_verdict")),
        "selector_source": _safe_str(workflow_selection.get("selector_source")) or "default",
        "completion_gate_decision": _safe_str(completion_gate.get("decision")),
        "completion_gate_decision_reason": _safe_str(
            completion_gate.get("decision_reason")
        ),
        "active_workflow_episode_id": (
            _safe_str(latest_episode_payload.get("episode_id"))
            if isinstance(latest_episode_payload, Mapping)
            else None
        ),
        "active_workflow_source": (
            _safe_str(latest_episode_payload.get("source"))
            if isinstance(latest_episode_payload, Mapping)
            else None
        ),
        "active_workflow_status": (
            _safe_str(latest_episode_payload.get("status"))
            if isinstance(latest_episode_payload, Mapping)
            else None
        ),
        "active_workflow_terminal_stage": (
            _safe_str(latest_episode_payload.get("terminal_stage"))
            if isinstance(latest_episode_payload, Mapping)
            else None
        ),
        "active_workflow_final_state": (
            _safe_str(latest_episode_payload.get("final_state"))
            if isinstance(latest_episode_payload, Mapping)
            else None
        ),
        "requires_follow_up": requires_follow_up,
        "safe_to_claim_completion": safe_to_claim_completion,
        "has_unresolved_required_effects": bool(unresolved_required_effects),
        "unresolved_required_effects": unresolved_required_effects,
        "required_effects_contract": required_effects_contract,
        "workflow_required_effects_contract": workflow_required_effects_contract,
        "workflow_definition_identity": workflow_definition_identity,
        "resolved_contract_identifiers": resolved_contract_identifiers,
        "latest_workflow_episode": latest_episode_payload,
    }


def assess_prompt_for_workflow_continuation(
    *,
    prompt: str | None,
    continuation_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify whether prior workflow state offers continuation context.

    Priority cascade:
    1. Gate checks: empty prompt or no context → not continuation.
    2. No open work in context → not continuation (workflow-state decision).
    3. Workflow-state-authoritative: open work under a specific executable
       workflow → continuation context applies.
    4. Open work without a specific workflow → do not continue implicitly.

    This decision is structural, derived only from persisted workflow state.
    Whether the user's current message *rejects or redirects away from* the
    prior workflow is a semantic judgement owned by the represented selector
    and planner stages, which receive the continuation framing (resolved from
    represented prompt authority) together with the user's message
    (JVNAUTOSCI-2500). Launch-input projection is additionally gated on the
    selected workflow actually matching the continuation workflow, so a
    divergent selection never inherits stale continuation inputs.

    Returns a dict with ``applies``, ``reason``, and ``decision_source``
    (one of ``"gate"`` or ``"workflow_state"``).
    """

    prompt_text = _safe_str(prompt) or ""
    if not prompt_text:
        return {"applies": False, "reason": "empty_prompt", "decision_source": "gate"}
    if not isinstance(continuation_context, Mapping):
        return {
            "applies": False,
            "reason": "no_continuation_context",
            "decision_source": "gate",
        }

    has_open_work = bool(
        continuation_context.get("requires_follow_up")
        or continuation_context.get("has_unresolved_required_effects")
    )
    if not has_open_work:
        return {
            "applies": False,
            "reason": "no_open_work",
            "decision_source": "workflow_state",
        }

    selected_workflow_id = _safe_str(continuation_context.get("selected_workflow_id"))
    selected_workflow_is_executable = continuation_context.get(
        "selected_workflow_is_executable"
    )
    if (
        selected_workflow_id
        and selected_workflow_is_executable is False
    ):
        return {
            "applies": False,
            "reason": "selected_workflow_not_executable",
            "decision_source": "workflow_state",
            "selected_workflow_executability_reason": _safe_str(
                continuation_context.get("selected_workflow_executability_reason")
            ),
            "selected_workflow_executability_detail": _safe_str(
                continuation_context.get("selected_workflow_executability_detail")
            ),
        }

    # Workflow-state-authoritative path: when open work exists under a
    # specific workflow, the persisted continuation state supplies framing
    # context. The represented selector/planner stages judge whether the
    # user's message diverges from it.
    has_specific_workflow = bool(selected_workflow_id)
    if has_specific_workflow:
        return {
            "applies": True,
            "reason": "workflow_state_authoritative",
            "decision_source": "workflow_state",
        }

    return {
        "applies": False,
        "reason": "open_work_without_selected_workflow",
        "decision_source": "workflow_state",
    }


def build_workflow_continuation_summary_text(
    continuation_context: Mapping[str, Any] | None,
) -> str:
    """Summarise authoritative continuation facts for routing or planning."""

    if not isinstance(continuation_context, Mapping):
        return ""
    summary_lines = [
        "ACTIVE WORKFLOW CONTINUATION CONTEXT",
        f"Session: {_safe_str(continuation_context.get('session_id')) or 'unknown'}",
        (
            "Active workflow episode: "
            f"{_safe_str(continuation_context.get('active_workflow_episode_id')) or 'unknown'}"
        ),
        (
            "Active workflow source: "
            f"{_safe_str(continuation_context.get('active_workflow_source')) or 'unknown'}"
        ),
        (
            "Selected workflow / episode workflow: "
            f"{_safe_str(continuation_context.get('selected_workflow_id')) or 'unknown'}"
        ),
        (
            "Prior completion gate verdict: "
            f"{_safe_str(continuation_context.get('completion_gate_decision')) or 'unknown'}"
        ),
    ]
    completion_gate_reason = _safe_str(
        continuation_context.get("completion_gate_decision_reason")
    )
    if completion_gate_reason:
        summary_lines.append(f"Prior completion gate reason: {completion_gate_reason}")
    active_workflow_status = _safe_str(continuation_context.get("active_workflow_status"))
    if active_workflow_status:
        summary_lines.append(f"Active workflow status: {active_workflow_status}")
    active_workflow_final_state = _safe_str(
        continuation_context.get("active_workflow_final_state")
    )
    if active_workflow_final_state:
        summary_lines.append(f"Active workflow final state: {active_workflow_final_state}")
    workflow_definition_identity = continuation_context.get("workflow_definition_identity")
    if isinstance(workflow_definition_identity, Mapping):
        workflow_definition_hash = _safe_str(
            workflow_definition_identity.get("definition_hash")
        )
        if workflow_definition_hash:
            summary_lines.append(
                f"Workflow definition identity: {workflow_definition_hash}"
            )
    if continuation_context.get("requires_follow_up") is True:
        summary_lines.append("Follow-up still required: yes")
    elif continuation_context.get("requires_follow_up") is False:
        summary_lines.append("Follow-up still required: no")

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
            status_reason = _safe_str(effect.get("status_reason")) or ""
            failure_code = _safe_str(effect.get("failure_code")) or ""
            line = f"- {effect_type}"
            if targets:
                line += f" | targets: {targets}"
            if required_tools:
                line += f" | required_tools: {required_tools}"
            if description:
                line += f" | description: {description}"
            if failure_code:
                line += f" | failure_code: {failure_code}"
            if status_reason:
                line += f" | status_reason: {status_reason}"
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
    workflow_required_effects_contract = continuation_context.get(
        "workflow_required_effects_contract"
    )
    if isinstance(workflow_required_effects_contract, Mapping):
        workflow_required_effects_contract_id = _safe_str(
            workflow_required_effects_contract.get("contract_id")
        )
        if workflow_required_effects_contract_id:
            summary_lines.append(
                "Workflow required-evidence contract: "
                f"{workflow_required_effects_contract_id}"
            )
    resolved_contract_identifiers = continuation_context.get("resolved_contract_identifiers")
    if isinstance(resolved_contract_identifiers, Mapping) and resolved_contract_identifiers:
        summary_lines.append("Resolved workflow contracts and profile identifiers:")
        ordered_keys = (
            "workflow_required_effects_contract_id",
            "workflow_required_effects_contract_source",
            "required_effects_contract_profile_selected_id",
            "required_effects_contract_profile_source",
            "required_effects_contract_domain",
            "required_effects_contract_domain_concept_id",
            "selected_execution_mode",
            "dispatch_workflow_id",
            "dispatch_terminal_status",
        )
        for key in ordered_keys:
            value = _safe_str(resolved_contract_identifiers.get(key))
            if value:
                summary_lines.append(f"- {key}: {value}")

    return "\n".join(summary_lines)


def build_workflow_continuation_routing_prompt(
    *,
    prompt: str,
    continuation_context: Mapping[str, Any],
) -> str:
    """Return routing/planning text enriched with authoritative continuation facts."""

    prompt_text = _safe_str(prompt) or ""
    if not prompt_text or not isinstance(continuation_context, Mapping):
        return prompt_text

    summary = build_workflow_continuation_summary_text(continuation_context)
    if not summary:
        return prompt_text

    return "\n".join((summary, "Current user turn:", prompt_text))


def build_workflow_continuation_system_message(
    continuation_context: Mapping[str, Any] | None,
) -> str | None:
    """Return a system message for planner/tool-routing context, if available.

    The factual continuation summary is persisted-state evidence and is always
    included. The framing instruction (how to weigh that state, including the
    user's licence to diverge) comes from represented prompt authority and is
    omitted when unavailable rather than substituted from Python.
    """

    if not isinstance(continuation_context, Mapping):
        return None
    summary = build_workflow_continuation_summary_text(continuation_context)
    if not summary:
        return None
    instruction, _diagnostics = resolve_workflow_continuation_framing_instruction()
    if instruction:
        return f"{instruction}\n\n{summary}"
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
        if not text or not is_file_copy_concept_id(text):
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


def extract_url_targets_from_continuation_context(
    continuation_context: Mapping[str, Any] | None,
) -> list[str]:
    """Return URL targets referenced by unresolved continuation artefacts."""

    if not isinstance(continuation_context, Mapping):
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str | None) -> None:
        text = _safe_str(candidate)
        if not text or not _URL_TARGET_PATTERN.fullmatch(text):
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
            for candidate in _dedupe_strings(artefact_context.get("urls")):
                _add(candidate)

    return found


def project_launch_inputs_from_continuation_context(
    continuation_context: Mapping[str, Any] | None,
    *,
    selected_workflow_id: str | None = None,
) -> dict[str, Any]:
    """Project stable launch inputs from authoritative continuation artefacts.

    When ``selected_workflow_id`` is provided, projection only applies if the
    selection actually continues the continuation context's workflow. A
    divergent selection (the represented selector chose a different workflow)
    must not inherit stale continuation targets (JVNAUTOSCI-2500).
    """

    if not isinstance(continuation_context, Mapping):
        return {}

    selection = _safe_str(selected_workflow_id)
    continuation_workflow_id = _safe_str(
        continuation_context.get("selected_workflow_id")
    )
    if (
        selection
        and continuation_workflow_id
        and selection.lower() != continuation_workflow_id.lower()
    ):
        return {}

    projected: dict[str, Any] = {}

    def _project_targets(
        plural_key: str,
        targets: Sequence[str],
        *,
        singular_key: str | None = None,
    ) -> None:
        cleaned = [str(target) for target in targets if _safe_str(target)]
        if not cleaned:
            return
        projected[plural_key] = cleaned
        if singular_key and len(cleaned) == 1:
            projected[singular_key] = cleaned[0]

    file_copy_targets = extract_file_copy_targets_from_continuation_context(
        continuation_context
    )
    _project_targets(
        "file_copy_concept_ids",
        file_copy_targets,
        singular_key="file_copy_concept_id",
    )

    concept_targets = extract_concept_targets_from_continuation_context(
        continuation_context
    )
    _project_targets("concept_ids", concept_targets)

    url_targets = extract_url_targets_from_continuation_context(continuation_context)
    _project_targets("source_uris", url_targets, singular_key="source_uri")

    arxiv_ids = extract_arxiv_id_candidates(
        continuation_context.get("required_effects_contract"),
        continuation_context.get("unresolved_required_effects"),
        url_targets,
    )
    _project_targets("arxiv_ids", arxiv_ids, singular_key="arxiv_id")

    return projected
