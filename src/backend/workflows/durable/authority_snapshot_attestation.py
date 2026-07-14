"""Generic provenance for exact durable-workflow authority snapshots.

The attestation lives on the durable instance document, outside caller- and
workflow-controlled context.  It binds the small authority payload consumed by
the workflow-execute bridge to one lock-fenced worker claim and checkpoint.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY = (
    "durable_exact_workflow_authority_snapshot.v1"
)
DURABLE_WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION = (
    "durable_worker_claim_provenance.v1"
)
DURABLE_AUTHORITY_CHECKPOINT_ATTESTATION_SCHEMA_VERSION = (
    "durable_authority_checkpoint_attestation.v1"
)
DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY = (
    "durable_executed_workflow_definition_identity"
)
DURABLE_AUTHORITY_CHECKPOINT_ATTESTATION_FIELD = (
    "authority_checkpoint_attestation"
)

WORKFLOW_AUTHORITY_OUTPUT_KEY = "workflow_authority_output"
PROMPT_CONTEXT_DIAGNOSTICS_KEY = "prompt_context_diagnostics"
RESERVED_AUTHORITY_CONTEXT_KEYS = (
    WORKFLOW_AUTHORITY_OUTPUT_KEY,
    PROMPT_CONTEXT_DIAGNOSTICS_KEY,
    DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY,
)

_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "instance_id",
        "workflow_id",
        "current_state",
        "step_index",
        "claim_token",
        "worker_id",
        "authority_payload_sha256",
        "executed_definition_hash",
        "exact_snapshot_eligible",
        "ineligibility_reasons",
    }
)


def _clean_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_sha256(value: Any) -> bool:
    text = _clean_text(value)
    return bool(
        text
        and len(text) == 64
        and all(character in "0123456789abcdef" for character in text.lower())
    )


def _authority_payload(workflow_data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: workflow_data.get(key)
        for key in RESERVED_AUTHORITY_CONTEXT_KEYS
    }


def authority_payload_sha256(workflow_data: Mapping[str, Any]) -> str | None:
    """Digest the exact bounded authority payload, or fail closed."""

    try:
        encoded = json.dumps(
            _authority_payload(workflow_data),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def worker_claim_supports_exact_authority_snapshot(
    claimed_by_build: Mapping[str, Any] | None,
    *,
    expected_worker_id: str | None = None,
) -> bool:
    if not isinstance(claimed_by_build, Mapping):
        return False
    worker_id = _clean_text(claimed_by_build.get("worker_id"))
    if (
        claimed_by_build.get("schema_version")
        != DURABLE_WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION
        or worker_id is None
        or (
            expected_worker_id is not None
            and worker_id != expected_worker_id
        )
        or not any(
            _clean_text(claimed_by_build.get(field))
            for field in (
                "version",
                "version_base",
                "legacy_app_version",
                "git_commit",
                "git_short_commit",
            )
        )
    ):
        return False
    capabilities = claimed_by_build.get("capabilities")
    if not isinstance(capabilities, Sequence) or isinstance(
        capabilities, (str, bytes, bytearray)
    ):
        return False
    return EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY in {
        str(item).strip() for item in capabilities if str(item).strip()
    }


def build_authority_checkpoint_attestation(
    *,
    instance_id: str,
    workflow_id: str,
    current_state: str,
    step_index: int,
    claim_token: str,
    worker_id: str,
    workflow_data: Mapping[str, Any],
    exact_snapshot_eligible: bool,
    ineligibility_reasons: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Build the strict v1 checkpoint attestation for manager persistence."""

    instance_id_clean = _clean_text(instance_id)
    workflow_id_clean = _clean_text(workflow_id)
    state_clean = _clean_text(current_state)
    claim_token_clean = _clean_text(claim_token)
    worker_id_clean = _clean_text(worker_id)
    payload_digest = authority_payload_sha256(workflow_data)
    identity = workflow_data.get(
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY
    )
    definition_hash = (
        _clean_text(identity.get("definition_hash"))
        if isinstance(identity, Mapping)
        else None
    )
    if (
        not instance_id_clean
        or not workflow_id_clean
        or not state_clean
        or not claim_token_clean
        or not worker_id_clean
        or not isinstance(step_index, int)
        or isinstance(step_index, bool)
        or step_index < 0
        or payload_digest is None
        or not _is_sha256(definition_hash)
    ):
        return None
    reasons = sorted(
        {
            reason
            for item in ineligibility_reasons
            if (reason := _clean_text(item)) is not None
        }
    )
    eligible = bool(exact_snapshot_eligible) and not reasons
    if not eligible and not reasons:
        reasons = ["exact_snapshot_eligibility_unverified"]
    return {
        "schema_version": DURABLE_AUTHORITY_CHECKPOINT_ATTESTATION_SCHEMA_VERSION,
        "instance_id": instance_id_clean,
        "workflow_id": workflow_id_clean,
        "current_state": state_clean,
        "step_index": step_index,
        "claim_token": claim_token_clean,
        "worker_id": worker_id_clean,
        "authority_payload_sha256": payload_digest,
        "executed_definition_hash": definition_hash,
        "exact_snapshot_eligible": eligible,
        "ineligibility_reasons": reasons,
    }


