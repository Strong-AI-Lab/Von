"""Shared advisory-duration helpers for conversation-turn LLM stages.

The legacy timeout names remain as compatibility aliases. Callers must not turn
these values into provider or thread deadlines unless a separate, named hard
resource boundary has been established.
"""

from __future__ import annotations

from typing import Any

# Conversation-turn stages can include large selector, planning, and recovery
# prompts. Keep the shared default long enough for local and routed models to
# finish normal turn supervision without treating a slow prompt as a wedged call.
DEFAULT_CONVERSATION_TURN_LLM_ADVISORY_SEC = 120.0
DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC = DEFAULT_CONVERSATION_TURN_LLM_ADVISORY_SEC


def coerce_conversation_turn_llm_timeout_sec(raw_timeout: Any) -> float | None:
    """Compatibility alias returning a positive advisory duration."""
    if raw_timeout is None:
        return None
    try:
        timeout_sec = float(raw_timeout)
    except Exception:
        return None
    if timeout_sec <= 0.0:
        return None
    return max(1.0, min(timeout_sec, 600.0))


def default_conversation_turn_llm_timeout_sec(raw_timeout: Any) -> float:
    """Compatibility alias returning the default advisory duration."""
    if raw_timeout is None or not str(raw_timeout).strip():
        return DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC
    return (
        coerce_conversation_turn_llm_timeout_sec(raw_timeout)
        or DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC
    )


coerce_conversation_turn_llm_advisory_sec = coerce_conversation_turn_llm_timeout_sec
default_conversation_turn_llm_advisory_sec = default_conversation_turn_llm_timeout_sec
