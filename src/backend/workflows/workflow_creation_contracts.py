from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

WORKFLOW_CREATION_WORKFLOW_ID = "#V#von_workflow_creation_workflow"

WORKFLOW_CREATION_STEP_IDENTIFY_NEED = "#V#workflow_creation_step_identify_need"
WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE = "#V#workflow_creation_step_design_structure"
WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE = (
    "#V#workflow_creation_step_create_workflow_type"
)
WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS = (
    "#V#workflow_creation_step_create_step_concepts"
)
WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS = (
    "#V#workflow_creation_step_establish_relationships"
)
WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY = (
    "#V#workflow_creation_step_verify_discoverability"
)
WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA = "#V#workflow_creation_step_document_in_jira"

WORKFLOW_CREATION_ACTION_IDENTIFY_NEED = "workflow_authoring.identify_need"
WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE = "workflow_authoring.design_structure"
WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE = (
    "workflow_authoring.create_workflow_type"
)
WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS = (
    "workflow_authoring.create_step_concepts"
)
WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS = (
    "workflow_authoring.establish_relationships"
)
WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY = (
    "workflow_authoring.verify_discoverability"
)
WORKFLOW_CREATION_ACTION_FINALISE = "workflow_authoring.finalise"
WORKFLOW_CREATION_ACTION_EMIT_MARKER = "workflow_authoring.emit_marker"
WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS = (
    "workflow_authoring.resolve_scholarly_authors"
)
WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE = (
    "workflow_authoring.resolve_phd_student_candidate"
)
WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS = (
    "workflow_authoring.assert_phd_student_relationships"
)
WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT = (
    "workflow_authoring.ground_phd_student_text"
)

WORKFLOW_CREATION_ACTION_CONCEPT_IDENTIFY_NEED = (
    "#V#workflow_authoring_action_identify_need"
)
WORKFLOW_CREATION_ACTION_CONCEPT_DESIGN_STRUCTURE = (
    "#V#workflow_authoring_action_design_structure"
)
WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_WORKFLOW_TYPE = (
    "#V#workflow_authoring_action_create_workflow_type"
)
WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_STEP_CONCEPTS = (
    "#V#workflow_authoring_action_create_step_concepts"
)
WORKFLOW_CREATION_ACTION_CONCEPT_ESTABLISH_RELATIONSHIPS = (
    "#V#workflow_authoring_action_establish_relationships"
)
WORKFLOW_CREATION_ACTION_CONCEPT_VERIFY_DISCOVERABILITY = (
    "#V#workflow_authoring_action_verify_discoverability"
)
WORKFLOW_CREATION_ACTION_CONCEPT_FINALISE = (
    "#V#workflow_authoring_action_finalise"
)
WORKFLOW_CREATION_ACTION_CONCEPT_EMIT_MARKER = (
    "#V#workflow_authoring_action_emit_marker"
)
WORKFLOW_CREATION_ACTION_CONCEPT_RESOLVE_SCHOLARLY_AUTHORS = (
    "#V#workflow_authoring_action_resolve_scholarly_authors"
)
WORKFLOW_CREATION_ACTION_CONCEPT_RESOLVE_PHD_STUDENT_CANDIDATE = (
    "#V#workflow_authoring_action_resolve_phd_student_candidate"
)
WORKFLOW_CREATION_ACTION_CONCEPT_ASSERT_PHD_STUDENT_RELATIONSHIPS = (
    "#V#workflow_authoring_action_assert_phd_student_relationships"
)
WORKFLOW_CREATION_ACTION_CONCEPT_GROUND_PHD_STUDENT_TEXT = (
    "#V#workflow_authoring_action_ground_phd_student_text"
)


def _object_schema(*, properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": True,
        "properties": dict(properties),
    }
    if required:
        schema["required"] = list(required)
    return schema


@dataclass(frozen=True)
class WorkflowCreationActionContractDefinition:
    action_id: str
    concept_id: str
    name: str
    description: str
    input_schema: Mapping[str, Any] | None = None
    output_schema: Mapping[str, Any] | None = None
    side_effects: str | None = None
    postconditions: tuple[str, ...] = ()


