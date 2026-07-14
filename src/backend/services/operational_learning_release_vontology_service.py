"""Vontology-backed persistence for operational learning-release mechanics.

Represented workflows and prompts own proposal, evaluation, promotion, rejection,
and rollback policy.  This adapter only validates exact actor scope, performs
optimistic version/digest checks, delegates deterministic state transitions to
``operational_learning_release_service``, and persists/read-backs an auditable
JSON record through canonical concept and text-relation services.

The authenticated-approval registry is deliberately part of the protected
record envelope.  A self-consistent caller-provided receipt is not sufficient
for a live promotion or rollback: the exact approval must first have been
recorded by an authenticated actor and then found in this authoritative state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
from datetime import datetime, timezone
import json
import threading
from typing import Any
import uuid

from ..security.access_control import override_current_actor
from . import concept_service
from .concept_service import ConceptNotFoundError
from .namespace_service import (
    derive_actor_context_from_namespace,
    derive_namespace_for_actor,
    resolve_canonical_namespace,
)
from .operational_learning_release_service import (
    AUTHENTICATED_HUMAN_APPROVAL_RECEIPT_SCHEMA_VERSION,
    HUMAN_LEARNING_RELEASE_APPROVAL_SCHEMA_VERSION,
    LearningReleaseValidationError,
    build_empty_operational_learning_release_state,
    operational_learning_release_digest,
    promote_operational_learning_release_candidate,
    register_operational_learning_release_candidate,
    reject_operational_learning_release_candidate,
    rollback_operational_learning_release,
    validate_operational_learning_release_state,
)
from .text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)


OPERATIONAL_LEARNING_RELEASE_STATE_RECORD_SCHEMA_VERSION = (
    "operational_learning_release_state_record.v1"
)
AUTHENTICATED_HUMAN_APPROVAL_RECORD_SCHEMA_VERSION = (
    "authenticated_human_learning_release_approval_record.v1"
)
OPERATIONAL_LEARNING_RELEASE_CAMPAIGN_EVIDENCE_SCHEMA_VERSION = (
    "operational_learning_release_campaign_evidence.v1"
)
OPERATIONAL_LEARNING_RELEASE_CANDIDATE_EVALUATION_SCHEMA_VERSION = (
    "operational_learning_release_candidate_evaluation.v1"
)
REPRESENTED_LEARNING_CANDIDATE_CONTEXT_SCHEMA_VERSION = (
    "represented_learning_candidate_context.v1"
)
REPRESENTED_FAILURE_EVIDENCE_MATERIAL_AVAILABILITY_SCHEMA_VERSION = (
    "represented_failure_evidence_material_availability.v1"
)
REPRESENTED_ACTIVE_LEARNING_RELEASE_CONTEXT_SCHEMA_VERSION = (
    "represented_active_learning_release_context.v1"
)
REPRESENTED_LEARNING_RELEASE_BINDING_SCHEMA_VERSION = (
    "represented_learning_release_binding.v1"
)
OPERATIONAL_LEARNING_RELEASE_STATE_TYPE_ID = "#V#operational_learning_release_state"
OPERATIONAL_LEARNING_RELEASE_STATE_PREDICATE = "hasContent"

_MANAGED_BY = "operational_learning_release_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2575"
_LANGUAGE = "en-NZ"
_ACTIONS = frozenset({"promote", "reject", "rollback"})
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "state_concept_id",
        "namespace",
        "user_id",
        "org_id",
        "version",
        "state_sha256",
        "previous_state_sha256",
        "previous_record_sha256",
        "state",
        "authenticated_approval_records",
        "candidate_evaluation_records",
        "last_mutation",
        "updated_at",
        "record_sha256",
    }
)

_LOCKS_GUARD = threading.Lock()
_STATE_LOCKS: dict[str, threading.RLock] = {}
_WRITER_OWNER_TOKEN = f"operational-learning-release-{uuid.uuid4().hex}"
_WRITER_LEASE_NAME = "operational_learning_release_writer"


class LearningReleasePersistenceError(RuntimeError):
    """Typed failure for the Vontology persistence/readback boundary."""

    def __init__(
        self,
        code: str,
        *,
        details: Mapping[str, Any] | None = None,
        recovery_affordances: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.code = code
        self.details = copy.deepcopy(dict(details or {}))
        self.recovery_affordances = [
            copy.deepcopy(dict(item)) for item in (recovery_affordances or [])
        ]
        super().__init__(code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "operational_learning_release_persistence_error.v1",
            "error_code": self.code,
            "details": copy.deepcopy(self.details),
            "recovery_affordances": copy.deepcopy(self.recovery_affordances),
        }


class LearningReleaseStateConflictError(LearningReleasePersistenceError):
    """Optimistic version or digest mismatch with inspect/retry affordances."""

    def __init__(
        self,
        *,
        expected_version: int,
        expected_state_sha256: str,
        current_version: int,
        current_state_sha256: str,
        state_concept_id: str,
    ) -> None:
        super().__init__(
            "operational_learning_release_state_conflict",
            details={
                "expected_version": expected_version,
                "expected_state_sha256": expected_state_sha256,
                "current_version": current_version,
                "current_state_sha256": current_state_sha256,
                "state_concept_id": state_concept_id,
            },
            recovery_affordances=[
                {
                    "action_type": "read_latest_state",
                    "state_concept_id": state_concept_id,
                },
                {
                    "action_type": "retry_with_latest_version",
                    "current_version": current_version,
                    "current_state_sha256": current_state_sha256,
                },
            ],
        )


def _clean(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningReleasePersistenceError(code)
    return value.strip()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _require_sha256(value: Any, *, code: str) -> str:
    if not _is_sha256(value):
        raise LearningReleasePersistenceError(code)
    return str(value).lower()


def _utc_iso(value: datetime | None = None) -> str:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise LearningReleasePersistenceError(
            "learning_release_timestamp_timezone_required"
        )
    return resolved.astimezone(timezone.utc).isoformat()


def _validate_timestamp(value: Any, *, code: str) -> str:
    text = _clean(value, code=code)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise LearningReleasePersistenceError(code) from exc
    if parsed.tzinfo is None:
        raise LearningReleasePersistenceError(code)
    return text


def _scope(
    *,
    namespace: Any,
    user_id: Any,
    org_id: Any,
) -> dict[str, str]:
    resolved_namespace = _clean(
        namespace,
        code="operational_learning_release_namespace_required",
    )
    resolved_user = _clean(user_id, code="operational_learning_release_user_required")
    resolved_org = _clean(org_id, code="operational_learning_release_org_required")
    canonical = resolve_canonical_namespace(
        resolved_namespace,
        resolved_user,
        resolved_org,
    )
    derived = derive_namespace_for_actor(resolved_user, resolved_org)
    namespace_user, namespace_org = derive_actor_context_from_namespace(
        resolved_namespace
    )
    if (
        canonical != resolved_namespace
        or derived != resolved_namespace
        or namespace_user != resolved_user
        or namespace_org != resolved_org
    ):
        raise LearningReleasePersistenceError(
            "operational_learning_release_scope_mismatch",
            details={
                "namespace": resolved_namespace,
                "user_id": resolved_user,
                "org_id": resolved_org,
                "canonical_namespace": canonical,
                "derived_namespace": derived,
                "namespace_user_id": namespace_user,
                "namespace_org_id": namespace_org,
            },
        )
    return {
        "namespace": resolved_namespace,
        "user_id": resolved_user,
        "org_id": resolved_org,
    }


def operational_learning_release_state_concept_id(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
) -> str:
    """Return the deterministic concept ID for one exact actor scope."""

    resolved_scope = _scope(namespace=namespace, user_id=user_id, org_id=org_id)
    scope_sha256 = operational_learning_release_digest(resolved_scope)
    return f"#V#operational_learning_release_state_{scope_sha256[:24]}"


def _lock_for(state_concept_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(state_concept_id, threading.RLock())


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = concept_service.get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _record_without_digest(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key != "record_sha256"
    }


def _empty_record(scope: Mapping[str, str], state_concept_id: str) -> dict[str, Any]:
    state = build_empty_operational_learning_release_state()
    record: dict[str, Any] = {
        "schema_version": OPERATIONAL_LEARNING_RELEASE_STATE_RECORD_SCHEMA_VERSION,
        "state_concept_id": state_concept_id,
        **dict(scope),
        "version": 0,
        "state_sha256": operational_learning_release_digest(state),
        "previous_state_sha256": None,
        "previous_record_sha256": None,
        "state": state,
        "authenticated_approval_records": {},
        "candidate_evaluation_records": {},
        "last_mutation": None,
        "updated_at": None,
    }
    record["record_sha256"] = operational_learning_release_digest(
        _record_without_digest(record)
    )
    return record


def _validate_approval_record(
    raw_record: Any,
    *,
    approval_id: str,
    scope: Mapping[str, str],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_record, Mapping):
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_record_invalid"
        )
    record = copy.deepcopy(dict(raw_record))
    if record.get("schema_version") != (
        AUTHENTICATED_HUMAN_APPROVAL_RECORD_SCHEMA_VERSION
    ):
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_record_invalid"
        )
    approval = record.get("human_approval")
    if not isinstance(approval, Mapping) or approval.get("approval_id") != approval_id:
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_record_invalid"
        )
    if approval.get("schema_version") != HUMAN_LEARNING_RELEASE_APPROVAL_SCHEMA_VERSION:
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_record_invalid"
        )
    receipt = approval.get("authenticated_approval_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != (
        AUTHENTICATED_HUMAN_APPROVAL_RECEIPT_SCHEMA_VERSION
    ):
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_receipt_invalid"
        )
    receipt_without_digest = dict(receipt)
    observed_digest = receipt_without_digest.pop("receipt_sha256", None)
    if observed_digest != operational_learning_release_digest(receipt_without_digest):
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_receipt_hash_mismatch"
        )
    for field_name in (
        "approval_id",
        "approver_id",
        "action",
        "candidate_id",
        "candidate_release_sha256",
        "namespace",
        "user_id",
        "org_id",
        "approved_at",
    ):
        if approval.get(field_name) != receipt.get(field_name):
            raise LearningReleasePersistenceError(
                "authenticated_learning_release_approval_receipt_binding_mismatch",
                details={"field": field_name},
            )
    if approval.get("approved") is not True or approval.get("action") not in _ACTIONS:
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_record_invalid"
        )
    for field_name in ("namespace", "user_id", "org_id"):
        if approval.get(field_name) != scope[field_name]:
            raise LearningReleasePersistenceError(
                "authenticated_learning_release_approval_scope_mismatch",
                details={"field": field_name},
            )
    candidate_id = str(approval.get("candidate_id") or "")
    snapshots = state.get("candidate_snapshots")
    candidate = snapshots.get(candidate_id) if isinstance(snapshots, Mapping) else None
    if not isinstance(candidate, Mapping) or candidate.get("release_sha256") != (
        approval.get("candidate_release_sha256")
    ):
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_candidate_binding_invalid"
        )
    _validate_timestamp(
        approval.get("approved_at"),
        code="authenticated_learning_release_approval_timestamp_invalid",
    )
    _validate_timestamp(
        receipt.get("authenticated_at"),
        code="authenticated_learning_release_approval_timestamp_invalid",
    )
    record_digest_basis = dict(record)
    observed_record_digest = record_digest_basis.pop("record_sha256", None)
    if observed_record_digest != operational_learning_release_digest(
        record_digest_basis
    ):
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_record_hash_mismatch"
        )
    return record


def _validate_candidate_evaluation_record(
    raw_record: Any,
    *,
    evaluation_id: str,
    scope: Mapping[str, str],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_record, Mapping):
        raise LearningReleasePersistenceError(
            "operational_learning_release_candidate_evaluation_invalid"
        )
    record = copy.deepcopy(dict(raw_record))
    if (
        record.get("schema_version")
        != (OPERATIONAL_LEARNING_RELEASE_CANDIDATE_EVALUATION_SCHEMA_VERSION)
        or record.get("evaluation_id") != evaluation_id
    ):
        raise LearningReleasePersistenceError(
            "operational_learning_release_candidate_evaluation_invalid"
        )
    for field_name in ("namespace", "user_id", "org_id"):
        if record.get(field_name) != scope[field_name]:
            raise LearningReleasePersistenceError(
                "operational_learning_release_candidate_evaluation_scope_mismatch"
            )
    candidate_id = str(record.get("candidate_id") or "").strip()
    snapshots = state.get("candidate_snapshots")
    candidate = snapshots.get(candidate_id) if isinstance(snapshots, Mapping) else None
    if not isinstance(candidate, Mapping) or candidate.get("release_sha256") != (
        record.get("candidate_release_sha256")
    ):
        raise LearningReleasePersistenceError(
            "operational_learning_release_candidate_evaluation_binding_invalid"
        )
    _validate_timestamp(
        record.get("registered_at"),
        code="operational_learning_release_candidate_evaluation_timestamp_invalid",
    )
    digest_basis = dict(record)
    observed_digest = digest_basis.pop("record_sha256", None)
    if observed_digest != operational_learning_release_digest(digest_basis):
        raise LearningReleasePersistenceError(
            "operational_learning_release_candidate_evaluation_hash_mismatch"
        )
    return record


def _validate_record(
    raw_record: Any,
    *,
    scope: Mapping[str, str],
    state_concept_id: str,
) -> dict[str, Any]:
    if not isinstance(raw_record, Mapping):
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_record_invalid"
        )
    record = copy.deepcopy(dict(raw_record))
    if set(record) != _RECORD_FIELDS or record.get("schema_version") != (
        OPERATIONAL_LEARNING_RELEASE_STATE_RECORD_SCHEMA_VERSION
    ):
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_record_invalid"
        )
    if record.get("state_concept_id") != state_concept_id:
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_concept_mismatch"
        )
    for field_name in ("namespace", "user_id", "org_id"):
        if record.get(field_name) != scope[field_name]:
            raise LearningReleasePersistenceError(
                "operational_learning_release_state_scope_mismatch",
                details={"field": field_name},
            )
    version = record.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_version_invalid"
        )
    raw_state = record.get("state")
    if not isinstance(raw_state, Mapping):
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_record_invalid"
        )
    state = validate_operational_learning_release_state(raw_state)
    state_sha256 = _require_sha256(
        record.get("state_sha256"),
        code="operational_learning_release_state_digest_invalid",
    )
    if state_sha256 != operational_learning_release_digest(state):
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_digest_mismatch"
        )
    for field_name in ("previous_state_sha256", "previous_record_sha256"):
        value = record.get(field_name)
        if value is not None:
            _require_sha256(
                value,
                code="operational_learning_release_state_history_digest_invalid",
            )
    _validate_timestamp(
        record.get("updated_at"),
        code="operational_learning_release_state_timestamp_invalid",
    )
    if not isinstance(record.get("last_mutation"), Mapping):
        raise LearningReleasePersistenceError(
            "operational_learning_release_last_mutation_invalid"
        )
    approval_records = record.get("authenticated_approval_records")
    if not isinstance(approval_records, Mapping):
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_records_invalid"
        )
    validated_approvals: dict[str, Any] = {}
    for approval_id, approval_record in approval_records.items():
        if not isinstance(approval_id, str) or not approval_id.strip():
            raise LearningReleasePersistenceError(
                "authenticated_learning_release_approval_records_invalid"
            )
        validated_approvals[approval_id] = _validate_approval_record(
            approval_record,
            approval_id=approval_id,
            scope=scope,
            state=state,
        )
    record["state"] = state
    record["authenticated_approval_records"] = validated_approvals
    evaluation_records = record.get("candidate_evaluation_records")
    if not isinstance(evaluation_records, Mapping):
        raise LearningReleasePersistenceError(
            "operational_learning_release_candidate_evaluations_invalid"
        )
    validated_evaluations: dict[str, Any] = {}
    for evaluation_id, evaluation_record in evaluation_records.items():
        if not isinstance(evaluation_id, str) or not evaluation_id.strip():
            raise LearningReleasePersistenceError(
                "operational_learning_release_candidate_evaluations_invalid"
            )
        validated_evaluations[evaluation_id] = _validate_candidate_evaluation_record(
            evaluation_record,
            evaluation_id=evaluation_id,
            scope=scope,
            state=state,
        )
    record["candidate_evaluation_records"] = validated_evaluations
    observed_record_sha256 = _require_sha256(
        record.get("record_sha256"),
        code="operational_learning_release_record_digest_invalid",
    )
    if observed_record_sha256 != operational_learning_release_digest(
        _record_without_digest(record)
    ):
        raise LearningReleasePersistenceError(
            "operational_learning_release_record_digest_mismatch"
        )
    return record


def _validate_state_concept(
    concept: Mapping[str, Any],
    *,
    state_concept_id: str,
    scope: Mapping[str, str],
) -> None:
    if concept.get("concept_id") != state_concept_id:
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_concept_mismatch"
        )
    attributes = concept.get("attributes")
    represented_scope = (
        attributes.get("operational_learning_release_scope")
        if isinstance(attributes, Mapping)
        else None
    )
    if not isinstance(represented_scope, Mapping) or any(
        represented_scope.get(field_name) != scope[field_name]
        for field_name in ("namespace", "user_id", "org_id")
    ):
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_concept_scope_mismatch"
        )


def _load_raw_record(scope: Mapping[str, str]) -> tuple[dict[str, Any], bool]:
    state_concept_id = operational_learning_release_state_concept_id(**scope)
    concept = _safe_get_concept(state_concept_id)
    if concept is None:
        return _empty_record(scope, state_concept_id), False
    _validate_state_concept(
        concept,
        state_concept_id=state_concept_id,
        scope=scope,
    )
    rows = get_texts_for_concept(
        state_concept_id,
        predicate=OPERATIONAL_LEARNING_RELEASE_STATE_PREDICATE,
        lang=_LANGUAGE,
        limit=10,
    )
    texts = [
        str(row.get("text")).strip()
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("text"), str)
        and str(row.get("text")).strip()
    ]
    if len(texts) > 1:
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_record_ambiguous",
            details={"state_concept_id": state_concept_id, "record_count": len(texts)},
        )
    if not texts:
        return _empty_record(scope, state_concept_id), False
    try:
        raw_record = json.loads(texts[0])
    except (TypeError, ValueError) as exc:
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_record_malformed"
        ) from exc
    return (
        _validate_record(
            raw_record,
            scope=scope,
            state_concept_id=state_concept_id,
        ),
        True,
    )


def project_operational_learning_release_campaign_evidence(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Project exact learning-loop receipts without inventing pilot agreements."""

    storage_record = {
        field_name: copy.deepcopy(record.get(field_name))
        for field_name in _RECORD_FIELDS
    }
    scope = _scope(
        namespace=storage_record.get("namespace"),
        user_id=storage_record.get("user_id"),
        org_id=storage_record.get("org_id"),
    )
    state_concept_id = operational_learning_release_state_concept_id(**scope)
    if storage_record.get("state_concept_id") != state_concept_id:
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_concept_mismatch"
        )
    if storage_record.get("version") == 0:
        expected_empty = _empty_record(scope, state_concept_id)
        if storage_record != expected_empty:
            raise LearningReleasePersistenceError(
                "operational_learning_release_virtual_state_invalid"
            )
        validated_record = expected_empty
    else:
        validated_record = _validate_record(
            storage_record,
            scope=scope,
            state_concept_id=state_concept_id,
        )
    raw_state = validated_record.get("state")
    if not isinstance(raw_state, Mapping):
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_record_invalid"
        )
    state = validate_operational_learning_release_state(raw_state)
    receipts_by_id: dict[str, Mapping[str, Any]] = {}
    bindings: set[tuple[str, str]] = set()
    evaluation_records = validated_record.get("candidate_evaluation_records")
    for evaluation_record in (
        evaluation_records.values() if isinstance(evaluation_records, Mapping) else []
    ):
        if not isinstance(evaluation_record, Mapping):
            continue
        bindings.add(
            (
                str(evaluation_record.get("candidate_id") or ""),
                str(evaluation_record.get("candidate_release_sha256") or "").lower(),
            )
        )
    for receipt in state.get("decision_receipts") or []:
        if not isinstance(receipt, Mapping) or receipt.get("action") not in _ACTIONS:
            continue
        receipt_id = str(receipt.get("receipt_id") or "").strip()
        candidate_id = str(receipt.get("candidate_id") or "").strip()
        release_sha256 = str(receipt.get("release_sha256") or "").strip().lower()
        if not receipt_id or not candidate_id or not _is_sha256(release_sha256):
            raise LearningReleasePersistenceError(
                "operational_learning_release_receipt_projection_invalid"
            )
        receipts_by_id[receipt_id] = receipt
        bindings.add((candidate_id, release_sha256))
    candidate_bindings = [
        {
            "candidate_id": candidate_id,
            "candidate_release_sha256": release_sha256,
        }
        for candidate_id, release_sha256 in sorted(bindings)
    ]
    projection: dict[str, Any] = {
        "schema_version": (
            OPERATIONAL_LEARNING_RELEASE_CAMPAIGN_EVIDENCE_SCHEMA_VERSION
        ),
        "source": "vontology_operational_learning_release_state",
        "authority": {
            "state_concept_id": state_concept_id,
            "version": validated_record.get("version"),
            "state_sha256": validated_record.get("state_sha256"),
            "record_sha256": validated_record.get("record_sha256"),
        },
        **scope,
        "completed_learning_loop_count": len(receipts_by_id),
        "learning_release_receipt_ids": sorted(receipts_by_id),
        "evaluated_learning_release_candidate_bindings": candidate_bindings,
        "evaluated_learning_release_candidate_ids": sorted(
            {binding["candidate_id"] for binding in candidate_bindings}
        ),
    }
    projection["evidence_sha256"] = operational_learning_release_digest(projection)
    return projection


