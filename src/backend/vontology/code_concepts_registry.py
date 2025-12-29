"""Registry for virtual concepts that are primarily handled in code.

These concepts may not be persisted in MongoDB, but the UI and MCP tooling still
benefit from treating them as first-class for discovery, rendering, and
navigation.

This module is deliberately read-only: it does not perform any database writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional


MENTIONED_IN_VON_CODE_ID = "#V#mentioned_in_von_code"
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


# These are the canonical text-relation predicates supported by the backend.
#
# Note: The text-relations layer also supports non-#V# predicates (e.g. "hasContent"),
# but the UI expects concept-like identifiers for cartouches and navigation.
_CODE_PREDICATE_CONCEPTS: Dict[str, CodeConcept] = {
    "#V#hasName": CodeConcept(
        concept_id="#V#hasName",
        display_name="hasName",
        kind="predicate",
        md_content=_predicate_md("#V#hasName", "hasName"),
    ),
    "#V#hasDescription": CodeConcept(
        concept_id="#V#hasDescription",
        display_name="hasDescription",
        kind="predicate",
        md_content=_predicate_md("#V#hasDescription", "hasDescription"),
    ),
    "#V#hasContent": CodeConcept(
        concept_id="#V#hasContent",
        display_name="hasContent",
        kind="predicate",
        md_content=_predicate_md("#V#hasContent", "hasContent"),
    ),
    "#V#hasNote": CodeConcept(
        concept_id="#V#hasNote",
        display_name="hasNote",
        kind="predicate",
        md_content=_predicate_md("#V#hasNote", "hasNote"),
    ),
    "#V#hasInteraction": CodeConcept(
        concept_id="#V#hasInteraction",
        display_name="hasInteraction",
        kind="predicate",
        md_content=_predicate_md("#V#hasInteraction", "hasInteraction"),
    ),
}


def iter_code_concepts() -> Iterable[CodeConcept]:
    return _CODE_PREDICATE_CONCEPTS.values()


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
