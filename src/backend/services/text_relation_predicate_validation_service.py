"""Validation helpers for user-facing text-relation predicate writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..models.text_value_models import RelationPredicate
from . import concept_service


_CORE_TEXT_PREDICATE_STORAGE_BY_CONCEPT_ID: dict[str, str] = {
    f"#V#{RelationPredicate.HAS_NAME}": RelationPredicate.HAS_NAME,
    f"#V#{RelationPredicate.HAS_NOTE}": RelationPredicate.HAS_NOTE,
    f"#V#{RelationPredicate.HAS_DESCRIPTION}": RelationPredicate.HAS_DESCRIPTION,
    f"#V#{RelationPredicate.HAS_INTERACTION}": RelationPredicate.HAS_INTERACTION,
    f"#V#{RelationPredicate.HAS_CONTENT}": RelationPredicate.HAS_CONTENT,
}
_CORE_TEXT_PREDICATE_CONCEPT_BY_STORAGE: dict[str, str] = {
    storage: concept_id
    for concept_id, storage in _CORE_TEXT_PREDICATE_STORAGE_BY_CONCEPT_ID.items()
}


@dataclass(frozen=True)
class TextRelationPredicateResolution:
    """Resolved predicate information for a text-relation write."""

    input_predicate: str
    storage_predicate: str
    predicate_concept_id: str
    predicate_doc: Mapping[str, Any] | None = None


class TextRelationPredicateResolutionError(ValueError):
    """Raised when a requested text-relation predicate is not write-safe."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})
        self.suggestions = list(suggestions or [])


def _relationship_values(doc: Mapping[str, Any], key: str) -> set[str]:
    relationships = doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return set()
    raw = relationships.get(key)
    if isinstance(raw, str):
        return {raw} if raw else set()
    if isinstance(raw, list):
        return {str(item).strip() for item in raw if str(item).strip()}
    return set()


def _looks_like_predicate_doc(doc: Mapping[str, Any]) -> bool:
    concept_id = str(doc.get("concept_id") or "").strip()
    if concept_id == "#V#predicate":
        return True

    if doc.get("kind") == "predicate":
        return True

    metadata = doc.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("concept_type") == "predicate":
        return True

    instance_of = _relationship_values(doc, "is_an_instance_of")
    type_of = _relationship_values(doc, "is_a_type_of")
    return "#V#predicate" in instance_of or "#V#predicate" in type_of


def resolve_text_relation_predicate_for_write(
    predicate: Any,
) -> TextRelationPredicateResolution:
    """Resolve and validate a text-relation predicate for user-facing writes.

    The lower-level text-value service intentionally remains generic because seed,
    import, and repair paths sometimes need to operate before authority concepts
    are fully materialised. The MCP/chat-facing write surface is stricter: custom
    ``#V#...`` predicates must resolve to real or virtual predicate concepts before
    any relation is persisted.
    """

    predicate_text = str(predicate or "").strip()
    if not predicate_text:
        raise TextRelationPredicateResolutionError(
            "missing_parameter",
            "Missing 'predicate' parameter",
            details={"missing": ["predicate"]},
            suggestions=[
                "Use one of hasName, hasDescription, hasContent, hasNote, hasInteraction, or an existing #V# predicate concept."
            ],
        )

    if predicate_text in _CORE_TEXT_PREDICATE_CONCEPT_BY_STORAGE:
        return TextRelationPredicateResolution(
            input_predicate=predicate_text,
            storage_predicate=predicate_text,
            predicate_concept_id=_CORE_TEXT_PREDICATE_CONCEPT_BY_STORAGE[
                predicate_text
            ],
            predicate_doc=None,
        )

    if predicate_text in _CORE_TEXT_PREDICATE_STORAGE_BY_CONCEPT_ID:
        return TextRelationPredicateResolution(
            input_predicate=predicate_text,
            storage_predicate=_CORE_TEXT_PREDICATE_STORAGE_BY_CONCEPT_ID[
                predicate_text
            ],
            predicate_concept_id=predicate_text,
            predicate_doc=None,
        )

    if not predicate_text.startswith("#V#"):
        raise TextRelationPredicateResolutionError(
            "unsupported_text_relation_predicate",
            f"Unsupported text-relation predicate '{predicate_text}'.",
            details={"predicate": predicate_text},
            suggestions=[
                "For custom text-relation predicates, first resolve or create a #V# predicate concept and pass that concept ID.",
                "For general content use hasContent; for descriptions use hasDescription; for names/titles use hasName.",
            ],
        )

    try:
        predicate_doc = concept_service.get_concept_by_concept_id(predicate_text)
    except Exception as exc:
        raise TextRelationPredicateResolutionError(
            "predicate_concept_not_found",
            f"Predicate concept '{predicate_text}' was not found.",
            details={
                "predicate": predicate_text,
                "exception_type": type(exc).__name__,
            },
            suggestions=[
                "Resolve an existing predicate concept before writing the relation.",
                "If this is a new durable predicate, create it as an instance of #V#predicate before using it.",
                "Do not invent ad-hoc field-name predicates during a write.",
            ],
        ) from exc

    if not isinstance(predicate_doc, Mapping) or not _looks_like_predicate_doc(
        predicate_doc
    ):
        kind = None
        if isinstance(predicate_doc, Mapping):
            metadata = predicate_doc.get("metadata")
            kind = predicate_doc.get("kind")
            if not kind and isinstance(metadata, Mapping):
                kind = metadata.get("concept_type")
        raise TextRelationPredicateResolutionError(
            "predicate_concept_not_predicate",
            f"Concept '{predicate_text}' exists but is not a predicate concept.",
            details={"predicate": predicate_text, "kind": kind},
            suggestions=[
                "Use a concept that is an instance of #V#predicate.",
                "If the target is an entity, type, or other represented value, use a relationship predicate rather than a text-relation field name.",
            ],
        )

    return TextRelationPredicateResolution(
        input_predicate=predicate_text,
        storage_predicate=predicate_text,
        predicate_concept_id=predicate_text,
        predicate_doc=predicate_doc,
    )


__all__ = [
    "TextRelationPredicateResolution",
    "TextRelationPredicateResolutionError",
    "resolve_text_relation_predicate_for_write",
]
