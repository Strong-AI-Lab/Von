import os

import pytest

from src.backend.services.google_oauth_config import (
    collect_google_oauth_startup_errors,
    configure_oauthlib_insecure_transport,
    load_secret_from_env_or_file,
    validate_google_oauth_startup_or_raise,
)


def test_load_secret_prefers_direct_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("file-secret", encoding="utf-8")
    monkeypatch.setenv("TEST_SECRET", "env-secret")
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    assert (
        load_secret_from_env_or_file("TEST_SECRET", "TEST_SECRET_FILE") == "env-secret"
    )


def test_load_secret_falls_back_to_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("file-secret", encoding="utf-8")
    monkeypatch.delenv("TEST_SECRET", raising=False)
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    assert (
        load_secret_from_env_or_file("TEST_SECRET", "TEST_SECRET_FILE")
        == "file-secret"
    )


def test_configure_oauthlib_insecure_transport_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_ALLOW_INSECURE_TRANSPORT", raising=False)
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)

    configure_oauthlib_insecure_transport(
        "http://localhost:5000/von/api/auth/google/callback"
    )

    assert os.getenv("OAUTHLIB_INSECURE_TRANSPORT") == "1"


def test_configure_oauthlib_insecure_transport_hosted_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    monkeypatch.delenv("GOOGLE_OAUTH_ALLOW_INSECURE_TRANSPORT", raising=False)

    configure_oauthlib_insecure_transport(
        "https://von.example.org/von/api/auth/google/callback"
    )

    assert os.getenv("OAUTHLIB_INSECURE_TRANSPORT") is None


def test_collect_oauth_errors_empty_when_strict_mode_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_STRICT_STARTUP", "0")
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)

    assert collect_google_oauth_startup_errors() == []


def test_collect_oauth_errors_reports_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_STRICT_STARTUP", "1")
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)

    errors = collect_google_oauth_startup_errors()
    assert any("GOOGLE_OAUTH_CLIENT_ID" in item for item in errors)
    assert any("GOOGLE_OAUTH_CLIENT_SECRET" in item for item in errors)
    assert any("GOOGLE_OAUTH_REDIRECT_URI" in item for item in errors)


def test_collect_oauth_errors_accepts_valid_file_based_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    client_id_file = tmp_path / "client_id"
    client_secret_file = tmp_path / "client_secret"
    client_id_file.write_text("client-id", encoding="utf-8")
    client_secret_file.write_text("client-secret", encoding="utf-8")

    monkeypatch.setenv("GOOGLE_OAUTH_STRICT_STARTUP", "1")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI", "https://von.example.org/von/api/auth/google/callback"
    )
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID_FILE", str(client_id_file))
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET_FILE", str(client_secret_file))
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLE_DYNAMIC_REDIRECTS", "0")
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)

    assert collect_google_oauth_startup_errors() == []


def test_validate_google_oauth_startup_raises_on_invalid_dynamic_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_STRICT_STARTUP", "1")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI", "https://von.example.org/von/api/auth/google/callback"
    )
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLE_DYNAMIC_REDIRECTS", "1")
    monkeypatch.delenv("GOOGLE_OAUTH_ALLOW_DYNAMIC_REDIRECTS_IN_PRODUCTION", raising=False)

    with pytest.raises(RuntimeError, match="Google OAuth startup validation failed"):
        validate_google_oauth_startup_or_raise()
