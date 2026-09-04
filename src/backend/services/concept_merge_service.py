"""Exact, authority-bound concept identity consolidation.

The public ``merge_concepts`` function remains the compatibility entry point,
but execution now requires a server-built plan covered by the current governed
ontology intent.  Preview is read-only.  The plan enumerates every concept
document whose relationships would change, every source text relation, and the
complete source/target documents.  Execution is conditionally idempotent: each
step may be in its exact before or exact after state and any third state stops
the merge with an honest partial/indeterminate result.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import bypass_access_control, can_access_concept
from ..security.visibility_predicates import (
    SPECIFIC_TO_ORG_PREDICATES_READ,
    SPECIFIC_TO_USER_PREDICATES,
)
from .ontology_publication_authority_service import (
    RESERVED_GENERIC_MUTATION_PREDICATES,
    current_authorised_ontology_intent,
)
from .relationship_write_service import is_structural_predicate
from .text_value_service import upsert_text_for_concept

logger = logging.getLogger(__name__)

PROTECTED_CONCEPTS = {"#V#Thing", "#V#Root", "#V#System"}
MERGE_PLAN_SCHEMA_VERSION = "ontology_identity_consolidation_plan.v1"
_SCOPE_PREDICATES = frozenset(
    {*SPECIFIC_TO_USER_PREDICATES, *SPECIFIC_TO_ORG_PREDICATES_READ}
)
_AUTHORITY_PREDICATES = (
    frozenset(RESERVED_GENERIC_MUTATION_PREDICATES) - _SCOPE_PREDICATES
)
_TARGET_IDENTITY_FIELDS = frozenset(
    {
        "_id",
        "concept_id",
        "guid",
        "created_at",
        "created_timestamp",
        "updated_at",
        "embedding_status",
        "relationships",
        "names",
        "name",
        "attributes",
        "system_tags",
        "user_tags",
    }
)
# These fields describe a stored concept document's derived index/cache state,
# not the represented meaning being consolidated.  The surviving document owns
# any existing values and the applicable index/cache workers may recompute them;
# source values must neither overwrite it nor create a semantic metadata
# conflict.  Keep this list explicit so unfamiliar metadata still fails closed.
_TARGET_OWNED_DERIVED_FIELDS = frozenset(
    {
        "embedding_updated_at",
        "embedding_error",
        "last_db_update_timestamp",
    }
)
_RECOMPUTED_DERIVED_FIELDS = frozenset(
    {
        "inherited_salient_binary_predicates",
        "inherited_salient_computed_at",
        "inherited_salient_error",
    }
)


@dataclass(frozen=True)
class ConceptMergePlanError(ValueError):
    reason_code: str
    public_message: str

    def __str__(self) -> str:
        return self.public_message


class _ConceptMergeApplyError(RuntimeError):
    def __init__(self, reason_code: str, public_message: str, *, changed: bool):
        super().__init__(public_message)
        self.reason_code = reason_code
        self.public_message = public_message
        self.changed = changed


def _normalise_concept_id(value: Any) -> str | None:
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001 - stable string fallback below
            return str(value)
    return str(value)


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_hash(plan: Mapping[str, Any]) -> str:
    return _sha256({key: value for key, value in plan.items() if key != "plan_sha256"})


def _ordered_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _replace_value(value: Any, source_id: str, target_id: str) -> tuple[Any, bool]:
    if isinstance(value, str):
        return (target_id, True) if value == source_id else (value, False)
    if isinstance(value, list):
        changed = False
        replaced: list[Any] = []
        for item in value:
            next_item, item_changed = _replace_value(item, source_id, target_id)
            replaced.append(next_item)
            changed = changed or item_changed
        if all(isinstance(item, str) for item in replaced):
            deduped = _ordered_unique(replaced)
            return deduped, changed or deduped != replaced
        return replaced, changed
    if isinstance(value, Mapping):
        changed = False
        replaced_map: dict[str, Any] = {}
        for key, item in value.items():
            next_item, item_changed = _replace_value(item, source_id, target_id)
            replaced_map[str(key)] = next_item
            changed = changed or item_changed
        return replaced_map, changed
    return value, False


def _replace_relationships(
    relationships: Any,
    source_id: str,
    target_id: str,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(relationships, Mapping):
        return {}, False
    changed = False
    replaced: dict[str, Any] = {}
    for predicate, targets in relationships.items():
        next_targets, targets_changed = _replace_value(
            targets,
            source_id,
            target_id,
        )
        replaced[str(predicate)] = next_targets
        changed = changed or targets_changed
    return replaced, changed


def _merge_relationship_maps(
    target_relationships: Mapping[str, Any],
    source_relationships: Mapping[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = deepcopy(dict(target_relationships))
    for predicate, source_targets in source_relationships.items():
        if predicate in _SCOPE_PREDICATES:
            # Publication scope belongs to the surviving identity. Source scope
            # is authorised as a retraction, never copied into the destination.
            continue
        if predicate in _AUTHORITY_PREDICATES and source_targets:
            raise ConceptMergePlanError(
                "identity_consolidation_authority_lifecycle_required",
                (
                    "An identity carrying authority or membership state must be "
                    "migrated through its dedicated role lifecycle before merge."
                ),
            )
        if predicate not in merged:
            merged[predicate] = deepcopy(source_targets)
            continue
        target_targets = merged.get(predicate)
        if isinstance(target_targets, list) or isinstance(source_targets, list):
            target_list = (
                list(target_targets)
                if isinstance(target_targets, list)
                else ([target_targets] if isinstance(target_targets, str) else [])
            )
            source_list = (
                list(source_targets)
                if isinstance(source_targets, list)
                else ([source_targets] if isinstance(source_targets, str) else [])
            )
            if all(isinstance(item, str) for item in (*target_list, *source_list)):
                merged[predicate] = _ordered_unique([*target_list, *source_list])
            elif target_targets != source_targets:
                raise ConceptMergePlanError(
                    "identity_consolidation_relationship_conflict",
                    (
                        "The source and target contain incompatible structured "
                        "relationship values."
                    ),
                )
        elif target_targets != source_targets:
            merged[predicate] = [target_targets, source_targets]
    return merged


def _relationship_value_mentions(value: Any, concept_id: str) -> bool:
    if isinstance(value, str):
        return value == concept_id
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return any(_relationship_value_mentions(item, concept_id) for item in value)
    if isinstance(value, Mapping):
        return any(
            _relationship_value_mentions(item, concept_id) for item in value.values()
        )
    return False


def _without_relationship_target(value: Any, concept_id: str) -> Any:
    if isinstance(value, str):
        return [] if value == concept_id else value
    if isinstance(value, list):
        return [
            item for item in value if not (isinstance(item, str) and item == concept_id)
        ]
    return value


def _remove_new_identity_collapse_self_edges(
    merged_relationships: Mapping[str, Any],
    *,
    source_relationships: Mapping[str, Any],
    target_relationships: Mapping[str, Any],
    source_id: str,
    target_id: str,
) -> dict[str, Any]:
    """Remove only structural self-edges introduced by identity collapse.

    A target's already-stored self-edge is left alone.  Otherwise, an edge can
    become target-to-target when the target pointed to the source or when a
    source edge to either identity is transferred to the target.
    """

    cleaned = deepcopy(dict(merged_relationships))
    for predicate, merged_targets in merged_relationships.items():
        if not is_structural_predicate(str(predicate)):
            continue
        target_before = target_relationships.get(predicate)
        if _relationship_value_mentions(target_before, target_id):
            continue
        source_before = source_relationships.get(predicate)
        collapse_created = (
            _relationship_value_mentions(target_before, source_id)
            or _relationship_value_mentions(source_before, source_id)
            or _relationship_value_mentions(source_before, target_id)
        )
        if collapse_created:
            cleaned[str(predicate)] = _without_relationship_target(
                merged_targets,
                target_id,
            )
    return cleaned


def _merge_legacy_names(
    target_doc: Mapping[str, Any],
    source_doc: Mapping[str, Any],
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, Any]] = []

    def add(entry: Any) -> None:
        if not isinstance(entry, Mapping):
            return
        name = str(entry.get("name") or "").strip()
        if not name:
            return
        language = str(entry.get("language") or "en").strip() or "en"
        kind = str(entry.get("type") or "NL").strip() or "NL"
        key = (name, language, kind)
        if key in seen:
            return
        seen.add(key)
        merged.append({"name": name, "language": language, "type": kind})

    for entry in target_doc.get("names") or []:
        add(entry)
    for entry in source_doc.get("names") or []:
        add(entry)
    source_name = str(source_doc.get("name") or "").strip()
    if source_name and source_name != str(target_doc.get("name") or "").strip():
        add({"name": source_name, "language": "en", "type": "NL"})
    return merged


def _merge_mapping_without_conflict(
    target_value: Any,
    source_value: Any,
    *,
    field_name: str,
) -> dict[str, Any]:
    target = dict(target_value) if isinstance(target_value, Mapping) else {}
    source = dict(source_value) if isinstance(source_value, Mapping) else {}
    merged = deepcopy(target)
    for key, value in source.items():
        if key in merged and merged[key] != value:
            raise ConceptMergePlanError(
                "identity_consolidation_metadata_conflict",
                f"Source and target have conflicting {field_name} metadata.",
            )
        merged.setdefault(str(key), deepcopy(value))
    return merged


def _merge_target_document(
    *,
    source_doc: Mapping[str, Any],
    target_doc: Mapping[str, Any],
    target_relationships: Mapping[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(dict(target_doc))
    merged["relationships"] = deepcopy(dict(target_relationships))
    names = _merge_legacy_names(target_doc, source_doc)
    if names or "names" in target_doc or "names" in source_doc:
        merged["names"] = names
    merged["attributes"] = _merge_mapping_without_conflict(
        target_doc.get("attributes"),
        source_doc.get("attributes"),
        field_name="attribute",
    )
    merged["system_tags"] = _ordered_unique(
        [*(target_doc.get("system_tags") or []), *(source_doc.get("system_tags") or [])]
    )
    merged["user_tags"] = _ordered_unique(
        [*(target_doc.get("user_tags") or []), *(source_doc.get("user_tags") or [])]
    )
    # This is an existing concept whose searchable content has changed.  The
    # text service uses the same status when a required source CODE alias is
    # materialised later in the exact plan, so the planned document remains an
    # exact postcondition whether or not that alias already existed.
    merged["embedding_status"] = "stale"
    # Inherited salience is a cache of the type graph that this merge changes.
    # Removing it forces the normal calculated path until the recompute worker
    # materialises a new value; retaining either identity's old cache would be
    # a plausible-looking but incorrect postcondition.
    for field_name in _RECOMPUTED_DERIVED_FIELDS:
        merged.pop(field_name, None)

    legacy_description = str(source_doc.get("description") or "").strip()
    if legacy_description:
        raise ConceptMergePlanError(
            "identity_consolidation_legacy_text_migration_required",
            (
                "The source still carries legacy inline descriptive text; migrate "
                "it to canonical text relations before identity consolidation."
            ),
        )

    for key, value in source_doc.items():
        if (
            key in _TARGET_IDENTITY_FIELDS
            or key in _TARGET_OWNED_DERIVED_FIELDS
            or key in _RECOMPUTED_DERIVED_FIELDS
            or key == "description"
        ):
            continue
        if key not in merged:
            merged[key] = deepcopy(value)
        elif merged[key] != value:
            raise ConceptMergePlanError(
                "identity_consolidation_metadata_conflict",
                "Source and target have conflicting canonical metadata.",
            )
    return merged


def _raw_concept(concept_id: str) -> dict[str, Any] | None:
    with bypass_access_control():
        document = ConceptsRepository.find_one({"concept_id": concept_id})
    return dict(document) if isinstance(document, Mapping) else None


def _raw_concepts() -> list[dict[str, Any]]:
    with bypass_access_control():
        return [
            dict(document)
            for document in ConceptsRepository.find({})
            if isinstance(document, Mapping)
        ]


def _raw_text_relations(subject_id: str) -> list[dict[str, Any]]:
    return [
        dict(relation)
        for relation in TextRelationsRepository.find({"subject_concept_id": subject_id})
        if isinstance(relation, Mapping)
    ]


def _relation_snapshot(relation: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy(dict(relation))


def _raw_text_value(object_text_id: Any) -> dict[str, Any] | None:
    text_value = TextValuesRepository.find_one({"_id": object_text_id})
    if not isinstance(text_value, Mapping):
        try:
            from bson import ObjectId

            text_value = TextValuesRepository.find_one(
                {"_id": ObjectId(str(object_text_id))}
            )
        except Exception:  # string identifiers are valid in fixtures
            text_value = None
    return dict(text_value) if isinstance(text_value, Mapping) else None


def _text_assertion_precondition(
    relation: Mapping[str, Any],
    *,
    include_text_value: bool = True,
) -> dict[str, Any]:
    relation_id = _relation_id(relation.get("_id"))
    text_value = _raw_text_value(relation.get("object_text_id"))
    if not relation_id or text_value is None:
        raise ConceptMergePlanError(
            "identity_consolidation_unaddressable_text_state",
            "A text assertion cannot be addressed and read exactly.",
        )
    relation_snapshot = _relation_snapshot(relation)
    text_value_snapshot = deepcopy(text_value)
    result = {
        "relation_id": relation_id,
        "relation": relation_snapshot,
        "relation_sha256": _sha256(relation_snapshot),
        "text_value_sha256": _sha256(text_value_snapshot),
    }
    if include_text_value:
        result["text_value"] = text_value_snapshot
    return result


def _relation_id(value: Any) -> str:
    return str(value or "").strip()


def _document_step(
    before_document: Mapping[str, Any],
    after_document: Mapping[str, Any],
) -> dict[str, Any]:
    concept_id = _normalise_concept_id(before_document.get("concept_id"))
    if not concept_id:
        raise ConceptMergePlanError(
            "identity_consolidation_unaddressable_affected_state",
            "An affected ontology record has no canonical concept identity.",
        )
    return {
        "concept_id": concept_id,
        "before_document": deepcopy(dict(before_document)),
        "after_document": deepcopy(dict(after_document)),
        "before_sha256": _sha256(before_document),
        "after_sha256": _sha256(after_document),
    }


def build_concept_merge_plan(source_id: str, target_id: str) -> dict[str, Any]:
    """Build a complete, non-disclosing identity-consolidation plan."""

    source = _normalise_concept_id(source_id)
    target = _normalise_concept_id(target_id)
    if not source or not target:
        raise ConceptMergePlanError(
            "ontology_mutation_subject_required",
            "Concept merge requires exact source and target identities.",
        )
    if source == target:
        raise ConceptMergePlanError(
            "identity_consolidation_same_identity",
            "Source and target concepts must be different.",
        )
    if source in PROTECTED_CONCEPTS:
        raise ConceptMergePlanError(
            "identity_consolidation_protected_source",
            "The requested source concept is protected from consolidation.",
        )
    if not can_access_concept(source) or not can_access_concept(target):
        raise ConceptMergePlanError(
            "ontology_mutation_target_not_accessible",
            "The requested identity consolidation is not accessible in this context.",
        )

    source_doc = _raw_concept(source)
    target_doc = _raw_concept(target)
    if source_doc is None or target_doc is None:
        raise ConceptMergePlanError(
            "ontology_mutation_target_not_found",
            "The requested identity consolidation target was not found.",
        )

    concept_steps_by_id: dict[str, dict[str, Any]] = {}
    target_replaced_relationships, target_refs_changed = _replace_relationships(
        target_doc.get("relationships"), source, target
    )
    if target_refs_changed:
        for predicate in RESERVED_GENERIC_MUTATION_PREDICATES:
            before_value = (target_doc.get("relationships") or {}).get(predicate)
            after_value = target_replaced_relationships.get(predicate)
            if before_value != after_value:
                raise ConceptMergePlanError(
                    "identity_consolidation_authority_lifecycle_required",
                    (
                        "Identity consolidation cannot rewrite authority, membership, "
                        "or publication-scope principals."
                    ),
                )

    for document in _raw_concepts():
        concept_id = _normalise_concept_id(document.get("concept_id"))
        if concept_id == source:
            continue
        replaced, changed = _replace_relationships(
            document.get("relationships"), source, target
        )
        if not changed:
            continue
        if not concept_id:
            raise ConceptMergePlanError(
                "identity_consolidation_unaddressable_affected_state",
                "An affected ontology record has no canonical concept identity.",
            )
        for predicate in RESERVED_GENERIC_MUTATION_PREDICATES:
            before_value = (document.get("relationships") or {}).get(predicate)
            after_value = replaced.get(predicate)
            if before_value != after_value:
                raise ConceptMergePlanError(
                    "identity_consolidation_authority_lifecycle_required",
                    (
                        "Identity consolidation cannot rewrite authority, membership, "
                        "or publication-scope principals."
                    ),
                )
        if concept_id == target:
            target_replaced_relationships = replaced
            continue
        after = deepcopy(document)
        after["relationships"] = replaced
        concept_steps_by_id[concept_id] = _document_step(document, after)

    source_relationships, _ = _replace_relationships(
        source_doc.get("relationships"), source, target
    )
    merged_relationships = _merge_relationship_maps(
        target_replaced_relationships,
        source_relationships,
    )
    merged_relationships = _remove_new_identity_collapse_self_edges(
        merged_relationships,
        source_relationships=(
            source_doc.get("relationships")
            if isinstance(source_doc.get("relationships"), Mapping)
            else {}
        ),
        target_relationships=(
            target_doc.get("relationships")
            if isinstance(target_doc.get("relationships"), Mapping)
            else {}
        ),
        source_id=source,
        target_id=target,
    )
    target_after = _merge_target_document(
        source_doc=source_doc,
        target_doc=target_doc,
        target_relationships=merged_relationships,
    )
    concept_steps_by_id[target] = _document_step(target_doc, target_after)

    affected_ids = tuple(sorted({source, target, *concept_steps_by_id.keys()}))
    if not all(can_access_concept(concept_id) for concept_id in affected_ids):
        raise ConceptMergePlanError(
            "identity_consolidation_inaccessible_affected_state",
            (
                "Identity consolidation cannot proceed because its complete "
                "affected graph is not accessible to the current actor."
            ),
        )

    source_relations = _raw_text_relations(source)
    target_relations = _raw_text_relations(target)
    for relation in source_relations:
        if str(relation.get("predicate") or "").strip() in (
            RESERVED_GENERIC_MUTATION_PREDICATES
        ):
            raise ConceptMergePlanError(
                "identity_consolidation_authority_lifecycle_required",
                (
                    "An identity carrying authority, membership, or scope state "
                    "must be migrated through its dedicated lifecycle first."
                ),
            )

    target_preconditions = [
        _text_assertion_precondition(relation, include_text_value=False)
        for relation in sorted(
            target_relations,
            key=lambda row: _relation_id(row.get("_id")),
        )
    ]
    target_keys = {
        (
            str(relation.get("predicate") or "").strip(),
            str(relation.get("object_text_id") or ""),
        )
        for relation in target_relations
        if str(relation.get("predicate") or "").strip()
        and relation.get("object_text_id") is not None
    }
    text_steps: list[dict[str, Any]] = []
    for relation in sorted(
        source_relations, key=lambda row: _relation_id(row.get("_id"))
    ):
        relation_id = _relation_id(relation.get("_id"))
        predicate = str(relation.get("predicate") or "").strip()
        object_text_id = relation.get("object_text_id")
        if not relation_id or not predicate or object_text_id is None:
            raise ConceptMergePlanError(
                "identity_consolidation_unaddressable_text_state",
                "A source text relation cannot be addressed exactly.",
            )
        key = (predicate, str(object_text_id))
        action = "delete_duplicate" if key in target_keys else "move"
        before = _relation_snapshot(relation)
        assertion_precondition = _text_assertion_precondition(relation)
        after = None
        if action == "move":
            after = deepcopy(before)
            after["subject_concept_id"] = target
            target_keys.add(key)
        text_steps.append(
            {
                "relation_id": relation_id,
                "action": action,
                "before_relation": before,
                "after_relation": after,
                "before_sha256": _sha256(before),
                "after_sha256": _sha256(after) if after is not None else None,
                "text_value": assertion_precondition["text_value"],
                "text_value_sha256": assertion_precondition["text_value_sha256"],
            }
        )

    aliases = _ordered_unique([source, str(source_doc.get("guid") or "").strip()])
    for alias in aliases:
        matching_target_names = []
        for relation in target_relations:
            if str(relation.get("predicate") or "") not in {"hasName", "#V#hasName"}:
                continue
            text_value = _raw_text_value(relation.get("object_text_id"))
            if (
                isinstance(text_value, Mapping)
                and str(text_value.get("text") or "") == alias
            ):
                matching_target_names.append(relation)
        if matching_target_names and not any(
            isinstance(relation.get("context"), Mapping)
            and relation["context"].get("name_type") == "CODE"
            for relation in matching_target_names
        ):
            raise ConceptMergePlanError(
                "identity_consolidation_alias_context_conflict",
                (
                    "A required source identifier already exists on the target "
                    "with incompatible name context. Resolve that name explicitly "
                    "before identity consolidation."
                ),
            )
    plan: dict[str, Any] = {
        "schema_version": MERGE_PLAN_SCHEMA_VERSION,
        "source_id": source,
        "target_id": target,
        "source_before_document": deepcopy(source_doc),
        "source_before_sha256": _sha256(source_doc),
        "concept_steps": [
            concept_steps_by_id[concept_id]
            for concept_id in sorted(concept_steps_by_id)
        ],
        "text_relation_steps": text_steps,
        "target_text_relation_preconditions": target_preconditions,
        "required_code_aliases": aliases,
        "affected_concept_ids": list(affected_ids),
        # Lock every existing source and target text relation while the exact
        # plan is rechecked and applied.  Target rows participate in duplicate
        # resolution even when they do not themselves become mutation steps.
        "affected_text_relation_ids": sorted(
            {
                *(
                    step["relation_id"]
                    for step in text_steps
                    if step.get("relation_id")
                ),
                *(
                    relation_id
                    for relation in target_relations
                    if (relation_id := _relation_id(relation.get("_id")))
                ),
            }
        ),
    }
    plan["plan_sha256"] = _plan_hash(plan)
    return plan


def _public_plan_report(plan: Mapping[str, Any], *, simulate: bool) -> dict[str, Any]:
    concept_steps = list(plan.get("concept_steps") or [])
    text_steps = list(plan.get("text_relation_steps") or [])
    incoming_count = sum(
        1
        for step in concept_steps
        if isinstance(step, Mapping) and step.get("concept_id") != plan.get("target_id")
    )
    return {
        "success": True,
        "simulate": simulate,
        "executed": False,
        "changed": False,
        "source_id": plan.get("source_id"),
        "target_id": plan.get("target_id"),
        "plan_sha256": plan.get("plan_sha256"),
        "affected_concept_count": len(plan.get("affected_concept_ids") or []),
        "operations": [
            {
                "type": "rewrite_incoming_relationship_references",
                "count": incoming_count,
            },
            {"type": "merge_source_into_target", "count": 1},
            {
                "type": "migrate_text_relations",
                "move_count": sum(
                    1
                    for step in text_steps
                    if isinstance(step, Mapping) and step.get("action") == "move"
                ),
                "duplicate_count": sum(
                    1
                    for step in text_steps
                    if isinstance(step, Mapping)
                    and step.get("action") == "delete_duplicate"
                ),
            },
            {"type": "delete_source", "count": 1},
        ],
        "warnings": [],
        "errors": [],
    }


def _validate_plan(plan: Mapping[str, Any], source_id: str, target_id: str) -> None:
    if plan.get("schema_version") != MERGE_PLAN_SCHEMA_VERSION:
        raise _ConceptMergeApplyError(
            "identity_consolidation_plan_invalid",
            "The identity-consolidation plan version is invalid.",
            changed=False,
        )
    if (
        _normalise_concept_id(plan.get("source_id")) != source_id
        or _normalise_concept_id(plan.get("target_id")) != target_id
        or str(plan.get("plan_sha256") or "") != _plan_hash(plan)
    ):
        raise _ConceptMergeApplyError(
            "identity_consolidation_plan_invalid",
            "The identity-consolidation plan does not match this exact effect.",
            changed=False,
        )
    intent = current_authorised_ontology_intent()
    if (
        intent is None
        or intent.operation != "concept.merge"
        or str(intent.delta.get("merge_plan_sha256") or "")
        != str(plan.get("plan_sha256") or "")
        or set(intent.target_concept_ids) != set(plan.get("affected_concept_ids") or [])
    ):
        raise _ConceptMergeApplyError(
            "ontology_identity_consolidation_authority_required",
            (
                "Identity consolidation requires an exact currently authorised "
                "merge plan."
            ),
            changed=False,
        )


def _raw_relation_by_id(relation_id: Any) -> dict[str, Any] | None:
    raw_id = relation_id
    try:
        from bson import ObjectId

        raw_id = ObjectId(str(relation_id))
    except Exception:  # noqa: BLE001 - string IDs are valid in fixtures
        raw_id = relation_id
    relation = TextRelationsRepository.find_one({"_id": raw_id})
    return dict(relation) if isinstance(relation, Mapping) else None


def _conditional_update_document(step: Mapping[str, Any]) -> bool:
    concept_id = _normalise_concept_id(step.get("concept_id"))
    before = step.get("before_document")
    after = step.get("after_document")
    if (
        not concept_id
        or not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
    ):
        raise _ConceptMergeApplyError(
            "identity_consolidation_plan_invalid",
            "An affected concept step is incomplete.",
            changed=False,
        )
    current = _raw_concept(concept_id)
    if current is not None and _sha256(current) == step.get("after_sha256"):
        return False
    if current is None or _sha256(current) != step.get("before_sha256"):
        raise _ConceptMergeApplyError(
            "identity_consolidation_precondition_failed",
            "An affected concept changed after the merge plan was authorised.",
            changed=False,
        )
    update_fields = {
        key: deepcopy(value) for key, value in after.items() if key != "_id"
    }
    unset_fields = {key: "" for key in before if key != "_id" and key not in after}
    update: dict[str, Any] = {"$set": update_fields}
    if unset_fields:
        update["$unset"] = unset_fields
    result = ConceptsRepository.update_one(
        deepcopy(dict(before)),
        update,
    )
    if int(getattr(result, "modified_count", 0) or 0) != 1:
        refreshed = _raw_concept(concept_id)
        if refreshed is None or _sha256(refreshed) != step.get("after_sha256"):
            raise _ConceptMergeApplyError(
                "identity_consolidation_precondition_failed",
                "An affected concept could not be conditionally updated.",
                changed=False,
            )
        return False
    return True


def _conditional_apply_text_step(step: Mapping[str, Any]) -> bool:
    relation_id = str(step.get("relation_id") or "")
    before = step.get("before_relation")
    after = step.get("after_relation")
    action = str(step.get("action") or "")
    if not relation_id or not isinstance(before, Mapping):
        raise _ConceptMergeApplyError(
            "identity_consolidation_plan_invalid",
            "A text-relation merge step is incomplete.",
            changed=False,
        )
    current = _raw_relation_by_id(before.get("_id") or relation_id)
    if action == "delete_duplicate":
        if current is None:
            return False
        current_text_value = _raw_text_value(current.get("object_text_id"))
        if current_text_value is None or _sha256(current_text_value) != step.get(
            "text_value_sha256"
        ):
            raise _ConceptMergeApplyError(
                "identity_consolidation_precondition_failed",
                "A source text value changed after merge authorisation.",
                changed=False,
            )
        if _sha256(current) != step.get("before_sha256"):
            raise _ConceptMergeApplyError(
                "identity_consolidation_precondition_failed",
                "A source text relation changed after merge authorisation.",
                changed=False,
            )
        result = TextRelationsRepository.delete_one(deepcopy(dict(before)))
        if int(getattr(result, "deleted_count", 0) or 0) != 1:
            if _raw_relation_by_id(before.get("_id") or relation_id) is not None:
                raise _ConceptMergeApplyError(
                    "identity_consolidation_precondition_failed",
                    "A duplicate source text relation could not be removed.",
                    changed=False,
                )
            return False
        return True
    if action != "move" or not isinstance(after, Mapping):
        raise _ConceptMergeApplyError(
            "identity_consolidation_plan_invalid",
            "A text-relation merge action is invalid.",
            changed=False,
        )
    current_text_value = (
        _raw_text_value(current.get("object_text_id")) if current is not None else None
    )
    if current_text_value is None or _sha256(current_text_value) != step.get(
        "text_value_sha256"
    ):
        raise _ConceptMergeApplyError(
            "identity_consolidation_precondition_failed",
            "A source text value changed after merge authorisation.",
            changed=False,
        )
    if _sha256(current) == step.get("after_sha256"):
        return False
    if current is None or _sha256(current) != step.get("before_sha256"):
        raise _ConceptMergeApplyError(
            "identity_consolidation_precondition_failed",
            "A source text relation changed after merge authorisation.",
            changed=False,
        )
    result = TextRelationsRepository.update_one(
        deepcopy(dict(before)),
        {"$set": {"subject_concept_id": after.get("subject_concept_id")}},
    )
    if int(getattr(result, "modified_count", 0) or 0) != 1:
        refreshed = _raw_relation_by_id(before.get("_id") or relation_id)
        if refreshed is None or _sha256(refreshed) != step.get("after_sha256"):
            raise _ConceptMergeApplyError(
                "identity_consolidation_precondition_failed",
                "A source text relation could not be conditionally moved.",
                changed=False,
            )
        return False
    return True


def _target_has_code_alias(target_id: str, alias: str) -> bool:
    for relation in _raw_text_relations(target_id):
        if str(relation.get("predicate") or "") not in {"hasName", "#V#hasName"}:
            continue
        context = relation.get("context")
        if not isinstance(context, Mapping) or context.get("name_type") != "CODE":
            continue
        text_value = TextValuesRepository.find_one_by_id(relation.get("object_text_id"))
        if (
            isinstance(text_value, Mapping)
            and str(text_value.get("text") or "") == alias
        ):
            return True
    return False


def _ensure_code_alias(target_id: str, alias: str, source_id: str) -> bool:
    if _target_has_code_alias(target_id, alias):
        return False
    result = upsert_text_for_concept(
        subject_concept_id=target_id,
        predicate="hasName",
        text=alias,
        lang="en-NZ",
        context={"name_type": "CODE"},
        provenance={
            "source": "ontology_identity_consolidation",
            "source_concept_id": source_id,
        },
    )
    if not bool(result.get("success", True)) or not _target_has_code_alias(
        target_id, alias
    ):
        raise _ConceptMergeApplyError(
            "identity_consolidation_alias_read_back_failed",
            "A source identity alias could not be canonically read back.",
            changed=True,
        )
    return True


def _conditional_delete_source(plan: Mapping[str, Any]) -> bool:
    source_id = _normalise_concept_id(plan.get("source_id"))
    before = plan.get("source_before_document")
    if not source_id or not isinstance(before, Mapping):
        raise _ConceptMergeApplyError(
            "identity_consolidation_plan_invalid",
            "The source deletion precondition is incomplete.",
            changed=False,
        )
    current = _raw_concept(source_id)
    if current is None:
        return False
    if _sha256(current) != plan.get("source_before_sha256"):
        raise _ConceptMergeApplyError(
            "identity_consolidation_precondition_failed",
            "The source concept changed after merge authorisation.",
            changed=False,
        )
    result = ConceptsRepository.delete_one(deepcopy(dict(before)))
    if int(getattr(result, "deleted_count", 0) or 0) != 1:
        if _raw_concept(source_id) is not None:
            raise _ConceptMergeApplyError(
                "identity_consolidation_precondition_failed",
                "The source concept could not be conditionally deleted.",
                changed=False,
            )
        return False
    return True


def read_concept_merge_postcondition(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded complete read-back without disclosing unexpected records."""

    plan_hash = str(plan.get("plan_sha256") or "")
    concept_steps = list(plan.get("concept_steps") or [])
    concept_matches = 0
    for step in concept_steps:
        if not isinstance(step, Mapping):
            continue
        concept_id = _normalise_concept_id(step.get("concept_id"))
        current = _raw_concept(concept_id or "") if concept_id else None
        if current is not None and _sha256(current) == step.get("after_sha256"):
            concept_matches += 1

    text_steps = list(plan.get("text_relation_steps") or [])
    text_matches = 0
    for step in text_steps:
        if not isinstance(step, Mapping):
            continue
        before = step.get("before_relation")
        relation_id = (
            before.get("_id")
            if isinstance(before, Mapping)
            else step.get("relation_id")
        )
        current = _raw_relation_by_id(relation_id)
        if step.get("action") == "delete_duplicate":
            matched = current is None
        else:
            matched = current is not None and _sha256(current) == step.get(
                "after_sha256"
            )
        if matched:
            text_matches += 1

    target_text_preconditions = list(
        plan.get("target_text_relation_preconditions") or []
    )
    target_text_precondition_matches = 0
    for precondition in target_text_preconditions:
        if not isinstance(precondition, Mapping):
            continue
        relation = precondition.get("relation")
        relation_id = (
            relation.get("_id")
            if isinstance(relation, Mapping)
            else precondition.get("relation_id")
        )
        current_relation = _raw_relation_by_id(relation_id)
        current_text_value = (
            _raw_text_value(current_relation.get("object_text_id"))
            if current_relation is not None
            else None
        )
        if (
            current_relation is not None
            and current_text_value is not None
            and _sha256(current_relation) == precondition.get("relation_sha256")
            and _sha256(current_text_value) == precondition.get("text_value_sha256")
        ):
            target_text_precondition_matches += 1

    source_id = _normalise_concept_id(plan.get("source_id")) or ""
    target_id = _normalise_concept_id(plan.get("target_id")) or ""
    aliases = [
        alias
        for alias in plan.get("required_code_aliases") or []
        if isinstance(alias, str) and alias
    ]
    alias_matches = sum(
        1 for alias in aliases if _target_has_code_alias(target_id, alias)
    )
    residual_reference_count = 0
    for document in _raw_concepts():
        _replaced, changed = _replace_relationships(
            document.get("relationships"), source_id, target_id
        )
        if changed:
            residual_reference_count += 1
    residual_source_text_count = len(_raw_text_relations(source_id))
    source_absent = _raw_concept(source_id) is None
    matches = (
        bool(plan_hash)
        and source_absent
        and concept_matches == len(concept_steps)
        and text_matches == len(text_steps)
        and target_text_precondition_matches == len(target_text_preconditions)
        and alias_matches == len(aliases)
        and residual_reference_count == 0
        and residual_source_text_count == 0
    )
    return {
        "schema_version": "ontology_identity_consolidation_read_back.v1",
        "plan_sha256": plan_hash,
        "source_id": source_id,
        "target_id": target_id,
        "source_absent": source_absent,
        "concept_step_count": len(concept_steps),
        "concept_step_match_count": concept_matches,
        "text_relation_step_count": len(text_steps),
        "text_relation_step_match_count": text_matches,
        "target_text_precondition_count": len(target_text_preconditions),
        "target_text_precondition_match_count": target_text_precondition_matches,
        "required_alias_count": len(aliases),
        "required_alias_match_count": alias_matches,
        "residual_source_reference_count": residual_reference_count,
        "residual_source_text_relation_count": residual_source_text_count,
        "matches_exact_plan": matches,
    }


