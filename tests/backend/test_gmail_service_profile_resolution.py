"""Regression tests for Gmail profile resolution.

JVNAUTOSCI-2122: when an LLM (or any caller) supplies the authorised Gmail
address as ``profile`` rather than the configured alias, ``get_profile``
should resolve it via the agent Gmail token store. The previous
behaviour raised ``KeyError`` and caused a user-facing turn failure.
"""

from __future__ import annotations

import pytest

from src.backend.integrations.google import gmail_service
from src.backend.integrations.google.gmail_service import (
    GmailProfile,
    get_profile,
    list_profile_summaries,
)
from src.backend.services.agent_gmail_token_store import AgentGmailTokenStatus


def _build_candidates() -> dict[str, GmailProfile]:
    return {
        "zhan-gmail": GmailProfile(
            profile_id="zhan-gmail",
            token_path="/tmp/zhan_token.json",
        ),
        "vonwitbrock-gmail": GmailProfile(
            profile_id="vonwitbrock-gmail",
            token_path="/tmp/vonwitbrock_token.json",
        ),
    }


@pytest.fixture
def fake_token_status(monkeypatch):
    """Stub the token-store status lookup with deterministic mappings."""

    mapping = {
        "zhan-gmail": "zhanvonwitbrock@gmail.com",
        "vonwitbrock-gmail": None,  # no DB tokens
    }

    def _status(profile_id: str) -> AgentGmailTokenStatus:
        return AgentGmailTokenStatus(
            profile_id=profile_id,
            has_tokens=mapping.get(profile_id) is not None,
            authorised_email=mapping.get(profile_id),
            expires_at=None,
            scopes=[],
        )

    monkeypatch.setattr(gmail_service, "_get_agent_gmail_token_status", _status)
    return mapping


def test_get_profile_returns_alias_match_directly(fake_token_status):
    candidates = _build_candidates()
    profile = get_profile("zhan-gmail", profiles=candidates)
    assert profile.profile_id == "zhan-gmail"


def test_get_profile_resolves_authorised_email_to_alias(fake_token_status):
    candidates = _build_candidates()
    profile = get_profile("zhanvonwitbrock@gmail.com", profiles=candidates)
    assert profile.profile_id == "zhan-gmail"


def test_get_profile_email_resolution_is_case_insensitive(fake_token_status):
    candidates = _build_candidates()
    profile = get_profile("ZhanVonWitbrock@GMAIL.com", profiles=candidates)
    assert profile.profile_id == "zhan-gmail"


def test_get_profile_unknown_email_lists_aliases_and_known_emails(fake_token_status):
    candidates = _build_candidates()
    with pytest.raises(KeyError) as excinfo:
        get_profile("unknown@example.com", profiles=candidates)
    message = str(excinfo.value)
    assert "zhan-gmail" in message
    assert "vonwitbrock-gmail" in message
    assert "zhanvonwitbrock@gmail.com" in message


def test_get_profile_unknown_alias_falls_through_to_email_resolution(fake_token_status):
    """A non-email, non-alias string should fail with a clear error rather
    than silently matching by accident."""
    candidates = _build_candidates()
    with pytest.raises(KeyError):
        get_profile("not-a-real-alias", profiles=candidates)


def test_list_profile_summaries_exposes_authorised_emails_without_secrets(
    fake_token_status,
):
    candidates = _build_candidates()
    summaries = list_profile_summaries(profiles=candidates)
    assert summaries == [
        {"profile_id": "vonwitbrock-gmail", "authorised_email": None},
        {
            "profile_id": "zhan-gmail",
            "authorised_email": "zhanvonwitbrock@gmail.com",
        },
    ]
    # Ensure no token-path or other secret leaks through this surface.
    for entry in summaries:
        assert set(entry.keys()) == {"profile_id", "authorised_email"}