WORKFLOW_CREATION_ACTION_CONTRACT_DEFINITIONS: tuple[
    WorkflowCreationActionContractDefinition,
    ...,
] = (
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_IDENTIFY_NEED,
        name="Workflow authoring identify need",
        description="Normalise a workflow-authoring request into deterministic workflow state.",
        input_schema=_object_schema(
            properties={
                "prompt": {"type": "string"},
                "workflow_spec": {"type": "object"},
                "parent_type_id": {"type": "string"},
            }
        ),
        output_schema=_object_schema(
            properties={
                "workflow_creation_spec": {"type": "object"},
                "required_effects_declared": {"type": "boolean"},
                "parent_concept_id_used": {"type": "string"},
            },
            required=("workflow_creation_spec", "required_effects_declared"),
        ),
        side_effects="None. Pure normalisation and contract shaping.",
        postconditions=("workflow_creation_spec_normalised",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_DESIGN_STRUCTURE,
        name="Workflow authoring design structure",
        description="Summarise the normalised workflow structure and declared step count.",
        input_schema=_object_schema(
            properties={"workflow_creation_spec": {"type": "object"}},
            required=("workflow_creation_spec",),
        ),
        output_schema=_object_schema(
            properties={
                "workflow_creation_spec": {"type": "object"},
                "workflow_creation_step_count": {"type": "integer"},
            },
            required=("workflow_creation_spec", "workflow_creation_step_count"),
        ),
        side_effects="None. Uses existing workflow state only.",
        postconditions=("workflow_step_count_available",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_WORKFLOW_TYPE,
        name="Workflow authoring ensure workflow concept",
        description="Create or update the draft workflow concept and its parent typing.",
        input_schema=_object_schema(
            properties={"workflow_creation_spec": {"type": "object"}},
            required=("workflow_creation_spec",),
        ),
        output_schema=_object_schema(
            properties={
                "workflow_concept_id": {"type": "string"},
                "parent_concept_id_used": {"type": "string"},
            },
            required=("workflow_concept_id", "parent_concept_id_used"),
        ),
        side_effects="Creates or updates the target workflow concept and draft lifecycle metadata.",
        postconditions=("workflow_concept_exists", "workflow_lifecycle_is_draft"),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_STEP_CONCEPTS,
        name="Workflow authoring ensure step concepts",
        description="Create or update step concepts for the authored workflow graph.",
        input_schema=_object_schema(
            properties={"workflow_creation_spec": {"type": "object"}},
            required=("workflow_creation_spec",),
        ),
        output_schema=_object_schema(
            properties={"workflow_step_concept_ids": {"type": "array"}},
            required=("workflow_step_concept_ids",),
        ),
        side_effects="Creates or updates workflow-step concepts declared in the authoring spec.",
        postconditions=("workflow_step_concepts_exist",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_ESTABLISH_RELATIONSHIPS,
        name="Workflow authoring write workflow graph",
        description="Write structural workflow graph relationships and action bindings.",
        input_schema=_object_schema(
            properties={"workflow_creation_spec": {"type": "object"}},
            required=("workflow_creation_spec",),
        ),
        output_schema=_object_schema(
            properties={
                "workflow_structure_written": {"type": "boolean"},
                "workflow_concept_id": {"type": "string"},
                "workflow_action_contract_concept_ids": {"type": "array"},
            },
            required=("workflow_structure_written", "workflow_concept_id"),
        ),
        side_effects="Writes hasInitialStep, hasStep, transition edges, input maps, and invokesAction bindings.",
        postconditions=(
            "workflow_graph_written_to_draft",
            "workflow_action_contract_concepts_materialised",
        ),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_VERIFY_DISCOVERABILITY,
        name="Workflow authoring verify publication",
        description="Validate authored workflow structure, execute a test instance, and update draft validation state.",
        input_schema=_object_schema(
            properties={"workflow_creation_spec": {"type": "object"}},
            required=("workflow_creation_spec",),
        ),
        output_schema=_object_schema(
            properties={
                "structural_validation_passed": {"type": "boolean"},
                "postconditions_verified": {"type": "boolean"},
                "optional_test_instance_id": {"type": "string"},
                "workflow_discoverable_before_publish": {"type": "boolean"},
            },
            required=("structural_validation_passed", "postconditions_verified"),
        ),
        side_effects="Runs structural validation and a bounded test execution against the draft workflow.",
        postconditions=("draft_validation_state_recorded",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_FINALISE,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_FINALISE,
        name="Workflow authoring completion gate",
        description="Promote a validated draft workflow to published state only when all authoring gates pass.",
        input_schema=_object_schema(
            properties={
                "workflow_concept_id": {"type": "string"},
                "required_effects_declared": {"type": "boolean"},
                "structural_validation_passed": {"type": "boolean"},
                "postconditions_verified": {"type": "boolean"},
            }
        ),
        output_schema=_object_schema(
            properties={
                "workflow_concept_id": {"type": "string"},
                "workflow_discoverable": {"type": "boolean"},
                "response_text": {"type": "string"},
            },
            required=("workflow_concept_id", "workflow_discoverable", "response_text"),
        ),
        side_effects="Updates workflow lifecycle state to published or a failed draft outcome.",
        postconditions=("workflow_completion_gate_enforced",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_EMIT_MARKER,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_EMIT_MARKER,
        name="Workflow authoring emit marker",
        description="Emit a deterministic marker value into workflow context for postcondition probes.",
        input_schema=_object_schema(
            properties={
                "marker_key": {"type": "string"},
                "marker_value": {},
            }
        ),
        output_schema=_object_schema(
            properties={"marker_output": {"type": "object"}},
        ),
        side_effects="None. Updates workflow context only.",
        postconditions=("marker_context_written",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_RESOLVE_SCHOLARLY_AUTHORS,
        name="Workflow authoring resolve scholarly authors",
        description="Resolve scholarly authors against existing person concepts and create/link missing people.",
        side_effects="Creates or updates person concepts and authorship links when grounded evidence supports them.",
        postconditions=("scholarly_author_resolution_completed",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_RESOLVE_PHD_STUDENT_CANDIDATE,
        name="Workflow authoring resolve PhD student candidate",
        description="Resolve a PhD-student candidate concept from provided text/profile data.",
        side_effects="May create or reuse a candidate concept, with fail-closed ambiguity reporting.",
        postconditions=("phd_student_candidate_resolved_or_failed_closed",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_ASSERT_PHD_STUDENT_RELATIONSHIPS,
        name="Workflow authoring assert PhD student relationships",
        description="Assert core person, student, and research relationships for the resolved candidate.",
        side_effects="Creates or updates supported PhD student relationships in Vontology.",
        postconditions=("phd_student_relationships_asserted",),
    ),
    WorkflowCreationActionContractDefinition(
        action_id=WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT,
        concept_id=WORKFLOW_CREATION_ACTION_CONCEPT_GROUND_PHD_STUDENT_TEXT,
        name="Workflow authoring ground PhD student text",
        description="Attach source text and provenance to the resolved PhD student concept.",
        side_effects="Writes grounded text and provenance to the resolved concept.",
        postconditions=("phd_student_text_grounded",),
    ),
)

WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID: dict[
    str,
    WorkflowCreationActionContractDefinition,
] = {
    item.action_id: item for item in WORKFLOW_CREATION_ACTION_CONTRACT_DEFINITIONS
}
WORKFLOW_CREATION_ACTION_ID_BY_CONCEPT_ID: dict[str, str] = {
    item.concept_id: item.action_id for item in WORKFLOW_CREATION_ACTION_CONTRACT_DEFINITIONS
}
WORKFLOW_CREATION_ACTION_CONCEPT_ID_BY_ACTION_ID: dict[str, str] = {
    item.action_id: item.concept_id for item in WORKFLOW_CREATION_ACTION_CONTRACT_DEFINITIONS
}


def workflow_creation_action_contract_definition(
    action_id: str,
) -> WorkflowCreationActionContractDefinition | None:
    return WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID.get(str(action_id or "").strip())


def workflow_creation_action_contract_concept_id(action_id: str) -> str | None:
    return WORKFLOW_CREATION_ACTION_CONCEPT_ID_BY_ACTION_ID.get(
        str(action_id or "").strip()
    )


def workflow_creation_action_target(action_id: str) -> str | None:
    action_id_text = str(action_id or "").strip()
    if not action_id_text:
        return None
    return (
        workflow_creation_action_contract_concept_id(action_id_text)
        or action_id_text
    )


__all__ = [
    "WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS",
    "WORKFLOW_CREATION_ACTION_CONCEPT_ASSERT_PHD_STUDENT_RELATIONSHIPS",
    "WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_STEP_CONCEPTS",
    "WORKFLOW_CREATION_ACTION_CONCEPT_CREATE_WORKFLOW_TYPE",
    "WORKFLOW_CREATION_ACTION_CONCEPT_DESIGN_STRUCTURE",
    "WORKFLOW_CREATION_ACTION_CONCEPT_EMIT_MARKER",
    "WORKFLOW_CREATION_ACTION_CONCEPT_ESTABLISH_RELATIONSHIPS",
    "WORKFLOW_CREATION_ACTION_CONCEPT_FINALISE",
    "WORKFLOW_CREATION_ACTION_CONCEPT_GROUND_PHD_STUDENT_TEXT",
    "WORKFLOW_CREATION_ACTION_CONCEPT_IDENTIFY_NEED",
    "WORKFLOW_CREATION_ACTION_CONCEPT_RESOLVE_PHD_STUDENT_CANDIDATE",
    "WORKFLOW_CREATION_ACTION_CONCEPT_RESOLVE_SCHOLARLY_AUTHORS",
    "WORKFLOW_CREATION_ACTION_CONCEPT_VERIFY_DISCOVERABILITY",
    "WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID",
    "WORKFLOW_CREATION_ACTION_CONTRACT_DEFINITIONS",
    "WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS",
    "WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE",
    "WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE",
    "WORKFLOW_CREATION_ACTION_EMIT_MARKER",
    "WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS",
    "WORKFLOW_CREATION_ACTION_FINALISE",
    "WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT",
    "WORKFLOW_CREATION_ACTION_IDENTIFY_NEED",
    "WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE",
    "WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS",
    "WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY",
    "WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS",
    "WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE",
    "WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE",
    "WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA",
    "WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS",
    "WORKFLOW_CREATION_STEP_IDENTIFY_NEED",
    "WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY",
    "WORKFLOW_CREATION_WORKFLOW_ID",
    "WorkflowCreationActionContractDefinition",
    "workflow_creation_action_contract_concept_id",
    "workflow_creation_action_contract_definition",
    "workflow_creation_action_target",
]
