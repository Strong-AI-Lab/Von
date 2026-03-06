"""Shared parent-specificity workflow contract constants.

These identifiers are consumed by both the built-in durable workflow
definitions and the authoritative Vontology publication path. Keeping them in
one module prevents the Python runtime and the published graph from drifting on
subworkflow contracts, output mappings, and wrapper failure semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from .subworkflow_contracts import WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE

PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID = (
    "#V#workflow_mapping_current_candidate_id_to_concept_id_parameter"
)

PARENT_SPECIFICITY_DOSSIER_FAILURE_MODE_BINDINGS: tuple[tuple[str, str], ...] = (
    ("failure_mode", WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE),
)


@dataclass(frozen=True)
class ParentSpecificityToolOutputMappingSpec:
    concept_id: str
    tool_output_field: str
    context_key: str
    child_output_field: str | None = None


PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS: tuple[
    ParentSpecificityToolOutputMappingSpec, ...
] = (
    ParentSpecificityToolOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_"
            "result_concept_dossier_to_concept_dossier"
        ),
        tool_output_field="result.concept_dossier",
        context_key="concept_dossier",
        child_output_field="concept_dossier",
    ),
    ParentSpecificityToolOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_"
            "result_concept_dossier_summary_to_concept_dossier_summary"
        ),
        tool_output_field="result.concept_dossier_summary",
        context_key="concept_dossier_summary",
        child_output_field="concept_dossier_summary",
    ),
    ParentSpecificityToolOutputMappingSpec(
        concept_id=(
            "#V#workflow_mapping_tool_field_"
            "child_workflow_failed_to_dossier_child_failed"
        ),
        tool_output_field="child_workflow_failed",
        context_key="dossier_child_failed",
    ),
    ParentSpecificityToolOutputMappingSpec(
        concept_id="#V#workflow_mapping_tool_field_subworkflow_error_to_dossier_error",
        tool_output_field="subworkflow_error",
        context_key="dossier_error",
    ),
)

PARENT_SPECIFICITY_DOSSIER_WRITES_CONTEXT_KEYS: tuple[str, ...] = (
    "concept_dossier",
    "concept_dossier_summary",
    "dossier_child_failed",
    "dossier_error",
)


__all__ = [
    "PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID",
    "PARENT_SPECIFICITY_DOSSIER_FAILURE_MODE_BINDINGS",
    "PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS",
    "PARENT_SPECIFICITY_DOSSIER_WRITES_CONTEXT_KEYS",
    "ParentSpecificityToolOutputMappingSpec",
]
