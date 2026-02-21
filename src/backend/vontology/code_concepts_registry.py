"""Registry for virtual concepts that are primarily handled in code.

These concepts may not be persisted in MongoDB, but the UI and MCP tooling still
benefit from treating them as first-class for discovery, rendering, and
navigation.

This module is deliberately read-only: it does not perform any database writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from ..models.text_value_models import RelationPredicate


MENTIONED_IN_VON_CODE_ID = "#V#mentioned_in_von_code"
MENTIONED_IN_VON_TEST_ID = "#V#mentioned_in_von_test"
PREDICATE_TYPE_ID = "#V#predicate"


@dataclass(frozen=True)
class CodeConcept:
    concept_id: str
    display_name: str
    kind: str
    md_content: str


def _predicate_md(concept_id: str, display_name: str) -> str:
    return (
        f"# {display_name}\n\n"
        "This is a built-in Von predicate that is used by code.\n\n"
        "It is treated as a first-class concept for UI navigation and inspection, "
        "even when it is not persisted as a MongoDB concept document.\n"
    )


def _unique_ids(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


_TEXT_PREDICATE_IDS = [
    f"#V#{RelationPredicate.HAS_NAME}",
    f"#V#{RelationPredicate.HAS_DESCRIPTION}",
    f"#V#{RelationPredicate.HAS_CONTENT}",
    f"#V#{RelationPredicate.HAS_NOTE}",
    f"#V#{RelationPredicate.HAS_INTERACTION}",
    "#V#hasDefinition",
]

_FILE_METADATA_PREDICATE_IDS = [
    "#V#has_original_filename",
    "#V#has_sha256",
    "#V#has_size_bytes",
    "#V#has_upload_timestamp",
    "#V#has_mime_type",
    "#V#has_blob_backend",
    "#V#has_blob_key",
    "#V#has_blob_uri",
]

_WORKFLOW_PREDICATE_IDS = [
    "#V#hasInitialStep",
    "#V#has_initial_step",
    "#V#hasStep",
    "#V#has_step",
    "#V#hasPrecondition",
    "#V#has_precondition",
    "#V#hasEffect",
    "#V#has_effect",
    "#V#invokesAction",
    "#V#invokes_action",
    "#V#nextStep",
    "#V#next_step",
    "#V#onTrueNextStep",
    "#V#on_true_next_step",
    "#V#onFalseNextStep",
    "#V#on_false_next_step",
    "#V#onFailureNextStep",
    "#V#on_failure_next_step",
    "#V#readsVariable",
    "#V#reads_variable",
    "#V#writesVariable",
    "#V#writes_variable",
]

_RELATION_META_PREDICATE_IDS = [
    "#V#salient_binary_predicate_for_type",
    "#V#suggested_relations_for_type",
    "#V#arg_num_is_instance",
]

_STRUCTURAL_PREDICATE_IDS = [
    "#V#is_a_type_of",
    "#V#has_subtype",
    "#V#is_an_instance_of",
    "#V#has_instance",
    "#V#related_to",
]

_OTHER_PREDICATE_IDS = [
    "#V#has_email",
    "#V#hasRole",
    "#V#memberOf",
    "#V#member_of_organisation",
    "#V#has_birthplace",
    "#V#has_occupation",
]

# Task management predicates (JVNAUTOSCI-1040)
_TASK_PREDICATE_IDS = [
    "#V#hasAssignee",
    "#V#hasCreatedBy",
    "#V#hasOriginatingConversation",
    "#V#hasDueDate",
    "#V#hasPriority",
    "#V#hasTaskStatus",
    "#V#hasTaskItem",
    "#V#executesTask",
    "#V#hasExecutor",
    "#V#hasExecutionStatus",
    "#V#hasStartTime",
    "#V#hasEndTime",
    "#V#hasResult",
    "#V#hasProgressNote",
]

# Conversation predicates (JVNAUTOSCI-1040)
_CONVERSATION_PREDICATE_IDS = [
    "#V#hasSessionId",
    "#V#hasParticipant",
    "#V#hasOwner",
    "#V#hasOrganisation",
    "#V#hasNamespace",
    "#V#hasTopic",
]

# Effort-unit predicates (JVNAUTOSCI-1238/JVNAUTOSCI-1240)
_EFFORT_UNIT_PREDICATE_IDS = [
    "#V#has_effort_unit_goal",
    "#V#completion_triggers_successor_effort_unit_type",
    "#V#has_successor_effort_unit_type",
    "#V#completion_produces_support_object",
]

# These are the canonical predicate concepts that we treat as built-in and
# surfaced in the UI even when no Mongo concept document exists.
_CODE_PREDICATE_IDS = _unique_ids(
    [
        *_TEXT_PREDICATE_IDS,
        *_FILE_METADATA_PREDICATE_IDS,
        *_WORKFLOW_PREDICATE_IDS,
        *_RELATION_META_PREDICATE_IDS,
        *_STRUCTURAL_PREDICATE_IDS,
        *_OTHER_PREDICATE_IDS,
        *_TASK_PREDICATE_IDS,
        *_CONVERSATION_PREDICATE_IDS,
        *_EFFORT_UNIT_PREDICATE_IDS,
    ]
)

# Note: The text-relations layer also supports non-#V# predicates (e.g. "hasContent"),
# but the UI expects concept-like identifiers for cartouches and navigation.
_CODE_PREDICATE_CONCEPTS: Dict[str, CodeConcept] = {
    concept_id: CodeConcept(
        concept_id=concept_id,
        display_name=concept_id.replace("#V#", ""),
        kind="predicate",
        md_content=_predicate_md(concept_id, concept_id.replace("#V#", "")),
    )
    for concept_id in _CODE_PREDICATE_IDS
}


def iter_code_concepts() -> Iterable[CodeConcept]:
    return _CODE_PREDICATE_CONCEPTS.values()


def list_code_predicate_ids() -> list[str]:
    return list(_CODE_PREDICATE_IDS)


def is_code_concept_id(concept_id: str) -> bool:
    return concept_id in _CODE_PREDICATE_CONCEPTS


def get_code_concept(concept_id: str) -> Optional[CodeConcept]:
    return _CODE_PREDICATE_CONCEPTS.get(concept_id)


def build_virtual_concept_doc(concept_id: str) -> Optional[dict]:
    """Return a Mongo-shaped concept document for a registered code concept.

    The shape is intentionally compatible with `is_predicate()` and similar helpers.
    """

    cc = get_code_concept(concept_id)
    if cc is None:
        return None

    if cc.kind == "predicate":
        instance_of = [PREDICATE_TYPE_ID, MENTIONED_IN_VON_CODE_ID]
        type_of = []
    else:
        instance_of = [MENTIONED_IN_VON_CODE_ID]
        type_of = []

    return {
        "concept_id": cc.concept_id,
        "name": cc.display_name,
        "names": [{"name": cc.display_name, "type": "NL", "language": "en-NZ"}],
        "relationships": {
            "is_a_type_of": type_of,
            "is_an_instance_of": instance_of,
        },
        "path": cc.concept_id,
        "md_content": cc.md_content,
        "metadata": {
            "concept_type": cc.kind,
            "virtual": True,
            "mentioned_in_von_code": True,
        },
    }
