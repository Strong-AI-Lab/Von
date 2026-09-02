"""Actor-scoped assertions over visible Vontology concepts.

This store deliberately does not mutate canonical concept or text-relation
documents. It lets an authenticated user or organisation assert provenance-
bearing knowledge about a concept they can see without acquiring publication
authority over that concept.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_scoped_knowledge_assertions_collection
from ..security.access_control import (
    can_access_concept,
    filter_accessible_concept_ids,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
    override_current_actor,
)
from .namespace_service import resolve_canonical_namespace
from .relationship_write_service import validate_predicate_concept
from .text_relation_predicate_validation_service import (
    predicate_concept_id_for_storage,
    resolve_text_relation_predicate_for_write,
)

SCHEMA_VERSION = "scoped_knowledge_assertion.v1"
TEXT_ASSERTION_SCHEMA_VERSION = "scoped_knowledge_assertion.v2"
STORAGE_SURFACE = "scoped_knowledge_assertions"
STANDALONE_TEXT_ASSERTION_FORM = "standalone_text"
SCOPE_MODES = frozenset({"user", "organisation"})
OBJECT_KINDS = frozenset({"text", "concept"})
EPISTEMIC_STATUSES = frozenset({"tentative", "asserted"})
MAX_PAGE_LIMIT = 1000
MAX_PAGE_SUBJECTS = 100
MAX_PAGE_CANDIDATES = 10_000
MAX_RETRACTION_HISTORY = 20
MAX_CONTEXT_ID_LENGTH = 512
MAX_SOURCE_EVENT_ID_LENGTH = 512
MAX_LINK_ROLE_LENGTH = 100
MAX_LINK_METHOD_LENGTH = 200
MAX_LINKS_PER_ASSERTION = 200
_LINK_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")


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


def _resolve_actor_context(
    *,
    acting_user_concept_id: Any,
    organisation_concept_id: Any,
    namespace: Any,
) -> dict[str, Any]:
    """Resolve explicit trusted actor values without enlarging ambient scope."""

    user_id = _clean_concept_id(
        acting_user_concept_id,
        "acting_user_concept_id",
    )
    supplied_org_id = (
        _clean_concept_id(
            organisation_concept_id,
            "organisation_concept_id",
        )
        if str(organisation_concept_id or "").strip()
        else None
    )
    ambient_user_id = _clean_optional_concept_id(get_effective_user_concept_id())
    ambient_org_id = _clean_optional_concept_id(
        get_effective_organisation_concept_id()
    )
    has_ambient_actor = bool(ambient_user_id or ambient_org_id)
    if has_ambient_actor and user_id != ambient_user_id:
        raise PermissionError(
            "acting user does not match the current trusted actor"
        )
    if (
        supplied_org_id
        and has_ambient_actor
        and supplied_org_id != ambient_org_id
    ):
        raise PermissionError(
            "organisation does not match the current trusted actor"
        )
    org_id = supplied_org_id or ambient_org_id
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
    return {
        "user_concept_id": user_id,
        "organisation_concept_id": org_id,
        "namespace": actor_namespace,
    }


def _scope_for_actor(
    *,
    scope_mode: str,
    acting_user_concept_id: Any,
    organisation_concept_id: Any,
    namespace: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve assertion scope and the trusted actor context that created it."""

    actor_context = _resolve_actor_context(
        acting_user_concept_id=acting_user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    user_id = actor_context["user_concept_id"]
    org_id = actor_context["organisation_concept_id"]
    mode = str(scope_mode or "user").strip().lower()
    if mode not in SCOPE_MODES:
        raise ValueError("scope_mode must be 'user' or 'organisation'")
    if mode == "organisation" and not org_id:
        raise ValueError(
            "organisation scope requires a trusted organisation concept ID"
        )

    audience_key = f"user:{user_id}" if mode == "user" else f"org:{org_id}"
    assertion_org_id = org_id if mode == "organisation" else None
    assertion_namespace = resolve_canonical_namespace(
        None,
        user_id,
        assertion_org_id,
    )
    scope = {
        "mode": mode,
        "user_concept_id": user_id,
        # User-scoped knowledge follows the user independently of whichever
        # organisation supplied the turn context that first established it.
        "organisation_concept_id": assertion_org_id,
        "namespace": assertion_namespace,
        "audience_key": audience_key,
        "audience_keys": [audience_key],
    }
    return scope, actor_context


def _assertion_identity(identity: Mapping[str, Any]) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"ska_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _bounded_token(value: Any, *, field_name: str, maximum: int) -> str:
    token = str(value or "").strip()
    if not token:
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(token) > maximum:
        raise ValueError(f"{field_name} is limited to {maximum} characters")
    return token


def _text_assertion_context(
    *,
    context_id: Any,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """Select an explicit context or the actor scope's implicit intake context.

    The implicit identifier is operationally stable but is not a Vontology
    concept or a claim that scope and context have the same semantics.
    """

    supplied = str(context_id or "").strip()
    if supplied:
        if len(supplied) > MAX_CONTEXT_ID_LENGTH:
            raise ValueError(
                f"context_id is limited to {MAX_CONTEXT_ID_LENGTH} characters"
            )
        return {
            "context_id": supplied,
            "selection": "explicit",
        }
    audience_keys = list(scope.get("audience_keys") or ())
    if len(audience_keys) != 1:
        raise ValueError("text assertion scope must resolve one intake audience")
    return {
        "context_id": f"intake:{audience_keys[0]}",
        "selection": "implicit",
        "kind": "intake",
    }


def _new_text_assertion_rag_index(
    *,
    now: datetime,
    namespace: str,
    revision: int,
    desired_operation: str,
) -> dict[str, Any]:
    """Materialise a durable derived-index job in the canonical assertion row."""

    return {
        "status": "pending",
        "desired_operation": desired_operation,
        "desired_revision": revision,
        "indexed_revision": None,
        "namespace": namespace,
        "attempt_count": 0,
        "next_attempt_at": now,
        "lease_token": None,
        "lease_expires_at": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error_code": None,
        "last_error": None,
    }


def _text_assertion_occurrence_identity(
    *,
    scope: Mapping[str, Any],
    context: Mapping[str, Any],
    text: str,
    language: str,
    source_event_id: Any,
    turn_id: Any,
) -> tuple[str, dict[str, str]]:
    """Return an occurrence ID without collapsing equal text across sources.

    An explicit source event is the strongest replay key. A turn-bound call is
    idempotent for the same exact text within that turn. Calls with neither get
    a fresh occurrence identity, so equal wording remains distinct knowledge.
    """

    clean_source_event_id = str(source_event_id or "").strip()
    clean_turn_id = str(turn_id or "").strip()
    if clean_source_event_id:
        if len(clean_source_event_id) > MAX_SOURCE_EVENT_ID_LENGTH:
            raise ValueError(
                "source_event_id is limited to "
                f"{MAX_SOURCE_EVENT_ID_LENGTH} characters"
            )
        occurrence = {
            "kind": "source_event",
            "value": clean_source_event_id,
        }
        identity_occurrence: Mapping[str, Any] = occurrence
    elif clean_turn_id:
        occurrence = {
            "kind": "turn_text",
            "value": clean_turn_id,
        }
        identity_occurrence = {
            **occurrence,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "language": language,
            "context_id": context["context_id"],
        }
    else:
        occurrence = {
            "kind": "generated",
            "value": str(uuid4()),
        }
        identity_occurrence = occurrence
    assertion_id = _assertion_identity(
        {
            "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
            "scope_mode": scope.get("mode"),
            "audience_keys": list(scope.get("audience_keys") or ()),
            "asserted_by_user_concept_id": scope.get("user_concept_id"),
            "occurrence": identity_occurrence,
        }
    )
    return assertion_id, occurrence


def _standalone_text_payload_matches(
    assertion: Mapping[str, Any],
    *,
    text: str,
    language: str,
    context: Mapping[str, Any],
) -> bool:
    object_text = assertion.get("object_text")
    return bool(
        assertion.get("assertion_form") == STANDALONE_TEXT_ASSERTION_FORM
        and isinstance(object_text, Mapping)
        and object_text.get("text") == text
        and object_text.get("language") == language
        and assertion.get("assertion_context") == context
    )


def _json_safe_value(value: Any, *, path: str) -> Any:
    """Recursively convert supported BSON values without hiding bad data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            result[key] = _json_safe_value(
                nested_value,
                path=f"{path}.{key}",
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_safe_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(
        f"{path} contains unsupported value type {type(value).__name__}"
    )


def _serialise(document: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(document, Mapping):
        return None
    serialised = _json_safe_value(
        {key: value for key, value in document.items() if key != "_id"},
        path="assertion",
    )
    if not isinstance(serialised, dict):
        raise TypeError("assertion serialisation did not produce an object")
    return serialised


def store_text_assertion(
    *,
    text: Any,
    language: Any = "en-NZ",
    scope_mode: str = "user",
    context_id: Any = None,
    source_event_id: Any = None,
    evidence: Any = None,
    acting_user_concept_id: Any,
    organisation_concept_id: Any = None,
    namespace: Any = None,
    turn_id: Any = None,
    canonical_publication: bool = False,
) -> dict[str, Any]:
    """Store one exact natural-language assertion before semantic analysis.

    No subject, predicate, concept link, or successful RAG operation is needed
    for admission. The returned canonical read-back is the write receipt; the
    embedded ``rag_index`` state is a durable, independently retryable job.
    """

    if canonical_publication is not False:
        raise ValueError("text assertions cannot request canonical publication")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    exact_text = text
    language_token = _bounded_token(
        language or "en-NZ",
        field_name="language",
        maximum=64,
    )
    scope, actor_context = _scope_for_actor(
        scope_mode=scope_mode,
        acting_user_concept_id=acting_user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    assertion_context = _text_assertion_context(
        context_id=context_id,
        scope=scope,
    )
    assertion_id, occurrence_identity = _text_assertion_occurrence_identity(
        scope=scope,
        context=assertion_context,
        text=exact_text,
        language=language_token,
        source_event_id=source_event_id,
        turn_id=turn_id,
    )
    now = datetime.now(timezone.utc)
    revision = 1
    provenance = {
        "asserted_by_user_concept_id": scope["user_concept_id"],
        "organisation_concept_id": actor_context["organisation_concept_id"],
        "namespace": actor_context["namespace"],
        "turn_id": str(turn_id or "").strip() or None,
        "source_event_id": str(source_event_id or "").strip() or None,
        "capability_name": "store_text_assertion",
        "evidence": _normalise_evidence(evidence),
    }
    insert_fields: dict[str, Any] = {
        "_id": assertion_id,
        "schema_version": TEXT_ASSERTION_SCHEMA_VERSION,
        "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
        "assertion_id": assertion_id,
        "assertion_revision": revision,
        "occurrence_identity": occurrence_identity,
        # These nulls keep old generic projections honest without treating a
        # standalone assertion as a malformed subject-predicate relation.
        "subject_concept_id": None,
        "predicate": None,
        "object_kind": "text",
        "object_text": {
            "text": exact_text,
            "language": language_token,
        },
        "object_concept_id": None,
        "concept_links": [],
        "assertion_context": assertion_context,
        "scope": scope,
        "canonical_publication": False,
        "status": "asserted",
        "created_at": now,
        "updated_at": now,
        "provenance": provenance,
        "rag_index": _new_text_assertion_rag_index(
            now=now,
            namespace=str(actor_context["namespace"]),
            revision=revision,
            desired_operation="upsert",
        ),
    }
    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        raise RuntimeError("text assertion storage is unavailable")
    try:
        previous = collection.find_one_and_update(
            {"assertion_id": assertion_id},
            {"$setOnInsert": insert_fields},
            upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
    except DuplicateKeyError:
        previous = collection.find_one({"assertion_id": assertion_id})
        if previous is None:
            raise
    created = previous is None
    persisted = collection.find_one({"assertion_id": assertion_id})
    if not isinstance(persisted, Mapping):
        raise RuntimeError("text assertion write did not produce read-back")
    if not _standalone_text_payload_matches(
        persisted,
        text=exact_text,
        language=language_token,
        context=assertion_context,
    ):
        raise ValueError(
            "the occurrence identity already identifies a different text assertion"
        )
    reactivated = None
    if persisted.get("status") == "retracted":
        current_revision = int(persisted.get("assertion_revision") or 1)
        rag_index = persisted.get("rag_index")
        rag_index = rag_index if isinstance(rag_index, Mapping) else {}
        index_namespace = str(
            rag_index.get("namespace") or actor_context["namespace"] or ""
        ).strip()
        retraction_history_item = {
            "retracted_at": persisted.get("retracted_at"),
            "retraction_provenance": persisted.get("retraction_provenance"),
        }
        reassertion_provenance = {
            "reasserted_by_user_concept_id": actor_context["user_concept_id"],
            "organisation_concept_id": actor_context[
                "organisation_concept_id"
            ],
            "namespace": actor_context["namespace"],
            "turn_id": str(turn_id or "").strip() or None,
            "source_event_id": str(source_event_id or "").strip() or None,
            "capability_name": "store_text_assertion",
            "evidence": _normalise_evidence(evidence),
        }
        reactivated = collection.find_one_and_update(
            {
                "assertion_id": assertion_id,
                "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
                "assertion_revision": current_revision,
                "status": "retracted",
            },
            {
                "$set": {
                    "status": "asserted",
                    "updated_at": now,
                    "reasserted_at": now,
                    "reassertion_provenance": reassertion_provenance,
                    "rag_index": _new_text_assertion_rag_index(
                        now=now,
                        namespace=index_namespace,
                        revision=current_revision + 1,
                        desired_operation="upsert",
                    ),
                },
                "$inc": {"assertion_revision": 1},
                "$unset": {
                    "retracted_at": "",
                    "retraction_provenance": "",
                },
                "$push": {
                    "retraction_history": {
                        "$each": [retraction_history_item],
                        "$slice": -MAX_RETRACTION_HISTORY,
                    }
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        persisted = reactivated or collection.find_one(
            {"assertion_id": assertion_id}
        )
    read_back = _serialise(persisted)
    if read_back is None:
        raise RuntimeError("text assertion write did not produce read-back")
    return {
        "success": True,
        "effect_status": "succeeded",
        "changed": bool(created or reactivated is not None),
        "assertion_id": assertion_id,
        "assertion": read_back,
        "canonical_read_back": read_back,
        "canonical_publication": False,
        "storage_surface": STORAGE_SURFACE,
        "derived_maintenance": {
            "durable": True,
            "rag_index": read_back.get("rag_index"),
        },
    }


def _normalise_text_assertion_link(
    raw_link: Any,
    *,
    assertion_id: str,
    assertion_text: str,
    actor_context: Mapping[str, Any],
    turn_id: Any,
    now: datetime,
) -> dict[str, Any]:
    if not isinstance(raw_link, Mapping):
        raise ValueError("each concept link must be an object")
    concept_id = _clean_concept_id(raw_link.get("concept_id"), "concept_id")
    if not can_access_concept(concept_id):
        raise PermissionError("linked concept is not visible to the current actor")
    role = _bounded_token(
        raw_link.get("role") or "mentions",
        field_name="role",
        maximum=MAX_LINK_ROLE_LENGTH,
    )
    if not _LINK_TOKEN_RE.fullmatch(role):
        raise ValueError(
            "role must start with a letter and contain only letters, digits, "
            "underscore, dot, colon, or hyphen"
        )
    method = _bounded_token(
        raw_link.get("method") or "unspecified",
        field_name="method",
        maximum=MAX_LINK_METHOD_LENGTH,
    )
    confidence_value = raw_link.get("confidence")
    confidence: float | None = None
    if confidence_value is not None:
        if isinstance(confidence_value, bool):
            raise ValueError("confidence must be a number from 0 to 1")
        try:
            confidence = float(confidence_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be a number from 0 to 1") from exc
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence must be a number from 0 to 1")

    raw_spans = raw_link.get("spans")
    if raw_spans is None:
        raw_spans = []
    if not isinstance(raw_spans, Sequence) or isinstance(raw_spans, (str, bytes)):
        raise ValueError("spans must be an array of {start, end} objects")
    spans: list[dict[str, int]] = []
    for raw_span in raw_spans:
        if not isinstance(raw_span, Mapping):
            raise ValueError("each span must be an object")
        start = raw_span.get("start")
        end = raw_span.get("end")
        if isinstance(start, bool) or isinstance(end, bool):
            raise ValueError("span start and end must be integers")
        try:
            start_value = int(start)
            end_value = int(end)
        except (TypeError, ValueError) as exc:
            raise ValueError("span start and end must be integers") from exc
        if start_value < 0 or end_value <= start_value or end_value > len(
            assertion_text
        ):
            raise ValueError(
                "span offsets must identify a non-empty range within the exact text"
            )
        spans.append({"start": start_value, "end": end_value})
    spans.sort(key=lambda span: (span["start"], span["end"]))
    link_identity = {
        "assertion_id": assertion_id,
        "concept_id": concept_id,
        "role": role,
        "spans": spans,
        "method": method,
    }
    encoded = json.dumps(link_identity, sort_keys=True, separators=(",", ":"))
    link_id = f"skl_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"
    return {
        "link_id": link_id,
        "concept_id": concept_id,
        "role": role,
        "spans": spans,
        "method": method,
        "confidence": confidence,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "provenance": {
            "linked_by_user_concept_id": actor_context["user_concept_id"],
            "organisation_concept_id": actor_context[
                "organisation_concept_id"
            ],
            "namespace": actor_context["namespace"],
            "turn_id": str(turn_id or "").strip() or None,
            "capability_name": "add_text_assertion_concept_links",
            "evidence": _normalise_evidence(raw_link.get("evidence")),
        },
    }


def add_text_assertion_concept_links(
    *,
    assertion_id: Any,
    links: Any,
    acting_user_concept_id: Any,
    organisation_concept_id: Any = None,
    namespace: Any = None,
    turn_id: Any = None,
) -> dict[str, Any]:
    """Add optional, qualified concept links without changing asserted text."""

    assertion_token = str(assertion_id or "").strip()
    if not assertion_token.startswith("ska_"):
        raise ValueError("assertion_id must be an exact scoped assertion ID")
    if not isinstance(links, Sequence) or isinstance(links, (str, bytes)):
        raise ValueError("links must be an array")
    if not links:
        raise ValueError("links must contain at least one concept link")
    if len(links) > MAX_LINKS_PER_ASSERTION:
        raise ValueError(
            f"links is limited to {MAX_LINKS_PER_ASSERTION} concept links"
        )
    actor_context = _resolve_actor_context(
        acting_user_concept_id=acting_user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    user_id = actor_context["user_concept_id"]
    org_id = actor_context["organisation_concept_id"]
    scope_clauses: list[dict[str, Any]] = [{"scope.mode": "user"}]
    if org_id:
        scope_clauses.append(
            {
                "scope.mode": "organisation",
                "scope.organisation_concept_id": org_id,
            }
        )
    authority_filter: dict[str, Any] = {
        "assertion_id": assertion_token,
        "assertion_form": STANDALONE_TEXT_ASSERTION_FORM,
        "status": "asserted",
        "provenance.asserted_by_user_concept_id": user_id,
        "$or": scope_clauses,
    }
    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        raise RuntimeError("text assertion storage is unavailable")

    for _attempt in range(5):
        current = collection.find_one(authority_filter)
        if not isinstance(current, Mapping):
            raise PermissionError(
                "text assertion is unavailable to the current actor"
            )
        object_text = current.get("object_text")
        exact_text = (
            object_text.get("text") if isinstance(object_text, Mapping) else None
        )
        if not isinstance(exact_text, str):
            raise RuntimeError("text assertion has no canonical text body")
        now = datetime.now(timezone.utc)
        with override_current_actor(user_id, org_id):
            requested_links = [
                _normalise_text_assertion_link(
                    raw_link,
                    assertion_id=assertion_token,
                    assertion_text=exact_text,
                    actor_context=actor_context,
                    turn_id=turn_id,
                    now=now,
                )
                for raw_link in links
            ]
        existing_links = [
            dict(link)
            for link in current.get("concept_links") or ()
            if isinstance(link, Mapping)
        ]
        existing_ids = {
            str(link.get("link_id") or "").strip() for link in existing_links
        }
        additions = [
            link for link in requested_links if link["link_id"] not in existing_ids
        ]
        if not additions:
            read_back = _serialise(current)
            if read_back is None:
                raise RuntimeError("concept-link replay did not produce read-back")
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": False,
                "assertion_id": assertion_token,
                "links_added": 0,
                "assertion": read_back,
                "canonical_read_back": read_back,
                "canonical_publication": False,
                "storage_surface": STORAGE_SURFACE,
            }
        merged_links = [*existing_links, *additions]
        if len(merged_links) > MAX_LINKS_PER_ASSERTION:
            raise ValueError(
                "the assertion would exceed the bounded concept-link limit of "
                f"{MAX_LINKS_PER_ASSERTION}"
            )
        current_revision = int(current.get("assertion_revision") or 1)
        next_revision = current_revision + 1
        scope = current.get("scope")
        scope = scope if isinstance(scope, Mapping) else {}
        index_namespace = str(
            (
                current.get("rag_index", {}).get("namespace")
                if isinstance(current.get("rag_index"), Mapping)
                else None
            )
            or actor_context["namespace"]
            or ""
        ).strip()
        updated = collection.find_one_and_update(
            {
                **authority_filter,
                "assertion_revision": current_revision,
            },
            {
                "$set": {
                    "concept_links": merged_links,
                    "assertion_revision": next_revision,
                    "updated_at": now,
                    "rag_index": _new_text_assertion_rag_index(
                        now=now,
                        namespace=index_namespace,
                        revision=next_revision,
                        desired_operation="upsert",
                    ),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if isinstance(updated, Mapping):
            read_back = _serialise(updated)
            if read_back is None:
                raise RuntimeError("concept-link write did not produce read-back")
            return {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "assertion_id": assertion_token,
                "links_added": len(additions),
                "assertion": read_back,
                "canonical_read_back": read_back,
                "canonical_publication": False,
                "storage_surface": STORAGE_SURFACE,
                "derived_maintenance": {
                    "durable": True,
                    "rag_index": read_back.get("rag_index"),
                },
            }
    raise RuntimeError("text assertion changed concurrently; retry the link operation")


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
    epistemic_status: str = "asserted",
) -> dict[str, Any]:
    """Upsert one idempotent scoped assertion and return canonical read-back."""

    if canonical_publication is not False:
        raise ValueError("scoped assertions cannot request canonical publication")
    requested_epistemic_status = str(epistemic_status or "").strip().lower()
    if requested_epistemic_status not in EPISTEMIC_STATUSES:
        raise ValueError("epistemic_status must be tentative or asserted")
    scope, actor_context = _scope_for_actor(
        scope_mode=scope_mode,
        acting_user_concept_id=acting_user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    with override_current_actor(
        actor_context["user_concept_id"],
        actor_context["organisation_concept_id"],
    ):
        subject_id = _clean_concept_id(
            subject_concept_id,
            "subject_concept_id",
        )
        if not can_access_concept(subject_id):
            raise PermissionError(
                "subject concept is not visible to the current actor"
            )

        has_text = isinstance(target_text, str) and bool(target_text.strip())
        has_concept = isinstance(target_concept_id, str) and bool(
            target_concept_id.strip()
        )
        if has_text == has_concept:
            raise ValueError(
                "provide exactly one of target_text or target_concept_id"
            )

        if has_text:
            requested_predicate_id = predicate_concept_id_for_storage(
                predicate
            )
            if requested_predicate_id and not can_access_concept(
                requested_predicate_id
            ):
                raise PermissionError(
                    "predicate concept is not visible to the current actor"
                )
            resolution = resolve_text_relation_predicate_for_write(predicate)
            storage_predicate = resolution.storage_predicate
            if not can_access_concept(resolution.predicate_concept_id):
                raise PermissionError(
                    "predicate concept is not visible to the current actor"
                )
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
            # Visibility is an authority boundary. Do not let predicate
            # validation reveal or inspect a concept the actor cannot see.
            if not can_access_concept(storage_predicate):
                raise PermissionError(
                    "predicate concept is not visible to the current actor"
                )
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
                raise PermissionError(
                    "target concept is not visible to the current actor"
                )
            identity_object = {"concept_id": object_concept_id}

    identity = {
        "scope_mode": scope["mode"],
        "audience_keys": scope["audience_keys"],
        # Organisation audience is not assertion authorship. Keeping the
        # trusted author in the identity prevents one member from replacing
        # another member's provenance for the same organisation-visible claim.
        "asserted_by_user_concept_id": scope["user_concept_id"],
        "subject_concept_id": subject_id,
        "predicate": storage_predicate,
        "object_kind": object_kind,
        "object": identity_object,
    }
    assertion_id = _assertion_identity(identity)
    now = datetime.now(timezone.utc)
    provenance = {
        "asserted_by_user_concept_id": scope["user_concept_id"],
        "organisation_concept_id": actor_context["organisation_concept_id"],
        "namespace": actor_context["namespace"],
        "turn_id": str(turn_id or "").strip() or None,
        "capability_name": "upsert_scoped_assertion",
        "evidence": _normalise_evidence(evidence),
    }
    insert_fields: dict[str, Any] = {
        # Mongo's built-in _id uniqueness is the final concurrency boundary
        # even if creation of the secondary assertion_id index was temporarily
        # unavailable at process startup.
        "_id": assertion_id,
        "schema_version": SCHEMA_VERSION,
        "assertion_id": assertion_id,
        "assertion_revision": 1,
        "subject_concept_id": subject_id,
        "predicate": storage_predicate,
        "object_kind": object_kind,
        "scope": scope,
        "canonical_publication": False,
        "status": "asserted",
        "epistemic_status": requested_epistemic_status,
        "created_at": now,
        "updated_at": now,
        "provenance": provenance,
    }
    if object_kind == "text":
        insert_fields["object_text"] = {
            "text": object_text,
            "language": language_token,
        }
        insert_fields["object_concept_id"] = None
    else:
        insert_fields["object_concept_id"] = object_concept_id
        insert_fields["object_text"] = None

    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        raise RuntimeError("scoped assertion storage is unavailable")
    try:
        previous = collection.find_one_and_update(
            {"assertion_id": assertion_id},
            {"$setOnInsert": insert_fields},
            upsert=True,
            # BEFORE is None only for the operation that atomically created the
            # assertion. A separate pre-read cannot provide this
            # concurrency-safe changed result.
            return_document=ReturnDocument.BEFORE,
        )
    except DuplicateKeyError:
        # A concurrent first writer may win on the deterministic built-in _id
        # before this unindexed assertion_id query observes it. That is an
        # idempotent replay, not a failed or indeterminate effect.
        previous = collection.find_one({"assertion_id": assertion_id})
        if previous is None:
            raise
    created = previous is None
    promoted = None
    if (
        isinstance(previous, Mapping)
        and previous.get("status") == "asserted"
        and previous.get("epistemic_status") == "tentative"
        and requested_epistemic_status == "asserted"
    ):
        # Epistemic status is monotonic on an active assertion. A normal
        # asserted upsert must not silently replay an older tentative candidate;
        # exactly one concurrent caller promotes it and emits the asserted event.
        promoted = collection.find_one_and_update(
            {
                "assertion_id": assertion_id,
                "status": "asserted",
                "epistemic_status": "tentative",
            },
            {
                "$set": {
                    "epistemic_status": "asserted",
                    "updated_at": now,
                    "epistemic_promotion": {
                        "promoted_at": now,
                        "promoted_by_user_concept_id": actor_context[
                            "user_concept_id"
                        ],
                        "turn_id": str(turn_id or "").strip() or None,
                        "capability_name": "upsert_scoped_assertion",
                        "reason": "asserted_upsert",
                    },
                },
                "$inc": {"assertion_revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
    reactivated = None
    if isinstance(previous, Mapping) and previous.get("status") == "retracted":
        retraction_history_item = {
            "retracted_at": previous.get("retracted_at"),
            "retraction_provenance": previous.get("retraction_provenance"),
        }
        reassertion_provenance = {
            "reasserted_by_user_concept_id": actor_context["user_concept_id"],
            "organisation_concept_id": actor_context[
                "organisation_concept_id"
            ],
            "namespace": actor_context["namespace"],
            "turn_id": str(turn_id or "").strip() or None,
            "capability_name": "upsert_scoped_assertion",
            "evidence": _normalise_evidence(evidence),
        }
        # Compare-and-set makes exactly one concurrent replay reactivate the
        # tombstone. Original assertion provenance and logical scope remain
        # immutable; a bounded history preserves the retraction being undone.
        reactivation_set: dict[str, Any] = {
            "status": "asserted",
            "epistemic_status": requested_epistemic_status,
            "updated_at": now,
            "reasserted_at": now,
            "reassertion_provenance": reassertion_provenance,
        }
        reactivated = collection.find_one_and_update(
            {
                "assertion_id": assertion_id,
                "status": "retracted",
            },
            {
                "$set": reactivation_set,
                "$inc": {"assertion_revision": 1},
                "$unset": {
                    "retracted_at": "",
                    "retraction_provenance": "",
                },
                "$push": {
                    "retraction_history": {
                        "$each": [retraction_history_item],
                        "$slice": -MAX_RETRACTION_HISTORY,
                    }
                },
            },
            return_document=ReturnDocument.AFTER,
        )
    persisted = promoted or reactivated or collection.find_one(
        {"assertion_id": assertion_id}
    )
    read_back = _serialise(persisted)
    if read_back is None:
        raise RuntimeError("scoped assertion write did not produce read-back")
    changed = bool(created or promoted is not None or reactivated is not None)
    result = {
        "success": True,
        "effect_status": "succeeded",
        "changed": changed,
        "assertion_id": assertion_id,
        "assertion": read_back,
        "canonical_read_back": read_back,
        "canonical_publication": False,
        "storage_surface": STORAGE_SURFACE,
        "epistemic_status": read_back.get("epistemic_status") or "asserted",
    }
    if changed and (read_back.get("epistemic_status") or "asserted") == "asserted":
        # Scoped knowledge is a first-class mutation surface. Emit the same
        # actor-bound, best-effort event shape used by canonical Vontology
        # writes so represented workflows can react without task-specific
        # polling or database coupling. Do not put private assertion content in
        # the event envelope; an authorised consumer can read it canonically.
        try:
            from .workflow_event_integration_service import (
                EVENT_TYPE_SCOPED_ASSERTION_UPSERTED,
                maybe_launch_vontology_mutation_workflow,
            )

            event_payload = {
                "assertion_id": assertion_id,
                "subject_concept_id": subject_id,
                "predicate": storage_predicate,
                "object_kind": object_kind,
                "scope_mode": scope["mode"],
                "epistemic_status": "asserted",
                "canonical_publication": False,
            }
            if promoted is not None:
                event_payload["promoted_from_epistemic_status"] = "tentative"
            maybe_launch_vontology_mutation_workflow(
                mutation_event_type=EVENT_TYPE_SCOPED_ASSERTION_UPSERTED,
                mutation_id=assertion_id,
                user_id=actor_context["user_concept_id"],
                org_id=actor_context["organisation_concept_id"],
                namespace=actor_context["namespace"],
                event_payload=event_payload,
                inputs=dict(event_payload),
            )
        except Exception:
            # The durable assertion and canonical read-back are authoritative;
            # event fan-out is recoverable and must not turn that write into an
            # ambiguous failure.
            pass
    return result


def promote_scoped_assertion_epistemic_status(
    *,
    assertion_id: Any,
    acting_user_concept_id: Any,
    organisation_concept_id: Any = None,
    namespace: Any = None,
    request_id: Any = None,
) -> dict[str, Any]:
    """Idempotently promote an actor-owned tentative assertion to asserted."""

    assertion_token = str(assertion_id or "").strip()
    if not assertion_token.startswith("ska_"):
        raise ValueError("assertion_id must be an exact scoped assertion ID")
    actor_context = _resolve_actor_context(
        acting_user_concept_id=acting_user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    user_id = actor_context["user_concept_id"]
    org_id = actor_context["organisation_concept_id"]
    scope_clauses: list[dict[str, Any]] = [{"scope.mode": "user"}]
    if org_id:
        scope_clauses.append(
            {
                "scope.mode": "organisation",
                "scope.organisation_concept_id": org_id,
            }
        )
    authority_filter: dict[str, Any] = {
        "assertion_id": assertion_token,
        "provenance.asserted_by_user_concept_id": user_id,
        "status": "asserted",
        "$or": scope_clauses,
    }
    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        raise RuntimeError("scoped assertion storage is unavailable")
    now = datetime.now(timezone.utc)
    promoted = collection.find_one_and_update(
        {
            **authority_filter,
            "epistemic_status": "tentative",
        },
        {
            "$set": {
                "epistemic_status": "asserted",
                "updated_at": now,
                "epistemic_confirmation": {
                    "confirmed_at": now,
                    "confirmed_by_user_concept_id": user_id,
                    "request_id": str(request_id or "").strip() or None,
                    "capability_name": "promote_scoped_assertion_epistemic_status",
                },
            },
            "$inc": {"assertion_revision": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    changed = promoted is not None
    persisted = promoted or collection.find_one(authority_filter)
    if not isinstance(persisted, Mapping):
        raise PermissionError("scoped assertion is unavailable to the current actor")
    # Assertions created before epistemic status existed are asserted by default.
    stored_epistemic_status = persisted.get("epistemic_status") or "asserted"
    if stored_epistemic_status != "asserted":
        raise RuntimeError("scoped assertion epistemic promotion did not read back")
    read_back = _serialise(persisted)
    if read_back is None:
        raise RuntimeError("scoped assertion promotion did not produce read-back")
    if changed:
        try:
            from .workflow_event_integration_service import (
                EVENT_TYPE_SCOPED_ASSERTION_UPSERTED,
                maybe_launch_vontology_mutation_workflow,
            )

            event_payload = {
                "assertion_id": assertion_token,
                "subject_concept_id": read_back.get("subject_concept_id"),
                "predicate": read_back.get("predicate"),
                "object_kind": read_back.get("object_kind"),
                "scope_mode": (read_back.get("scope") or {}).get("mode"),
                "epistemic_status": "asserted",
                "canonical_publication": False,
                "promoted_from_epistemic_status": "tentative",
            }
            maybe_launch_vontology_mutation_workflow(
                mutation_event_type=EVENT_TYPE_SCOPED_ASSERTION_UPSERTED,
                mutation_id=assertion_token,
                user_id=user_id,
                org_id=org_id,
                namespace=actor_context["namespace"],
                event_payload=event_payload,
                inputs=dict(event_payload),
            )
        except Exception:
            pass
    return {
        "success": True,
        "effect_status": "succeeded",
        "changed": changed,
        "assertion_id": assertion_token,
        "epistemic_status": "asserted",
        "assertion": read_back,
        "canonical_read_back": read_back,
        "canonical_publication": False,
        "storage_surface": STORAGE_SURFACE,
    }


def retract_scoped_assertion(
    *,
    assertion_id: Any,
    acting_user_concept_id: Any,
    organisation_concept_id: Any = None,
    namespace: Any = None,
) -> dict[str, Any]:
    """Idempotently retract an assertion under its original author's authority.

    This recovery operation deliberately does not require current visibility of
    the subject, predicate, or object: losing visibility must not strand an
    assertion that the original author remains authorised to withdraw.
    """

    assertion_token = str(assertion_id or "").strip()
    if not assertion_token.startswith("ska_"):
        raise ValueError("assertion_id must be an exact scoped assertion ID")
    actor_context = _resolve_actor_context(
        acting_user_concept_id=acting_user_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    user_id = actor_context["user_concept_id"]
    org_id = actor_context["organisation_concept_id"]
    scope_clauses: list[dict[str, Any]] = [{"scope.mode": "user"}]
    if org_id:
        scope_clauses.append(
            {
                "scope.mode": "organisation",
                "scope.organisation_concept_id": org_id,
            }
        )
    authority_filter: dict[str, Any] = {
        "assertion_id": assertion_token,
        "provenance.asserted_by_user_concept_id": user_id,
        "$or": scope_clauses,
    }
    now = datetime.now(timezone.utc)
    retraction_provenance = {
        "retracted_by_user_concept_id": user_id,
        "organisation_concept_id": org_id,
        "namespace": actor_context["namespace"],
        "capability_name": "retract_scoped_assertion",
    }

    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        raise RuntimeError("scoped assertion storage is unavailable")
    retract_filter = {
        **authority_filter,
        "status": "asserted",
    }
    current = collection.find_one(
        retract_filter,
        {
            "_id": 0,
            "assertion_form": 1,
            "epistemic_status": 1,
        },
    )
    update: dict[str, Any] = {
        "$set": {
            "status": "retracted",
            "retracted_at": now,
            "updated_at": now,
            "retraction_provenance": retraction_provenance,
        },
        "$inc": {"assertion_revision": 1},
    }
    if (
        isinstance(current, Mapping)
        and current.get("assertion_form") == STANDALONE_TEXT_ASSERTION_FORM
    ):
        update["$set"].update(
            {
                "rag_index.status": "pending",
                "rag_index.desired_operation": "delete",
                "rag_index.next_attempt_at": now,
                "rag_index.lease_token": None,
                "rag_index.lease_expires_at": None,
                "rag_index.last_error_code": None,
                "rag_index.last_error": None,
            }
        )
        update["$inc"]["rag_index.desired_revision"] = 1
        update["$unset"] = {
            "rag_index.indexed_revision": "",
            "rag_index.indexed_at": "",
        }
    persisted = (
        collection.find_one_and_update(
            retract_filter,
            update,
            return_document=ReturnDocument.AFTER,
        )
        if isinstance(current, Mapping)
        else None
    )
    changed = persisted is not None
    if persisted is None:
        # This authority-bearing filter is intentionally reused for idempotent
        # read-back. A missing or differently owned assertion yields the same
        # generic denial and does not disclose whether the identifier exists.
        persisted = collection.find_one(authority_filter)
        if persisted is None:
            raise PermissionError(
                "scoped assertion is unavailable to the current actor"
            )
    read_back = _serialise(persisted)
    if read_back is None:
        raise RuntimeError("scoped assertion retraction did not produce read-back")
    receipt = {
        "success": True,
        "effect_status": "succeeded",
        "changed": changed,
        "assertion_id": assertion_token,
        "assertion": read_back,
        "canonical_read_back": read_back,
        "canonical_publication": False,
        "storage_surface": STORAGE_SURFACE,
    }
    if read_back.get("assertion_form") == STANDALONE_TEXT_ASSERTION_FORM:
        receipt["derived_maintenance"] = {
            "durable": True,
            "rag_index": read_back.get("rag_index"),
        }
    current_epistemic_status = (
        current.get("epistemic_status") or "asserted"
        if isinstance(current, Mapping)
        else None
    )
    if (
        changed
        and current_epistemic_status == "asserted"
        and read_back.get("assertion_form") != STANDALONE_TEXT_ASSERTION_FORM
    ):
        try:
            from .workflow_event_integration_service import (
                EVENT_TYPE_SCOPED_ASSERTION_RETRACTED,
                maybe_launch_vontology_mutation_workflow,
            )

            event_payload = {
                "assertion_id": assertion_token,
                "subject_concept_id": read_back.get("subject_concept_id"),
                "predicate": read_back.get("predicate"),
                "object_kind": read_back.get("object_kind"),
                "scope_mode": (read_back.get("scope") or {}).get("mode"),
                "epistemic_status": "asserted",
                "canonical_publication": False,
            }
            maybe_launch_vontology_mutation_workflow(
                mutation_event_type=EVENT_TYPE_SCOPED_ASSERTION_RETRACTED,
                mutation_id=assertion_token,
                user_id=user_id,
                org_id=org_id,
                namespace=actor_context["namespace"],
                event_payload=event_payload,
                inputs=dict(event_payload),
            )
        except Exception:
            pass
    return receipt


def _resolve_read_actor(
    *,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve an explicit trusted actor without contradicting ambient scope."""

    ambient_user_id = _clean_optional_concept_id(get_effective_user_concept_id())
    ambient_org_id = _clean_optional_concept_id(
        get_effective_organisation_concept_id()
    )
    has_ambient_actor = bool(ambient_user_id or ambient_org_id)

    explicit_user_id = (
        _clean_concept_id(user_concept_id, "user_concept_id")
        if str(user_concept_id or "").strip()
        else None
    )
    explicit_org_id = (
        _clean_concept_id(
            organisation_concept_id,
            "organisation_concept_id",
        )
        if str(organisation_concept_id or "").strip()
        else None
    )
    if (
        explicit_user_id
        and has_ambient_actor
        and explicit_user_id != ambient_user_id
    ):
        raise PermissionError(
            "explicit user audience does not match the current trusted actor"
        )
    if explicit_org_id and has_ambient_actor and explicit_org_id != ambient_org_id:
        raise PermissionError(
            "explicit organisation audience does not match the current trusted actor"
        )

    return (
        explicit_user_id or ambient_user_id,
        explicit_org_id or ambient_org_id,
    )


def _audience_keys_for_actor(
    *,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
) -> list[str]:
    keys: list[str] = []
    if user_concept_id:
        keys.append(f"user:{user_concept_id}")
    if organisation_concept_id:
        keys.append(f"org:{organisation_concept_id}")
    return keys


def _normalise_subject_ids(
    subject_concept_ids: Sequence[str] | None,
) -> list[str]:
    ordered_subjects: list[str] = []
    seen: set[str] = set()
    for item in subject_concept_ids or ():
        if not isinstance(item, str) or not item.strip():
            continue
        subject_id = _clean_concept_id(item, "subject_concept_ids item")
        if subject_id in seen:
            continue
        seen.add(subject_id)
        ordered_subjects.append(subject_id)
    if len(ordered_subjects) > MAX_PAGE_SUBJECTS:
        raise ValueError(
            f"subject_concept_ids is limited to {MAX_PAGE_SUBJECTS} concepts"
        )
    return ordered_subjects


def _assertion_visibility_concept_ids(
    assertion: Mapping[str, Any],
) -> tuple[str, ...] | None:
    """Return every concept whose current visibility is required for one row."""

    if assertion.get("assertion_form") == STANDALONE_TEXT_ASSERTION_FORM:
        object_text = assertion.get("object_text")
        if (
            assertion.get("object_kind") != "text"
            or not isinstance(object_text, Mapping)
            or not isinstance(object_text.get("text"), str)
            or not object_text.get("text", "").strip()
        ):
            return None
        return ()
    subject_id = _clean_optional_concept_id(assertion.get("subject_concept_id"))
    predicate_id = predicate_concept_id_for_storage(assertion.get("predicate"))
    if not subject_id or not predicate_id:
        return None
    if assertion.get("object_kind") != "concept":
        return (subject_id, predicate_id)
    object_concept_id = _clean_optional_concept_id(
        assertion.get("object_concept_id")
    )
    if not object_concept_id:
        return None
    return (subject_id, predicate_id, object_concept_id)


def _visible_assertion_documents(
    documents: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Batch visibility and redact optional links without hiding their row."""

    candidates: list[tuple[Mapping[str, Any], tuple[str, ...]]] = []
    required_concept_ids: set[str] = set()
    optional_link_concept_ids: set[str] = set()
    for document in documents:
        required_ids = _assertion_visibility_concept_ids(document)
        if required_ids is None:
            continue
        candidates.append((document, required_ids))
        required_concept_ids.update(required_ids)
        if document.get("assertion_form") == STANDALONE_TEXT_ASSERTION_FORM:
            for link in document.get("concept_links") or ():
                if not isinstance(link, Mapping) or link.get("status") != "active":
                    continue
                concept_id = _clean_optional_concept_id(link.get("concept_id"))
                if concept_id:
                    optional_link_concept_ids.add(concept_id)
    all_concept_ids = required_concept_ids | optional_link_concept_ids
    try:
        visible_ids = (
            filter_accessible_concept_ids(all_concept_ids)
            if all_concept_ids
            else set()
        )
    except Exception:
        # Optional enrichment must never become an admission or raw-retrieval
        # dependency. If only link visibility failed, keep the assertion and
        # redact every optional link. Required legacy relation concepts remain
        # fail-closed through a separate visibility check.
        visible_ids = (
            filter_accessible_concept_ids(required_concept_ids)
            if required_concept_ids
            else set()
        )
    visible_documents: list[Mapping[str, Any]] = []
    for document, required_ids in candidates:
        if not all(concept_id in visible_ids for concept_id in required_ids):
            continue
        if document.get("assertion_form") != STANDALONE_TEXT_ASSERTION_FORM:
            visible_documents.append(document)
            continue
        projected = dict(document)
        projected["concept_links"] = [
            dict(link)
            for link in document.get("concept_links") or ()
            if isinstance(link, Mapping)
            and link.get("status") == "active"
            and _clean_optional_concept_id(link.get("concept_id")) in visible_ids
        ]
        visible_documents.append(projected)
    return visible_documents


def _normalise_page_bound(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return min(parsed, maximum)


def _build_assertion_query(
    *,
    audience_keys: Sequence[str],
    subject_concept_ids: Sequence[str],
    argument_concept_id: str | None,
    predicates: Sequence[str] | None,
    object_kind: str | None,
    languages: Sequence[str] | None,
    assertion_form: str | None,
    context_id: str | None,
    epistemic_statuses: Sequence[str] | None,
) -> dict[str, Any]:
    requested_epistemic_statuses = {
        str(item or "").strip().lower() for item in (epistemic_statuses or ())
    }
    if not requested_epistemic_statuses:
        requested_epistemic_statuses = {"asserted"}
    invalid_epistemic_statuses = requested_epistemic_statuses - EPISTEMIC_STATUSES
    if invalid_epistemic_statuses:
        raise ValueError("epistemic_statuses may contain tentative or asserted")
    epistemic_clauses: list[dict[str, Any]] = []
    if "asserted" in requested_epistemic_statuses:
        epistemic_clauses.extend(
            [
                {"epistemic_status": "asserted"},
                {"epistemic_status": {"$exists": False}},
            ]
        )
    if "tentative" in requested_epistemic_statuses:
        epistemic_clauses.append({"epistemic_status": "tentative"})
    query: dict[str, Any] = {
        (
            "scope.audience_key"
            if assertion_form == STANDALONE_TEXT_ASSERTION_FORM
            else "scope.audience_keys"
        ): {"$in": list(audience_keys)},
        "status": "asserted",
        "$and": [{"$or": epistemic_clauses}],
    }
    if subject_concept_ids:
        query["subject_concept_id"] = {"$in": list(subject_concept_ids)}
    if argument_concept_id:
        query["$or"] = [
            {"subject_concept_id": argument_concept_id},
            {"object_concept_id": argument_concept_id},
            {
                "concept_links": {
                    "$elemMatch": {
                        "concept_id": argument_concept_id,
                        "status": "active",
                    }
                }
            },
        ]
    if assertion_form is not None:
        if assertion_form not in {"relation", STANDALONE_TEXT_ASSERTION_FORM}:
            raise ValueError(
                "assertion_form must be 'relation' or 'standalone_text'"
            )
        if assertion_form == "relation":
            query["assertion_form"] = {
                "$ne": STANDALONE_TEXT_ASSERTION_FORM
            }
        else:
            query["assertion_form"] = STANDALONE_TEXT_ASSERTION_FORM
    if context_id:
        query["assertion_context.context_id"] = str(context_id).strip()
    if predicates:
        predicate_values: set[str] = set()
        for item in predicates:
            if not isinstance(item, str) or not item.strip():
                continue
            predicate_value = item.strip()
            predicate_values.add(predicate_value)
            predicate_concept_id = predicate_concept_id_for_storage(
                predicate_value
            )
            if predicate_concept_id:
                predicate_values.add(predicate_concept_id)
            if predicate_value.startswith("#V#"):
                storage_candidate = predicate_value[3:]
                if (
                    predicate_concept_id_for_storage(storage_candidate)
                    == predicate_value
                ):
                    predicate_values.add(storage_candidate)
        if predicate_values:
            query["predicate"] = {"$in": sorted(predicate_values)}
    if object_kind is not None:
        if object_kind not in OBJECT_KINDS:
            raise ValueError("object_kind must be 'text' or 'concept'")
        query["object_kind"] = object_kind
    if languages:
        language_values = sorted(
            {
                str(item).strip()
                for item in languages
                if isinstance(item, str) and item.strip()
            }
        )
        if language_values:
            query["object_kind"] = "text"
            query["object_text.language"] = {"$in": language_values}
    return query


def _empty_page(
    *,
    offset: int,
    limit: int,
    limit_per_subject: int | None,
    subject_concept_ids: Sequence[str],
) -> dict[str, Any]:
    per_subject = {
        subject_id: {
            "items": [],
            "returned": 0,
            "offset": offset,
            "limit": limit_per_subject,
            "has_more": False,
            "next_offset": None,
            "visibility_filtered": False,
            "counts_are_lower_bounds": False,
        }
        for subject_id in subject_concept_ids
    }
    return {
        "items": [],
        "returned": 0,
        "offset": offset,
        "limit": limit,
        "limit_per_subject": limit_per_subject,
        "has_more": False,
        "next_offset": None,
        "truncated": False,
        "visibility_filtered": False,
        "counts_are_lower_bounds": False,
        "per_subject": per_subject,
    }


def list_visible_scoped_assertions_page(
    *,
    subject_concept_ids: Sequence[str] | None = None,
    argument_concept_id: str | None = None,
    predicates: Sequence[str] | None = None,
    object_kind: str | None = None,
    languages: Sequence[str] | None = None,
    assertion_form: str | None = None,
    context_id: str | None = None,
    epistemic_statuses: Sequence[str] | None = None,
    limit: int = 200,
    offset: int = 0,
    limit_per_subject: int | None = None,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
) -> dict[str, Any]:
    """Return a bounded page visible to one effective or explicit trusted actor.

    ``offset`` advances only through rows whose referenced concepts are
    currently visible. Inaccessible and retracted storage candidates are
    deliberately absent from pagination, counts, continuation, and diagnostics.
    Candidate scanning is bounded; when that bound cannot establish a safe
    visible page, the read fails closed rather than exposing a misleading or
    visibility-sensitive continuation. With ``limit_per_subject``, one
    aggregate command independently bounds each requested subject so an
    assertion-rich subject cannot starve another.
    """

    page_limit = _normalise_page_bound(
        limit,
        name="limit",
        minimum=1,
        maximum=MAX_PAGE_LIMIT,
    )
    page_offset = _normalise_page_bound(
        offset,
        name="offset",
        minimum=0,
        maximum=2_147_483_647,
    )
    per_subject_limit = (
        _normalise_page_bound(
            limit_per_subject,
            name="limit_per_subject",
            minimum=1,
            maximum=MAX_PAGE_LIMIT,
        )
        if limit_per_subject is not None
        else None
    )
    requested_subjects = _normalise_subject_ids(subject_concept_ids)
    if per_subject_limit is not None and not requested_subjects:
        raise ValueError(
            "limit_per_subject requires explicit subject_concept_ids"
        )
    if per_subject_limit is not None:
        candidate_bound = len(requested_subjects) * (
            page_offset + per_subject_limit + 1
        )
        if candidate_bound > MAX_PAGE_CANDIDATES:
            raise ValueError(
                "per-subject visible page exceeds the bounded candidate limit "
                f"of {MAX_PAGE_CANDIDATES}"
            )

    actor_user_id, actor_org_id = _resolve_read_actor(
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    audience_keys = _audience_keys_for_actor(
        user_concept_id=actor_user_id,
        organisation_concept_id=actor_org_id,
    )
    if not audience_keys:
        return _empty_page(
            offset=page_offset,
            limit=page_limit,
            limit_per_subject=per_subject_limit,
            subject_concept_ids=requested_subjects,
        )

    with override_current_actor(actor_user_id, actor_org_id):
        resolved_argument_id = (
            _clean_concept_id(
                argument_concept_id,
                "argument_concept_id",
            )
            if argument_concept_id
            else None
        )
        preflight_concept_ids = {
            *requested_subjects,
            *([resolved_argument_id] if resolved_argument_id else []),
        }
        visible_preflight_ids = (
            filter_accessible_concept_ids(preflight_concept_ids)
            if preflight_concept_ids
            else set()
        )
        visible_subjects = [
            subject_id
            for subject_id in requested_subjects
            if subject_id in visible_preflight_ids
        ]
        if (
            resolved_argument_id
            and resolved_argument_id not in visible_preflight_ids
        ):
            return _empty_page(
                offset=page_offset,
                limit=page_limit,
                limit_per_subject=per_subject_limit,
                subject_concept_ids=requested_subjects,
            )
        if requested_subjects and not visible_subjects:
            return _empty_page(
                offset=page_offset,
                limit=page_limit,
                limit_per_subject=per_subject_limit,
                subject_concept_ids=requested_subjects,
            )

        query = _build_assertion_query(
            audience_keys=audience_keys,
            subject_concept_ids=visible_subjects,
            argument_concept_id=resolved_argument_id,
            predicates=predicates,
            object_kind=object_kind,
            languages=languages,
            assertion_form=assertion_form,
            context_id=context_id,
            epistemic_statuses=epistemic_statuses,
        )
        collection = get_scoped_knowledge_assertions_collection()
        if collection is None:
            return _empty_page(
                offset=page_offset,
                limit=page_limit,
                limit_per_subject=per_subject_limit,
                subject_concept_ids=requested_subjects,
            )

        if per_subject_limit is None:
            raw_candidates = list(
                collection.find(query)
                .sort([("updated_at", -1), ("assertion_id", 1)])
                .limit(MAX_PAGE_CANDIDATES + 1)
            )
            raw_window_exhausted = len(raw_candidates) > MAX_PAGE_CANDIDATES
            visible_candidates = _visible_assertion_documents(
                raw_candidates[:MAX_PAGE_CANDIDATES]
            )
            visible_target = page_offset + page_limit + 1
            if (
                raw_window_exhausted
                and len(visible_candidates) < visible_target
            ):
                raise RuntimeError(
                    "scoped assertion page could not be evaluated within the "
                    "bounded visibility window"
                )
            has_more = len(visible_candidates) > page_offset + page_limit
            page_candidates = visible_candidates[
                page_offset : page_offset + page_limit
            ]
            visible_items = [
                serialised
                for document in page_candidates
                if (serialised := _serialise(document)) is not None
            ]
            return {
                "items": visible_items,
                "returned": len(visible_items),
                "offset": page_offset,
                "limit": page_limit,
                "limit_per_subject": None,
                "has_more": has_more,
                "next_offset": (
                    page_offset + page_limit if has_more else None
                ),
                "truncated": has_more,
                "visibility_filtered": False,
                "counts_are_lower_bounds": has_more,
                "per_subject": {},
            }

        raw_by_subject: dict[str, list[Mapping[str, Any]]] = {
            subject_id: [] for subject_id in requested_subjects
        }
        raw_window_exhausted_by_subject: dict[str, bool] = {
            subject_id: False for subject_id in requested_subjects
        }
        if visible_subjects:
            candidate_limit_per_subject = (
                MAX_PAGE_CANDIDATES // len(visible_subjects)
            )
            branch_limit = candidate_limit_per_subject + 1

            def _subject_pipeline(subject_id: str) -> list[dict[str, Any]]:
                subject_query = dict(query)
                subject_query["subject_concept_id"] = subject_id
                return [
                    {"$match": subject_query},
                    {
                        "$sort": {
                            "updated_at": -1,
                            "assertion_id": 1,
                        }
                    },
                    {"$limit": branch_limit},
                ]

            pipeline = _subject_pipeline(visible_subjects[0])
            collection_name = str(
                getattr(collection, "name", STORAGE_SURFACE)
                or STORAGE_SURFACE
            )
            for subject_id in visible_subjects[1:]:
                pipeline.append(
                    {
                        "$unionWith": {
                            "coll": collection_name,
                            "pipeline": _subject_pipeline(subject_id),
                        }
                    }
                )
            for document in collection.aggregate(pipeline):
                if not isinstance(document, Mapping):
                    continue
                document_subject = str(
                    document.get("subject_concept_id") or ""
                ).strip()
                bucket = raw_by_subject.get(document_subject)
                if bucket is not None:
                    bucket.append(document)

        visibility_candidates: list[Mapping[str, Any]] = []
        for subject_id in visible_subjects:
            raw_subject_candidates = raw_by_subject[subject_id]
            raw_window_exhausted_by_subject[subject_id] = (
                len(raw_subject_candidates) > candidate_limit_per_subject
            )
            visibility_candidates.extend(
                raw_subject_candidates[:candidate_limit_per_subject]
            )
        visible_documents = _visible_assertion_documents(
            visibility_candidates
        )
        visible_by_subject: dict[str, list[Mapping[str, Any]]] = {
            subject_id: [] for subject_id in requested_subjects
        }
        for document in visible_documents:
            document_subject = str(
                document.get("subject_concept_id") or ""
            ).strip()
            bucket = visible_by_subject.get(document_subject)
            if bucket is not None:
                bucket.append(document)

        flattened_items: list[dict[str, Any]] = []
        per_subject: dict[str, dict[str, Any]] = {}
        any_more = False
        for subject_id in requested_subjects:
            visible_subject_candidates = visible_by_subject[subject_id]
            visible_target = page_offset + per_subject_limit + 1
            if (
                raw_window_exhausted_by_subject[subject_id]
                and len(visible_subject_candidates) < visible_target
            ):
                raise RuntimeError(
                    "scoped assertion page could not be evaluated within the "
                    "bounded visibility window"
                )
            has_more = (
                len(visible_subject_candidates)
                > page_offset + per_subject_limit
            )
            page_candidates = visible_subject_candidates[
                page_offset : page_offset + per_subject_limit
            ]
            visible_items = [
                serialised
                for document in page_candidates
                if (serialised := _serialise(document)) is not None
            ]
            any_more = any_more or has_more
            flattened_items.extend(visible_items)
            per_subject[subject_id] = {
                "items": visible_items,
                "returned": len(visible_items),
                "offset": page_offset,
                "limit": per_subject_limit,
                "has_more": has_more,
                "next_offset": (
                    page_offset + per_subject_limit if has_more else None
                ),
                "visibility_filtered": False,
                "counts_are_lower_bounds": has_more,
            }
        return {
            "items": flattened_items,
            "returned": len(flattened_items),
            "offset": page_offset,
            "limit": page_limit,
            "limit_per_subject": per_subject_limit,
            "has_more": any_more,
            "next_offset": None,
            "truncated": any_more,
            "visibility_filtered": False,
            "counts_are_lower_bounds": any_more,
            "per_subject": per_subject,
        }


def get_visible_scoped_assertion_by_id(
    assertion_id: Any,
    *,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
) -> dict[str, Any] | None:
    """Return one exact actor-visible assertion in any lifecycle state.

    Exact inspection is deliberately separate from the active assertion-list
    query. A retracted occurrence remains inspectable by an authorised actor,
    while audience and referenced-concept visibility still fail closed. Missing,
    inaccessible, and malformed identifiers all return the same absence result.
    """

    assertion_token = str(assertion_id or "").strip()
    if (
        not assertion_token.startswith("ska_")
        or len(assertion_token) > 256
        or not re.fullmatch(r"ska_[A-Za-z0-9_-]+", assertion_token)
    ):
        return None

    actor_user_id, actor_org_id = _resolve_read_actor(
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    audience_keys = _audience_keys_for_actor(
        user_concept_id=actor_user_id,
        organisation_concept_id=actor_org_id,
    )
    if not audience_keys:
        return None

    collection = get_scoped_knowledge_assertions_collection()
    if collection is None:
        raise RuntimeError("scoped assertion store unavailable")

    audience_query = {
        "$or": [
            {"scope.audience_key": {"$in": audience_keys}},
            {"scope.audience_keys": {"$in": audience_keys}},
        ]
    }
    with override_current_actor(actor_user_id, actor_org_id):
        document = collection.find_one(
            {
                "assertion_id": assertion_token,
                **audience_query,
            }
        )
        if not isinstance(document, Mapping):
            return None
        visible_documents = _visible_assertion_documents([document])
        if len(visible_documents) != 1:
            return None
        return _serialise(visible_documents[0])


def list_visible_scoped_assertions(
    *,
    subject_concept_ids: Sequence[str] | None = None,
    argument_concept_id: str | None = None,
    predicates: Sequence[str] | None = None,
    object_kind: str | None = None,
    languages: Sequence[str] | None = None,
    assertion_form: str | None = None,
    context_id: str | None = None,
    epistemic_statuses: Sequence[str] | None = None,
    limit: int = 200,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only the bounded page items."""

    return list_visible_scoped_assertions_page(
        subject_concept_ids=subject_concept_ids,
        argument_concept_id=argument_concept_id,
        predicates=predicates,
        object_kind=object_kind,
        languages=languages,
        assertion_form=assertion_form,
        context_id=context_id,
        epistemic_statuses=epistemic_statuses,
        limit=limit,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
    )["items"]


def scoped_text_assertion_to_relation_row(
    assertion: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Project a scoped text assertion into the generic text-read row shape."""

    if assertion.get("assertion_form") == STANDALONE_TEXT_ASSERTION_FORM:
        return None
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
        "relation_id": None,
        "assertion_id": assertion_id or None,
        "row_kind": "scoped_assertion",
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
    "STANDALONE_TEXT_ASSERTION_FORM",
    "add_text_assertion_concept_links",
    "get_visible_scoped_assertion_by_id",
    "list_visible_scoped_assertions",
    "list_visible_scoped_assertions_page",
    "retract_scoped_assertion",
    "scoped_text_assertion_to_relation_row",
    "store_text_assertion",
    "upsert_scoped_assertion",
]
