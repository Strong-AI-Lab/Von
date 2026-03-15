"""Workflow definition identity and strict contract validation helpers.

This module is the canonical place to:
1. derive deterministic workflow definition identity hashes for monitor/introspection
   surfaces, and
2. enforce strict contract gates for workflow publication/runnability checks.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence

from .subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    normalise_subworkflow_contract,
)
from .plan_state_runtime import (
    normalise_workflow_completion_gate_spec,
    normalise_workflow_plan_state_policy_spec,
    normalise_workflow_step_checkpoint_policy_spec,
)
from .execution_contracts import (
    WORKFLOW_CONTROL_BREAK_ACTION_IDS,
    WORKFLOW_CONTROL_CONTINUE_ACTION_IDS,
    WORKFLOW_CONTROL_FOR_EACH_ACTION_IDS,
    WORKFLOW_CONTROL_FORK_ACTION_IDS,
    WORKFLOW_CONTROL_JOIN_ACTION_IDS,
    WORKFLOW_FOR_EACH_ALLOWED_SUCCESS_POLICIES,
)

WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION = "workflow_definition_identity.v1"
WORKFLOW_DEFINITION_IDENTITY_VERSION = 1
WORKFLOW_CONTRACT_SHAPE_SCHEMA_VERSION = "workflow_contract_shape.v1"

_STATE_METADATA_CONTRACT_KEYS: tuple[str, ...] = (
    "preconditions",
    "effects",
    "reads_variables",
    "writes_variables",
    "reads_context_keys",
    "writes_context_keys",
    "context_input_mappings",
    "tool_output_context_mappings",
    "subworkflow_contract",
    "invokes_workflow",
    "loop_scope_id",
    "fork_id",
    "join_fork_id",
    "retry_policy",
    "approval_gate",
    "idempotency_policy",
    "checkpoint_policy",
    "prompt_contract",
)


def _is_sequence_like(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _normalise_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return {"__type__": type(value).__name__}


def _normalise_json_like(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_json_like(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_json_like(item) for item in value]
    if isinstance(value, set):
        return sorted(_normalise_json_like(item) for item in value)
    return _normalise_scalar(value)


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalise_symbol_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    symbols: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        symbols.append(text)
    return symbols


def _state_metadata(state_spec: Any) -> dict[str, Any]:
    metadata_raw = getattr(state_spec, "metadata", {})
    if isinstance(metadata_raw, Mapping):
        return dict(metadata_raw)
    return {}


def _extract_initial_required_inputs(definition: Any) -> set[str]:
    states_raw = getattr(definition, "states", {})
    states = states_raw if isinstance(states_raw, Mapping) else {}
    initial_state = str(getattr(definition, "initial_state", "") or "").strip()
    if not initial_state:
        return set()
    state_spec = states.get(initial_state)
    if state_spec is None:
        return set()
    metadata = _state_metadata(state_spec)
    required_inputs: set[str] = set()
    required_inputs.update(_normalise_symbol_list(metadata.get("preconditions")))
    required_inputs.update(_normalise_symbol_list(metadata.get("reads_variables")))
    required_inputs.update(_normalise_symbol_list(metadata.get("reads_context_keys")))
    return required_inputs


def _extract_possible_outputs(definition: Any) -> set[str]:
    states_raw = getattr(definition, "states", {})
    states = states_raw if isinstance(states_raw, Mapping) else {}
    outputs: set[str] = set()
    for state_spec in states.values():
        metadata = _state_metadata(state_spec)
        outputs.update(_normalise_symbol_list(metadata.get("effects")))
        outputs.update(_normalise_symbol_list(metadata.get("writes_variables")))
        outputs.update(_normalise_symbol_list(metadata.get("writes_context_keys")))
    return outputs


def _extract_action_input_text(action: Any, *keys: str) -> str:
    inputs = getattr(action, "inputs", {})
    if not isinstance(inputs, Mapping):
        return ""
    for key in keys:
        value = inputs.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _collect_subworkflow_children(definition: Any) -> set[str]:
    children: set[str] = set()
    states_raw = getattr(definition, "states", {})
    states = states_raw if isinstance(states_raw, Mapping) else {}
    for state_spec in states.values():
        actions_raw = getattr(state_spec, "actions", ())
        actions = actions_raw if _is_sequence_like(actions_raw) else ()
        metadata = _state_metadata(state_spec)
        contract_raw = metadata.get("subworkflow_contract")
        contract, _ = normalise_subworkflow_contract(contract_raw)
        if isinstance(contract, Mapping):
            workflow_id = str(contract.get("workflow_id") or "").strip()
            if workflow_id:
                children.add(workflow_id)
            for candidate_id in contract.get("candidate_workflow_ids", []):
                candidate_text = str(candidate_id or "").strip()
                if candidate_text:
                    children.add(candidate_text)
        for action in actions:
            action_id = str(getattr(action, "action_id", "") or "").strip()
            if action_id != WORKFLOW_SUBWORKFLOW_ACTION_ID:
                continue
            workflow_id = _extract_action_input_text(action, "workflow_id")
            if workflow_id:
                children.add(workflow_id)
    return children


def _find_recursive_subworkflow_cycle(
    *,
    parent_workflow_id: str,
    child_workflow_id: str,
    loader: Callable[[str], Any | None] | None,
    max_depth: int = 16,
) -> list[str] | None:
    if (
        not parent_workflow_id
        or not child_workflow_id
        or not callable(loader)
        or max_depth < 1
    ):
        return None

    stack: list[tuple[str, list[str]]] = [(child_workflow_id, [child_workflow_id])]
    while stack:
        workflow_id, path = stack.pop()
        if len(path) > max_depth:
            continue
        if workflow_id == parent_workflow_id and len(path) > 1:
            return path
        try:
            definition = loader(workflow_id)
        except Exception:
            continue
        if definition is None:
            continue
        for child_id in sorted(_collect_subworkflow_children(definition)):
            if child_id == parent_workflow_id:
                return [*path, child_id]
            if child_id in path:
                continue
            stack.append((child_id, [*path, child_id]))
    return None


def collect_workflow_action_ids(definition: Any) -> tuple[str, ...]:
    """Collect unique action IDs referenced by a workflow definition."""

    action_ids: set[str] = set()
    states_raw = getattr(definition, "states", {})
    states = states_raw if isinstance(states_raw, Mapping) else {}
    for state_spec in states.values():
        actions_raw = getattr(state_spec, "actions", ())
        actions = actions_raw if _is_sequence_like(actions_raw) else ()
        for action in actions:
            action_id = str(
                getattr(action, "action_id", None)
                or getattr(action, "target_id", "")
                or ""
            ).strip()
            if action_id:
                action_ids.add(action_id)
    return tuple(sorted(action_ids))


def _transition_payload_from_graph_step(
    *,
    control_flow: Mapping[str, Any],
) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []

    explicit_branches = control_flow.get("conditions")
    if explicit_branches is None:
        explicit_branches = control_flow.get("declarative_conditions")
    if isinstance(explicit_branches, list):
        for index, branch in enumerate(explicit_branches):
            if not isinstance(branch, Mapping):
                continue
            to_state = str(
                branch.get("to") or branch.get("to_state") or branch.get("next") or ""
            ).strip()
            if not to_state:
                continue
            reason = str(branch.get("reason") or f"condition_{index + 1}").strip()
            condition_spec = branch.get("condition")
            transitions.append(
                {
                    "to_state": to_state,
                    "reason": reason,
                    "condition_spec": (
                        _normalise_json_like(condition_spec)
                        if isinstance(condition_spec, Mapping)
                        else None
                    ),
                }
            )

    on_failure_target = str(control_flow.get("on_failure") or "").strip()
    if on_failure_target:
        transitions.append(
            {
                "to_state": on_failure_target,
                "reason": "on_failure",
                "condition_spec": {
                    "kind": "context_flag",
                    "key": "last_action_failed",
                    "expected": True,
                },
            }
        )

    on_unknown_target = str(control_flow.get("on_unknown") or "").strip()
    if on_unknown_target:
        transitions.append(
            {
                "to_state": on_unknown_target,
                "reason": "on_unknown",
                "condition_spec": {
                    "kind": "context_flag",
                    "key": "last_action_unknown",
                    "expected": True,
                },
            }
        )

    on_break_target = str(control_flow.get("on_break") or "").strip()
    if on_break_target:
        transitions.append(
            {
                "to_state": on_break_target,
                "reason": "on_break",
                "condition_spec": {
                    "kind": "control_signal",
                    "signal": "break",
                },
            }
        )

    on_continue_target = str(control_flow.get("on_continue") or "").strip()
    if on_continue_target:
        transitions.append(
            {
                "to_state": on_continue_target,
                "reason": "on_continue",
                "condition_spec": {
                    "kind": "control_signal",
                    "signal": "continue",
                },
            }
        )

    on_true_target = str(control_flow.get("on_true") or "").strip()
    if on_true_target:
        transitions.append(
            {
                "to_state": on_true_target,
                "reason": "on_true",
                "condition_spec": {
                    "kind": "transition_result_truth",
                    "expected": True,
                },
            }
        )

    on_false_target = str(control_flow.get("on_false") or "").strip()
    if on_false_target:
        transitions.append(
            {
                "to_state": on_false_target,
                "reason": "on_false",
                "condition_spec": {
                    "kind": "transition_result_truth",
                    "expected": False,
                },
            }
        )

    next_target = str(control_flow.get("next") or "").strip()
    if next_target:
        transitions.append(
            {
                "to_state": next_target,
                "reason": "next_step",
                "condition_spec": {"kind": "always"},
            }
        )

    return transitions


def _normalise_workflow_contract_shape_from_graph(
    *,
    workflow_id: str,
    graph: Mapping[str, Any],
) -> dict[str, Any]:
    raw_steps = graph.get("steps")
    steps = raw_steps if isinstance(raw_steps, list) else []
    step_entries: list[dict[str, Any]] = []
    termination_states: list[str] = []

    for step in sorted(
        (item for item in steps if isinstance(item, Mapping)),
        key=lambda item: str(item.get("step_id") or ""),
    ):
        state_id = str(step.get("step_id") or "").strip()
        if not state_id:
            continue
        invokes_action = str(step.get("invokes_action") or "").strip()
        invokes_workflow = str(step.get("invokes_workflow") or "").strip()
        actions: list[str] = []
        if invokes_action:
            actions = [invokes_action]
        elif invokes_workflow:
            actions = [WORKFLOW_SUBWORKFLOW_ACTION_ID]

        control_flow_raw = step.get("control_flow")
        control_flow = control_flow_raw if isinstance(control_flow_raw, Mapping) else {}
        transitions = _transition_payload_from_graph_step(control_flow=control_flow)
        terminal = len(transitions) == 0
        if terminal:
            termination_states.append(state_id)

        metadata: Dict[str, Any] = {}
        for key in _STATE_METADATA_CONTRACT_KEYS:
            value = step.get(key)
            if value:
                metadata[key] = _normalise_json_like(value)
        if invokes_workflow:
            metadata.setdefault("invokes_workflow", invokes_workflow)

        step_entries.append(
            {
                "state_id": state_id,
                "terminal": terminal,
                "actions": actions,
                "transitions": transitions,
                "metadata": metadata,
            }
        )

    graph_metadata_raw = graph.get("workflow_metadata")
    if not isinstance(graph_metadata_raw, Mapping):
        graph_metadata_raw = graph.get("metadata")
    graph_metadata = (
        _normalise_json_like(dict(graph_metadata_raw))
        if isinstance(graph_metadata_raw, Mapping)
        else {}
    )

    return {
        "schema_version": WORKFLOW_CONTRACT_SHAPE_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "initial_state": str(graph.get("initial_step") or "").strip(),
        "termination_states": sorted(set(termination_states)),
        "metadata": graph_metadata,
        "states": step_entries,
    }


def _normalise_workflow_contract_shape_from_definition(
    definition: Any,
) -> dict[str, Any]:
    state_entries: list[dict[str, Any]] = []
    termination_raw = getattr(definition, "termination_states", ())
    termination_values = termination_raw if _is_sequence_like(termination_raw) else ()
    termination_states = set(str(item) for item in termination_values)

    states_raw = getattr(definition, "states", {})
    states = states_raw if isinstance(states_raw, Mapping) else {}
    definition_metadata_raw = getattr(definition, "metadata", {})
    definition_metadata = _normalise_json_like(
        dict(definition_metadata_raw)
        if isinstance(definition_metadata_raw, Mapping)
        else {}
    )

    for state_id in sorted(str(key) for key in states.keys()):
        state_spec = states[state_id]
        actions_raw = getattr(state_spec, "actions", ())
        actions = [
            str(getattr(action, "action_id", "") or "").strip()
            for action in (actions_raw if _is_sequence_like(actions_raw) else ())
        ]
        actions = [item for item in actions if item]
        transitions = [
            {
                "to_state": str(getattr(transition, "to_state", "") or "").strip(),
                "reason": str(getattr(transition, "reason", "") or "").strip() or None,
                "condition_spec": (
                    _normalise_json_like(getattr(transition, "condition_spec", None))
                    if isinstance(getattr(transition, "condition_spec", None), Mapping)
                    else None
                ),
            }
            for transition in (
                getattr(state_spec, "transitions", ())
                if _is_sequence_like(getattr(state_spec, "transitions", ()))
                else ()
            )
        ]
        metadata_raw = getattr(state_spec, "metadata", {})
        metadata = _normalise_json_like(
            dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
        )
        if bool(getattr(state_spec, "terminal", False)):
            termination_states.add(state_id)

        state_entries.append(
            {
                "state_id": state_id,
                "terminal": bool(getattr(state_spec, "terminal", False)),
                "actions": actions,
                "transitions": transitions,
                "metadata": metadata,
            }
        )

    return {
        "schema_version": WORKFLOW_CONTRACT_SHAPE_SCHEMA_VERSION,
        "workflow_id": str(getattr(definition, "workflow_id", "") or "").strip(),
        "initial_state": str(getattr(definition, "initial_state", "") or "").strip(),
        "termination_states": sorted(termination_states),
        "metadata": definition_metadata,
        "states": state_entries,
    }


def build_workflow_definition_identity(
    *,
    workflow_id: str,
    source: str | None,
    definition: Any | None,
    authoritative_definition: Any | None = None,
) -> dict[str, Any]:
    """Build deterministic definition identity payload for monitoring surfaces."""

    runtime_hash: str | None = None
    authoritative_hash: str | None = None
    state_count = 0
    action_count = 0

    if definition is not None:
        runtime_shape = _normalise_workflow_contract_shape_from_definition(definition)
        runtime_hash = _hash_payload(runtime_shape)
        state_count = len(runtime_shape.get("states", []))
        action_count = len(collect_workflow_action_ids(definition))

    if authoritative_definition is not None:
        authoritative_shape = _normalise_workflow_contract_shape_from_definition(
            authoritative_definition
        )
        authoritative_hash = _hash_payload(authoritative_shape)
        if state_count == 0:
            state_count = len(authoritative_shape.get("states", []))
        if action_count == 0:
            action_count = len(collect_workflow_action_ids(authoritative_definition))

    source_text = str(source or "unknown").strip() or "unknown"
    source_normalised = source_text.lower()
    definition_hash = runtime_hash
    if source_normalised == "vontology" and authoritative_hash:
        definition_hash = authoritative_hash

    hash_mismatch = bool(
        runtime_hash and authoritative_hash and runtime_hash != authoritative_hash
    )

    return {
        "schema_version": WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION,
        "version": WORKFLOW_DEFINITION_IDENTITY_VERSION,
        "workflow_id": str(workflow_id or "").strip(),
        "source": source_text,
        "definition_hash": definition_hash,
        "runtime_definition_hash": runtime_hash,
        "authoritative_definition_hash": authoritative_hash,
        "hash_mismatch": hash_mismatch,
        "state_count": state_count,
        "action_count": action_count,
    }


def build_workflow_definition_identity_from_graph(
    *,
    workflow_id: str,
    graph: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build identity payload from raw Vontology graph representation only."""

    if not isinstance(graph, Mapping):
        return None
    contract_shape = _normalise_workflow_contract_shape_from_graph(
        workflow_id=workflow_id,
        graph=graph,
    )
    definition_hash = _hash_payload(contract_shape)
    action_count = 0
    for state in contract_shape.get("states", []):
        if not isinstance(state, Mapping):
            continue
        actions = state.get("actions")
        if isinstance(actions, Sequence):
            action_count += len([item for item in actions if isinstance(item, str) and item])
    return {
        "schema_version": WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION,
        "version": WORKFLOW_DEFINITION_IDENTITY_VERSION,
        "workflow_id": str(workflow_id or "").strip(),
        "source": "vontology",
        "definition_hash": definition_hash,
        "runtime_definition_hash": None,
        "authoritative_definition_hash": definition_hash,
        "hash_mismatch": False,
        "state_count": len(contract_shape.get("states", [])),
        "action_count": action_count,
    }


