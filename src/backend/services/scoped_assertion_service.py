"""Actor-scoped assertions over visible Vontology concepts.

This store deliberately does not mutate canonical concept or text-relation
documents. It lets an authenticated user or organisation assert provenance-
bearing knowledge about a concept they can see without acquiring publication
authority over that concept.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping, Sequence

from pymongo import ReturnDocument

from ..db.mongo_client import get_scoped_knowledge_assertions_collection
from ..security.access_control import (
    can_access_concept,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from .namespace_service import resolve_canonical_namespace
from .relationship_write_service import validate_predicate_concept
from .text_relation_predicate_validation_service import (
    resolve_text_relation_predicate_for_write,
)

SCHEMA_VERSION = "scoped_knowledge_assertion.v1"
STORAGE_SURFACE = "scoped_knowledge_assertions"
SCOPE_MODES = frozenset({"user", "organisation"})
OBJECT_KINDS = frozenset({"text", "concept"})


def _clean_concept_id(value: Any, field_name: str) -> str:
    token = str(value or "").strip()
    if not token.startswith("#V#"):
        raise ValueError(f"{field_name} must be an exact #V# concept ID")
    return token


def _clean_optional_concept_id(value: Any) -> str | None:
    token = str(value or "").strip()
    return token if token.startswith("#V#") else None


def _normalise_evidence(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("evidence must be an object when provided")
    # Copy through JSON so caller-owned mutable values and BSON-hostile values
    # cannot leak into the persisted record.
    try:
        return json.loads(json.dumps(dict(value), default=str))
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence must be JSON-serialisable") from exc


def _scope_for_actor(
    *,
    scope_mode: str,
    acting_user_concept_id: Any,
    organisation_concept_id: Any,
    namespace: Any,
) -> dict[str, Any]:
    user_id = _clean_concept_id(
        acting_user_concept_id,
        "acting_user_concept_id",
    )
    org_id = _clean_optional_concept_id(organisation_concept_id)
    mode = str(scope_mode or "user").strip().lower()
    if mode not in SCOPE_MODES:
        raise ValueError("scope_mode must be 'user' or 'organisation'")
    if mode == "organisation" and not org_id:
        raise ValueError(
            "organisation scope requires a trusted organisation concept ID"
        )

    actor_namespace = resolve_canonical_namespace(
        None,
        user_id,
        org_id,
    )
    supplied_namespace = resolve_canonical_namespace(
        str(namespace or "").strip() or None,
    )
    if supplied_namespace and actor_namespace and supplied_namespace != actor_namespace:
        raise ValueError(
            "namespace does not match the trusted user and organisation scope"
        )
    audience_key = f"user:{user_id}" if mode == "user" else f"org:{org_id}"
    return {
        "mode": mode,
        "user_concept_id": user_id,
        "organisation_concept_id": org_id,
        "namespace": actor_namespace,
        "audience_keys": [audience_key],
    }


def _assertion_identity(identity: Mapping[str, Any]) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"ska_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _serialise(document: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(document, Mapping):
        return None
    result = {key: value for key, value in document.items() if key != "_id"}
    for field in ("created_at", "updated_at"):
        value = result.get(field)
        if isinstance(value, datetime):
            result[field] = value.isoformat()
    return result


def upsert_scoped_assertion(
    *,
    subject_concept_id: Any,
    predicate: Any,
    target_text: Any = None,
    target_concept_id: Any = None,
    language: Any = "en-NZ",
    scope_mode: str = "user",
    evidence: Any = None,
    acting_user_concept_id: Any,
    organisation_concept_id: Any = None,
    namespace: Any = None,
    turn_id: Any = None,
    canonical_publication: bool = False,
) -> dict[str, Any]:
    """Upsert one idempotent scoped assertion and return canonical read-back."""

    if canonical_publication is not False:
        raise ValueError("scoped assertions cannot request canonical publication")
    subject_id = _clean_concept_id(subject_concept_id, "subject_concept_id")
    if not can_access_concept(subject_id):
        raise PermissionError("subject concept is not visible to the current actor")

    has_text = isinstance(target_text, str) and bool(target_text.strip())
    has_concept = isinstance(target_concept_id, str) and bool(target_concept_id.strip())
    if has_text == has_concept:
        raise ValueError("provide exactly one of target_text or target_concept_id")

    if has_text:
        resolution = resolve_text_relation_predicate_for_write(predicate)
        storage_predicate = resolution.storage_predicate
        object_kind = "text"
        object_text = str(target_text).strip()
        language_token = str(language or "en-NZ").strip() or "en-NZ"
        object_concept_id = None
        identity_object: dict[str, Any] = {
            "text": object_text,
            "language": language_token,
        }
    else:
        storage_predicate = _clean_concept_id(predicate, "predicate")
        is_valid, error_code, error_details = validate_predicate_concept(
            storage_predicate
        )
        if not is_valid:
            raise ValueError(
                f"{error_code or 'invalid_predicate'}: {error_details or {}}"
            )
        object_kind = "concept"
        object_text = None
        language_token = None
        object_concept_id = _clean_concept_id(
            target_concept_id,
            "target_concept_id",
        )
        if not can_access_concept(object_concept_id):
            raise PermissionError("target concept is not visible to the current actor")
        identity_object = {"concept_id": object_concept_id}

    scope = _scope_for_actor(
        scope_mode=scope_mode,
        acting_user_concept_id=acting_user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    identity = {
        "scope_mode": scope["mode"],
        "audience_keys": scope["audience_keys"],
        "subject_concept_id": subject_id,
        "predicate": storage_predicate,
        "object_kind": object_kind,
        "object": identity_object,
    }
    assertion_id = _assertion_identity(identity)
    now = datetime.now(timezone.utc)
    provenance = {
        "asserted_by_user_concept_id": scope["user_concept_id"],
        "organisation_concept_id": scope["organisation_concept_id"],
        "namespace": scope["namespace"],
        "turn_id": str(turn_id or "").strip() or None,
        "capability_name": "upsert_scoped_assertion",
        "evidence": _normalise_evidence(evidence),
    }
    update_fields: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "assertion_id": assertion_id,
        "subject_concept_id": subject_id,
        "predicate": storage_predicate,
        "object_kind": object_kind,
        "scope": scope,
        "provenance": provenance,
        "canonical_publication": False,
        "status": "asserted",
        "updated_at": now,
    }
    if object_kind == "text":
        update_fields["object_text"] = {
            "text": object_text,
            "language": language_token,
        }
        update_fields["object_concept_id"] = None
    else:
        update_fields["object_concept_id"] = object_concept_id
        update_fields["object_text"] = None

    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        raise RuntimeError("scoped assertion storage is unavailable")
    existing = collection.find_one({"assertion_id": assertion_id}, {"_id": 1})
    persisted = collection.find_one_and_update(
        {"assertion_id": assertion_id},
        {
            "$set": update_fields,
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    read_back = _serialise(persisted)
    if read_back is None:
        raise RuntimeError("scoped assertion write did not produce read-back")
    return {
        "success": True,
        "effect_status": "succeeded",
        "changed": existing is None,
        "assertion_id": assertion_id,
        "assertion": read_back,
        "canonical_read_back": read_back,
        "canonical_publication": False,
        "storage_surface": STORAGE_SURFACE,
    }


def _audience_keys_for_actor(
    *,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
) -> list[str]:
    user_id = _clean_optional_concept_id(
        user_concept_id or get_effective_user_concept_id()
    )
    org_id = _clean_optional_concept_id(
        organisation_concept_id or get_effective_organisation_concept_id()
    )
    keys: list[str] = []
    if user_id:
        keys.append(f"user:{user_id}")
    if org_id:
        keys.append(f"org:{org_id}")
    return keys


def list_visible_scoped_assertions(
    *,
    subject_concept_ids: Sequence[str] | None = None,
    argument_concept_id: str | None = None,
    predicates: Sequence[str] | None = None,
    object_kind: str | None = None,
    limit: int = 200,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return scoped assertions visible to the effective or explicit actor."""

    audience_keys = _audience_keys_for_actor(
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    if not audience_keys:
        return []
    query: dict[str, Any] = {
        "scope.audience_keys": {"$in": audience_keys},
        "status": "asserted",
    }
    if subject_concept_ids:
        visible_subjects = sorted(
            {
                str(item).strip()
                for item in subject_concept_ids
                if isinstance(item, str)
                and item.strip()
                and can_access_concept(item.strip())
            }
        )
        if not visible_subjects:
            return []
        query["subject_concept_id"] = {"$in": visible_subjects}
    if argument_concept_id:
        concept_id = _clean_concept_id(argument_concept_id, "argument_concept_id")
        if not can_access_concept(concept_id):
            return []
        query["$or"] = [
            {"subject_concept_id": concept_id},
            {"object_concept_id": concept_id},
        ]
    if predicates:
        predicate_values = sorted(
            {
                str(item).strip()
                for item in predicates
                if isinstance(item, str) and item.strip()
            }
        )
        if predicate_values:
            query["predicate"] = {"$in": predicate_values}
    if object_kind is not None:
        if object_kind not in OBJECT_KINDS:
            raise ValueError("object_kind must be 'text' or 'concept'")
        query["object_kind"] = object_kind

    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        return []
    cursor = (
        collection.find(query)
        .sort([("updated_at", -1), ("assertion_id", 1)])
        .limit(max(1, min(int(limit), 1000)))
    )
    return [
        serialised
        for document in cursor
        if (serialised := _serialise(document)) is not None
    ]


def scoped_text_assertion_to_relation_row(
    assertion: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Project a scoped text assertion into the generic text-read row shape."""

    object_text = assertion.get("object_text")
    if assertion.get("object_kind") != "text" or not isinstance(object_text, Mapping):
        return None
    assertion_id = str(assertion.get("assertion_id") or "").strip()
    return {
        "subject_concept_id": assertion.get("subject_concept_id"),
        "text": object_text.get("text"),
        "lang": object_text.get("language"),
        "text_value_id": None,
        "predicate": assertion.get("predicate"),
        "relation_id": assertion_id or None,
        "assertion_id": assertion_id or None,
        "context": {
            "assertion_scope": assertion.get("scope"),
            "canonical_publication": False,
            "storage_surface": STORAGE_SURFACE,
        },
        "provenance": assertion.get("provenance") or {},
        "assertion_scope": assertion.get("scope"),
        "canonical_publication": False,
        "storage_surface": STORAGE_SURFACE,
        "text_value_created_at": assertion.get("created_at"),
        "text_value_updated_at": assertion.get("updated_at"),
        "relation_created_at": assertion.get("created_at"),
        "relation_updated_at": assertion.get("updated_at"),
    }


__all__ = [
    "list_visible_scoped_assertions",
    "scoped_text_assertion_to_relation_row",
    "upsert_scoped_assertion",
]
