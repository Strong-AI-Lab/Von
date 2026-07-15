from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from src.backend.services.operational_certification_attestation_service import (
    build_operational_certification_runner_attestation,
    verify_operational_certification_runner_attestation,
)


EXPECTED = {
    "experiment_run_id": "#V#experiment_run_1",
    "campaign_execution_id": "campaign-1",
    "namespace": "#V#user@org",
    "user_id": "#V#user",
    "org_id": "#V#org",
    "report_sha256": "a" * 64,
    "contract_sha256": "b" * 64,
}


def test_runner_attestation_is_server_signed_and_exactly_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY", "s" * 64)
    monkeypatch.setenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID", "test-key-1")
    attestation = build_operational_certification_runner_attestation(
        **EXPECTED,
        issued_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
    )

    verified = verify_operational_certification_runner_attestation(
        attestation,
        expected=EXPECTED,
    )
    assert verified["signature_verified"] is True

    forged = copy.deepcopy(attestation)
    forged["report_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="mismatch:report_sha256"):
        verify_operational_certification_runner_attestation(
            forged,
            expected=EXPECTED,
        )


def test_runner_attestation_fails_closed_without_strong_server_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY", raising=False)
    monkeypatch.delenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID", raising=False)

    with pytest.raises(
        RuntimeError,
        match="operational_certification_attestation_secret_unavailable",
    ):
        build_operational_certification_runner_attestation(
            **EXPECTED,
            issued_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
        )


def test_runner_attestation_rejects_public_development_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY",
        "von-dev-secret-key-change-in-production",
    )
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID",
        "unsafe-default",
    )

    with pytest.raises(
        RuntimeError,
        match="operational_certification_attestation_key_insecure",
    ):
        build_operational_certification_runner_attestation(
            **EXPECTED,
            issued_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
        )


def test_runner_attestation_fails_closed_after_uncoordinated_key_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY",
        "original-local-key-" + "s" * 64,
    )
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID",
        "macos-keychain-von-operational-certification-v1",
    )
    attestation = build_operational_certification_runner_attestation(
        **EXPECTED,
        issued_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
    )

    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY",
        "rotated-local-key-" + "r" * 64,
    )
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID",
        "macos-keychain-von-operational-certification-v2",
    )

    with pytest.raises(
        ValueError,
        match="operational_certification_runner_attestation_key_id_invalid",
    ):
        verify_operational_certification_runner_attestation(
            attestation,
            expected=EXPECTED,
        )
