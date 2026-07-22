"""Reusable helpers for declarative workflow authoring.

The goal of this module is to keep workflow authoring policy in VWL-visible
data rather than hiding graph-shaping logic inside one-off handler code.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from .engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
    build_transition_condition,
)
from .subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE,
    build_subworkflow_contract,
)
from .static_input_binding_utils import coerce_static_input_binding

_TRANSIENT_WORKFLOW_EXECUTION_INPUT_SUFFIX_REASON_CODES: tuple[
    tuple[str, str],
    ...,
] = (
    ("request_text", "transient_request_text_default_persisted"),
    ("recent_turns_json", "transient_recent_turns_default_persisted"),
    ("base_response_text", "transient_base_response_default_persisted"),
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _transient_workflow_execution_input_reason_code(key: Any) -> str | None:
    key_text = _clean_text(key).casefold()
    if not key_text:
        return None
    for suffix, reason_code in _TRANSIENT_WORKFLOW_EXECUTION_INPUT_SUFFIX_REASON_CODES:
        if key_text == suffix or key_text.endswith(f"_{suffix}"):
            return reason_code
    return None


def _build_context_input_mapping_spec(
    *,
    tool_param: Any,
    raw_value: Any,
) -> dict[str, Any] | None:
    tool_param_text = _clean_text(tool_param)
    if not tool_param_text or not isinstance(raw_value, Mapping):
        return None

    context_key = _clean_text(
        raw_value.get("$context_key") or raw_value.get("context_key")
    )
    if not context_key:
        return None

    spec: dict[str, Any] = {
        "tool_param": tool_param_text,
        "context_key": context_key,
    }
    mapping_concept_id = _clean_text(
        raw_value.get("$mapping_concept_id") or raw_value.get("mapping_concept_id")
    )
    if mapping_concept_id:
        spec["mapping_concept_id"] = mapping_concept_id
    if "$required" in raw_value or "required" in raw_value:
        spec["required"] = bool(raw_value.get("$required", raw_value.get("required", True)))
    return spec


def _split_action_inputs_for_authoring(
    *,
    inputs: Mapping[str, Any],
    invoked_workflow_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    static_input_bindings: list[dict[str, Any]] = []
    context_input_mappings: list[dict[str, Any]] = []

    for tool_param, raw_value in inputs.items():
        tool_param_text = _clean_text(tool_param)
        if not tool_param_text:
            continue
        if tool_param_text == "workflow_id" and _clean_text(invoked_workflow_id):
            continue

        context_mapping = _build_context_input_mapping_spec(
            tool_param=tool_param_text,
            raw_value=raw_value,
        )
        if context_mapping is not None:
            context_input_mappings.append(context_mapping)
            continue

        binding = coerce_static_input_binding(tool_param_text, raw_value)
        if binding is None:
            continue
        static_input_bindings.append(
            {
                "tool_param": binding[0],
                "value": copy.deepcopy(binding[1]),
            }
        )

    return static_input_bindings, context_input_mappings


def _normalise_static_input_bindings_payload(
    raw_payload: Any,
) -> list[tuple[str, Any]]:
    bindings: list[tuple[str, Any]] = []
    if isinstance(raw_payload, Mapping):
        iterable = raw_payload.items()
    elif isinstance(raw_payload, Sequence) and not isinstance(raw_payload, str):
        iterable = raw_payload
    else:
        iterable = ()

    for item in iterable:
        if isinstance(item, Mapping):
            tool_param = _clean_text(item.get("tool_param") or item.get("key"))
            value = item.get("value")
        elif (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
            and len(item) == 2
        ):
            tool_param = _clean_text(item[0])
            value = item[1]
        else:
            continue
        binding = coerce_static_input_binding(tool_param, value)
        if binding is not None:
            bindings.append(binding)
    return bindings


def _normalise_context_input_mappings_payload(
    raw_payload: Any,
) -> list[dict[str, Any]]:
    if not isinstance(raw_payload, Sequence) or isinstance(
        raw_payload,
        (str, bytes, bytearray),
    ):
        return []

    mappings: list[dict[str, Any]] = []
    for item in raw_payload:
        if not isinstance(item, Mapping):
            continue
        tool_param = _clean_text(item.get("tool_param"))
        context_key = _clean_text(item.get("context_key"))
        if not tool_param or not context_key:
            continue
        mapping: dict[str, Any] = {
            "tool_param": tool_param,
            "context_key": context_key,
        }
        mapping_concept_id = _clean_text(
            item.get("mapping_concept_id") or item.get("$mapping_concept_id")
        )
        if mapping_concept_id:
            mapping["mapping_concept_id"] = mapping_concept_id
        if "required" in item or "$required" in item:
            mapping["required"] = bool(item.get("required", item.get("$required", True)))
        mappings.append(mapping)
    return mappings


def _build_action_inputs_from_authoring_row(
    row: Mapping[str, Any],
    *,
    subworkflow_id: str | None = None,
) -> dict[str, Any]:
    inputs_raw = row.get("inputs")
    inputs = (
        {
            str(key): copy.deepcopy(value)
            for key, value in inputs_raw.items()
            if isinstance(key, str) and str(key).strip()
        }
        if isinstance(inputs_raw, Mapping)
        else {}
    )

    for tool_param, value in _normalise_static_input_bindings_payload(
        row.get("static_input_bindings")
    ):
        inputs[tool_param] = copy.deepcopy(value)

    for mapping in _normalise_context_input_mappings_payload(
        row.get("context_input_mappings")
    ):
        mapping_value: dict[str, Any] = {"$context_key": mapping["context_key"]}
        mapping_concept_id = _clean_text(mapping.get("mapping_concept_id"))
        if mapping_concept_id:
            mapping_value["$mapping_concept_id"] = mapping_concept_id
        if "required" in mapping:
            mapping_value["$required"] = bool(mapping["required"])
        inputs[mapping["tool_param"]] = mapping_value

    subworkflow_id_text = _clean_text(subworkflow_id)
    if subworkflow_id_text:
        inputs["workflow_id"] = subworkflow_id_text
    return inputs


def _build_subworkflow_contract_from_authoring_row(
    *,
    row: Mapping[str, Any],
    action: WorkflowActionInvocation,
) -> dict[str, Any] | None:
    """Build preview-time subworkflow metadata from the authored edge.

    Publication builds the same contract before persisting a workflow.  The
    authoring preview must materialise it too, otherwise a valid explicit
    ``subworkflow_id`` is rejected as contractless before publication can run.
    """

    workflow_id = _clean_text(action.subworkflow_id)
    if not workflow_id:
        return None

    input_mappings: list[dict[str, str]] = []
    static_input_keys: list[str] = []
    inputs = action.inputs if isinstance(action.inputs, Mapping) else {}
    for child_input_key, raw_value in inputs.items():
        child_input_key_text = _clean_text(child_input_key)
        if not child_input_key_text or child_input_key_text in {
            "workflow_id",
            "failure_mode",
            "__failure_mode",
            "max_transitions",
        }:
            continue
        if isinstance(raw_value, Mapping):
            parent_context_key = _clean_text(raw_value.get("$context_key"))
            if not parent_context_key:
                continue
            mapping = {
                "child_input_key": child_input_key_text,
                "parent_context_key": parent_context_key,
            }
            mapping_concept_id = _clean_text(
                raw_value.get("$mapping_concept_id")
            )
            if mapping_concept_id:
                mapping["mapping_concept_id"] = mapping_concept_id
            input_mappings.append(mapping)
            continue
        static_input_keys.append(child_input_key_text)

    output_mappings: list[dict[str, str]] = []
    raw_output_mappings = row.get("tool_output_context_mappings")
    if isinstance(raw_output_mappings, Sequence) and not isinstance(
        raw_output_mappings, (str, bytes, bytearray)
    ):
        for raw_mapping in raw_output_mappings:
            if not isinstance(raw_mapping, Mapping):
                continue
            tool_output_field = _clean_text(raw_mapping.get("tool_output_field"))
            child_output_field = (
                tool_output_field[len("result.") :]
                if tool_output_field.startswith("result.")
                else tool_output_field
            )
            parent_context_key = _clean_text(raw_mapping.get("context_key"))
            if not child_output_field or not parent_context_key:
                continue
            mapping = {
                "child_output_field": child_output_field,
                "parent_context_key": parent_context_key,
            }
            mapping_concept_id = _clean_text(
                raw_mapping.get("mapping_concept_id")
            )
            if mapping_concept_id:
                mapping["mapping_concept_id"] = mapping_concept_id
            output_mappings.append(mapping)

    failure_mode = _clean_text(
        inputs.get("failure_mode") or inputs.get("__failure_mode")
    )
    return build_subworkflow_contract(
        workflow_id=workflow_id,
        input_mappings=input_mappings,
        output_mappings=output_mappings,
        static_input_keys=list(dict.fromkeys(static_input_keys)),
        failure_mode=(
            failure_mode or WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE
        ),
    )


def strip_transient_execution_defaults_from_authoring_spec(
    authoring_spec: Mapping[str, Any],
) -> dict[str, Any]:
    cleaned_spec = copy.deepcopy(dict(authoring_spec))
    steps = cleaned_spec.get("steps")
    if not isinstance(steps, list):
        return cleaned_spec

    for step in steps:
        if not isinstance(step, dict):
            continue

        inputs = step.get("inputs")
        if isinstance(inputs, Mapping):
            step["inputs"] = {
                str(key): copy.deepcopy(value)
                for key, value in inputs.items()
                if isinstance(key, str)
                and str(key).strip()
                and _transient_workflow_execution_input_reason_code(key) is None
            }

        static_input_bindings = step.get("static_input_bindings")
        if isinstance(static_input_bindings, Mapping):
            step["static_input_bindings"] = {
                str(key): copy.deepcopy(value)
                for key, value in static_input_bindings.items()
                if isinstance(key, str)
                and str(key).strip()
                and _transient_workflow_execution_input_reason_code(key) is None
            }
        elif isinstance(static_input_bindings, Sequence) and not isinstance(
            static_input_bindings,
            (str, bytes, bytearray),
        ):
            cleaned_bindings: list[Any] = []
            for item in static_input_bindings:
                if not isinstance(item, Mapping):
                    continue
                tool_param = _clean_text(item.get("tool_param") or item.get("key"))
                if (
                    not tool_param
                    or _transient_workflow_execution_input_reason_code(tool_param) is not None
                ):
                    continue
                cleaned_bindings.append(copy.deepcopy(dict(item)))
            step["static_input_bindings"] = cleaned_bindings

    return cleaned_spec


def collect_transient_execution_input_issues(
    definition: WorkflowDefinition,
) -> list[dict[str, Any]]:
    states = definition.states if isinstance(definition.states, Mapping) else {}
    issues: list[dict[str, Any]] = []

    for state_id, state_spec in states.items():
        if not isinstance(state_spec, WorkflowStateSpec):
            continue
        actions = tuple(getattr(state_spec, "actions", ()) or ())
        for action in actions:
            if not isinstance(action, WorkflowActionInvocation):
                continue
            action_id = _clean_text(getattr(action, "action_id", ""))
            subworkflow_id = _clean_text(getattr(action, "subworkflow_id", ""))
            inputs = getattr(action, "inputs", None)
            if not isinstance(inputs, Mapping):
                continue
            for tool_param, raw_value in inputs.items():
                tool_param_text = _clean_text(tool_param)
                reason_code = _transient_workflow_execution_input_reason_code(
                    tool_param_text
                )
                if not reason_code:
                    continue
                if tool_param_text == "workflow_id" and subworkflow_id:
                    continue
                if _build_context_input_mapping_spec(
                    tool_param=tool_param_text,
                    raw_value=raw_value,
                ) is not None:
                    continue
                issues.append(
                    {
                        "state_id": str(state_id),
                        "action_id": action_id,
                        "tool_param": tool_param_text,
                        "reason_code": reason_code,
                    }
                )

    return sorted(
        issues,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("action_id") or ""),
            str(item.get("tool_param") or ""),
            str(item.get("reason_code") or ""),
        ),
    )


def _build_transition(
    *,
    to_state: str,
    reason: str,
    condition_spec: Mapping[str, Any],
) -> WorkflowTransitionSpec:
    normalised_spec, compiled_condition = build_transition_condition(condition_spec)
    return WorkflowTransitionSpec(
        to_state=to_state,
        condition=compiled_condition,
        condition_spec=normalised_spec,
        reason=reason,
    )


def _build_transitions_from_row(row: Mapping[str, Any]) -> tuple[WorkflowTransitionSpec, ...]:
    transitions: list[WorkflowTransitionSpec] = []

    def _append_if_present(
        target_keys: str | tuple[str, ...],
        reason: str,
        condition_spec: Mapping[str, Any],
    ) -> None:
        keys = (target_keys,) if isinstance(target_keys, str) else target_keys
        target = ""
        for key in keys:
            target = _clean_text(row.get(key))
            if target:
                break
        if not target:
            return
        transitions.append(
            _build_transition(
                to_state=target,
                reason=reason,
                condition_spec=condition_spec,
            )
        )

    conditional_transitions = row.get("conditional_transitions")
    if isinstance(conditional_transitions, list):
        for index, item in enumerate(conditional_transitions):
            if not isinstance(item, Mapping):
                continue
            target = _clean_text(
                item.get("to_state")
                or item.get("to_state_key")
                or item.get("to")
                or item.get("next")
            )
            if not target:
                continue
            reason = _clean_text(item.get("reason")) or f"condition_{index + 1}"
            condition_spec = item.get("condition_spec") or item.get("condition")
            if not isinstance(condition_spec, Mapping):
                condition_spec = {"kind": "always"}
            transitions.append(
                _build_transition(
                    to_state=target,
                    reason=reason,
                    condition_spec=condition_spec,
            )
        )

    _append_if_present(
        ("on_failure_state_key", "on_failure_state"),
        "on_failure",
        {"kind": "context_flag", "key": "last_action_failed", "expected": True},
    )
    _append_if_present(
        ("on_unknown_state_key", "on_unknown_state"),
        "on_unknown",
        {"kind": "context_flag", "key": "last_action_unknown", "expected": True},
    )
    _append_if_present(
        ("on_approval_required_state_key", "on_approval_required_state"),
        "on_approval_required",
        {"kind": "context_flag", "key": "approval_required", "expected": True},
    )
    _append_if_present(
        ("on_break_state_key", "on_break_state"),
        "on_break",
        {"kind": "control_signal", "signal": "break"},
    )
    _append_if_present(
        ("on_continue_state_key", "on_continue_state"),
        "on_continue",
        {"kind": "control_signal", "signal": "continue"},
    )
    _append_if_present(
        ("on_true_state_key", "on_true_state"),
        "on_true",
        {"kind": "transition_result_truth", "expected": True},
    )
    _append_if_present(
        ("on_false_state_key", "on_false_state"),
        "on_false",
        {"kind": "transition_result_truth", "expected": False},
    )
    _append_if_present(("next_state_key", "next_state"), "next_step", {"kind": "always"})

    return tuple(transitions)


def _build_action_from_row(row: Mapping[str, Any]) -> WorkflowActionInvocation | None:
    action_id = _clean_text(row.get("action_id")) or None
    subworkflow_id = _clean_text(row.get("subworkflow_id")) or None
    execution_mode = _clean_text(row.get("execution_mode")) or "deterministic"
    if subworkflow_id and not action_id:
        action_id = WORKFLOW_SUBWORKFLOW_ACTION_ID
        execution_mode = "subworkflow"
    if not action_id and not subworkflow_id:
        return None

    inputs = _build_action_inputs_from_authoring_row(
        row,
        subworkflow_id=subworkflow_id,
    )
    prompt_contract_raw = row.get("prompt_contract")
    prompt_contract = (
        dict(prompt_contract_raw) if isinstance(prompt_contract_raw, Mapping) else None
    )
    llm_policy_raw = row.get("llm_policy")
    llm_policy = dict(llm_policy_raw) if isinstance(llm_policy_raw, Mapping) else None
    validation_policy_raw = row.get("validation_policy")
    validation_policy = (
        dict(validation_policy_raw)
        if isinstance(validation_policy_raw, Mapping)
        else None
    )
    contract_concept_id = _clean_text(row.get("action_concept_id")) or None

    return WorkflowActionInvocation(
        action_id=action_id,
        inputs=inputs,
        contract_concept_id=contract_concept_id,
        execution_mode=execution_mode,
        prompt_contract=prompt_contract,
        llm_policy=llm_policy,
        validation_policy=validation_policy,
        subworkflow_id=subworkflow_id,
    )


def _serialise_transition_row(
    *,
    state_row: dict[str, Any],
    transition: WorkflowTransitionSpec,
) -> None:
    reason = _clean_text(getattr(transition, "reason", "")).lower()
    condition_spec = (
        dict(transition.condition_spec)
        if isinstance(transition.condition_spec, Mapping)
        else {"kind": "always"}
    )
    target_state = _clean_text(getattr(transition, "to_state", ""))
    if not target_state:
        return

    standard_reason_keys = {
        "next_step": "next_state_key",
        "on_true": "on_true_state_key",
        "on_false": "on_false_state_key",
        "on_failure": "on_failure_state_key",
        "on_unknown": "on_unknown_state_key",
        "on_approval_required": "on_approval_required_state_key",
        "on_break": "on_break_state_key",
        "on_continue": "on_continue_state_key",
    }
    target_key = standard_reason_keys.get(reason)
    if target_key is not None:
        state_row[target_key] = target_state
        return

    conditional_transitions = state_row.setdefault("conditional_transitions", [])
    if not isinstance(conditional_transitions, list):
        conditional_transitions = []
        state_row["conditional_transitions"] = conditional_transitions
    conditional_transitions.append(
        {
            "to_state": target_state,
            "reason": reason or "condition",
            "condition_spec": condition_spec,
        }
    )


def serialise_workflow_definition_to_authoring_spec(
    definition: WorkflowDefinition,
) -> dict[str, Any]:
    """Render a runtime definition into a declarative authoring spec.

    This reverse mapping keeps repair/edit workflows generic: they can inspect
    an existing workflow definition, transform the declarative spec, and then
    hand the result back to the shared materialisation pathway.
    """

    workflow_id = _clean_text(getattr(definition, "workflow_id", ""))
    if not workflow_id:
        raise ValueError("workflow_definition_missing_workflow_id")

    states = definition.states if isinstance(definition.states, Mapping) else {}
    if not states:
        raise ValueError(f"workflow_definition_missing_states:{workflow_id}")

    step_rows: list[dict[str, Any]] = []
    for state_id, state_spec in states.items():
        if not isinstance(state_spec, WorkflowStateSpec):
            continue
        row: dict[str, Any] = {
            "state_id": str(state_id),
            "terminal": bool(state_spec.terminal),
        }

        action = state_spec.actions[0] if state_spec.actions else None
        if isinstance(action, WorkflowActionInvocation):
            action_id = _clean_text(action.action_id)
            if action_id:
                row["action_id"] = action_id
            subworkflow_id = _clean_text(action.subworkflow_id)
            if (
                not subworkflow_id
                and action_id == WORKFLOW_SUBWORKFLOW_ACTION_ID
                and isinstance(action.inputs, Mapping)
            ):
                subworkflow_id = _clean_text(action.inputs.get("workflow_id"))
            if subworkflow_id:
                row["subworkflow_id"] = subworkflow_id
            execution_mode = _clean_text(action.execution_mode)
            if execution_mode:
                row["execution_mode"] = execution_mode
            if isinstance(action.inputs, Mapping) and action.inputs:
                static_input_bindings, context_input_mappings = (
                    _split_action_inputs_for_authoring(
                        inputs=action.inputs,
                        invoked_workflow_id=subworkflow_id or None,
                    )
                )
                if static_input_bindings:
                    row["static_input_bindings"] = static_input_bindings
                if context_input_mappings:
                    row["context_input_mappings"] = context_input_mappings
            if isinstance(action.prompt_contract, Mapping) and action.prompt_contract:
                row["prompt_contract"] = dict(action.prompt_contract)
            if isinstance(action.llm_policy, Mapping) and action.llm_policy:
                row["llm_policy"] = dict(action.llm_policy)
            if isinstance(action.validation_policy, Mapping) and action.validation_policy:
                row["validation_policy"] = dict(action.validation_policy)
            contract_concept_id = _clean_text(action.contract_concept_id)
            if contract_concept_id:
                row["action_concept_id"] = contract_concept_id

        metadata = (
            dict(state_spec.metadata)
            if isinstance(state_spec.metadata, Mapping) and state_spec.metadata
            else {}
        )
        step_concept_id = _clean_text(metadata.get("workflow_step_concept_id"))
        if step_concept_id:
            row["concept_id"] = step_concept_id
        writes_context_keys = metadata.get("writes_context_keys")
        if isinstance(writes_context_keys, list) and writes_context_keys:
            row["writes_context_keys"] = [
                str(item).strip()
                for item in writes_context_keys
                if isinstance(item, str) and str(item).strip()
            ]
        tool_output_mappings = metadata.get("tool_output_context_mappings")
        if isinstance(tool_output_mappings, list) and tool_output_mappings:
            row["tool_output_context_mappings"] = [
                dict(item) for item in tool_output_mappings if isinstance(item, Mapping)
            ]
        mutation_authority = metadata.get("mutation_authority")
        if isinstance(mutation_authority, Mapping) and mutation_authority:
            row["mutation_authority"] = dict(mutation_authority)
        remaining_metadata = {
            str(key): value
            for key, value in metadata.items()
            if str(key)
            not in {
                "workflow_step_concept_id",
                "writes_context_keys",
                "tool_output_context_mappings",
                "mutation_authority",
                "transition_condition_specs",
            }
        }
        if remaining_metadata:
            row["metadata"] = remaining_metadata

        for transition in state_spec.transitions:
            if isinstance(transition, WorkflowTransitionSpec):
                _serialise_transition_row(state_row=row, transition=transition)

        step_rows.append(row)

    definition_metadata = (
        dict(definition.metadata)
        if isinstance(definition.metadata, Mapping) and definition.metadata
        else {}
    )
    authoring_spec: dict[str, Any] = {
        "workflow_id": workflow_id,
        "initial_state_key": _clean_text(definition.initial_state),
        "steps": step_rows,
    }

    purpose = _clean_text(getattr(definition, "purpose", ""))
    if purpose:
        authoring_spec["description"] = purpose
    required_effects = definition_metadata.get("required_effects")
    if isinstance(required_effects, list) and required_effects:
        authoring_spec["required_effects"] = [
            str(item).strip()
            for item in required_effects
            if isinstance(item, str) and str(item).strip()
        ]
    postcondition_probe = definition_metadata.get("postcondition_probe")
    if isinstance(postcondition_probe, Mapping) and postcondition_probe:
        authoring_spec["postcondition_probe"] = dict(postcondition_probe)
    verification_inputs = definition_metadata.get("verification_inputs")
    if isinstance(verification_inputs, Mapping) and verification_inputs:
        authoring_spec["verification_inputs"] = dict(verification_inputs)
    remaining_workflow_metadata = {
        str(key): value
        for key, value in definition_metadata.items()
        if str(key)
        not in {
            "required_effects",
            "postcondition_probe",
            "verification_inputs",
        }
    }
    if remaining_workflow_metadata:
        authoring_spec["workflow_metadata"] = remaining_workflow_metadata

    return authoring_spec


def build_workflow_definition_from_authoring_spec(
    spec: Mapping[str, Any],
) -> WorkflowDefinition:
    """Build a runtime definition from a normalised declarative authoring spec."""

    workflow_id = _clean_text(spec.get("workflow_id"))
    if not workflow_id:
        raise ValueError("workflow_authoring_spec_missing_workflow_id")

    raw_steps = spec.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("workflow_authoring_spec_missing_steps")

    states: dict[str, WorkflowStateSpec] = {}
    termination_states: list[str] = []
    seen_state_ids: set[str] = set()

    for index, raw_row in enumerate(raw_steps):
        if not isinstance(raw_row, Mapping):
            continue
        state_id = _clean_text(raw_row.get("state_key") or raw_row.get("state_id"))
        if not state_id:
            raise ValueError(f"workflow_authoring_spec_missing_state_id:{index}")
        if state_id in seen_state_ids:
            raise ValueError(f"workflow_authoring_spec_duplicate_state_id:{state_id}")
        seen_state_ids.add(state_id)

        action = _build_action_from_row(raw_row)
        transitions = _build_transitions_from_row(raw_row)
        terminal = bool(raw_row.get("terminal")) or not transitions

        metadata_raw = raw_row.get("metadata")
        metadata = dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
        step_concept_id = _clean_text(raw_row.get("concept_id"))
        if step_concept_id:
            metadata["workflow_step_concept_id"] = step_concept_id
        writes_context_keys = raw_row.get("writes_context_keys")
        if isinstance(writes_context_keys, list):
            metadata["writes_context_keys"] = [
                str(item).strip()
                for item in writes_context_keys
                if isinstance(item, str) and str(item).strip()
            ]
        tool_output_mappings = raw_row.get("tool_output_context_mappings")
        if isinstance(tool_output_mappings, list):
            metadata["tool_output_context_mappings"] = [
                dict(item) for item in tool_output_mappings if isinstance(item, Mapping)
            ]
        mutation_authority = raw_row.get("mutation_authority")
        if isinstance(mutation_authority, Mapping):
            metadata["mutation_authority"] = dict(mutation_authority)
        if action is not None and _clean_text(action.subworkflow_id):
            metadata["invokes_workflow"] = _clean_text(action.subworkflow_id)
            subworkflow_contract = _build_subworkflow_contract_from_authoring_row(
                row=raw_row,
                action=action,
            )
            if subworkflow_contract is not None:
                metadata["subworkflow_contract"] = subworkflow_contract

        states[state_id] = WorkflowStateSpec(
            state_id=state_id,
            actions=(action,) if action is not None else (),
            transitions=transitions,
            terminal=terminal,
            metadata=metadata,
        )
        if terminal:
            termination_states.append(state_id)

    initial_state = _clean_text(spec.get("initial_state_key"))
    if not initial_state or initial_state not in states:
        initial_state = next(iter(states.keys()), "")
    if not initial_state:
        raise ValueError("workflow_authoring_spec_missing_initial_state")

    purpose = _clean_text(spec.get("workflow_description") or spec.get("description")) or None
    workflow_metadata: dict[str, Any] = {}
    raw_workflow_metadata = spec.get("workflow_metadata")
    if isinstance(raw_workflow_metadata, Mapping):
        workflow_metadata = {
            str(key): value
            for key, value in raw_workflow_metadata.items()
            if str(key)
        }
    required_effects = spec.get("required_effects")
    if isinstance(required_effects, list):
        workflow_metadata["required_effects"] = [
            str(item).strip()
            for item in required_effects
            if isinstance(item, str) and str(item).strip()
        ]
    postcondition_probe = spec.get("postcondition_probe")
    if isinstance(postcondition_probe, Mapping):
        workflow_metadata["postcondition_probe"] = dict(postcondition_probe)
    verification_inputs = spec.get("verification_inputs")
    if isinstance(verification_inputs, Mapping):
        workflow_metadata["verification_inputs"] = dict(verification_inputs)

    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=initial_state,
        states=states,
        termination_states=tuple(dict.fromkeys(termination_states)),
        purpose=purpose,
        metadata=workflow_metadata,
    )


__all__ = [
    "build_workflow_definition_from_authoring_spec",
    "collect_transient_execution_input_issues",
    "serialise_workflow_definition_to_authoring_spec",
    "strip_transient_execution_defaults_from_authoring_spec",
]
