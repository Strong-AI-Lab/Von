"""Persist source-grounded learning candidates without activating them.

This service is deliberately smaller than an advice or policy subsystem.  It
captures a potentially reusable lesson as an actor- or organisation-visible
Vontology artefact,
supports inspection and revision, and does not project the lesson into any
runtime prompt, workflow, or tool decision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import override_current_actor
from ..security.visibility_predicates import (
    get_specific_to_org_values,
    get_specific_to_user_values,
    set_specific_to_org_values,
    set_specific_to_user_values,
)
from . import chat_history_service
from .feature_flags import suppress_event_workflow_launches
from .namespace_service import (
    derive_actor_context_from_namespace,
    derive_namespace_for_actor,
)
from .text_value_service import (
    get_texts_for_concept,
    get_texts_for_concepts,
    upsert_singleton_text_relation,
)

LEARNING_CANDIDATE_SCHEMA_VERSION = "learning_candidate.v1"
LEARNING_CANDIDATE_SYSTEM_TAG = "von_learning_candidate"
LEARNING_CANDIDATE_ARTIFACT_TYPE_ID = "#V#artifact"
LEARNING_CANDIDATE_AUTHOR_ID = "#V#von_system"
LEARNING_CANDIDATE_LIFECYCLE_STATE = "non_active"

CONVERSATION_TYPE_ID = "#V#conversation"
EPISODE_CRITIQUE_MEMORY_TYPE_ID = "#V#episode_critique_memory"
EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION = "episode_critique_memory.v1"

_SOURCE_CONVERSATION = "conversation"
_SOURCE_EPISODE_CRITIQUE_MEMORY = "episode_critique_memory"
_SOURCE_KINDS = {_SOURCE_CONVERSATION, _SOURCE_EPISODE_CRITIQUE_MEMORY}
_VISIBILITY_SCOPES = {"actor", "organisation"}
_DESCRIPTION_PREDICATES = {"hasDescription", "#V#hasDescription"}
_MAX_BODY_CHARS = 12_000
_MAX_REFERENCE_COUNT = 100
_MAX_LIST_LIMIT = 200


class LearningCandidateError(RuntimeError):
    """Base error for represented learning-candidate operations."""


class InvalidLearningCandidateData(LearningCandidateError, ValueError):
    """Raised when candidate input is malformed or internally inconsistent."""


class LearningCandidateNotFoundError(LearningCandidateError, LookupError):
    """Raised when a candidate is absent or outside the trusted visibility scope."""


class LearningCandidateAccessError(LearningCandidateError, PermissionError):
    """Raised when an operation would exceed its trusted source or visibility."""

    def __init__(self, reason_code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.reason_code = reason_code
        self.safe_message = safe_message


class LearningCandidateConflictError(LearningCandidateError):
    """Raised when an idempotency or revision identity is reused differently."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _required_text(value: Any, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidLearningCandidateData(f"{field} is required")
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise InvalidLearningCandidateData(f"{field} is too long")
    return cleaned


def _optional_text(value: Any, *, field: str, max_chars: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field=field, max_chars=max_chars)


def _concept_id(value: Any, *, field: str) -> str:
    cleaned = _required_text(value, field=field, max_chars=300)
    if not cleaned.startswith("#V#") or len(cleaned) <= 3:
        raise InvalidLearningCandidateData(f"{field} must be a #V# concept ID")
    return cleaned


def _normalise_concept_ids(
    values: Any,
    *,
    field: str,
    required: bool,
) -> list[str]:
    if values is None:
        values = []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise InvalidLearningCandidateData(f"{field} must be a list of concept IDs")
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        concept_id = _concept_id(value, field=f"{field}[{index}]")
        if concept_id in seen:
            continue
        seen.add(concept_id)
        result.append(concept_id)
        if len(result) > _MAX_REFERENCE_COUNT:
            raise InvalidLearningCandidateData(
                f"{field} must contain at most {_MAX_REFERENCE_COUNT} concept IDs"
            )
    if required and not result:
        raise InvalidLearningCandidateData(f"{field} requires at least one concept ID")
    return result


def _stable_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidLearningCandidateData(
            "candidate data must contain JSON values"
        ) from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _revision_identity_sha256(
    *,
    revision_request_id: str,
    body: str,
    references: Mapping[str, Sequence[str]],
) -> str:
    return _stable_digest(
        {
            "revision_request_id": revision_request_id,
            "body_sha256": _body_digest(body),
            **{field: list(values) for field, values in references.items()},
        }
    )


def _resolve_actor_scope(
    *,
    actor_user_id: Any,
    organisation_concept_id: Any,
    namespace: Any,
) -> tuple[str | None, str | None, str | None]:
    explicit_actor = (
        _concept_id(actor_user_id, field="actor_user_id")
        if actor_user_id is not None
        else None
    )
    explicit_org = (
        _concept_id(organisation_concept_id, field="organisation_concept_id")
        if organisation_concept_id is not None
        else None
    )
    namespace_value = _optional_text(namespace, field="namespace", max_chars=500)
    namespace_actor, namespace_org = derive_actor_context_from_namespace(
        namespace_value
    )
    if namespace_value and namespace_actor is None:
        raise InvalidLearningCandidateData(
            "namespace is not a canonical actor namespace"
        )
    if explicit_actor and namespace_actor and explicit_actor != namespace_actor:
        raise LearningCandidateAccessError(
            "learning_candidate_actor_scope_mismatch",
            "The actor does not match the trusted namespace",
        )
    if explicit_org and namespace_org and explicit_org != namespace_org:
        raise LearningCandidateAccessError(
            "learning_candidate_organisation_scope_mismatch",
            "The organisation does not match the trusted namespace",
        )
    actor = explicit_actor or namespace_actor
    org = explicit_org or namespace_org
    if actor is None and org is None:
        raise LearningCandidateAccessError(
            "learning_candidate_actor_context_required",
            "A trusted actor or organisation context is required",
        )
    canonical_namespace = (
        derive_namespace_for_actor(actor, org) if actor is not None else None
    )
    if actor is not None and not canonical_namespace:
        raise InvalidLearningCandidateData(
            "Could not derive a canonical actor namespace"
        )
    return actor, org, canonical_namespace


