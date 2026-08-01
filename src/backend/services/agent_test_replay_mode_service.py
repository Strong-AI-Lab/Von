"""Support-only replay mode helpers for AgentTest acceptance paths."""

from __future__ import annotations

import os
from typing import Any, Mapping


AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY = "agent_test_selector_replay_mode"
AGENT_TEST_SELECTOR_REPLAY_MODE_FAST_PATH = "fast_path"
AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM = "represented_selector_llm"
AGENT_TEST_REAL_POSTCONDITION_CRITIC_ENV = "VON_AGENT_TEST_REAL_POSTCONDITION_CRITIC"


def _clean_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value or "").strip()


def agent_test_real_postcondition_critic_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether AgentTest must run the represented critic workflow."""

    env = environ if environ is not None else os.environ
    return _clean_text(env.get(AGENT_TEST_REAL_POSTCONDITION_CRITIC_ENV)).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def represented_postcondition_critic_enabled_for_runtime(
    *,
    agent_test_instance: bool,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Project whether this runtime uses the represented critic path.

    Normal runtimes always use the represented subworkflow. AgentTest uses its
    deterministic acceleration unless the explicit acceptance-mode override is
    enabled.
    """

    return bool(
        not agent_test_instance
        or agent_test_real_postcondition_critic_enabled(environ=environ)
    )


def use_represented_selector_llm_for_agent_test_replay(
    data: Mapping[str, Any] | None,
) -> bool:
    """Return whether AgentTest should exercise the represented selector LLM path.

    This helper owns only replay wiring. It does not infer routing semantics
    from prompts or candidates; callers must pass an explicit replay-mode flag.
    """

    if not isinstance(data, Mapping):
        return False

    mode = _clean_text(data.get(AGENT_TEST_SELECTOR_REPLAY_MODE_CONTEXT_KEY)).lower()
    if mode in {
        AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM,
        "represented_llm",
        "selector_llm",
        "llm",
    }:
        return True
    if mode in {AGENT_TEST_SELECTOR_REPLAY_MODE_FAST_PATH, "deterministic"}:
        return False

    if data.get("agent_test_use_represented_selector_llm") is True:
        return True
    if data.get("agent_test_disable_selector_fast_path") is True:
        return True
    if data.get("agent_test_selector_fast_path_enabled") is False:
        return True
    return False
