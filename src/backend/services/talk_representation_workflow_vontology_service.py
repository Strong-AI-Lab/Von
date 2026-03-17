"""Materialise canonical talk representation workflows in Vontology."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from .text_value_service import upsert_singleton_text_relation
from .talk_representation_service import ensure_talk_representation_primitives
from .workflow_discovery_service import invalidate_workflow_discovery_executability_caches
from .workflow_vontology_materialisation_helpers import ensure_instance_typing
from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows.durable.subworkflow_actions import WORKFLOW_SUBWORKFLOW_ACTION_ID
from ..workflows.durable.talk_representation_workflow import (
    TALK_MATERIALISE_ACTION_ID,
    TALK_NORMALISE_INPUTS_ACTION_ID,
    TALK_VERIFY_ACTION_ID,
)
from ..workflows.vontology_loader import (
    build_workflow_process_graph,
    load_workflow_definition_from_vontology,
)
from ..workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)

TALK_REPRESENTATION_WORKFLOW_ID = "#V#talk_representation_workflow"
TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID = (
    "#V#technical_scientific_talk_representation_workflow"
)
ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID = "#V#academic_presentation_instance_workflow"

_WORKFLOW_STEP_TYPE_ID = "#V#workflow_step"
_WORKFLOW_TYPE_IDS = ("#V#ai_workflow", "#V#durable_workflow")
_TALK_CONTEXT_KEYS: tuple[str, ...] = (
    "presentation_concept_id",
    "title",
    "speaker_name",
    "summary",
    "start_time",
    "meeting_link",
    "meeting_id",
    "meeting_passcode",
    "presentation_status",
    "presentation_type_ids",
    "verification_profile",
)


def _context_mapping(
    *,
    workflow_id: str,
    state_id: str,
    tool_param: str,
    context_key: str,
) -> authority_service._CanonicalContextInputMappingSpec:
    return authority_service._CanonicalContextInputMappingSpec(
        concept_id=authority_service._runtime_context_input_mapping_concept_id(
            workflow_id=workflow_id,
            state_id=state_id,
            tool_param=tool_param,
            context_key=context_key,
        ),
        context_key=context_key,
        tool_param=tool_param,
    )


def _output_mapping(
    *,
    workflow_id: str,
    state_id: str,
    tool_output_field: str,
    context_key: str,
) -> authority_service._CanonicalToolOutputMappingSpec:
    return authority_service._CanonicalToolOutputMappingSpec(
        concept_id=authority_service._runtime_tool_output_mapping_concept_id(
            workflow_id=workflow_id,
            state_id=state_id,
            tool_output_field=tool_output_field,
            context_key=context_key,
        ),
        tool_output_field=tool_output_field,
        context_key=context_key,
    )


def _workflow_name(workflow_id: str) -> str:
    if workflow_id == TALK_REPRESENTATION_WORKFLOW_ID:
        return "Talk Representation Workflow"
    if workflow_id == TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID:
        return "Technical Scientific Talk Representation Workflow"
    if workflow_id == ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID:
        return "Academic Presentation Instance Workflow"
    return authority_service._titleise_workflow_id(workflow_id)


def _workflow_description(workflow_id: str) -> str:
    if workflow_id == TALK_REPRESENTATION_WORKFLOW_ID:
        return (
            "Canonical durable workflow for representing talks and presentations in "
            "Vontology from structured meeting and speaker inputs."
        )
    if workflow_id == TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID:
        return (
            "Wrapper workflow that delegates to the canonical talk workflow with "
            "technical/scientific talk typing and verification requirements."
        )
    return (
        "Wrapper workflow that delegates to the canonical talk workflow with "
        "academic presentation typing and seminar verification requirements."
    )


def _workflow_content(workflow_id: str) -> str:
    if workflow_id == TALK_REPRESENTATION_WORKFLOW_ID:
        return (
            "Represent a talk by normalising presentation inputs, materialising or "
            "reusing the presentation concept, linking the presenter, and verifying "
            "the requested talk representation profile."
        )
    if workflow_id == TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID:
        return (
            "Represent a technical or scientific talk by delegating to the canonical "
            "talk workflow with scientific presentation typing."
        )
    return (
        "Represent an academic presentation instance by delegating to the canonical "
        "talk workflow with scientific presentation and seminar typing."
    )


def _workflow_capability_gap_note(workflow_id: str) -> str:
    return (
        "Capability-gap note: this workflow depends on Python durable actions "
        f"because current VWL/runtime primitives cannot yet express a reusable, "
        "deterministic representation-materialisation primitive for concept "
        f"creation/update plus postcondition verification. Workflow {workflow_id} "
        "should migrate fully into VWL once the runtime can declaratively express "
        "stable concept reuse, singleton text upserts, relationship writes, and "
        "verification against required representation predicates."
    )


def _workflow_supported_action_ids() -> tuple[str, ...]:
    return (
        TALK_NORMALISE_INPUTS_ACTION_ID,
        TALK_MATERIALISE_ACTION_ID,
        TALK_VERIFY_ACTION_ID,
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
    )


def _base_talk_context_input_mappings(
    *,
    workflow_id: str,
    state_id: str,
) -> tuple[authority_service._CanonicalContextInputMappingSpec, ...]:
    return tuple(
        _context_mapping(
            workflow_id=workflow_id,
            state_id=state_id,
            tool_param=context_key,
            context_key=context_key,
        )
        for context_key in _TALK_CONTEXT_KEYS
    )


def _build_talk_workflow_spec() -> authority_service._CanonicalWorkflowPublicationSpec:
    workflow_id = TALK_REPRESENTATION_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="normalise_inputs",
        steps=(
            authority_service._CanonicalStepPublicationSpec(
                state_id="normalise_inputs",
                action_id=TALK_NORMALISE_INPUTS_ACTION_ID,
                context_input_mapping_specs=_base_talk_context_input_mappings(
                    workflow_id=workflow_id,
                    state_id="normalise_inputs",
                ),
                tool_output_mapping_specs=tuple(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field=context_key,
                        context_key=context_key,
                    )
                    for context_key in _TALK_CONTEXT_KEYS
                )
                + (
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="normalise_inputs",
                        tool_output_field="normalised_talk_inputs",
                        context_key="normalised_talk_inputs",
                    ),
                ),
                writes_context_keys=_TALK_CONTEXT_KEYS + ("normalised_talk_inputs",),
                on_failure_state="failed",
                next_state="materialise_representation",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="materialise_representation",
                action_id=TALK_MATERIALISE_ACTION_ID,
                context_input_mapping_specs=_base_talk_context_input_mappings(
                    workflow_id=workflow_id,
                    state_id="materialise_representation",
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_representation",
                        tool_output_field="presentation_concept_id",
                        context_key="presentation_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_representation",
                        tool_output_field="presentation_display_name",
                        context_key="presentation_display_name",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_representation",
                        tool_output_field="speaker_concept_id",
                        context_key="speaker_concept_id",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_representation",
                        tool_output_field="presentation_type_ids",
                        context_key="presentation_type_ids",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_representation",
                        tool_output_field="verification_profile",
                        context_key="verification_profile",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_representation",
                        tool_output_field="talk_representation_materialised",
                        context_key="talk_representation_materialised",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="materialise_representation",
                        tool_output_field="materialisation_report",
                        context_key="materialisation_report",
                    ),
                ),
                writes_context_keys=(
                    "presentation_concept_id",
                    "presentation_display_name",
                    "speaker_concept_id",
                    "presentation_type_ids",
                    "verification_profile",
                    "talk_representation_materialised",
                    "materialisation_report",
                ),
                on_failure_state="failed",
                next_state="verify_representation",
            ),
            authority_service._CanonicalStepPublicationSpec(
                state_id="verify_representation",
                action_id=TALK_VERIFY_ACTION_ID,
                context_input_mapping_specs=(
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="presentation_concept_id",
                        context_key="presentation_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="speaker_concept_id",
                        context_key="speaker_concept_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="summary",
                        context_key="summary",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="start_time",
                        context_key="start_time",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="meeting_link",
                        context_key="meeting_link",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="meeting_id",
                        context_key="meeting_id",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="meeting_passcode",
                        context_key="meeting_passcode",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="presentation_status",
                        context_key="presentation_status",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="presentation_type_ids",
                        context_key="presentation_type_ids",
                    ),
                    _context_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_param="verification_profile",
                        context_key="verification_profile",
                    ),
                ),
                tool_output_mapping_specs=(
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_output_field="talk_representation_verified",
                        context_key="talk_representation_verified",
                    ),
                    _output_mapping(
                        workflow_id=workflow_id,
                        state_id="verify_representation",
                        tool_output_field="verification_failures",
                        context_key="verification_failures",
                    ),
                ),
                writes_context_keys=(
                    "talk_representation_verified",
                    "verification_failures",
                ),
                on_failure_state="failed",
                on_true_state="completed",
                on_false_state="failed",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="completed"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _build_wrapper_delegate_step(
    *,
    workflow_id: str,
    state_id: str,
) -> authority_service._CanonicalStepPublicationSpec:
    return authority_service._CanonicalStepPublicationSpec(
        state_id=state_id,
        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
        invoked_workflow_id=TALK_REPRESENTATION_WORKFLOW_ID,
        context_input_mapping_specs=tuple(
            _context_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_param=context_key,
                context_key=context_key,
            )
            for context_key in (
                "presentation_concept_id",
                "title",
                "speaker_name",
                "summary",
                "start_time",
                "meeting_link",
                "meeting_id",
                "meeting_passcode",
                "presentation_status",
                "presentation_type_ids",
                "verification_profile",
            )
        ),
        tool_output_mapping_specs=(
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.presentation_concept_id",
                context_key="presentation_concept_id",
            ),
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.presentation_display_name",
                context_key="presentation_display_name",
            ),
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.speaker_concept_id",
                context_key="speaker_concept_id",
            ),
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.presentation_type_ids",
                context_key="presentation_type_ids",
            ),
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.verification_profile",
                context_key="verification_profile",
            ),
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.talk_representation_materialised",
                context_key="talk_representation_materialised",
            ),
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.talk_representation_verified",
                context_key="talk_representation_verified",
            ),
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.verification_failures",
                context_key="verification_failures",
            ),
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="result.materialisation_report",
                context_key="materialisation_report",
            ),
        ),
        writes_context_keys=(
            "presentation_concept_id",
            "presentation_display_name",
            "speaker_concept_id",
            "presentation_type_ids",
            "verification_profile",
            "talk_representation_materialised",
            "talk_representation_verified",
            "verification_failures",
            "materialisation_report",
        ),
        on_failure_state="failed",
        next_state="completed",
    )


def _build_wrapper_normalise_step(
    *,
    workflow_id: str,
    state_id: str,
    verification_profile: str,
    default_type_ids: str,
) -> authority_service._CanonicalStepPublicationSpec:
    return authority_service._CanonicalStepPublicationSpec(
        state_id=state_id,
        action_id=TALK_NORMALISE_INPUTS_ACTION_ID,
        static_input_bindings=(
            ("verification_profile", verification_profile),
            ("presentation_type_ids", default_type_ids),
        ),
        tool_output_mapping_specs=tuple(
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field=context_key,
                context_key=context_key,
            )
            for context_key in _TALK_CONTEXT_KEYS
        )
        + (
            _output_mapping(
                workflow_id=workflow_id,
                state_id=state_id,
                tool_output_field="normalised_talk_inputs",
                context_key="normalised_talk_inputs",
            ),
        ),
        writes_context_keys=_TALK_CONTEXT_KEYS + ("normalised_talk_inputs",),
        on_failure_state="failed",
        next_state="delegate_to_talk_representation",
    )


def _build_technical_scientific_talk_workflow_spec() -> (
    authority_service._CanonicalWorkflowPublicationSpec
):
    workflow_id = TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="normalise_inputs",
        steps=(
            _build_wrapper_normalise_step(
                workflow_id=workflow_id,
                state_id="normalise_inputs",
                verification_profile="technical_scientific_talk",
                default_type_ids="#V#presentation,#V#scientific_presentation",
            ),
            _build_wrapper_delegate_step(
                workflow_id=workflow_id,
                state_id="delegate_to_talk_representation",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="completed"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _build_academic_presentation_workflow_spec() -> (
    authority_service._CanonicalWorkflowPublicationSpec
):
    workflow_id = ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID
    return authority_service._CanonicalWorkflowPublicationSpec(
        initial_state="normalise_inputs",
        steps=(
            _build_wrapper_normalise_step(
                workflow_id=workflow_id,
                state_id="normalise_inputs",
                verification_profile="academic_presentation",
                default_type_ids="#V#presentation,#V#scientific_presentation,#V#seminar",
            ),
            _build_wrapper_delegate_step(
                workflow_id=workflow_id,
                state_id="delegate_to_talk_representation",
            ),
            authority_service._CanonicalStepPublicationSpec(state_id="completed"),
            authority_service._CanonicalStepPublicationSpec(state_id="failed"),
        ),
    )


def _ensure_workflow_texts(workflow_id: str) -> None:
    shared_context = {
        "source": "JVNAUTOSCI-1459",
        "workflow_id": workflow_id,
        "managed_by": "talk_representation_workflow_vontology_service",
    }
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate="hasDescription",
        text=_workflow_description(workflow_id),
        lang="en-NZ",
        context=dict(shared_context),
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate="hasContent",
        text=_workflow_content(workflow_id),
        lang="en-NZ",
        context=dict(shared_context),
        garbage_collect=True,
    )
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id,
        predicate="hasNote",
        text=_workflow_capability_gap_note(workflow_id),
        lang="en-NZ",
        context=dict(shared_context),
        garbage_collect=True,
    )


def _step_concept_ids_for_spec(
    *,
    workflow_id: str,
    spec: authority_service._CanonicalWorkflowPublicationSpec,
) -> tuple[str, ...]:
    ordered_ids: list[str] = []
    for step in spec.steps:
        explicit_concept_id = (
            step.concept_id.strip()
            if isinstance(step.concept_id, str) and step.concept_id.strip()
            else None
        )
        ordered_ids.append(
            explicit_concept_id
            if explicit_concept_id is not None
            else authority_service._step_concept_id(
                workflow_id=workflow_id,
                state_id=step.state_id,
            )
        )
    return tuple(ordered_ids)


def _build_publication_specs() -> dict[
    str,
    authority_service._CanonicalWorkflowPublicationSpec,
]:
    return {
        TALK_REPRESENTATION_WORKFLOW_ID: _build_talk_workflow_spec(),
        TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID: (
            _build_technical_scientific_talk_workflow_spec()
        ),
        ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID: (
            _build_academic_presentation_workflow_spec()
        ),
    }


def _build_publication_purposes(workflow_ids: Sequence[str]) -> dict[str, str]:
    return {
        workflow_id: _workflow_content(workflow_id)
        for workflow_id in workflow_ids
        if isinstance(workflow_id, str) and workflow_id.strip()
    }


def _validate_existing_materialisation(
    *,
    target_workflow_ids: Sequence[str],
) -> tuple[bool, dict[str, dict[str, Any]]]:
    workflow_ids = tuple(
        str(item).strip()
        for item in target_workflow_ids
        if isinstance(item, str) and str(item).strip()
    )
    if not workflow_ids:
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

    supported_action_ids = _workflow_supported_action_ids()
    for workflow_id in workflow_ids:
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

        validation = validate_workflow_definition_contract(
            definition=definition,
            supported_action_ids=supported_action_ids,
            known_workflow_ids=workflow_ids,
            workflow_definition_loader=_cached_loader,
        )
        validation_by_workflow_id[workflow_id] = copy.deepcopy(validation)
        if not bool(validation.get("valid")):
            return False, {}

    return True, validation_by_workflow_id


def bootstrap_canonical_talk_representation_workflows() -> dict[str, Any]:
    """Publish and validate the canonical talk workflow family."""

    ensure_talk_representation_primitives()
    specs = _build_publication_specs()
    target_workflow_ids = tuple(specs.keys())
    publication_definitions = authority_service._build_definition_map_from_publication_specs(
        specs
    )
    publication_purposes = _build_publication_purposes(target_workflow_ids)
    already_current, existing_validation_by_workflow_id = (
        _validate_existing_materialisation(target_workflow_ids=target_workflow_ids)
    )

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
            publication_specs=specs,
            publication_definitions=publication_definitions,
            publication_purposes=publication_purposes,
        )

    typed_workflow_ids: list[str] = []
    typed_step_ids: list[str] = []
    validation_by_workflow_id: dict[str, dict[str, Any]] = {}
    if already_current:
        validation_by_workflow_id.update(existing_validation_by_workflow_id)

    for workflow_id, spec in specs.items():
        if ensure_instance_typing(
            concept_id=workflow_id,
            type_ids=_WORKFLOW_TYPE_IDS,
            remove_type_parent_ids=_WORKFLOW_TYPE_IDS,
        ):
            typed_workflow_ids.append(workflow_id)
        _ensure_workflow_texts(workflow_id)

        for step_concept_id in _step_concept_ids_for_spec(workflow_id=workflow_id, spec=spec):
            if ensure_instance_typing(
                concept_id=step_concept_id,
                type_ids=(_WORKFLOW_STEP_TYPE_ID,),
            ):
                typed_step_ids.append(step_concept_id)

        validation = validation_by_workflow_id.get(workflow_id)
        if already_current:
            if not isinstance(validation, dict):
                raise RuntimeError(
                    f"talk_workflow_validation_missing_after_short_circuit:{workflow_id}"
                )
        else:
            graph, graph_warnings = build_workflow_process_graph(workflow_id)
            if not isinstance(graph, dict):
                raise RuntimeError(f"talk_workflow_graph_missing:{workflow_id}")
            warning_items = [
                str(item).strip()
                for item in (graph_warnings or [])
                if isinstance(item, str) and str(item).strip()
            ]
            if warning_items:
                raise RuntimeError(
                    "talk_workflow_graph_warnings_present:"
                    f"{workflow_id}:"
                    + ",".join(warning_items)
                )

            definition = load_workflow_definition_from_vontology(workflow_id)
            if definition is None:
                raise RuntimeError(
                    f"talk_workflow_definition_not_loadable:{workflow_id}"
                )
            validation = validate_workflow_definition_contract(
                definition=definition,
                supported_action_ids=_workflow_supported_action_ids(),
                known_workflow_ids=target_workflow_ids,
                workflow_definition_loader=load_workflow_definition_from_vontology,
            )
        validation_by_workflow_id[workflow_id] = validation
        if not bool(validation.get("valid")):
            raise RuntimeError(
                "talk_workflow_validation_failed:"
                f"{workflow_id}:"
                + ",".join(
                    str(item).strip()
                    for item in validation.get("errors", [])
                    if isinstance(item, str) and str(item).strip()
                )
            )

    invalidate_workflow_discovery_executability_caches()

    return {
        "workflow_ids": list(target_workflow_ids),
        "publication": publication_report,
        "typed_workflow_ids": typed_workflow_ids,
        "typed_step_ids": typed_step_ids,
        "validation_by_workflow_id": validation_by_workflow_id,
    }


__all__ = [
    "ACADEMIC_PRESENTATION_INSTANCE_WORKFLOW_ID",
    "TALK_REPRESENTATION_WORKFLOW_ID",
    "TECHNICAL_SCIENTIFIC_TALK_REPRESENTATION_WORKFLOW_ID",
    "bootstrap_canonical_talk_representation_workflows",
]