def _relationship_targets(relationships: Any, predicate: str) -> list[str]:
    if not isinstance(relationships, Mapping):
        return []
    values = relationships.get(predicate)
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    return [
        value.strip() for value in values if isinstance(value, str) and value.strip()
    ]


def _source_not_visible() -> LearningCandidateAccessError:
    # Do not distinguish a missing identifier from an identifier hidden from the
    # current actor.
    return LearningCandidateAccessError(
        "learning_candidate_source_not_visible",
        "The learning source is not available in the trusted visibility scope",
    )


def _normalise_visible_source(
    source: Any,
    *,
    actor_user_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(source, Mapping):
        raise InvalidLearningCandidateData("source must be an object")
    kind = _required_text(source.get("kind"), field="source.kind", max_chars=80)
    if kind not in _SOURCE_KINDS:
        raise InvalidLearningCandidateData(
            "source.kind must be conversation or episode_critique_memory"
        )

    if kind == _SOURCE_CONVERSATION:
        supplied_source_id = source.get("conversation_concept_id") or source.get(
            "concept_id"
        )
        supplied_session_id = _optional_text(
            source.get("session_id"), field="source.session_id", max_chars=500
        )
        if supplied_source_id is not None:
            source_id = _concept_id(
                supplied_source_id,
                field="source.conversation_concept_id",
            )
            source_doc = ConceptsRepository.find_one({"concept_id": source_id})
            if not isinstance(source_doc, Mapping):
                raise _source_not_visible()
            if CONVERSATION_TYPE_ID not in _relationship_targets(
                source_doc.get("relationships"), "is_an_instance_of"
            ):
                raise _source_not_visible()
            metadata = source_doc.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
            stored_session_id = _optional_text(
                metadata.get("session_id"),
                field="stored source session_id",
                max_chars=500,
            )
            if stored_session_id is None:
                raise InvalidLearningCandidateData(
                    "The conversation source has no canonical session locator"
                )
            if supplied_session_id and supplied_session_id != stored_session_id:
                raise InvalidLearningCandidateData(
                    "source.session_id does not match the canonical conversation"
                )
            return (
                {
                    "kind": kind,
                    "locator": {
                        "conversation_concept_id": source_id,
                        "session_id": stored_session_id,
                    },
                },
                dict(source_doc),
            )

        if actor_user_id is None:
            raise LearningCandidateAccessError(
                "learning_candidate_conversation_owner_required",
                "A conversation-session source requires its trusted owner actor",
            )
        stored_owner = _optional_text(
            source.get("owner_user_id"),
            field="source.owner_user_id",
            max_chars=300,
        )
        if stored_owner is not None and stored_owner != actor_user_id:
            raise _source_not_visible()
        if supplied_session_id is None:
            raise InvalidLearningCandidateData(
                "source.session_id is required when no conversation concept is supplied"
            )
        stored_namespace = _optional_text(
            source.get("namespace"), field="source.namespace", max_chars=500
        )
        if stored_namespace and namespace and stored_namespace != namespace:
            raise LearningCandidateAccessError(
                "learning_candidate_source_namespace_mismatch",
                "The conversation source namespace does not match the trusted scope",
            )
        source_namespace = namespace or stored_namespace
        try:
            source_exists = chat_history_service.has_chat_history_session(
                actor_user_id,
                supplied_session_id,
                namespace=source_namespace,
                include_legacy=True,
            )
        except Exception as exc:
            raise LearningCandidateAccessError(
                "learning_candidate_source_read_unavailable",
                "The conversation source could not be verified",
            ) from exc
        if not source_exists:
            raise _source_not_visible()

        locator: dict[str, Any] = {
            "session_id": supplied_session_id,
            "owner_user_id": actor_user_id,
        }
        if source_namespace:
            locator["namespace"] = source_namespace
        # Keep this locator stable if a conversation concept is materialised
        # later.  A caller that already holds such a concept can use the exact
        # concept-source form above; session-only retries must not change identity.
        synthetic_source_doc = {
            "concept_id": None,
            "relationships": set_specific_to_user_values({}, [actor_user_id]),
        }
        return {"kind": kind, "locator": locator}, synthetic_source_doc

    source_id = _concept_id(
        source.get("memory_id") or source.get("concept_id"),
        field="source.memory_id",
    )
    source_doc = ConceptsRepository.find_one({"concept_id": source_id})
    if not isinstance(source_doc, Mapping):
        raise _source_not_visible()
    if EPISODE_CRITIQUE_MEMORY_TYPE_ID not in _relationship_targets(
        source_doc.get("relationships"), "is_an_instance_of"
    ):
        raise _source_not_visible()
    concept_data = source_doc.get("concept_data")
    concept_data = dict(concept_data) if isinstance(concept_data, Mapping) else {}
    memory = concept_data.get("episode_critique_memory")
    if not isinstance(memory, Mapping):
        raise _source_not_visible()
    if memory.get("schema_version") != EPISODE_CRITIQUE_MEMORY_SCHEMA_VERSION:
        raise _source_not_visible()
    stored_memory_id = str(memory.get("memory_id") or "").strip()
    if stored_memory_id != source_id:
        raise InvalidLearningCandidateData(
            "source.memory_id does not match the canonical episode memory"
        )
    subject_episode = memory.get("subject_episode")
    subject_episode = (
        dict(subject_episode) if isinstance(subject_episode, Mapping) else {}
    )
    locator: dict[str, Any] = {"memory_id": source_id}
    stable_key = _optional_text(
        subject_episode.get("stable_key"),
        field="stored source stable_key",
        max_chars=500,
    )
    if stable_key:
        locator["episode_stable_key"] = stable_key
    request_id = _optional_text(
        memory.get("request_id"), field="stored source request_id", max_chars=500
    )
    if request_id:
        locator["request_id"] = request_id
    return {"kind": kind, "locator": locator}, dict(source_doc)


def _source_concept_id(source: Mapping[str, Any]) -> str | None:
    locator = source.get("locator")
    if not isinstance(locator, Mapping):
        raise InvalidLearningCandidateData("Stored candidate source is invalid")
    value = (
        locator.get("conversation_concept_id")
        if source.get("kind") == _SOURCE_CONVERSATION
        else locator.get("memory_id")
    )
    if value is None and source.get("kind") == _SOURCE_CONVERSATION:
        return None
    return _concept_id(value, field="stored source locator")


def _visible_reference_ids(
    reference_groups: Mapping[str, Sequence[str]],
) -> set[str]:
    expected = {
        concept_id for values in reference_groups.values() for concept_id in values
    }
    if not expected:
        return set()
    return {
        str(doc.get("concept_id") or "").strip()
        for doc in ConceptsRepository.find(
            {"concept_id": {"$in": sorted(expected)}},
            projection={"concept_id": 1},
            limit=len(expected),
        )
        if isinstance(doc, Mapping)
    }


def _validate_visible_references(reference_groups: Mapping[str, Sequence[str]]) -> None:
    expected = {
        concept_id for values in reference_groups.values() for concept_id in values
    }
    visible = _visible_reference_ids(reference_groups)
    missing = expected - visible
    if missing:
        fields = sorted(
            field
            for field, values in reference_groups.items()
            if any(value in missing for value in values)
        )
        raise LearningCandidateAccessError(
            "learning_candidate_reference_not_visible",
            "One or more candidate references are not available in the trusted "
            f"visibility scope ({', '.join(fields)})",
        )


def _visibility_relationships(
    *,
    visibility_scope: str,
    actor_user_id: str | None,
    organisation_concept_id: str | None,
    source_doc: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if visibility_scope not in _VISIBILITY_SCOPES:
        raise InvalidLearningCandidateData(
            "visibility_scope must be actor or organisation"
        )
    relationships: dict[str, Any] = {}
    if visibility_scope == "actor":
        if actor_user_id is None:
            raise LearningCandidateAccessError(
                "learning_candidate_actor_context_required",
                "Actor visibility requires a trusted actor context",
            )
        relationships = set_specific_to_user_values(relationships, [actor_user_id])
        return relationships, {
            "scope": "actor",
            "actor_user_id": actor_user_id,
            "organisation_concept_id": organisation_concept_id,
        }

    if organisation_concept_id is None:
        raise LearningCandidateAccessError(
            "learning_candidate_organisation_context_required",
            "Organisation visibility requires a trusted organisation context",
        )
    source_relationships = source_doc.get("relationships")
    source_relationships = (
        dict(source_relationships) if isinstance(source_relationships, Mapping) else {}
    )
    source_users = get_specific_to_user_values(source_relationships)
    source_orgs = get_specific_to_org_values(source_relationships)
    source_is_global = not source_users and not source_orgs
    if not source_is_global and organisation_concept_id not in source_orgs:
        raise LearningCandidateAccessError(
            "learning_candidate_source_visibility_would_expand",
            "The candidate cannot be made organisation-visible from this source",
        )
    relationships = set_specific_to_org_values(relationships, [organisation_concept_id])
    return relationships, {
        "scope": "organisation",
        "actor_user_id": actor_user_id,
        "organisation_concept_id": organisation_concept_id,
    }


def _candidate_state(doc: Mapping[str, Any]) -> dict[str, Any]:
    tags = doc.get("system_tags")
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes, bytearray)):
        raise LearningCandidateNotFoundError("Learning candidate not found")
    if LEARNING_CANDIDATE_SYSTEM_TAG not in tags:
        raise LearningCandidateNotFoundError("Learning candidate not found")
    if LEARNING_CANDIDATE_ARTIFACT_TYPE_ID not in _relationship_targets(
        doc.get("relationships"), "is_an_instance_of"
    ):
        raise InvalidLearningCandidateData(
            "Stored learning candidate is not a #V#artifact instance"
        )
    concept_data = doc.get("concept_data")
    concept_data = dict(concept_data) if isinstance(concept_data, Mapping) else {}
    state = concept_data.get("learning_candidate")
    if not isinstance(state, Mapping):
        raise LearningCandidateNotFoundError("Learning candidate not found")
    state = copy.deepcopy(dict(state))
    if state.get("schema_version") != LEARNING_CANDIDATE_SCHEMA_VERSION:
        raise LearningCandidateNotFoundError("Learning candidate not found")
    if state.get("lifecycle_state") != LEARNING_CANDIDATE_LIFECYCLE_STATE:
        raise InvalidLearningCandidateData(
            "Stored learning candidate is not in the fixed non_active state"
        )
    if state.get("author_concept_id") != LEARNING_CANDIDATE_AUTHOR_ID:
        raise InvalidLearningCandidateData(
            "Stored learning candidate authorship is invalid"
        )
    source = state.get("source")
    if not isinstance(source, Mapping) or state.get(
        "source_locator_sha256"
    ) != _stable_digest(source):
        raise InvalidLearningCandidateData(
            "Stored learning candidate source locator is invalid"
        )
    return state


