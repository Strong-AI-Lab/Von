"""Versioned server-session assurance for interactive authentication."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

AUTHENTICATION_ASSURANCE_SESSION_KEY = "von_authentication_assurance"
GOOGLE_OAUTH_LOGIN_EMAIL_ASSURANCE = "hasVonLoginEmail.v1"
BROWSER_TEST_AUTH_PROVIDER = "browser_test_fixture"
GOOGLE_OAUTH_AUTH_PROVIDER = "google_oauth"


def session_requires_login_email_assurance(session_data: Mapping[str, Any]) -> bool:
    """Whether this session claims an interactive email-based identity."""

    provider = str(session_data.get("auth_provider") or "").strip()
    if provider == BROWSER_TEST_AUTH_PROVIDER:
        return False
    return provider == GOOGLE_OAUTH_AUTH_PROVIDER or bool(
        session_data.get("user_email")
    )


def session_has_required_authentication_assurance(
    session_data: Mapping[str, Any],
) -> bool:
    """Reject pre-cutover email sessions lacking the narrow-binding proof."""

    if not session_requires_login_email_assurance(session_data):
        return True
    return (
        session_data.get(AUTHENTICATION_ASSURANCE_SESSION_KEY)
        == GOOGLE_OAUTH_LOGIN_EMAIL_ASSURANCE
    )


__all__ = [
    "AUTHENTICATION_ASSURANCE_SESSION_KEY",
    "BROWSER_TEST_AUTH_PROVIDER",
    "GOOGLE_OAUTH_AUTH_PROVIDER",
    "GOOGLE_OAUTH_LOGIN_EMAIL_ASSURANCE",
    "session_has_required_authentication_assurance",
    "session_requires_login_email_assurance",
]
