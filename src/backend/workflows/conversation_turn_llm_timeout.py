"""Shared timeout helpers for conversation-turn LLM stages."""

from __future__ import annotations

from typing import Any

DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC = 30.0


def coerce_conversation_turn_llm_timeout_sec(raw_timeout: Any) -> float | None:
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
    if raw_timeout is None or not str(raw_timeout).strip():
        return DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC
    return (
        coerce_conversation_turn_llm_timeout_sec(raw_timeout)
        or DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC
    )
