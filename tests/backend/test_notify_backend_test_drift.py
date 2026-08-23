from __future__ import annotations

import pytest

from scripts.notify_backend_test_drift import build_message


def test_failure_notification_links_the_complete_evidence_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIGHTLY_SMTP_USERNAME", "sender@example.test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Strong-AI-Lab/Von")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")

    message = build_message("new_failures", "one test failed")

    body = message.get_content()
    assert "https://github.com/Strong-AI-Lab/Von/actions/runs/12345" in body
    assert "Actions artifact backend-test-drift-12345" in body


def test_notification_path_probe_does_not_claim_an_absent_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIGHTLY_SMTP_USERNAME", "sender@example.test")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")

    message = build_message("test", "notification probe")

    assert "Actions artifact" not in message.get_content()


def test_failure_notification_does_not_claim_a_failed_artifact_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIGHTLY_SMTP_USERNAME", "sender@example.test")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("NIGHTLY_DRIFT_ARTIFACT_AVAILABLE", "false")

    message = build_message("broken", "collection failed")

    assert "Actions artifact" not in message.get_content()
