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
LEARNING_CANDIDATE_EVALUATION_DISPOSITIONS = frozenset(
    {"undecided", "retained", "rejected", "retracted"}
)
LEARNING_CANDIDATE_DISPOSITION_SCHEMA_VERSION = "learning_candidate_disposition.v1"
LEARNING_CANDIDATE_EVIDENCE_VERDICTS = frozenset(
    {"supports_use", "does_not_support_use", "inconclusive", "material_harm"}
)

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
_MAX_SOURCE_MESSAGE_COUNT = 32
_MAX_TRANSCRIPT_PAGE_SIZE = 100
_MAX_LIST_LIMIT = 200
_MAX_DISPOSITION_HISTORY = 100
_SHA256_HEX_CHARS = frozenset("0123456789abcdef")


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


def _required_sha256(value: Any, *, field: str) -> str:
    digest = _required_text(value, field=field, max_chars=64).lower()
    if len(digest) != 64 or any(char not in _SHA256_HEX_CHARS for char in digest):
        raise InvalidLearningCandidateData(f"{field} must be a SHA-256 digest")
    return digest


def _validate_learning_advice_evaluation_evidence(
    evaluation: Mapping[str, Any],
    *,
    planned_trial: Mapping[str, Any],
    model: Mapping[str, Any],
    runtime_snapshot: Mapping[str, Any],
) -> None:
    """Reject a typed verdict whose represented or model provenance is absent."""

    verdict = evaluation.get("verdict")
    if (
        evaluation.get("schema_version") != "learning_advice_blind_evaluation_result.v1"
        or evaluation.get("status") != "completed"
        or verdict not in {"pass", "partial", "fail", "inconclusive"}
        or evaluation.get("passed") is not (verdict == "pass")
        or evaluation.get("capability_choice")
        not in {"pass", "partial", "fail", "inconclusive"}
        or evaluation.get("work_product")
        not in {"pass", "partial", "fail", "inconclusive"}
        or not isinstance(evaluation.get("material_failure"), bool)
        or not isinstance(evaluation.get("external_availability_changed"), bool)
    ):
        raise InvalidLearningCandidateData(
            "The canonical experiment evaluation has no completed typed outcome"
        )
    # Import lazily: the experiment service uses this candidate service for its
    # canonical loader, while disposition validation runs only after both are
    # fully initialised.
    from .learning_advice_experiment_service import (
        LearningAdviceExperimentError,
        build_learning_advice_evaluator_runtime_identity,
        derive_learning_advice_evaluator_verdict,
    )

    try:
        expected_verdict = derive_learning_advice_evaluator_verdict(
            capability_choice=evaluation["capability_choice"],
            work_product=evaluation["work_product"],
            material_failure=evaluation["material_failure"],
        )
    except LearningAdviceExperimentError as exc:
        raise InvalidLearningCandidateData(
            "The canonical experiment evaluator dimensions are invalid"
        ) from exc
    if verdict != expected_verdict:
        raise InvalidLearningCandidateData(
            "The canonical experiment verdict contradicts its evaluator dimensions"
        )
    _required_sha256(
        evaluation.get("evaluator_output_sha256"),
        field="experiment evaluator_output_sha256",
    )
    for field in (
        "evaluator_concept_id",
        "evaluator_sha256",
        "rubric_concept_id",
        "rubric_sha256",
    ):
        if evaluation.get(field) != planned_trial.get(field):
            raise InvalidLearningCandidateData(
                "The canonical experiment evaluator authority contradicts its plan"
            )
    attestations = evaluation.get("authority_attestations")
    expected_attestation_refs = (
        (
            "evaluator",
            planned_trial.get("evaluator_concept_id"),
            planned_trial.get("evaluator_sha256"),
        ),
        (
            "rubric",
            planned_trial.get("rubric_concept_id"),
            planned_trial.get("rubric_sha256"),
        ),
    )
    if (
        not isinstance(attestations, Sequence)
        or isinstance(attestations, (str, bytes, bytearray))
        or len(attestations) != len(expected_attestation_refs)
    ):
        raise InvalidLearningCandidateData(
            "The canonical experiment has no exact evaluator authority attestations"
        )
    for attestation, (kind, concept_id, sha256) in zip(
        attestations, expected_attestation_refs, strict=True
    ):
        if (
            not isinstance(attestation, Mapping)
            or set(attestation)
            != {"kind", "concept_id", "sha256", "utf8_byte_count", "status"}
            or attestation.get("kind") != kind
            or attestation.get("concept_id") != concept_id
            or attestation.get("sha256") != sha256
            or not isinstance(attestation.get("utf8_byte_count"), int)
            or isinstance(attestation.get("utf8_byte_count"), bool)
            or int(attestation["utf8_byte_count"]) <= 0
            or attestation.get("status") != "canonical_bytes_attested"
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment evaluator authority attestation is invalid"
            )

    receipt = evaluation.get("model_call_receipt")
    expected_receipt_fields = {
        "schema_version",
        "call_id",
        "provider",
        "requested_model",
        "selected_model",
        "effective_model",
        "provider_observed_model",
        "provider_request_sent",
        "success",
    }
    expected_provider = model.get("provider")
    expected_model_id = model.get("model_id")
    if (
        not isinstance(receipt, Mapping)
        or set(receipt) != expected_receipt_fields
        or receipt.get("schema_version")
        != "learning_advice_evaluator_model_call_receipt.v1"
        or not isinstance(receipt.get("call_id"), str)
        or not str(receipt["call_id"]).strip()
        or receipt.get("provider") != expected_provider
        or any(
            receipt.get(field) != expected_model_id
            for field in (
                "requested_model",
                "selected_model",
                "effective_model",
                "provider_observed_model",
            )
        )
        or receipt.get("provider_request_sent") is not True
        or receipt.get("success") is not True
    ):
        raise InvalidLearningCandidateData(
            "The canonical experiment evaluator model receipt is invalid"
        )
    parameters = model.get("parameters")
    if not isinstance(parameters, Mapping):
        raise InvalidLearningCandidateData(
            "The canonical experiment evaluator model parameters are invalid"
        )
    runtime_sha256 = _stable_digest(runtime_snapshot)
    runtime_identity = build_learning_advice_evaluator_runtime_identity(
        model=model, runtime_snapshot=runtime_snapshot
    )
    expected_provenance = {
        "schema_version": "learning_advice_evaluator_provenance.v1",
        "model_call_id": receipt.get("call_id"),
        "provider": expected_provider,
        "requested_model": expected_model_id,
        "selected_model": expected_model_id,
        "effective_model": expected_model_id,
        "provider_observed_model": expected_model_id,
        "model_call_receipt_sha256": _stable_digest(receipt),
        "model_parameters_sha256": _stable_digest(parameters),
        "code_revision": runtime_snapshot.get("code_revision"),
        "runtime_snapshot_sha256": runtime_sha256,
        "runtime_code_identity_sha256": _stable_digest(runtime_identity),
        "evaluator_concept_id": planned_trial.get("evaluator_concept_id"),
        "evaluator_sha256": planned_trial.get("evaluator_sha256"),
        "rubric_concept_id": planned_trial.get("rubric_concept_id"),
        "rubric_sha256": planned_trial.get("rubric_sha256"),
    }
    if evaluation.get("evaluation_provenance") != expected_provenance:
        raise InvalidLearningCandidateData(
            "The canonical experiment evaluator provenance is invalid"
        )