def _public_record(record: Mapping[str, Any], *, persisted: bool) -> dict[str, Any]:
    projection = copy.deepcopy(dict(record))
    projection["persisted"] = persisted
    projection["campaign_evidence_projection"] = (
        project_operational_learning_release_campaign_evidence(record)
    )
    return projection


def load_operational_learning_release_state(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
) -> dict[str, Any]:
    """Read and strictly validate one exact-scope learning-release record."""

    resolved_scope = _scope(namespace=namespace, user_id=user_id, org_id=org_id)
    record, persisted = _load_raw_record(resolved_scope)
    return _public_record(record, persisted=persisted)


def _release_context_authority(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "represented_learning_release_state_authority.v1",
        "state_concept_id": record.get("state_concept_id"),
        "version": record.get("version"),
        "state_sha256": record.get("state_sha256"),
        "record_sha256": record.get("record_sha256"),
    }


def _release_context_binding(
    *,
    candidate: Mapping[str, Any],
    candidate_snapshot_sha256: str,
    release_payload_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": REPRESENTED_LEARNING_RELEASE_BINDING_SCHEMA_VERSION,
        "candidate_id": candidate["candidate_id"],
        "release_sha256": candidate["release_sha256"],
        "affected_artifact": candidate["affected_artifact"],
        "namespace": candidate["namespace"],
        "user_id": candidate["user_id"],
        "org_id": candidate["org_id"],
        "candidate_snapshot_sha256": candidate_snapshot_sha256,
        "release_payload_sha256": release_payload_sha256,
    }


