"""Google OAuth startup configuration helpers.

This module centralises production-safety checks for OAuth and keeps secret
loading logic in one place so auth paths and startup validation stay aligned.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse
from .runtime_env import (
    clean_env_value,
    load_secret_from_env_or_file,
    truthy,
)


def google_oauth_strict_startup_enabled() -> bool:
    """Return True when production-style OAuth startup validation is required."""
    return truthy(os.getenv("GOOGLE_OAUTH_STRICT_STARTUP"))


def _is_local_development_redirect(redirect_uri: str | None) -> bool:
    value = clean_env_value(redirect_uri)
    if not value:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "http" and host in {"localhost", "127.0.0.1"}


def configure_oauthlib_insecure_transport(redirect_uri: str | None) -> None:
    """Set OAUTHLIB insecure transport only for explicit/local development flows."""
    if truthy(os.getenv("GOOGLE_OAUTH_ALLOW_INSECURE_TRANSPORT")):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        return
    if _is_local_development_redirect(redirect_uri):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        return
    os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)


def collect_google_oauth_startup_errors() -> list[str]:
    """Collect strict-mode OAuth startup errors with actionable diagnostics."""
    if not google_oauth_strict_startup_enabled():
        return []

    errors: list[str] = []

    client_id = load_secret_from_env_or_file(
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID_FILE"
    )
    client_secret = load_secret_from_env_or_file(
        "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET_FILE"
    )
    redirect_uri = clean_env_value(os.getenv("GOOGLE_OAUTH_REDIRECT_URI"))
    dynamic_redirects_enabled = truthy(os.getenv("GOOGLE_OAUTH_ENABLE_DYNAMIC_REDIRECTS"))
    dynamic_redirects_override = truthy(
        os.getenv("GOOGLE_OAUTH_ALLOW_DYNAMIC_REDIRECTS_IN_PRODUCTION")
    )

    if not client_id:
        errors.append(
            "Missing GOOGLE_OAUTH_CLIENT_ID (or GOOGLE_OAUTH_CLIENT_ID_FILE with readable content)."
        )
    if not client_secret:
        errors.append(
            "Missing GOOGLE_OAUTH_CLIENT_SECRET (or GOOGLE_OAUTH_CLIENT_SECRET_FILE with readable content)."
        )
    if not redirect_uri:
        errors.append("Missing GOOGLE_OAUTH_REDIRECT_URI.")
        return errors

    parsed = urlparse(redirect_uri)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https"}:
        errors.append(
            "GOOGLE_OAUTH_REDIRECT_URI must use https in strict startup mode."
        )
    if host in {"localhost", "127.0.0.1"}:
        errors.append(
            "GOOGLE_OAUTH_REDIRECT_URI must not target localhost in strict startup mode."
        )
    if not parsed.path.startswith("/von/api/auth/google/callback"):
        errors.append(
            "GOOGLE_OAUTH_REDIRECT_URI must point to /von/api/auth/google/callback."
        )

    if dynamic_redirects_enabled and not dynamic_redirects_override:
        errors.append(
            "GOOGLE_OAUTH_ENABLE_DYNAMIC_REDIRECTS must be disabled in strict startup mode."
        )

    if truthy(os.getenv("OAUTHLIB_INSECURE_TRANSPORT")) and not _is_local_development_redirect(
        redirect_uri
    ):
        errors.append(
            "OAUTHLIB_INSECURE_TRANSPORT must not be enabled for non-local redirect URIs."
        )

    return errors


def validate_google_oauth_startup_or_raise() -> None:
    """Fail-fast for unsafe/invalid OAuth startup configuration."""
    errors = collect_google_oauth_startup_errors()
    if not errors:
        return
    detail = " ; ".join(errors)
    raise RuntimeError(f"Google OAuth startup validation failed: {detail}")
