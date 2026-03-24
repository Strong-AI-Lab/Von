"""Generic bootstrap helpers for authored workflow source assets."""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from .text_value_service import upsert_singleton_text_relation
from .workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from .workflow_vontology_materialisation_helpers import ensure_instance_typing
from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows.vontology_loader import (
    build_workflow_process_graph,
    load_workflow_definition_from_vontology,
)
from ..workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)

_WORKFLOW_STEP_TYPE_ID = "#V#workflow_step"


def _stable_state_metadata_subset(state: Any) -> dict[str, Any]:
    metadata = getattr(state, "metadata", None)
    if not isinstance(metadata, dict):
        return {}
    comparable: dict[str, Any] = {}
    for key in (
        "invokes_workflow",
        "reads_context_keys",
        "writes_context_keys",
        "tool_output_context_mappings",
        "subworkflow_contract",
        "mutation_authority",
    ):
        value = metadata.get(key)
        if value:
            comparable[key] = value
    return comparable


def _normalise_mapping_spec_signatures(
    mapping_specs: tuple[Any, ...],
) -> tuple[tuple[str, str, str], ...]:
    signatures: list[tuple[str, str, str]] = []
    for spec in mapping_specs:
        concept_id = str(getattr(spec, "concept_id", "") or "").strip()
        left = str(
            getattr(spec, "tool_param", None)
            or getattr(spec, "tool_output_field", None)
            or ""
        ).strip()
        right = str(
            getattr(spec, "context_key", None)
            or ""
        ).strip()
        if concept_id and left and right:
            signatures.append((concept_id, left, right))
    return tuple(sorted(dict.fromkeys(signatures)))


def _normalise_static_input_bindings(
    bindings: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    cleaned = [
        (str(key or "").strip(), str(value or "").strip())
        for key, value in bindings
        if str(key or "").strip() and str(value or "").strip()
    ]
    return tuple(sorted(dict.fromkeys(cleaned)))


def _normalise_string_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            dict.fromkeys(
                str(item or "").strip()
                for item in values
                if str(item or "").strip()
            )
        )
    )


def _prune_shadowed_static_input_bindings(
    *,
    static_input_bindings: tuple[tuple[str, str], ...],
    context_input_mapping_specs: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[str, str], ...]:
    mapped_tool_params = {tool_param for _concept_id, tool_param, _context_key in context_input_mapping_specs}
    if not mapped_tool_params:
        return static_input_bindings
    return tuple(
        binding
        for binding in static_input_bindings
        if binding[0] not in mapped_tool_params
    )


def _step_concept_id_by_state(
    *,
    workflow_id: str,
    spec: Any,
) -> dict[str, str]:
    ordered_ids = authority_service.publication_spec_step_concept_ids(
        workflow_id=workflow_id,
        spec=spec,
    )
    return {
        step.state_id: step_concept_id
        for step, step_concept_id in zip(spec.steps, ordered_ids)
        if str(getattr(step, "state_id", "") or "").strip() and step_concept_id
    }


def _resolve_loaded_state_entry(
    *,
    workflow_id: str,
    loaded_definition: Any,
    step: Any,
    step_concept_id_by_state: dict[str, str],
) -> tuple[str, Any] | None:
    state_id = str(getattr(step, "state_id", "") or "").strip()
    if not state_id:
        return None
    states_raw = getattr(loaded_definition, "states", None)
    states = states_raw if isinstance(states_raw, dict) else {}
    loaded_state = states.get(state_id)
    if loaded_state is not None:
        return state_id, loaded_state
    explicit_concept_id = str(getattr(step, "concept_id", "") or "").strip()
    if explicit_concept_id:
        loaded_state = states.get(explicit_concept_id)
        if loaded_state is not None:
            return explicit_concept_id, loaded_state
    computed_concept_id = step_concept_id_by_state.get(state_id)
    if computed_concept_id:
        loaded_state = states.get(computed_concept_id)
        if loaded_state is not None:
            return computed_concept_id, loaded_state
        default_concept_id = authority_service._step_concept_id(
            workflow_id=workflow_id,
            state_id=state_id,
        )
        loaded_state = states.get(default_concept_id)
        if loaded_state is not None:
            return default_concept_id, loaded_state
    return None


