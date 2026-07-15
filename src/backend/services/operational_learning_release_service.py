"""Generic evidence-gated release support for represented learning candidates.

The represented proposal, evaluator, experiment, certification, and promotion
workflows own every semantic decision.  This module supplies only deterministic
support surfaces: strict evidence projection, immutable hashes, fail-closed
validation, pointer updates, safety approval checks, and auditable receipts.

It deliberately contains no remedy catalogue, ranking rule, domain case, or
promotion threshold.  Callers inject a JSON-compatible state mapping so the
same transition logic can be used behind Vontology, workflow, or test stores.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, cast

from .operational_certification_contract_service import (
    validate_operational_certification_campaign_result_integrity,
)


FAILURE_EVIDENCE_PACKET_SCHEMA_VERSION = "failure_evidence_packet.v1"
OPERATIONAL_LEARNING_RELEASE_CANDIDATE_SCHEMA_VERSION = (
    "operational_learning_release_candidate.v1"
)
OPERATIONAL_LEARNING_RELEASE_POINTER_SCHEMA_VERSION = (
    "operational_learning_release_pointer.v1"
)
OPERATIONAL_LEARNING_RELEASE_STATE_SCHEMA_VERSION = (
    "operational_learning_release_state.v1"
)
OPERATIONAL_LEARNING_RELEASE_RECEIPT_SCHEMA_VERSION = (
    "operational_learning_release_receipt.v1"
)
REPRESENTED_AUTHORITY_REFERENCE_SCHEMA_VERSION = "represented_authority_reference.v1"
REPRESENTED_LEARNING_RELEASE_DECISION_SCHEMA_VERSION = (
    "represented_learning_release_decision.v1"
)
LEARNING_RELEASE_EXPERIMENT_EVIDENCE_SCHEMA_VERSION = (
    "learning_release_experiment_evidence.v1"
)
LEARNING_RELEASE_CERTIFICATION_EVIDENCE_SCHEMA_VERSION = (
    "learning_release_certification_evidence.v1"
)
LEARNING_RELEASE_EVALUATOR_EVIDENCE_PROJECTION_SCHEMA_VERSION = (
    "represented_learning_release_evaluator_evidence_projection.v1"
)
LEARNING_RELEASE_EVALUATOR_DIGEST_ONLY_REFERENCE_SCHEMA_VERSION = (
    "represented_learning_release_evaluator_digest_only_reference.v1"
)
HUMAN_LEARNING_RELEASE_APPROVAL_SCHEMA_VERSION = "human_learning_release_approval.v1"
AUTHENTICATED_HUMAN_APPROVAL_RECEIPT_SCHEMA_VERSION = (
    "authenticated_human_approval_receipt.v1"
)

EXPERIMENT_RUN_SCHEMA_VERSION = "experiment_run.v1"
OPERATIONAL_CERTIFICATION_CAMPAIGN_RESULT_SCHEMA_VERSION = (
    "operational_certification_campaign_result.v1"
)

# This is a hard safety boundary, not authored promotion policy.  Other risk
# classes pass through unchanged for the represented workflow to interpret.
HUMAN_APPROVAL_REQUIRED_RISK_CLASSES = frozenset(
    {"security", "destructive", "privilege"}
)

_DECISIONS = frozenset({"promote", "reject", "rollback"})
_EXPERIMENT_VERDICTS = frozenset({"pass", "fail", "partial", "inconclusive"})
_EXPERIMENT_TERMINAL_STATUSES = frozenset({"completed", "failed"})
_RELEASE_LIFECYCLE_STATES = frozenset(
    {"proposed", "active", "superseded", "rejected", "rolled_back"}
)
_HEX_DIGEST_LENGTH = 64


class LearningReleaseValidationError(ValueError):
    """A typed, fail-closed learning-release validation failure."""

    def __init__(
        self,
        code: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = _json_projection(details or {})
        super().__init__(code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "operational_learning_release_error.v1",
            "error_code": self.code,
            "details": copy.deepcopy(self.details),
        }


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    )


def _json_projection(value: Any) -> Any:
    """Return a strict, detached JSON-compatible projection."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LearningReleaseValidationError("non_finite_number_not_allowed")
        return value
    if isinstance(value, Mapping):
        projection: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise LearningReleaseValidationError("state_mapping_key_must_be_string")
            projection[key] = _json_projection(item)
        return projection
    if _is_sequence(value):
        return [_json_projection(item) for item in value]
    raise LearningReleaseValidationError(
        "non_json_learning_release_value",
        details={"value_type": type(value).__name__},
    )