def _evaluation_disposition(value: Any, *, field: str) -> str:
    disposition = _required_text(value, field=field, max_chars=40).lower()
    if disposition not in LEARNING_CANDIDATE_EVALUATION_DISPOSITIONS:
        raise InvalidLearningCandidateData(f"{field} is unsupported")
    return disposition


def _normalise_disposition_history(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise InvalidLearningCandidateData(
            "Stored learning candidate disposition history is invalid"
        )
    if len(value) > _MAX_DISPOSITION_HISTORY:
        raise InvalidLearningCandidateData(
            "Stored learning candidate disposition history is too long"
        )
    result: list[dict[str, Any]] = []
    seen_requests: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InvalidLearningCandidateData(
                f"Stored learning candidate disposition {index} is invalid"
            )
        record = copy.deepcopy(dict(item))
        if (
            record.get("schema_version")
            != LEARNING_CANDIDATE_DISPOSITION_SCHEMA_VERSION
        ):
            raise InvalidLearningCandidateData(
                f"Stored learning candidate disposition {index} has an "
                "unsupported schema"
            )
        request_id = _required_text(
            record.get("disposition_request_id"),
            field=f"stored disposition_history[{index}].disposition_request_id",
            max_chars=300,
        )
        if request_id in seen_requests:
            raise InvalidLearningCandidateData(
                "Stored learning candidate disposition requests are not unique"
            )
        seen_requests.add(request_id)
        normalised_disposition = _evaluation_disposition(
            record.get("evaluation_disposition"),
            field=f"stored disposition_history[{index}].evaluation_disposition",
        )
        if record.get("evaluation_disposition") != normalised_disposition:
            raise InvalidLearningCandidateData(
                f"stored disposition_history[{index}].evaluation_disposition "
                "is not canonical"
            )
        normalised_run_id = _concept_id(
            record.get("experiment_run_id"),
            field=f"stored disposition_history[{index}].experiment_run_id",
        )
        if record.get("experiment_run_id") != normalised_run_id:
            raise InvalidLearningCandidateData(
                f"stored disposition_history[{index}].experiment_run_id is "
                "not canonical"
            )
        normalised_evidence_sha256 = _required_sha256(
            record.get("evidence_sha256"),
            field=f"stored disposition_history[{index}].evidence_sha256",
        )
        if record.get("evidence_sha256") != normalised_evidence_sha256:
            raise InvalidLearningCandidateData(
                f"stored disposition_history[{index}].evidence_sha256 is not canonical"
            )
        verdict = _required_text(
            record.get("evidence_verdict"),
            field=f"stored disposition_history[{index}].evidence_verdict",
            max_chars=60,
        ).lower()
        if verdict not in LEARNING_CANDIDATE_EVIDENCE_VERDICTS:
            raise InvalidLearningCandidateData(
                f"stored disposition_history[{index}].evidence_verdict is unsupported"
            )
        if record.get("evidence_verdict") != verdict:
            raise InvalidLearningCandidateData(
                f"stored disposition_history[{index}].evidence_verdict is not canonical"
            )
        revision = record.get("candidate_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise InvalidLearningCandidateData(
                f"stored disposition_history[{index}].candidate_revision is invalid"
            )
        _required_sha256(
            record.get("candidate_revision_identity_sha256"),
            field=(
                f"stored disposition_history[{index}]."
                "candidate_revision_identity_sha256"
            ),
        )
        identity_payload = {
            "disposition_request_id": request_id,
            "evaluation_disposition": record.get("evaluation_disposition"),
            "evidence_verdict": verdict,
            "experiment_run_id": record.get("experiment_run_id"),
            "evidence_sha256": record.get("evidence_sha256"),
            "candidate_revision": revision,
            "candidate_revision_identity_sha256": record.get(
                "candidate_revision_identity_sha256"
            ),
        }
        if _required_sha256(
            record.get("disposition_identity_sha256"),
            field=f"stored disposition_history[{index}].disposition_identity_sha256",
        ) != _stable_digest(identity_payload):
            raise InvalidLearningCandidateData(
                f"stored disposition_history[{index}] identity does not match"
            )
        result.append(record)
    return result


def _normalise_history_indices(source: Mapping[str, Any]) -> list[int]:
    """Return a bounded, stable set of exact transcript positions to attest."""

    values = source.get("history_indices")
    if values is None:
        return []
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise InvalidLearningCandidateData(
            "source.history_indices must be a list of non-negative integers"
        )
    result: list[int] = []
    seen: set[int] = set()
    for index, value in enumerate(values):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise InvalidLearningCandidateData(
                f"source.history_indices[{index}] must be a non-negative integer"
            )
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) > _MAX_SOURCE_MESSAGE_COUNT:
            raise InvalidLearningCandidateData(
                "source.history_indices must contain at most "
                f"{_MAX_SOURCE_MESSAGE_COUNT} distinct indices"
            )
    return result