def _stable_loaded_action_surface(
    *,
    workflow_id: str,
    state_id: str,
    loaded_state: Any,
    loaded_definition: Any,
) -> dict[str, Any]:
    actions = tuple(getattr(loaded_state, "actions", ()) or ())
    if len(actions) > 1:
        return {"invalid_action_count": len(actions)}
    action = actions[0] if actions else None
    runtime_details = authority_service._extract_runtime_step_publication_details(
        workflow_id=workflow_id,
        state_id=state_id,
        registration_definition=loaded_definition,
    )
    context_input_mapping_specs = _normalise_mapping_spec_signatures(
        runtime_details.context_input_mapping_specs
    )
    static_input_bindings = _prune_shadowed_static_input_bindings(
        static_input_bindings=_normalise_static_input_bindings(
            runtime_details.static_input_bindings
        ),
        context_input_mapping_specs=context_input_mapping_specs,
    )
    return {
        "action_id": (
            str(getattr(action, "action_id", "") or "").strip() or None
            if action is not None
            else None
        ),
        "action_concept_id": (
            str(getattr(action, "contract_concept_id", "") or "").strip() or None
            if action is not None
            else None
        ),
        "execution_mode": (
            str(getattr(action, "execution_mode", "") or "").strip() or None
            if action is not None
            else None
        ),
        "prompt_concept_ids": authority_service._extract_publication_prompt_concept_ids(
            action
        )
        if action is not None
        else (),
        "llm_policy": (
            dict(runtime_details.llm_policy)
            if isinstance(runtime_details.llm_policy, dict)
            else None
        ),
        "validation_policy": (
            dict(runtime_details.validation_policy)
            if isinstance(runtime_details.validation_policy, dict)
            else None
        ),
        "mutation_authority": (
            dict(runtime_details.mutation_authority)
            if isinstance(runtime_details.mutation_authority, dict)
            else None
        ),
        "invoked_workflow_id": (
            str(runtime_details.invoked_workflow_id or "").strip() or None
        ),
        "static_input_bindings": static_input_bindings,
        "context_input_mapping_specs": context_input_mapping_specs,
        "tool_output_mapping_specs": _normalise_mapping_spec_signatures(
            runtime_details.tool_output_mapping_specs
        ),
        "writes_context_keys": _normalise_string_tuple(
            runtime_details.writes_context_keys
        ),
    }


def _stable_expected_action_surface(step: Any) -> dict[str, Any]:
    action_id = str(getattr(step, "action_id", "") or "").strip() or None
    execution_mode = str(getattr(step, "execution_mode", "") or "").strip() or None
    if action_id and not execution_mode:
        execution_mode = "deterministic"
    context_input_mapping_specs = _normalise_mapping_spec_signatures(
        getattr(step, "context_input_mapping_specs", ()) or ()
    )
    static_input_bindings = _prune_shadowed_static_input_bindings(
        static_input_bindings=_normalise_static_input_bindings(
            getattr(step, "static_input_bindings", ()) or ()
        ),
        context_input_mapping_specs=context_input_mapping_specs,
    )
    return {
        "action_id": action_id,
        "action_concept_id": (
            str(getattr(step, "action_concept_id", "") or "").strip() or None
        ),
        "execution_mode": execution_mode,
        "prompt_concept_ids": tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in getattr(step, "prompt_concept_ids", ()) or ()
                if str(item or "").strip()
            )
        ),
        "llm_policy": (
            dict(step.llm_policy)
            if isinstance(getattr(step, "llm_policy", None), dict)
            else None
        ),
        "validation_policy": (
            dict(step.validation_policy)
            if isinstance(getattr(step, "validation_policy", None), dict)
            else None
        ),
        "mutation_authority": (
            dict(step.mutation_authority)
            if isinstance(getattr(step, "mutation_authority", None), dict)
            else None
        ),
        "invoked_workflow_id": (
            str(getattr(step, "invoked_workflow_id", "") or "").strip() or None
        ),
        "static_input_bindings": static_input_bindings,
        "context_input_mapping_specs": context_input_mapping_specs,
        "tool_output_mapping_specs": _normalise_mapping_spec_signatures(
            getattr(step, "tool_output_mapping_specs", ()) or ()
        ),
        "writes_context_keys": _normalise_string_tuple(
            getattr(step, "writes_context_keys", ()) or ()
        ),
    }


def _stable_expected_state_metadata_subset(
    *,
    workflow_id: str,
    state_id: str,
    step: Any,
) -> dict[str, Any]:
    synthetic_definition = authority_service._build_definition_from_publication_spec(
        workflow_id=workflow_id,
        spec=authority_service._CanonicalWorkflowPublicationSpec(
            initial_state=state_id,
            steps=(step,),
        ),
    )
    return _stable_state_metadata_subset(synthetic_definition.states[state_id])