def _execute_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    source_id = _normalise_concept_id(plan.get("source_id")) or ""
    target_id = _normalise_concept_id(plan.get("target_id")) or ""
    changed = False
    try:
        _validate_plan(plan, source_id, target_id)
        for step in plan.get("concept_steps") or []:
            if not isinstance(step, Mapping):
                raise _ConceptMergeApplyError(
                    "identity_consolidation_plan_invalid",
                    "An affected concept step is invalid.",
                    changed=changed,
                )
            try:
                changed = _conditional_update_document(step) or changed
            except _ConceptMergeApplyError as exc:
                raise _ConceptMergeApplyError(
                    exc.reason_code,
                    exc.public_message,
                    changed=changed or exc.changed,
                ) from exc
        for step in plan.get("text_relation_steps") or []:
            if not isinstance(step, Mapping):
                raise _ConceptMergeApplyError(
                    "identity_consolidation_plan_invalid",
                    "A text-relation step is invalid.",
                    changed=changed,
                )
            try:
                changed = _conditional_apply_text_step(step) or changed
            except _ConceptMergeApplyError as exc:
                raise _ConceptMergeApplyError(
                    exc.reason_code,
                    exc.public_message,
                    changed=changed or exc.changed,
                ) from exc
        for alias in plan.get("required_code_aliases") or []:
            if isinstance(alias, str) and alias:
                changed = _ensure_code_alias(target_id, alias, source_id) or changed
        try:
            changed = _conditional_delete_source(plan) or changed
        except _ConceptMergeApplyError as exc:
            raise _ConceptMergeApplyError(
                exc.reason_code,
                exc.public_message,
                changed=changed or exc.changed,
            ) from exc
    except _ConceptMergeApplyError as exc:
        return {
            "success": False,
            "simulate": False,
            "executed": changed,
            "changed": changed,
            "mutation_outcome": "partial" if changed else "not_started",
            "source_id": source_id,
            "target_id": target_id,
            "plan_sha256": plan.get("plan_sha256"),
            "error_code": exc.reason_code,
            "error": exc.public_message,
            "errors": [exc.public_message],
        }

    try:
        from .concept_service import _invalidate_concept_mutation_caches

        _invalidate_concept_mutation_caches()
    except Exception:  # read-back still decides effect success
        logger.warning(
            "Identity consolidation cache invalidation failed",
            exc_info=True,
        )
    return {
        **_public_plan_report(plan, simulate=False),
        "success": True,
        "executed": True,
        "changed": changed,
        "mutation_outcome": "succeeded",
    }