def _canonical_body_from_rows(rows: Any) -> str:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        rows = []
    bodies = [
        str(row.get("text") or "").strip()
        for row in rows
        if isinstance(row, Mapping)
        and row.get("predicate") in _DESCRIPTION_PREDICATES
        and isinstance(row.get("text"), str)
        and str(row.get("text") or "").strip()
    ]
    if not bodies:
        raise InvalidLearningCandidateData(
            "Learning candidate canonical hasDescription body is missing"
        )
    return bodies[0]


def _project_candidate(
    doc: Mapping[str, Any],
    *,
    description_rows: Any,
    validate_source: bool,
    actor_user_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None,
) -> dict[str, Any]:
    state = _candidate_state(doc)
    stored_references = {
        "contributor_concept_ids": _normalise_concept_ids(
            state.get("contributor_concept_ids"),
            field="stored contributor_concept_ids",
            required=True,
        ),
        "target_concept_ids": _normalise_concept_ids(
            state.get("target_concept_ids"),
            field="stored target_concept_ids",
            required=True,
        ),
        "audience_concept_ids": _normalise_concept_ids(
            state.get("audience_concept_ids"),
            field="stored audience_concept_ids",
            required=False,
        ),
        "beneficiary_concept_ids": _normalise_concept_ids(
            state.get("beneficiary_concept_ids"),
            field="stored beneficiary_concept_ids",
            required=False,
        ),
        "purpose_concept_ids": _normalise_concept_ids(
            state.get("purpose_concept_ids"),
            field="stored purpose_concept_ids",
            required=False,
        ),
    }
    stored_prior_revisions: list[tuple[dict[str, Any], dict[str, list[str]]]] = []
    for index, prior_value in enumerate(state.get("prior_revisions") or []):
        if not isinstance(prior_value, Mapping):
            raise InvalidLearningCandidateData(
                f"Stored prior revision {index} is invalid"
            )
        prior = copy.deepcopy(dict(prior_value))
        prior_references = {
            field: _normalise_concept_ids(
                prior.get(field),
                field=f"stored prior_revisions[{index}].{field}",
                required=field in {"contributor_concept_ids", "target_concept_ids"},
            )
            for field in stored_references
        }
        stored_prior_revisions.append((prior, prior_references))

    all_stored_references = {
        "all": [
            concept_id for values in stored_references.values() for concept_id in values
        ]
    }
    for _prior, prior_references in stored_prior_revisions:
        all_stored_references["all"].extend(
            concept_id for values in prior_references.values() for concept_id in values
        )
    visible_reference_ids = _visible_reference_ids(all_stored_references)

    def _project_references(
        values_by_field: Mapping[str, Sequence[str]],
    ) -> tuple[dict[str, list[str]], dict[str, int]]:
        projected = {
            field: [value for value in values if value in visible_reference_ids]
            for field, values in values_by_field.items()
        }
        redacted_counts = {
            field: len(values_by_field[field]) - len(projected[field])
            for field in values_by_field
            if len(values_by_field[field]) != len(projected[field])
        }
        return projected, redacted_counts

    references, redacted_reference_counts = _project_references(stored_references)
    prior_revisions: list[dict[str, Any]] = []
    historical_redacted_reference_count = 0
    for prior, prior_references in stored_prior_revisions:
        projected_prior_references, prior_redacted_counts = _project_references(
            prior_references
        )
        prior.update(projected_prior_references)
        prior["semantic_reference_projection"] = {
            "redacted_counts": prior_redacted_counts,
        }
        historical_redacted_reference_count += sum(prior_redacted_counts.values())
        prior_revisions.append(prior)
    source = state.get("source")
    if not isinstance(source, Mapping):
        raise InvalidLearningCandidateData(
            "Stored learning candidate source is invalid"
        )
    if validate_source:
        _normalise_visible_source(
            {
                "kind": source.get("kind"),
                **dict(source.get("locator") or {}),
            },
            actor_user_id=actor_user_id,
            organisation_concept_id=organisation_concept_id,
            namespace=namespace,
        )
    body = _canonical_body_from_rows(description_rows)
    if _body_digest(body) != state.get("body_sha256"):
        raise InvalidLearningCandidateData(
            "Learning candidate body does not match its canonical digest"
        )
    return {
        "candidate_id": doc.get("concept_id"),
        "schema_version": state.get("schema_version"),
        "lifecycle_state": state.get("lifecycle_state"),
        "body": body,
        "body_sha256": state.get("body_sha256"),
        "source": copy.deepcopy(dict(source)),
        "authorship": {
            "author_concept_id": state.get("author_concept_id"),
            "capture_actor_concept_id": state.get("capture_actor_concept_id"),
            "capture_organisation_concept_id": state.get(
                "capture_organisation_concept_id"
            ),
            "last_revised_by_actor_concept_id": state.get(
                "last_revised_by_actor_concept_id"
            ),
            "last_revised_by_organisation_concept_id": state.get(
                "last_revised_by_organisation_concept_id"
            ),
        },
        **references,
        "semantic_reference_projection": {
            "access_basis": "candidate_and_source_visibility",
            "redacted_counts": redacted_reference_counts,
            "historical_redacted_reference_count": (
                historical_redacted_reference_count
            ),
        },
        "visibility": copy.deepcopy(dict(state.get("visibility") or {})),
        "namespace": state.get("namespace"),
        "capture_request_id": state.get("capture_request_id"),
        "idempotency_key": state.get("idempotency_key"),
        "revision": state.get("revision"),
        "revision_request_id": state.get("revision_request_id"),
        "prior_revisions": prior_revisions,
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
    }


