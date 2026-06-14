"""Gmail profile enforcement in apply_runtime_defaults_to_mcp_payload.

Which Gmail account a tool call uses is an identity/credential decision the
caller's selection owns — not the model. A turn was observed failing because
qwen3:8b chose an explicit stale profile ("zhan-gmail", expired/revoked token)
even though a valid profile was selected; the old defaulting only filled blank
or "default"/"primary" placeholders, so the model's explicit (wrong) profile
won and the turn hard-failed with invalid_grant. The authoritative profile must
override any supplied one.
"""

from __future__ import annotations

from src.backend.workflows.mcp_tool_bridge import (
    apply_runtime_defaults_to_mcp_payload,
)

_AUTH = "vonwitbrock-gmail"


def _apply(payload: dict, *, default: str | None):
    return apply_runtime_defaults_to_mcp_payload(
        payload,
        tool_name="gmail_list_messages",
        input_schema=None,  # None => schema accepts any field (profile included)
        user_namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        default_gmail_profile=default,
    )


def _binding_sources(bindings) -> set[str]:
    return {b.get("source") for b in bindings if b.get("field") == "profile"}


def test_model_supplied_wrong_profile_is_overridden() -> None:
    payload = {"profile": "zhan-gmail", "max_results": 8}
    bindings = _apply(payload, default=_AUTH)
    assert payload["profile"] == _AUTH  # the bug: previously stayed zhan-gmail
    assert "default_gmail_profile_enforced_override" in _binding_sources(bindings)
    override = next(b for b in bindings if b.get("field") == "profile")
    assert override["overridden_profile"] == "zhan-gmail"


def test_blank_profile_is_filled() -> None:
    payload = {"max_results": 5}
    _apply(payload, default=_AUTH)
    assert payload["profile"] == _AUTH


def test_placeholder_profile_is_replaced() -> None:
    payload = {"profile": "default"}
    bindings = _apply(payload, default=_AUTH)
    assert payload["profile"] == _AUTH
    assert (
        "default_gmail_profile_placeholder_replacement"
        in _binding_sources(bindings)
    )


def test_profile_matching_default_is_untouched() -> None:
    payload = {"profile": _AUTH}
    bindings = _apply(payload, default=_AUTH)
    assert payload["profile"] == _AUTH
    assert _binding_sources(bindings) == set()  # no profile binding emitted


def test_no_authoritative_default_leaves_supplied_profile() -> None:
    # Deterministic/scheduled contexts pass default_gmail_profile=None; there is
    # nothing to enforce, so an explicit profile must be preserved.
    payload = {"profile": "zhan-gmail"}
    _apply(payload, default=None)
    assert payload["profile"] == "zhan-gmail"


def test_non_gmail_tool_profile_is_untouched() -> None:
    payload = {"profile": "zhan-gmail"}
    apply_runtime_defaults_to_mcp_payload(
        payload,
        tool_name="search_web",
        input_schema=None,
        user_namespace=None,
        default_gmail_profile=_AUTH,
    )
    assert payload["profile"] == "zhan-gmail"  # enforcement is gmail-only
