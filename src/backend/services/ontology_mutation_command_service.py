"""Shared command adapter for externally reachable canonical ontology writes."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import (
    bypass_access_control,
    can_access_concept,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from ..security.visibility_predicates import (
    SPECIFIC_TO_ORG_PREDICATES_READ,
    SPECIFIC_TO_USER_PREDICATES,
)
from .actor_scoped_referent_identity_service import (
    ACTOR_SCOPED_REFERENT_COLLISION_MODE,
    ACTOR_SCOPED_REFERENT_RECOVERY_ACTION,
    ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT,
    ACTOR_SCOPED_REFERENT_SCOPE_MODE,
    actor_scoped_referent_concept_id,
)
from .concept_predicate_metadata_service import get_structural_inverse_map
from .ontology_publication_authority_service import (
    RESERVED_GENERIC_MUTATION_PREDICATES,
    OntologyMutationIntent,
    OntologyMutationResourceBusy,
    PublicationContext,
    actor_can_access_intent_targets,
    authorise_ontology_mutation,
    concept_publication_context,
    current_ontology_invocation,
    execute_authorised_ontology_mutation,
    issue_agent_delegation,
    ontology_authority_denial_payload,
    ontology_authority_resource_keys,
    ontology_mutation_resource_lock,
    publication_context_for_creation,
    reconcile_indeterminate_mutation_receipt,
)
from .relationship_write_service import (
    CORE_RELATIONSHIP_TEXT_PREDICATES,
    is_structural_predicate,
    normalise_structural_predicate,
)
from .text_relation_predicate_validation_service import (
    TextRelationPredicateResolutionError,
    resolve_text_relation_predicate_for_write,
)

logger = logging.getLogger(__name__)


@dataclass
class OntologyMutationCommandError(Exception):
    reason_code: str
    public_message: str
    error_details: Mapping[str, Any] | None = None
    recovery_affordances: Sequence[Mapping[str, Any]] | None = None

    def __str__(self) -> str:
        return self.public_message


_METHOD_OPERATION = {
    "create_concepts": "concept.create",
    "upsert_text_relation": "text.upsert",
    "update_text_relation": "text.update",
    "upsert_singleton_text_relation": "text.upsert",
    "add_names_to_concept": "text.upsert",
    "delete_text_relation": "text.delete",
    "delete_legacy_name": "text.delete",
    "add_relationship": "relationship.add",
    "remove_relationship": "relationship.remove",
    "remove_relationships_bulk": "relationship.remove",
    "promote_uncertain_relationship_assertion": "relationship.promote",
    "delete_concept": "concept.delete",
    "merge_concepts": "concept.merge",
    "rename_concept": "concept.rename",
    "update_concept": "concept.update",
    "preview_concept_publication_scope_change": "scope.change",
    "change_concept_publication_scope": "scope.change",
}

_UNTRUSTED_ONTOLOGY_IDENTITY_FIELDS = frozenset(
    {
        "actor",
        "actor_concept_id",
        "actor_id",
        "admin",
        "administrator",
        "allow_admin",
        "authority",
        "authority_role",
        "authority_resolved_parent_id",
        "created_by",
        "created_by_concept_id",
        "global_admin",
        "is_admin",
        "is_operator",
        "namespace",
        "organisation_concept_id",
        "organisation_id",
        "organization_concept_id",
        "organization_id",
        "org_id",
        "operator",
        "operator_override",
        "roles",
        "user",
        "user_concept_id",
        "user_id",
    }
)

_INTENT_TRANSPORT_FIELDS = frozenset(
    {
        "effect_id",
        "idempotency_key",
        "ontology_delegation_id",
        "ontology_effect_id",
        "operator",
        "request_id",
    }
)

_CREATE_CORE_CONCEPT_FIELDS = frozenset(
    {
        "concept_id",
        "description",
        "instance_of_type",
        "kind",
        "name",
        "notes",
        "vontology_path",
    }
)


def is_ontology_mutation_method(method_name: str) -> bool:
    return str(method_name or "").strip() in _METHOD_OPERATION


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise_concept_id(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"


def _target_is_concept(value: Any) -> bool:
    return bool(_clean_text(value).startswith("#V#"))


def _normalise_predicate(value: Any) -> str | None:
    cleaned = _clean_text(value)
    return cleaned or None


def _text_value_snapshot(object_text_id: Any) -> Mapping[str, Any] | None:
    if object_text_id is None:
        return None
    try:
        from bson import ObjectId

        query_id = (
            object_text_id
            if isinstance(object_text_id, ObjectId)
            else ObjectId(str(object_text_id))
        )
    except Exception:  # noqa: BLE001 - fixtures may use string identifiers
        query_id = object_text_id
    value = TextValuesRepository.find_one({"_id": query_id})
    return value if isinstance(value, Mapping) else None


def _text_relation_snapshot(
    *,
    subject_concept_id: str,
    relation_id: str,
) -> dict[str, Any]:
    """Resolve an opaque relation ID to its canonical subject and predicate."""

    cleaned_relation_id = _clean_text(relation_id)
    if not cleaned_relation_id:
        raise OntologyMutationCommandError(
            "text_relation_id_required",
            "An exact text relation ID is required.",
        )
    try:
        from bson import ObjectId

        relation_query: dict[str, Any] = {"_id": ObjectId(cleaned_relation_id)}
    except Exception:  # noqa: BLE001 - fixtures may use string identifiers
        relation_query = {"_id": cleaned_relation_id}
    relation = TextRelationsRepository.find_one(relation_query)
    if not isinstance(relation, Mapping):
        raise OntologyMutationCommandError(
            "ontology_mutation_target_not_found",
            "The requested text relation was not found.",
        )
    stored_subject = _normalise_concept_id(relation.get("subject_concept_id"))
    stored_predicate = _normalise_predicate(relation.get("predicate"))
    if not stored_subject or stored_subject != subject_concept_id:
        raise OntologyMutationCommandError(
            "ontology_mutation_target_not_found",
            "The requested text relation was not found for this concept.",
        )
    if not stored_predicate:
        raise OntologyMutationCommandError(
            "text_relation_predicate_unavailable",
            "The stored text relation predicate is unavailable.",
        )
    text_value = _text_value_snapshot(relation.get("object_text_id"))
    snapshot = {
        "relation_id": str(relation.get("_id") or cleaned_relation_id),
        "subject_concept_id": stored_subject,
        "predicate": stored_predicate,
        "object_text_id": str(relation.get("object_text_id") or "") or None,
        "current_text_sha256": (
            _text_sha256(text_value.get("text"))
            if isinstance(text_value, Mapping)
            else None
        ),
        "current_language": (
            _clean_text(text_value.get("lang"))
            if isinstance(text_value, Mapping)
            else None
        ),
        "current_context_sha256": _canonical_json_sha256(relation.get("context") or {}),
        "current_provenance_sha256": _canonical_json_sha256(
            text_value.get("provenance") or {}
            if isinstance(text_value, Mapping)
            else {}
        ),
        "updated_at": str(relation.get("updated_at") or "") or None,
    }
    snapshot["revision"] = _effect_arguments_sha256(snapshot)
    return snapshot


def normalise_governed_ontology_arguments(
    method_name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove authority claims and inject only server-established provenance."""

    method = _clean_text(method_name)
    normalised = {
        key: value
        for key, value in arguments.items()
        if key not in _UNTRUSTED_ONTOLOGY_IDENTITY_FIELDS
        and key not in {"ontology_delegation_id", "ontology_effect_id"}
    }
    actor_id = get_effective_user_concept_id()
    organisation_id = get_effective_organisation_concept_id()
    invocation = current_ontology_invocation()
    if method == "create_concepts":
        if actor_id:
            normalised["created_by_concept_id"] = actor_id
        if organisation_id:
            normalised["organisation_concept_id"] = organisation_id
        # Canonical create authority is confined to the new child documents.
        # Existing referenced concepts must not be changed as an implicit
        # inverse side effect.
        normalised["maintain_relationship_inverses"] = False
        normalised["resolve_visibility_from_event_namespace"] = False
    if method == "promote_uncertain_relationship_assertion":
        operator_id = (
            invocation.executing_agent_concept_id
            if invocation and invocation.executing_agent_concept_id
            else actor_id
        )
        normalised["operator"] = operator_id or "governed_ontology_mutation"
    if method in {"remove_relationship", "remove_relationships_bulk"}:
        normalised["operator_override"] = False
    return normalised


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
    return str(value)