def _persist_body(
    *,
    candidate_id: str,
    body: str,
    actor_user_id: str | None,
    organisation_concept_id: str | None,
    revision: int,
) -> None:
    upsert_singleton_text_relation(
        subject_concept_id=candidate_id,
        predicate="hasDescription",
        text=body,
        lang="en-NZ",
        provenance={
            "author_concept_id": LEARNING_CANDIDATE_AUTHOR_ID,
            "acting_actor_concept_id": actor_user_id,
            "acting_organisation_concept_id": organisation_concept_id,
        },
        context={
            "schema_version": LEARNING_CANDIDATE_SCHEMA_VERSION,
            "lifecycle_state": LEARNING_CANDIDATE_LIFECYCLE_STATE,
            "revision": revision,
        },
        garbage_collect=True,
    )


def _capture_identity_payload(
    *,
    source: Mapping[str, Any],
    body: str,
    references: Mapping[str, Sequence[str]],
    visibility: Mapping[str, Any],
    namespace: str | None,
    request_id: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "source": dict(source),
        "body_sha256": _body_digest(body),
        **{field: list(values) for field, values in references.items()},
        "visibility": dict(visibility),
        "namespace": namespace,
        "capture_request_id": request_id,
        "idempotency_key": idempotency_key,
    }


def _candidate_id_for_capture(
    *,
    actor_user_id: str | None,
    organisation_concept_id: str | None,
    source: Mapping[str, Any],
    idempotency_key: str,
) -> str:
    digest = _stable_digest(
        {
            "actor_user_id": actor_user_id,
            "organisation_concept_id": organisation_concept_id,
            "source": dict(source),
            "idempotency_key": idempotency_key,
        }
    )[:32]
    return f"#V#learning_candidate_{digest}"