def _action_surfaces_match(
    *,
    loaded_surface: dict[str, Any],
    expected_surface: dict[str, Any],
) -> bool:
    exact_match_keys = (
        "action_id",
        "action_concept_id",
        "execution_mode",
        "prompt_concept_ids",
        "invoked_workflow_id",
        "static_input_bindings",
        "context_input_mapping_specs",
        "tool_output_mapping_specs",
        "writes_context_keys",
    )
    for key in exact_match_keys:
        if loaded_surface.get(key) != expected_surface.get(key):
            return False

    subset_match_keys = (
        "llm_policy",
        "validation_policy",
        "mutation_authority",
    )
    for key in subset_match_keys:
        expected_value = expected_surface.get(key)
        if expected_value is None:
            continue
        if key not in loaded_surface or not _materialisation_value_matches(
            loaded_value=loaded_surface.get(key),
            expected_value=expected_value,
        ):
            return False
    return True


def _state_reference_matches(
    *,
    workflow_id: str,
    expected_state_id: str,
    actual_state_ref: Any,
    step_concept_id_by_state: dict[str, str],
) -> bool:
    actual = str(actual_state_ref or "").strip()
    if not actual:
        return False
    expected_refs = {
        expected_state_id,
        authority_service._step_concept_id(
            workflow_id=workflow_id,
            state_id=expected_state_id,
        ),
    }
    expected_concept_id = step_concept_id_by_state.get(expected_state_id)
    if expected_concept_id:
        expected_refs.add(expected_concept_id)
    return actual in expected_refs