def _normalise_include_legacy(
    source: Mapping[str, Any],
) -> tuple[bool, bool]:
    """Resolve the read boundary without changing identities of older retries."""

    if "include_legacy" not in source:
        return True, False
    value = source.get("include_legacy")
    if not isinstance(value, bool):
        raise InvalidLearningCandidateData("source.include_legacy must be a boolean")
    return value, True


def _normalise_include_execution_evidence(source: Mapping[str, Any]) -> bool:
    """Return whether exact assistant-turn evidence is part of this source.

    Execution evidence is deliberately opt-in.  Most conversation citations need
    only the selected transcript bytes, and must not acquire debug-record reads or
    a stronger source identity merely because that evidence happens to exist.
    """

    value = source.get("include_execution_evidence", False)
    if not isinstance(value, bool):
        raise InvalidLearningCandidateData(
            "source.include_execution_evidence must be a boolean"
        )
    return value


def _conversation_turn_execution_evidence(
    *,
    actor_user_id: str,
    session_id: str,
    namespace: str | None,
    include_legacy: bool,
    history_index: int,
) -> dict[str, str]:
    """Attest one exact stored assistant Turn Execution Record without copying it."""

    try:
        debug_entry = chat_history_service.get_chat_history_debug_entry(
            user_id=actor_user_id,
            session_id=session_id,
            history_index=history_index,
            namespace=namespace,
            include_legacy=include_legacy,
            hydrate_blob_refs=False,
        )
    except Exception as exc:
        raise LearningCandidateAccessError(
            "learning_candidate_source_execution_evidence_read_unavailable",
            "The cited assistant turn execution evidence could not be verified",
        ) from exc
    turn_execution_record = (
        debug_entry.get("turn_execution_record")
        if isinstance(debug_entry, Mapping)
        else None
    )
    if not isinstance(turn_execution_record, Mapping):
        raise InvalidLearningCandidateData(
            f"source.history_indices[{history_index}] assistant message has no "
            "exact turn execution record"
        )
    request_id = _required_text(
        turn_execution_record.get("request_id"),
        field=(
            f"source.history_indices[{history_index}] turn execution record request_id"
        ),
        max_chars=300,
    )
    debug_request_id = (
        str(debug_entry.get("request_id") or "").strip()
        if isinstance(debug_entry, Mapping)
        else ""
    )
    if debug_request_id and debug_request_id != request_id:
        raise InvalidLearningCandidateData(
            f"source.history_indices[{history_index}] turn execution record "
            "request_id does not match its debug entry"
        )
    return {
        "request_id": request_id,
        "turn_execution_record_sha256": _stable_digest(dict(turn_execution_record)),
    }


