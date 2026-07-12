"""Server-signed origin attestations for persisted certification campaigns."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from typing import Any, Mapping


OPERATIONAL_CERTIFICATION_ATTESTATION_SCHEMA_VERSION = (
    "operational_certification_runner_attestation.v1"
)
_SECRET_ENV = "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY"
_KEY_ID_ENV = "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID"
_FORBIDDEN_KEY_MARKERS = (
    "change-in-production",
    "dev-secret",
    "development-secret",
    "von-dev-secret-key",
)


def _signing_material() -> tuple[bytes, str]:
    value: str | bytes | None = os.environ.get(_SECRET_ENV)
    if isinstance(value, str):
        normalised = value.strip()
        if any(marker in normalised.lower() for marker in _FORBIDDEN_KEY_MARKERS):
            raise RuntimeError("operational_certification_attestation_key_insecure")
        value = normalised.encode("utf-8")
    if not isinstance(value, bytes) or len(value) < 32:
        raise RuntimeError("operational_certification_attestation_secret_unavailable")
    key_id = str(os.environ.get(_KEY_ID_ENV) or "").strip()
    if not key_id or len(key_id) > 128:
        raise RuntimeError("operational_certification_attestation_key_id_unavailable")
    return value, key_id


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def build_operational_certification_runner_attestation(
    *,
    experiment_run_id: str,
    campaign_execution_id: str,
    namespace: str,
    user_id: str,
    org_id: str,
    report_sha256: str,
    contract_sha256: str,
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    secret, key_id = _signing_material()
    timestamp = (
        (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    )
    unsigned = {
        "schema_version": OPERATIONAL_CERTIFICATION_ATTESTATION_SCHEMA_VERSION,
        "issuer": "operational_certification_runner_service",
        "key_id": key_id,
        "experiment_run_id": str(experiment_run_id).strip(),
        "campaign_execution_id": str(campaign_execution_id).strip(),
        "namespace": str(namespace).strip(),
        "user_id": str(user_id).strip(),
        "org_id": str(org_id).strip(),
        "report_sha256": str(report_sha256).strip(),
        "contract_sha256": str(contract_sha256).strip(),
        "issued_at": timestamp,
    }
    if any(
        not unsigned[field] for field in unsigned if field not in {"schema_version"}
    ):
        raise ValueError("operational_certification_attestation_field_required")
    signature = hmac.new(secret, _canonical(unsigned), hashlib.sha256).hexdigest()
    return {**unsigned, "signature": signature}


def verify_operational_certification_runner_attestation(
    value: Any,
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("operational_certification_runner_attestation_required")
    attestation = dict(value)
    signature = attestation.pop("signature", None)
    secret, expected_key_id = _signing_material()
    if (
        attestation.get("schema_version")
        != (OPERATIONAL_CERTIFICATION_ATTESTATION_SCHEMA_VERSION)
        or attestation.get("issuer") != "operational_certification_runner_service"
    ):
        raise ValueError("operational_certification_runner_attestation_invalid")
    if attestation.get("key_id") != expected_key_id:
        raise ValueError("operational_certification_runner_attestation_key_id_invalid")
    for field_name, expected_value in expected.items():
        if attestation.get(field_name) != expected_value:
            raise ValueError(
                f"operational_certification_runner_attestation_mismatch:{field_name}"
            )
    expected_signature = hmac.new(
        secret, _canonical(attestation), hashlib.sha256
    ).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, expected_signature
    ):
        raise ValueError(
            "operational_certification_runner_attestation_signature_invalid"
        )
    return {**attestation, "signature": signature, "signature_verified": True}


__all__ = [
    "OPERATIONAL_CERTIFICATION_ATTESTATION_SCHEMA_VERSION",
    "build_operational_certification_runner_attestation",
    "verify_operational_certification_runner_attestation",
]
