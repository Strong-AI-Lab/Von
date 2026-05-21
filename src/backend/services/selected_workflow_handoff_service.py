"""Support surfaces for selected-workflow recovery handoff decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, cast

from src.backend.workflows.execution_contracts import (
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
    WORKFLOW_RESULT_ENVELOPE_KEY,
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
)
from src.backend.workflows.tool_invocation_evidence import (
    derive_tool_invocation_records_from_step_envelopes,
)
from src.backend.workflows.turn_expected_outcome_contract import (
    TurnExpectedOutcomeContract,
)

_REQUIRED_EFFECTS_UNRESTRICTED_TOOL_SURFACE_ACTION_IDS = frozenset(
    {
        "tool_calling.respond",
        "tool_calling.execute",
    }
)


class MissingPromptRequirementsDeriver(Protocol):
    def __call__(
        self,
        *,
        required_tools: Sequence[str],
        required_fetch_concept_ids: Sequence[str],
        required_read_file_copy_ids: Sequence[str] = (),
        required_scholarly_representation_for_file_copy_ids: Sequence[str] = (),
        tool_invocations: Sequence[Mapping[str, Any]],
    ) -> tuple[list[str], list[str], list[str], list[str]]: ...


@dataclass(frozen=True)
class SelectedWorkflowMissingToolEvaluation:
    """Classified missing-tool state for a selected workflow result."""

    missing_required_tools: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_tools_source: str | None
    evidence_classification: Mapping[str, Any]


@dataclass(frozen=True)
class SelectedWorkflowHandoffDecision:
    """Result of interpreting whether selected-workflow execution should hand off."""

    continue_to_tool_pipeline: bool
    reason: str | None
    fallback_tool_workflow_id: str | None
    missing_required_tools: tuple[str, ...]
    required_tools_for_tool_pipeline: tuple[str, ...]
    selected_workflow_snapshot: Mapping[str, Any] | None
    custom_failure_extra: Mapping[str, Any]
    recovery_handoff_payload: Mapping[str, Any] | None
    evidence_classification: Mapping[str, Any]


def clean_workflow_summary_text(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def workflow_execution_summary_mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [
        cast(Mapping[str, Any], item) for item in value if isinstance(item, Mapping)
    ]


def normalise_tool_name_sequence(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    tools: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        tool = item.strip()
        if not tool:
            continue
        lowered = tool.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        tools.append(tool)
    return tools


def workflow_required_tools_from_contract(
    contract: Mapping[str, Any] | None,
) -> list[str]:
    if not isinstance(contract, Mapping):
        return []
    required_effects = contract.get("required_effects")
    if not isinstance(required_effects, Sequence) or isinstance(
        required_effects, (str, bytes, bytearray)
    ):
        return []
    tools: list[str] = []
    seen: set[str] = set()
    for effect in required_effects:
        if not isinstance(effect, Mapping):
            continue
        raw_tools = effect.get("required_tools")
        if not isinstance(raw_tools, Sequence) or isinstance(
            raw_tools, (str, bytes, bytearray)
        ):
            continue
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, str):
                continue
            tool = raw_tool.strip()
            if not tool:
                continue
            lowered = tool.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            tools.append(tool)
    return tools


def llm_policy_enables_tool_surface(llm_policy: Mapping[str, Any]) -> bool:
    raw_mode = str(llm_policy.get("tool_mode") or "").strip().lower()
    if raw_mode in {"none", "disabled", "off"}:
        return False
    if raw_mode in {"allowed", "tool_augmented", "tools"}:
        return True
    return "allowed_tools" in llm_policy or "required_tools" in llm_policy


def evaluate_workflow_required_effects_tool_policy(
    workflow_def: Any | None,
) -> dict[str, Any]:
    metadata = (
        getattr(workflow_def, "metadata", None) if workflow_def is not None else None
    )
    contract = (
        metadata.get("required_effects_contract")
        if isinstance(metadata, Mapping)
        else None
    )
    required_tools = workflow_required_tools_from_contract(
        contract if isinstance(contract, Mapping) else None
    )
    if not required_tools:
        return {
            "checked": False,
            "ok": True,
            "required_tools": [],
            "allowed_tools": [],
            "direct_action_tools": [],
            "unrestricted_llm_tool_step": False,
            "unrestricted_tool_pipeline_step": False,
            "unrestricted_tool_pipeline_actions": [],
            "unavailable_required_tools": [],
            "reason_code": "no_required_effect_tools",
        }

    allowed_tools: set[str] = set()
    direct_action_tools: set[str] = set()
    has_unrestricted_llm_tool_step = False
    unrestricted_tool_pipeline_actions: set[str] = set()
    states = getattr(workflow_def, "states", None)
    if isinstance(states, Mapping):
        for state in states.values():
            actions = getattr(state, "actions", None)
            if not isinstance(actions, Sequence) or isinstance(actions, str):
                continue
            for action in actions:
                action_id = getattr(action, "action_id", None)
                if isinstance(action_id, str) and action_id.strip():
                    cleaned_action_id = action_id.strip().lower()
                    direct_action_tools.add(cleaned_action_id)
                    if (
                        cleaned_action_id
                        in _REQUIRED_EFFECTS_UNRESTRICTED_TOOL_SURFACE_ACTION_IDS
                    ):
                        unrestricted_tool_pipeline_actions.add(cleaned_action_id)
                if not bool(getattr(action, "is_llm_step", False)):
                    continue
                llm_policy = getattr(action, "llm_policy", None)
                llm_policy_map = llm_policy if isinstance(llm_policy, Mapping) else {}
                if not llm_policy_enables_tool_surface(llm_policy_map):
                    continue
                allowed = normalise_tool_name_sequence(
                    llm_policy_map.get("allowed_tools")
                )
                if allowed:
                    allowed_tools.update(tool.lower() for tool in allowed)
                else:
                    has_unrestricted_llm_tool_step = True

    has_unrestricted_tool_pipeline_step = bool(unrestricted_tool_pipeline_actions)
    if has_unrestricted_llm_tool_step or has_unrestricted_tool_pipeline_step:
        unavailable_required_tools: list[str] = []
    else:
        available_lower = set(allowed_tools)
        available_lower.update(direct_action_tools)
        unavailable_required_tools = [
            tool for tool in required_tools if tool.lower() not in available_lower
        ]

    ok = not unavailable_required_tools
    return {
        "checked": True,
        "ok": ok,
        "required_tools": list(required_tools),
        "allowed_tools": sorted(allowed_tools),
        "direct_action_tools": sorted(direct_action_tools),
        "unrestricted_llm_tool_step": bool(has_unrestricted_llm_tool_step),
        "unrestricted_tool_pipeline_step": has_unrestricted_tool_pipeline_step,
        "unrestricted_tool_pipeline_actions": sorted(
            unrestricted_tool_pipeline_actions
        ),
        "unavailable_required_tools": list(unavailable_required_tools),
        "reason_code": ("ok" if ok else "workflow_required_effect_tool_not_allowed"),
    }


def workflow_result_effective_completed(workflow_result: Any) -> bool:
    if not bool(getattr(workflow_result, "completed", False)):
        return False
    if clean_workflow_summary_text(getattr(workflow_result, "error", None)):
        return False
    final_state = clean_workflow_summary_text(
        getattr(workflow_result, "final_state", None)
    )
    if workflow_final_state_is_failure_like(final_state):
        return False
    terminal_success_evaluation = workflow_terminal_success_evaluation_from_result(
        workflow_result
    )
    if isinstance(terminal_success_evaluation, Mapping):
        return bool(terminal_success_evaluation.get("success"))
    return True


def workflow_final_state_is_failure_like(final_state: str | None) -> bool:
    if not isinstance(final_state, str) or not final_state.strip():
        return False
    lowered = final_state.strip().lower()
    if lowered in {"failed", "failure", "error", "cancelled", "canceled"}:
        return True
    return lowered.endswith("_failed") or lowered.endswith("_error")


def workflow_terminal_success_evaluation_from_result(
    workflow_result: Any,
) -> Mapping[str, Any] | None:
    result_data = getattr(workflow_result, "data", None)
    if not isinstance(result_data, Mapping):
        return None
    evaluation = result_data.get("workflow_terminal_success_evaluation")
    if isinstance(evaluation, Mapping):
        return evaluation
    return None


def extract_workflow_step_failure_detail(envelope: Any) -> str | None:
    if not isinstance(envelope, Mapping):
        return None

    diagnostics = envelope.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        error_text = clean_workflow_summary_text(diagnostics.get("error"))
        if error_text:
            return error_text

    for key in ("error", "action_error"):
        error_text = clean_workflow_summary_text(envelope.get(key))
        if error_text:
            return error_text

    output_payload = envelope.get("output_payload")
    if isinstance(output_payload, Mapping):
        for payload_key in ("result", "mcp_result"):
            payload = output_payload.get(payload_key)
            if not isinstance(payload, Mapping):
                continue
            error_text = clean_workflow_summary_text(payload.get("error"))
            if error_text:
                return error_text
            message_text = clean_workflow_summary_text(payload.get("message"))
            if message_text:
                return message_text

    return None


def extract_explicit_workflow_failure_detail(workflow_result: Any) -> str | None:
    if workflow_result_effective_completed(workflow_result):
        return None

    result_data = getattr(workflow_result, "data", None)
    if not isinstance(result_data, Mapping):
        return None

    for key in (
        "last_action_error",
        "workflow_error",
        "failure_detail",
        "termination_detail",
        "error",
    ):
        error_text = clean_workflow_summary_text(result_data.get(key))
        if error_text:
            return normalise_workflow_failure_detail_for_user(error_text)

    step_error = extract_workflow_step_failure_detail(
        result_data.get(LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY)
    )
    if step_error:
        return normalise_workflow_failure_detail_for_user(step_error)

    step_envelopes = workflow_execution_summary_mapping_list(
        result_data.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY)
    )
    for envelope in reversed(step_envelopes):
        step_error = extract_workflow_step_failure_detail(envelope)
        if step_error:
            return normalise_workflow_failure_detail_for_user(step_error)

    result_envelope = result_data.get(WORKFLOW_RESULT_ENVELOPE_KEY)
    if isinstance(result_envelope, Mapping):
        diagnostics = result_envelope.get("diagnostics")
        if isinstance(diagnostics, Mapping):
            error_text = clean_workflow_summary_text(diagnostics.get("error"))
            if error_text:
                return normalise_workflow_failure_detail_for_user(error_text)

    return None


def normalise_workflow_failure_detail_for_user(error_text: Any) -> str | None:
    cleaned_error = clean_workflow_summary_text(error_text)
    if not cleaned_error:
        return None

    if "insufficient_quota" in cleaned_error.lower():
        return (
            "OpenAI quota exhausted (insufficient_quota). "
            "Please check provider billing or try again later."
        )

    return cleaned_error


def workflow_error_text_looks_like_machine_reason(error_text: Any) -> bool:
    cleaned = clean_workflow_summary_text(error_text)
    if not cleaned:
        return False
    if len(cleaned) > 120 or any(char.isspace() for char in cleaned):
        return False
    return bool(re.fullmatch(r"[#A-Za-z0-9_.:-]+", cleaned))


def extract_operational_workflow_error_signal(workflow_result: Any) -> str | None:
    error_text = clean_workflow_summary_text(getattr(workflow_result, "error", None))
    if error_text:
        return error_text

    result_data = getattr(workflow_result, "data", None)
    if not isinstance(result_data, Mapping):
        return None

    for key in ("last_action_error", "workflow_error", "error"):
        error_text = clean_workflow_summary_text(result_data.get(key))
        if error_text:
            return error_text

    step_error = extract_workflow_step_failure_detail(
        result_data.get(LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY)
    )
    if step_error:
        return step_error

    for envelope in workflow_execution_summary_mapping_list(
        result_data.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY)
    ):
        step_error = extract_workflow_step_failure_detail(envelope)
        if step_error:
            return step_error

    return None


def workflow_result_contains_tool_progress(workflow_result: Any) -> bool:
    result_data = getattr(workflow_result, "data", None)
    if not isinstance(result_data, Mapping):
        return False

    for key in ("tool_invocations", "invocations", "tool_messages"):
        if workflow_execution_summary_mapping_list(result_data.get(key)):
            return True

    extra_messages = workflow_execution_summary_mapping_list(
        result_data.get("extra_messages")
    )
    return any(
        clean_workflow_summary_text(message.get("role")) == "tool"
        for message in extra_messages
    )


def should_continue_failed_custom_workflow_into_tool_pipeline(
    workflow_result: Any,
) -> bool:
    if workflow_result_effective_completed(workflow_result):
        return False
    if workflow_result_contains_tool_progress(workflow_result):
        return False

    operational_error = extract_operational_workflow_error_signal(workflow_result)
    if operational_error and not workflow_error_text_looks_like_machine_reason(
        operational_error
    ):
        return False
    return True


def build_failed_custom_workflow_snapshot(
    *,
    workflow_id: str | None,
    workflow_result: Any,
) -> dict[str, Any] | None:
    cleaned_workflow_id = (
        workflow_id.strip()
        if isinstance(workflow_id, str) and workflow_id.strip()
        else None
    )
    if not cleaned_workflow_id or workflow_result_effective_completed(workflow_result):
        return None

    snapshot: dict[str, Any] = {
        "workflow_id": cleaned_workflow_id,
        "completed": False,
        "tool_progress_detected": workflow_result_contains_tool_progress(
            workflow_result
        ),
    }

    final_state = clean_workflow_summary_text(
        getattr(workflow_result, "final_state", None)
    )
    if final_state:
        snapshot["final_state"] = final_state

    operational_error = extract_operational_workflow_error_signal(workflow_result)
    if operational_error:
        snapshot["operational_error"] = operational_error

    explicit_failure_detail = extract_explicit_workflow_failure_detail(workflow_result)
    if explicit_failure_detail and explicit_failure_detail != operational_error:
        snapshot["failure_detail"] = explicit_failure_detail

    result_data = getattr(workflow_result, "data", None)
    if isinstance(result_data, Mapping):
        user_visible_failure_text = clean_workflow_summary_text(
            result_data.get("response_text")
        ) or clean_workflow_summary_text(result_data.get("summary"))
        if user_visible_failure_text:
            snapshot["user_visible_failure_text"] = user_visible_failure_text

    return snapshot


def workflow_result_tool_invocations(
    result: Any,
    *,
    required_tools: Sequence[str] | None = None,
) -> list[Mapping[str, Any]]:
    result_data = getattr(result, "data", None)
    if not isinstance(result_data, Mapping):
        return []

    invocations: list[Mapping[str, Any]] = []
    seen_signatures: set[str] = set()

    def _append_invocation(value: Mapping[str, Any]) -> None:
        signature = repr(sorted(value.items(), key=lambda item: str(item[0])))
        if signature in seen_signatures:
            return
        seen_signatures.add(signature)
        invocations.append(value)

    for key in ("tool_invocations", "invocations"):
        values = result_data.get(key)
        if not isinstance(values, Sequence) or isinstance(
            values, (str, bytes, bytearray)
        ):
            continue
        for value in values:
            if isinstance(value, Mapping):
                _append_invocation(value)

    for invocation in derive_tool_invocation_records_from_step_envelopes(
        result_data.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY),
        required_tools=required_tools,
    ):
        _append_invocation(invocation)
    return invocations


def required_tools_from_selected_workflow_mapping(
    candidate: Mapping[str, Any],
) -> tuple[list[str], str | None]:
    for key in ("required_prompt_tools", "missing_prompt_tools"):
        tools = normalise_tool_name_sequence(candidate.get(key))
        if tools:
            return tools, key

    tools = normalise_tool_name_sequence(
        candidate.get("workflow_required_effects_required_tools")
    )
    if tools:
        return tools, "workflow_required_effects_required_tools"

    for key in (
        "turn_expected_outcome_contract_state",
        "turn_expected_outcome_contract",
        "expected_outcome_contract_state",
        "expected_outcome_contract",
    ):
        contract = TurnExpectedOutcomeContract.from_mapping(candidate.get(key))
        tools = normalise_tool_name_sequence(contract.required_tools)
        if tools:
            return tools, key

    tools = workflow_required_tools_from_contract(
        candidate.get("workflow_required_effects_contract")
        if isinstance(candidate.get("workflow_required_effects_contract"), Mapping)
        else None
    )
    if tools:
        return tools, "workflow_required_effects_contract"

    return [], None


def required_tools_from_selected_workflow_result(
    result: Any,
) -> tuple[list[str], str | None]:
    result_data = getattr(result, "data", None)
    if not isinstance(result_data, Mapping):
        return [], None

    for candidate in (
        result_data,
        result_data.get("completion_report"),
        result_data.get("workflow_execution_summary"),
        result_data.get("selected_workflow_trace"),
    ):
        if not isinstance(candidate, Mapping):
            continue
        tools, source = required_tools_from_selected_workflow_mapping(candidate)
        if tools:
            return tools, source
    return [], None


def filter_workflow_internal_required_effect_tools(
    *,
    required_tools: Sequence[str],
    selected_workflow_definition: Any | None,
) -> tuple[list[str], dict[str, Any]]:
    tools = normalise_tool_name_sequence(required_tools)
    policy = evaluate_workflow_required_effects_tool_policy(
        selected_workflow_definition
    )
    direct_action_tools = {
        str(item).strip().lower()
        for item in (policy.get("direct_action_tools") or [])
        if isinstance(item, str) and item.strip()
    }
    if not tools or not direct_action_tools:
        return tools, {
            "policy": dict(policy),
            "workflow_internal_action_tools": sorted(direct_action_tools),
            "filtered_workflow_internal_action_tools": [],
        }

    filtered_tools: list[str] = []
    filtered_internal_tools: list[str] = []
    for tool in tools:
        if tool.strip().lower() in direct_action_tools:
            filtered_internal_tools.append(tool)
            continue
        filtered_tools.append(tool)
    return filtered_tools, {
        "policy": dict(policy),
        "workflow_internal_action_tools": sorted(direct_action_tools),
        "filtered_workflow_internal_action_tools": filtered_internal_tools,
    }


def evaluate_selected_workflow_missing_required_tools(
    *,
    workflow_result: Any,
    selected_workflow_definition: Any | None,
    routing_required_tools: Sequence[str],
    routing_required_fetch_concept_ids: Sequence[str],
    routing_required_read_file_copy_ids: Sequence[str],
    routing_required_scholarly_representation_file_copy_ids: Sequence[str],
    derive_missing_prompt_requirements: MissingPromptRequirementsDeriver,
) -> SelectedWorkflowMissingToolEvaluation:
    result_data = getattr(workflow_result, "data", None)
    if isinstance(result_data, Mapping):
        for candidate in (
            result_data,
            result_data.get("completion_report"),
            result_data.get("workflow_execution_summary"),
            result_data.get("selected_workflow_trace"),
        ):
            if not isinstance(candidate, Mapping):
                continue
            explicit_missing = normalise_tool_name_sequence(
                candidate.get("missing_prompt_tools")
            )
            if explicit_missing:
                return SelectedWorkflowMissingToolEvaluation(
                    missing_required_tools=tuple(explicit_missing),
                    required_tools=tuple(explicit_missing),
                    required_tools_source="missing_prompt_tools",
                    evidence_classification={
                        "required_tools_source": "missing_prompt_tools",
                        "prompt_required_tools": [],
                        "expected_outcome_contract_tools": [],
                        "workflow_required_effects_tools": [],
                        "workflow_internal_action_tools": [],
                        "filtered_workflow_internal_action_tools": [],
                        "missing_required_tools": list(explicit_missing),
                    },
                )

    required_tools = normalise_tool_name_sequence(routing_required_tools)
    required_tools_source: str | None = (
        "routing_prompt_requirements" if required_tools else None
    )
    if not required_tools:
        required_tools, required_tools_source = (
            required_tools_from_selected_workflow_result(workflow_result)
        )

    policy_classification: dict[str, Any] = {
        "policy": {},
        "workflow_internal_action_tools": [],
        "filtered_workflow_internal_action_tools": [],
    }
    if required_tools_source in {
        "workflow_required_effects_required_tools",
        "workflow_required_effects_contract",
    }:
        required_tools, policy_classification = (
            filter_workflow_internal_required_effect_tools(
                required_tools=required_tools,
                selected_workflow_definition=selected_workflow_definition,
            )
        )

    source_classification = _classify_required_tools_source(
        required_tools=required_tools,
        required_tools_source=required_tools_source,
        policy_classification=policy_classification,
    )
    if not required_tools:
        return SelectedWorkflowMissingToolEvaluation(
            missing_required_tools=(),
            required_tools=(),
            required_tools_source=required_tools_source,
            evidence_classification=source_classification,
        )

    missing_tools, _missing_fetch_ids, _missing_read_ids, _missing_scholarly_ids = (
        derive_missing_prompt_requirements(
            required_tools=required_tools,
            required_fetch_concept_ids=routing_required_fetch_concept_ids,
            required_read_file_copy_ids=routing_required_read_file_copy_ids,
            required_scholarly_representation_for_file_copy_ids=(
                routing_required_scholarly_representation_file_copy_ids
            ),
            tool_invocations=workflow_result_tool_invocations(
                workflow_result,
                required_tools=required_tools,
            ),
        )
    )
    missing_required_tools = normalise_tool_name_sequence(missing_tools)
    return SelectedWorkflowMissingToolEvaluation(
        missing_required_tools=tuple(missing_required_tools),
        required_tools=tuple(required_tools),
        required_tools_source=required_tools_source,
        evidence_classification={
            **source_classification,
            "missing_required_tools": list(missing_required_tools),
        },
    )


def _classify_required_tools_source(
    *,
    required_tools: Sequence[str],
    required_tools_source: str | None,
    policy_classification: Mapping[str, Any],
) -> dict[str, Any]:
    prompt_sources = {
        "routing_prompt_requirements",
        "required_prompt_tools",
        "missing_prompt_tools",
    }
    expected_contract_sources = {
        "turn_expected_outcome_contract_state",
        "turn_expected_outcome_contract",
        "expected_outcome_contract_state",
        "expected_outcome_contract",
    }
    workflow_effect_sources = {
        "workflow_required_effects_required_tools",
        "workflow_required_effects_contract",
    }
    source = required_tools_source or "none"
    tools = list(required_tools)
    raw_policy = policy_classification.get("policy")
    policy_payload = (
        {str(key): value for key, value in raw_policy.items() if isinstance(key, str)}
        if isinstance(raw_policy, Mapping)
        else {}
    )
    return {
        "required_tools_source": source,
        "prompt_required_tools": tools if source in prompt_sources else [],
        "expected_outcome_contract_tools": (
            tools if source in expected_contract_sources else []
        ),
        "workflow_required_effects_tools": (
            tools if source in workflow_effect_sources else []
        ),
        "workflow_internal_action_tools": list(
            policy_classification.get("workflow_internal_action_tools") or []
        ),
        "filtered_workflow_internal_action_tools": list(
            policy_classification.get("filtered_workflow_internal_action_tools") or []
        ),
        "required_effects_tool_policy": policy_payload,
        "missing_required_tools": [],
    }


def build_custom_workflow_missing_required_tools_snapshot(
    *,
    workflow_id: str | None,
    workflow_result: Any,
    missing_required_tools: Sequence[str],
) -> dict[str, Any] | None:
    cleaned_workflow_id = (
        workflow_id.strip()
        if isinstance(workflow_id, str) and workflow_id.strip()
        else None
    )
    missing_tools = normalise_tool_name_sequence(missing_required_tools)
    if not cleaned_workflow_id or not missing_tools:
        return None

    snapshot: dict[str, Any] = {
        "workflow_id": cleaned_workflow_id,
        "completed": bool(getattr(workflow_result, "completed", False)),
        "tool_progress_detected": workflow_result_contains_tool_progress(
            workflow_result
        ),
        "missing_required_tools": list(missing_tools),
        "recovery_reason": "custom_workflow_missing_required_prompt_tools",
    }
    final_state = clean_workflow_summary_text(
        getattr(workflow_result, "final_state", None)
    )
    if final_state:
        snapshot["final_state"] = final_state
    result_data = getattr(workflow_result, "data", None)
    if isinstance(result_data, Mapping):
        user_visible_text = clean_workflow_summary_text(
            result_data.get("response_text")
        ) or clean_workflow_summary_text(result_data.get("summary"))
        if user_visible_text:
            snapshot["user_visible_response_text"] = user_visible_text[:800]
    return snapshot


def evaluate_selected_workflow_handoff(
    *,
    selected_workflow_id: str | None,
    workflow_result: Any,
    selected_workflow_definition: Any | None,
    routing_required_tools: Sequence[str],
    routing_required_fetch_concept_ids: Sequence[str],
    routing_required_read_file_copy_ids: Sequence[str],
    routing_required_scholarly_representation_file_copy_ids: Sequence[str],
    fallback_tool_workflow_id: str | None,
    derive_missing_prompt_requirements: MissingPromptRequirementsDeriver,
) -> SelectedWorkflowHandoffDecision:
    missing_evaluation = evaluate_selected_workflow_missing_required_tools(
        workflow_result=workflow_result,
        selected_workflow_definition=selected_workflow_definition,
        routing_required_tools=routing_required_tools,
        routing_required_fetch_concept_ids=routing_required_fetch_concept_ids,
        routing_required_read_file_copy_ids=routing_required_read_file_copy_ids,
        routing_required_scholarly_representation_file_copy_ids=(
            routing_required_scholarly_representation_file_copy_ids
        ),
        derive_missing_prompt_requirements=derive_missing_prompt_requirements,
    )
    missing_required_tools = tuple(missing_evaluation.missing_required_tools)

    reason: str | None = None
    if should_continue_failed_custom_workflow_into_tool_pipeline(workflow_result):
        reason = "failed_custom_workflow_before_tool_progress"
    elif missing_required_tools:
        reason = "custom_workflow_missing_required_prompt_tools"

    cleaned_fallback_workflow_id = (
        fallback_tool_workflow_id.strip()
        if isinstance(fallback_tool_workflow_id, str)
        and fallback_tool_workflow_id.strip()
        else None
    )
    continue_to_tool_pipeline = bool(reason and cleaned_fallback_workflow_id)
    if not continue_to_tool_pipeline:
        return SelectedWorkflowHandoffDecision(
            continue_to_tool_pipeline=False,
            reason=reason,
            fallback_tool_workflow_id=cleaned_fallback_workflow_id,
            missing_required_tools=missing_required_tools,
            required_tools_for_tool_pipeline=(),
            selected_workflow_snapshot=None,
            custom_failure_extra={},
            recovery_handoff_payload=None,
            evidence_classification=missing_evaluation.evidence_classification,
        )

    if reason == "failed_custom_workflow_before_tool_progress":
        selected_workflow_snapshot = build_failed_custom_workflow_snapshot(
            workflow_id=selected_workflow_id,
            workflow_result=workflow_result,
        )
    else:
        selected_workflow_snapshot = (
            build_custom_workflow_missing_required_tools_snapshot(
                workflow_id=selected_workflow_id,
                workflow_result=workflow_result,
                missing_required_tools=missing_required_tools,
            )
        )

    custom_failure_extra: dict[str, Any] = {
        "continued_to_tool_pipeline": True,
        "fallback_tool_workflow_id": cleaned_fallback_workflow_id,
        "handoff_reason": reason,
        "evidence_classification": dict(missing_evaluation.evidence_classification),
    }
    if missing_required_tools:
        custom_failure_extra["missing_required_tools"] = list(missing_required_tools)

    recovery_handoff_payload: dict[str, Any] = {
        "type": "workflow_recovery_handoff",
        "from_execution_mode": "custom_workflow",
        "to_execution_mode": "tool_pipeline",
        "selected_workflow_id": selected_workflow_id,
        "reason": reason,
        "evidence_classification": dict(missing_evaluation.evidence_classification),
    }
    if isinstance(selected_workflow_snapshot, Mapping):
        recovery_handoff_payload["selected_workflow_snapshot"] = dict(
            selected_workflow_snapshot
        )
        if reason == "failed_custom_workflow_before_tool_progress":
            recovery_handoff_payload["failed_workflow_snapshot"] = dict(
                selected_workflow_snapshot
            )
    if missing_required_tools:
        recovery_handoff_payload["missing_required_tools"] = list(
            missing_required_tools
        )

    return SelectedWorkflowHandoffDecision(
        continue_to_tool_pipeline=True,
        reason=reason,
        fallback_tool_workflow_id=cleaned_fallback_workflow_id,
        missing_required_tools=missing_required_tools,
        required_tools_for_tool_pipeline=missing_required_tools,
        selected_workflow_snapshot=selected_workflow_snapshot,
        custom_failure_extra=custom_failure_extra,
        recovery_handoff_payload=recovery_handoff_payload,
        evidence_classification=missing_evaluation.evidence_classification,
    )