def capture_learning_candidate(
    *,
    body: str,
    source: Mapping[str, Any],
    contributor_concept_ids: Sequence[str],
    target_concept_ids: Sequence[str],
    actor_user_id: str | None = None,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    audience_concept_ids: Sequence[str] | None = None,
    beneficiary_concept_ids: Sequence[str] | None = None,
    purpose_concept_ids: Sequence[str] | None = None,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    visibility_scope: str = "actor",
) -> dict[str, Any]:
    """Capture one explicitly non-active candidate grounded in a visible source."""

    candidate_body = _required_text(body, field="body", max_chars=_MAX_BODY_CHARS)
    request = _optional_text(request_id, field="request_id", max_chars=300)
    stable_key = (
        _optional_text(idempotency_key, field="idempotency_key", max_chars=300)
        or request
    )
    if stable_key is None:
        raise InvalidLearningCandidateData("idempotency_key or request_id is required")
    actor, org, canonical_namespace = _resolve_actor_scope(
        actor_user_id=actor_user_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    references = {
        "contributor_concept_ids": _normalise_concept_ids(
            contributor_concept_ids,
            field="contributor_concept_ids",
            required=True,
        ),
        "target_concept_ids": _normalise_concept_ids(
            target_concept_ids, field="target_concept_ids", required=True
        ),
        "audience_concept_ids": _normalise_concept_ids(
            audience_concept_ids,
            field="audience_concept_ids",
            required=False,
        ),
        "beneficiary_concept_ids": _normalise_concept_ids(
            beneficiary_concept_ids,
            field="beneficiary_concept_ids",
            required=False,
        ),
        "purpose_concept_ids": _normalise_concept_ids(
            purpose_concept_ids,
            field="purpose_concept_ids",
            required=False,
        ),
    }

    with override_current_actor(actor, org):
        normalised_source, source_doc = _normalise_visible_source(
            source,
            actor_user_id=actor,
            organisation_concept_id=org,
            namespace=canonical_namespace,
        )
        _validate_visible_references(references)
        visibility_relationships, visibility = _visibility_relationships(
            visibility_scope=visibility_scope,
            actor_user_id=actor,
            organisation_concept_id=org,
            source_doc=source_doc,
        )
        candidate_id = _candidate_id_for_capture(
            actor_user_id=actor,
            organisation_concept_id=org,
            source=normalised_source,
            idempotency_key=stable_key,
        )
        identity_payload = _capture_identity_payload(
            source=normalised_source,
            body=candidate_body,
            references=references,
            visibility=visibility,
            namespace=canonical_namespace,
            request_id=request,
            idempotency_key=stable_key,
        )
        identity_sha256 = _stable_digest(identity_payload)
        initial_revision_request = request or stable_key
        now = _now()
        state = {
            "schema_version": LEARNING_CANDIDATE_SCHEMA_VERSION,
            "lifecycle_state": LEARNING_CANDIDATE_LIFECYCLE_STATE,
            "source": normalised_source,
            "source_locator_sha256": _stable_digest(normalised_source),
            "author_concept_id": LEARNING_CANDIDATE_AUTHOR_ID,
            "capture_actor_concept_id": actor,
            "capture_organisation_concept_id": org,
            "last_revised_by_actor_concept_id": actor,
            "last_revised_by_organisation_concept_id": org,
            **references,
            "visibility": visibility,
            "namespace": canonical_namespace,
            "capture_request_id": request,
            "idempotency_key": stable_key,
            "capture_identity_sha256": identity_sha256,
            "body_sha256": _body_digest(candidate_body),
            "revision": 1,
            "revision_request_id": initial_revision_request,
            "revision_identity_sha256": _revision_identity_sha256(
                revision_request_id=initial_revision_request,
                body=candidate_body,
                references=references,
            ),
            "prior_revisions": [],
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }
        relationships: dict[str, Any] = {
            "is_an_instance_of": [LEARNING_CANDIDATE_ARTIFACT_TYPE_ID],
            "#V#authored_by": [LEARNING_CANDIDATE_AUTHOR_ID],
            **visibility_relationships,
        }
        source_concept_id = _source_concept_id(normalised_source)
        if source_concept_id is not None:
            relationships["related_to"] = [source_concept_id]
        doc = {
            "concept_id": candidate_id,
            "guid": str(uuid.uuid4()),
            "relationships": relationships,
            "system_tags": [
                LEARNING_CANDIDATE_SYSTEM_TAG,
                LEARNING_CANDIDATE_SCHEMA_VERSION,
                LEARNING_CANDIDATE_LIFECYCLE_STATE,
            ],
            "attributes": {
                "learning_candidate": {
                    "schema_version": LEARNING_CANDIDATE_SCHEMA_VERSION,
                    "lifecycle_state": LEARNING_CANDIDATE_LIFECYCLE_STATE,
                    "source_kind": normalised_source["kind"],
                    "capture_identity_sha256": identity_sha256,
                }
            },
            "concept_data": {"learning_candidate": state},
            "created_at": now,
            "updated_at": now,
        }

        created = True
        with suppress_event_workflow_launches("learning_candidate_non_active_capture"):
            try:
                ConceptsRepository.insert_one(doc)
            except DuplicateKeyError:
                created = False
            if not created:
                existing = ConceptsRepository.find_one({"concept_id": candidate_id})
                if not isinstance(existing, Mapping):
                    raise LearningCandidateNotFoundError(
                        "The existing learning candidate is not accessible"
                    )
                existing_state = _candidate_state(existing)
                if existing_state.get("capture_identity_sha256") != identity_sha256:
                    raise LearningCandidateConflictError(
                        "The idempotency identity is already bound to different candidate data"
                    )
                state = existing_state
                doc = dict(existing)
            # A retry may repair a partial initial capture, but it must never
            # overwrite a later revision with the original capture body.
            if created or int(state.get("revision") or 1) == 1:
                _persist_body(
                    candidate_id=candidate_id,
                    body=candidate_body,
                    actor_user_id=actor,
                    organisation_concept_id=org,
                    revision=int(state.get("revision") or 1),
                )

        rows = get_texts_for_concept(
            candidate_id,
            predicate="hasDescription",
            lang="en-NZ",
            recent_first=True,
        )
        result = _project_candidate(
            doc,
            description_rows=rows,
            validate_source=True,
            actor_user_id=actor,
            organisation_concept_id=org,
            namespace=canonical_namespace,
        )
        result["created"] = created
        result["idempotent"] = not created
        return result


def get_learning_candidate(
    candidate_id: str,
    *,
    actor_user_id: str | None = None,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Read one candidate and its canonical body in a trusted visibility scope."""

    resolved_id = _concept_id(candidate_id, field="candidate_id")
    actor, org, canonical_namespace = _resolve_actor_scope(
        actor_user_id=actor_user_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    with override_current_actor(actor, org):
        doc = ConceptsRepository.find_one({"concept_id": resolved_id})
        if not isinstance(doc, Mapping):
            raise LearningCandidateNotFoundError("Learning candidate not found")
        rows = get_texts_for_concept(
            resolved_id,
            predicate="hasDescription",
            lang="en-NZ",
            recent_first=True,
        )
        return _project_candidate(
            doc,
            description_rows=rows,
            validate_source=True,
            actor_user_id=actor,
            organisation_concept_id=org,
            namespace=canonical_namespace,
        )


def list_learning_candidates(
    *,
    actor_user_id: str | None = None,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    target_concept_id: str | None = None,
    source_kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List actor- or organisation-visible candidates with visible sources."""

    actor, org, canonical_namespace = _resolve_actor_scope(
        actor_user_id=actor_user_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise InvalidLearningCandidateData("limit must be a positive integer")
    safe_limit = min(limit, _MAX_LIST_LIMIT)
    query: dict[str, Any] = {
        "system_tags": LEARNING_CANDIDATE_SYSTEM_TAG,
        "concept_data.learning_candidate.schema_version": (
            LEARNING_CANDIDATE_SCHEMA_VERSION
        ),
        "concept_data.learning_candidate.lifecycle_state": (
            LEARNING_CANDIDATE_LIFECYCLE_STATE
        ),
    }
    if target_concept_id is not None:
        query["concept_data.learning_candidate.target_concept_ids"] = _concept_id(
            target_concept_id, field="target_concept_id"
        )
    if source_kind is not None:
        resolved_kind = _required_text(source_kind, field="source_kind", max_chars=80)
        if resolved_kind not in _SOURCE_KINDS:
            raise InvalidLearningCandidateData(
                "source_kind must be conversation or episode_critique_memory"
            )
        query["concept_data.learning_candidate.source.kind"] = resolved_kind

    with override_current_actor(actor, org):
        docs = list(
            ConceptsRepository.find(
                query,
                sort=[("updated_at", -1), ("concept_id", 1)],
                limit=safe_limit,
            )
        )
        ids = [
            str(doc.get("concept_id") or "").strip()
            for doc in docs
            if isinstance(doc, Mapping)
            and isinstance(doc.get("concept_id"), str)
            and str(doc.get("concept_id") or "").strip()
        ]
        rows_by_id = get_texts_for_concepts(
            ids,
            predicate="hasDescription",
            lang="en-NZ",
            limit_per_concept=5,
            recent_first=True,
        )
        candidates: list[dict[str, Any]] = []
        for doc in docs:
            if not isinstance(doc, Mapping):
                continue
            candidate_id = str(doc.get("concept_id") or "").strip()
            try:
                candidates.append(
                    _project_candidate(
                        doc,
                        description_rows=rows_by_id.get(candidate_id, []),
                        validate_source=True,
                        actor_user_id=actor,
                        organisation_concept_id=org,
                        namespace=canonical_namespace,
                    )
                )
            except LearningCandidateAccessError:
                # A candidate never makes a source visible.  If source access was
                # withdrawn, omit it from discovery without disclosing the source.
                continue
            except InvalidLearningCandidateData:
                # Keep a partial cross-store write visible as an explicitly
                # invalid non-active row, without leaking its body or references
                # or making one damaged row abort the whole discovery result.
                candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "schema_version": LEARNING_CANDIDATE_SCHEMA_VERSION,
                        "lifecycle_state": LEARNING_CANDIDATE_LIFECYCLE_STATE,
                        "projection_status": "invalid",
                        "error_code": "learning_candidate_invalid_projection",
                    }
                )
        return candidates


def revise_learning_candidate(
    candidate_id: str,
    *,
    body: str,
    revision_request_id: str,
    actor_user_id: str | None = None,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    contributor_concept_ids: Sequence[str] | None = None,
    target_concept_ids: Sequence[str] | None = None,
    audience_concept_ids: Sequence[str] | None = None,
    beneficiary_concept_ids: Sequence[str] | None = None,
    purpose_concept_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Revise candidate content/references while preserving source and non-activity."""

    resolved_id = _concept_id(candidate_id, field="candidate_id")
    candidate_body = _required_text(body, field="body", max_chars=_MAX_BODY_CHARS)
    revision_request = _required_text(
        revision_request_id, field="revision_request_id", max_chars=300
    )
    actor, org, canonical_namespace = _resolve_actor_scope(
        actor_user_id=actor_user_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    supplied_reference_values = {
        "contributor_concept_ids": contributor_concept_ids,
        "target_concept_ids": target_concept_ids,
        "audience_concept_ids": audience_concept_ids,
        "beneficiary_concept_ids": beneficiary_concept_ids,
        "purpose_concept_ids": purpose_concept_ids,
    }

    with override_current_actor(actor, org):
        doc = ConceptsRepository.find_one({"concept_id": resolved_id})
        if not isinstance(doc, Mapping):
            raise LearningCandidateNotFoundError("Learning candidate not found")
        state = _candidate_state(doc)
        source = state.get("source")
        if not isinstance(source, Mapping):
            raise InvalidLearningCandidateData(
                "Stored learning candidate source is invalid"
            )
        _normalise_visible_source(
            {
                "kind": source.get("kind"),
                **dict(source.get("locator") or {}),
            },
            actor_user_id=actor,
            organisation_concept_id=org,
            namespace=canonical_namespace,
        )
        normalised_supplied_references: dict[str, list[str] | None] = {}
        for field, supplied in supplied_reference_values.items():
            if supplied is None:
                normalised_supplied_references[field] = None
            else:
                normalised_supplied_references[field] = _normalise_concept_ids(
                    supplied,
                    field=field,
                    required=field in {"contributor_concept_ids", "target_concept_ids"},
                )

        def _references_for(snapshot: Mapping[str, Any]) -> dict[str, list[str]]:
            return {
                field: (
                    list(supplied)
                    if supplied is not None
                    else _normalise_concept_ids(
                        snapshot.get(field),
                        field=f"stored {field}",
                        required=field
                        in {"contributor_concept_ids", "target_concept_ids"},
                    )
                )
                for field, supplied in normalised_supplied_references.items()
            }

        if state.get("revision_request_id") == revision_request:
            current_request_references = _references_for(state)
            current_request_identity = _revision_identity_sha256(
                revision_request_id=revision_request,
                body=candidate_body,
                references=current_request_references,
            )
            if state.get("revision_identity_sha256") != current_request_identity:
                raise LearningCandidateConflictError(
                    "The revision request is already bound to different candidate data"
                )
            _validate_visible_references(current_request_references)
            # The state write deliberately precedes the relation write.  An
            # exact retry repairs an interrupted canonical-body write.
            with suppress_event_workflow_launches(
                "learning_candidate_non_active_revision_retry"
            ):
                _persist_body(
                    candidate_id=resolved_id,
                    body=candidate_body,
                    actor_user_id=actor,
                    organisation_concept_id=org,
                    revision=int(state.get("revision") or 1),
                )
            result = _project_candidate(
                doc,
                description_rows=get_texts_for_concept(
                    resolved_id,
                    predicate="hasDescription",
                    lang="en-NZ",
                    recent_first=True,
                ),
                validate_source=True,
                actor_user_id=actor,
                organisation_concept_id=org,
                namespace=canonical_namespace,
            )
            result["revised"] = False
            result["idempotent"] = True
            return result

        prior_revisions = copy.deepcopy(list(state.get("prior_revisions") or []))
        for prior in prior_revisions:
            if (
                not isinstance(prior, Mapping)
                or prior.get("revision_request_id") != revision_request
            ):
                continue
            prior_request_references = _references_for(prior)
            prior_request_identity = _revision_identity_sha256(
                revision_request_id=revision_request,
                body=candidate_body,
                references=prior_request_references,
            )
            if prior.get("revision_identity_sha256") != prior_request_identity:
                raise LearningCandidateConflictError(
                    "The revision request is already bound to different candidate data"
                )
            # A delayed exact retry acknowledges the historical request but
            # returns the current candidate.  It must not reapply an old body.
            result = _project_candidate(
                doc,
                description_rows=get_texts_for_concept(
                    resolved_id,
                    predicate="hasDescription",
                    lang="en-NZ",
                    recent_first=True,
                ),
                validate_source=True,
                actor_user_id=actor,
                organisation_concept_id=org,
                namespace=canonical_namespace,
            )
            result["revised"] = False
            result["idempotent"] = True
            result["replayed_revision"] = prior.get("revision")
            return result

        revised_references = _references_for(state)
        _validate_visible_references(revised_references)
        revision_identity_sha256 = _revision_identity_sha256(
            revision_request_id=revision_request,
            body=candidate_body,
            references=revised_references,
        )

        existing_rows = get_texts_for_concept(
            resolved_id,
            predicate="hasDescription",
            lang="en-NZ",
            recent_first=True,
        )
        current_body = _canonical_body_from_rows(existing_rows)
        if _body_digest(current_body) != state.get("body_sha256"):
            raise InvalidLearningCandidateData(
                "Learning candidate body does not match its canonical digest"
            )

        current_revision = int(state.get("revision") or 0)
        if current_revision < 1:
            raise InvalidLearningCandidateData(
                "Stored learning candidate revision is invalid"
            )
        now = _now()
        prior_revisions.append(
            {
                "revision": current_revision,
                "revision_request_id": state.get("revision_request_id"),
                "revision_identity_sha256": state.get("revision_identity_sha256"),
                "body": current_body,
                "body_sha256": state.get("body_sha256"),
                "contributor_concept_ids": list(
                    state.get("contributor_concept_ids") or []
                ),
                "target_concept_ids": list(state.get("target_concept_ids") or []),
                "audience_concept_ids": list(state.get("audience_concept_ids") or []),
                "beneficiary_concept_ids": list(
                    state.get("beneficiary_concept_ids") or []
                ),
                "purpose_concept_ids": list(state.get("purpose_concept_ids") or []),
                "valid_from": state.get("updated_at") or state.get("created_at"),
                "superseded_at": _iso(now),
                "superseded_by_actor_concept_id": actor,
                "superseded_by_organisation_concept_id": org,
                "superseded_by_revision_request_id": revision_request,
            }
        )
        revised_state = {
            **state,
            **revised_references,
            # These invariants are restated on every revision rather than being
            # accepted from mutable caller input.
            "lifecycle_state": LEARNING_CANDIDATE_LIFECYCLE_STATE,
            "author_concept_id": LEARNING_CANDIDATE_AUTHOR_ID,
            "body_sha256": _body_digest(candidate_body),
            "revision": current_revision + 1,
            "revision_request_id": revision_request,
            "revision_identity_sha256": revision_identity_sha256,
            "last_revised_by_actor_concept_id": actor,
            "last_revised_by_organisation_concept_id": org,
            "prior_revisions": prior_revisions,
            "updated_at": _iso(now),
        }
        with suppress_event_workflow_launches("learning_candidate_non_active_revision"):
            update_result = ConceptsRepository.update_one(
                {
                    "concept_id": resolved_id,
                    "concept_data.learning_candidate.revision": current_revision,
                    "concept_data.learning_candidate.lifecycle_state": (
                        LEARNING_CANDIDATE_LIFECYCLE_STATE
                    ),
                },
                {
                    "$set": {
                        "concept_data.learning_candidate": revised_state,
                        "attributes.learning_candidate.lifecycle_state": (
                            LEARNING_CANDIDATE_LIFECYCLE_STATE
                        ),
                        "updated_at": now,
                    }
                },
            )
            if int(getattr(update_result, "matched_count", 0) or 0) != 1:
                raise LearningCandidateConflictError(
                    "The learning candidate changed before this revision was stored"
                )
            _persist_body(
                candidate_id=resolved_id,
                body=candidate_body,
                actor_user_id=actor,
                organisation_concept_id=org,
                revision=current_revision + 1,
            )

        refreshed = ConceptsRepository.find_one({"concept_id": resolved_id})
        if not isinstance(refreshed, Mapping):
            raise LearningCandidateNotFoundError(
                "Learning candidate was not readable after revision"
            )
        result = _project_candidate(
            refreshed,
            description_rows=get_texts_for_concept(
                resolved_id,
                predicate="hasDescription",
                lang="en-NZ",
                recent_first=True,
            ),
            validate_source=True,
            actor_user_id=actor,
            organisation_concept_id=org,
            namespace=canonical_namespace,
        )
        result["revised"] = True
        result["idempotent"] = False
        return result


__all__ = [
    "LEARNING_CANDIDATE_ARTIFACT_TYPE_ID",
    "LEARNING_CANDIDATE_AUTHOR_ID",
    "LEARNING_CANDIDATE_LIFECYCLE_STATE",
    "LEARNING_CANDIDATE_SCHEMA_VERSION",
    "LEARNING_CANDIDATE_SYSTEM_TAG",
    "InvalidLearningCandidateData",
    "LearningCandidateAccessError",
    "LearningCandidateConflictError",
    "LearningCandidateError",
    "LearningCandidateNotFoundError",
    "capture_learning_candidate",
    "get_learning_candidate",
    "list_learning_candidates",
    "revise_learning_candidate",
]