def _normalise_condition_spec(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {"kind": "always"}


def _stable_loaded_transition_surface(loaded_state: Any) -> list[dict[str, Any]]:
    transitions = []
    for transition in tuple(getattr(loaded_state, "transitions", ()) or ()):
        transitions.append(
            {
                "to_state": str(getattr(transition, "to_state", "") or "").strip(),
                "reason": str(getattr(transition, "reason", "") or "").strip() or None,
                "condition_spec": _normalise_condition_spec(
                    getattr(transition, "condition_spec", None)
                ),
            }
        )
    return transitions


def _stable_expected_transition_surface(step: Any) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    for branch in getattr(step, "conditional_transitions", ()) or ():
        transitions.append(
            {
                "to_state": str(getattr(branch, "to_state", "") or "").strip(),
                "reason": str(getattr(branch, "reason", "") or "").strip() or None,
                "condition_spec": _normalise_condition_spec(
                    getattr(branch, "condition_spec", None)
                ),
            }
        )
    for attribute_name, reason, condition_spec in (
        ("on_failure_state", "on_failure", {"kind": "context_flag", "key": "last_action_failed", "expected": True}),
        ("on_unknown_state", "on_unknown", {"kind": "context_flag", "key": "last_action_unknown", "expected": True}),
        ("on_approval_required_state", "on_approval_required", {"kind": "context_flag", "key": "approval_required", "expected": True}),
        ("on_break_state", "on_break", {"kind": "control_signal", "signal": "break"}),
        ("on_continue_state", "on_continue", {"kind": "control_signal", "signal": "continue"}),
        ("on_true_state", "on_true", {"kind": "transition_result_truth", "expected": True}),
        ("on_false_state", "on_false", {"kind": "transition_result_truth", "expected": False}),
        ("next_state", "next_step", {"kind": "always"}),
    ):
        target_state = str(getattr(step, attribute_name, "") or "").strip()
        if target_state:
            transitions.append(
                {
                    "to_state": target_state,
                    "reason": reason,
                    "condition_spec": condition_spec,
                }
            )
    return transitions


def _transition_surfaces_match(
    *,
    workflow_id: str,
    step: Any,
    loaded_state: Any,
    step_concept_id_by_state: dict[str, str],
) -> bool:
    expected_transitions = _stable_expected_transition_surface(step)
    loaded_transitions = _stable_loaded_transition_surface(loaded_state)
    if len(expected_transitions) != len(loaded_transitions):
        return False

    unmatched = list(loaded_transitions)
    for expected in expected_transitions:
        match_index = next(
            (
                index
                for index, candidate in enumerate(unmatched)
                if candidate.get("reason") == expected.get("reason")
                and _materialisation_value_matches(
                    loaded_value=candidate.get("condition_spec"),
                    expected_value=expected.get("condition_spec"),
                )
                and _state_reference_matches(
                    workflow_id=workflow_id,
                    expected_state_id=str(expected.get("to_state") or "").strip(),
                    actual_state_ref=candidate.get("to_state"),
                    step_concept_id_by_state=step_concept_id_by_state,
                )
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched.pop(match_index)
    return not unmatched


def _materialisation_value_matches(*, loaded_value: Any, expected_value: Any) -> bool:
    if loaded_value == expected_value:
        return True
    if isinstance(loaded_value, dict) and isinstance(expected_value, dict):
        for key, value in expected_value.items():
            if key not in loaded_value:
                return False
            if not _materialisation_value_matches(
                loaded_value=loaded_value.get(key),
                expected_value=value,
            ):
                return False
        return True
    if isinstance(loaded_value, list) and isinstance(expected_value, list):
        if len(loaded_value) != len(expected_value):
            return False
        return all(
            _materialisation_value_matches(
                loaded_value=item,
                expected_value=expected,
            )
            for item, expected in zip(loaded_value, expected_value)
        )
    return False


def _materialisation_matches_publication_spec(
    *,
    loaded_definition: Any,
    workflow_id: str,
    publication_spec: Any,
) -> bool:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        workflow_id = str(getattr(loaded_definition, "workflow_id", "") or "").strip()
    if not workflow_id:
        return False
    steps = tuple(getattr(publication_spec, "steps", ()) or ())
    step_concept_id_by_state = _step_concept_id_by_state(
        workflow_id=workflow_id,
        spec=publication_spec,
    )
    initial_state = str(getattr(publication_spec, "initial_state", "") or "").strip()
    if initial_state and not _state_reference_matches(
        workflow_id=workflow_id,
        expected_state_id=initial_state,
        actual_state_ref=getattr(loaded_definition, "initial_state", None),
        step_concept_id_by_state=step_concept_id_by_state,
    ):
        return False

    for step in steps:
        state_id = str(getattr(step, "state_id", "") or "").strip()
        if not state_id:
            return False
        loaded_state_entry = _resolve_loaded_state_entry(
            workflow_id=workflow_id,
            loaded_definition=loaded_definition,
            step=step,
            step_concept_id_by_state=step_concept_id_by_state,
        )
        if loaded_state_entry is None:
            return False
        loaded_state_id, loaded_state = loaded_state_entry
        expected_publication = _stable_expected_action_surface(step)
        loaded_surface = _stable_loaded_action_surface(
            workflow_id=workflow_id,
            state_id=loaded_state_id,
            loaded_state=loaded_state,
            loaded_definition=loaded_definition,
        )
        if not _action_surfaces_match(
            loaded_surface=loaded_surface,
            expected_surface=expected_publication,
        ):
            return False
        expected = _stable_expected_state_metadata_subset(
            workflow_id=workflow_id,
            state_id=state_id,
            step=step,
        )
        if not expected:
            expected = {}
        loaded_metadata = _stable_state_metadata_subset(loaded_state)
        if expected and any(
            key not in loaded_metadata
            or not _materialisation_value_matches(
                loaded_value=loaded_metadata.get(key),
                expected_value=value,
            )
            for key, value in expected.items()
        ):
            return False
        if not _transition_surfaces_match(
            workflow_id=workflow_id,
            step=step,
            loaded_state=loaded_state,
            step_concept_id_by_state=step_concept_id_by_state,
        ):
            return False
    return True


def _validate_existing_materialisation(
    *,
    target_workflow_ids: tuple[str, ...],
    supported_action_ids: tuple[str, ...],
    publication_specs: dict[str, Any],
) -> tuple[bool, dict[str, dict[str, Any]]]:
    if not target_workflow_ids:
        return False, {}

    cached_definitions: dict[str, Any | None] = {}
    validation_by_workflow_id: dict[str, dict[str, Any]] = {}

    def _cached_loader(candidate_workflow_id: str) -> Any | None:
        workflow_id = str(candidate_workflow_id or "").strip()
        if not workflow_id:
            return None
        if workflow_id not in cached_definitions:
            cached_definitions[workflow_id] = load_workflow_definition_from_vontology(
                workflow_id
            )
        return cached_definitions[workflow_id]

    for workflow_id in target_workflow_ids:
        graph, graph_warnings = build_workflow_process_graph(workflow_id)
        if not isinstance(graph, dict):
            return False, {}
        warning_items = [
            str(item).strip()
            for item in (graph_warnings or [])
            if isinstance(item, str) and str(item).strip()
        ]
        if warning_items:
            return False, {}

        definition = _cached_loader(workflow_id)
        if definition is None:
            return False, {}

        publication_spec = publication_specs.get(workflow_id)
        if publication_spec is not None and not _materialisation_matches_publication_spec(
            loaded_definition=definition,
            workflow_id=workflow_id,
            publication_spec=publication_spec,
        ):
            return False, {}

        validation = validate_workflow_definition_contract(
            definition=definition,
            supported_action_ids=supported_action_ids,
            known_workflow_ids=target_workflow_ids,
            workflow_definition_loader=_cached_loader,
        )
        validation_by_workflow_id[workflow_id] = copy.deepcopy(validation)
        if not bool(validation.get("valid")):
            return False, {}

    return True, validation_by_workflow_id


def _upsert_text_relations(
    *,
    subject_concept_id: str,
    relation_specs: tuple[dict[str, Any], ...],
    source_tag: str | None,
    managed_by: str | None,
    workflow_id: str,
    state_id: str | None = None,
) -> None:
    shared_context = {
        "workflow_id": workflow_id,
    }
    if source_tag:
        shared_context["source"] = source_tag
    if managed_by:
        shared_context["managed_by"] = managed_by
    if state_id:
        shared_context["state_id"] = state_id
        shared_context["workflow_step_id"] = subject_concept_id

    for spec in relation_specs:
        predicate = str(spec.get("predicate") or "").strip()
        text = str(spec.get("text") or "").strip()
        lang = str(spec.get("lang") or "en-NZ").strip() or "en-NZ"
        if not predicate or not text:
            continue
        upsert_singleton_text_relation(
            subject_concept_id=subject_concept_id,
            predicate=predicate,
            text=text,
            lang=lang,
            context=dict(shared_context),
            garbage_collect=True,
        )


def bootstrap_authored_workflow_source_bundle(
    *,
    asset_path: str | Path,
    publish_context_manager_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Publish and validate one authored workflow source bundle."""

    bundle = authority_service.load_authored_workflow_source_bundle(asset_path)
    managed_by = str(bundle.get("managed_by") or "").strip() or None
    source_tag = str(bundle.get("source_tag") or "").strip() or None
    publication_specs = dict(bundle.get("publication_specs") or {})
    publication_purposes = dict(bundle.get("publication_purposes") or {})
    workflow_type_ids = dict(bundle.get("workflow_type_ids") or {})
    workflow_text_relations = dict(bundle.get("workflow_text_relations") or {})
    workflow_launch_contracts = dict(bundle.get("workflow_launch_contracts") or {})
    step_text_relations = dict(bundle.get("step_text_relations") or {})
    supported_action_ids = tuple(bundle.get("supported_action_ids") or ())
    target_workflow_ids = tuple(publication_specs.keys())
    already_current, existing_validation_by_workflow_id = (
        _validate_existing_materialisation(
            target_workflow_ids=target_workflow_ids,
            supported_action_ids=supported_action_ids,
            publication_specs=publication_specs,
        )
    )

    typed_workflow_ids: list[str] = []
    typed_step_ids: list[str] = []
    validation_by_workflow_id: dict[str, dict[str, Any]] = {}
    if already_current:
        validation_by_workflow_id.update(existing_validation_by_workflow_id)

    context_manager_factory = publish_context_manager_factory or nullcontext
    with context_manager_factory():
        publication_report: dict[str, Any]
        if already_current:
            publication_report = {
                "counts": {
                    "workflows_targeted": len(target_workflow_ids),
                    "workflows_published": 0,
                    "workflows_skipped_missing_registration": 0,
                    "workflows_skipped_missing_concept": 0,
                    "step_concepts_created": 0,
                    "action_concepts_created": 0,
                    "mapping_concepts_created": 0,
                    "validation_failures": 0,
                    "errors": 0,
                },
                "published_workflow_ids": [],
                "skipped_due_to_current_materialisation": list(target_workflow_ids),
                "skip_reason": "existing_materialisation_valid",
                "skipped": True,
            }
        else:
            publication_report = authority_service.publish_canonical_chat_workflow_graphs(
                target_workflow_ids=target_workflow_ids,
                publication_specs=publication_specs,
                publication_definitions=authority_service._build_definition_map_from_publication_specs(
                    publication_specs
                ),
                publication_purposes=publication_purposes,
            )

        for workflow_id, spec in publication_specs.items():
            type_ids = tuple(workflow_type_ids.get(workflow_id) or ())
            if type_ids and ensure_instance_typing(
                concept_id=workflow_id,
                type_ids=type_ids,
                remove_type_parent_ids=type_ids,
            ):
                typed_workflow_ids.append(workflow_id)

            relation_specs = tuple(workflow_text_relations.get(workflow_id) or ())
            if relation_specs:
                _upsert_text_relations(
                    subject_concept_id=workflow_id,
                    relation_specs=relation_specs,
                    source_tag=source_tag,
                    managed_by=managed_by,
                    workflow_id=workflow_id,
                )

            launch_contract = workflow_launch_contracts.get(workflow_id)
            if isinstance(launch_contract, dict):
                upsert_singleton_text_relation(
                    subject_concept_id=workflow_id,
                    predicate="#V#hasWorkflowLaunchInputContractJson",
                    text=json.dumps(launch_contract, ensure_ascii=True, sort_keys=True),
                    lang="en-NZ",
                    context={
                        "workflow_id": workflow_id,
                        **({"source": source_tag} if source_tag else {}),
                        **({"managed_by": managed_by} if managed_by else {}),
                    },
                    garbage_collect=True,
                )

            for step, step_concept_id in zip(
                spec.steps,
                authority_service.publication_spec_step_concept_ids(
                    workflow_id=workflow_id,
                    spec=spec,
                ),
            ):
                if ensure_instance_typing(
                    concept_id=step_concept_id,
                    type_ids=(_WORKFLOW_STEP_TYPE_ID,),
                ):
                    typed_step_ids.append(step_concept_id)
                step_relation_specs = tuple(step_text_relations.get(step_concept_id) or ())
                if step_relation_specs:
                    _upsert_text_relations(
                        subject_concept_id=step_concept_id,
                        relation_specs=step_relation_specs,
                        source_tag=source_tag,
                        managed_by=managed_by,
                        workflow_id=workflow_id,
                        state_id=str(getattr(step, "state_id", "") or "").strip() or None,
                    )

            validation = validation_by_workflow_id.get(workflow_id)
            if already_current:
                if not isinstance(validation, dict):
                    raise RuntimeError(
                        "authored_workflow_validation_missing_after_short_circuit:"
                        f"{workflow_id}"
                    )
            else:
                graph, graph_warnings = build_workflow_process_graph(workflow_id)
                if not isinstance(graph, dict):
                    raise RuntimeError(
                        f"authored_workflow_graph_missing:{workflow_id}"
                    )
                warning_items = [
                    str(item).strip()
                    for item in (graph_warnings or [])
                    if isinstance(item, str) and str(item).strip()
                ]
                if warning_items:
                    raise RuntimeError(
                        "authored_workflow_graph_warnings_present:"
                        f"{workflow_id}:"
                        + ",".join(warning_items)
                    )

                definition = load_workflow_definition_from_vontology(workflow_id)
                if definition is None:
                    raise RuntimeError(
                        f"authored_workflow_definition_not_loadable:{workflow_id}"
                    )
                validation = validate_workflow_definition_contract(
                    definition=definition,
                    supported_action_ids=supported_action_ids,
                    known_workflow_ids=target_workflow_ids,
                    workflow_definition_loader=load_workflow_definition_from_vontology,
                )
                validation_by_workflow_id[workflow_id] = copy.deepcopy(validation)

                if not bool(validation.get("valid")):
                    raise RuntimeError(
                        "authored_workflow_definition_invalid:"
                        f"{workflow_id}:"
                        + ",".join(
                            str(item).strip()
                            for item in (validation.get("errors") or [])
                            if isinstance(item, str) and str(item).strip()
                        )
                    )

    invalidate_workflow_discovery_executability_caches()
    return {
        "asset_path": str(bundle.get("asset_path") or Path(asset_path)),
        "family_id": bundle.get("family_id"),
        "workflow_ids": list(target_workflow_ids),
        "publication": publication_report,
        "typed_workflow_ids": typed_workflow_ids,
        "typed_step_ids": typed_step_ids,
        "validation_by_workflow_id": validation_by_workflow_id,
    }


__all__ = ["bootstrap_authored_workflow_source_bundle"]
