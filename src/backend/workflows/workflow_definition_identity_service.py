"""Workflow definition identity and strict contract validation helpers.

This module is the canonical place to:
1. derive deterministic workflow definition identity hashes for monitor/introspection
   surfaces, and
2. enforce strict contract gates for workflow publication/runnability checks.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Mapping, Sequence

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


def collect_workflow_action_ids(definition: Any) -> tuple[str, ...]:
    """Collect unique action IDs referenced by a workflow definition."""

    action_ids: set[str] = set()
    states_raw = getattr(definition, "states", {})
    states = states_raw if isinstance(states_raw, Mapping) else {}
    for state_spec in states.values():
        actions_raw = getattr(state_spec, "actions", ())
        actions = actions_raw if _is_sequence_like(actions_raw) else ()
        for action in actions:
            action_id = str(getattr(action, "action_id", "") or "").strip()
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
        actions = [invokes_action] if invokes_action else []

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

        step_entries.append(
            {
                "state_id": state_id,
                "terminal": terminal,
                "actions": actions,
                "transitions": transitions,
                "metadata": metadata,
            }
        )

    return {
        "schema_version": WORKFLOW_CONTRACT_SHAPE_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "initial_state": str(graph.get("initial_step") or "").strip(),
        "termination_states": sorted(set(termination_states)),
        "metadata": {},
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
        }

    states_raw = getattr(definition, "states", {})
    states = states_raw if isinstance(states_raw, Mapping) else {}
    state_ids = set(str(state_id) for state_id in states.keys())
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

    supported_action_set: set[str] = set()
    if supported_action_ids is not None:
        supported_action_set = {
            str(action_id).strip()
            for action_id in supported_action_ids
            if isinstance(action_id, str) and str(action_id).strip()
        }

    for state_id in sorted(str(item) for item in states.keys()):
        state_spec = states[state_id]
        metadata_raw = getattr(state_spec, "metadata", {})
        metadata = dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
        actions_raw = getattr(state_spec, "actions", ())
        actions = [
            str(getattr(action, "action_id", "") or "").strip()
            for action in (actions_raw if _is_sequence_like(actions_raw) else ())
        ]
        actions = [item for item in actions if item]
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
    }


__all__ = [
    "WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION",
    "WORKFLOW_DEFINITION_IDENTITY_VERSION",
    "build_workflow_definition_identity",
    "build_workflow_definition_identity_from_graph",
    "collect_workflow_action_ids",
    "validate_workflow_definition_contract",
]