def validate_workflow_definition_contract(
    *,
    definition: Any | None,
    supported_action_ids: Iterable[str] | None = None,
    enforce_supported_actions: bool = False,
    known_workflow_ids: Iterable[str] | None = None,
    workflow_definition_loader: Callable[[str], Any | None] | None = None,
) -> dict[str, Any]:
    """Validate strict workflow contract gates for publication/runnability.

    Gates checked:
    - control-flow completeness for action-bearing states
    - transition target resolvability
    - non-vacuous step contracts
    - valid input/output/context mapping declarations
    - optional action support resolution
    """

    if definition is None:
        return {
            "valid": False,
            "errors": ["workflow_definition_missing"],
            "unsupported_action_ids": [],
            "missing_transition_state_ids": [],
            "unknown_transition_targets": [],
            "vacuous_state_ids": [],
            "unresolved_input_mapping_states": [],
            "invalid_input_mapping_specs": [],
            "unresolved_output_mapping_states": [],
            "invalid_output_mapping_specs": [],
            "subworkflow_contract_issues": [],
            "control_signal_issues": [],
            "fork_join_issues": [],
            "plan_state_issues": [],
            "completion_gate_issues": [],
            "prompt_contract_issues": [],
        }

    states_raw = getattr(definition, "states", {})
    states = states_raw if isinstance(states_raw, Mapping) else {}
    state_ids = set(str(state_id) for state_id in states.keys())
    parent_workflow_id = str(getattr(definition, "workflow_id", "") or "").strip()
    termination_raw = getattr(definition, "termination_states", ())
    termination_values = termination_raw if _is_sequence_like(termination_raw) else ()
    termination_states = set(str(item) for item in termination_values)
    for state_id, state_spec in states.items():
        if bool(getattr(state_spec, "terminal", False)):
            termination_states.add(str(state_id))

    unsupported_action_ids: list[str] = []
    missing_transition_state_ids: list[str] = []
    unknown_transition_targets: list[dict[str, str]] = []
    vacuous_state_ids: list[str] = []
    unresolved_input_mapping_states: list[str] = []
    invalid_input_mapping_specs: list[dict[str, Any]] = []
    unresolved_output_mapping_states: list[str] = []
    invalid_output_mapping_specs: list[dict[str, Any]] = []
    subworkflow_contract_issues: list[dict[str, Any]] = []
    control_signal_issues: list[dict[str, Any]] = []
    fork_join_issues: list[dict[str, Any]] = []
    iterator_issues: list[dict[str, Any]] = []
    approval_gate_issues: list[dict[str, Any]] = []
    runtime_policy_issues: list[dict[str, Any]] = []
    plan_state_issues: list[dict[str, Any]] = []
    completion_gate_issues: list[dict[str, Any]] = []
    prompt_contract_issues: list[dict[str, Any]] = []
    declared_fork_ids: set[str] = set()

    supported_action_set: set[str] = set()
    if supported_action_ids is not None:
        supported_action_set = {
            str(action_id).strip()
            for action_id in supported_action_ids
            if isinstance(action_id, str) and str(action_id).strip()
        }
    known_workflow_set: set[str] = set()
    if known_workflow_ids is not None:
        known_workflow_set = {
            str(workflow_id).strip()
            for workflow_id in known_workflow_ids
            if isinstance(workflow_id, str) and str(workflow_id).strip()
        }

    definition_metadata_raw = getattr(definition, "metadata", {})
    definition_metadata = (
        dict(definition_metadata_raw)
        if isinstance(definition_metadata_raw, Mapping)
        else {}
    )
    try:
        plan_state_policy = normalise_workflow_plan_state_policy_spec(
            definition_metadata.get("plan_state_policy")
        )
    except ValueError as exc:
        plan_state_policy = None
        plan_state_issues.append(
            {
                "scope": "workflow",
                "reason_code": str(exc),
            }
        )
    try:
        completion_gate = normalise_workflow_completion_gate_spec(
            definition_metadata.get("completion_gate")
        )
    except ValueError as exc:
        completion_gate = None
        completion_gate_issues.append(
            {
                "scope": "workflow",
                "reason_code": str(exc),
            }
        )

    for scan_state_id in sorted(str(item) for item in states.keys()):
        scan_state_spec = states[scan_state_id]
        actions_raw = getattr(scan_state_spec, "actions", ())
        actions = actions_raw if _is_sequence_like(actions_raw) else ()
        for action in actions:
            action_id = str(getattr(action, "action_id", "") or "").strip()
            if action_id not in WORKFLOW_CONTROL_FORK_ACTION_IDS:
                continue
            fork_id = _extract_action_input_text(action, "fork_id", "fork_context_id")
            declared_fork_ids.add(fork_id or scan_state_id)

    for state_id in sorted(str(item) for item in states.keys()):
        state_spec = states[state_id]
        metadata_raw = getattr(state_spec, "metadata", {})
        metadata = dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
        actions_raw = getattr(state_spec, "actions", ())
        action_objects = actions_raw if _is_sequence_like(actions_raw) else ()
        action_specs: list[dict[str, Any]] = []
        for action in action_objects:
            action_id = str(getattr(action, "action_id", "") or "").strip()
            target_id = str(getattr(action, "target_id", "") or "").strip()
            execution_mode = str(getattr(action, "execution_mode", "") or "").strip()
            action_inputs_raw = getattr(action, "inputs", {})
            action_inputs = (
                dict(action_inputs_raw)
                if isinstance(action_inputs_raw, Mapping)
                else {}
            )
            prompt_contract_raw = getattr(action, "prompt_contract", None)
            action_specs.append(
                {
                    "action_id": action_id,
                    "target_id": target_id,
                    "execution_mode": execution_mode,
                    "inputs": action_inputs,
                    "prompt_contract": (
                        dict(prompt_contract_raw)
                        if isinstance(prompt_contract_raw, Mapping)
                        else {}
                    ),
                }
            )
        executable_actions = [
            spec for spec in action_specs if str(spec.get("target_id") or "").strip()
        ]
        actions = [
            str(spec.get("action_id") or spec.get("target_id") or "").strip()
            for spec in executable_actions
            if str(spec.get("action_id") or spec.get("target_id") or "").strip()
        ]
        llm_actions = [
            spec
            for spec in executable_actions
            if str(spec.get("execution_mode") or "").strip().lower() == "llm"
        ]
        is_terminal_state = bool(getattr(state_spec, "terminal", False)) or (
            state_id in termination_states
        )

        has_metadata_contract = any(
            bool(metadata.get(key)) for key in _STATE_METADATA_CONTRACT_KEYS
        )
        # Terminal sink states (for example an explicit "failed" state) may
        # intentionally omit actions/mappings while remaining structurally valid.
        if not actions and not has_metadata_contract and not is_terminal_state:
            vacuous_state_ids.append(state_id)

        transitions_raw = getattr(state_spec, "transitions", ())
        transitions = list(transitions_raw) if _is_sequence_like(transitions_raw) else []
        if actions and not transitions and state_id not in termination_states:
            missing_transition_state_ids.append(state_id)

        for transition in transitions:
            to_state = str(getattr(transition, "to_state", "") or "").strip()
            if not to_state or to_state not in state_ids:
                unknown_transition_targets.append(
                    {
                        "state_id": state_id,
                        "to_state": to_state or "<missing>",
                        "reason": (
                            str(getattr(transition, "reason", "") or "").strip()
                            or "unspecified"
                        ),
                    }
                )

        has_on_break_transition = any(
            str(getattr(transition, "reason", "") or "").strip().lower() == "on_break"
            for transition in transitions
        )
        has_on_continue_transition = any(
            str(getattr(transition, "reason", "") or "").strip().lower()
            == "on_continue"
            for transition in transitions
        )
        has_on_approval_required_transition = any(
            str(getattr(transition, "reason", "") or "").strip().lower()
            == "on_approval_required"
            for transition in transitions
        )
        loop_scope_id = str(metadata.get("loop_scope_id") or "").strip()
        raw_prompt_contract = metadata.get("prompt_contract")
        prompt_contract: Mapping[str, Any] = (
            raw_prompt_contract if isinstance(raw_prompt_contract, Mapping) else {}
        )
        if metadata.get("approval_gate") and not has_on_approval_required_transition:
            approval_gate_issues.append(
                {
                    "state_id": state_id,
                    "reason_code": "approval_transition_missing",
                }
            )
        if metadata.get("retry_policy") and len(actions) > 1:
            runtime_policy_issues.append(
                {
                    "state_id": state_id,
                    "reason_code": "retry_policy_multi_action_state_unsupported",
                }
            )
        if metadata.get("idempotency_policy") and len(actions) > 1:
            runtime_policy_issues.append(
                {
                    "state_id": state_id,
                    "reason_code": "idempotency_policy_multi_action_state_unsupported",
                }
            )
        try:
            normalise_workflow_step_checkpoint_policy_spec(
                metadata.get("checkpoint_policy")
            )
        except ValueError as exc:
            plan_state_issues.append(
                {
                    "scope": "state",
                    "state_id": state_id,
                    "reason_code": str(exc),
                }
            )
        requested_prompt_concept_ids = _normalise_symbol_list(
            prompt_contract.get("requested_prompt_concept_ids")
        )
        resolved_prompt_concept_id = str(
            prompt_contract.get("resolved_prompt_concept_id") or ""
        ).strip()
        if prompt_contract and not executable_actions:
            prompt_contract_issues.append(
                {
                    "state_id": state_id,
                    "reason_code": "prompt_contract_without_action",
                    "severity": "error",
                }
            )
        if prompt_contract and executable_actions and not llm_actions:
            prompt_contract_issues.append(
                {
                    "state_id": state_id,
                    "reason_code": "prompt_contract_not_compiled_as_llm_step",
                    "severity": "error",
                }
            )
        if requested_prompt_concept_ids and not resolved_prompt_concept_id:
            prompt_contract_issues.append(
                {
                    "state_id": state_id,
                    "reason_code": "prompt_resolution_missing",
                    "severity": "error",
                    "requested_prompt_concept_ids": requested_prompt_concept_ids,
                }
            )
        for action_spec in action_specs:
            action_id = str(
                action_spec.get("action_id") or action_spec.get("target_id") or ""
            ).strip()
            raw_action_inputs = action_spec.get("inputs")
            action_inputs: dict[str, Any] = (
                {
                    str(key): value
                    for key, value in raw_action_inputs.items()
                    if isinstance(key, str)
                }
                if isinstance(raw_action_inputs, Mapping)
                else {}
            )
            raw_prompt_diagnostics = action_inputs.get("__prompt_resolution_diagnostics")
            prompt_diagnostics: dict[str, Any] = (
                {
                    str(key): value
                    for key, value in raw_prompt_diagnostics.items()
                    if isinstance(key, str)
                }
                if isinstance(raw_prompt_diagnostics, Mapping)
                else {}
            )
            for reason_code in _normalise_symbol_list(prompt_diagnostics.get("errors")):
                prompt_contract_issues.append(
                    {
                        "state_id": state_id,
                        "action_id": action_id,
                        "reason_code": reason_code,
                        "severity": "error",
                    }
                )
            for reason_code in _normalise_symbol_list(prompt_diagnostics.get("warnings")):
                prompt_contract_issues.append(
                    {
                        "state_id": state_id,
                        "action_id": action_id,
                        "reason_code": reason_code,
                        "severity": "warning",
                    }
                )
            if action_id in WORKFLOW_CONTROL_BREAK_ACTION_IDS:
                action_scope = str(
                    action_inputs.get("loop_scope_id")
                    or action_inputs.get("scope")
                    or ""
                ).strip()
                if not (action_scope or loop_scope_id):
                    control_signal_issues.append(
                        {
                            "state_id": state_id,
                            "action_id": action_id,
                            "reason_code": "break_outside_loop_scope",
                        }
                    )
                if not has_on_break_transition:
                    control_signal_issues.append(
                        {
                            "state_id": state_id,
                            "action_id": action_id,
                            "reason_code": "break_transition_missing",
                        }
                    )
            if action_id in WORKFLOW_CONTROL_CONTINUE_ACTION_IDS:
                action_scope = str(
                    action_inputs.get("loop_scope_id")
                    or action_inputs.get("scope")
                    or ""
                ).strip()
                if not (action_scope or loop_scope_id):
                    control_signal_issues.append(
                        {
                            "state_id": state_id,
                            "action_id": action_id,
                            "reason_code": "continue_outside_loop_scope",
                        }
                    )
                if not has_on_continue_transition:
                    control_signal_issues.append(
                        {
                            "state_id": state_id,
                            "action_id": action_id,
                            "reason_code": "continue_transition_missing",
                        }
                    )
            if action_id in WORKFLOW_CONTROL_JOIN_ACTION_IDS:
                join_fork_id = str(
                    action_inputs.get("fork_id")
                    or action_inputs.get("fork_context_id")
                    or metadata.get("fork_id")
                    or metadata.get("join_fork_id")
                    or state_id
                ).strip()
                if join_fork_id not in declared_fork_ids:
                    fork_join_issues.append(
                        {
                            "state_id": state_id,
                            "action_id": action_id,
                            "fork_id": join_fork_id,
                            "reason_code": "join_without_matching_fork",
                        }
                    )
            if action_id in WORKFLOW_CONTROL_FOR_EACH_ACTION_IDS:
                workflow_id = str(
                    action_inputs.get("workflow_id")
                    or action_inputs.get("workflow")
                    or ""
                ).strip()
                if not workflow_id:
                    iterator_issues.append(
                        {
                            "state_id": state_id,
                            "action_id": action_id,
                            "reason_code": "for_each_workflow_id_missing",
                        }
                    )
                explicit_items = action_inputs.get("items")
                if not (
                    (
                        _is_sequence_like(explicit_items)
                        and not isinstance(explicit_items, (str, bytes, bytearray))
                    )
                    or str(
                        action_inputs.get("items_context_key")
                        or action_inputs.get("items_path")
                        or action_inputs.get("context_key")
                        or ""
                    ).strip()
                ):
                    iterator_issues.append(
                        {
                            "state_id": state_id,
                            "action_id": action_id,
                            "reason_code": "for_each_items_missing",
                        }
                    )
                success_policy = str(action_inputs.get("success_policy") or "").strip()
                if success_policy and success_policy not in set(
                    WORKFLOW_FOR_EACH_ALLOWED_SUCCESS_POLICIES
                ):
                    iterator_issues.append(
                        {
                            "state_id": state_id,
                            "action_id": action_id,
                            "reason_code": "for_each_success_policy_invalid",
                            "success_policy": success_policy,
                        }
                    )

        if metadata.get("unresolved_input_mappings"):
            unresolved_input_mapping_states.append(state_id)
        invalid_input_specs = metadata.get("invalid_input_mapping_specs")
        if isinstance(invalid_input_specs, list):
            for spec in invalid_input_specs:
                if isinstance(spec, Mapping):
                    invalid_input_mapping_specs.append(
                        {
                            "state_id": state_id,
                            "mapping_concept_id": str(spec.get("mapping_concept_id") or ""),
                            "reason_code": str(spec.get("reason_code") or "unknown"),
                        }
                    )

        if metadata.get("unresolved_output_mappings"):
            unresolved_output_mapping_states.append(state_id)
        invalid_output_specs = metadata.get("invalid_output_mapping_specs")
        if isinstance(invalid_output_specs, list):
            for spec in invalid_output_specs:
                if isinstance(spec, Mapping):
                    invalid_output_mapping_specs.append(
                        {
                            "state_id": state_id,
                            "mapping_concept_id": str(spec.get("mapping_concept_id") or ""),
                            "reason_code": str(spec.get("reason_code") or "unknown"),
                        }
                    )

        has_subworkflow_action = WORKFLOW_SUBWORKFLOW_ACTION_ID in actions
        raw_subworkflow_contract = metadata.get("subworkflow_contract")
        if has_subworkflow_action or raw_subworkflow_contract is not None:
            normalised_contract, contract_error = normalise_subworkflow_contract(
                raw_subworkflow_contract
            )
            if normalised_contract is None:
                subworkflow_contract_issues.append(
                    {
                        "state_id": state_id,
                        "reason_code": contract_error
                        or "subworkflow_contract_invalid",
                    }
                )
            else:
                child_workflow_id = str(normalised_contract.get("workflow_id") or "").strip()
                workflow_id_context_key = str(
                    normalised_contract.get("workflow_id_context_key") or ""
                ).strip()
                candidate_workflow_ids = {
                    str(item or "").strip()
                    for item in normalised_contract.get("candidate_workflow_ids", [])
                    if isinstance(item, str) and str(item or "").strip()
                }
                if (
                    parent_workflow_id
                    and child_workflow_id
                    and parent_workflow_id == child_workflow_id
                ):
                    subworkflow_contract_issues.append(
                        {
                            "state_id": state_id,
                            "workflow_id": child_workflow_id,
                            "reason_code": "subworkflow_recursive_self_reference",
                        }
                    )

                provided_inputs = {
                    item
                    for item in normalised_contract.get("provided_inputs", [])
                    if isinstance(item, str) and item.strip()
                }
                mapped_outputs = {
                    item
                    for item in normalised_contract.get("mapped_outputs", [])
                    if isinstance(item, str) and item.strip()
                }
                required_outputs = {
                    item
                    for item in normalised_contract.get("required_outputs", [])
                    if isinstance(item, str) and item.strip()
                }

                missing_required_outputs = sorted(required_outputs - mapped_outputs)
                if missing_required_outputs:
                    subworkflow_contract_issues.append(
                        {
                            "state_id": state_id,
                            "workflow_id": child_workflow_id,
                            "reason_code": "subworkflow_required_outputs_unmapped",
                            "missing_outputs": missing_required_outputs,
                        }
                    )

                workflow_id_input_mappings = {
                    str(item.get("parent_context_key") or "").strip()
                    for item in normalised_contract.get("input_mappings", [])
                    if isinstance(item, Mapping)
                    and str(item.get("child_input_key") or "").strip() == "workflow_id"
                    and str(item.get("parent_context_key") or "").strip()
                }
                if workflow_id_context_key and workflow_id_context_key not in workflow_id_input_mappings:
                    subworkflow_contract_issues.append(
                        {
                            "state_id": state_id,
                            "reason_code": "subworkflow_workflow_id_context_unmapped",
                            "workflow_id_context_key": workflow_id_context_key,
                        }
                    )

                child_definition = None
                if callable(workflow_definition_loader) and child_workflow_id:
                    try:
                        child_definition = workflow_definition_loader(child_workflow_id)
                    except Exception as exc:  # pragma: no cover - defensive
                        subworkflow_contract_issues.append(
                            {
                                "state_id": state_id,
                                "workflow_id": child_workflow_id,
                                "reason_code": "subworkflow_loader_error",
                                "detail": str(exc),
                            }
                        )
                recursive_cycle_path = _find_recursive_subworkflow_cycle(
                    parent_workflow_id=parent_workflow_id,
                    child_workflow_id=child_workflow_id,
                    loader=workflow_definition_loader,
                )
                if recursive_cycle_path:
                    subworkflow_contract_issues.append(
                        {
                            "state_id": state_id,
                            "workflow_id": child_workflow_id,
                            "reason_code": "subworkflow_recursive_cycle",
                            "cycle_path": recursive_cycle_path,
                        }
                    )

                child_known = bool(child_definition is not None)
                if child_workflow_id and not child_known and known_workflow_set:
                    child_known = child_workflow_id in known_workflow_set
                if child_workflow_id and not child_known:
                    subworkflow_contract_issues.append(
                        {
                            "state_id": state_id,
                            "workflow_id": child_workflow_id,
                            "reason_code": "subworkflow_workflow_not_found",
                        }
                    )
                for candidate_workflow_id in sorted(candidate_workflow_ids):
                    if known_workflow_set and candidate_workflow_id not in known_workflow_set:
                        subworkflow_contract_issues.append(
                            {
                                "state_id": state_id,
                                "workflow_id": candidate_workflow_id,
                                "reason_code": "subworkflow_candidate_workflow_not_found",
                            }
                        )

                if child_definition is not None:
                    child_required_inputs = _extract_initial_required_inputs(
                        child_definition
                    )
                    child_possible_outputs = _extract_possible_outputs(child_definition)

                    missing_child_inputs = sorted(
                        item for item in child_required_inputs if item not in provided_inputs
                    )
                    if missing_child_inputs:
                        subworkflow_contract_issues.append(
                            {
                                "state_id": state_id,
                                "workflow_id": child_workflow_id,
                                "reason_code": "subworkflow_input_contract_mismatch",
                                "missing_inputs": missing_child_inputs,
                            }
                        )

                    if child_possible_outputs:
                        unknown_output_fields = sorted(
                            item
                            for item in mapped_outputs
                            if item not in child_possible_outputs
                        )
                        if unknown_output_fields:
                            subworkflow_contract_issues.append(
                                {
                                    "state_id": state_id,
                                    "workflow_id": child_workflow_id,
                                    "reason_code": "subworkflow_output_contract_mismatch",
                                "unknown_outputs": unknown_output_fields,
                            }
                        )

        if enforce_supported_actions:
            for action_id in actions:
                if action_id not in supported_action_set:
                    unsupported_action_ids.append(action_id)

    unsupported_action_ids = sorted(set(unsupported_action_ids))
    unresolved_input_mapping_states = sorted(set(unresolved_input_mapping_states))
    unresolved_output_mapping_states = sorted(set(unresolved_output_mapping_states))
    vacuous_state_ids = sorted(set(vacuous_state_ids))
    missing_transition_state_ids = sorted(set(missing_transition_state_ids))
    unknown_transition_targets = sorted(
        unknown_transition_targets,
        key=lambda item: (item["state_id"], item["reason"], item["to_state"]),
    )
    invalid_input_mapping_specs = sorted(
        invalid_input_mapping_specs,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("mapping_concept_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    invalid_output_mapping_specs = sorted(
        invalid_output_mapping_specs,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("mapping_concept_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    subworkflow_contract_issues = sorted(
        subworkflow_contract_issues,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("workflow_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    control_signal_issues = sorted(
        control_signal_issues,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("action_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    fork_join_issues = sorted(
        fork_join_issues,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("action_id") or ""),
            str(item.get("fork_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    iterator_issues = sorted(
        iterator_issues,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("action_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    approval_gate_issues = sorted(
        approval_gate_issues,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    runtime_policy_issues = sorted(
        runtime_policy_issues,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    plan_state_issues = sorted(
        plan_state_issues,
        key=lambda item: (
            str(item.get("scope") or ""),
            str(item.get("state_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    completion_gate_issues = sorted(
        completion_gate_issues,
        key=lambda item: (
            str(item.get("scope") or ""),
            str(item.get("state_id") or ""),
            str(item.get("reason_code") or ""),
        ),
    )
    prompt_contract_issues = sorted(
        prompt_contract_issues,
        key=lambda item: (
            str(item.get("state_id") or ""),
            str(item.get("action_id") or ""),
            str(item.get("severity") or ""),
            str(item.get("reason_code") or ""),
        ),
    )

    errors: list[str] = []
    if vacuous_state_ids:
        errors.append("workflow_step_contract_vacuous")
    if missing_transition_state_ids:
        errors.append("workflow_control_flow_incomplete")
    if unknown_transition_targets:
        errors.append("workflow_transition_target_unresolved")
    if unresolved_input_mapping_states:
        errors.append("workflow_input_mapping_unresolved")
    if invalid_input_mapping_specs:
        errors.append("workflow_input_mapping_invalid")
    if unresolved_output_mapping_states:
        errors.append("workflow_output_mapping_unresolved")
    if invalid_output_mapping_specs:
        errors.append("workflow_output_mapping_invalid")
    if control_signal_issues:
        errors.append("workflow_control_signal_invalid")
    if fork_join_issues:
        errors.append("workflow_fork_join_invalid")
    if iterator_issues:
        errors.append("workflow_iterator_invalid")
    if approval_gate_issues:
        errors.append("workflow_approval_gate_invalid")
    if runtime_policy_issues:
        errors.append("workflow_runtime_policy_invalid")
    if plan_state_issues:
        errors.append("workflow_plan_state_policy_invalid")
    if completion_gate_issues:
        errors.append("workflow_completion_gate_invalid")
    if any(
        str(item.get("severity") or "").strip().lower() == "error"
        for item in prompt_contract_issues
    ):
        errors.append("workflow_prompt_contract_invalid")
    if subworkflow_contract_issues:
        unresolved_reason_codes = {
            "subworkflow_workflow_not_found",
            "subworkflow_loader_error",
        }
        mismatch_reason_codes = {
            "subworkflow_input_contract_mismatch",
            "subworkflow_output_contract_mismatch",
            "subworkflow_required_outputs_unmapped",
        }
        reason_codes = {
            str(item.get("reason_code") or "").strip()
            for item in subworkflow_contract_issues
        }
        if reason_codes.intersection(unresolved_reason_codes):
            errors.append("workflow_subworkflow_unresolved")
        if reason_codes.intersection(mismatch_reason_codes):
            errors.append("workflow_subworkflow_contract_mismatch")
        if (
            "workflow_subworkflow_contract_mismatch" not in errors
            or reason_codes - unresolved_reason_codes - mismatch_reason_codes
        ):
            errors.append("workflow_subworkflow_contract_invalid")
    if unsupported_action_ids:
        errors.append("unsupported_workflow_actions")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "unsupported_action_ids": unsupported_action_ids,
        "missing_transition_state_ids": missing_transition_state_ids,
        "unknown_transition_targets": unknown_transition_targets,
        "vacuous_state_ids": vacuous_state_ids,
        "unresolved_input_mapping_states": unresolved_input_mapping_states,
        "invalid_input_mapping_specs": invalid_input_mapping_specs,
        "unresolved_output_mapping_states": unresolved_output_mapping_states,
        "invalid_output_mapping_specs": invalid_output_mapping_specs,
        "subworkflow_contract_issues": subworkflow_contract_issues,
        "control_signal_issues": control_signal_issues,
        "fork_join_issues": fork_join_issues,
        "iterator_issues": iterator_issues,
        "approval_gate_issues": approval_gate_issues,
        "runtime_policy_issues": runtime_policy_issues,
        "plan_state_issues": plan_state_issues,
        "completion_gate_issues": completion_gate_issues,
        "prompt_contract_issues": prompt_contract_issues,
    }


__all__ = [
    "WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION",
    "WORKFLOW_DEFINITION_IDENTITY_VERSION",
    "build_workflow_definition_identity",
    "build_workflow_definition_identity_from_graph",
    "collect_workflow_action_ids",
    "validate_workflow_definition_contract",
]
