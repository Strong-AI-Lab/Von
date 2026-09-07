"""Operator-configured public KnowKat MCP source; no model-selected processes."""

from __future__ import annotations
import json
import os
from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient
from ...utils.runtime_env import read_repo_dotenv_values

COMMAND_ENV = "VON_KNOWKAT_COMMAND_JSON"
PUBLIC_ENV = "VON_KNOWKAT_PUBLIC"
TIMEOUT_ENV = "VON_KNOWKAT_TIMEOUT_SEC"


def configuration():
    values = read_repo_dotenv_values((COMMAND_ENV, PUBLIC_ENV, TIMEOUT_ENV))
    values.update(os.environ)
    return values


def public_source_enabled():
    return configuration().get(PUBLIC_ENV, "").strip().lower() == "true"


def get_knowkat_client():
    values = configuration()
    if values.get(PUBLIC_ENV, "").strip().lower() != "true":
        raise PermissionError(
            "KnowKat packages have not been designated public by the operator"
        )
    raw = values.get(COMMAND_ENV)
    if not raw:
        raise ValueError("KnowKat is not configured: set VON_KNOWKAT_COMMAND_JSON")
    command = json.loads(raw)
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(x, str) or not x.strip() or "\x00" in x for x in command)
    ):
        raise ValueError("KnowKat command must be a nonempty JSON string array")
    timeout = float(values.get(TIMEOUT_ENV, "60"))
    if not 5 <= timeout <= 300:
        raise ValueError("KnowKat timeout must be between 5 and 300 seconds")
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    return MCPStdIOClient(
        MCPServerConfig(
            command=command[0],
            args=command[1:],
            env=env,
            timeout_sec=timeout,
            transport_retry_attempts=0,
            log_tag="[knowkat_proxy]",
        )
    )
