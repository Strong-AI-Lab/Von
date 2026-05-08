"""Vontology KR vocabulary for tool evidence contracts.

This module is a bootstrap and validation surface only. Runtime behaviour should
query the materialised Vontology concepts and relationships rather than treating
these Python constants as the authority for a particular tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import concept_service
from .relationship_write_service import add_relationship
from .workflow_vontology_materialisation_helpers import (
    load_concept,
    normalise_relationship_targets,
    suspend_event_workflow_integration,
)

TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION = "tool_evidence_contract_vocabulary.v1"
TOOL_EVIDENCE_CONTRACT_SOURCE_TAG = "JVNAUTOSCI-2287"
TOOL_EVIDENCE_CONTRACT_MANAGED_BY = "tool_evidence_contract_vontology_service"

TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID = "#V#tool_evidence_contract_vocabulary_v1"
PREDICATE_TYPE_ID = "#V#predicate"
BINARY_PREDICATE_TYPE_ID = "#V#binary_predicate"

_PREDICATE_PARENT_IDS: tuple[str, ...] = (
    PREDICATE_TYPE_ID,
    BINARY_PREDICATE_TYPE_ID,
)
_CORE_PARENT_IDS: tuple[str, ...] = (
    "#V#abstract_object",
    "#V#information_object",
    PREDICATE_TYPE_ID,
)


@dataclass(frozen=True)
class ConceptSpec:
    concept_id: str
    name: str
    description: str
    parent_concept_ids: tuple[str, ...]
    create_as_instance: bool
    category: str


@dataclass(frozen=True)
class RelationshipSpec:
    source_id: str
    predicate: str
    target_id: str


_CORE_TYPE_SPECS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        concept_id="#V#abstract_object",
        name="Abstract object",
        description="A non-physical object used as a parent for concepts, roles, and specifications.",
        parent_concept_ids=("#V#thing",),
        create_as_instance=False,
        category="core_type",
    ),
    ConceptSpec(
        concept_id="#V#information_object",
        name="Information object",
        description="An abstract object whose primary function is carrying or specifying information.",
        parent_concept_ids=("#V#abstract_object",),
        create_as_instance=False,
        category="core_type",
    ),
    ConceptSpec(
        concept_id=PREDICATE_TYPE_ID,
        name="Predicate",
        description="A Vontology concept used as a relationship predicate.",
        parent_concept_ids=("#V#abstract_object",),
        create_as_instance=False,
        category="core_type",
    ),
    ConceptSpec(
        concept_id=BINARY_PREDICATE_TYPE_ID,
        name="Binary predicate",
        description="A predicate used for binary concept-to-concept relationships.",
        parent_concept_ids=(PREDICATE_TYPE_ID,),
        create_as_instance=False,
        category="core_type",
    ),
)

_VOCABULARY_TYPE_SPECS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        concept_id="#V#tool_evidence_contract_vocabulary",
        name="Tool evidence contract vocabulary",
        description="Type for versioned vocabularies that describe tool evidence contracts as Vontology KR.",
        parent_concept_ids=("#V#information_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id=TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
        name="Tool evidence contract vocabulary v1",
        description="Versioned vocabulary for representing tool result entities, fields, evidence views, and follow-up affordances as graph KR.",
        parent_concept_ids=("#V#tool_evidence_contract_vocabulary",),
        create_as_instance=True,
        category="vocabulary_instance",
    ),
    ConceptSpec(
        concept_id="#V#tool_interface_contract",
        name="Tool interface contract",
        description="A represented specification of tool inputs, outputs, evidence views, or follow-up affordances.",
        parent_concept_ids=("#V#information_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_result_entity_type",
        name="Tool result entity type",
        description="Metatype for Vontology type concepts describing entities returned by tools.",
        parent_concept_ids=("#V#abstract_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_result_field",
        name="Tool result field",
        description="A represented field that can be extracted from, required by, or preserved from a tool result.",
        parent_concept_ids=("#V#information_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_wire_key",
        name="Tool wire key",
        description="A named key or alias used by a concrete tool payload for a represented result field.",
        parent_concept_ids=("#V#information_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_payload_path",
        name="Tool payload path",
        description="A represented path into a concrete tool payload used to extract a field value.",
        parent_concept_ids=("#V#information_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_evidence_view",
        name="Tool evidence view",
        description="A represented view declaring which tool fields must be retained for a particular LLM-facing or user-facing evidence purpose.",
        parent_concept_ids=("#V#tool_interface_contract",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_list_detail_affordance",
        name="Tool list-detail affordance",
        description="A represented contract that a list/search tool can be followed by a detail tool to complete fields for each item.",
        parent_concept_ids=("#V#tool_interface_contract",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_result_field_role",
        name="Tool result field role",
        description="Type for roles that explain why a field matters to follow-up, answer synthesis, display, or safety.",
        parent_concept_ids=("#V#abstract_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_evidence_view_purpose",
        name="Tool evidence view purpose",
        description="Type for purposes that evidence views serve in a tool-use workflow.",
        parent_concept_ids=("#V#abstract_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_field_completion_policy",
        name="Tool field completion policy",
        description="Type for policies describing whether a field must be completed before answer synthesis or follow-up execution.",
        parent_concept_ids=("#V#abstract_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
    ConceptSpec(
        concept_id="#V#tool_redaction_policy",
        name="Tool redaction policy",
        description="Type for policies describing how a field should be retained, redacted, or hidden in evidence views.",
        parent_concept_ids=("#V#abstract_object",),
        create_as_instance=False,
        category="vocabulary_type",
    ),
)

_PREDICATE_SPECS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        concept_id="#V#vocabulary_includes_concept",
        name="vocabulary includes concept",
        description="Relates a versioned vocabulary concept to a concept included in that vocabulary.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_contract_applies_to_tool",
        name="tool contract applies to tool",
        description="Relates a represented tool contract or evidence view to the tool concept it constrains.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_contract_has_evidence_view",
        name="tool contract has evidence view",
        description="Relates a tool contract to an evidence view concept that controls retained fields.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_contract_has_list_detail_affordance",
        name="tool contract has list detail affordance",
        description="Relates a tool contract to a list-detail affordance concept.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_emits_entity_type",
        name="tool emits entity type",
        description="Relates a tool to the entity type represented by its result payload.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_emits_collection_entity_type",
        name="tool emits collection entity type",
        description="Relates a list/search tool to the entity type represented by each returned item.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_has_input_field",
        name="tool has input field",
        description="Relates a tool to a represented field accepted as an input or argument.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_has_output_field",
        name="tool has output field",
        description="Relates a tool to a represented field it can output.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#entity_type_has_tool_field",
        name="entity type has tool field",
        description="Relates a tool result entity type to one of its represented fields.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#field_has_wire_alias",
        name="field has wire alias",
        description="Relates a represented field to a tool wire-key concept that names it in a payload.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#field_extracts_from_payload_path",
        name="field extracts from payload path",
        description="Relates a represented field to a payload-path concept used to extract its value.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#field_has_role",
        name="field has role",
        description="Relates a represented tool field to a semantic role such as identifier, answer evidence, or follow-up argument.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#field_has_completion_policy",
        name="field has completion policy",
        description="Relates a represented field to a completion policy concept.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#field_has_redaction_policy",
        name="field has redaction policy",
        description="Relates a represented field to a redaction policy concept.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#evidence_view_applies_to_tool",
        name="evidence view applies to tool",
        description="Relates an evidence view to the tool whose result it shapes.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#evidence_view_applies_to_entity_type",
        name="evidence view applies to entity type",
        description="Relates an evidence view to the result entity type it describes.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#evidence_view_has_purpose",
        name="evidence view has purpose",
        description="Relates an evidence view to its workflow purpose.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#evidence_view_includes_field",
        name="evidence view includes field",
        description="Relates an evidence view to a field that should be retained when space permits.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#evidence_view_requires_field",
        name="evidence view requires field",
        description="Relates an evidence view to a field that must be preserved for its purpose.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#evidence_view_redacts_field",
        name="evidence view redacts field",
        description="Relates an evidence view to a field that should be hidden or summarised before presentation.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#list_tool_has_detail_tool",
        name="list tool has detail tool",
        description="Relates a list/search tool to the detail tool that can complete item fields.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#list_detail_affordance_has_list_tool",
        name="list detail affordance has list tool",
        description="Relates a list-detail affordance to its source list/search tool.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#list_detail_affordance_has_detail_tool",
        name="list detail affordance has detail tool",
        description="Relates a list-detail affordance to the detail tool it authorises.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#list_detail_affordance_uses_identifier_field",
        name="list detail affordance uses identifier field",
        description="Relates a list-detail affordance to the field used to bind a list item to a detail call.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#detail_tool_accepts_identifier_field",
        name="detail tool accepts identifier field",
        description="Relates a detail tool to the identifier field it accepts from a list item.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#detail_tool_completes_field",
        name="detail tool completes field",
        description="Relates a detail tool to a field it completes beyond what the list/search tool provided.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_argument_maps_from_field",
        name="tool argument maps from field",
        description="Relates an input field to the output field whose value should be passed into it for follow-up calls.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
    ConceptSpec(
        concept_id="#V#tool_result_preserves_field",
        name="tool result preserves field",
        description="Relates a tool or projection contract to a field that must survive compression and context limiting.",
        parent_concept_ids=_PREDICATE_PARENT_IDS,
        create_as_instance=True,
        category="predicate",
    ),
)

_FIELD_ROLE_SPECS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        concept_id="#V#tool_field_role_entity_identifier",
        name="tool field role entity identifier",
        description="Role for a field that identifies the returned entity or list item.",
        parent_concept_ids=("#V#tool_result_field_role",),
        create_as_instance=True,
        category="role",
    ),
    ConceptSpec(
        concept_id="#V#tool_field_role_follow_up_argument",
        name="tool field role follow-up argument",
        description="Role for a field whose value can be passed into a subsequent tool call.",
        parent_concept_ids=("#V#tool_result_field_role",),
        create_as_instance=True,
        category="role",
    ),
    ConceptSpec(
        concept_id="#V#tool_field_role_answer_evidence",
        name="tool field role answer evidence",
        description="Role for a field that must remain visible to answer synthesis.",
        parent_concept_ids=("#V#tool_result_field_role",),
        create_as_instance=True,
        category="role",
    ),
    ConceptSpec(
        concept_id="#V#tool_field_role_display_label",
        name="tool field role display label",
        description="Role for a field that provides a human-readable label for an item.",
        parent_concept_ids=("#V#tool_result_field_role",),
        create_as_instance=True,
        category="role",
    ),
    ConceptSpec(
        concept_id="#V#tool_field_role_collection_membership",
        name="tool field role collection membership",
        description="Role for a field that locates or contains the returned item collection.",
        parent_concept_ids=("#V#tool_result_field_role",),
        create_as_instance=True,
        category="role",
    ),
    ConceptSpec(
        concept_id="#V#tool_field_role_sensitive_content",
        name="tool field role sensitive content",
        description="Role for a field whose content may require redaction or summarisation before display.",
        parent_concept_ids=("#V#tool_result_field_role",),
        create_as_instance=True,
        category="role",
    ),
)

_VIEW_PURPOSE_SPECS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        concept_id="#V#tool_evidence_view_purpose_final_answer",
        name="tool evidence view purpose final answer",
        description="Purpose for fields that must be available when drafting the final answer.",
        parent_concept_ids=("#V#tool_evidence_view_purpose",),
        create_as_instance=True,
        category="view_purpose",
    ),
    ConceptSpec(
        concept_id="#V#tool_evidence_view_purpose_follow_up_selection",
        name="tool evidence view purpose follow-up selection",
        description="Purpose for fields needed to decide and parameterise follow-up tool calls.",
        parent_concept_ids=("#V#tool_evidence_view_purpose",),
        create_as_instance=True,
        category="view_purpose",
    ),
    ConceptSpec(
        concept_id="#V#tool_evidence_view_purpose_user_display",
        name="tool evidence view purpose user display",
        description="Purpose for fields that may be rendered directly to a user.",
        parent_concept_ids=("#V#tool_evidence_view_purpose",),
        create_as_instance=True,
        category="view_purpose",
    ),
    ConceptSpec(
        concept_id="#V#tool_evidence_view_purpose_telemetry",
        name="tool evidence view purpose telemetry",
        description="Purpose for fields retained for debugging, replay, or telemetry review.",
        parent_concept_ids=("#V#tool_evidence_view_purpose",),
        create_as_instance=True,
        category="view_purpose",
    ),
)

_POLICY_SPECS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        concept_id="#V#tool_field_completion_required_before_answer",
        name="tool field completion required before answer",
        description="Completion policy for a field that must be populated before answer synthesis.",
        parent_concept_ids=("#V#tool_field_completion_policy",),
        create_as_instance=True,
        category="completion_policy",
    ),
    ConceptSpec(
        concept_id="#V#tool_field_completion_optional_before_answer",
        name="tool field completion optional before answer",
        description="Completion policy for a field that is useful but not mandatory before answer synthesis.",
        parent_concept_ids=("#V#tool_field_completion_policy",),
        create_as_instance=True,
        category="completion_policy",
    ),
    ConceptSpec(
        concept_id="#V#tool_field_completion_required_for_follow_up",
        name="tool field completion required for follow-up",
        description="Completion policy for a field needed to construct a subsequent tool call.",
        parent_concept_ids=("#V#tool_field_completion_policy",),
        create_as_instance=True,
        category="completion_policy",
    ),
    ConceptSpec(
        concept_id="#V#tool_redaction_policy_include_plaintext",
        name="tool redaction policy include plaintext",
        description="Redaction policy allowing the field value to be retained as plaintext in an evidence view.",
        parent_concept_ids=("#V#tool_redaction_policy",),
        create_as_instance=True,
        category="redaction_policy",
    ),
    ConceptSpec(
        concept_id="#V#tool_redaction_policy_redact_by_default",
        name="tool redaction policy redact by default",
        description="Redaction policy requiring the field value to be hidden unless an authored view explicitly permits it.",
        parent_concept_ids=("#V#tool_redaction_policy",),
        create_as_instance=True,
        category="redaction_policy",
    ),
    ConceptSpec(
        concept_id="#V#tool_redaction_policy_preserve_identifier_only",
        name="tool redaction policy preserve identifier only",
        description="Redaction policy preserving stable identifiers while omitting sensitive content values.",
        parent_concept_ids=("#V#tool_redaction_policy",),
        create_as_instance=True,
        category="redaction_policy",
    ),
)

TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS: tuple[ConceptSpec, ...] = (
    *_CORE_TYPE_SPECS,
    *_VOCABULARY_TYPE_SPECS,
    *_PREDICATE_SPECS,
    *_FIELD_ROLE_SPECS,
    *_VIEW_PURPOSE_SPECS,
    *_POLICY_SPECS,
)


def _specs_by_category(category: str) -> tuple[ConceptSpec, ...]:
    return tuple(
        spec for spec in TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS if spec.category == category
    )


def canonical_tool_evidence_contract_concept_ids(
    *,
    include_core: bool = False,
) -> tuple[str, ...]:
    """Return canonical vocabulary concept IDs in materialisation order."""

    return tuple(
        spec.concept_id
        for spec in TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS
        if include_core or spec.category != "core_type"
    )


def canonical_tool_evidence_contract_predicate_ids() -> tuple[str, ...]:
    """Return predicate IDs required to express tool evidence contracts."""

    return tuple(spec.concept_id for spec in _PREDICATE_SPECS)


def _normalise_targets(value: Any) -> list[str]:
    return normalise_relationship_targets(value)


def _ensure_structural_targets(
    *,
    concept_id: str,
    relationship_key: str,
    target_ids: Sequence[str],
) -> bool:
    concept_doc = load_concept(concept_id)
    if not isinstance(concept_doc, Mapping):
        return False

    relationships = dict(concept_doc.get("relationships") or {})
    existing_targets = _normalise_targets(relationships.get(relationship_key))
    updated_targets = list(existing_targets)
    for target_id in target_ids:
        cleaned = str(target_id or "").strip()
        if cleaned and cleaned not in updated_targets:
            updated_targets.append(cleaned)

    if updated_targets == existing_targets:
        return False

    concept_service.update_concept(
        concept_id,
        {f"relationships.{relationship_key}": updated_targets},
    )
    return True


def _ensure_concept(spec: ConceptSpec) -> str:
    concept_doc = load_concept(spec.concept_id)
    if not isinstance(concept_doc, Mapping):
        concept_service.create_concept(
            name=spec.name,
            concept_id=spec.concept_id,
            description=spec.description,
            parent_concept_ids=list(spec.parent_concept_ids),
            create_as_instance=spec.create_as_instance,
            attributes={
                "repo_seed_source_tag": TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
                "repo_seed_managed_by": TOOL_EVIDENCE_CONTRACT_MANAGED_BY,
                "schema_version": TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
                "vocabulary_concept_id": TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
            },
            system_tags=[
                "tool_evidence_contract_vocabulary",
                TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
            ],
            visibility_scope_mode="global_general",
        )
        return "created"

    relationship_key = "is_an_instance_of" if spec.create_as_instance else "is_a_type_of"
    repaired = _ensure_structural_targets(
        concept_id=spec.concept_id,
        relationship_key=relationship_key,
        target_ids=spec.parent_concept_ids,
    )
    if spec.category == "predicate":
        repaired = (
            _ensure_structural_targets(
                concept_id=spec.concept_id,
                relationship_key="is_an_instance_of",
                target_ids=(PREDICATE_TYPE_ID,),
            )
            or repaired
        )
        repaired = _remove_predicate_type_parent_misclassification(spec.concept_id) or repaired
    return "repaired" if repaired else "existing"


def _remove_predicate_type_parent_misclassification(concept_id: str) -> bool:
    concept_doc = load_concept(concept_id)
    if not isinstance(concept_doc, Mapping):
        return False
    relationships = dict(concept_doc.get("relationships") or {})
    parent_targets = _normalise_targets(relationships.get("is_a_type_of"))
    filtered_targets = [
        target for target in parent_targets if target not in _PREDICATE_PARENT_IDS
    ]
    if filtered_targets == parent_targets:
        return False
    concept_service.update_concept(
        concept_id,
        {"relationships.is_a_type_of": filtered_targets},
    )
    return True


def _vocabulary_relationship_specs() -> tuple[RelationshipSpec, ...]:
    return tuple(
        RelationshipSpec(
            source_id=TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
            predicate="#V#vocabulary_includes_concept",
            target_id=concept_id,
        )
        for concept_id in canonical_tool_evidence_contract_concept_ids(include_core=False)
        if concept_id != TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID
    )


def _ensure_relationship(spec: RelationshipSpec) -> dict[str, Any]:
    result = add_relationship(
        source_id=spec.source_id,
        predicate=spec.predicate,
        target=spec.target_id,
    )
    return dict(result) if isinstance(result, Mapping) else {"success": False}


def _relationship_identity(spec: RelationshipSpec) -> str:
    return f"{spec.source_id}:{spec.predicate}:{spec.target_id}"


def bootstrap_tool_evidence_contract_vocabulary() -> dict[str, Any]:
    """Materialise the canonical tool evidence contract vocabulary into Vontology."""

    concept_status_by_id: dict[str, str] = {}
    relationship_ids: list[str] = []
    errors: list[dict[str, Any]] = []

    with suspend_event_workflow_integration():
        for spec in TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS:
            try:
                concept_status_by_id[spec.concept_id] = _ensure_concept(spec)
            except Exception as exc:
                errors.append(
                    {
                        "section": "concepts",
                        "concept_id": spec.concept_id,
                        "reason_code": str(exc),
                    }
                )

        for relationship_spec in _vocabulary_relationship_specs():
            try:
                result = _ensure_relationship(relationship_spec)
            except Exception as exc:
                errors.append(
                    {
                        "section": "relationships",
                        "relationship": _relationship_identity(relationship_spec),
                        "reason_code": str(exc),
                    }
                )
                continue
            if result.get("success") is True:
                relationship_ids.append(_relationship_identity(relationship_spec))
            else:
                errors.append(
                    {
                        "section": "relationships",
                        "relationship": _relationship_identity(relationship_spec),
                        "reason_code": str(result.get("error") or "relationship_failed"),
                        "details": result,
                    }
                )

    validation = validate_tool_evidence_contract_vocabulary()
    validation_errors = validation.get("errors") or []
    if isinstance(validation_errors, list):
        for error in validation_errors:
            if isinstance(error, Mapping):
                errors.append(dict(error))

    created_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "created"
    ]
    repaired_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "repaired"
    ]
    existing_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "existing"
    ]

    return {
        "success": not errors and bool(validation.get("success")),
        "schema_version": TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "source_tag": TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
        "managed_by": TOOL_EVIDENCE_CONTRACT_MANAGED_BY,
        "vocabulary_concept_id": TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
        "created_concept_ids": created_concept_ids,
        "repaired_concept_ids": repaired_concept_ids,
        "existing_concept_ids": existing_concept_ids,
        "relationship_ids": relationship_ids,
        "predicate_concept_ids": canonical_tool_evidence_contract_predicate_ids(),
        "validation": validation,
        "errors": errors,
        "counts": {
            "concept_specs": len(TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS),
            "created_concepts": len(created_concept_ids),
            "existing_concepts": len(existing_concept_ids),
            "repaired_concepts": len(repaired_concept_ids),
            "predicate_specs": len(_PREDICATE_SPECS),
            "field_role_specs": len(_FIELD_ROLE_SPECS),
            "view_purpose_specs": len(_VIEW_PURPOSE_SPECS),
            "policy_specs": len(_POLICY_SPECS),
            "relationships_written": len(relationship_ids),
            "errors": len(errors),
        },
    }


def validate_tool_evidence_contract_vocabulary() -> dict[str, Any]:
    """Validate that the materialised vocabulary has the expected graph shape."""

    errors: list[dict[str, Any]] = []
    missing_concept_ids: list[str] = []
    predicate_typing_errors: list[dict[str, Any]] = []

    for spec in TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS:
        concept_doc = load_concept(spec.concept_id)
        if not isinstance(concept_doc, Mapping):
            missing_concept_ids.append(spec.concept_id)
            errors.append(
                {
                    "section": "concepts",
                    "concept_id": spec.concept_id,
                    "reason_code": "missing_concept",
                }
            )
            continue
        if spec.category == "predicate":
            relationships = dict(concept_doc.get("relationships") or {})
            instance_targets = set(
                _normalise_targets(relationships.get("is_an_instance_of"))
            )
            type_parent_targets = set(_normalise_targets(relationships.get("is_a_type_of")))
            if PREDICATE_TYPE_ID not in instance_targets:
                predicate_typing_errors.append(
                    {
                        "concept_id": spec.concept_id,
                        "reason_code": "predicate_missing_instance_typing",
                        "current_instance_of": sorted(instance_targets),
                    }
                )
            misplaced_parent_targets = sorted(
                target for target in _PREDICATE_PARENT_IDS if target in type_parent_targets
            )
            if misplaced_parent_targets:
                predicate_typing_errors.append(
                    {
                        "concept_id": spec.concept_id,
                        "reason_code": "predicate_misclassified_as_type_parent",
                        "misplaced_type_parents": misplaced_parent_targets,
                    }
                )

    for error in predicate_typing_errors:
        errors.append({"section": "predicate_typing", **error})

    vocabulary_doc = load_concept(TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID)
    included_targets = set(
        _normalise_targets(
            (vocabulary_doc or {}).get("relationships", {}).get(
                "#V#vocabulary_includes_concept"
            )
            if isinstance(vocabulary_doc, Mapping)
            else None
        )
    )
    expected_members = set(
        concept_id
        for concept_id in canonical_tool_evidence_contract_concept_ids(
            include_core=False
        )
        if concept_id != TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID
    )
    missing_vocabulary_members = sorted(expected_members.difference(included_targets))
    for concept_id in missing_vocabulary_members:
        errors.append(
            {
                "section": "vocabulary_membership",
                "concept_id": concept_id,
                "reason_code": "missing_vocabulary_membership_relationship",
            }
        )

    return {
        "success": not errors,
        "schema_version": TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "vocabulary_concept_id": TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
        "missing_concept_ids": missing_concept_ids,
        "predicate_typing_errors": predicate_typing_errors,
        "missing_vocabulary_members": missing_vocabulary_members,
        "errors": errors,
        "counts": {
            "expected_concepts": len(TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS),
            "expected_predicates": len(_PREDICATE_SPECS),
            "missing_concepts": len(missing_concept_ids),
            "predicate_typing_errors": len(predicate_typing_errors),
            "missing_vocabulary_members": len(missing_vocabulary_members),
        },
    }


__all__ = [
    "TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS",
    "TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION",
    "TOOL_EVIDENCE_CONTRACT_SOURCE_TAG",
    "TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID",
    "bootstrap_tool_evidence_contract_vocabulary",
    "canonical_tool_evidence_contract_concept_ids",
    "canonical_tool_evidence_contract_predicate_ids",
    "validate_tool_evidence_contract_vocabulary",
]