def operational_learning_release_digest(value: Any) -> str:
    """Return the canonical SHA-256 digest used by release evidence."""

    encoded = json.dumps(
        _json_projection(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningReleaseValidationError(code)
    return value.strip()


def _normalise_string_sequence(
    value: Any,
    *,
    code: str,
    allow_empty: bool = True,
) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not _is_sequence(value):
        raise LearningReleaseValidationError(code)
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item, code=code)
        if text in seen:
            continue
        seen.add(text)
        output.append(text)
    if not output and not allow_empty:
        raise LearningReleaseValidationError(code)
    return output


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HEX_DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _require_sha256(value: Any, *, code: str) -> str:
    if not _is_sha256(value):
        raise LearningReleaseValidationError(code)
    return str(value).lower()


def _timestamp(value: Any, *, code: str) -> str:
    text = _text(value, code=code)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise LearningReleaseValidationError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LearningReleaseValidationError(code)
    return parsed.isoformat()


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _now_iso(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise LearningReleaseValidationError("release_timestamp_timezone_required")
    return current.isoformat()


def _validate_authority_reference(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningReleaseValidationError(code)
    authority = _json_projection(value)
    if authority.get("schema_version") != (
        REPRESENTED_AUTHORITY_REFERENCE_SCHEMA_VERSION
    ):
        raise LearningReleaseValidationError(code)
    concept_id = _text(
        authority.get("authority_concept_id"),
        code=code,
    )
    if not concept_id.startswith("#V#"):
        raise LearningReleaseValidationError(code)
    revision_sha256 = _require_sha256(
        authority.get("authority_revision_sha256"),
        code=code,
    )
    return {
        **authority,
        "authority_concept_id": concept_id,
        "authority_revision_sha256": revision_sha256,
    }


def _packet_without_digest(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in packet.items()
        if key != "packet_sha256"
    }


def _validate_failure_packet(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningReleaseValidationError("failure_evidence_packet_required")
    packet = _json_projection(value)
    if packet.get("schema_version") != FAILURE_EVIDENCE_PACKET_SCHEMA_VERSION:
        raise LearningReleaseValidationError("failure_evidence_packet_schema_invalid")
    _text(packet.get("packet_id"), code="failure_evidence_packet_id_required")
    _text(packet.get("causal_stage"), code="failure_causal_stage_required")
    _text(packet.get("cause_code"), code="failure_cause_code_required")
    _text(packet.get("affected_artifact"), code="failure_affected_artifact_required")
    members = _normalise_string_sequence(
        packet.get("member_request_ids"),
        code="failure_member_request_ids_required",
        allow_empty=False,
    )
    if packet.get("member_request_ids") != members:
        raise LearningReleaseValidationError(
            "failure_member_request_ids_must_be_unique_and_ordered"
        )
    member_count = packet.get("member_count")
    if not isinstance(member_count, int) or isinstance(member_count, bool):
        raise LearningReleaseValidationError("failure_member_count_invalid")
    if member_count != len(members):
        raise LearningReleaseValidationError("failure_member_count_mismatch")
    expected_digest = operational_learning_release_digest(
        _packet_without_digest(packet)
    )
    if packet.get("packet_sha256") != expected_digest:
        raise LearningReleaseValidationError("failure_evidence_packet_hash_mismatch")
    return packet


def build_failure_evidence_packets(
    failure_evidence: Sequence[Mapping[str, Any]],
    *,
    created_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Group represented failures on exactly three causal dimensions.

    Free text, domains, workflow names, remedies, and outcomes never influence
    grouping.  Every member request identifier and source-evidence digest is
    retained for later replay and audit.
    """

    if not _is_sequence(failure_evidence):
        raise LearningReleaseValidationError("failure_evidence_sequence_required")
    timestamp = _now_iso(created_at)
    groups: dict[tuple[str, str, str], list[dict[str, str | None]]] = {}
    request_membership: dict[str, tuple[str, str, str]] = {}
    request_evidence_digest: dict[str, str] = {}
    for raw_evidence in failure_evidence:
        if not isinstance(raw_evidence, Mapping):
            raise LearningReleaseValidationError(
                "represented_failure_evidence_required"
            )
        evidence = _json_projection(raw_evidence)
        request_id = _text(
            evidence.get("request_id"),
            code="failure_request_id_required",
        )
        causal_stage = _text(
            evidence.get("causal_stage"),
            code="failure_causal_stage_required",
        )
        cause_code = _text(
            evidence.get("cause_code"),
            code="failure_cause_code_required",
        )
        affected_artifact = _text(
            evidence.get("affected_artifact"),
            code="failure_affected_artifact_required",
        )
        if evidence.get("outcome") == "verified_success":
            raise LearningReleaseValidationError(
                "verified_success_is_not_failure_evidence"
            )
        grouping_key = (causal_stage, cause_code, affected_artifact)
        prior_group = request_membership.get(request_id)
        if prior_group is not None and prior_group != grouping_key:
            raise LearningReleaseValidationError(
                "failure_request_has_conflicting_causal_dimensions",
                details={"request_id": request_id},
            )
        request_membership[request_id] = grouping_key
        source_evidence_sha256 = operational_learning_release_digest(evidence)
        prior_digest = request_evidence_digest.get(request_id)
        if prior_digest is not None:
            if prior_digest != source_evidence_sha256:
                raise LearningReleaseValidationError(
                    "failure_request_has_conflicting_source_evidence",
                    details={"request_id": request_id},
                )
            continue
        request_evidence_digest[request_id] = source_evidence_sha256
        member = {
            "request_id": request_id,
            "source_schema_version": (
                str(evidence.get("schema_version")).strip()
                if isinstance(evidence.get("schema_version"), str)
                and str(evidence.get("schema_version")).strip()
                else None
            ),
            "source_evidence_sha256": source_evidence_sha256,
        }
        group = groups.setdefault(grouping_key, [])
        if member not in group:
            group.append(member)

    packets: list[dict[str, Any]] = []
    for grouping_key in sorted(groups):
        causal_stage, cause_code, affected_artifact = grouping_key
        members = sorted(groups[grouping_key], key=lambda item: str(item["request_id"]))
        member_request_ids = [str(item["request_id"]) for item in members]
        packet_identity = operational_learning_release_digest(
            {
                "causal_stage": causal_stage,
                "cause_code": cause_code,
                "affected_artifact": affected_artifact,
                "member_request_ids": member_request_ids,
            }
        )
        packet: dict[str, Any] = {
            "schema_version": FAILURE_EVIDENCE_PACKET_SCHEMA_VERSION,
            "packet_id": f"failure-evidence-{packet_identity[:24]}",
            "grouping_dimensions": [
                "causal_stage",
                "cause_code",
                "affected_artifact",
            ],
            "causal_stage": causal_stage,
            "cause_code": cause_code,
            "affected_artifact": affected_artifact,
            "member_request_ids": member_request_ids,
            "member_count": len(member_request_ids),
            "members": members,
            "created_at": timestamp,
        }
        packet["packet_sha256"] = operational_learning_release_digest(packet)
        packets.append(packet)
    return packets


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": OPERATIONAL_LEARNING_RELEASE_STATE_SCHEMA_VERSION,
        "candidate_snapshots": {},
        "candidate_states": {},
        "release_pointers": {},
        "release_states": {},
        "decision_receipts": [],
    }


def build_empty_operational_learning_release_state() -> dict[str, Any]:
    """Return a detached, valid empty state for a persistence adapter."""

    return _empty_state()


def _working_state(state: MutableMapping[str, Any]) -> dict[str, Any]:
    if not isinstance(state, MutableMapping):
        raise LearningReleaseValidationError("mutable_release_state_required")
    if not state:
        return _empty_state()
    projected = _json_projection(state)
    if (
        projected.get("schema_version")
        != OPERATIONAL_LEARNING_RELEASE_STATE_SCHEMA_VERSION
    ):
        raise LearningReleaseValidationError("learning_release_state_schema_invalid")
    for field in (
        "candidate_snapshots",
        "candidate_states",
        "release_pointers",
        "release_states",
    ):
        if not isinstance(projected.get(field), Mapping):
            raise LearningReleaseValidationError(
                "learning_release_state_mapping_invalid",
                details={"field": field},
            )
        projected[field] = dict(projected[field])
    if not _is_sequence(projected.get("decision_receipts")):
        raise LearningReleaseValidationError("learning_release_receipts_invalid")
    projected["decision_receipts"] = list(projected["decision_receipts"])
    _validate_persisted_release_state_integrity(projected)
    return projected


def validate_operational_learning_release_state(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a detached projection after strict persisted-state validation."""

    if not isinstance(state, Mapping):
        raise LearningReleaseValidationError("learning_release_state_mapping_required")
    return _working_state(dict(state))


def _commit_state(
    target: MutableMapping[str, Any],
    working: Mapping[str, Any],
) -> None:
    snapshot = _json_projection(working)
    target.clear()
    target.update(snapshot)


def _compact_release_reference(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise LearningReleaseValidationError("release_pointer_invalid")
    release_id = _text(value.get("release_id"), code="release_pointer_id_required")
    release_sha256 = _require_sha256(
        value.get("release_sha256"),
        code="release_pointer_hash_required",
    )
    candidate_id = value.get("candidate_id")
    if candidate_id is not None:
        candidate_id = _text(candidate_id, code="release_pointer_candidate_id_invalid")
    return {
        "release_id": release_id,
        "release_sha256": release_sha256,
        "candidate_id": candidate_id,
    }


def _pointer_for_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": OPERATIONAL_LEARNING_RELEASE_POINTER_SCHEMA_VERSION,
        "affected_artifact": candidate["affected_artifact"],
        "namespace": candidate["namespace"],
        "user_id": candidate["user_id"],
        "org_id": candidate["org_id"],
        "candidate_id": candidate["candidate_id"],
        "release_id": candidate["release_id"],
        "release_sha256": candidate["release_sha256"],
        "risk_classes": copy.deepcopy(candidate["risk_classes"]),
        "expires_at": candidate["expires_at"],
        "retest_after": candidate["retest_after"],
        "retest_requirements": copy.deepcopy(candidate["retest_requirements"]),
    }


def _candidate_without_digest(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in candidate.items()
        if key != "candidate_sha256"
    }


def _release_hash_basis(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(candidate[key])
        for key in (
            "candidate_id",
            "release_id",
            "affected_artifact",
            "namespace",
            "user_id",
            "org_id",
            "release_payload",
            "parent_release",
            "failure_evidence_packets",
            "proposal_authority",
            "risk_classes",
            "expires_at",
            "retest_after",
            "retest_requirements",
            "created_at",
        )
    }


def _validate_candidate(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningReleaseValidationError("learning_release_candidate_required")
    candidate = _json_projection(value)
    if candidate.get("schema_version") != (
        OPERATIONAL_LEARNING_RELEASE_CANDIDATE_SCHEMA_VERSION
    ):
        raise LearningReleaseValidationError(
            "learning_release_candidate_schema_invalid"
        )
    _text(candidate.get("candidate_id"), code="learning_release_candidate_id_required")
    _text(candidate.get("release_id"), code="learning_release_id_required")
    _text(
        candidate.get("affected_artifact"),
        code="learning_release_affected_artifact_required",
    )
    _text(candidate.get("namespace"), code="learning_release_namespace_required")
    _text(candidate.get("user_id"), code="learning_release_user_id_required")
    _text(candidate.get("org_id"), code="learning_release_org_id_required")
    _validate_authority_reference(
        candidate.get("proposal_authority"),
        code="learning_release_proposal_authority_required",
    )
    packets = candidate.get("failure_evidence_packets")
    if not _is_sequence(packets) or not packets:
        raise LearningReleaseValidationError(
            "learning_release_failure_evidence_required"
        )
    for packet in packets:
        _validate_failure_packet(packet)
    expected_release_hash = operational_learning_release_digest(
        _release_hash_basis(candidate)
    )
    if candidate.get("release_sha256") != expected_release_hash:
        raise LearningReleaseValidationError("learning_release_hash_mismatch")
    expected_candidate_hash = operational_learning_release_digest(
        _candidate_without_digest(candidate)
    )
    if candidate.get("candidate_sha256") != expected_candidate_hash:
        raise LearningReleaseValidationError("learning_release_candidate_hash_mismatch")
    return candidate


def _validate_persisted_release_state_integrity(state: Mapping[str, Any]) -> None:
    """Reject tampered pointers, candidate states, releases, or receipts."""

    snapshots = state.get("candidate_snapshots") or {}
    candidates: dict[str, dict[str, Any]] = {}
    for candidate_id, raw_candidate in snapshots.items():
        candidate = _validate_candidate(raw_candidate)
        if candidate.get("candidate_id") != candidate_id:
            raise LearningReleaseValidationError(
                "learning_release_candidate_snapshot_key_mismatch"
            )
        candidates[str(candidate_id)] = candidate

    raw_candidate_states = state.get("candidate_states") or {}
    if set(raw_candidate_states) != set(candidates):
        raise LearningReleaseValidationError(
            "learning_release_candidate_state_integrity_invalid"
        )
    candidate_lifecycle_by_id: dict[str, str] = {}
    for candidate_id, raw_state in raw_candidate_states.items():
        candidate = candidates.get(str(candidate_id))
        if candidate is None or not isinstance(raw_state, Mapping):
            raise LearningReleaseValidationError(
                "learning_release_candidate_state_integrity_invalid"
            )
        if raw_state.get("release_sha256") != candidate["release_sha256"]:
            raise LearningReleaseValidationError(
                "learning_release_candidate_state_hash_mismatch"
            )
        lifecycle_state = raw_state.get("state")
        if lifecycle_state not in _RELEASE_LIFECYCLE_STATES:
            raise LearningReleaseValidationError(
                "learning_release_lifecycle_state_inconsistent"
            )
        candidate_lifecycle_by_id[str(candidate_id)] = str(lifecycle_state)

    for artifact, pointer_state in (state.get("release_pointers") or {}).items():
        if not isinstance(pointer_state, Mapping):
            raise LearningReleaseValidationError("release_pointer_state_invalid")
        for pointer_name in ("active", "previous"):
            raw_pointer = pointer_state.get(pointer_name)
            if raw_pointer is None:
                continue
            pointer = _compact_release_reference(raw_pointer)
            candidate_id = pointer.get("candidate_id") if pointer else None
            candidate = candidates.get(str(candidate_id)) if candidate_id else None
            if (
                pointer is None
                or candidate is None
                or candidate["affected_artifact"] != artifact
                or candidate["release_sha256"] != pointer["release_sha256"]
                or candidate["release_id"] != pointer["release_id"]
                or _json_projection(raw_pointer) != _pointer_for_candidate(candidate)
            ):
                raise LearningReleaseValidationError(
                    "release_pointer_candidate_integrity_mismatch"
                )
            candidate_lifecycle = candidate_lifecycle_by_id.get(str(candidate_id))
            if (pointer_name == "active" and candidate_lifecycle != "active") or (
                pointer_name == "previous"
                and candidate_lifecycle not in {"superseded", "rolled_back"}
            ):
                raise LearningReleaseValidationError(
                    "learning_release_lifecycle_state_inconsistent"
                )

    raw_release_states = state.get("release_states") or {}
    expected_release_sha256s = {
        candidate["release_sha256"] for candidate in candidates.values()
    }
    if set(raw_release_states) != expected_release_sha256s:
        raise LearningReleaseValidationError("release_state_integrity_invalid")
    for release_sha256, raw_release_state in raw_release_states.items():
        if not isinstance(raw_release_state, Mapping) or not _is_sha256(release_sha256):
            raise LearningReleaseValidationError("release_state_integrity_invalid")
        candidate_id = raw_release_state.get("candidate_id")
        candidate = candidates.get(str(candidate_id)) if candidate_id else None
        if candidate is None or candidate["release_sha256"] != release_sha256:
            raise LearningReleaseValidationError(
                "release_state_candidate_integrity_mismatch"
            )
        release_lifecycle = raw_release_state.get("state")
        if (
            release_lifecycle not in _RELEASE_LIFECYCLE_STATES
            or release_lifecycle != candidate_lifecycle_by_id.get(str(candidate_id))
        ):
            raise LearningReleaseValidationError(
                "learning_release_lifecycle_state_inconsistent"
            )

    for raw_receipt in state.get("decision_receipts") or []:
        if (
            not isinstance(raw_receipt, Mapping)
            or raw_receipt.get("schema_version")
            != OPERATIONAL_LEARNING_RELEASE_RECEIPT_SCHEMA_VERSION
        ):
            raise LearningReleaseValidationError("learning_release_receipt_invalid")
        receipt = _json_projection(raw_receipt)
        observed_digest = receipt.pop("receipt_sha256", None)
        if observed_digest != operational_learning_release_digest(receipt):
            raise LearningReleaseValidationError(
                "learning_release_receipt_hash_mismatch"
            )


def register_operational_learning_release_candidate(
    state: MutableMapping[str, Any],
    *,
    candidate_id: str,
    release_id: str,
    affected_artifact: str,
    namespace: str,
    user_id: str,
    org_id: str,
    release_payload: Mapping[str, Any],
    failure_evidence_packets: Sequence[Mapping[str, Any]],
    proposal_authority: Mapping[str, Any],
    risk_classes: Sequence[str] = (),
    expires_at: str,
    retest_after: str,
    retest_requirements: Mapping[str, Any],
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist an immutable candidate snapshot and its release provenance."""

    working = _working_state(state)
    resolved_candidate_id = _text(
        candidate_id,
        code="learning_release_candidate_id_required",
    )
    resolved_release_id = _text(release_id, code="learning_release_id_required")
    resolved_artifact = _text(
        affected_artifact,
        code="learning_release_affected_artifact_required",
    )
    resolved_namespace = _text(
        namespace,
        code="learning_release_namespace_required",
    )
    resolved_user_id = _text(user_id, code="learning_release_user_id_required")
    resolved_org_id = _text(org_id, code="learning_release_org_id_required")
    payload = _json_projection(release_payload)
    authority = _validate_authority_reference(
        proposal_authority,
        code="learning_release_proposal_authority_required",
    )
    risks = sorted(
        _normalise_string_sequence(
            risk_classes,
            code="learning_release_risk_classes_invalid",
        )
    )
    created_at_iso = _now_iso(created_at)
    expires_at_iso = _timestamp(
        expires_at,
        code="learning_release_expiry_invalid",
    )
    retest_after_iso = _timestamp(
        retest_after,
        code="learning_release_retest_after_invalid",
    )
    if not (
        _timestamp_value(created_at_iso)
        <= _timestamp_value(retest_after_iso)
        <= _timestamp_value(expires_at_iso)
    ):
        raise LearningReleaseValidationError(
            "learning_release_retest_expiry_order_invalid"
        )
    retest = _json_projection(retest_requirements)
    if not isinstance(retest, Mapping) or not retest:
        raise LearningReleaseValidationError(
            "learning_release_retest_requirements_required"
        )

    packets: list[dict[str, Any]] = []
    seen_packet_ids: set[str] = set()
    for raw_packet in failure_evidence_packets:
        packet = _validate_failure_packet(raw_packet)
        if packet["affected_artifact"] != resolved_artifact:
            raise LearningReleaseValidationError(
                "failure_evidence_affected_artifact_mismatch"
            )
        if packet["packet_id"] in seen_packet_ids:
            continue
        seen_packet_ids.add(packet["packet_id"])
        packets.append(packet)
    if not packets:
        raise LearningReleaseValidationError(
            "learning_release_failure_evidence_required"
        )
    packets.sort(key=lambda item: str(item["packet_id"]))

    pointer_state = working["release_pointers"].get(resolved_artifact)
    if pointer_state is not None and not isinstance(pointer_state, Mapping):
        raise LearningReleaseValidationError("release_pointer_state_invalid")
    active_pointer = (
        pointer_state.get("active") if isinstance(pointer_state, Mapping) else None
    )
    parent_release = _compact_release_reference(active_pointer)
    candidate: dict[str, Any] = {
        "schema_version": OPERATIONAL_LEARNING_RELEASE_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": resolved_candidate_id,
        "release_id": resolved_release_id,
        "affected_artifact": resolved_artifact,
        "namespace": resolved_namespace,
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "release_payload": payload,
        "parent_release": parent_release,
        "failure_evidence_packets": packets,
        "failure_packet_sha256s": [packet["packet_sha256"] for packet in packets],
        "member_request_ids": sorted(
            {
                request_id
                for packet in packets
                for request_id in packet["member_request_ids"]
            }
        ),
        "proposal_authority": authority,
        "risk_classes": risks,
        "expires_at": expires_at_iso,
        "retest_after": retest_after_iso,
        "retest_requirements": retest,
        "created_at": created_at_iso,
    }
    candidate["release_sha256"] = operational_learning_release_digest(
        _release_hash_basis(candidate)
    )
    candidate["candidate_sha256"] = operational_learning_release_digest(candidate)
    _validate_candidate(candidate)

    existing = working["candidate_snapshots"].get(resolved_candidate_id)
    if existing is not None:
        existing_candidate = _validate_candidate(existing)
        if existing_candidate["candidate_sha256"] != candidate["candidate_sha256"]:
            raise LearningReleaseValidationError(
                "learning_release_candidate_id_conflict"
            )
        return copy.deepcopy(existing_candidate)

    working["candidate_snapshots"][resolved_candidate_id] = candidate
    working["candidate_states"][resolved_candidate_id] = {
        "state": "proposed",
        "release_sha256": candidate["release_sha256"],
        "updated_at": created_at_iso,
    }
    working["release_states"][candidate["release_sha256"]] = {
        "state": "proposed",
        "candidate_id": resolved_candidate_id,
        "updated_at": created_at_iso,
    }
    _commit_state(state, working)
    return copy.deepcopy(candidate)


def build_learning_release_experiment_evidence(
    *,
    candidate: Mapping[str, Any],
    experiment_run: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a canonical experiment run to an immutable candidate hash."""

    resolved_candidate = _validate_candidate(candidate)
    run = _json_projection(experiment_run)
    if run.get("schema_version") != EXPERIMENT_RUN_SCHEMA_VERSION:
        raise LearningReleaseValidationError("experiment_run_schema_invalid")
    _text(run.get("run_id"), code="experiment_run_id_required")
    _text(
        run.get("experiment_spec_id"),
        code="experiment_spec_id_required",
    )
    for field_name in ("namespace", "user_id", "org_id"):
        if run.get(field_name) != resolved_candidate[field_name]:
            raise LearningReleaseValidationError(
                "experiment_run_scope_mismatch",
                details={"field": field_name},
            )
    run_metadata = run.get("metadata")
    if not isinstance(run_metadata, Mapping) or (
        run_metadata.get("learning_release_candidate_id")
        != resolved_candidate["candidate_id"]
        or run_metadata.get("learning_release_candidate_release_sha256")
        != resolved_candidate["release_sha256"]
    ):
        raise LearningReleaseValidationError("experiment_run_candidate_binding_missing")
    if run.get("status") not in _EXPERIMENT_TERMINAL_STATUSES:
        raise LearningReleaseValidationError("experiment_run_not_terminal")
    if run.get("verdict") not in _EXPERIMENT_VERDICTS:
        raise LearningReleaseValidationError("experiment_verdict_invalid")
    observations = run.get("observations")
    evidence = run.get("evidence")
    if not (
        (_is_sequence(observations) and bool(observations))
        or (isinstance(evidence, Mapping) and bool(evidence))
    ):
        raise LearningReleaseValidationError("experiment_evidence_missing")
    wrapper: dict[str, Any] = {
        "schema_version": LEARNING_RELEASE_EXPERIMENT_EVIDENCE_SCHEMA_VERSION,
        "candidate_id": resolved_candidate["candidate_id"],
        "candidate_release_sha256": resolved_candidate["release_sha256"],
        "experiment_run": run,
        "experiment_run_sha256": operational_learning_release_digest(run),
    }
    wrapper["evidence_sha256"] = operational_learning_release_digest(wrapper)
    return wrapper


def _certification_report_without_digest(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in report.items()
        if key != "report_sha256"
    }


def build_learning_release_certification_evidence(
    *,
    candidate: Mapping[str, Any],
    campaign_result: Mapping[str, Any],
    execution_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a strict operational-certification report to a candidate."""

    resolved_candidate = _validate_candidate(candidate)
    report = _json_projection(campaign_result)
    if report.get("schema_version") != (
        OPERATIONAL_CERTIFICATION_CAMPAIGN_RESULT_SCHEMA_VERSION
    ):
        raise LearningReleaseValidationError("operational_certification_schema_invalid")
    _text(report.get("suite_id"), code="certification_suite_id_required")
    _require_sha256(
        report.get("contract_sha256"),
        code="certification_contract_hash_required",
    )
    report_digest = _require_sha256(
        report.get("report_sha256"),
        code="certification_report_hash_required",
    )
    if report_digest != operational_learning_release_digest(
        _certification_report_without_digest(report)
    ):
        raise LearningReleaseValidationError("certification_report_hash_mismatch")
    integrity_errors = validate_operational_certification_campaign_result_integrity(
        report
    )
    if integrity_errors:
        raise LearningReleaseValidationError(
            "operational_certification_integrity_invalid",
            details={"integrity_errors": integrity_errors},
        )
    provenance = _json_projection(execution_provenance)
    if not isinstance(provenance, Mapping):
        raise LearningReleaseValidationError(
            "certification_execution_provenance_required"
        )
    for candidate_field, provenance_field in (
        ("namespace", "effective_namespace"),
        ("user_id", "effective_user_id"),
        ("org_id", "effective_org_id"),
    ):
        if provenance.get(provenance_field) != resolved_candidate[candidate_field]:
            raise LearningReleaseValidationError(
                "certification_evidence_scope_mismatch",
                details={"field": provenance_field},
            )
    campaign_evidence = report.get("represented_campaign_evidence")
    raw_candidate_bindings = (
        campaign_evidence.get("evaluated_learning_release_candidate_bindings")
        if isinstance(campaign_evidence, Mapping)
        else None
    )
    candidate_bindings: Sequence[Any] = (
        cast(Sequence[Any], raw_candidate_bindings)
        if _is_sequence(raw_candidate_bindings)
        else ()
    )
    exact_candidate_binding_found = False
    for raw_binding in candidate_bindings:
        if not isinstance(raw_binding, Mapping):
            raise LearningReleaseValidationError(
                "certification_campaign_candidate_binding_invalid"
            )
        candidate_id = _text(
            raw_binding.get("candidate_id"),
            code="certification_campaign_candidate_binding_invalid",
        )
        release_sha256 = _require_sha256(
            raw_binding.get("candidate_release_sha256"),
            code="certification_campaign_candidate_binding_invalid",
        )
        if (
            candidate_id == resolved_candidate["candidate_id"]
            and release_sha256 == resolved_candidate["release_sha256"]
        ):
            exact_candidate_binding_found = True
    if not exact_candidate_binding_found:
        raise LearningReleaseValidationError(
            "certification_campaign_candidate_binding_missing"
        )
    wrapper: dict[str, Any] = {
        "schema_version": LEARNING_RELEASE_CERTIFICATION_EVIDENCE_SCHEMA_VERSION,
        "candidate_id": resolved_candidate["candidate_id"],
        "candidate_release_sha256": resolved_candidate["release_sha256"],
        "campaign_result": report,
        "execution_provenance": provenance,
        "certification_report_sha256": report_digest,
    }
    wrapper["evidence_sha256"] = operational_learning_release_digest(wrapper)
    return wrapper


def _validate_evidence_wrapper(
    *,
    candidate: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    experiment = _json_projection(experiment_evidence)
    certification = _json_projection(certification_evidence)
    expected_bindings = {
        "candidate_id": candidate["candidate_id"],
        "candidate_release_sha256": candidate["release_sha256"],
    }
    if experiment.get("schema_version") != (
        LEARNING_RELEASE_EXPERIMENT_EVIDENCE_SCHEMA_VERSION
    ):
        raise LearningReleaseValidationError(
            "learning_release_experiment_evidence_invalid"
        )
    if certification.get("schema_version") != (
        LEARNING_RELEASE_CERTIFICATION_EVIDENCE_SCHEMA_VERSION
    ):
        raise LearningReleaseValidationError(
            "learning_release_certification_evidence_invalid"
        )
    for field, expected in expected_bindings.items():
        if experiment.get(field) != expected:
            raise LearningReleaseValidationError(
                "experiment_evidence_candidate_binding_mismatch"
            )
        if certification.get(field) != expected:
            raise LearningReleaseValidationError(
                "certification_evidence_candidate_binding_mismatch"
            )
    rebuilt_experiment = build_learning_release_experiment_evidence(
        candidate=candidate,
        experiment_run=experiment.get("experiment_run"),
    )
    rebuilt_certification = build_learning_release_certification_evidence(
        candidate=candidate,
        campaign_result=certification.get("campaign_result"),
        execution_provenance=certification.get("execution_provenance"),
    )
    if experiment != rebuilt_experiment:
        raise LearningReleaseValidationError(
            "learning_release_experiment_evidence_hash_mismatch"
        )
    if certification != rebuilt_certification:
        raise LearningReleaseValidationError(
            "learning_release_certification_evidence_hash_mismatch"
        )
    return experiment, certification


def _evaluator_digest_only_reference(
    value: Any,
    *,
    source_path: str,
) -> dict[str, Any]:
    """Describe omitted evaluator material without silently discarding it."""

    projected = _json_projection(value)
    encoded = json.dumps(
        projected,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    reference: dict[str, Any] = {
        "schema_version": (
            LEARNING_RELEASE_EVALUATOR_DIGEST_ONLY_REFERENCE_SCHEMA_VERSION
        ),
        "source_path": source_path,
        "source_sha256": hashlib.sha256(encoded).hexdigest(),
        "source_json_bytes": len(encoded),
        "inline": False,
        "evidence_availability": "digest_only",
    }
    if isinstance(projected, Mapping):
        reference["source_kind"] = "mapping"
        reference["source_mapping_keys"] = sorted(projected)
    elif _is_sequence(projected):
        reference["source_kind"] = "sequence"
        reference["source_item_count"] = len(projected)
    elif isinstance(projected, str):
        reference["source_kind"] = "string"
        reference["source_character_count"] = len(projected)
    else:
        reference["source_kind"] = type(projected).__name__
    return reference


def build_learning_release_evaluator_evidence_projection(
    *,
    candidate: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Project validated evidence for represented evaluator LLM context.

    The full immutable wrappers remain the persistence authority.  This
    projection removes only the two large, repeated support payloads within
    experiment observations.  Their exact digests, sizes, kinds and keys stay
    visible so the represented evaluator can recognise digest-only evidence as
    unavailable rather than silently treating omitted material as supportive.
    """

    resolved_candidate = _validate_candidate(candidate)
    experiment, certification = _validate_evidence_wrapper(
        candidate=resolved_candidate,
        experiment_evidence=experiment_evidence,
        certification_evidence=certification_evidence,
    )
    experiment_run = experiment["experiment_run"]
    run_fields = {
        key: copy.deepcopy(value)
        for key, value in experiment_run.items()
        if key != "observations"
    }
    observation_projections: list[dict[str, Any]] = []
    digest_only_material: list[dict[str, Any]] = []
    for index, raw_observation in enumerate(experiment_run.get("observations") or []):
        observation_path = f"experiment_run.observations[{index}]"
        if not isinstance(raw_observation, Mapping):
            reference = _evaluator_digest_only_reference(
                raw_observation,
                source_path=observation_path,
            )
            observation_projections.append(
                {
                    "source_observation_sha256": operational_learning_release_digest(
                        raw_observation
                    ),
                    "projected_observation": reference,
                    "digest_only_fields": [observation_path],
                }
            )
            digest_only_material.append(reference)
            continue

        projected_observation = _json_projection(raw_observation)
        digest_only_fields: list[str] = []
        for field_name in ("evidence", "execution_provenance"):
            if field_name not in projected_observation:
                continue
            source_path = f"{observation_path}.{field_name}"
            reference = _evaluator_digest_only_reference(
                projected_observation[field_name],
                source_path=source_path,
            )
            projected_observation[field_name] = reference
            digest_only_fields.append(source_path)
            digest_only_material.append(copy.deepcopy(reference))
        observation_projections.append(
            {
                "source_observation_sha256": operational_learning_release_digest(
                    raw_observation
                ),
                "projected_observation": projected_observation,
                "digest_only_fields": digest_only_fields,
            }
        )

    projection: dict[str, Any] = {
        "schema_version": (
            LEARNING_RELEASE_EVALUATOR_EVIDENCE_PROJECTION_SCHEMA_VERSION
        ),
        "candidate_binding": {
            "candidate_id": resolved_candidate["candidate_id"],
            "candidate_release_sha256": resolved_candidate["release_sha256"],
        },
        "experiment_evidence": {
            "schema_version": experiment["schema_version"],
            "candidate_id": experiment["candidate_id"],
            "candidate_release_sha256": experiment["candidate_release_sha256"],
            "evidence_sha256": experiment["evidence_sha256"],
            "experiment_run_sha256": experiment["experiment_run_sha256"],
            "experiment_run_fields": run_fields,
            "observation_projections": observation_projections,
        },
        "certification_evidence": copy.deepcopy(certification),
        "digest_only_material": digest_only_material,
    }
    projection["projection_sha256"] = operational_learning_release_digest(projection)
    return projection


def _validate_represented_decision(
    value: Any,
    *,
    candidate: Mapping[str, Any],
    action: str,
    experiment: Mapping[str, Any],
    certification: Mapping[str, Any],
) -> dict[str, Any]:
    if action not in _DECISIONS:
        raise LearningReleaseValidationError("learning_release_decision_invalid")
    if not isinstance(value, Mapping):
        raise LearningReleaseValidationError(
            "represented_learning_release_decision_required"
        )
    decision = _json_projection(value)
    if decision.get("schema_version") != (
        REPRESENTED_LEARNING_RELEASE_DECISION_SCHEMA_VERSION
    ):
        raise LearningReleaseValidationError(
            "represented_learning_release_decision_schema_invalid"
        )
    _text(decision.get("decision_id"), code="represented_decision_id_required")
    if decision.get("decision") != action:
        raise LearningReleaseValidationError("represented_decision_action_mismatch")
    if decision.get("candidate_id") != candidate["candidate_id"]:
        raise LearningReleaseValidationError("represented_decision_candidate_mismatch")
    if decision.get("candidate_release_sha256") != candidate["release_sha256"]:
        raise LearningReleaseValidationError(
            "represented_decision_release_hash_mismatch"
        )
    for field_name in ("namespace", "user_id", "org_id"):
        if decision.get(field_name) != candidate[field_name]:
            raise LearningReleaseValidationError(
                "represented_decision_scope_mismatch",
                details={"field": field_name},
            )
    _validate_authority_reference(
        decision.get("authority"),
        code="represented_evaluator_authority_required",
    )
    _timestamp(
        decision.get("decided_at"),
        code="represented_decision_timestamp_invalid",
    )
    if decision.get("experiment_evidence_sha256") != experiment.get("evidence_sha256"):
        raise LearningReleaseValidationError(
            "represented_decision_experiment_evidence_mismatch"
        )
    if decision.get("certification_evidence_sha256") != certification.get(
        "evidence_sha256"
    ):
        raise LearningReleaseValidationError(
            "represented_decision_certification_evidence_mismatch"
        )
    _text(
        decision.get("rationale"),
        code="represented_decision_rationale_required",
    )
    return decision


def _requires_human_approval(candidate: Mapping[str, Any]) -> bool:
    return bool(
        HUMAN_APPROVAL_REQUIRED_RISK_CLASSES.intersection(
            set(candidate.get("risk_classes") or [])
        )
    )


def _validate_human_approval(
    value: Any,
    *,
    candidate: Mapping[str, Any],
    action: str,
) -> dict[str, Any] | None:
    if value is None:
        if action != "reject" and _requires_human_approval(candidate):
            raise LearningReleaseValidationError(
                "human_approval_required_for_high_risk_release"
            )
        return None
    if not isinstance(value, Mapping):
        raise LearningReleaseValidationError("human_learning_release_approval_invalid")
    approval = _json_projection(value)
    if approval.get("schema_version") != HUMAN_LEARNING_RELEASE_APPROVAL_SCHEMA_VERSION:
        raise LearningReleaseValidationError("human_learning_release_approval_invalid")
    _text(approval.get("approval_id"), code="human_approval_id_required")
    _text(approval.get("approver_id"), code="human_approver_id_required")
    if approval.get("approved") is not True:
        raise LearningReleaseValidationError("human_learning_release_approval_denied")
    if approval.get("action") != action:
        raise LearningReleaseValidationError("human_approval_action_mismatch")
    if approval.get("candidate_id") != candidate["candidate_id"]:
        raise LearningReleaseValidationError("human_approval_candidate_mismatch")
    if approval.get("candidate_release_sha256") != candidate["release_sha256"]:
        raise LearningReleaseValidationError("human_approval_release_hash_mismatch")
    for field_name in ("namespace", "user_id", "org_id"):
        if approval.get(field_name) != candidate[field_name]:
            raise LearningReleaseValidationError(
                "human_approval_scope_mismatch",
                details={"field": field_name},
            )
    _timestamp(approval.get("approved_at"), code="human_approval_timestamp_invalid")
    authenticated_receipt = approval.get("authenticated_approval_receipt")
    if not isinstance(authenticated_receipt, Mapping) or (
        authenticated_receipt.get("schema_version")
        != AUTHENTICATED_HUMAN_APPROVAL_RECEIPT_SCHEMA_VERSION
    ):
        raise LearningReleaseValidationError(
            "authenticated_human_approval_receipt_required"
        )
    receipt = _json_projection(authenticated_receipt)
    observed_receipt_digest = receipt.pop("receipt_sha256", None)
    if observed_receipt_digest != operational_learning_release_digest(receipt):
        raise LearningReleaseValidationError(
            "authenticated_human_approval_receipt_hash_mismatch"
        )
    for approval_field, receipt_field in (
        ("approval_id", "approval_id"),
        ("approver_id", "approver_id"),
        ("action", "action"),
        ("candidate_id", "candidate_id"),
        ("candidate_release_sha256", "candidate_release_sha256"),
        ("namespace", "namespace"),
        ("user_id", "user_id"),
        ("org_id", "org_id"),
        ("approved_at", "approved_at"),
    ):
        if approval.get(approval_field) != receipt.get(receipt_field):
            raise LearningReleaseValidationError(
                "authenticated_human_approval_receipt_binding_mismatch",
                details={"field": receipt_field},
            )
    _timestamp(
        receipt.get("authenticated_at"),
        code="authenticated_human_approval_receipt_timestamp_invalid",
    )
    return approval


def _candidate_from_state(
    working: Mapping[str, Any],
    candidate_id: str,
) -> dict[str, Any]:
    snapshots = working.get("candidate_snapshots")
    candidate = snapshots.get(candidate_id) if isinstance(snapshots, Mapping) else None
    if candidate is None:
        raise LearningReleaseValidationError("learning_release_candidate_not_found")
    return _validate_candidate(candidate)


def _candidate_state(
    working: Mapping[str, Any],
    candidate_id: str,
) -> dict[str, Any]:
    states = working.get("candidate_states")
    state = states.get(candidate_id) if isinstance(states, Mapping) else None
    if not isinstance(state, Mapping):
        raise LearningReleaseValidationError("learning_release_candidate_state_missing")
    return dict(state)


def _existing_receipt(
    working: Mapping[str, Any],
    decision_id: str,
) -> dict[str, Any] | None:
    for item in working.get("decision_receipts") or []:
        if isinstance(item, Mapping) and item.get("decision_id") == decision_id:
            return dict(item)
    return None


def _build_receipt(
    *,
    action: str,
    outcome: str,
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
    experiment: Mapping[str, Any],
    certification: Mapping[str, Any],
    human_approval: Mapping[str, Any] | None,
    prior_active: Mapping[str, Any] | None,
    resulting_active: Mapping[str, Any] | None,
    resulting_previous: Mapping[str, Any] | None,
    recorded_at: str,
) -> dict[str, Any]:
    receipt_identity = operational_learning_release_digest(
        {
            "decision_id": decision["decision_id"],
            "action": action,
            "candidate_release_sha256": candidate["release_sha256"],
        }
    )
    receipt: dict[str, Any] = {
        "schema_version": OPERATIONAL_LEARNING_RELEASE_RECEIPT_SCHEMA_VERSION,
        "receipt_id": f"learning-release-{receipt_identity[:24]}",
        "decision_id": decision["decision_id"],
        "action": action,
        "outcome": outcome,
        "state": outcome,
        "affected_artifact": candidate["affected_artifact"],
        "candidate_id": candidate["candidate_id"],
        "release_id": candidate["release_id"],
        "release_sha256": candidate["release_sha256"],
        "candidate_sha256": candidate["candidate_sha256"],
        "prior_active": _json_projection(prior_active),
        "resulting_active": _json_projection(resulting_active),
        "resulting_previous": _json_projection(resulting_previous),
        "active_pointer_changed": (
            _compact_release_reference(prior_active)
            != _compact_release_reference(resulting_active)
        ),
        "represented_decision_sha256": operational_learning_release_digest(decision),
        "represented_evaluator_authority": copy.deepcopy(decision["authority"]),
        "experiment_evidence_sha256": experiment["evidence_sha256"],
        "certification_evidence_sha256": certification["evidence_sha256"],
        "human_approval_id": (
            human_approval.get("approval_id") if human_approval else None
        ),
        "human_approval_sha256": (
            operational_learning_release_digest(human_approval)
            if human_approval
            else None
        ),
        "expires_at": candidate["expires_at"],
        "retest_after": candidate["retest_after"],
        "retest_requirements": copy.deepcopy(candidate["retest_requirements"]),
        "recovery_affordances": project_learning_release_recovery_affordances(
            action=action,
            prior_active=prior_active,
            resulting_active=resulting_active,
            resulting_previous=resulting_previous,
        ),
        "recorded_at": recorded_at,
    }
    receipt["receipt_sha256"] = operational_learning_release_digest(receipt)
    return receipt


def project_learning_release_recovery_affordances(
    *,
    action: str,
    prior_active: Mapping[str, Any] | None,
    resulting_active: Mapping[str, Any] | None,
    resulting_previous: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Expose generic inspect/retest/revision/rollback opportunities.

    This reports available mechanics only. Represented workflow/prompt authority
    still decides whether and how to use them.
    """

    affordances: list[dict[str, Any]] = [
        {"action_type": "inspect_release_evidence"},
        {"action_type": "retest_candidate"},
    ]
    if action == "reject":
        affordances.append({"action_type": "revise_candidate"})
        if resulting_active is not None:
            affordances.append(
                {
                    "action_type": "retain_active_release",
                    "release": _compact_release_reference(resulting_active),
                }
            )
    if action == "promote" and prior_active is not None:
        affordances.append(
            {
                "action_type": "rollback",
                "release": _compact_release_reference(prior_active),
            }
        )
    if action == "rollback" and resulting_previous is not None:
        affordances.append(
            {
                "action_type": "inspect_replaced_release",
                "release": _compact_release_reference(resulting_previous),
            }
        )
    return affordances


def _validate_common_decision_inputs(
    working: Mapping[str, Any],
    *,
    candidate_id: str,
    action: str,
    represented_evaluator_decision: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
    human_approval: Mapping[str, Any] | None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any] | None,
]:
    candidate = _candidate_from_state(working, candidate_id)
    experiment, certification = _validate_evidence_wrapper(
        candidate=candidate,
        experiment_evidence=experiment_evidence,
        certification_evidence=certification_evidence,
    )
    decision = _validate_represented_decision(
        represented_evaluator_decision,
        candidate=candidate,
        action=action,
        experiment=experiment,
        certification=certification,
    )
    approval = _validate_human_approval(
        human_approval,
        candidate=candidate,
        action=action,
    )
    return candidate, experiment, certification, decision, approval


def promote_operational_learning_release_candidate(
    state: MutableMapping[str, Any],
    *,
    candidate_id: str,
    represented_evaluator_decision: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
    human_approval: Mapping[str, Any] | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Promote only explicit, evidence-bound represented decisions."""

    working = _working_state(state)
    candidate, experiment, certification, decision, approval = (
        _validate_common_decision_inputs(
            working,
            candidate_id=candidate_id,
            action="promote",
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
            human_approval=human_approval,
        )
    )
    existing = _existing_receipt(working, str(decision["decision_id"]))
    if existing is not None:
        if (
            existing.get("action") == "promote"
            and existing.get("release_sha256") == candidate["release_sha256"]
        ):
            return copy.deepcopy(existing)
        raise LearningReleaseValidationError("represented_decision_id_conflict")
    if _candidate_state(working, candidate_id).get("state") != "proposed":
        raise LearningReleaseValidationError("learning_release_candidate_not_proposed")
    experiment_run = experiment["experiment_run"]
    campaign_result = certification["campaign_result"]
    if (
        experiment_run.get("status") != "completed"
        or experiment_run.get("verdict") != "pass"
    ):
        raise LearningReleaseValidationError(
            "promotion_requires_passing_experiment_evidence"
        )
    if campaign_result.get("certified") is not True:
        raise LearningReleaseValidationError(
            "promotion_requires_certified_campaign_evidence"
        )
    if campaign_result.get("suite_source") != "vontology":
        raise LearningReleaseValidationError(
            "promotion_requires_live_vontology_certification"
        )
    if campaign_result.get("experiment_persistence_complete") is not True:
        raise LearningReleaseValidationError(
            "promotion_requires_persisted_certification_evidence"
        )
    gate_results = campaign_result.get("certification_gate_results") or []
    live_gate = next(
        (
            item
            for item in gate_results
            if isinstance(item, Mapping)
            and item.get("gate_id") == "live_release_evidence_eligible"
        ),
        None,
    )
    if not isinstance(live_gate, Mapping) or live_gate.get("passed") is not True:
        raise LearningReleaseValidationError(
            "promotion_requires_live_release_evidence_gate"
        )
    now_iso = _now_iso(recorded_at)
    if _timestamp_value(candidate["expires_at"]) <= _timestamp_value(now_iso):
        raise LearningReleaseValidationError("learning_release_candidate_expired")
    if _timestamp_value(candidate["retest_after"]) <= _timestamp_value(now_iso):
        raise LearningReleaseValidationError("learning_release_candidate_retest_due")

    artifact = str(candidate["affected_artifact"])
    pointer_state = working["release_pointers"].get(artifact)
    if pointer_state is not None and not isinstance(pointer_state, Mapping):
        raise LearningReleaseValidationError("release_pointer_state_invalid")
    prior_active = (
        copy.deepcopy(pointer_state.get("active"))
        if isinstance(pointer_state, Mapping)
        else None
    )
    if _compact_release_reference(prior_active) != candidate["parent_release"]:
        raise LearningReleaseValidationError("learning_release_parent_changed")
    active_pointer = _pointer_for_candidate(candidate)
    working["release_pointers"][artifact] = {
        "active": active_pointer,
        "previous": prior_active,
    }
    prior_reference = _compact_release_reference(prior_active)
    if prior_reference is not None:
        working["release_states"][prior_reference["release_sha256"]] = {
            "state": "superseded",
            "candidate_id": prior_reference.get("candidate_id"),
            "updated_at": now_iso,
        }
        prior_candidate_id = prior_reference.get("candidate_id")
        if prior_candidate_id in working["candidate_states"]:
            working["candidate_states"][prior_candidate_id] = {
                "state": "superseded",
                "release_sha256": prior_reference["release_sha256"],
                "updated_at": now_iso,
            }
    working["candidate_states"][candidate_id] = {
        "state": "active",
        "release_sha256": candidate["release_sha256"],
        "updated_at": now_iso,
    }
    working["release_states"][candidate["release_sha256"]] = {
        "state": "active",
        "candidate_id": candidate_id,
        "updated_at": now_iso,
        "expires_at": candidate["expires_at"],
        "retest_after": candidate["retest_after"],
        "retest_requirements": copy.deepcopy(candidate["retest_requirements"]),
    }
    receipt = _build_receipt(
        action="promote",
        outcome="promoted",
        candidate=candidate,
        decision=decision,
        experiment=experiment,
        certification=certification,
        human_approval=approval,
        prior_active=prior_active,
        resulting_active=active_pointer,
        resulting_previous=prior_active,
        recorded_at=now_iso,
    )
    working["decision_receipts"].append(receipt)
    _commit_state(state, working)
    return copy.deepcopy(receipt)


def reject_operational_learning_release_candidate(
    state: MutableMapping[str, Any],
    *,
    candidate_id: str,
    represented_evaluator_decision: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Record a represented rejection without changing release pointers."""

    working = _working_state(state)
    candidate, experiment, certification, decision, _approval = (
        _validate_common_decision_inputs(
            working,
            candidate_id=candidate_id,
            action="reject",
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
            human_approval=None,
        )
    )
    existing = _existing_receipt(working, str(decision["decision_id"]))
    if existing is not None:
        if (
            existing.get("action") == "reject"
            and existing.get("release_sha256") == candidate["release_sha256"]
        ):
            return copy.deepcopy(existing)
        raise LearningReleaseValidationError("represented_decision_id_conflict")
    if _candidate_state(working, candidate_id).get("state") != "proposed":
        raise LearningReleaseValidationError("learning_release_candidate_not_proposed")
    now_iso = _now_iso(recorded_at)
    artifact = str(candidate["affected_artifact"])
    pointer_state = working["release_pointers"].get(artifact)
    if pointer_state is not None and not isinstance(pointer_state, Mapping):
        raise LearningReleaseValidationError("release_pointer_state_invalid")
    active_pointer = (
        copy.deepcopy(pointer_state.get("active"))
        if isinstance(pointer_state, Mapping)
        else None
    )
    previous_pointer = (
        copy.deepcopy(pointer_state.get("previous"))
        if isinstance(pointer_state, Mapping)
        else None
    )
    working["candidate_states"][candidate_id] = {
        "state": "rejected",
        "release_sha256": candidate["release_sha256"],
        "updated_at": now_iso,
    }
    working["release_states"][candidate["release_sha256"]] = {
        "state": "rejected",
        "candidate_id": candidate_id,
        "updated_at": now_iso,
    }
    receipt = _build_receipt(
        action="reject",
        outcome="rejected",
        candidate=candidate,
        decision=decision,
        experiment=experiment,
        certification=certification,
        human_approval=None,
        prior_active=active_pointer,
        resulting_active=active_pointer,
        resulting_previous=previous_pointer,
        recorded_at=now_iso,
    )
    working["decision_receipts"].append(receipt)
    _commit_state(state, working)
    return copy.deepcopy(receipt)


def rollback_operational_learning_release(
    state: MutableMapping[str, Any],
    *,
    affected_artifact: str,
    represented_evaluator_decision: Mapping[str, Any],
    experiment_evidence: Mapping[str, Any],
    certification_evidence: Mapping[str, Any],
    human_approval: Mapping[str, Any] | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Restore the exact previous release hash and mark the active one rolled back."""

    working = _working_state(state)
    artifact = _text(
        affected_artifact,
        code="learning_release_affected_artifact_required",
    )
    pointer_state = working["release_pointers"].get(artifact)
    if not isinstance(pointer_state, Mapping):
        raise LearningReleaseValidationError("learning_release_pointer_missing")
    active_pointer = pointer_state.get("active")
    previous_pointer = pointer_state.get("previous")
    active_reference = _compact_release_reference(active_pointer)
    restored_reference = _compact_release_reference(previous_pointer)
    if active_reference is None:
        raise LearningReleaseValidationError("active_learning_release_missing")
    if restored_reference is None:
        raise LearningReleaseValidationError("previous_learning_release_missing")
    active_candidate_id = active_reference.get("candidate_id")
    if not active_candidate_id:
        raise LearningReleaseValidationError(
            "active_learning_release_candidate_missing"
        )

    candidate, experiment, certification, decision, approval = (
        _validate_common_decision_inputs(
            working,
            candidate_id=str(active_candidate_id),
            action="rollback",
            represented_evaluator_decision=represented_evaluator_decision,
            experiment_evidence=experiment_evidence,
            certification_evidence=certification_evidence,
            human_approval=human_approval,
        )
    )
    if candidate["affected_artifact"] != artifact:
        raise LearningReleaseValidationError("rollback_affected_artifact_mismatch")
    if active_reference["release_sha256"] != candidate["release_sha256"]:
        raise LearningReleaseValidationError("rollback_active_release_hash_mismatch")
    existing = _existing_receipt(working, str(decision["decision_id"]))
    if existing is not None:
        if (
            existing.get("action") == "rollback"
            and existing.get("release_sha256") == candidate["release_sha256"]
        ):
            return copy.deepcopy(existing)
        raise LearningReleaseValidationError("represented_decision_id_conflict")
    if _candidate_state(working, str(active_candidate_id)).get("state") != "active":
        raise LearningReleaseValidationError("rollback_candidate_not_active")

    now_iso = _now_iso(recorded_at)
    restored_candidate_id = restored_reference.get("candidate_id")
    if not restored_candidate_id:
        raise LearningReleaseValidationError("rollback_previous_candidate_missing")
    restored_candidate = _candidate_from_state(
        working,
        str(restored_candidate_id),
    )
    if _timestamp_value(restored_candidate["expires_at"]) <= _timestamp_value(now_iso):
        raise LearningReleaseValidationError("rollback_previous_release_expired")
    if _timestamp_value(restored_candidate["retest_after"]) <= _timestamp_value(
        now_iso
    ):
        raise LearningReleaseValidationError("rollback_previous_release_retest_due")
    restored_pointer = _json_projection(previous_pointer)
    rolled_back_pointer = _json_projection(active_pointer)
    working["release_pointers"][artifact] = {
        "active": restored_pointer,
        "previous": rolled_back_pointer,
    }
    working["candidate_states"][str(active_candidate_id)] = {
        "state": "rolled_back",
        "release_sha256": candidate["release_sha256"],
        "updated_at": now_iso,
    }
    working["release_states"][candidate["release_sha256"]] = {
        "state": "rolled_back",
        "candidate_id": active_candidate_id,
        "updated_at": now_iso,
    }
    if restored_candidate_id in working["candidate_states"]:
        working["candidate_states"][restored_candidate_id] = {
            "state": "active",
            "release_sha256": restored_reference["release_sha256"],
            "updated_at": now_iso,
        }
    working["release_states"][restored_reference["release_sha256"]] = {
        "state": "active",
        "candidate_id": restored_candidate_id,
        "updated_at": now_iso,
        "restored_by_rollback": True,
    }
    receipt = _build_receipt(
        action="rollback",
        outcome="rolled_back",
        candidate=candidate,
        decision=decision,
        experiment=experiment,
        certification=certification,
        human_approval=approval,
        prior_active=rolled_back_pointer,
        resulting_active=restored_pointer,
        resulting_previous=rolled_back_pointer,
        recorded_at=now_iso,
    )
    if receipt["resulting_active"]["release_sha256"] != (
        restored_reference["release_sha256"]
    ):
        raise LearningReleaseValidationError("rollback_exact_hash_restore_failed")
    working["decision_receipts"].append(receipt)
    _commit_state(state, working)
    return copy.deepcopy(receipt)


__all__ = [
    "AUTHENTICATED_HUMAN_APPROVAL_RECEIPT_SCHEMA_VERSION",
    "FAILURE_EVIDENCE_PACKET_SCHEMA_VERSION",
    "HUMAN_APPROVAL_REQUIRED_RISK_CLASSES",
    "HUMAN_LEARNING_RELEASE_APPROVAL_SCHEMA_VERSION",
    "LEARNING_RELEASE_CERTIFICATION_EVIDENCE_SCHEMA_VERSION",
    "LEARNING_RELEASE_EVALUATOR_DIGEST_ONLY_REFERENCE_SCHEMA_VERSION",
    "LEARNING_RELEASE_EVALUATOR_EVIDENCE_PROJECTION_SCHEMA_VERSION",
    "LEARNING_RELEASE_EXPERIMENT_EVIDENCE_SCHEMA_VERSION",
    "OPERATIONAL_LEARNING_RELEASE_CANDIDATE_SCHEMA_VERSION",
    "OPERATIONAL_LEARNING_RELEASE_POINTER_SCHEMA_VERSION",
    "OPERATIONAL_LEARNING_RELEASE_RECEIPT_SCHEMA_VERSION",
    "OPERATIONAL_LEARNING_RELEASE_STATE_SCHEMA_VERSION",
    "REPRESENTED_AUTHORITY_REFERENCE_SCHEMA_VERSION",
    "REPRESENTED_LEARNING_RELEASE_DECISION_SCHEMA_VERSION",
    "LearningReleaseValidationError",
    "build_empty_operational_learning_release_state",
    "build_failure_evidence_packets",
    "build_learning_release_certification_evidence",
    "build_learning_release_evaluator_evidence_projection",
    "build_learning_release_experiment_evidence",
    "operational_learning_release_digest",
    "project_learning_release_recovery_affordances",
    "promote_operational_learning_release_candidate",
    "register_operational_learning_release_candidate",
    "reject_operational_learning_release_candidate",
    "rollback_operational_learning_release",
    "validate_operational_learning_release_state",
]