def _resolved_candidate_from_record(
    record: Mapping[str, Any],
    *,
    candidate_id: str,
    release_sha256: str,
    affected_artifact: str,
) -> tuple[dict[str, Any], str]:
    state = record.get("state")
    if not isinstance(state, Mapping):
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_record_invalid"
        )
    snapshots = state.get("candidate_snapshots")
    candidate = snapshots.get(candidate_id) if isinstance(snapshots, Mapping) else None
    if not isinstance(candidate, Mapping):
        raise LearningReleasePersistenceError(
            "operational_learning_release_candidate_not_found",
            details={"candidate_id": candidate_id},
            recovery_affordances=[{"action_type": "read_latest_state"}],
        )
    candidate_snapshot = copy.deepcopy(dict(candidate))
    mismatches = [
        field_name
        for field_name, expected in (
            ("release_sha256", release_sha256),
            ("affected_artifact", affected_artifact),
            ("namespace", record.get("namespace")),
            ("user_id", record.get("user_id")),
            ("org_id", record.get("org_id")),
        )
        if candidate_snapshot.get(field_name) != expected
    ]
    if mismatches:
        raise LearningReleasePersistenceError(
            "operational_learning_release_candidate_binding_mismatch",
            details={"candidate_id": candidate_id, "mismatches": mismatches},
            recovery_affordances=[{"action_type": "read_latest_state"}],
        )
    candidate_states = state.get("candidate_states")
    lifecycle = (
        candidate_states.get(candidate_id)
        if isinstance(candidate_states, Mapping)
        else None
    )
    lifecycle_state = (
        str(lifecycle.get("state") or "").strip()
        if isinstance(lifecycle, Mapping)
        else ""
    )
    if not lifecycle_state:
        raise LearningReleasePersistenceError(
            "operational_learning_release_candidate_lifecycle_missing",
            details={"candidate_id": candidate_id},
        )
    return candidate_snapshot, lifecycle_state


