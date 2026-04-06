"""Shared coding-agent MCP access profile diagnostics and policy helpers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

PROFILE_ID = "coding_agent_vontology_mcp_access"
PROFILE_VERSION = "v1"
PRIMARY_DATABASE_NAME = "von_db"
WRITE_APPROVAL_ENV_VAR = "VON_MCP_ALLOW_WRITES"
INTERNAL_MCP_ENABLE_ENV_VAR = "VON_INTERNAL_MCP_ENABLE"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _truthy_env(name: str, *, default: str = "0") -> bool:
    value = os.getenv(name, default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_environment_state() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "configured_database_name": None,
        "running_under_pytest": False,
        "looks_like_test_database_name": False,
        "looks_like_noncanonical_database": False,
        "authority_state": "unknown",
        "authority_kind": "unknown",
        "reason_codes": [],
    }

    try:
        from ..db.mongo_client import (
            _is_running_under_pytest,
            get_configured_database_name,
        )

        db_name = get_configured_database_name()
        running_under_pytest = bool(_is_running_under_pytest())
        looks_like_test_database_name = db_name.lower().startswith("test")
        looks_like_noncanonical_database = db_name != PRIMARY_DATABASE_NAME

        payload["configured_database_name"] = db_name
        payload["running_under_pytest"] = running_under_pytest
        payload["looks_like_test_database_name"] = looks_like_test_database_name
        payload["looks_like_noncanonical_database"] = looks_like_noncanonical_database

        reason_codes: list[str] = []
        if running_under_pytest:
            reason_codes.append("running_under_pytest")
        if looks_like_test_database_name:
            reason_codes.append("test_database_name")
        if looks_like_noncanonical_database:
            reason_codes.append("noncanonical_database_name")
        else:
            reason_codes.append("canonical_primary_database")

        if running_under_pytest and db_name == PRIMARY_DATABASE_NAME:
            authority_state = "unsafe_pytest_primary_db"
            authority_kind = "unsafe"
            reason_codes.append("pytest_primary_database_misconfiguration")
        elif running_under_pytest or looks_like_test_database_name:
            authority_state = "test_isolated"
            authority_kind = "test_like"
        elif looks_like_noncanonical_database:
            authority_state = "local_noncanonical"
            authority_kind = "dev_like"
        else:
            authority_state = "canonical_primary"
            authority_kind = "prod_like"

        payload["authority_state"] = authority_state
        payload["authority_kind"] = authority_kind
        payload["reason_codes"] = list(dict.fromkeys(reason_codes))
        return payload
    except Exception as exc:
        payload["reason_codes"] = ["environment_detection_failed"]
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload


def build_coding_agent_mcp_access_profile() -> dict[str, Any]:
    """Return the effective coding-agent MCP access profile for this process."""

    environment = _load_environment_state()
    authority_state = str(environment.get("authority_state") or "unknown").strip()
    authority_kind = str(environment.get("authority_kind") or "unknown").strip()
    explicit_write_approval = _truthy_env(WRITE_APPROVAL_ENV_VAR, default="0")
    internal_mcp_enabled = _truthy_env(INTERNAL_MCP_ENABLE_ENV_VAR, default="0")

    write_mode = "blocked_read_only"
    default_write_tools_allowed = False
    write_reason_codes: list[str] = []

    if authority_state == "unsafe_pytest_primary_db":
        write_mode = "blocked_unsafe_pytest_primary_db"
        write_reason_codes.extend(
            ["pytest_primary_database_misconfiguration", "canonical_write_blocked"]
        )
    elif authority_state == "test_isolated":
        write_mode = "enabled_test_isolated"
        default_write_tools_allowed = True
        write_reason_codes.extend(
            ["test_isolated_authority", "canonical_write_isolated"]
        )
    elif authority_state == "local_noncanonical":
        write_mode = "enabled_noncanonical_local"
        default_write_tools_allowed = True
        write_reason_codes.extend(
            ["noncanonical_local_authority", "canonical_write_not_targeting_primary_db"]
        )
    elif authority_state == "canonical_primary" and explicit_write_approval:
        write_mode = "enabled_canonical_primary_with_explicit_approval"
        default_write_tools_allowed = True
        write_reason_codes.extend(
            ["canonical_primary_database", "explicit_write_approval_present"]
        )
    elif authority_state == "canonical_primary":
        write_mode = "blocked_canonical_primary_requires_explicit_approval"
        write_reason_codes.extend(
            ["canonical_primary_database", "explicit_write_approval_missing"]
        )
    else:
        write_mode = "blocked_unknown_environment"
        write_reason_codes.extend(["unknown_environment"])

    default_dry_run = not default_write_tools_allowed

    return {
        "success": True,
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "generated_at_utc": _utc_now_iso(),
        "environment": {
            **environment,
            "primary_database_name": PRIMARY_DATABASE_NAME,
            "internal_mcp_enabled": internal_mcp_enabled,
            "explicit_write_approval_env_var": WRITE_APPROVAL_ENV_VAR,
            "explicit_write_approval_present": explicit_write_approval,
        },
        "shared_authority_read_policy": {
            "mode": "default_enabled",
            "default_allowed": True,
            "reason_codes": ["shared_authority_read_default"],
            "notes": [
                "Coding-agent MCP sessions may read shared workflow, prompt, template, and KB-authority surfaces by default.",
            ],
        },
        "shared_authority_write_policy": {
            "mode": write_mode,
            "default_allowed": default_write_tools_allowed,
            "write_category_tools_allowed": default_write_tools_allowed,
            "authority_kind": authority_kind,
            "reason_codes": list(dict.fromkeys(write_reason_codes)),
            "explicit_write_approval_env_var": WRITE_APPROVAL_ENV_VAR,
            "explicit_write_approval_present": explicit_write_approval,
            "notes": [
                "Write-category tool access is enabled by default only when the target authority is test-isolated, clearly non-primary, or the canonical primary DB has explicit approval.",
                "Destructive or high-impact writes remain subject to tool-level confirmations, dry-run controls, and theory/promotion boundaries.",
            ],
        },
        "user_scoped_data_policy": {
            "namespace_required": True,
            "protected_surfaces": [
                "rag_sessions",
                "private_history",
                "user_bound_session_state",
            ],
            "notes": [
                "Shared-authority access does not relax namespace or authentication requirements for user-scoped data.",
            ],
        },
        "testing_workflow_policy": {
            "theory_local_writes_preferred": True,
            "explicit_promotion_required": True,
            "notes": [
                "Testing workflows should continue to write theory-local state and promote only through explicit promotion helpers.",
            ],
        },
        "destructive_write_policy": {
            "tool_level_guardrails_required": True,
            "notes": [
                "Coding-agent mode does not bypass destructive-write safeguards or confirmation requirements.",
            ],
        },
        "von_chat_run_policy": {
            "default_allow_writes": default_write_tools_allowed,
            "default_dry_run": default_dry_run,
            "requires_internal_mcp_enabled": True,
            "notes": [
                "When write-category tools are allowed by profile, von_chat_run defaults to write-enabled unless dry_run is explicitly requested.",
                "When write-category tools are blocked by profile, von_chat_run remains read-only by default and may still allow preview-safe dry-run calls.",
            ],
        },
    }
