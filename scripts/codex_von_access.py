"""Operator-installed access profiles; ordinary coding workers stay confined."""

import json
from pathlib import Path


def operator_access(config):
    profile = config.get("operator_access")
    if profile is None:
        return None
    if not isinstance(profile, dict) or profile.get("enabled") is not True:
        raise ValueError("operator_access must be an explicitly enabled installation")
    command = profile.get("mcp_command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
        or not Path(command[0]).is_absolute()
    ):
        raise ValueError("Operator MCP requires an absolute installed command")
    return profile


def launch_access_args(config, *, inbox=False):
    profile = operator_access(config)
    if profile:
        command = profile["mcp_command"]
        return [
            "--sandbox",
            "danger-full-access",
            "-c",
            "mcp_servers.von.enabled=true",
            "-c",
            "mcp_servers.von.command=" + json.dumps(command[0]),
            "-c",
            "mcp_servers.von.args=" + json.dumps(command[1:]),
            "-c",
            "mcp_servers.von.startup_timeout_sec=60",
            "-c",
            "mcp_servers.von.tool_timeout_sec=180",
        ]
    args = ["-c", "mcp_servers.von.enabled=false"]
    if inbox:
        return ["--sandbox", "read-only", *args]
    if config.get("permission_profile"):
        return [
            "-c",
            "default_permissions=" + json.dumps(config["permission_profile"]),
            *args,
        ]
    return ["--sandbox", "workspace-write", *args]


def assignment_actors(config):
    """Explicit operator delegation, never inferred from a reporting relation."""
    extra = config.get("assignment_actor_ids", [])
    if not isinstance(extra, list) or any(
        not isinstance(value, str) or not value.startswith("#V#") for value in extra
    ):
        raise ValueError("assignment_actor_ids must contain installed principal IDs")
    return tuple(dict.fromkeys([config["delegator_id"], *extra]))