def _project_failure_evidence_material_availability(
    candidate_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose neutral structural facts about candidate failure evidence.

    The represented evaluator decides whether the available material is enough
    for a particular behavioural case.  This support projection only
    distinguishes digest locators from inline source material or a represented
    source-material reference; it does not decide an evaluation verdict.
    """

    raw_packets = candidate_snapshot.get("failure_evidence_packets")
    packets = (
        [item for item in raw_packets if isinstance(item, Mapping)]
        if isinstance(raw_packets, Sequence)
        and not isinstance(raw_packets, (str, bytes, bytearray))
        else []
    )
    member_count = 0
    member_digest_locator_count = 0
    member_inline_source_material_count = 0
    member_source_material_reference_count = 0
    member_source_material_count = 0
    for packet in packets:
        raw_members = packet.get("members")
        members = (
            [item for item in raw_members if isinstance(item, Mapping)]
            if isinstance(raw_members, Sequence)
            and not isinstance(raw_members, (str, bytes, bytearray))
            else []
        )
        for member in members:
            member_count += 1
            if (
                isinstance(member.get("source_evidence_sha256"), str)
                and str(member.get("source_evidence_sha256")).strip()
            ):
                member_digest_locator_count += 1
            inline_material = isinstance(
                member.get("source_evidence"), Mapping
            ) and bool(member.get("source_evidence"))
            material_reference = any(
                isinstance(member.get(key), Mapping) and bool(member.get(key))
                for key in ("source_evidence_ref", "source_evidence_blob_ref")
            )
            if inline_material:
                member_inline_source_material_count += 1
            if material_reference:
                member_source_material_reference_count += 1
            if inline_material or material_reference:
                member_source_material_count += 1

    if member_count == 0:
        coverage = "none"
    elif member_source_material_count == member_count:
        coverage = "source_material_present_for_all_members"
    elif member_source_material_count > 0:
        coverage = "partial_source_material"
    elif member_digest_locator_count == member_count:
        coverage = "digest_locators_only"
    else:
        coverage = "source_material_absent"

    return {
        "schema_version": (
            REPRESENTED_FAILURE_EVIDENCE_MATERIAL_AVAILABILITY_SCHEMA_VERSION
        ),
        "packet_count": len(packets),
        "member_count": member_count,
        "member_digest_locator_count": member_digest_locator_count,
        "member_inline_source_material_count": (member_inline_source_material_count),
        "member_source_material_reference_count": (
            member_source_material_reference_count
        ),
        "member_source_material_count": member_source_material_count,
        "all_members_have_source_material": bool(
            member_count > 0 and member_source_material_count == member_count
        ),
        "coverage": coverage,
    }


def resolve_operational_learning_release_candidate_in_vontology(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    candidate_id: str,
    release_sha256: str,
    affected_artifact: str,
) -> dict[str, Any]:
    """Resolve one immutable candidate and its opaque payload without mutation."""

    scope = _scope(namespace=namespace, user_id=user_id, org_id=org_id)
    resolved_candidate_id = _clean(
        candidate_id,
        code="operational_learning_release_candidate_id_required",
    )
    resolved_release_sha256 = _require_sha256(
        release_sha256,
        code="operational_learning_release_candidate_hash_required",
    )
    resolved_artifact = _clean(
        affected_artifact,
        code="operational_learning_release_affected_artifact_required",
    )
    record = load_operational_learning_release_state(**scope)
    candidate, lifecycle_state = _resolved_candidate_from_record(
        record,
        candidate_id=resolved_candidate_id,
        release_sha256=resolved_release_sha256,
        affected_artifact=resolved_artifact,
    )
    candidate_snapshot_sha256 = _require_sha256(
        candidate.get("candidate_sha256"),
        code="operational_learning_release_candidate_snapshot_hash_missing",
    )
    release_payload = copy.deepcopy(candidate.get("release_payload"))
    release_payload_sha256 = operational_learning_release_digest(release_payload)
    parent_release = candidate.get("parent_release")
    if parent_release is None:
        parent_release_sha256 = None
    elif isinstance(parent_release, Mapping):
        parent_release_sha256 = _require_sha256(
            parent_release.get("release_sha256"),
            code="operational_learning_release_parent_hash_missing",
        )
    else:
        raise LearningReleasePersistenceError(
            "operational_learning_release_parent_invalid"
        )
    binding = _release_context_binding(
        candidate=candidate,
        candidate_snapshot_sha256=candidate_snapshot_sha256,
        release_payload_sha256=release_payload_sha256,
    )
    candidate_context: dict[str, Any] = {
        "schema_version": REPRESENTED_LEARNING_CANDIDATE_CONTEXT_SCHEMA_VERSION,
        "status": "resolved",
        "candidate_id": resolved_candidate_id,
        "release_sha256": resolved_release_sha256,
        "affected_artifact": resolved_artifact,
        **scope,
        "candidate_lifecycle_state": lifecycle_state,
        "candidate_snapshot": candidate,
        "candidate_snapshot_sha256": candidate_snapshot_sha256,
        "failure_evidence_material_availability": (
            _project_failure_evidence_material_availability(candidate)
        ),
        "release_payload": release_payload,
        "release_payload_sha256": release_payload_sha256,
        "parent_release_sha256": parent_release_sha256,
        "authority": _release_context_authority(record),
        "binding": binding,
    }
    candidate_context["context_sha256"] = operational_learning_release_digest(
        candidate_context
    )
    return {"success": True, "result": {"candidate_context": candidate_context}}


def resolve_operational_learning_active_release_in_vontology(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    affected_artifact: str,
    expected_release_sha256: str | None = None,
) -> dict[str, Any]:
    """Resolve the exact actor-scoped active baseline without mutation."""

    scope = _scope(namespace=namespace, user_id=user_id, org_id=org_id)
    resolved_artifact = _clean(
        affected_artifact,
        code="operational_learning_release_affected_artifact_required",
    )
    expected_hash = (
        _require_sha256(
            expected_release_sha256,
            code="operational_learning_release_expected_active_hash_invalid",
        )
        if expected_release_sha256 is not None and str(expected_release_sha256).strip()
        else None
    )
    record = load_operational_learning_release_state(**scope)
    state = record.get("state")
    pointers = state.get("release_pointers") if isinstance(state, Mapping) else None
    pointer_state = (
        pointers.get(resolved_artifact) if isinstance(pointers, Mapping) else None
    )
    active_pointer = (
        pointer_state.get("active") if isinstance(pointer_state, Mapping) else None
    )
    authority = _release_context_authority(record)
    if not isinstance(active_pointer, Mapping):
        if expected_hash is not None:
            raise LearningReleasePersistenceError(
                "operational_learning_release_expected_active_not_found",
                details={
                    "affected_artifact": resolved_artifact,
                    "expected_release_sha256": expected_hash,
                },
                recovery_affordances=[{"action_type": "read_latest_state"}],
            )
        active_release: dict[str, Any] = {
            "schema_version": (
                REPRESENTED_ACTIVE_LEARNING_RELEASE_CONTEXT_SCHEMA_VERSION
            ),
            "status": "no_active_release",
            "affected_artifact": resolved_artifact,
            **scope,
            "authority": authority,
        }
        active_release["context_sha256"] = operational_learning_release_digest(
            active_release
        )
        return {"success": True, "result": {"active_release": active_release}}

    pointer_candidate_id = _clean(
        active_pointer.get("candidate_id"),
        code="operational_learning_release_active_candidate_id_missing",
    )
    pointer_release_sha256 = _require_sha256(
        active_pointer.get("release_sha256"),
        code="operational_learning_release_active_hash_missing",
    )
    if expected_hash is not None and pointer_release_sha256 != expected_hash:
        raise LearningReleasePersistenceError(
            "operational_learning_release_active_hash_mismatch",
            details={
                "affected_artifact": resolved_artifact,
                "expected_release_sha256": expected_hash,
                "observed_release_sha256": pointer_release_sha256,
            },
            recovery_affordances=[{"action_type": "read_latest_state"}],
        )
    candidate, lifecycle_state = _resolved_candidate_from_record(
        record,
        candidate_id=pointer_candidate_id,
        release_sha256=pointer_release_sha256,
        affected_artifact=resolved_artifact,
    )
    if lifecycle_state != "active":
        raise LearningReleasePersistenceError(
            "operational_learning_release_active_lifecycle_mismatch",
            details={
                "candidate_id": pointer_candidate_id,
                "lifecycle_state": lifecycle_state,
            },
        )
    candidate_snapshot_sha256 = _require_sha256(
        candidate.get("candidate_sha256"),
        code="operational_learning_release_candidate_snapshot_hash_missing",
    )
    release_payload = copy.deepcopy(candidate.get("release_payload"))
    release_payload_sha256 = operational_learning_release_digest(release_payload)
    active_release = {
        "schema_version": REPRESENTED_ACTIVE_LEARNING_RELEASE_CONTEXT_SCHEMA_VERSION,
        "status": "active_release_resolved",
        "candidate_id": pointer_candidate_id,
        "release_id": candidate.get("release_id"),
        "release_sha256": pointer_release_sha256,
        "affected_artifact": resolved_artifact,
        **scope,
        "candidate_lifecycle_state": lifecycle_state,
        "candidate_snapshot": candidate,
        "candidate_snapshot_sha256": candidate_snapshot_sha256,
        "release_payload": release_payload,
        "release_payload_sha256": release_payload_sha256,
        "authority": authority,
        "binding": _release_context_binding(
            candidate=candidate,
            candidate_snapshot_sha256=candidate_snapshot_sha256,
            release_payload_sha256=release_payload_sha256,
        ),
    }
    active_release["context_sha256"] = operational_learning_release_digest(
        active_release
    )
    return {"success": True, "result": {"active_release": active_release}}


def _ensure_state_concept(
    *,
    scope: Mapping[str, str],
    state_concept_id: str,
) -> None:
    state_type = _safe_get_concept(OPERATIONAL_LEARNING_RELEASE_STATE_TYPE_ID)
    if state_type is None:
        try:
            concept_service.create_concept(
                name="Operational learning release state",
                concept_id=OPERATIONAL_LEARNING_RELEASE_STATE_TYPE_ID,
                description=(
                    "Type for exact-scope operational learning-release state records. "
                    "Represented workflows own proposal and release policy."
                ),
                parent_concept_ids=["#V#thing"],
                create_as_instance=False,
                visibility_scope_mode="global_general",
            )
        except Exception:
            if _safe_get_concept(OPERATIONAL_LEARNING_RELEASE_STATE_TYPE_ID) is None:
                raise
    concept = _safe_get_concept(state_concept_id)
    if concept is None:
        try:
            concept_service.create_concept(
                name="Operational learning release state",
                concept_id=state_concept_id,
                description=(
                    "Exact actor-scoped persisted state and authenticated approval "
                    "registry for represented operational learning-release workflows."
                ),
                attributes={
                    "managed_by": _MANAGED_BY,
                    "schema_version": (
                        OPERATIONAL_LEARNING_RELEASE_STATE_RECORD_SCHEMA_VERSION
                    ),
                    "operational_learning_release_scope": dict(scope),
                },
                parent_concept_ids=[OPERATIONAL_LEARNING_RELEASE_STATE_TYPE_ID],
                create_as_instance=True,
                created_by_concept_id=scope["user_id"],
                organisation_concept_id=scope["org_id"],
                event_namespace=scope["namespace"],
            )
        except Exception:
            concept = _safe_get_concept(state_concept_id)
            if concept is None:
                raise
        else:
            concept = _safe_get_concept(state_concept_id)
    if concept is None:
        raise LearningReleasePersistenceError(
            "operational_learning_release_state_concept_create_failed"
        )
    _validate_state_concept(
        concept,
        state_concept_id=state_concept_id,
        scope=scope,
    )


def _expected_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LearningReleasePersistenceError(
            "operational_learning_release_expected_version_invalid"
        )
    return value


def _authoritative_approval(
    record: Mapping[str, Any],
    human_approval: Mapping[str, Any] | None,
    *,
    required: bool = False,
) -> Mapping[str, Any] | None:
    if human_approval is None:
        if required:
            raise LearningReleasePersistenceError(
                "authoritative_human_approval_required_for_live_release",
                recovery_affordances=[
                    {"action_type": "record_authenticated_human_approval"}
                ],
            )
        return None
    approval_id = str(human_approval.get("approval_id") or "").strip()
    approval_records = record.get("authenticated_approval_records")
    stored_record = (
        approval_records.get(approval_id)
        if approval_id and isinstance(approval_records, Mapping)
        else None
    )
    stored_approval = (
        stored_record.get("human_approval")
        if isinstance(stored_record, Mapping)
        else None
    )
    if not isinstance(stored_approval, Mapping):
        raise LearningReleasePersistenceError(
            "authoritative_human_approval_not_found",
            details={"approval_id": approval_id or None},
            recovery_affordances=[
                {"action_type": "record_authenticated_human_approval"},
                {"action_type": "inspect_authenticated_approval_records"},
            ],
        )
    if operational_learning_release_digest(stored_approval) != (
        operational_learning_release_digest(human_approval)
    ):
        raise LearningReleasePersistenceError(
            "authoritative_human_approval_mismatch",
            details={"approval_id": approval_id},
        )
    return copy.deepcopy(stored_approval)


def _load_canonical_experiment_run(run_id: str) -> Mapping[str, Any] | None:
    from .experiment_run_service import get_experiment_run_state

    run = get_experiment_run_state(run_id)
    return run if isinstance(run, Mapping) else None


def _load_authoritative_workflow_identity(workflow_id: str) -> Mapping[str, Any] | None:
    from ..workflows.vontology_loader import load_workflow_definition_from_vontology
    from ..workflows.workflow_definition_identity_service import (
        build_workflow_definition_identity,
    )

    definition = load_workflow_definition_from_vontology(workflow_id)
    if definition is None:
        return None
    return build_workflow_definition_identity(
        workflow_id=workflow_id,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )


def _load_canonical_decision_trace(
    request_id: str,
    namespace: str,
) -> Mapping[str, Any] | None:
    from .turn_execution_record_service import get_turn_execution_record_projection

    record = get_turn_execution_record_projection(
        request_id=request_id,
        namespace=namespace,
    )
    return record if isinstance(record, Mapping) else None


def _mapping_occurs_in_projection(
    projection: Any,
    expected: Mapping[str, Any],
) -> bool:
    expected_digest = operational_learning_release_digest(expected)
    pending = [projection]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            try:
                if operational_learning_release_digest(current) == expected_digest:
                    return True
            except LearningReleaseValidationError:
                pass
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _trace_values_for_keys(
    projection: Any,
    keys: set[str],
    *,
    skip_mapping: Mapping[str, Any],
) -> set[str]:
    skipped_digest = operational_learning_release_digest(skip_mapping)
    values: set[str] = set()
    pending = [projection]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            try:
                if operational_learning_release_digest(current) == skipped_digest:
                    continue
            except LearningReleaseValidationError:
                pass
            for key, value in current.items():
                if key in keys and isinstance(value, str) and value.strip():
                    values.add(value.strip())
                pending.append(value)
        elif isinstance(current, list):
            pending.extend(current)
    return values


def _verify_persisted_represented_decision_trace(
    *,
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> None:
    authority = decision.get("authority")
    request_id = (
        str(authority.get("authority_execution_request_id") or "").strip()
        if isinstance(authority, Mapping)
        else ""
    )
    trace = _load_canonical_decision_trace(request_id, candidate["namespace"])
    if not request_id or not isinstance(trace, Mapping):
        raise LearningReleasePersistenceError(
            "represented_decision_execution_trace_required",
            details={"authority_execution_request_id": request_id or None},
            recovery_affordances=[
                {"action_type": "persist_represented_decision_execution_trace"}
            ],
        )
    if trace.get("namespace") != candidate["namespace"]:
        raise LearningReleasePersistenceError(
            "represented_decision_execution_trace_scope_mismatch"
        )
    for field_name in ("user_id", "org_id"):
        observed = trace.get(field_name)
        if observed is not None and observed != candidate[field_name]:
            raise LearningReleasePersistenceError(
                "represented_decision_execution_trace_scope_mismatch",
                details={"field": field_name},
            )
    if not _mapping_occurs_in_projection(trace, decision):
        raise LearningReleasePersistenceError(
            "represented_decision_not_found_in_execution_trace",
            details={
                "authority_execution_request_id": request_id,
                "decision_id": decision.get("decision_id"),
            },
            recovery_affordances=[
                {"action_type": "rerun_represented_evaluator"},
                {"action_type": "read_represented_decision_execution_trace"},
            ],
        )
    authority = decision.get("authority")
    workflow_id = str(
        authority.get("authority_concept_id") if isinstance(authority, Mapping) else ""
    ).strip()
    authority_revision = str(
        authority.get("authority_revision_sha256")
        if isinstance(authority, Mapping)
        else ""
    ).strip()
    prompt_revision = str(
        authority.get("authority_prompt_revision_sha256")
        if isinstance(authority, Mapping)
        else ""
    ).strip()
    observed_workflow_ids = _trace_values_for_keys(
        trace,
        {
            "workflow_id",
            "selected_workflow_id",
            "dispatch_workflow_id",
            "represented_workflow_id",
        },
        skip_mapping=decision,
    )
    observed_definition_revisions = _trace_values_for_keys(
        trace,
        {
            "authoritative_definition_hash",
            "workflow_definition_sha256",
            "definition_hash",
        },
        skip_mapping=decision,
    )
    observed_prompt_revisions = _trace_values_for_keys(
        trace,
        {"prompt_revision_sha256", "prompt_content_sha256", "prompt_sha256"},
        skip_mapping=decision,
    )
    if (
        not workflow_id
        or workflow_id not in observed_workflow_ids
        or not authority_revision
        or authority_revision not in observed_definition_revisions
        or not _is_sha256(prompt_revision)
        or prompt_revision not in observed_prompt_revisions
    ):
        raise LearningReleasePersistenceError(
            "represented_decision_execution_authority_mismatch",
            details={
                "authority_execution_request_id": request_id,
                "authority_concept_id": workflow_id or None,
                "authority_revision_sha256": authority_revision or None,
                "authority_prompt_revision_sha256": prompt_revision or None,
            },
            recovery_affordances=[
                {"action_type": "rerun_represented_evaluator_with_trace_lineage"}
            ],
        )


def _verify_live_authority_reference(
    value: Any,
    *,
    user_id: str,
    org_id: str,
) -> None:
    if not isinstance(value, Mapping):
        raise LearningReleasePersistenceError(
            "live_represented_authority_reference_required"
        )
    workflow_id = str(value.get("authority_concept_id") or "").strip()
    expected_revision = str(value.get("authority_revision_sha256") or "").strip()
    # Durable submission and execution resolve Vontology workflow authority in
    # the authenticated actor's scope. Revalidate in that same scope so an
    # ambient or anonymous materialisation cannot be compared with an exact
    # actor-scoped execution snapshot.
    with override_current_actor(user_id, org_id):
        identity = _load_authoritative_workflow_identity(workflow_id)
    observed_revision = (
        str(identity.get("authoritative_definition_hash") or "").strip()
        if isinstance(identity, Mapping)
        else ""
    )
    if not workflow_id or not observed_revision:
        raise LearningReleasePersistenceError(
            "live_represented_authority_unavailable",
            details={"authority_concept_id": workflow_id or None},
            recovery_affordances=[
                {
                    "action_type": "inspect_represented_authority",
                    "authority_concept_id": workflow_id or None,
                }
            ],
        )
    if expected_revision != observed_revision:
        raise LearningReleasePersistenceError(
            "live_represented_authority_revision_mismatch",
            details={
                "authority_concept_id": workflow_id,
                "expected_revision_sha256": expected_revision or None,
                "current_revision_sha256": observed_revision,
            },
            recovery_affordances=[
                {
                    "action_type": "read_current_authority_revision",
                    "authority_concept_id": workflow_id,
                    "current_revision_sha256": observed_revision,
                },
                {"action_type": "rerun_represented_evaluator"},
            ],
        )


def _verify_canonical_experiment_evidence(
    *,
    candidate: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
) -> None:
    embedded_run = experiment_evidence.get("experiment_run")
    if not isinstance(embedded_run, Mapping):
        raise LearningReleasePersistenceError(
            "canonical_learning_release_experiment_run_required"
        )
    run_id = str(embedded_run.get("run_id") or "").strip()
    canonical_run = _load_canonical_experiment_run(run_id)
    if not run_id or not isinstance(canonical_run, Mapping):
        raise LearningReleasePersistenceError(
            "canonical_learning_release_experiment_run_not_found",
            details={"run_id": run_id or None},
            recovery_affordances=[{"action_type": "persist_experiment_run"}],
        )
    for field_name in ("namespace", "user_id", "org_id"):
        if canonical_run.get(field_name) != candidate[field_name]:
            raise LearningReleasePersistenceError(
                "canonical_learning_release_experiment_scope_mismatch",
                details={"field": field_name, "run_id": run_id},
            )
    canonical_digest = operational_learning_release_digest(canonical_run)
    embedded_digest = operational_learning_release_digest(embedded_run)
    if (
        canonical_digest != embedded_digest
        or experiment_evidence.get("experiment_run_sha256") != canonical_digest
    ):
        raise LearningReleasePersistenceError(
            "canonical_learning_release_experiment_run_mismatch",
            details={
                "run_id": run_id,
                "canonical_run_sha256": canonical_digest,
                "supplied_run_sha256": embedded_digest,
            },
            recovery_affordances=[
                {"action_type": "read_canonical_experiment_run", "run_id": run_id}
            ],
        )


def _verify_canonical_certification_evidence(
    *,
    candidate: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
) -> None:
    campaign_result = certification_evidence.get("campaign_result")
    provenance = certification_evidence.get("execution_provenance")
    if not isinstance(campaign_result, Mapping) or not isinstance(provenance, Mapping):
        raise LearningReleasePersistenceError(
            "canonical_operational_certification_evidence_required"
        )
    experiment_run_id = str(provenance.get("experiment_run_id") or "").strip()
    canonical_run = _load_canonical_experiment_run(experiment_run_id)
    if not experiment_run_id or not isinstance(canonical_run, Mapping):
        raise LearningReleasePersistenceError(
            "canonical_operational_certification_run_not_found",
            details={"experiment_run_id": experiment_run_id or None},
            recovery_affordances=[
                {"action_type": "run_and_persist_operational_certification"}
            ],
        )
    from .operational_certification_attestation_service import (
        verify_operational_certification_runner_attestation,
    )

    try:
        verify_operational_certification_runner_attestation(
            provenance.get("trusted_runner_attestation"),
            expected={
                "experiment_run_id": experiment_run_id,
                "campaign_execution_id": str(
                    provenance.get("campaign_execution_id") or ""
                ),
                "namespace": candidate["namespace"],
                "user_id": candidate["user_id"],
                "org_id": candidate["org_id"],
                "report_sha256": campaign_result.get("report_sha256"),
                "contract_sha256": campaign_result.get("contract_sha256"),
            },
        )
    except (RuntimeError, ValueError) as exc:
        raise LearningReleasePersistenceError(
            "trusted_operational_certification_attestation_invalid",
            details={"reason": str(exc)},
            recovery_affordances=[
                {"action_type": "rerun_trusted_operational_certification"}
            ],
        ) from exc
    for field_name in ("namespace", "user_id", "org_id"):
        if canonical_run.get(field_name) != candidate[field_name]:
            raise LearningReleasePersistenceError(
                "canonical_operational_certification_scope_mismatch",
                details={"field": field_name, "experiment_run_id": experiment_run_id},
            )
    from .operational_certification_runner_service import (
        validate_campaign_experiment_observation,
    )

    matching_observation_found = False
    for raw_observation in canonical_run.get("observations") or []:
        if not isinstance(raw_observation, Mapping):
            continue
        evidence = raw_observation.get("evidence")
        persisted_campaign = (
            evidence.get("operational_certification_campaign_result")
            if isinstance(evidence, Mapping)
            else None
        )
        if not isinstance(persisted_campaign, Mapping):
            continue
        if persisted_campaign.get("report_sha256") != campaign_result.get(
            "report_sha256"
        ):
            continue
        if validate_campaign_experiment_observation(raw_observation):
            raise LearningReleasePersistenceError(
                "canonical_operational_certification_observation_invalid",
                details={"experiment_run_id": experiment_run_id},
            )
        if operational_learning_release_digest(persisted_campaign) != (
            operational_learning_release_digest(campaign_result)
        ):
            raise LearningReleasePersistenceError(
                "canonical_operational_certification_report_mismatch",
                details={"experiment_run_id": experiment_run_id},
            )
        persisted_provenance = raw_observation.get("execution_provenance")
        if not isinstance(persisted_provenance, Mapping) or (
            operational_learning_release_digest(persisted_provenance)
            != operational_learning_release_digest(provenance)
        ):
            raise LearningReleasePersistenceError(
                "canonical_operational_certification_provenance_mismatch",
                details={"experiment_run_id": experiment_run_id},
            )
        matching_observation_found = True
        break
    if not matching_observation_found:
        raise LearningReleasePersistenceError(
            "canonical_operational_certification_campaign_not_found",
            details={
                "experiment_run_id": experiment_run_id,
                "report_sha256": campaign_result.get("report_sha256"),
            },
            recovery_affordances=[
                {
                    "action_type": "persist_operational_certification_campaign",
                    "experiment_run_id": experiment_run_id,
                }
            ],
        )


def _verify_live_transition_evidence(
    *,
    candidate: Mapping[str, Any],
    represented_evaluator_decision: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
) -> None:
    """Require canonical authority and persisted experiment/campaign readback."""

    _verify_live_authority_reference(represented_evaluator_decision.get("authority"))
    _verify_persisted_represented_decision_trace(
        candidate=candidate,
        decision=represented_evaluator_decision,
    )
    _verify_canonical_experiment_evidence(
        candidate=candidate,
        experiment_evidence=experiment_evidence,
    )
    _verify_canonical_certification_evidence(
        candidate=candidate,
        certification_evidence=certification_evidence,
    )


def _apply_and_readback_release_activation(
    candidate: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Fail closed until an affected-artefact activation adapter is registered."""

    raise LearningReleasePersistenceError(
        "canonical_learning_release_activation_adapter_unavailable",
        details={
            "candidate_id": candidate.get("candidate_id"),
            "affected_artifact": candidate.get("affected_artifact"),
            "candidate_release_sha256": candidate.get("release_sha256"),
            "decision_state": "promotion_approved_not_activated",
        },
        recovery_affordances=[
            {
                "action_type": "register_canonical_release_activation_adapter",
                "affected_artifact": candidate.get("affected_artifact"),
            },
            {"action_type": "retain_active_release"},
        ],
    )


def _validate_activation_readback(
    value: Any,
    *,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningReleasePersistenceError(
            "canonical_learning_release_activation_readback_invalid"
        )
    receipt = copy.deepcopy(dict(value))
    if (
        receipt.get("activated") is not True
        or receipt.get("affected_artifact") != candidate.get("affected_artifact")
        or receipt.get("candidate_release_sha256") != candidate.get("release_sha256")
        or receipt.get("runtime_release_sha256") != candidate.get("release_sha256")
    ):
        raise LearningReleasePersistenceError(
            "canonical_learning_release_activation_readback_mismatch"
        )
    digest_basis = dict(receipt)
    observed_digest = digest_basis.pop("receipt_sha256", None)
    if observed_digest != operational_learning_release_digest(digest_basis):
        raise LearningReleasePersistenceError(
            "canonical_learning_release_activation_receipt_hash_mismatch"
        )
    return receipt


def _apply_and_readback_release_rollback_activation(
    *,
    active_candidate: Mapping[str, Any],
    previous_candidate: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Fail closed until runtime can restore/read back the previous release."""

    raise LearningReleasePersistenceError(
        "canonical_learning_release_rollback_adapter_unavailable",
        details={
            "active_candidate_id": active_candidate.get("candidate_id"),
            "previous_candidate_id": previous_candidate.get("candidate_id"),
            "affected_artifact": active_candidate.get("affected_artifact"),
            "previous_release_sha256": previous_candidate.get("release_sha256"),
            "decision_state": "rollback_approved_not_activated",
        },
        recovery_affordances=[
            {
                "action_type": "register_canonical_release_rollback_adapter",
                "affected_artifact": active_candidate.get("affected_artifact"),
            },
            {"action_type": "retain_active_release"},
        ],
    )


Mutation = Callable[
    [dict[str, Any], dict[str, Any], dict[str, Any]],
    Any,
]


def _mutate_with_process_lock(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    expected_version: int,
    expected_state_sha256: str,
    operation: str,
    mutation: Mutation,
    mutation_subject: Mapping[str, Any],
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    scope = _scope(namespace=namespace, user_id=user_id, org_id=org_id)
    state_concept_id = operational_learning_release_state_concept_id(**scope)
    resolved_expected_version = _expected_version(expected_version)
    resolved_expected_sha256 = _require_sha256(
        expected_state_sha256,
        code="operational_learning_release_expected_state_digest_invalid",
    )
    with _lock_for(state_concept_id):
        current, _persisted = _load_raw_record(scope)
        if (
            current["version"] != resolved_expected_version
            or current["state_sha256"] != resolved_expected_sha256
        ):
            raise LearningReleaseStateConflictError(
                expected_version=resolved_expected_version,
                expected_state_sha256=resolved_expected_sha256,
                current_version=current["version"],
                current_state_sha256=current["state_sha256"],
                state_concept_id=state_concept_id,
            )
        next_state = copy.deepcopy(current["state"])
        next_approvals = copy.deepcopy(current["authenticated_approval_records"])
        next_evaluations = copy.deepcopy(current["candidate_evaluation_records"])
        result = mutation(next_state, next_approvals, next_evaluations)
        next_state = validate_operational_learning_release_state(next_state)
        now_iso = _utc_iso(recorded_at)
        next_record: dict[str, Any] = {
            "schema_version": (
                OPERATIONAL_LEARNING_RELEASE_STATE_RECORD_SCHEMA_VERSION
            ),
            "state_concept_id": state_concept_id,
            **scope,
            "version": current["version"] + 1,
            "state_sha256": operational_learning_release_digest(next_state),
            "previous_state_sha256": current["state_sha256"],
            "previous_record_sha256": current["record_sha256"],
            "state": next_state,
            "authenticated_approval_records": next_approvals,
            "candidate_evaluation_records": next_evaluations,
            "last_mutation": {
                "operation": operation,
                "subject": copy.deepcopy(dict(mutation_subject)),
                "recorded_at": now_iso,
            },
            "updated_at": now_iso,
        }
        next_record["record_sha256"] = operational_learning_release_digest(
            _record_without_digest(next_record)
        )
        next_record = _validate_record(
            next_record,
            scope=scope,
            state_concept_id=state_concept_id,
        )
        _ensure_state_concept(scope=scope, state_concept_id=state_concept_id)
        upsert_singleton_text_relation(
            subject_concept_id=state_concept_id,
            predicate=OPERATIONAL_LEARNING_RELEASE_STATE_PREDICATE,
            text=json.dumps(
                next_record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            lang=_LANGUAGE,
            policy="replace_others",
            provenance={
                "source": _MANAGED_BY,
                "jira": _SOURCE_TAG,
                "namespace": scope["namespace"],
                "actor_concept_id": scope["user_id"],
            },
            context={
                "operation": operation,
                "state_version": next_record["version"],
                "state_sha256": next_record["state_sha256"],
            },
            garbage_collect=True,
        )
        readback, persisted = _load_raw_record(scope)
        if not persisted or readback != next_record:
            raise LearningReleasePersistenceError(
                "operational_learning_release_state_readback_mismatch",
                details={
                    "state_concept_id": state_concept_id,
                    "expected_version": next_record["version"],
                    "observed_version": readback.get("version"),
                    "expected_record_sha256": next_record["record_sha256"],
                    "observed_record_sha256": readback.get("record_sha256"),
                },
                recovery_affordances=[
                    {
                        "action_type": "read_latest_state",
                        "state_concept_id": state_concept_id,
                    }
                ],
            )
        return {
            "success": True,
            "operation": operation,
            "result": copy.deepcopy(result),
            "state_record": _public_record(readback, persisted=True),
        }


def _mutate(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    expected_version: int,
    expected_state_sha256: str,
    operation: str,
    mutation: Mutation,
    mutation_subject: Mapping[str, Any],
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Mutate only while holding a storage-level single-writer lease."""

    scope = _scope(namespace=namespace, user_id=user_id, org_id=org_id)
    state_concept_id = operational_learning_release_state_concept_id(**scope)
    _ensure_state_concept(scope=scope, state_concept_id=state_concept_id)
    lease = concept_service.acquire_concept_mutation_lease(
        concept_id=state_concept_id,
        lease_name=_WRITER_LEASE_NAME,
        owner_token=_WRITER_OWNER_TOKEN,
        ttl_seconds=120,
    )
    if lease.get("success") is not True:
        raise LearningReleasePersistenceError(
            "operational_learning_release_single_writer_unavailable",
            details={"state_concept_id": state_concept_id},
            recovery_affordances=[
                {"action_type": "read_latest_state"},
                {"action_type": "retry_after_writer_lease"},
            ],
        )
    try:
        return _mutate_with_process_lock(
            namespace=scope["namespace"],
            user_id=scope["user_id"],
            org_id=scope["org_id"],
            expected_version=expected_version,
            expected_state_sha256=expected_state_sha256,
            operation=operation,
            mutation=mutation,
            mutation_subject=mutation_subject,
            recorded_at=recorded_at,
        )
    finally:
        try:
            concept_service.release_concept_mutation_lease(
                concept_id=state_concept_id,
                lease_name=_WRITER_LEASE_NAME,
                owner_token=_WRITER_OWNER_TOKEN,
            )
        except Exception:
            # The bounded lease expires even if release/readback infrastructure
            # is temporarily unavailable. Never replace the mutation outcome
            # with a misleading release error.
            pass


def register_operational_learning_release_candidate_in_vontology(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    expected_version: int,
    expected_state_sha256: str,
    candidate_id: str,
    release_id: str,
    affected_artifact: str,
    release_payload: Mapping[str, Any],
    failure_evidence_packets: Sequence[Mapping[str, Any]],
    proposal_authority: Mapping[str, Any],
    risk_classes: list[str] | None,
    expires_at: str,
    retest_after: str,
    retest_requirements: Mapping[str, Any],
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate/register a represented candidate and persist exact readback."""

    _verify_live_authority_reference(
        proposal_authority,
        user_id=user_id,
        org_id=org_id,
    )
    _verify_persisted_represented_candidate_proposal_trace(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        candidate_id=candidate_id,
        release_id=release_id,
        affected_artifact=affected_artifact,
        release_payload=release_payload,
        failure_evidence_packets=failure_evidence_packets,
        proposal_authority=proposal_authority,
        risk_classes=risk_classes or [],
        expires_at=expires_at,
        retest_after=retest_after,
        retest_requirements=retest_requirements,
    )
    return _mutate(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        expected_version=expected_version,
        expected_state_sha256=expected_state_sha256,
        operation="register_candidate",
        mutation_subject={"candidate_id": candidate_id, "release_id": release_id},
        recorded_at=created_at,
        mutation=lambda state, _approvals, _evaluations: (
            register_operational_learning_release_candidate(
                state,
                candidate_id=candidate_id,
                release_id=release_id,
                affected_artifact=affected_artifact,
                namespace=namespace,
                user_id=user_id,
                org_id=org_id,
                release_payload=release_payload,
                failure_evidence_packets=failure_evidence_packets,
                proposal_authority=proposal_authority,
                risk_classes=risk_classes or [],
                expires_at=expires_at,
                retest_after=retest_after,
                retest_requirements=retest_requirements,
                created_at=created_at,
            )
        ),
    )


def record_authenticated_human_learning_release_approval(
    *,
    authenticated_namespace: str,
    authenticated_user_id: str,
    authenticated_org_id: str,
    expected_version: int,
    expected_state_sha256: str,
    approval_id: str,
    action: str,
    candidate_id: str,
    authenticated_at: datetime | None = None,
) -> dict[str, Any]:
    """Record an approval whose actor and scope came from authentication context."""

    resolved_approval_id = _clean(
        approval_id,
        code="authenticated_learning_release_approval_id_required",
    )
    resolved_action = _clean(
        action,
        code="authenticated_learning_release_approval_action_required",
    )
    if resolved_action not in _ACTIONS:
        raise LearningReleasePersistenceError(
            "authenticated_learning_release_approval_action_invalid"
        )
    resolved_candidate_id = _clean(
        candidate_id,
        code="authenticated_learning_release_approval_candidate_id_required",
    )
    approved_at = _utc_iso(authenticated_at)

    def _record(
        state: dict[str, Any],
        approvals: dict[str, Any],
        _evaluations: dict[str, Any],
    ) -> dict[str, Any]:
        if resolved_approval_id in approvals:
            raise LearningReleasePersistenceError(
                "authenticated_learning_release_approval_id_conflict",
                details={"approval_id": resolved_approval_id},
            )
        candidate = (state.get("candidate_snapshots") or {}).get(resolved_candidate_id)
        if not isinstance(candidate, Mapping):
            raise LearningReleasePersistenceError(
                "authenticated_learning_release_approval_candidate_not_found"
            )
        receipt: dict[str, Any] = {
            "schema_version": (AUTHENTICATED_HUMAN_APPROVAL_RECEIPT_SCHEMA_VERSION),
            "approval_id": resolved_approval_id,
            "approver_id": authenticated_user_id,
            "action": resolved_action,
            "candidate_id": resolved_candidate_id,
            "candidate_release_sha256": candidate.get("release_sha256"),
            "namespace": authenticated_namespace,
            "user_id": authenticated_user_id,
            "org_id": authenticated_org_id,
            "approved_at": approved_at,
            "authenticated_at": approved_at,
        }
        receipt["receipt_sha256"] = operational_learning_release_digest(receipt)
        human_approval = {
            "schema_version": HUMAN_LEARNING_RELEASE_APPROVAL_SCHEMA_VERSION,
            "approval_id": resolved_approval_id,
            "approver_id": authenticated_user_id,
            "action": resolved_action,
            "candidate_id": resolved_candidate_id,
            "candidate_release_sha256": candidate.get("release_sha256"),
            "namespace": authenticated_namespace,
            "user_id": authenticated_user_id,
            "org_id": authenticated_org_id,
            "approved": True,
            "approved_at": approved_at,
            "authenticated_approval_receipt": receipt,
        }
        approval_record: dict[str, Any] = {
            "schema_version": AUTHENTICATED_HUMAN_APPROVAL_RECORD_SCHEMA_VERSION,
            "human_approval": human_approval,
            "recorded_at": approved_at,
        }
        approval_record["record_sha256"] = operational_learning_release_digest(
            approval_record
        )
        approvals[resolved_approval_id] = approval_record
        return copy.deepcopy(human_approval)

    return _mutate(
        namespace=authenticated_namespace,
        user_id=authenticated_user_id,
        org_id=authenticated_org_id,
        expected_version=expected_version,
        expected_state_sha256=expected_state_sha256,
        operation="record_authenticated_human_approval",
        mutation_subject={
            "approval_id": resolved_approval_id,
            "candidate_id": resolved_candidate_id,
            "action": resolved_action,
        },
        recorded_at=authenticated_at,
        mutation=_record,
    )


def register_operational_learning_release_candidate_evaluation_in_vontology(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    expected_version: int,
    expected_state_sha256: str,
    evaluation_id: str,
    candidate_id: str,
    registered_at: datetime | None = None,
) -> dict[str, Any]:
    """Register an exact candidate/release pair for pre-decision evaluation."""

    resolved_evaluation_id = _clean(
        evaluation_id,
        code="operational_learning_release_evaluation_id_required",
    )
    resolved_candidate_id = _clean(
        candidate_id,
        code="operational_learning_release_evaluation_candidate_id_required",
    )
    timestamp = _utc_iso(registered_at)

    def _register_evaluation(
        state: dict[str, Any],
        _approvals: dict[str, Any],
        evaluations: dict[str, Any],
    ) -> dict[str, Any]:
        if resolved_evaluation_id in evaluations:
            raise LearningReleasePersistenceError(
                "operational_learning_release_evaluation_id_conflict",
                details={"evaluation_id": resolved_evaluation_id},
            )
        candidate = (state.get("candidate_snapshots") or {}).get(resolved_candidate_id)
        if not isinstance(candidate, Mapping):
            raise LearningReleasePersistenceError(
                "operational_learning_release_evaluation_candidate_not_found"
            )
        evaluation_record: dict[str, Any] = {
            "schema_version": (
                OPERATIONAL_LEARNING_RELEASE_CANDIDATE_EVALUATION_SCHEMA_VERSION
            ),
            "evaluation_id": resolved_evaluation_id,
            "candidate_id": resolved_candidate_id,
            "candidate_release_sha256": candidate.get("release_sha256"),
            "namespace": namespace,
            "user_id": user_id,
            "org_id": org_id,
            "registered_at": timestamp,
        }
        evaluation_record["record_sha256"] = operational_learning_release_digest(
            evaluation_record
        )
        evaluations[resolved_evaluation_id] = evaluation_record
        return copy.deepcopy(evaluation_record)

    return _mutate(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        expected_version=expected_version,
        expected_state_sha256=expected_state_sha256,
        operation="register_candidate_evaluation",
        mutation_subject={
            "evaluation_id": resolved_evaluation_id,
            "candidate_id": resolved_candidate_id,
        },
        recorded_at=registered_at,
        mutation=_register_evaluation,
    )


def promote_operational_learning_release_candidate_in_vontology(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    expected_version: int,
    expected_state_sha256: str,
    candidate_id: str,
    represented_evaluator_decision: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
    human_approval: Mapping[str, Any] | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Apply a represented promote decision with authoritative approval lookup."""

    def _promote(
        state: dict[str, Any],
        _approvals: dict[str, Any],
        _evaluations: dict[str, Any],
    ) -> dict[str, Any]:
        candidate = (state.get("candidate_snapshots") or {}).get(candidate_id)
        if not isinstance(candidate, Mapping):
            raise LearningReleasePersistenceError(
                "operational_learning_release_candidate_not_found"
            )
        _verify_live_transition_evidence(
            candidate=candidate,
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
        )
        current_record = {
            "authenticated_approval_records": _approvals,
        }
        approval = _authoritative_approval(
            current_record,
            human_approval,
            required=True,
        )
        _validate_activation_readback(
            _apply_and_readback_release_activation(candidate),
            candidate=candidate,
        )
        return promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate_id,
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
            human_approval=approval,
            recorded_at=recorded_at,
        )

    return _mutate(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        expected_version=expected_version,
        expected_state_sha256=expected_state_sha256,
        operation="promote",
        mutation_subject={"candidate_id": candidate_id},
        recorded_at=recorded_at,
        mutation=_promote,
    )


def reject_operational_learning_release_candidate_in_vontology(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    expected_version: int,
    expected_state_sha256: str,
    candidate_id: str,
    represented_evaluator_decision: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Apply a represented reject decision and persist exact readback."""

    def _reject(
        state: dict[str, Any],
        _approvals: dict[str, Any],
        _evaluations: dict[str, Any],
    ) -> dict[str, Any]:
        candidate = (state.get("candidate_snapshots") or {}).get(candidate_id)
        if not isinstance(candidate, Mapping):
            raise LearningReleasePersistenceError(
                "operational_learning_release_candidate_not_found"
            )
        _verify_live_transition_evidence(
            candidate=candidate,
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
        )
        return reject_operational_learning_release_candidate(
            state,
            candidate_id=candidate_id,
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
            recorded_at=recorded_at,
        )

    return _mutate(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        expected_version=expected_version,
        expected_state_sha256=expected_state_sha256,
        operation="reject",
        mutation_subject={"candidate_id": candidate_id},
        recorded_at=recorded_at,
        mutation=_reject,
    )


def rollback_operational_learning_release_in_vontology(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
    expected_version: int,
    expected_state_sha256: str,
    affected_artifact: str,
    represented_evaluator_decision: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
    human_approval: Mapping[str, Any] | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Restore the exact previous hash after authoritative approval lookup."""

    def _rollback(
        state: dict[str, Any],
        approvals: dict[str, Any],
        _evaluations: dict[str, Any],
    ) -> dict[str, Any]:
        pointer_state = (state.get("release_pointers") or {}).get(affected_artifact)
        active_pointer = (
            pointer_state.get("active") if isinstance(pointer_state, Mapping) else None
        )
        active_candidate_id = (
            active_pointer.get("candidate_id")
            if isinstance(active_pointer, Mapping)
            else None
        )
        candidate = (state.get("candidate_snapshots") or {}).get(active_candidate_id)
        if not isinstance(candidate, Mapping):
            raise LearningReleasePersistenceError(
                "operational_learning_release_active_candidate_not_found"
            )
        _verify_live_transition_evidence(
            candidate=candidate,
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
        )
        approval = _authoritative_approval(
            {"authenticated_approval_records": approvals},
            human_approval,
            required=True,
        )
        previous_pointer = (
            pointer_state.get("previous")
            if isinstance(pointer_state, Mapping)
            else None
        )
        previous_candidate_id = (
            previous_pointer.get("candidate_id")
            if isinstance(previous_pointer, Mapping)
            else None
        )
        previous_candidate = (state.get("candidate_snapshots") or {}).get(
            previous_candidate_id
        )
        if not isinstance(previous_candidate, Mapping):
            raise LearningReleasePersistenceError(
                "operational_learning_release_previous_candidate_not_found"
            )
        _validate_activation_readback(
            _apply_and_readback_release_rollback_activation(
                active_candidate=candidate,
                previous_candidate=previous_candidate,
            ),
            candidate=previous_candidate,
        )
        return rollback_operational_learning_release(
            state,
            affected_artifact=affected_artifact,
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
            human_approval=approval,
            recorded_at=recorded_at,
        )

    return _mutate(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        expected_version=expected_version,
        expected_state_sha256=expected_state_sha256,
        operation="rollback",
        mutation_subject={"affected_artifact": affected_artifact},
        recorded_at=recorded_at,
        mutation=_rollback,
    )


__all__ = [
    "AUTHENTICATED_HUMAN_APPROVAL_RECORD_SCHEMA_VERSION",
    "LearningReleasePersistenceError",
    "LearningReleaseStateConflictError",
    "OPERATIONAL_LEARNING_RELEASE_CAMPAIGN_EVIDENCE_SCHEMA_VERSION",
    "OPERATIONAL_LEARNING_RELEASE_CANDIDATE_EVALUATION_SCHEMA_VERSION",
    "OPERATIONAL_LEARNING_RELEASE_STATE_PREDICATE",
    "OPERATIONAL_LEARNING_RELEASE_STATE_RECORD_SCHEMA_VERSION",
    "OPERATIONAL_LEARNING_RELEASE_STATE_TYPE_ID",
    "REPRESENTED_ACTIVE_LEARNING_RELEASE_CONTEXT_SCHEMA_VERSION",
    "REPRESENTED_LEARNING_CANDIDATE_CONTEXT_SCHEMA_VERSION",
    "REPRESENTED_LEARNING_RELEASE_BINDING_SCHEMA_VERSION",
    "load_operational_learning_release_state",
    "operational_learning_release_state_concept_id",
    "project_operational_learning_release_campaign_evidence",
    "promote_operational_learning_release_candidate_in_vontology",
    "record_authenticated_human_learning_release_approval",
    "register_operational_learning_release_candidate_evaluation_in_vontology",
    "register_operational_learning_release_candidate_in_vontology",
    "reject_operational_learning_release_candidate_in_vontology",
    "resolve_operational_learning_active_release_in_vontology",
    "resolve_operational_learning_release_candidate_in_vontology",
    "rollback_operational_learning_release_in_vontology",
]
