"""Shared defaults for live Von test scripts.

The JVNAUTOSCI-2070 launcher path keeps automated coding-agent tests on an
isolated backend so they do not restart or reuse the user's interactive server.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


DEFAULT_AGENT_TEST_BASE_URL = "http://127.0.0.1:5010"
AGENT_TEST_BASE_URL_ENV_VAR = "VON_AGENT_TEST_BASE_URL"
AGENT_TEST_LAUNCHER_COMMAND = r".\run.ps1 restart -AgentTest -HealthTimeoutSec 180"
AGENT_TEST_HEALTH_ENV_MARKER = "VON_AGENT_TEST_INSTANCE"
SERVER_AGENT_TEST_ENVIRONMENT_KEY = "server_agent_test_instance"


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _normalise_base_url(value: str) -> str:
    return value.rstrip("/")


def get_default_agent_test_base_url(
    environ: Mapping[str, str] | None = None,
) -> str:
    env = environ if environ is not None else os.environ
    configured = _normalise_base_url(_safe_text(env.get(AGENT_TEST_BASE_URL_ENV_VAR)))
    return configured or DEFAULT_AGENT_TEST_BASE_URL


def resolve_live_test_base_url(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    configured = _normalise_base_url(_safe_text(value))
    return configured or get_default_agent_test_base_url(environ=environ)


def build_agent_test_server_requirement_error(
    run_environment: Mapping[str, Any],
    *,
    base_url: str,
) -> str | None:
    marker = run_environment.get(SERVER_AGENT_TEST_ENVIRONMENT_KEY)
    if marker is True:
        return None

    metadata_source = _safe_text(run_environment.get("server_metadata_source")) or "none"
    metadata_error = _safe_text(run_environment.get("server_metadata_error")) or "none"
    marker_text = "missing" if marker is None else repr(marker)
    return (
        "Live coding-agent testing must use the isolated JVNAUTOSCI-2070 "
        f"server setup. Targeted base_url={base_url!r}, but /health did not "
        f"report {SERVER_AGENT_TEST_ENVIRONMENT_KEY}=True "
        f"(marker={marker_text}, metadata_source={metadata_source}, "
        f"metadata_error={metadata_error}). Start the test backend with "
        f"`{AGENT_TEST_LAUNCHER_COMMAND}` and retry, or pass "
        "`--allow-non-agent-test-server` only when intentionally testing the "
        "interactive/user-facing server."
    )