def _effect_arguments_sha256(arguments: Mapping[str, Any]) -> str:
    effect_arguments = {
        key: value
        for key, value in arguments.items()
        if key not in _INTENT_TRANSPORT_FIELDS
    }
    encoded = json.dumps(
        _json_safe(effect_arguments),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    """Hash one JSON-safe semantic value without exposing it in receipts."""

    encoded = json.dumps(
        _json_safe(value),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _raw_json_sha256(value: Any) -> str:
    """Hash a JSON-safe value without changing any Unicode string value.

    ``ensure_ascii=False`` makes the byte contract explicit: composed and
    decomposed Unicode remain distinct UTF-8 byte sequences.  Sorting object
    keys makes hashes stable without normalising, trimming, or case-folding
    any stored string.
    """

    encoded = json.dumps(
        _json_safe(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def legacy_name_selector_metadata(
    *,
    concept_id: str,
    names: Sequence[Any],
    ordinal: int,
) -> dict[str, Any]:
    """Return the opaque exact selector exposed for one legacy ``names[]`` row."""

    canonical_id = _normalise_concept_id(concept_id)
    if canonical_id is None:
        raise ValueError("concept_id is required")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise TypeError("legacy name ordinal must be an integer")
    snapshot = list(names)
    if ordinal < 0 or ordinal >= len(snapshot):
        raise IndexError("legacy name ordinal is out of range")
    return {
        "concept_id": canonical_id,
        "ordinal": ordinal,
        "entry_sha256": _raw_json_sha256(snapshot[ordinal]),
        "names_snapshot_sha256": _raw_json_sha256(snapshot),
    }


def _legacy_name_document_snapshot(concept_id: str) -> dict[str, Any]:
    concept = ConceptsRepository.find_one({"concept_id": concept_id})
    if not isinstance(concept, Mapping):
        raise OntologyMutationCommandError(
            "ontology_mutation_target_not_found",
            "The requested ontology target was not found.",
        )
    raw_names = concept.get("names")
    names = list(raw_names) if isinstance(raw_names, list) else []
    return {
        "concept_id": concept_id,
        "names": names,
        "names_count": len(names),
        "names_snapshot_sha256": _raw_json_sha256(names),
    }


def _canonical_has_name_snapshot(concept_id: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    relations = TextRelationsRepository.find(
        {
            "subject_concept_id": concept_id,
            "predicate": {"$in": ["hasName", "#V#hasName"]},
        }
    )
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        text_value = TextValuesRepository.find_one_by_id(relation.get("object_text_id"))
        if not isinstance(text_value, Mapping) or not isinstance(
            text_value.get("text"), str
        ):
            continue
        rows.append(
            {
                "relation_id": str(relation.get("_id") or ""),
                "object_text_id": str(relation.get("object_text_id") or ""),
                "predicate": relation.get("predicate"),
                "text_sha256": hashlib.sha256(
                    text_value["text"].encode("utf-8")
                ).hexdigest(),
                "language_sha256": _raw_json_sha256(text_value.get("lang")),
                "context_sha256": _raw_json_sha256(relation.get("context") or {}),
                "provenance_sha256": _raw_json_sha256(
                    text_value.get("provenance") or {}
                ),
            }
        )
    rows.sort(key=lambda row: (row["relation_id"], row["object_text_id"]))
    return {
        "canonical_has_name_present": bool(rows),
        "canonical_has_name_count": len(rows),
        "canonical_has_name_relation_ids": [row["relation_id"] for row in rows],
        "canonical_has_name_snapshot_sha256": _raw_json_sha256(rows),
    }


def _validated_legacy_name_precondition(
    *,
    concept_id: str,
    legacy_name_selector: Any,
) -> dict[str, Any]:
    if not isinstance(legacy_name_selector, Mapping):
        raise OntologyMutationCommandError(
            "exact_legacy_name_selector_required",
            "An exact legacy name selector is required.",
        )
    selector_concept_id = _normalise_concept_id(legacy_name_selector.get("concept_id"))
    ordinal = legacy_name_selector.get("ordinal")
    entry_sha256 = _clean_text(legacy_name_selector.get("entry_sha256")).lower()
    names_snapshot_sha256 = _clean_text(
        legacy_name_selector.get("names_snapshot_sha256")
    ).lower()

    def valid_sha256(value: str) -> bool:
        return len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )

    if (
        selector_concept_id != concept_id
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or ordinal < 0
        or not valid_sha256(entry_sha256)
        or not valid_sha256(names_snapshot_sha256)
    ):
        raise OntologyMutationCommandError(
            "exact_legacy_name_selector_required",
            "The legacy name selector is incomplete or does not match this concept.",
        )

    snapshot = _legacy_name_document_snapshot(concept_id)
    if snapshot["names_snapshot_sha256"] != names_snapshot_sha256:
        raise OntologyMutationCommandError(
            "legacy_name_snapshot_precondition_failed",
            "The legacy name list changed; reload the concept and select it again.",
        )
    names = snapshot["names"]
    if ordinal >= len(names) or _raw_json_sha256(names[ordinal]) != entry_sha256:
        raise OntologyMutationCommandError(
            "legacy_name_selector_precondition_failed",
            "The selected legacy name is no longer present at that exact position.",
        )
    resulting_names = [*names[:ordinal], *names[ordinal + 1 :]]
    return {
        **snapshot,
        "ordinal": ordinal,
        "entry_sha256": entry_sha256,
        "resulting_names": resulting_names,
        "resulting_names_snapshot_sha256": _raw_json_sha256(resulting_names),
    }


def _uncertain_assertion_snapshot(
    *,
    source_id: str,
    assertion_id: str,
) -> dict[str, Any]:
    from .uncertain_relationship_service import (
        list_uncertain_relationship_assertions,
    )

    rows = list_uncertain_relationship_assertions(
        source_id=source_id,
        include_legacy=True,
    )
    assertion = next(
        (
            dict(row)
            for row in rows
            if _clean_text(row.get("assertion_id")) == assertion_id
        ),
        None,
    )
    if assertion is None:
        raise OntologyMutationCommandError(
            "uncertain_relationship_assertion_not_found",
            "The requested uncertain relationship assertion is unavailable.",
        )
    snapshot = {
        key: assertion.get(key)
        for key in (
            "assertion_id",
            "source_id",
            "predicate",
            "target",
            "target_kind",
            "status",
            "updated_at_utc",
        )
    }
    snapshot["revision"] = _effect_arguments_sha256(snapshot)
    return snapshot


def _idempotency_key(
    arguments: Mapping[str, Any],
    explicit: str | None,
) -> str | None:
    invocation = current_ontology_invocation()
    # An agent delegation is one exact effect. Caller-supplied request IDs are
    # transport labels and must not turn the same grant into multiple writes.
    if invocation and invocation.executing_agent_concept_id and invocation.effect_id:
        return _clean_text(invocation.effect_id)
    if _clean_text(explicit):
        return _clean_text(explicit)
    for key in ("idempotency_key", "request_id", "effect_id"):
        value = _clean_text(arguments.get(key))
        if value:
            return value
    return invocation.effect_id if invocation and invocation.effect_id else None


def _visible_or_fail(concept_ids: Sequence[str]) -> None:
    for concept_id in concept_ids:
        if not can_access_concept(concept_id):
            raise OntologyMutationCommandError(
                "ontology_mutation_target_not_accessible",
                "The requested ontology target is not accessible in this context.",
            )


def _concept_exists_unfiltered(concept_id: str) -> bool:
    """Check an exact ID without exposing the matching document to the caller."""

    with bypass_access_control():
        document = ConceptsRepository.find_one({"concept_id": concept_id})
    return isinstance(document, Mapping)


def _concepts_from_create_arguments(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    concept_ids: list[str] = []
    for item in arguments.get("concepts") or []:
        if not isinstance(item, Mapping):
            continue
        concept_id = _normalise_concept_id(item.get("concept_id") or item.get("id"))
        if concept_id:
            concept_ids.append(concept_id)
    parent_id = _normalise_concept_id(arguments.get("parent_id"))
    if parent_id:
        concept_ids.append(parent_id)
    return tuple(dict.fromkeys(concept_ids))


def _create_intent_concepts(arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a bounded exact projection of every requested concept delta."""

    projected: list[dict[str, Any]] = []
    for item in arguments.get("concepts") or []:
        if not isinstance(item, Mapping):
            projected.append(
                {"invalid_item_sha256": _effect_arguments_sha256({"item": item})}
            )
            continue
        projected.append(
            {
                key: _json_safe(value)
                for key, value in item.items()
                if key not in _UNTRUSTED_ONTOLOGY_IDENTITY_FIELDS
                and key not in _INTENT_TRANSPORT_FIELDS
            }
        )
    return projected


def _creation_scope_projection(
    *,
    scope_mode: Any,
    actor_concept_id: str | None,
    organisation_concept_id: str | None,
) -> dict[str, Any]:
    """Project the exact visibility edges the canonical create service stores."""

    mode = (_clean_text(scope_mode) or "user_org_default").lower().replace("-", "_")
    actor_id = _normalise_concept_id(actor_concept_id)
    organisation_id = _normalise_concept_id(organisation_concept_id)
    supported_modes = {
        "default",
        "user_org_default",
        "user_org",
        "private",
        "user_only",
        "user_only_default",
        "organisation_general",
        "organization_general",
        "org_general",
        "global",
        "global_general",
        "public",
    }
    if mode not in supported_modes:
        raise OntologyMutationCommandError(
            "invalid_create_scope_mode",
            "The requested concept creation scope mode is unsupported.",
        )
    if mode in {"global", "global_general", "public"}:
        return {
            "effective_scope_mode": "global_general",
            "specific_to_user_concept_ids": [],
            "specific_to_organisation_concept_ids": [],
        }
    if mode in {"organisation_general", "organization_general", "org_general"}:
        if organisation_id is None:
            raise OntologyMutationCommandError(
                "missing_organisation_context",
                "organisation_general scope requires trusted organisation context.",
            )
        return {
            "effective_scope_mode": "organisation_general",
            "specific_to_user_concept_ids": [],
            "specific_to_organisation_concept_ids": [organisation_id],
        }
    if actor_id is None:
        raise OntologyMutationCommandError(
            "authenticated_actor_context_required",
            "Trusted actor context is required to derive concept creation scope.",
        )
    user_only = mode in {"private", "user_only", "user_only_default"}
    return {
        "effective_scope_mode": (
            "user_only_default"
            if user_only or organisation_id is None
            else "user_org_default"
        ),
        "specific_to_user_concept_ids": [actor_id],
        "specific_to_organisation_concept_ids": (
            [] if user_only or organisation_id is None else [organisation_id]
        ),
    }


def _create_reference_concept_ids(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every pre-existing concept referenced by a create effect."""

    references: list[str] = []
    for value in (
        arguments.get("parent_id"),
        *(arguments.get("parent_concept_ids") or []),
        arguments.get("instance_of_type"),
    ):
        concept_id = _normalise_concept_id(value)
        if concept_id:
            references.append(concept_id)
    for item in arguments.get("linked_concepts") or []:
        if isinstance(item, str):
            concept_id = _normalise_concept_id(item)
        elif isinstance(item, Mapping):
            concept_id = _normalise_concept_id(
                item.get("concept_id")
                or item.get("target_concept_id")
                or item.get("target_id")
            )
        else:
            concept_id = None
        if concept_id:
            references.append(concept_id)
    for item in arguments.get("concepts") or []:
        if not isinstance(item, Mapping):
            continue
        instance_type = _normalise_concept_id(item.get("instance_of_type"))
        if instance_type:
            references.append(instance_type)
    # Predicate creation deterministically types the child under this canonical
    # parent even though the caller supplies a general semantic parent.
    if any(
        isinstance(item, Mapping)
        and _clean_text(item.get("kind")).lower() == "predicate"
        for item in arguments.get("concepts") or []
    ):
        from ..vontology.code_concepts_registry import PREDICATE_TYPE_ID

        references.append(PREDICATE_TYPE_ID)
    return tuple(dict.fromkeys(references))


def _relationship_arguments_with_resolved_selector(
    method_name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve opaque relation IDs without treating the token as authority."""

    resolved = dict(arguments)
    if method_name not in {"remove_relationship", "remove_relationships_bulk"}:
        return resolved
    from .relationship_removal_service import parse_relationship_relation_id

    def exact_selector(row: Mapping[str, Any]) -> dict[str, Any]:
        canonical = dict(row)
        relation_id = canonical.get("relation_id")
        if not _clean_text(relation_id):
            return canonical
        parsed = parse_relationship_relation_id(relation_id)
        if not parsed:
            raise OntologyMutationCommandError(
                "invalid_relationship_relation_id",
                "The relationship relation ID is invalid.",
            )
        for key in ("source_id", "predicate", "target"):
            supplied = _clean_text(canonical.get(key))
            expected = _clean_text(parsed.get(key))
            if supplied and supplied != expected:
                raise OntologyMutationCommandError(
                    "relationship_selector_conflict",
                    (
                        "The supplied relationship selector does not match its "
                        "canonical relation ID."
                    ),
                )
        canonical.update(parsed)
        return canonical

    if method_name == "remove_relationship":
        canonical = exact_selector(resolved)
        predicate = _clean_text(canonical.get("predicate"))
        if predicate:
            canonical["predicate"] = normalise_structural_predicate(predicate)
        return canonical

    if resolved.get("filter") is not None:
        raise OntologyMutationCommandError(
            "bulk_relationship_filter_requires_exact_preview",
            (
                "Governed bulk removal requires exact relation IDs or triples; "
                "a broad filter must first be resolved through a read-only preview."
            ),
        )

    rows: list[Any] = []
    for raw_row in resolved.get("relationships") or resolved.get("relations") or []:
        if not isinstance(raw_row, Mapping):
            raise OntologyMutationCommandError(
                "invalid_bulk_relationship_selector",
                "Every bulk relationship selector must be an object.",
            )
        row = exact_selector(raw_row)
        if not all(
            _clean_text(row.get(key)) for key in ("source_id", "predicate", "target")
        ):
            raise OntologyMutationCommandError(
                "invalid_bulk_relationship_selector",
                (
                    "Every bulk relationship selector requires an exact source, "
                    "predicate, and target before any write starts."
                ),
            )
        predicate = _clean_text(row.get("predicate"))
        if predicate:
            row["predicate"] = normalise_structural_predicate(predicate)
        rows.append(row)
    for relation_id in resolved.get("relation_ids") or []:
        parsed = parse_relationship_relation_id(relation_id)
        if not parsed:
            raise OntologyMutationCommandError(
                "invalid_relationship_relation_id",
                "A bulk relationship relation ID is invalid.",
            )
        parsed["predicate"] = normalise_structural_predicate(parsed["predicate"])
        rows.append(parsed)
    for row in rows:
        if _clean_text(row.get("predicate")) in RESERVED_GENERIC_MUTATION_PREDICATES:
            raise OntologyMutationCommandError(
                "dedicated_ontology_governance_operation_required",
                (
                    "Authority, membership, and visibility relations may only be "
                    "changed through their dedicated operation."
                ),
            )
    if rows:
        resolved["relationships"] = rows
        # The catalogue handler consumes ``relations``. Use the same immutable
        # canonical selector set for the authority decision and actual write.
        resolved["relations"] = rows
        resolved["relation_ids"] = []
        resolved["filter"] = None
    return resolved


def resolve_governed_ontology_arguments(
    method_name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve canonical selectors before both authorisation and mutation."""

    resolved = _relationship_arguments_with_resolved_selector(
        _clean_text(method_name),
        normalise_governed_ontology_arguments(method_name, arguments),
    )
    if _clean_text(method_name) == "merge_concepts":
        from .concept_merge_service import (
            ConceptMergePlanError,
            build_concept_merge_plan,
        )

        try:
            # A plan is always rebuilt from canonical state. A client- or
            # model-supplied plan can neither select affected records nor
            # become the mutation precondition.
            resolved["exact_plan"] = build_concept_merge_plan(
                _normalise_concept_id(resolved.get("source_id")) or "",
                _normalise_concept_id(resolved.get("target_id")) or "",
            )
        except ConceptMergePlanError as exc:
            raise OntologyMutationCommandError(
                exc.reason_code,
                exc.public_message,
            ) from exc
    if _clean_text(method_name) == "create_concepts":
        from .create_concepts_parent_resolution_service import (
            resolve_parent_for_create_concepts,
        )

        def resolve_parent(value: Any) -> str | None:
            parent_id = _normalise_concept_id(value)
            if parent_id is None:
                return None
            resolution = resolve_parent_for_create_concepts(parent_id)
            if not resolution.success or not resolution.resolved_parent_id:
                raise OntologyMutationCommandError(
                    "ontology_mutation_target_not_found",
                    "The requested concept parent is unavailable.",
                )
            return resolution.resolved_parent_id

        raw_scope_mode = resolved.get("scope_mode")
        raw_visibility_scope_mode = resolved.get("visibility_scope_mode")
        supplied_scope_modes = {
            _clean_text(value).lower().replace("-", "_")
            for value in (raw_scope_mode, raw_visibility_scope_mode)
            if _clean_text(value)
        }
        if len(supplied_scope_modes) > 1:
            raise OntologyMutationCommandError(
                "conflicting_create_scope_mode",
                "A governed create requires one consistent exact scope mode.",
            )
        scope_mode = next(iter(supplied_scope_modes), "user_only_default")
        if scope_mode in {"default", "user_org_default", "user_org"}:
            if supplied_scope_modes:
                raise OntologyMutationCommandError(
                    "ambiguous_create_scope_mode",
                    (
                        "Dual user-and-organisation visibility is not a canonical "
                        "publication context; use user_only_default or "
                        "organisation_general."
                    ),
                )
            scope_mode = "user_only_default"
        scope_aliases = {
            "private": "user_only_default",
            "user_only": "user_only_default",
            "user_only_default": "user_only_default",
            "organisation_general": "organisation_general",
            "organization_general": "organisation_general",
            "org_general": "organisation_general",
            "global": "global_general",
            "global_general": "global_general",
            "public": "global_general",
        }
        canonical_scope_mode = scope_aliases.get(scope_mode)
        if canonical_scope_mode is None:
            raise OntologyMutationCommandError(
                "invalid_create_scope_mode",
                "The requested concept creation scope mode is unsupported.",
            )
        resolved["scope_mode"] = canonical_scope_mode
        resolved.pop("visibility_scope_mode", None)

        raw_concepts = resolved.get("concepts")
        if not isinstance(raw_concepts, list):
            raise OntologyMutationCommandError(
                "invalid_create_concepts",
                "concepts must be a list of exact create specifications.",
            )
        from ..utils.concept_id_utils import canonicalise_vontology_concept_id

        canonical_concepts: list[Any] = []
        for raw_item in raw_concepts:
            if not isinstance(raw_item, Mapping):
                canonical_concepts.append(raw_item)
                continue
            item = dict(raw_item)
            supplied_id = _clean_text(item.get("concept_id") or item.get("id"))
            if supplied_id:
                canonical_id = canonicalise_vontology_concept_id(supplied_id)
                if not canonical_id:
                    raise OntologyMutationCommandError(
                        "invalid_create_concept_id",
                        "A supplied concept ID is invalid after canonicalisation.",
                    )
                item["concept_id"] = canonical_id
                item.pop("id", None)
            else:
                canonical_id = canonicalise_vontology_concept_id(item.get("name"))
                if not canonical_id:
                    raise OntologyMutationCommandError(
                        "invalid_create_concept_id",
                        "A concept name must resolve to one exact canonical ID.",
                    )
                item["concept_id"] = canonical_id
            if not _clean_text(item.get("kind")):
                requested_instance = item.get("create_as_instance")
                if requested_instance is None:
                    requested_instance = resolved.get("create_as_instance")
                item["kind"] = "instance" if requested_instance is True else "type"
            canonical_concepts.append(item)

        raw_top_level_instance_type = resolved.get("instance_of_type")
        top_level_instance_type = (
            canonicalise_vontology_concept_id(raw_top_level_instance_type)
            if _clean_text(raw_top_level_instance_type)
            else None
        )
        if _clean_text(raw_top_level_instance_type) and not top_level_instance_type:
            raise OntologyMutationCommandError(
                "invalid_create_instance_of_type",
                "A governed create requires one exact instance_of_type concept ID.",
                recovery_affordances=(),
            )
        consolidated_concepts: list[Any] = []
        for item in canonical_concepts:
            if not isinstance(item, Mapping):
                consolidated_concepts.append(item)
                continue
            concept_item = dict(item)
            raw_item_instance_type = concept_item.get("instance_of_type")
            item_instance_type = (
                canonicalise_vontology_concept_id(raw_item_instance_type)
                if _clean_text(raw_item_instance_type)
                else None
            )
            if _clean_text(raw_item_instance_type) and not item_instance_type:
                raise OntologyMutationCommandError(
                    "invalid_create_instance_of_type",
                    (
                        "A governed create requires one exact instance_of_type "
                        "concept ID."
                    ),
                    recovery_affordances=(),
                )
            if (
                top_level_instance_type
                and item_instance_type
                and top_level_instance_type != item_instance_type
            ):
                raise OntologyMutationCommandError(
                    "conflicting_create_instance_of_type",
                    (
                        "Top-level and per-concept instance_of_type must identify "
                        "the same exact concept."
                    ),
                    recovery_affordances=(),
                )
            canonical_instance_type = item_instance_type or top_level_instance_type
            if canonical_instance_type:
                concept_item["instance_of_type"] = canonical_instance_type
            consolidated_concepts.append(concept_item)
        canonical_concepts = consolidated_concepts
        resolved["concepts"] = canonical_concepts
        resolved.pop("instance_of_type", None)

        parent_id = resolve_parent(resolved.get("parent_id"))
        if parent_id:
            resolved["parent_id"] = parent_id
            resolved["authority_resolved_parent_id"] = parent_id
        raw_parent_ids = resolved.get("parent_concept_ids")
        if raw_parent_ids is not None:
            if not isinstance(raw_parent_ids, list):
                raise OntologyMutationCommandError(
                    "invalid_create_parent_concept_ids",
                    "parent_concept_ids must be a list of exact concept IDs.",
                )
            resolved["parent_concept_ids"] = list(
                dict.fromkeys(
                    parent
                    for parent in (resolve_parent(item) for item in raw_parent_ids)
                    if parent
                )
            )
            resolved_parent_ids = resolved["parent_concept_ids"]
            if parent_id and resolved_parent_ids and resolved_parent_ids != [parent_id]:
                raise OntologyMutationCommandError(
                    "conflicting_create_parent",
                    "A governed create requires one consistent exact parent.",
                )
            if not parent_id and resolved_parent_ids:
                resolved["parent_id"] = resolved_parent_ids[0]
                resolved["authority_resolved_parent_id"] = resolved_parent_ids[0]

        collision_mode = _clean_text(resolved.get("collision_resolution_mode"))
        if collision_mode and collision_mode != ACTOR_SCOPED_REFERENT_COLLISION_MODE:
            raise OntologyMutationCommandError(
                "invalid_create_collision_resolution_mode",
                "The requested concept-ID collision resolution mode is unsupported.",
                error_details={
                    "supported_modes": [ACTOR_SCOPED_REFERENT_COLLISION_MODE]
                },
                recovery_affordances=(),
            )
        if collision_mode == ACTOR_SCOPED_REFERENT_COLLISION_MODE:
            if canonical_scope_mode != ACTOR_SCOPED_REFERENT_SCOPE_MODE:
                raise OntologyMutationCommandError(
                    "actor_scoped_referent_requires_user_only_scope",
                    (
                        "Actor-scoped referent recovery is available only for "
                        "user_only_default creation."
                    ),
                    recovery_affordances=(),
                )
            if len(canonical_concepts) != 1 or not isinstance(
                canonical_concepts[0], Mapping
            ):
                raise OntologyMutationCommandError(
                    "actor_scoped_referent_requires_single_instance",
                    (
                        "Actor-scoped referent recovery requires exactly one "
                        "instance concept."
                    ),
                    recovery_affordances=(),
                )
            referent_spec = dict(canonical_concepts[0])
            referent_kind = _clean_text(referent_spec.get("kind")).lower()
            if referent_kind == "individual":
                referent_kind = "instance"
            if referent_kind != "instance":
                raise OntologyMutationCommandError(
                    "actor_scoped_referent_requires_instance",
                    (
                        "Actor-scoped referent recovery is available only for "
                        "instance concepts."
                    ),
                    recovery_affordances=(),
                )
            requested_concept_id = canonicalise_vontology_concept_id(
                resolved.get("requested_concept_id")
            )
            if not requested_concept_id:
                raise OntologyMutationCommandError(
                    "actor_scoped_referent_requested_id_required",
                    (
                        "Actor-scoped referent recovery requires the requested "
                        "concept ID used as its deterministic identity seed."
                    ),
                    recovery_affordances=(),
                )
            # This is a caller-supplied deterministic identity seed, not an
            # authority-bearing conflict receipt. Direct recovery mode must not
            # inspect, confirm, or disclose whether the requested ID is occupied.
            actor_concept_id = get_effective_user_concept_id()
            if not actor_concept_id:
                raise OntologyMutationCommandError(
                    "authenticated_actor_context_required",
                    "Trusted actor context is required for referent recovery.",
                    recovery_affordances=(),
                )
            effective_concept_id = actor_scoped_referent_concept_id(
                requested_concept_id=requested_concept_id,
                actor_concept_id=actor_concept_id,
            )
            supplied_referent_id = _normalise_concept_id(
                referent_spec.get("concept_id")
            )
            if supplied_referent_id != effective_concept_id:
                raise OntologyMutationCommandError(
                    "actor_scoped_referent_identity_mismatch",
                    (
                        "The supplied recovery concept ID does not match the "
                        "server-derived actor-scoped referent identity."
                    ),
                    recovery_affordances=(),
                )
            referent_spec["concept_id"] = effective_concept_id
            referent_spec["kind"] = "instance"
            resolved["concepts"] = [referent_spec]
            resolved["requested_concept_id"] = requested_concept_id
            resolved["collision_resolution_mode"] = collision_mode
    if _clean_text(method_name) == "add_relationship":
        predicate_ref = resolved.get("predicate_ref")
        if predicate_ref is not None:
            if not isinstance(predicate_ref, Mapping):
                raise OntologyMutationCommandError(
                    "invalid_predicate_reference",
                    "predicate_ref must be an object.",
                )
            predicate_concept_id = _clean_text(predicate_ref.get("concept_id"))
            if not predicate_concept_id or _clean_text(predicate_ref.get("name")):
                raise OntologyMutationCommandError(
                    "exact_predicate_concept_id_required",
                    "A governed write requires one exact predicate concept ID.",
                )
            resolved["predicate"] = predicate_concept_id
            resolved.pop("predicate_ref", None)
        if resolved.get("predicate_if_missing") is not None:
            raise OntologyMutationCommandError(
                "predicate_dependency_requires_separate_governed_creation",
                (
                    "Create a missing predicate through its own exact governed "
                    "effect before adding the relationship."
                ),
            )
        predicate = _clean_text(resolved.get("predicate"))
        if not predicate:
            raise OntologyMutationCommandError(
                "exact_predicate_concept_id_required",
                "A governed write requires one exact predicate.",
            )
        canonical = normalise_structural_predicate(predicate)
        core_text = canonical[3:] if canonical.startswith("#V#") else canonical
        if not (
            is_structural_predicate(canonical)
            or core_text in CORE_RELATIONSHIP_TEXT_PREDICATES
            or canonical.startswith("#V#")
        ):
            raise OntologyMutationCommandError(
                "exact_predicate_concept_id_required",
                (
                    "Natural-language predicate resolution is read-only; retry "
                    "with the exact predicate concept ID."
                ),
            )
        resolved["predicate"] = canonical
    if _clean_text(method_name) in {
        "upsert_text_relation",
        "upsert_singleton_text_relation",
    } or (
        _clean_text(method_name) == "delete_text_relation"
        and not _clean_text(resolved.get("relation_id"))
    ):
        try:
            predicate_resolution = resolve_text_relation_predicate_for_write(
                resolved.get("predicate")
            )
        except TextRelationPredicateResolutionError as exc:
            raise OntologyMutationCommandError(exc.error_code, str(exc)) from exc
        resolved["predicate"] = predicate_resolution.storage_predicate
    return resolved


def build_ontology_mutation_intent(
    *,
    method_name: str,
    arguments: Mapping[str, Any],
    idempotency_key: str | None = None,
) -> OntologyMutationIntent:
    """Derive target and publication context; never trust payload actor scope."""

    method = _clean_text(method_name)
    arguments = resolve_governed_ontology_arguments(method, arguments)
    operation = _METHOD_OPERATION.get(method)
    if not operation:
        raise OntologyMutationCommandError(
            "ontology_mutation_method_not_governed",
            "This method is not a governed canonical ontology mutation.",
        )

    if method in {
        "preview_concept_publication_scope_change",
        "change_concept_publication_scope",
    }:
        from .ontology_scope_change_service import (
            OntologyScopeChangePreconditionError,
            build_concept_publication_scope_change_intent,
        )

        try:
            return build_concept_publication_scope_change_intent(
                concept_id=_clean_text(arguments.get("concept_id")),
                destination_kind=_clean_text(arguments.get("destination_kind")),
                destination_concept_id=(
                    _clean_text(arguments.get("destination_concept_id")) or None
                ),
                expected_scope_fingerprint=_clean_text(
                    arguments.get("expected_scope_fingerprint")
                ),
                request_id=(
                    _clean_text(arguments.get("request_id"))
                    or _clean_text(idempotency_key)
                ),
                preview=method == "preview_concept_publication_scope_change",
                reason=_clean_text(arguments.get("reason")) or None,
                invocation_tool_name=method,
                idempotency_key=idempotency_key,
            )
        except OntologyScopeChangePreconditionError as exc:
            raise OntologyMutationCommandError(
                exc.reason_code,
                exc.public_message,
            ) from exc

    if method == "add_relationship":
        predicate_ref = arguments.get("predicate_ref")
        creates_predicate_dependency = arguments.get("predicate_if_missing") is not None
        if isinstance(predicate_ref, Mapping):
            creates_predicate_dependency = creates_predicate_dependency or (
                _clean_text(predicate_ref.get("on_missing")).lower()
                == "create_typed_predicate"
            )
        if creates_predicate_dependency:
            raise OntologyMutationCommandError(
                "predicate_dependency_requires_separate_governed_creation",
                (
                    "A canonical predicate dependency must be created through its "
                    "own exact governed effect before adding the relationship."
                ),
            )
        if isinstance(predicate_ref, Mapping):
            predicate_concept_id = _normalise_predicate(predicate_ref.get("concept_id"))
            if not predicate_concept_id:
                raise OntologyMutationCommandError(
                    "exact_predicate_concept_id_required",
                    (
                        "A governed canonical relationship write requires an "
                        "exact predicate concept ID."
                    ),
                )
            arguments["predicate"] = predicate_concept_id
            arguments.pop("predicate_ref", None)

    if method == "update_concept":
        update_data = arguments.get("update_data")
        if isinstance(update_data, Mapping) and any(
            str(key) == "relationships" or str(key).startswith("relationships.")
            for key in update_data
        ):
            raise OntologyMutationCommandError(
                "relationship_mutation_requires_dedicated_command",
                (
                    "Canonical relationships, including visibility, must be "
                    "changed through their dedicated governed operation."
                ),
            )
    if method == "add_names_to_concept":
        raise OntologyMutationCommandError(
            "batch_names_require_individual_effects",
            (
                "Batch name publication is unavailable at the ontology authority "
                "boundary; publish each exact name as its own governed effect."
            ),
        )

    actor_id = get_effective_user_concept_id()
    organisation_id = get_effective_organisation_concept_id()
    predicate = _normalise_predicate(arguments.get("predicate"))
    targets: tuple[str, ...]
    source_contexts: tuple[PublicationContext, ...] = ()
    delta: dict[str, Any] = {}

    if method == "create_concepts":
        concept_specs = arguments.get("concepts") or []
        if len(concept_specs) != 1 or not isinstance(concept_specs[0], Mapping):
            raise OntologyMutationCommandError(
                "multi_create_requires_individual_effects",
                (
                    "Governed creation accepts exactly one core concept per effect; "
                    f"received {len(concept_specs)}. Submit each concept as its own "
                    "create_concepts effect so every outcome remains independently "
                    "observable."
                ),
                error_details={
                    "constraint": "exactly_one_core_concept_per_effect",
                    "received_concept_count": len(concept_specs),
                },
            )
        concept_spec = concept_specs[0]
        unsupported_create_fields = {
            "attributes": concept_spec.get("attributes") or arguments.get("attributes"),
            "system_tags": concept_spec.get("system_tags")
            or arguments.get("system_tags"),
            "user_tags": concept_spec.get("user_tags") or arguments.get("user_tags"),
            "linked_concepts": concept_spec.get("linked_concepts")
            or arguments.get("linked_concepts"),
            "external_identifiers": concept_spec.get("external_identifiers")
            or arguments.get("external_identifiers"),
            "identity_candidate_concept_ids": concept_spec.get(
                "identity_candidate_concept_ids"
            )
            or arguments.get("identity_candidate_concept_ids"),
            "identity_rejected_candidate_concept_ids": concept_spec.get(
                "identity_rejected_candidate_concept_ids"
            )
            or arguments.get("identity_rejected_candidate_concept_ids"),
        }
        unsupported_present = sorted(
            key for key, value in unsupported_create_fields.items() if value
        )
        if bool(arguments.get("allow_duplicate_instances")):
            unsupported_present.append("allow_duplicate_instances")
        parent_concept_ids = list(arguments.get("parent_concept_ids") or [])
        if len(parent_concept_ids) > 1:
            unsupported_present.append("parent_concept_ids")
        unsupported_present = sorted(set(unsupported_present))
        if unsupported_present:
            raise OntologyMutationCommandError(
                "complex_create_requires_typed_effects",
                (
                    "This governed create path currently supports one exact core "
                    "concept only. Rejected fields: "
                    f"{', '.join(unsupported_present)}. No mutation was started, "
                    "and no automatic recovery is available for this request shape."
                ),
                error_details={
                    "rejected_fields": unsupported_present,
                    "supported_core_concept_fields": [
                        "concept_id",
                        "name",
                        "kind",
                        "description",
                        "notes",
                        "vontology_path",
                        "instance_of_type",
                    ],
                },
            )
        scope_mode = arguments.get("scope_mode") or arguments.get(
            "visibility_scope_mode"
        )
        try:
            publication_context = publication_context_for_creation(
                scope_mode=scope_mode,
                actor_concept_id=actor_id,
                organisation_concept_id=organisation_id,
            )
        except ValueError as exc:
            raise OntologyMutationCommandError(
                "authenticated_actor_context_required",
                "Trusted actor context is required to derive concept creation scope.",
            ) from exc
        scope_projection = _creation_scope_projection(
            scope_mode=scope_mode,
            actor_concept_id=actor_id,
            organisation_concept_id=organisation_id,
        )
        expected_concept_id = _normalise_concept_id(concept_spec.get("concept_id"))
        if not expected_concept_id:
            raise OntologyMutationCommandError(
                "invalid_create_concept_id",
                "A governed create requires one exact canonical concept ID.",
            )
        parent_id = _normalise_concept_id(arguments.get("parent_id"))
        if _clean_text(concept_spec.get("kind")).lower() == "predicate":
            from ..vontology.code_concepts_registry import PREDICATE_TYPE_ID

            parent_id = PREDICATE_TYPE_ID
        instance_of_type = _normalise_concept_id(
            arguments.get("instance_of_type") or concept_spec.get("instance_of_type")
        )
        actor_scoped_referent = (
            _clean_text(arguments.get("collision_resolution_mode"))
            == ACTOR_SCOPED_REFERENT_COLLISION_MODE
        )
        visible_exact_reuse = False
        if _concept_exists_unfiltered(expected_concept_id):
            required_context = (
                publication_context.to_mapping() if actor_scoped_referent else None
            )
            visible_exact_reuse = can_access_concept(
                expected_concept_id
            ) and _visible_concept_satisfies_requested_core(
                concept_id=expected_concept_id,
                concept_spec=concept_spec,
                parent_id=parent_id,
                instance_of_type=instance_of_type,
                required_publication_context=required_context,
            )
            if not visible_exact_reuse:
                # Visible incompatible and inaccessible IDs retain the same
                # non-disclosing error. Only an actor-invisible instance
                # collision can advertise the bounded scoped-referent action.
                raise _create_id_conflict_error(
                    arguments=arguments,
                    expected_concept_id=expected_concept_id,
                )
        reference_ids = _create_reference_concept_ids(arguments)
        _visible_or_fail(reference_ids)
        targets = tuple(
            dict.fromkeys((*_concepts_from_create_arguments(arguments), *reference_ids))
        )
        delta = {
            "concept_count": len(arguments.get("concepts") or []),
            "concepts": _create_intent_concepts(arguments),
            "expected_concept_id": expected_concept_id,
            "parent_id": parent_id,
            "parent_concept_ids": list(arguments.get("parent_concept_ids") or []),
            "instance_of_type": instance_of_type,
            "referenced_concept_ids": list(reference_ids),
            "scope_mode": scope_projection["effective_scope_mode"],
            "stored_scope": scope_projection,
            "visible_exact_reuse": visible_exact_reuse,
            "actor_scoped_referent": actor_scoped_referent,
            # Parent inverses are deliberately not canonical side effects of
            # governed creation. The child's exact forward typing assertion is
            # authoritative; reverse traversal belongs to the derived extent.
            "maintain_parent_inverse": False,
        }
    elif method == "delete_legacy_name":
        subject_id = _normalise_concept_id(arguments.get("concept_id"))
        if not subject_id:
            raise OntologyMutationCommandError(
                "ontology_mutation_subject_required",
                "A canonical ontology mutation subject is required.",
            )
        _visible_or_fail((subject_id,))
        legacy_snapshot = _validated_legacy_name_precondition(
            concept_id=subject_id,
            legacy_name_selector=arguments.get("legacy_name_selector"),
        )
        canonical_names = _canonical_has_name_snapshot(subject_id)
        if canonical_names["canonical_has_name_present"] is not True:
            raise OntologyMutationCommandError(
                "canonical_has_name_required_for_legacy_cleanup",
                (
                    "A legacy inline name can be removed only after a canonical "
                    "hasName relation exists."
                ),
            )
        publication_context = concept_publication_context(subject_id)
        targets = (subject_id,)
        predicate = "hasName"
        delta = {
            "legacy_name_ordinal": legacy_snapshot["ordinal"],
            "legacy_name_entry_sha256": legacy_snapshot["entry_sha256"],
            "legacy_names_count": legacy_snapshot["names_count"],
            "legacy_names_snapshot_sha256": legacy_snapshot["names_snapshot_sha256"],
            "resulting_legacy_names_count": legacy_snapshot["names_count"] - 1,
            "resulting_legacy_names_snapshot_sha256": legacy_snapshot[
                "resulting_names_snapshot_sha256"
            ],
            "canonical_has_name_count": canonical_names["canonical_has_name_count"],
            "canonical_has_name_relation_ids": canonical_names[
                "canonical_has_name_relation_ids"
            ],
            "canonical_has_name_snapshot_sha256": canonical_names[
                "canonical_has_name_snapshot_sha256"
            ],
        }
    elif method in {
        "upsert_text_relation",
        "update_text_relation",
        "upsert_singleton_text_relation",
        "add_names_to_concept",
        "delete_text_relation",
        "update_concept",
    }:
        subject_id = _normalise_concept_id(
            arguments.get("concept_id") or arguments.get("subject_concept_id")
        )
        if not subject_id:
            raise OntologyMutationCommandError(
                "ontology_mutation_subject_required",
                "A canonical ontology mutation subject is required.",
            )
        if method == "update_concept":
            raise OntologyMutationCommandError(
                "typed_concept_update_required",
                (
                    "Generic concept updates are unavailable at the ontology "
                    "authority boundary; use a dedicated typed text or relation "
                    "command with exact read-back."
                ),
            )
        _visible_or_fail((subject_id,))
        relation_snapshot: dict[str, Any] | None = None
        relation_id = _clean_text(arguments.get("relation_id"))
        if method in {"update_text_relation", "delete_text_relation"} and relation_id:
            relation_snapshot = _text_relation_snapshot(
                subject_concept_id=subject_id,
                relation_id=relation_id,
            )
            predicate = _normalise_predicate(relation_snapshot.get("predicate"))
            # The canonical row, not a caller-supplied predicate, decides
            # whether this is a generic text edit or a dedicated authority
            # lifecycle operation.
            arguments["predicate"] = predicate
            if predicate in RESERVED_GENERIC_MUTATION_PREDICATES:
                raise OntologyMutationCommandError(
                    "dedicated_ontology_governance_operation_required",
                    (
                        "Authority, membership, and visibility relations may "
                        "only be changed through their dedicated operation."
                    ),
                )
        publication_context = concept_publication_context(subject_id)
        targets = (subject_id,)
        delta = {
            "relation_id": relation_id or None,
            "relation_revision": (
                relation_snapshot.get("revision") if relation_snapshot else None
            ),
            "stored_predicate": (
                relation_snapshot.get("predicate") if relation_snapshot else predicate
            ),
            "current_text_sha256": (
                relation_snapshot.get("current_text_sha256")
                if relation_snapshot
                else None
            ),
            "text_sha256": _text_sha256(
                arguments.get("new_text")
                if method == "update_text_relation"
                else arguments.get("text")
            ),
            "language": (
                _clean_text(arguments.get("language") or arguments.get("lang"))
                or "en-NZ"
            ),
            "context_sha256": (
                _canonical_json_sha256(arguments.get("context") or {})
                if method in {"upsert_text_relation", "upsert_singleton_text_relation"}
                else None
            ),
            "provenance_sha256": (
                _canonical_json_sha256(arguments.get("provenance") or {})
                if method
                in {
                    "upsert_text_relation",
                    "update_text_relation",
                    "upsert_singleton_text_relation",
                }
                else None
            ),
            "update_data": (
                _json_safe(arguments.get("update_data"))
                if method == "update_concept"
                else None
            ),
        }
    elif method in {
        "add_relationship",
        "remove_relationship",
        "promote_uncertain_relationship_assertion",
    }:
        source_id = _normalise_concept_id(arguments.get("source_id"))
        if not source_id:
            raise OntologyMutationCommandError(
                "ontology_mutation_subject_required",
                "A canonical relationship source is required.",
            )
        assertion_snapshot: dict[str, Any] | None = None
        if method == "promote_uncertain_relationship_assertion":
            assertion_id = _clean_text(arguments.get("assertion_id"))
            if not assertion_id:
                raise OntologyMutationCommandError(
                    "uncertain_relationship_assertion_required",
                    "An exact uncertain relationship assertion is required.",
                )
            assertion_snapshot = _uncertain_assertion_snapshot(
                source_id=source_id,
                assertion_id=assertion_id,
            )
            predicate = _normalise_predicate(assertion_snapshot.get("predicate"))
            arguments["predicate"] = predicate
            arguments["target"] = assertion_snapshot.get("target")
        target_id = (
            _normalise_concept_id(arguments.get("target"))
            if _target_is_concept(arguments.get("target"))
            else None
        )
        visible_targets = (source_id, target_id) if target_id else (source_id,)
        _visible_or_fail(tuple(item for item in visible_targets if item))
        publication_context = concept_publication_context(source_id)
        normalised_structural_predicate = (
            normalise_structural_predicate(predicate or "") if predicate else ""
        )
        inverse_predicate = get_structural_inverse_map().get(
            normalised_structural_predicate
        )
        if target_id and inverse_predicate:
            source_contexts = (concept_publication_context(target_id),)
        targets = tuple(item for item in visible_targets if item)
        delta = {
            "target_concept_id": target_id,
            "target_text_sha256": (
                None if target_id else _text_sha256(arguments.get("target"))
            ),
            "assertion_id": _clean_text(arguments.get("assertion_id")) or None,
            "relation_id": _clean_text(arguments.get("relation_id")) or None,
            "assertion_revision": (
                assertion_snapshot.get("revision") if assertion_snapshot else None
            ),
            "maintain_inverse": bool(target_id and inverse_predicate),
            "inverse_predicate": inverse_predicate,
        }
    elif method == "remove_relationships_bulk":
        bulk_rows = arguments.get("relationships") or arguments.get("relations") or []
        source_ids = tuple(
            dict.fromkeys(
                item
                for item in (
                    _normalise_concept_id(row.get("source_id"))
                    for row in bulk_rows
                    if isinstance(row, Mapping)
                )
                if item
            )
        )
        if not source_ids:
            raise OntologyMutationCommandError(
                "ontology_mutation_subject_required",
                "Bulk canonical relationship removal requires explicit sources.",
            )
        target_ids = tuple(
            dict.fromkeys(
                item
                for item in (
                    _normalise_concept_id(row.get("target"))
                    for row in bulk_rows
                    if isinstance(row, Mapping)
                    and _target_is_concept(row.get("target"))
                    and get_structural_inverse_map().get(
                        normalise_structural_predicate(
                            _normalise_predicate(row.get("predicate")) or ""
                        )
                    )
                )
                if item
            )
        )
        all_affected_ids = tuple(dict.fromkeys((*source_ids, *target_ids)))
        _visible_or_fail(all_affected_ids)
        contexts = tuple(
            dict.fromkeys(
                concept_publication_context(item) for item in all_affected_ids
            )
        )
        publication_context = contexts[-1]
        source_contexts = contexts[:-1]
        targets = all_affected_ids
        delta = {
            "relationship_count": len(bulk_rows),
            "selectors_sha256": _effect_arguments_sha256({"relationships": bulk_rows}),
            "selector_projection_sha256": _effect_arguments_sha256(
                {
                    "relationships": [
                        {
                            "source_id": row.get("source_id"),
                            "predicate": row.get("predicate"),
                            "target": row.get("target"),
                        }
                        for row in bulk_rows
                        if isinstance(row, Mapping)
                    ]
                }
            ),
            "inverse_target_concept_ids": list(target_ids),
        }
    elif method in {"delete_concept", "rename_concept"}:
        source_id = _normalise_concept_id(
            arguments.get("concept_id") or arguments.get("old_id")
        )
        if not source_id:
            raise OntologyMutationCommandError(
                "ontology_mutation_subject_required",
                "A concept identity is required.",
            )
        if not bool(arguments.get("simulate", True)):
            raise OntologyMutationCommandError(
                "graph_rewrite_exact_plan_required",
                (
                    "Executing this graph-wide rewrite is unavailable until its "
                    "complete affected-scope plan can be authorised and verified."
                ),
            )
        _visible_or_fail((source_id,))
        publication_context = concept_publication_context(source_id)
        targets = (source_id,)
        delta = {
            "new_id": _normalise_concept_id(arguments.get("new_id")),
            "simulate": bool(arguments.get("simulate", True)),
        }
    elif method == "merge_concepts":
        source_id = _normalise_concept_id(arguments.get("source_id"))
        target_id = _normalise_concept_id(arguments.get("target_id"))
        if not source_id or not target_id:
            raise OntologyMutationCommandError(
                "ontology_mutation_subject_required",
                "Concept merge requires exact source and target identities.",
            )
        exact_plan = arguments.get("exact_plan")
        if not isinstance(exact_plan, Mapping):
            raise OntologyMutationCommandError(
                "identity_consolidation_plan_required",
                "A server-built identity-consolidation plan is required.",
            )
        affected_ids = tuple(
            dict.fromkeys(
                concept_id
                for concept_id in (
                    _normalise_concept_id(item)
                    for item in exact_plan.get("affected_concept_ids") or []
                )
                if concept_id
            )
        )
        if (
            not affected_ids
            or source_id not in affected_ids
            or target_id not in affected_ids
        ):
            raise OntologyMutationCommandError(
                "identity_consolidation_plan_invalid",
                "The identity-consolidation affected set is invalid.",
            )
        _visible_or_fail(affected_ids)
        context_by_key: dict[tuple[str, str | None], PublicationContext] = {}
        for concept_id in affected_ids:
            context = concept_publication_context(concept_id)
            context_by_key.setdefault(
                (context.kind.value, context.concept_id),
                context,
            )
        publication_context = concept_publication_context(target_id)
        target_context_key = (
            publication_context.kind.value,
            publication_context.concept_id,
        )
        source_contexts = tuple(
            context
            for key, context in context_by_key.items()
            if key != target_context_key
        )
        targets = affected_ids
        delta = {
            "simulate": bool(arguments.get("simulate", True)),
            "source_id": source_id,
            "target_id": target_id,
            "merge_plan_sha256": _clean_text(exact_plan.get("plan_sha256")),
            "affected_set_sha256": _effect_arguments_sha256(
                {"affected_concept_ids": sorted(affected_ids)}
            ),
            "affected_concept_count": len(affected_ids),
            "affected_text_relation_count": len(
                exact_plan.get("affected_text_relation_ids") or []
            ),
            "merge_text_relation_ids": list(
                exact_plan.get("affected_text_relation_ids") or []
            ),
        }
    else:  # pragma: no cover - mapping above makes this defensive only
        raise OntologyMutationCommandError(
            "ontology_mutation_method_not_governed",
            "Unsupported ontology mutation method.",
        )

    delta["effect_arguments_sha256"] = _effect_arguments_sha256(arguments)
    affected_scope_fingerprints: dict[str, str] = {}
    for target_concept_id in sorted(set(targets)):
        try:
            snapshot = scope_read_back(target_concept_id)
        except LookupError:
            # New concept identifiers do not exist yet. Their exact requested
            # publication context is already bound above.
            continue
        affected_scope_fingerprints[target_concept_id] = _clean_text(
            snapshot.get("scope_fingerprint")
        )
    if affected_scope_fingerprints:
        delta["affected_scope_fingerprints"] = affected_scope_fingerprints
    return OntologyMutationIntent(
        operation=operation,
        publication_context=publication_context,
        target_concept_ids=targets,
        tool_name=method,
        predicate=predicate,
        source_contexts=source_contexts,
        delta=delta,
        idempotency_key=_idempotency_key(arguments, idempotency_key),
    )


def _text_sha256(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    import hashlib

    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _concept_read_back(concept_id: str) -> dict[str, Any]:
    if not can_access_concept(concept_id):
        return {"concept_id": concept_id, "exists": False, "accessible": False}
    concept = ConceptsRepository.find_one({"concept_id": concept_id})
    if not isinstance(concept, Mapping):
        return {"concept_id": concept_id, "exists": False}
    relationships = concept.get("relationships")
    relationship_map = relationships if isinstance(relationships, Mapping) else {}
    scope_edges: dict[str, list[str]] = {}
    for predicate_name in (
        *SPECIFIC_TO_USER_PREDICATES,
        *SPECIFIC_TO_ORG_PREDICATES_READ,
    ):
        raw_values = relationship_map.get(predicate_name)
        if isinstance(raw_values, str):
            values = [raw_values] if raw_values.strip() else []
        elif isinstance(raw_values, Sequence):
            values = [
                str(value).strip()
                for value in raw_values
                if isinstance(value, str) and value.strip()
            ]
        else:
            values = []
        if values:
            scope_edges[predicate_name] = values
    text_relations: list[dict[str, Any]] = []
    for relation in TextRelationsRepository.find({"subject_concept_id": concept_id}):
        if not isinstance(relation, Mapping):
            continue
        text_value = TextValuesRepository.find_one_by_id(
            relation.get("object_text_id")
        )
        if not isinstance(text_value, Mapping):
            continue
        text_relations.append(
            {
                "predicate": _clean_text(relation.get("predicate")),
                "text": text_value.get("text"),
                "language": text_value.get("lang"),
                "context": _json_safe(relation.get("context") or {}),
            }
        )
    text_relations.sort(key=lambda item: json_dumps_sorted(item))
    return {
        "concept_id": concept_id,
        "exists": True,
        "publication_context": concept_publication_context(concept_id).to_mapping(),
        "publication_scope_edges": scope_edges,
        "forward_relationships": {
            key: _json_safe(relationship_map.get(key) or [])
            for key in ("is_a_type_of", "is_an_instance_of", "linked_to")
        },
        "attributes": _json_safe(concept.get("attributes") or {}),
        "system_tags": _json_safe(concept.get("system_tags") or []),
        "user_tags": _json_safe(concept.get("user_tags") or []),
        "vontology_path": concept.get("vontology_path"),
        "text_relations": text_relations,
        "relationship_keys": sorted(str(key) for key in relationship_map),
        "updated_at": str(concept.get("updated_at") or "") or None,
    }


def _visible_concept_satisfies_requested_core(
    *,
    concept_id: str,
    concept_spec: Mapping[str, Any],
    parent_id: str | None,
    instance_of_type: str | None,
    required_publication_context: Mapping[str, Any] | None = None,
) -> bool:
    """Verify actor-visible core compatibility without editing the concept."""

    actual = _concept_read_back(concept_id)
    if actual.get("exists") is not True or actual.get("concept_id") != concept_id:
        return False
    if required_publication_context is not None:
        actual_context = actual.get("publication_context")
        if not isinstance(actual_context, Mapping) or (
            actual_context.get("kind") != required_publication_context.get("kind")
            or actual_context.get("concept_id")
            != required_publication_context.get("concept_id")
        ):
            return False

    relationships = actual.get("forward_relationships")
    if not isinstance(relationships, Mapping):
        return False
    type_parents = set(relationships.get("is_a_type_of") or [])
    instance_types = set(relationships.get("is_an_instance_of") or [])
    kind = _clean_text(concept_spec.get("kind")).lower()
    if kind == "individual":
        kind = "instance"
    if instance_of_type:
        if instance_of_type not in instance_types:
            return False
        if parent_id and parent_id not in type_parents:
            return False
    elif kind in {"instance", "predicate"}:
        if parent_id and parent_id not in instance_types:
            return False
    elif parent_id and parent_id not in type_parents:
        return False

    requested_path = concept_spec.get("vontology_path")
    if requested_path is not None and actual.get("vontology_path") != requested_path:
        return False
    texts = actual.get("text_relations")
    if not isinstance(texts, list):
        return False

    def has_requested_text(
        predicate: str,
        value: Any,
        *,
        name_type: str | None = None,
    ) -> bool:
        if not isinstance(value, str) or not value.strip():
            return True
        return any(
            isinstance(row, Mapping)
            and row.get("predicate") == predicate
            and row.get("text") == value.strip()
            and (
                name_type is None
                or (
                    isinstance(row.get("context"), Mapping)
                    and row["context"].get("name_type") == name_type
                )
            )
            for row in texts
        )

    return (
        has_requested_text("hasName", concept_spec.get("name"), name_type="NL")
        and has_requested_text("hasDescription", concept_spec.get("description"))
        and has_requested_text("hasNote", concept_spec.get("notes"))
    )


def _actor_scoped_referent_result_metadata(concept_id: str) -> dict[str, Any]:
    return {
        "schema_version": ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT,
        "effective_concept_id": concept_id,
        "scope_mode": ACTOR_SCOPED_REFERENT_SCOPE_MODE,
        "identity_status": "unreconciled_actor_scoped_referent",
        "equivalence_asserted": False,
        "alias_created": False,
    }


def _create_id_conflict_error(
    *,
    arguments: Mapping[str, Any],
    expected_concept_id: str,
) -> OntologyMutationCommandError:
    """Return a non-disclosing conflict with at most one executable recovery."""

    affordances: list[dict[str, Any]] = []
    collision_mode = _clean_text(arguments.get("collision_resolution_mode"))
    concept_specs = arguments.get("concepts") or []
    concept_spec = (
        concept_specs[0]
        if len(concept_specs) == 1 and isinstance(concept_specs[0], Mapping)
        else None
    )
    actor_id = get_effective_user_concept_id()
    scope_mode = _clean_text(arguments.get("scope_mode"))
    kind = _clean_text((concept_spec or {}).get("kind")).lower()
    if kind == "individual":
        kind = "instance"
    references = _create_reference_concept_ids(arguments)
    parent_id = _normalise_concept_id(arguments.get("parent_id"))
    recovery_is_eligible = (
        not collision_mode
        and concept_spec is not None
        and kind == "instance"
        and scope_mode == ACTOR_SCOPED_REFERENT_SCOPE_MODE
        and bool(actor_id)
        and bool(parent_id)
        and bool(_clean_text(concept_spec.get("name")))
        and not can_access_concept(expected_concept_id)
        and all(can_access_concept(reference_id) for reference_id in references)
    )
    if recovery_is_eligible and actor_id:
        effective_concept_id = actor_scoped_referent_concept_id(
            requested_concept_id=expected_concept_id,
            actor_concept_id=actor_id,
        )
        required_context = publication_context_for_creation(
            scope_mode=ACTOR_SCOPED_REFERENT_SCOPE_MODE,
            actor_concept_id=actor_id,
            organisation_concept_id=None,
        ).to_mapping()
        derived_id_available = not _concept_exists_unfiltered(effective_concept_id)
        derived_id_compatible = can_access_concept(
            effective_concept_id
        ) and _visible_concept_satisfies_requested_core(
            concept_id=effective_concept_id,
            concept_spec=concept_spec,
            parent_id=parent_id,
            instance_of_type=_normalise_concept_id(
                arguments.get("instance_of_type")
                or concept_spec.get("instance_of_type")
            ),
            required_publication_context=required_context,
        )
        if derived_id_available or derived_id_compatible:
            recovered_spec = {
                key: _json_safe(value)
                for key, value in concept_spec.items()
                if key in _CREATE_CORE_CONCEPT_FIELDS
            }
            recovered_spec.update(
                {
                    "concept_id": effective_concept_id,
                    "kind": "instance",
                }
            )
            requested_instance_type = _normalise_concept_id(
                arguments.get("instance_of_type")
                or concept_spec.get("instance_of_type")
            )
            if requested_instance_type:
                recovered_spec["instance_of_type"] = requested_instance_type
            recovery_arguments = {
                "parent_id": parent_id,
                "concepts": [recovered_spec],
                "duplicate_resolution_mode": "canonical_id_only",
                "collision_resolution_mode": (ACTOR_SCOPED_REFERENT_COLLISION_MODE),
                "requested_concept_id": expected_concept_id,
                "scope_mode": ACTOR_SCOPED_REFERENT_SCOPE_MODE,
            }
            affordances.append(
                {
                    "action_type": ACTOR_SCOPED_REFERENT_RECOVERY_ACTION,
                    "tool": "create_concepts",
                    "recovery_contract": (ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT),
                    "arguments": recovery_arguments,
                    "semantic_effect": (
                        "Create or idempotently reuse an actor-private instance "
                        "referent. This does not create an alias, assert identity "
                        "or equivalence, publish knowledge, or access the concept "
                        "occupying the requested ID."
                    ),
                }
            )
    return OntologyMutationCommandError(
        "ontology_create_concept_id_conflict",
        "The requested concept ID cannot be created.",
        recovery_affordances=affordances,
    )


def _create_read_back_concept_ids(
    *,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
) -> list[str]:
    """Resolve created or reused subjects without mistaking parents for output."""

    if result.get("success") is False:
        return []
    idempotent_reuse = bool(result.get("idempotent_reuse"))
    if result.get("changed") is False and not idempotent_reuse:
        return []
    candidates: list[Any] = [
        *(result.get("created_concept_ids") or []),
        *(result.get("resolved_concept_ids") or []),
    ]
    for row in result.get("results") or []:
        if (
            not isinstance(row, Mapping)
            or not bool(row.get("success"))
            or (row.get("changed") is False and not bool(row.get("idempotent_reuse")))
        ):
            continue
        candidates.extend(
            (
                row.get("concept_id"),
                (row.get("concept") or {}).get("concept_id")
                if isinstance(row.get("concept"), Mapping)
                else None,
            )
        )
    return list(
        dict.fromkeys(
            concept_id
            for concept_id in (_normalise_concept_id(item) for item in candidates)
            if concept_id
        )
    )


def _relationship_read_back(
    *,
    source_id: str,
    predicate: str | None,
    target: Any,
) -> dict[str, Any]:
    source = ConceptsRepository.find_one({"concept_id": source_id})
    if not isinstance(source, Mapping):
        return {"source_id": source_id, "source_exists": False}
    predicate_value = _clean_text(predicate)
    target_value = _clean_text(target)
    relationships = source.get("relationships")
    relation_map = relationships if isinstance(relationships, Mapping) else {}
    candidates = {predicate_value}
    if predicate_value.startswith("#V#"):
        candidates.add(predicate_value[3:])
    else:
        candidates.add(f"#V#{predicate_value}")
    values: list[str] = []
    for candidate in candidates:
        raw = relation_map.get(candidate)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(str(item) for item in raw if isinstance(item, str))
    inverse_predicate = get_structural_inverse_map().get(
        normalise_structural_predicate(predicate_value)
    )
    inverse_present: bool | None = None
    if inverse_predicate and target_value.startswith("#V#"):
        target_document = ConceptsRepository.find_one({"concept_id": target_value})
        target_relationships = (
            target_document.get("relationships")
            if isinstance(target_document, Mapping)
            and isinstance(target_document.get("relationships"), Mapping)
            else {}
        )
        inverse_raw = target_relationships.get(inverse_predicate)
        inverse_values = (
            [inverse_raw] if isinstance(inverse_raw, str) else list(inverse_raw or [])
        )
        inverse_present = source_id in inverse_values
    return {
        "source_id": source_id,
        "source_exists": True,
        "predicate": predicate_value,
        "target": target_value,
        "relationship_present": target_value in values,
        "inverse_predicate": inverse_predicate,
        "inverse_relationship_present": inverse_present,
        "publication_context": concept_publication_context(source_id).to_mapping(),
    }


def _text_read_back(
    *,
    concept_id: str,
    predicate: str | None,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    relation_id = _clean_text((result or {}).get("relation_id"))
    query: dict[str, Any] = {"subject_concept_id": concept_id}
    if relation_id:
        try:
            from bson import ObjectId

            query = {"_id": ObjectId(relation_id), "subject_concept_id": concept_id}
        except Exception:  # noqa: BLE001 - relation IDs can be non-ObjectId fixtures
            query = {"_id": relation_id, "subject_concept_id": concept_id}
    elif predicate:
        query["predicate"] = predicate
    relation = TextRelationsRepository.find_one(query)
    text_value = (
        _text_value_snapshot(relation.get("object_text_id"))
        if isinstance(relation, Mapping)
        else None
    )
    return {
        "concept_id": concept_id,
        "predicate": (
            _normalise_predicate(relation.get("predicate"))
            if isinstance(relation, Mapping)
            else predicate
        ),
        "relation_id": str(relation.get("_id"))
        if isinstance(relation, Mapping)
        else None,
        "relation_present": isinstance(relation, Mapping),
        "text_sha256": (
            _text_sha256(text_value.get("text"))
            if isinstance(text_value, Mapping)
            else None
        ),
        "language": (
            _clean_text(text_value.get("lang"))
            if isinstance(text_value, Mapping)
            else None
        ),
        "context_sha256": (
            _canonical_json_sha256(relation.get("context") or {})
            if isinstance(relation, Mapping)
            else None
        ),
        "provenance_sha256": (
            _canonical_json_sha256(text_value.get("provenance") or {})
            if isinstance(text_value, Mapping)
            else None
        ),
        "publication_context": concept_publication_context(concept_id).to_mapping(),
    }


def _legacy_name_read_back(
    *,
    concept_id: str,
    intent_delta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = _legacy_name_document_snapshot(concept_id)
    canonical_names = _canonical_has_name_snapshot(concept_id)
    expected = intent_delta if isinstance(intent_delta, Mapping) else {}
    expected_result_hash = _clean_text(
        expected.get("resulting_legacy_names_snapshot_sha256")
    )
    selected_entry_absent = bool(expected_result_hash) and (
        snapshot["names_snapshot_sha256"] == expected_result_hash
        and snapshot["names_count"] == expected.get("resulting_legacy_names_count")
    )
    return {
        "concept_id": concept_id,
        "legacy_name_selected_entry_absent": selected_entry_absent,
        "legacy_names_count": snapshot["names_count"],
        "legacy_names_snapshot_sha256": snapshot["names_snapshot_sha256"],
        **canonical_names,
        "publication_context": concept_publication_context(concept_id).to_mapping(),
    }


def _delete_legacy_name_compare_and_set(
    *,
    concept_id: str,
    precondition: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically remove one selected legacy row without rewriting its peers."""

    updated = ConceptsRepository.find_one_and_update(
        {
            "concept_id": concept_id,
            "names": list(precondition["names"]),
        },
        {
            "$set": {
                "names": list(precondition["resulting_names"]),
                "embedding_status": "stale",
                "updated_at": datetime.now(UTC),
            }
        },
        return_document=True,
    )
    if not isinstance(updated, Mapping):
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": "legacy_name_compare_and_set_failed",
            "error": (
                "The legacy name list changed before it could be updated; "
                "reload the concept and select it again."
            ),
        }
    try:
        from .concept_service import _invalidate_concept_mutation_caches

        _invalidate_concept_mutation_caches()
    except Exception:
        logger.exception("Legacy-name cleanup cache invalidation failed")
    return {
        "success": True,
        "changed": True,
        "concept_id": concept_id,
        "deleted_legacy_name_ordinal": precondition["ordinal"],
        "deleted_legacy_name_entry_sha256": precondition["entry_sha256"],
        "previous_legacy_names_snapshot_sha256": precondition["names_snapshot_sha256"],
        "legacy_names_snapshot_sha256": precondition["resulting_names_snapshot_sha256"],
        "legacy_names_count": len(precondition["resulting_names"]),
    }


def _uncertain_promotion_read_back(
    *,
    source_id: str,
    assertion_id: str,
) -> dict[str, Any]:
    try:
        snapshot = _uncertain_assertion_snapshot(
            source_id=source_id,
            assertion_id=assertion_id,
        )
    except OntologyMutationCommandError:
        return {
            "source_id": source_id,
            "assertion_id": assertion_id,
            "assertion_present": False,
        }
    predicate = _normalise_predicate(snapshot.get("predicate"))
    target = snapshot.get("target")
    if _target_is_concept(target):
        relation = _relationship_read_back(
            source_id=source_id,
            predicate=predicate,
            target=target,
        )
    else:
        relation = _text_read_back(
            concept_id=source_id,
            predicate=predicate,
        )
    return {
        "source_id": source_id,
        "assertion_id": assertion_id,
        "assertion_present": True,
        "assertion_status": snapshot.get("status"),
        "assertion_revision": snapshot.get("revision"),
        "predicate": predicate,
        "target_concept_id": target if _target_is_concept(target) else None,
        "target_text_sha256": None
        if _target_is_concept(target)
        else _text_sha256(target),
        "canonical_relation": relation,
    }


def _bulk_relationship_read_back(arguments: Mapping[str, Any]) -> dict[str, Any]:
    resolved = _relationship_arguments_with_resolved_selector(
        "remove_relationships_bulk",
        arguments,
    )
    rows = resolved.get("relationships") or resolved.get("relations") or []
    read_backs = [
        _relationship_read_back(
            source_id=_normalise_concept_id(row.get("source_id")) or "",
            predicate=_normalise_predicate(row.get("predicate")),
            target=row.get("target"),
        )
        for row in rows
        if isinstance(row, Mapping) and _normalise_concept_id(row.get("source_id"))
    ]
    return {
        "relationship_count": len(read_backs),
        "relationships": read_backs,
        "selector_projection_sha256": _effect_arguments_sha256(
            {
                "relationships": [
                    {
                        "source_id": row.get("source_id"),
                        "predicate": row.get("predicate"),
                        "target": row.get("target"),
                    }
                    for row in read_backs
                ]
            }
        ),
    }


def canonical_read_back_for_method(
    *,
    method_name: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    method = _clean_text(method_name)
    arguments = _relationship_arguments_with_resolved_selector(method, arguments)
    if method == "promote_uncertain_relationship_assertion":
        return _uncertain_promotion_read_back(
            source_id=_normalise_concept_id(arguments.get("source_id")) or "",
            assertion_id=_clean_text(arguments.get("assertion_id")),
        )
    if method in {"add_relationship", "remove_relationship"}:
        return _relationship_read_back(
            source_id=_normalise_concept_id(arguments.get("source_id")) or "",
            predicate=_normalise_predicate(arguments.get("predicate"))
            or _normalise_predicate((result or {}).get("predicate")),
            target=arguments.get("target") or (result or {}).get("target"),
        )
    if method == "remove_relationships_bulk":
        return _bulk_relationship_read_back(arguments)
    if method == "delete_legacy_name":
        result_map = result if isinstance(result, Mapping) else {}
        return _legacy_name_read_back(
            concept_id=_normalise_concept_id(arguments.get("concept_id")) or "",
            intent_delta={
                "resulting_legacy_names_snapshot_sha256": result_map.get(
                    "legacy_names_snapshot_sha256"
                ),
                "resulting_legacy_names_count": result_map.get("legacy_names_count"),
            },
        )
    if method in {
        "upsert_text_relation",
        "update_text_relation",
        "upsert_singleton_text_relation",
        "add_names_to_concept",
        "delete_text_relation",
    }:
        return _text_read_back(
            concept_id=_normalise_concept_id(
                arguments.get("concept_id") or arguments.get("subject_concept_id")
            )
            or "",
            predicate=_normalise_predicate(arguments.get("predicate")),
            result=result,
        )
    if method == "create_concepts":
        concept_ids = _create_read_back_concept_ids(
            arguments=arguments,
            result=result or {},
        )
        return {
            "concepts": [
                _concept_read_back(item)
                for item in concept_ids
                if _normalise_concept_id(item)
            ]
        }
    if method == "rename_concept":
        new_id = _normalise_concept_id(arguments.get("new_id"))
        return _concept_read_back(new_id) if new_id else {"exists": False}
    if method == "merge_concepts":
        from .concept_merge_service import read_concept_merge_postcondition

        exact_plan = arguments.get("exact_plan")
        if not isinstance(exact_plan, Mapping):
            return {
                "matches_exact_plan": False,
                "error_code": "identity_consolidation_plan_required",
            }
        return read_concept_merge_postcondition(exact_plan)
    concept_id = _normalise_concept_id(
        arguments.get("concept_id") or arguments.get("old_id")
    )
    return _concept_read_back(concept_id) if concept_id else {"exists": False}


def _safe_command_error(exc: OntologyMutationCommandError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "effect_status": "not_started",
        "mutation_outcome": "not_started",
        "changed": False,
        "error_code": exc.reason_code,
        "error": exc.public_message,
    }
    if exc.error_details is not None:
        payload["error_details"] = dict(exc.error_details)
    if exc.recovery_affordances is not None:
        if exc.recovery_affordances:
            payload["recovery_affordances"] = [
                dict(affordance) for affordance in exc.recovery_affordances
            ]
    elif exc.reason_code not in {
        "complex_create_requires_typed_effects",
        "multi_create_requires_individual_effects",
    }:
        payload["recovery_affordances"] = [
            {"action_type": "create_scoped_assertion"},
            {"action_type": "request_ontology_administrator_delegation"},
        ]
    return payload


def _verify_method_postcondition(
    *,
    method_name: str,
    intent: OntologyMutationIntent,
    result: Mapping[str, Any],
    canonical_state: Any,
) -> bool:
    """Prove the smallest exact postcondition needed to claim success."""

    if not isinstance(canonical_state, Mapping):
        return False
    method = _clean_text(method_name)
    if method == "add_relationship":
        if canonical_state.get("relationship_present") is not True:
            return False
        if intent.delta.get("maintain_inverse"):
            return canonical_state.get("inverse_relationship_present") is True
        return True
    if method == "remove_relationship":
        if canonical_state.get("relationship_present") is not False:
            return False
        if intent.delta.get("maintain_inverse"):
            return canonical_state.get("inverse_relationship_present") is False
        return True
    if method == "remove_relationships_bulk":
        rows = canonical_state.get("relationships")
        return (
            result.get("success") is True
            and str(result.get("status") or "").lower() != "partial"
            and canonical_state.get("relationship_count")
            == intent.delta.get("relationship_count")
            and canonical_state.get("selector_projection_sha256")
            == intent.delta.get("selector_projection_sha256")
            and isinstance(rows, list)
            and len(rows) == int(intent.delta.get("relationship_count") or 0)
            and all(
                isinstance(row, Mapping)
                and row.get("relationship_present") is False
                and (row.get("inverse_relationship_present") in {False, None})
                for row in rows
            )
        )
    if method in {
        "upsert_text_relation",
        "update_text_relation",
        "upsert_singleton_text_relation",
    }:
        expected_hash = intent.delta.get("text_sha256")
        exact_relation = (
            canonical_state.get("relation_present") is True
            and canonical_state.get("text_sha256") == expected_hash
            and canonical_state.get("predicate") == intent.predicate
            and canonical_state.get("language") == intent.delta.get("language")
            and canonical_state.get("provenance_sha256")
            == intent.delta.get("provenance_sha256")
        )
        if method in {"upsert_text_relation", "upsert_singleton_text_relation"}:
            exact_relation = exact_relation and (
                canonical_state.get("context_sha256")
                == intent.delta.get("context_sha256")
            )
        return bool(expected_hash) and exact_relation
    if method == "delete_text_relation":
        return canonical_state.get("relation_present") is False
    if method == "delete_legacy_name":
        return (
            result.get("success") is True
            and canonical_state.get("legacy_name_selected_entry_absent") is True
            and canonical_state.get("legacy_names_count")
            == intent.delta.get("resulting_legacy_names_count")
            and canonical_state.get("legacy_names_snapshot_sha256")
            == intent.delta.get("resulting_legacy_names_snapshot_sha256")
            and canonical_state.get("canonical_has_name_present") is True
            and canonical_state.get("canonical_has_name_count")
            == intent.delta.get("canonical_has_name_count")
            and canonical_state.get("canonical_has_name_relation_ids")
            == intent.delta.get("canonical_has_name_relation_ids")
            and canonical_state.get("canonical_has_name_snapshot_sha256")
            == intent.delta.get("canonical_has_name_snapshot_sha256")
        )
    if method == "create_concepts":
        concepts = canonical_state.get("concepts")
        expected_context = intent.publication_context.to_mapping()
        specs = intent.delta.get("concepts")
        if (
            not isinstance(concepts, list)
            or len(concepts) != 1
            or not isinstance(specs, list)
            or len(specs) != 1
            or not isinstance(concepts[0], Mapping)
            or not isinstance(specs[0], Mapping)
        ):
            return False
        actual = concepts[0]
        spec = specs[0]
        expected_id = _normalise_concept_id(spec.get("concept_id"))
        if intent.delta.get("visible_exact_reuse"):
            if result.get("idempotent_reuse") is not True or not expected_id:
                return False
            required_context = (
                expected_context if intent.delta.get("actor_scoped_referent") else None
            )
            return _visible_concept_satisfies_requested_core(
                concept_id=expected_id,
                concept_spec=spec,
                parent_id=_normalise_concept_id(intent.delta.get("parent_id")),
                instance_of_type=_normalise_concept_id(
                    intent.delta.get("instance_of_type") or spec.get("instance_of_type")
                ),
                required_publication_context=required_context,
            )
        if (
            actual.get("exists") is not True
            or actual.get("concept_id") != expected_id
            or not isinstance(actual.get("publication_context"), Mapping)
            or actual["publication_context"].get("kind") != expected_context.get("kind")
            or actual["publication_context"].get("concept_id")
            != expected_context.get("concept_id")
        ):
            return False
        parent_id = _normalise_concept_id(intent.delta.get("parent_id"))
        instance_of_type = _normalise_concept_id(
            intent.delta.get("instance_of_type") or spec.get("instance_of_type")
        )
        kind = _clean_text(spec.get("kind")).lower()
        if kind == "individual":
            kind = "instance"
        if instance_of_type:
            expected_type_parents = [parent_id] if parent_id else []
            expected_instance_types = [instance_of_type]
        elif kind in {"instance", "predicate"}:
            expected_type_parents = []
            expected_instance_types = [parent_id] if parent_id else []
        else:
            expected_type_parents = [parent_id] if parent_id else []
            expected_instance_types = []
        relationships = actual.get("forward_relationships")
        if not isinstance(relationships, Mapping) or (
            relationships.get("is_a_type_of") != expected_type_parents
            or relationships.get("is_an_instance_of") != expected_instance_types
            or relationships.get("linked_to") != []
        ):
            return False
        if (
            actual.get("attributes") != {}
            or actual.get("system_tags") != []
            or actual.get("user_tags") != []
            or actual.get("vontology_path") != spec.get("vontology_path")
        ):
            return False
        texts = actual.get("text_relations")
        if not isinstance(texts, list):
            return False

        def has_text(
            predicate: str, value: Any, *, name_type: str | None = None
        ) -> bool:
            if not isinstance(value, str) or not value.strip():
                return True
            return any(
                isinstance(row, Mapping)
                and row.get("predicate") == predicate
                and row.get("text") == value.strip()
                and (
                    name_type is None
                    or (
                        isinstance(row.get("context"), Mapping)
                        and row["context"].get("name_type") == name_type
                    )
                )
                for row in texts
            )

        return (
            has_text("hasName", spec.get("name"), name_type="NL")
            and has_text("hasDescription", spec.get("description"))
            and has_text("hasNote", spec.get("notes"))
        )
    if method == "promote_uncertain_relationship_assertion":
        relation = canonical_state.get("canonical_relation")
        return (
            canonical_state.get("assertion_status") == "promoted"
            and isinstance(relation, Mapping)
            and (
                relation.get("relationship_present") is True
                or relation.get("relation_present") is True
            )
        )
    if method == "update_concept":
        # The command read-back at minimum proves the same canonical target and
        # publication context remained present. Field-specific values are bound
        # in the intent and specialised text updates use dedicated commands.
        return (
            canonical_state.get("exists") is True
            and isinstance(canonical_state.get("publication_context"), Mapping)
            and canonical_state["publication_context"].get("kind")
            == intent.publication_context.kind.value
            and canonical_state["publication_context"].get("concept_id")
            == intent.publication_context.concept_id
        )
    if method == "add_names_to_concept":
        # The handler reports one result per requested name; no partial result
        # is a successful postcondition.
        return bool(result.get("success")) and int(result.get("error_count") or 0) == 0
    if method == "merge_concepts":
        return (
            result.get("success") is True
            and canonical_state.get("matches_exact_plan") is True
            and canonical_state.get("plan_sha256")
            == intent.delta.get("merge_plan_sha256")
            and canonical_state.get("source_id") == intent.delta.get("source_id")
            and canonical_state.get("target_id") == intent.delta.get("target_id")
        )
    # Rename and delete remain preview-only at this boundary.
    return False


def _postcondition_reconciliation_contract(
    *,
    method_name: str,
    intent: OntologyMutationIntent,
) -> dict[str, Any]:
    """Carry the immutable verified intent needed for a later exact read."""

    return {
        "schema_version": "ontology_mutation_postcondition_reconciliation.v1",
        "method_name": _clean_text(method_name),
        "intent_fingerprint": intent.fingerprint,
        "intent": intent.canonical_payload(),
    }


def _visible_create_reuse_result(intent: OntologyMutationIntent) -> dict[str, Any]:
    specs = intent.delta.get("concepts") or []
    spec = specs[0] if len(specs) == 1 and isinstance(specs[0], Mapping) else {}
    concept_id = _normalise_concept_id(spec.get("concept_id"))
    result: dict[str, Any] = {
        "success": True,
        "effect_status": "succeeded",
        "mutation_outcome": "succeeded",
        "changed": False,
        "idempotent_reuse": True,
        "created_concept_ids": [],
        "resolved_concept_ids": [concept_id] if concept_id else [],
        "results": [
            {
                "success": True,
                "effect_status": "succeeded",
                "changed": False,
                "idempotent_reuse": True,
                "concept_id": concept_id,
                "requested_name": spec.get("name"),
                "requested_kind": spec.get("kind"),
            }
        ],
        "total": 1,
        "successful": 1,
        "already_existed": 1,
        "failed": 0,
    }
    if concept_id and intent.delta.get("actor_scoped_referent"):
        result["actor_scoped_referent"] = _actor_scoped_referent_result_metadata(
            concept_id
        )
    return result


def _publication_context_from_payload(value: Any) -> PublicationContext | None:
    if not isinstance(value, Mapping):
        return None
    from .ontology_publication_authority_service import PublicationContextKind

    try:
        kind = PublicationContextKind(_clean_text(value.get("kind")))
    except ValueError:
        return None
    historical_predicates = tuple(
        _clean_text(item)
        for item in value.get("historical_predicates") or ()
        if _clean_text(item)
    )
    return PublicationContext(
        kind=kind,
        concept_id=_normalise_concept_id(value.get("concept_id")),
        source=_clean_text(value.get("source")) or "resolved",
        historical_predicates=historical_predicates,
    )


def _intent_from_postcondition_reconciliation_contract(
    value: Any,
) -> OntologyMutationIntent | None:
    if not isinstance(value, Mapping) or value.get("schema_version") != (
        "ontology_mutation_postcondition_reconciliation.v1"
    ):
        return None
    payload = value.get("intent")
    if not isinstance(payload, Mapping):
        return None
    publication_context = _publication_context_from_payload(
        payload.get("publication_context")
    )
    if publication_context is None:
        return None
    source_contexts: list[PublicationContext] = []
    for raw_context in payload.get("source_contexts") or ():
        context = _publication_context_from_payload(raw_context)
        if context is None:
            return None
        source_contexts.append(context)
    intent = OntologyMutationIntent(
        operation=_clean_text(payload.get("operation")),
        publication_context=publication_context,
        target_concept_ids=tuple(
            concept_id
            for concept_id in (
                _normalise_concept_id(item)
                for item in payload.get("target_concept_ids") or ()
            )
            if concept_id
        ),
        tool_name=_clean_text(payload.get("tool_name")) or None,
        predicate=_normalise_predicate(payload.get("predicate")),
        source_contexts=tuple(source_contexts),
        delta=dict(payload.get("delta") or {}),
    )
    expected_fingerprint = _clean_text(value.get("intent_fingerprint"))
    if not expected_fingerprint or intent.fingerprint != expected_fingerprint:
        return None
    return intent


def reconcile_governed_ontology_postcondition(
    *,
    method_name: str,
    arguments: Mapping[str, Any],
    original_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-read and prove an earlier indeterminate ontology mutation exactly.

    No mutation is retried.  The original command's immutable intent contract
    and authority receipt are required, then the same method verifier is run
    over current canonical state.  A successful proof also advances the
    durable operational receipt from indeterminate to succeeded.
    """

    method = _clean_text(method_name)
    contract = original_result.get("postcondition_reconciliation")
    intent = _intent_from_postcondition_reconciliation_contract(contract)
    authority_receipt = original_result.get("authority_receipt")
    authority_receipt = (
        authority_receipt if isinstance(authority_receipt, Mapping) else {}
    )
    if (
        intent is None
        or method != intent.tool_name
        or method != _clean_text((contract or {}).get("method_name"))
        or authority_receipt.get("intent_fingerprint") != intent.fingerprint
        or _clean_text(authority_receipt.get("status")) != "indeterminate"
    ):
        return {
            "success": False,
            "verified": False,
            "error_code": "ontology_reconciliation_contract_invalid",
        }
    try:
        reconciliation_arguments = resolve_governed_ontology_arguments(
            method,
            normalise_governed_ontology_arguments(method, arguments),
        )
    except OntologyMutationCommandError:
        reconciliation_arguments = None
    if (
        reconciliation_arguments is None
        or intent.delta.get("effect_arguments_sha256")
        != _effect_arguments_sha256(reconciliation_arguments)
    ):
        return {
            "success": False,
            "verified": False,
            "error_code": "ontology_reconciliation_contract_invalid",
        }

    result_hint = dict(original_result)
    result_hint.update({"success": True, "changed": True})
    if method == "create_concepts":
        expected_concept_id = _normalise_concept_id(
            intent.delta.get("expected_concept_id")
        )
        result_hint["created_concept_ids"] = (
            [expected_concept_id] if expected_concept_id else []
        )
    try:
        canonical_state = canonical_read_back_for_method(
            method_name=method,
            arguments=reconciliation_arguments,
            result=result_hint,
        )
        verified = _verify_method_postcondition(
            method_name=method,
            intent=intent,
            result=result_hint,
            canonical_state=canonical_state,
        )
    except Exception:
        logger.exception("Governed ontology postcondition reconciliation failed")
        return {
            "success": False,
            "verified": False,
            "error_code": "ontology_reconciliation_read_failed",
        }
    if not verified:
        return {
            "success": True,
            "verified": False,
            "receipt_id": authority_receipt.get("receipt_id"),
            "intent_fingerprint": intent.fingerprint,
            "target_concept_ids": list(intent.target_concept_ids),
            "canonical_read_back": canonical_state,
        }

    receipt = reconcile_indeterminate_mutation_receipt(
        receipt_id=_clean_text(authority_receipt.get("receipt_id")),
        intent_fingerprint=intent.fingerprint,
        canonical_read_back=canonical_state,
        response_projection={
            **result_hint,
            "effect_status": "succeeded",
            "mutation_outcome": "succeeded",
            "outcome_finality": "terminal_for_turn",
        },
    )
    if not isinstance(receipt, Mapping) or receipt.get("status") != "succeeded":
        return {
            "success": False,
            "verified": False,
            "error_code": "ontology_reconciliation_receipt_not_finalised",
        }
    return {
        "success": True,
        "verified": True,
        "status": "verified",
        "method_name": method,
        "receipt_id": receipt.get("receipt_id"),
        "intent_fingerprint": intent.fingerprint,
        "target_concept_ids": list(intent.target_concept_ids),
        "canonical_read_back": canonical_state,
        "authority_receipt": dict(receipt),
    }


def execute_governed_ontology_method(
    *,
    method_name: str,
    arguments: Mapping[str, Any],
    mutate: Callable[[], Mapping[str, Any]],
    preview: bool = False,
) -> dict[str, Any]:
    try:
        intent = build_ontology_mutation_intent(
            method_name=method_name,
            arguments=arguments,
        )
    except OntologyMutationCommandError as exc:
        return _safe_command_error(exc)
    except LookupError:
        return _safe_command_error(
            OntologyMutationCommandError(
                "ontology_mutation_target_not_found",
                "The requested ontology target was not found.",
            )
        )
    except ValueError:
        return _safe_command_error(
            OntologyMutationCommandError(
                "invalid_ontology_mutation",
                "The requested ontology mutation is invalid.",
            )
        )
    except Exception:
        # Intent construction is read-only. An unexpected repository or
        # resolver failure here is therefore a definite pre-effect failure,
        # not an indeterminate mutation. Keep the public response non-leaking
        # while retaining the exception in server diagnostics.
        logger.exception("Governed ontology mutation preflight failed")
        return {
            "success": False,
            "error_code": "exception",
            "error": "Ontology mutation preflight failed before any effect.",
        }
    result_holder: dict[str, Mapping[str, Any]] = {}

    def perform_locked(*, exact_create_reuse: bool = False) -> Mapping[str, Any]:
        if method_name == "create_concepts" and (
            exact_create_reuse or intent.delta.get("visible_exact_reuse")
        ):
            result = _visible_create_reuse_result(intent)
            result_holder["result"] = result
            return result
        if method_name == "delete_legacy_name":
            concept_id = _normalise_concept_id(arguments.get("concept_id")) or ""
            try:
                precondition = _validated_legacy_name_precondition(
                    concept_id=concept_id,
                    legacy_name_selector=arguments.get("legacy_name_selector"),
                )
                canonical_names = _canonical_has_name_snapshot(concept_id)
            except OntologyMutationCommandError as exc:
                return _safe_command_error(exc)
            if canonical_names["canonical_has_name_present"] is not True:
                return _safe_command_error(
                    OntologyMutationCommandError(
                        "canonical_has_name_required_for_legacy_cleanup",
                        (
                            "A legacy inline name can be removed only while a "
                            "canonical hasName relation exists."
                        ),
                    )
                )
            if (
                canonical_names["canonical_has_name_count"]
                != intent.delta.get("canonical_has_name_count")
                or canonical_names["canonical_has_name_relation_ids"]
                != intent.delta.get("canonical_has_name_relation_ids")
                or canonical_names["canonical_has_name_snapshot_sha256"]
                != intent.delta.get("canonical_has_name_snapshot_sha256")
            ):
                return _safe_command_error(
                    OntologyMutationCommandError(
                        "canonical_has_name_precondition_failed",
                        (
                            "The canonical hasName relation changed; reload the "
                            "concept before removing legacy data."
                        ),
                    )
                )
            result = _delete_legacy_name_compare_and_set(
                concept_id=concept_id,
                precondition=precondition,
            )
            result_holder["result"] = result
            return result
        if method_name == "merge_concepts":
            from .concept_merge_service import (
                ConceptMergePlanError,
                build_concept_merge_plan,
            )

            try:
                current_plan = build_concept_merge_plan(
                    _normalise_concept_id(arguments.get("source_id")) or "",
                    _normalise_concept_id(arguments.get("target_id")) or "",
                )
            except ConceptMergePlanError as exc:
                return _safe_command_error(
                    OntologyMutationCommandError(
                        exc.reason_code,
                        exc.public_message,
                    )
                )
            expected_plan = arguments.get("exact_plan")
            if (
                not isinstance(expected_plan, Mapping)
                or _clean_text(current_plan.get("plan_sha256"))
                != _clean_text(expected_plan.get("plan_sha256"))
                or _clean_text(current_plan.get("plan_sha256"))
                != _clean_text(intent.delta.get("merge_plan_sha256"))
            ):
                return {
                    "success": False,
                    "effect_status": "not_started",
                    "mutation_outcome": "not_started",
                    "changed": False,
                    "error_code": "identity_consolidation_precondition_failed",
                    "error": (
                        "The affected identity graph changed after merge "
                        "authority was resolved."
                    ),
                }
        if method_name in {
            "update_text_relation",
            "delete_text_relation",
        } and _clean_text(arguments.get("relation_id")):
            try:
                relation_id = _clean_text(arguments.get("relation_id"))
                with ontology_mutation_resource_lock(
                    f"ontology-text-relation:{relation_id}"
                ):
                    current_relation = _text_relation_snapshot(
                        subject_concept_id=(
                            _normalise_concept_id(
                                arguments.get("concept_id")
                                or arguments.get("subject_concept_id")
                            )
                            or ""
                        ),
                        relation_id=relation_id,
                    )
                    if current_relation.get("revision") != intent.delta.get(
                        "relation_revision"
                    ):
                        return {
                            "success": False,
                            "effect_status": "not_started",
                            "mutation_outcome": "not_started",
                            "changed": False,
                            "error_code": "text_relation_precondition_failed",
                            "error": (
                                "The text relation changed after authority was "
                                "resolved; obtain a new exact delegation."
                            ),
                        }
                    result = mutate()
                    result_holder["result"] = result
                    return result
            except OntologyMutationCommandError as exc:
                return _safe_command_error(exc)
            except OntologyMutationResourceBusy:
                return {
                    "success": False,
                    "effect_status": "not_started",
                    "mutation_outcome": "not_started",
                    "changed": False,
                    "retryable": True,
                    "error_code": "ontology_mutation_resource_busy",
                    "error": "Another text relation mutation is already in progress.",
                }
        if method_name == "promote_uncertain_relationship_assertion":
            try:
                current_assertion = _uncertain_assertion_snapshot(
                    source_id=_normalise_concept_id(arguments.get("source_id")) or "",
                    assertion_id=_clean_text(arguments.get("assertion_id")),
                )
            except OntologyMutationCommandError as exc:
                return _safe_command_error(exc)
            if current_assertion.get("revision") != intent.delta.get(
                "assertion_revision"
            ):
                return {
                    "success": False,
                    "effect_status": "not_started",
                    "mutation_outcome": "not_started",
                    "changed": False,
                    "error_code": "uncertain_assertion_precondition_failed",
                    "error": (
                        "The uncertain assertion changed after authority was "
                        "resolved; obtain a new exact delegation."
                    ),
                }
        result = mutate()
        if method_name == "create_concepts" and "success" not in result:
            expected_concept_id = _normalise_concept_id(
                intent.delta.get("expected_concept_id")
            )
            result = {
                "success": True,
                "changed": True,
                "created_concept_ids": (
                    [expected_concept_id] if expected_concept_id else []
                ),
                "concept": dict(result),
            }
        if (
            method_name == "create_concepts"
            and intent.delta.get("actor_scoped_referent")
            and result.get("success") is True
        ):
            effective_concept_id = _normalise_concept_id(
                intent.delta.get("expected_concept_id")
            )
            if effective_concept_id:
                result = {
                    **dict(result),
                    "actor_scoped_referent": (
                        _actor_scoped_referent_result_metadata(effective_concept_id)
                    ),
                }
        result_holder["result"] = result
        return result

    def perform() -> Mapping[str, Any]:
        exact_create_reuse = bool(intent.delta.get("visible_exact_reuse"))
        scope_fingerprints = intent.delta.get("affected_scope_fingerprints")
        expected = (
            dict(scope_fingerprints) if isinstance(scope_fingerprints, Mapping) else {}
        )
        try:
            with ExitStack() as locks:
                resource_keys = {
                    *(f"ontology-publication-scope:{item}" for item in expected),
                    *ontology_authority_resource_keys(intent),
                }
                if method_name == "merge_concepts":
                    resource_keys.update(
                        f"ontology-text-relation:{relation_id}"
                        for relation_id in intent.delta.get("merge_text_relation_ids")
                        or []
                        if _clean_text(relation_id)
                    )
                if (
                    method_name
                    in {
                        "upsert_text_relation",
                        "update_text_relation",
                        "upsert_singleton_text_relation",
                        "delete_text_relation",
                        "delete_legacy_name",
                    }
                    and _clean_text(intent.predicate).removeprefix("#V#") == "hasName"
                ):
                    concept_id = _normalise_concept_id(
                        arguments.get("concept_id")
                        or arguments.get("subject_concept_id")
                    )
                    if concept_id:
                        resource_keys.add(f"ontology-concept-names:{concept_id}")
                if method_name == "delete_legacy_name":
                    concept_id = _normalise_concept_id(arguments.get("concept_id"))
                    resource_keys.update(
                        f"ontology-text-relation:{relation_id}"
                        for relation_id in intent.delta.get(
                            "canonical_has_name_relation_ids"
                        )
                        or []
                        if _clean_text(relation_id)
                    )
                if method_name == "create_concepts":
                    expected_concept_id = _normalise_concept_id(
                        intent.delta.get("expected_concept_id")
                    )
                    if expected_concept_id:
                        resource_keys.add(
                            f"ontology-publication-scope:{expected_concept_id}"
                        )
                for resource_key in sorted(resource_keys):
                    locks.enter_context(ontology_mutation_resource_lock(resource_key))
                for concept_id, expected_fingerprint in expected.items():
                    try:
                        current = scope_read_back(concept_id)
                    except LookupError:
                        return _safe_command_error(
                            OntologyMutationCommandError(
                                "ontology_mutation_target_not_found",
                                "An affected ontology target is no longer available.",
                            )
                        )
                    if _clean_text(current.get("scope_fingerprint")) != _clean_text(
                        expected_fingerprint
                    ):
                        return {
                            "success": False,
                            "effect_status": "not_started",
                            "mutation_outcome": "not_started",
                            "changed": False,
                            "error_code": "ontology_mutation_scope_precondition_failed",
                            "error": (
                                "An affected publication scope changed after "
                                "authority was resolved."
                            ),
                        }
                if method_name == "create_concepts":
                    expected_concept_id = _normalise_concept_id(
                        intent.delta.get("expected_concept_id")
                    )
                    expected_exists = bool(
                        expected_concept_id
                        and _concept_exists_unfiltered(expected_concept_id)
                    )
                    if exact_create_reuse and not expected_exists:
                        return _safe_command_error(
                            OntologyMutationCommandError(
                                "ontology_create_reuse_precondition_failed",
                                (
                                    "The compatible concept selected for reuse is "
                                    "no longer available."
                                ),
                                recovery_affordances=(),
                            )
                        )
                    if expected_exists:
                        specs = intent.delta.get("concepts") or []
                        spec = (
                            specs[0]
                            if len(specs) == 1 and isinstance(specs[0], Mapping)
                            else None
                        )
                        required_context = (
                            intent.publication_context.to_mapping()
                            if intent.delta.get("actor_scoped_referent")
                            else None
                        )
                        compatible_reuse = bool(
                            expected_concept_id
                            and spec is not None
                            and can_access_concept(expected_concept_id)
                            and _visible_concept_satisfies_requested_core(
                                concept_id=expected_concept_id,
                                concept_spec=spec,
                                parent_id=_normalise_concept_id(
                                    intent.delta.get("parent_id")
                                ),
                                instance_of_type=_normalise_concept_id(
                                    intent.delta.get("instance_of_type")
                                    or spec.get("instance_of_type")
                                ),
                                required_publication_context=required_context,
                            )
                        )
                        if not compatible_reuse:
                            return _safe_command_error(
                                _create_id_conflict_error(
                                    arguments=arguments,
                                    expected_concept_id=(expected_concept_id or ""),
                                )
                            )
                        exact_create_reuse = True
                live_decision = authorise_ontology_mutation(intent)
                if not live_decision.allowed:
                    return ontology_authority_denial_payload(live_decision, intent)
                return perform_locked(exact_create_reuse=exact_create_reuse)
        except OntologyMutationResourceBusy:
            return {
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "retryable": True,
                "error_code": "ontology_mutation_resource_busy",
                "error": "Another ontology mutation is already in progress.",
            }

    outcome = execute_authorised_ontology_mutation(
        intent=intent,
        mutate=perform,
        read_before=lambda: canonical_read_back_for_method(
            method_name=method_name,
            arguments=arguments,
        ),
        read_back=lambda: canonical_read_back_for_method(
            method_name=method_name,
            arguments=arguments,
            result=result_holder.get("result"),
        ),
        verify_read_back=lambda result, state: _verify_method_postcondition(
            method_name=method_name,
            intent=intent,
            result=result,
            canonical_state=state,
        ),
        preview=preview,
    )
    if outcome.get("effect_status") == "indeterminate" or outcome.get(
        "mutation_outcome"
    ) in {"unknown", "partial"}:
        outcome.setdefault(
            "outcome_finality",
            "requires_canonical_reconciliation",
        )
        outcome["postcondition_reconciliation"] = (
            _postcondition_reconciliation_contract(
                method_name=method_name,
                intent=intent,
            )
        )
    return outcome


def delete_legacy_name(
    *,
    concept_id: str,
    legacy_name_selector: Mapping[str, Any],
    request_id: str | None = None,
) -> dict[str, Any]:
    """Govern and atomically remove one exact legacy inline ``names[]`` row."""

    arguments: dict[str, Any] = {
        "concept_id": concept_id,
        "legacy_name_selector": dict(legacy_name_selector),
    }
    if _clean_text(request_id):
        arguments["request_id"] = _clean_text(request_id)
    return execute_governed_ontology_method(
        method_name="delete_legacy_name",
        arguments=arguments,
        # The command service deliberately owns this mutation.  This fallback
        # can report no effect if the dedicated branch is ever disconnected;
        # it cannot become a generic concept update.
        mutate=lambda: {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": "legacy_name_dedicated_command_unavailable",
            "error": "The dedicated legacy-name cleanup command is unavailable.",
        },
    )


def issue_same_turn_method_delegation(
    *,
    method_name: str,
    arguments: Mapping[str, Any],
    actor_concept_id: str,
    organisation_concept_id: str | None,
    delegate_concept_id: str,
    audience: str,
    effect_id: str,
    turn_id: str | None,
) -> dict[str, Any]:
    try:
        intent = build_ontology_mutation_intent(
            method_name=method_name,
            arguments=arguments,
            idempotency_key=effect_id,
        )
        if method_name != "create_concepts" and not actor_can_access_intent_targets(
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            target_concept_ids=intent.target_concept_ids,
        ):
            raise OntologyMutationCommandError(
                "ontology_mutation_target_not_accessible",
                "The requested ontology target is not accessible in this context.",
            )
        return issue_agent_delegation(
            intent=intent,
            grantor_actor_concept_id=actor_concept_id,
            grantor_organisation_concept_id=organisation_concept_id,
            delegate_concept_id=delegate_concept_id,
            audience=audience,
            tool_name=method_name,
            effect_id=effect_id,
            turn_id=turn_id,
        )
    except OntologyMutationCommandError as exc:
        return _safe_command_error(exc)
    except PermissionError as exc:
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": str(exc),
            "error": "Semantic ontology authority is required for this effect.",
            "recovery_affordances": [
                {"action_type": "create_scoped_assertion"},
                {"action_type": "request_ontology_administrator_delegation"},
            ],
        }
    except (RuntimeError, ValueError) as exc:
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": "ontology_delegation_not_issued",
            "error": "The exact ontology delegation could not be issued.",
            "details": {"exception_type": type(exc).__name__},
        }


def scope_read_back(concept_id: str) -> dict[str, Any]:
    """Return exact scope edges for optimistic scope-change commands."""

    normalised = _normalise_concept_id(concept_id)
    if not normalised:
        raise ValueError("concept_id is required")
    with bypass_access_control():
        document = ConceptsRepository.find_one({"concept_id": normalised})
    if not isinstance(document, Mapping):
        raise LookupError("Concept not found")  # noqa: TRY004
    relationships = document.get("relationships")
    relation_map = relationships if isinstance(relationships, Mapping) else {}
    scope_edges = {
        predicate: list(value) if isinstance(value, list) else [value]
        for predicate in (
            *SPECIFIC_TO_USER_PREDICATES,
            *SPECIFIC_TO_ORG_PREDICATES_READ,
        )
        if (value := relation_map.get(predicate)) is not None
    }
    return {
        "concept_id": normalised,
        "publication_context": concept_publication_context(normalised).to_mapping(),
        "scope_edges": scope_edges,
        "scope_fingerprint": _text_sha256(json_dumps_sorted(scope_edges)),
    }


def json_dumps_sorted(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


__all__ = [
    "OntologyMutationCommandError",
    "build_ontology_mutation_intent",
    "canonical_read_back_for_method",
    "delete_legacy_name",
    "execute_governed_ontology_method",
    "is_ontology_mutation_method",
    "issue_same_turn_method_delegation",
    "legacy_name_selector_metadata",
    "normalise_governed_ontology_arguments",
    "reconcile_governed_ontology_postcondition",
    "resolve_governed_ontology_arguments",
    "scope_read_back",
]