def merge_concepts(
    source_id: str,
    target_id: str,
    simulate: bool = True,
    *,
    exact_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview or execute one exact governed identity consolidation."""

    try:
        plan = (
            dict(exact_plan)
            if isinstance(exact_plan, Mapping)
            else build_concept_merge_plan(source_id, target_id)
        )
        if simulate:
            return _public_plan_report(plan, simulate=True)
        if exact_plan is None:
            return {
                "success": False,
                "simulate": False,
                "executed": False,
                "changed": False,
                "mutation_outcome": "not_started",
                "source_id": _normalise_concept_id(source_id),
                "target_id": _normalise_concept_id(target_id),
                "error_code": "ontology_identity_consolidation_authority_required",
                "error": (
                    "Identity consolidation execution requires a server-built "
                    "plan covered by the governed ontology command."
                ),
                "errors": [
                    "Identity consolidation execution requires a governed plan."
                ],
            }
        return _execute_plan(plan)
    except ConceptMergePlanError as exc:
        return {
            "success": False,
            "simulate": bool(simulate),
            "executed": False,
            "changed": False,
            "mutation_outcome": "not_started",
            "source_id": _normalise_concept_id(source_id),
            "target_id": _normalise_concept_id(target_id),
            "error_code": exc.reason_code,
            "error": exc.public_message,
            "errors": [exc.public_message],
        }


__all__ = [
    "MERGE_PLAN_SCHEMA_VERSION",
    "ConceptMergePlanError",
    "build_concept_merge_plan",
    "merge_concepts",
    "read_concept_merge_postcondition",
]