def validate_authority_checkpoint_attestation(
    value: Mapping[str, Any] | None,
    *,
    instance_id: str,
    workflow_id: str,
    current_state: str,
    step_index: int,
    workflow_data: Mapping[str, Any],
    expected_claim_token: str | None = None,
    expected_worker_id: str | None = None,
    require_exact_eligible: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate an attestation against the currently persisted checkpoint."""

    if not isinstance(value, Mapping) or set(value) != _ATTESTATION_FIELDS:
        return None, "authority_checkpoint_attestation_shape_invalid"
    reasons = value.get("ineligibility_reasons")
    if not isinstance(reasons, list) or any(
        not isinstance(item, str) or not item.strip() for item in reasons
    ):
        return None, "authority_checkpoint_attestation_reasons_invalid"
    if len(reasons) != len(set(reasons)) or reasons != sorted(reasons):
        return None, "authority_checkpoint_attestation_reasons_invalid"
    eligible = value.get("exact_snapshot_eligible")
    if not isinstance(eligible, bool) or eligible == bool(reasons):
        return None, "authority_checkpoint_attestation_eligibility_invalid"
    if (
        value.get("schema_version")
        != DURABLE_AUTHORITY_CHECKPOINT_ATTESTATION_SCHEMA_VERSION
        or value.get("instance_id") != instance_id
        or value.get("workflow_id") != workflow_id
        or value.get("current_state") != current_state
        or value.get("step_index") != step_index
        or not _clean_text(value.get("claim_token"))
        or not _clean_text(value.get("worker_id"))
        or (
            expected_claim_token is not None
            and value.get("claim_token") != expected_claim_token
        )
        or (
            expected_worker_id is not None
            and value.get("worker_id") != expected_worker_id
        )
    ):
        return None, "authority_checkpoint_attestation_binding_mismatch"
    observed_digest = authority_payload_sha256(workflow_data)
    identity = workflow_data.get(
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY
    )
    observed_definition_hash = (
        _clean_text(identity.get("definition_hash"))
        if isinstance(identity, Mapping)
        else None
    )
    if (
        observed_digest is None
        or value.get("authority_payload_sha256") != observed_digest
        or not _is_sha256(value.get("authority_payload_sha256"))
        or value.get("executed_definition_hash") != observed_definition_hash
        or not _is_sha256(observed_definition_hash)
    ):
        return None, "authority_checkpoint_attestation_payload_mismatch"
    if require_exact_eligible and not eligible:
        return None, "authority_checkpoint_attestation_ineligible"
    return dict(value), None


__all__ = [
    "DURABLE_AUTHORITY_CHECKPOINT_ATTESTATION_FIELD",
    "DURABLE_AUTHORITY_CHECKPOINT_ATTESTATION_SCHEMA_VERSION",
    "DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY",
    "DURABLE_WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION",
    "EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY",
    "PROMPT_CONTEXT_DIAGNOSTICS_KEY",
    "RESERVED_AUTHORITY_CONTEXT_KEYS",
    "WORKFLOW_AUTHORITY_OUTPUT_KEY",
    "authority_payload_sha256",
    "build_authority_checkpoint_attestation",
    "validate_authority_checkpoint_attestation",
    "worker_claim_supports_exact_authority_snapshot",
]