def _conversation_message_evidence(
    *,
    actor_user_id: str | None,
    session_id: str,
    namespace: str | None,
    include_legacy: bool,
    include_execution_evidence: bool,
    history_indices: Sequence[int],
) -> list[dict[str, Any]]:
    """Verify cited messages and freeze locators plus non-content attestations."""

    if not history_indices:
        return []
    if actor_user_id is None:
        raise LearningCandidateAccessError(
            "learning_candidate_conversation_owner_required",
            "Exact conversation-message sources require their trusted owner actor",
        )

    # Page by bounded windows instead of loading an unbounded transcript or
    # issuing one datastore query for every cited message.  A sparse citation
    # set may require more than one page, but never more than the bounded input.
    requested = sorted(set(history_indices))
    messages_by_index: dict[int, Mapping[str, Any]] = {}
    remaining = list(requested)
    try:
        while remaining:
            offset = remaining[0]
            window_end = min(offset + _MAX_TRANSCRIPT_PAGE_SIZE - 1, remaining[-1])
            page = chat_history_service.get_chat_history_transcript_page(
                user_id=actor_user_id,
                session_id=session_id,
                namespace=namespace,
                include_legacy=include_legacy,
                offset=offset,
                page_size=window_end - offset + 1,
            )
            if not isinstance(page, Mapping) or page.get("found") is not True:
                raise _source_not_visible()
            raw_messages = page.get("messages")
            if isinstance(raw_messages, Sequence) and not isinstance(
                raw_messages, (str, bytes, bytearray)
            ):
                for message in raw_messages:
                    if not isinstance(message, Mapping):
                        continue
                    source_locator = message.get("source_locator")
                    if not isinstance(source_locator, Mapping):
                        continue
                    history_index = source_locator.get("history_index")
                    if (
                        isinstance(history_index, int)
                        and not isinstance(history_index, bool)
                        and history_index in requested
                    ):
                        messages_by_index[history_index] = message
            remaining = [value for value in remaining if value > window_end]
    except LearningCandidateError:
        raise
    except Exception as exc:
        raise LearningCandidateAccessError(
            "learning_candidate_source_read_unavailable",
            "The cited conversation messages could not be verified",
        ) from exc

    evidence: list[dict[str, Any]] = []
    for history_index in history_indices:
        message = messages_by_index.get(history_index)
        if not isinstance(message, Mapping):
            raise InvalidLearningCandidateData(
                f"source.history_indices[{history_index}] is not a readable message"
            )
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role.strip():
            raise InvalidLearningCandidateData(
                f"source.history_indices[{history_index}] has no stable role"
            )
        if not isinstance(content, str):
            raise InvalidLearningCandidateData(
                f"source.history_indices[{history_index}] has no stable text content"
            )
        raw_locator = message.get("source_locator")
        raw_locator = dict(raw_locator) if isinstance(raw_locator, Mapping) else {}
        locator: dict[str, Any] = {
            "session_id": session_id,
            "history_index": history_index,
        }
        for field in ("turn_id", "message_id"):
            value = raw_locator.get(field) or message.get(field)
            if isinstance(value, str) and value.strip():
                locator[field] = value.strip()
        evidence_item: dict[str, Any] = {
            "source_locator": locator,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "role_sha256": hashlib.sha256(role.strip().encode("utf-8")).hexdigest(),
            "turn_identity_sha256": _stable_digest(locator),
        }
        if include_execution_evidence and role.strip().casefold() == "assistant":
            evidence_item["turn_execution_evidence"] = (
                _conversation_turn_execution_evidence(
                    actor_user_id=actor_user_id,
                    session_id=session_id,
                    namespace=namespace,
                    include_legacy=include_legacy,
                    history_index=history_index,
                )
            )
        evidence.append(evidence_item)
    return evidence


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
        history_indices = _normalise_history_indices(source)
        include_legacy, include_legacy_was_explicit = _normalise_include_legacy(source)
        include_execution_evidence = _normalise_include_execution_evidence(source)
        if include_execution_evidence and not history_indices:
            raise InvalidLearningCandidateData(
                "source.include_execution_evidence requires source.history_indices"
            )
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
            locator: dict[str, Any] = {
                "conversation_concept_id": source_id,
                "session_id": stored_session_id,
            }
            if include_legacy_was_explicit:
                locator["include_legacy"] = include_legacy
            if include_execution_evidence:
                locator["include_execution_evidence"] = True
            normalised_source: dict[str, Any] = {
                "kind": kind,
                "locator": locator,
            }
            message_evidence = _conversation_message_evidence(
                actor_user_id=actor_user_id,
                session_id=stored_session_id,
                namespace=namespace,
                include_legacy=include_legacy,
                include_execution_evidence=include_execution_evidence,
                history_indices=history_indices,
            )
            if message_evidence:
                normalised_source["message_evidence"] = message_evidence
            return (
                normalised_source,
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
                include_legacy=include_legacy,
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
        if include_legacy_was_explicit:
            locator["include_legacy"] = include_legacy
        if include_execution_evidence:
            locator["include_execution_evidence"] = True
        if source_namespace:
            locator["namespace"] = source_namespace
        # Keep this locator stable if a conversation concept is materialised
        # later.  A caller that already holds such a concept can use the exact
        # concept-source form above; session-only retries must not change identity.
        synthetic_source_doc = {
            "concept_id": None,
            "relationships": set_specific_to_user_values({}, [actor_user_id]),
        }
        normalised_source = {"kind": kind, "locator": locator}
        message_evidence = _conversation_message_evidence(
            actor_user_id=actor_user_id,
            session_id=supplied_session_id,
            namespace=source_namespace,
            include_legacy=include_legacy,
            include_execution_evidence=include_execution_evidence,
            history_indices=history_indices,
        )
        if message_evidence:
            normalised_source["message_evidence"] = message_evidence
        return normalised_source, synthetic_source_doc

    if "include_execution_evidence" in source:
        raise InvalidLearningCandidateData(
            "source.include_execution_evidence is supported only for conversation sources"
        )

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


def _stored_source_validation_input(source: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct capture input needed to re-attest a stored source."""

    locator = source.get("locator")
    if not isinstance(locator, Mapping):
        raise InvalidLearningCandidateData("Stored candidate source is invalid")
    result = {"kind": source.get("kind"), **dict(locator)}
    message_evidence = source.get("message_evidence")
    if message_evidence is None:
        return result
    if not isinstance(message_evidence, Sequence) or isinstance(
        message_evidence, (str, bytes, bytearray)
    ):
        raise InvalidLearningCandidateData(
            "Stored learning candidate message evidence is invalid"
        )
    history_indices: list[int] = []
    for index, evidence in enumerate(message_evidence):
        if not isinstance(evidence, Mapping):
            raise InvalidLearningCandidateData(
                f"Stored learning candidate message evidence {index} is invalid"
            )
        evidence_locator = evidence.get("source_locator")
        if not isinstance(evidence_locator, Mapping):
            raise InvalidLearningCandidateData(
                f"Stored learning candidate message evidence {index} has no locator"
            )
        history_index = evidence_locator.get("history_index")
        if (
            not isinstance(history_index, int)
            or isinstance(history_index, bool)
            or history_index < 0
        ):
            raise InvalidLearningCandidateData(
                f"Stored learning candidate message evidence {index} has an "
                "invalid index"
            )
        history_indices.append(history_index)
    if not history_indices:
        raise InvalidLearningCandidateData(
            "Stored learning candidate message evidence is empty"
        )
    result["history_indices"] = history_indices
    return result


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
    current_disposition = _evaluation_disposition(
        state.get("evaluation_disposition") or "undecided",
        field="stored evaluation_disposition",
    )
    disposition_history = _normalise_disposition_history(
        state.get("disposition_history")
    )
    latest_disposition = disposition_history[-1] if disposition_history else None
    latest_is_current = bool(
        isinstance(latest_disposition, Mapping)
        and latest_disposition.get("candidate_revision") == state.get("revision")
    )
    if latest_is_current and (
        latest_disposition.get("evaluation_disposition") != current_disposition
    ):
        raise InvalidLearningCandidateData(
            "Stored learning candidate disposition does not match its history"
        )
    if current_disposition != "undecided" and not latest_is_current:
        raise InvalidLearningCandidateData(
            "Stored learning candidate disposition has no current evidence record"
        )
    state["evaluation_disposition"] = current_disposition
    state["disposition_history"] = disposition_history
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
        revalidated_source, _source_doc = _normalise_visible_source(
            _stored_source_validation_input(source),
            actor_user_id=actor_user_id,
            organisation_concept_id=organisation_concept_id,
            namespace=namespace,
        )
        if _stable_digest(revalidated_source) != _stable_digest(source):
            raise InvalidLearningCandidateData(
                "Stored learning candidate message evidence no longer matches "
                "the visible source transcript"
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
        "evaluation_disposition": state.get("evaluation_disposition"),
        "disposition_history": copy.deepcopy(
            list(state.get("disposition_history") or [])
        ),
        "body": body,
        "body_sha256": state.get("body_sha256"),
        "revision_identity_sha256": state.get("revision_identity_sha256"),
        "source_locator_sha256": state.get("source_locator_sha256"),
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
        identity_mode="exact",
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
    explicit_stable_key = _optional_text(
        idempotency_key, field="idempotency_key", max_chars=300
    )
    stable_key = explicit_stable_key or request
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
            # A server-bound turn ID records the concrete capture attempt, but
            # an explicit idempotency key deliberately spans attempts. Do not
            # make cross-turn retries conflict solely because their attempt
            # provenance differs.
            request_id=None if explicit_stable_key is not None else request,
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
            "evaluation_disposition": "undecided",
            "disposition_history": [],
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
                        "The idempotency identity is already bound to different "
                        "candidate data"
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
        revalidated_source, _source_doc = _normalise_visible_source(
            _stored_source_validation_input(source),
            actor_user_id=actor,
            organisation_concept_id=org,
            namespace=canonical_namespace,
        )
        if _stable_digest(revalidated_source) != _stable_digest(source):
            raise InvalidLearningCandidateData(
                "Stored learning candidate message evidence no longer matches "
                "the visible source transcript"
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
                "evaluation_disposition": state.get("evaluation_disposition"),
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
            # A changed semantic revision is a new hypothesis. Historical
            # dispositions remain inspectable, but cannot automatically carry
            # evidence forward to the new bytes.
            "evaluation_disposition": "undecided",
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
                    "concept_data.learning_candidate.updated_at": state.get(
                        "updated_at"
                    ),
                    "concept_data.learning_candidate.evaluation_disposition": (
                        state.get("evaluation_disposition")
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


def record_learning_candidate_disposition(
    candidate_id: str,
    *,
    experiment_run_id: str,
    disposition_request_id: str,
    actor_user_id: str | None = None,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Derive and attach one evidence-linked use disposition to this revision.

    A disposition changes whether the same bytes remain eligible for an
    experimental projection; it never activates them. The experiment run owns
    detailed observations. This service reads its canonical final result and
    derives the disposition; callers cannot independently assert a verdict or
    supply an unrelated digest.
    """

    resolved_id = _concept_id(candidate_id, field="candidate_id")
    run_id = _concept_id(experiment_run_id, field="experiment_run_id")
    request_id = _required_text(
        disposition_request_id,
        field="disposition_request_id",
        max_chars=300,
    )
    actor, org, canonical_namespace = _resolve_actor_scope(
        actor_user_id=actor_user_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )

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
        revalidated_source, _source_doc = _normalise_visible_source(
            _stored_source_validation_input(source),
            actor_user_id=actor,
            organisation_concept_id=org,
            namespace=canonical_namespace,
        )
        if _stable_digest(revalidated_source) != _stable_digest(source):
            raise InvalidLearningCandidateData(
                "Stored learning candidate message evidence no longer matches "
                "the visible source transcript"
            )
        revision = state.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise InvalidLearningCandidateData(
                "Stored learning candidate revision is invalid"
            )
        revision_identity = _required_sha256(
            state.get("revision_identity_sha256"),
            field="stored revision_identity_sha256",
        )

        # Import lazily to keep the candidate persistence primitive independent
        # of the broader experiment machinery at module import time.
        from .experiment_run_service import get_experiment_run_state
        from .learning_advice_experiment_service import (
            LearningAdviceExperimentError,
            compute_learning_advice_paired_results,
            evaluate_learning_advice_content_decision,
            validate_learning_advice_experiment_run_binding,
        )
        from .turn_execution_record_service import (
            get_turn_execution_record_projection,
            turn_execution_record_evidence_sha256,
        )

        experiment_state = get_experiment_run_state(run_id)
        if not isinstance(experiment_state, Mapping):
            raise InvalidLearningCandidateData(
                "The canonical experiment run is not visible"
            )
        expected_scope = {
            "run_id": run_id,
            "namespace": canonical_namespace,
            "user_id": actor,
            "org_id": org,
        }
        for field, expected in expected_scope.items():
            if experiment_state.get(field) != expected:
                raise InvalidLearningCandidateData(
                    "The canonical experiment run scope does not match the candidate"
                )
        terminal_status = experiment_state.get("status")
        generic_verdict = experiment_state.get("verdict")
        completed_at = _iso(experiment_state.get("completed_at_utc"))
        if (
            terminal_status not in {"completed", "failed"}
            or completed_at is None
            or (terminal_status == "failed" and generic_verdict != "fail")
            or (
                terminal_status == "completed"
                and generic_verdict not in {"pass", "partial", "inconclusive"}
            )
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment run is not in a valid terminal state"
            )
        observations = experiment_state.get("observations")
        if not isinstance(observations, Sequence) or isinstance(
            observations, (str, bytes, bytearray)
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment run has no inspectable observations"
            )
        result_observations = [
            item
            for item in observations
            if isinstance(item, Mapping)
            and item.get("observation_type") == "learning_advice_experiment_result"
        ]
        if len(result_observations) != 1:
            raise InvalidLearningCandidateData(
                "The canonical experiment run must contain exactly one final result"
            )
        result_observation = result_observations[0]
        evidence = result_observation.get("evidence")
        result = (
            evidence.get("learning_advice_result")
            if isinstance(evidence, Mapping)
            else None
        )
        if not isinstance(result, Mapping) or result.get("schema_version") != (
            "learning_advice_experiment_result.v1"
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment result is invalid"
            )
        expected_result_fields = {
            "schema_version",
            "experiment_spec_id",
            "experiment_run_id",
            "manifest_sha256",
            "plan_sha256",
            "candidate_ref",
            "runtime_snapshot",
            "acting_support_identity",
            "planned_trial_count",
            "executed_trial_count",
            "persisted_ter_count",
            "persisted_trial_observation_count",
            "valid_trial_count",
            "completed_evaluation_count",
            "paired_results",
            "content_decision",
            "secondary_metrics",
            "trial_observation_ids",
            "result_evidence_sha256",
        }
        if set(result) != expected_result_fields:
            raise InvalidLearningCandidateData(
                "The canonical experiment result fields are invalid"
            )
        _required_sha256(
            result.get("manifest_sha256"),
            field="experiment manifest_sha256",
        )
        _required_sha256(result.get("plan_sha256"), field="experiment plan_sha256")
        paired_results = result.get("paired_results")
        if not all(
            isinstance(result.get(field), Mapping)
            for field in (
                "runtime_snapshot",
                "acting_support_identity",
                "content_decision",
                "secondary_metrics",
            )
        ) or not isinstance(paired_results, Mapping):
            raise InvalidLearningCandidateData(
                "The canonical experiment result structure is invalid"
            )
        if set(paired_results) != {
            "pair_count",
            "valid_pair_count",
            "comparison",
            "outcome_counts",
            "applicable",
            "controls",
            "b_only_material_failure_count",
            "pairs",
        }:
            raise InvalidLearningCandidateData(
                "The canonical paired experiment result fields are invalid"
            )
        pairs = paired_results.get("pairs")
        if (
            not isinstance(pairs, Sequence)
            or isinstance(pairs, (str, bytes, bytearray))
            or not isinstance(paired_results.get("comparison"), str)
            or not all(
                isinstance(paired_results.get(field), Mapping)
                for field in ("outcome_counts", "applicable", "controls")
            )
        ):
            raise InvalidLearningCandidateData(
                "The canonical paired experiment result structure is invalid"
            )
        result_payload = copy.deepcopy(dict(result))
        evidence_digest = _required_sha256(
            result_payload.pop("result_evidence_sha256", None),
            field="experiment result_evidence_sha256",
        )
        if _stable_digest(result_payload) != evidence_digest:
            raise InvalidLearningCandidateData(
                "The canonical experiment result digest does not match"
            )
        if result.get("experiment_run_id") != run_id or result.get(
            "experiment_spec_id"
        ) != experiment_state.get("experiment_spec_id"):
            raise InvalidLearningCandidateData(
                "The canonical experiment result is bound to a different run"
            )
        frozen_ref = result.get("candidate_ref")
        expected_ref = {
            "candidate_id": resolved_id,
            "revision": revision,
            "body_sha256": state.get("body_sha256"),
            "revision_identity_sha256": revision_identity,
            "source_locator_sha256": state.get("source_locator_sha256"),
        }
        if (
            not isinstance(frozen_ref, Mapping)
            or {field: frozen_ref.get(field) for field in expected_ref} != expected_ref
            or set(frozen_ref)
            != {
                *expected_ref,
                "evaluation_disposition",
            }
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment result targets a different candidate revision"
            )
        frozen_disposition = frozen_ref.get("evaluation_disposition")
        if frozen_disposition not in {"undecided", "retained"}:
            raise InvalidLearningCandidateData(
                "The experiment used an ineligible candidate disposition"
            )
        metadata = experiment_state.get("metadata")
        raw_binding = (
            metadata.get("learning_advice_experiment")
            if isinstance(metadata, Mapping)
            else None
        )
        try:
            run_binding = validate_learning_advice_experiment_run_binding(raw_binding)
        except LearningAdviceExperimentError as exc:
            raise InvalidLearningCandidateData(
                "The canonical experiment run is not bound to inspectable frozen evidence"
            ) from exc
        manifest_preimage = run_binding["body_free_manifest_preimage"]
        plan_preimage = run_binding["body_free_plan_preimage"]
        expected_binding_values = {
            "experiment_spec_id": result.get("experiment_spec_id"),
            "experiment_run_id": run_id,
            "manifest_sha256": result.get("manifest_sha256"),
            "plan_sha256": result.get("plan_sha256"),
            "candidate_ref": frozen_ref,
            "trusted_scope_sha256": _stable_digest(
                {
                    "actor_user_id": actor,
                    "organisation_concept_id": org,
                    "namespace": canonical_namespace,
                }
            ),
            "runtime_snapshot_sha256": _stable_digest(result.get("runtime_snapshot")),
        }
        if any(
            run_binding.get(field) != expected
            for field, expected in expected_binding_values.items()
        ) or manifest_preimage.get("runtime_snapshot") != result.get(
            "runtime_snapshot"
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment run binding contradicts its result"
            )
        canonical_plan_trials = plan_preimage.get("trials")
        if not isinstance(canonical_plan_trials, Sequence) or isinstance(
            canonical_plan_trials, (str, bytes, bytearray)
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment run has no inspectable frozen plan"
            )
        planned_count = result.get("planned_trial_count")
        count_fields = (
            "executed_trial_count",
            "persisted_ter_count",
            "persisted_trial_observation_count",
            "completed_evaluation_count",
        )
        if (
            not isinstance(planned_count, int)
            or isinstance(planned_count, bool)
            or planned_count <= 0
            or any(result.get(field) != planned_count for field in count_fields)
            or len(canonical_plan_trials) != planned_count
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment result does not evidence a complete run"
            )
        valid_trial_count = result.get("valid_trial_count")
        if (
            not isinstance(valid_trial_count, int)
            or isinstance(valid_trial_count, bool)
            or valid_trial_count != planned_count
            or paired_results.get("pair_count") != len(pairs)
            or len(pairs) * 2 != planned_count
            or not isinstance(paired_results.get("valid_pair_count"), int)
            or isinstance(paired_results.get("valid_pair_count"), bool)
            or paired_results.get("valid_pair_count")
            != paired_results.get("pair_count")
            or not isinstance(paired_results.get("b_only_material_failure_count"), int)
            or isinstance(paired_results.get("b_only_material_failure_count"), bool)
            or paired_results.get("b_only_material_failure_count") < 0
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment result trial totals are invalid"
            )
        trial_observation_ids = result.get("trial_observation_ids")
        if (
            not isinstance(trial_observation_ids, Sequence)
            or isinstance(trial_observation_ids, (str, bytes, bytearray))
            or len(trial_observation_ids) != planned_count
            or any(
                not isinstance(item, str) or not item.strip()
                for item in trial_observation_ids
            )
            or len(set(trial_observation_ids)) != planned_count
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment trial observation index is invalid"
            )
        persisted_observation_ids = {
            item.get("observation_id")
            for item in observations
            if isinstance(item, Mapping)
            and item.get("observation_type") == "learning_advice_trial"
            and isinstance(item.get("evidence"), Mapping)
            and isinstance(item["evidence"].get("learning_advice_trial"), Mapping)
        }
        if set(trial_observation_ids) != persisted_observation_ids:
            raise InvalidLearningCandidateData(
                "The canonical experiment result does not read back all trial evidence"
            )
        ordered_trial_observations = [
            item
            for item in observations
            if isinstance(item, Mapping)
            and item.get("observation_type") == "learning_advice_trial"
        ]
        if (
            len(observations) != planned_count + 1
            or len(ordered_trial_observations) != planned_count
            or [item.get("observation_id") for item in ordered_trial_observations]
            != list(trial_observation_ids)
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment run contains foreign or reordered evidence"
            )
        expected_request_ids: list[str] = []
        canonical_trial_rows: list[Mapping[str, Any]] = []
        for trial_index, item in enumerate(ordered_trial_observations):
            item_evidence = item.get("evidence")
            trial = (
                item_evidence.get("learning_advice_trial")
                if isinstance(item_evidence, Mapping)
                else None
            )
            planned_trial = canonical_plan_trials[trial_index]
            evaluation = trial.get("evaluation") if isinstance(trial, Mapping) else None
            execution = trial.get("execution") if isinstance(trial, Mapping) else None
            execution_identity = (
                trial.get("execution_identity") if isinstance(trial, Mapping) else None
            )
            request_id_value = (
                execution_identity.get("request_id")
                if isinstance(execution_identity, Mapping)
                else None
            )
            request_ids = (
                trial.get("turn_execution_request_ids")
                if isinstance(trial, Mapping)
                else None
            )
            envelope_request_ids = item.get("turn_execution_request_ids")
            if (
                not isinstance(trial, Mapping)
                or trial.get("schema_version")
                != "learning_advice_experiment_observation.v1"
                or trial.get("observation_kind") != "paired_trial"
                or trial.get("experiment_spec_id") != result.get("experiment_spec_id")
                or trial.get("experiment_run_id") != run_id
                or trial.get("manifest_sha256") != result.get("manifest_sha256")
                or trial.get("plan_sha256") != result.get("plan_sha256")
                or trial.get("candidate_ref") != frozen_ref
                or trial.get("runtime_snapshot") != result.get("runtime_snapshot")
                or trial.get("model") != manifest_preimage.get("model")
                or trial.get("trial_integrity_valid") is not True
                or not isinstance(evaluation, Mapping)
                or evaluation.get("status") != "completed"
                or not isinstance(execution, Mapping)
                or execution.get("acting_support_identity")
                != result.get("acting_support_identity")
                or not isinstance(request_id_value, str)
                or not request_id_value.strip()
                or request_ids != [request_id_value]
                or envelope_request_ids != [request_id_value]
            ):
                raise InvalidLearningCandidateData(
                    "The canonical experiment trial evidence is not bound to the result"
                )
            planned_identity_fields = (
                "trial_id",
                "pair_id",
                "case_id",
                "case_kind",
                "repeat",
                "arm",
            )
            if (
                not isinstance(planned_trial, Mapping)
                or any(
                    trial.get(field) != planned_trial.get(field)
                    for field in planned_identity_fields
                )
                or any(
                    evaluation.get(field) != planned_trial.get(field)
                    for field in (
                        "evaluator_concept_id",
                        "evaluator_sha256",
                        "rubric_concept_id",
                        "rubric_sha256",
                    )
                )
            ):
                raise InvalidLearningCandidateData(
                    "The canonical experiment trial evidence contradicts its frozen plan"
                )
            _validate_learning_advice_evaluation_evidence(
                evaluation,
                planned_trial=planned_trial,
                model=manifest_preimage["model"],
                runtime_snapshot=result["runtime_snapshot"],
            )
            ter_sha256 = _required_sha256(
                execution.get("turn_execution_record_sha256"),
                field="trial execution turn_execution_record_sha256",
            )
            try:
                turn_execution_record = get_turn_execution_record_projection(
                    request_id=request_id_value,
                    namespace=canonical_namespace,
                )
            except Exception as exc:
                raise InvalidLearningCandidateData(
                    "The canonical trial turn execution record is not readable"
                ) from exc
            execution_session_id = execution_identity.get("session_id")
            execution_turn_id = execution_identity.get("turn_id")
            expected_ter_scope = {
                "request_id": request_id_value,
                "session_id": execution_session_id,
                "namespace": canonical_namespace,
                "user_id": actor,
                "org_id": org,
            }
            prompt_projection = (
                turn_execution_record.get("prompt")
                if isinstance(turn_execution_record, Mapping)
                else None
            )
            final_response_projection = (
                turn_execution_record.get("final_response")
                if isinstance(turn_execution_record, Mapping)
                else None
            )
            aux_calls = (
                turn_execution_record.get("aux_llm_calls")
                if isinstance(turn_execution_record, Mapping)
                else None
            )
            trial_bindings = [
                aux
                for aux in aux_calls or []
                if isinstance(aux, Mapping)
                and aux.get("type") == "learning_advice_experiment_trial_binding"
            ]
            trial_binding = trial_bindings[0] if len(trial_bindings) == 1 else None
            if (
                not isinstance(turn_execution_record, Mapping)
                or turn_execution_record.get("schema_version")
                != "turn_execution_record.v1"
                or any(
                    turn_execution_record.get(field) != expected
                    for field, expected in expected_ter_scope.items()
                )
                or turn_execution_record_evidence_sha256(turn_execution_record)
                != ter_sha256
                or not isinstance(prompt_projection, Mapping)
                or prompt_projection.get("sha256")
                != planned_trial.get("prompt_utf8_sha256")
                or not isinstance(final_response_projection, Mapping)
                or final_response_projection.get("response_sha256")
                != execution.get("response_sha256")
                or not isinstance(trial_binding, Mapping)
                or trial_binding.get("schema_version")
                != "learning_advice_experiment_trial_execution.v1"
                or trial_binding.get("experiment_spec_id")
                != result.get("experiment_spec_id")
                or trial_binding.get("experiment_run_id") != run_id
                or trial_binding.get("manifest_sha256") != result.get("manifest_sha256")
                or trial_binding.get("plan_sha256") != result.get("plan_sha256")
                or trial_binding.get("trial_id") != planned_trial.get("trial_id")
                or trial_binding.get("pair_id") != planned_trial.get("pair_id")
                or trial_binding.get("case_id") != planned_trial.get("case_id")
                or trial_binding.get("repeat") != planned_trial.get("repeat")
                or trial_binding.get("arm") != planned_trial.get("arm")
                or trial_binding.get("turn_id") != execution_turn_id
            ):
                raise InvalidLearningCandidateData(
                    "The canonical trial turn execution record does not match its observation"
                )
            expected_request_ids.append(request_id_value)
            canonical_trial_rows.append(trial)
        if experiment_state.get("turn_execution_request_ids") != expected_request_ids:
            raise InvalidLearningCandidateData(
                "The canonical experiment turn-execution index does not match the trials"
            )
        try:
            recomputed_paired_results = compute_learning_advice_paired_results(
                canonical_trial_rows,
                required_applicable_pair_count=run_binding[
                    "required_applicable_pair_count"
                ],
                required_control_pair_count=run_binding["required_control_pair_count"],
            )
            recomputed_content_decision = evaluate_learning_advice_content_decision(
                recomputed_paired_results,
                decision_rule=run_binding["decision_rule"],
            )
        except LearningAdviceExperimentError as exc:
            raise InvalidLearningCandidateData(
                "The canonical experiment evidence cannot be independently recomputed"
            ) from exc
        if recomputed_paired_results != paired_results:
            raise InvalidLearningCandidateData(
                "The canonical paired experiment result was not derived from its trials"
            )
        decision_record = result.get("content_decision")
        if (
            not isinstance(decision_record, Mapping)
            or set(decision_record)
            != {"decision", "activation_authorised", "decision_rule", "gates"}
            or decision_record.get("activation_authorised") is not False
            or not isinstance(decision_record.get("decision_rule"), Mapping)
            or not isinstance(decision_record.get("gates"), Sequence)
            or isinstance(decision_record.get("gates"), (str, bytes, bytearray))
        ):
            raise InvalidLearningCandidateData(
                "The canonical experiment content decision is invalid"
            )
        if recomputed_content_decision != decision_record:
            raise InvalidLearningCandidateData(
                "The canonical experiment decision was not derived from its trials"
            )
        decision = decision_record.get("decision")
        if decision == "arm_b_content_win":
            disposition = "retained"
            verdict = "supports_use"
            expected_observation_verdict = "pass"
        elif decision == "arm_b_not_supported":
            disposition = "rejected"
            verdict = "does_not_support_use"
            expected_observation_verdict = "fail"
        elif decision == "inconclusive":
            raise InvalidLearningCandidateData(
                "An inconclusive experiment cannot change candidate disposition"
            )
        else:
            raise InvalidLearningCandidateData(
                "The canonical experiment content decision is unsupported"
            )
        if result_observation.get("verdict") != expected_observation_verdict:
            raise InvalidLearningCandidateData(
                "The experiment observation verdict contradicts its content decision"
            )
        identity_payload = {
            "disposition_request_id": request_id,
            "evaluation_disposition": disposition,
            "evidence_verdict": verdict,
            "experiment_run_id": run_id,
            "evidence_sha256": evidence_digest,
            "candidate_revision": revision,
            "candidate_revision_identity_sha256": revision_identity,
        }
        identity_sha256 = _stable_digest(identity_payload)
        history = _normalise_disposition_history(state.get("disposition_history"))
        for prior in history:
            if prior.get("disposition_request_id") != request_id:
                continue
            if prior.get("disposition_identity_sha256") != identity_sha256:
                raise LearningCandidateConflictError(
                    "The disposition request is already bound to different evidence"
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
            result["disposition_recorded"] = False
            result["idempotent"] = True
            result["replayed_disposition"] = copy.deepcopy(dict(prior))
            return result

        if state.get("evaluation_disposition") != frozen_disposition:
            raise LearningCandidateConflictError(
                "The learning candidate disposition changed after this experiment "
                "was frozen"
            )

        if len(history) >= _MAX_DISPOSITION_HISTORY:
            raise InvalidLearningCandidateData(
                "The learning candidate disposition history is full"
            )
        now = _now()
        record = {
            "schema_version": LEARNING_CANDIDATE_DISPOSITION_SCHEMA_VERSION,
            **identity_payload,
            "disposition_identity_sha256": identity_sha256,
            "recorded_by_actor_concept_id": actor,
            "recorded_by_organisation_concept_id": org,
            "recorded_at": _iso(now),
        }
        revised_state = {
            **state,
            "evaluation_disposition": disposition,
            "disposition_history": [*history, record],
            "last_revised_by_actor_concept_id": actor,
            "last_revised_by_organisation_concept_id": org,
            "updated_at": _iso(now),
        }
        with suppress_event_workflow_launches(
            "learning_candidate_non_active_disposition"
        ):
            update_result = ConceptsRepository.update_one(
                {
                    "concept_id": resolved_id,
                    "concept_data.learning_candidate.revision": revision,
                    "concept_data.learning_candidate.updated_at": state.get(
                        "updated_at"
                    ),
                    "concept_data.learning_candidate.evaluation_disposition": (
                        frozen_disposition
                    ),
                },
                {
                    "$set": {
                        "concept_data.learning_candidate": revised_state,
                        "updated_at": now,
                    }
                },
            )
        if int(getattr(update_result, "matched_count", 0) or 0) != 1:
            raise LearningCandidateConflictError(
                "The learning candidate changed before this disposition was stored"
            )

        refreshed = ConceptsRepository.find_one({"concept_id": resolved_id})
        if not isinstance(refreshed, Mapping):
            raise LearningCandidateNotFoundError(
                "Learning candidate was not readable after disposition"
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
        result["disposition_recorded"] = True
        result["idempotent"] = False
        return result


__all__ = [
    "LEARNING_CANDIDATE_ARTIFACT_TYPE_ID",
    "LEARNING_CANDIDATE_AUTHOR_ID",
    "LEARNING_CANDIDATE_DISPOSITION_SCHEMA_VERSION",
    "LEARNING_CANDIDATE_EVALUATION_DISPOSITIONS",
    "LEARNING_CANDIDATE_EVIDENCE_VERDICTS",
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
    "record_learning_candidate_disposition",
    "revise_learning_candidate",
]
