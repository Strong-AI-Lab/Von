"""Jira MCP proxy using the shared stdio client helper.

Bridges the Jira MCP server defined in src/backend/mcp_server/mcp_server.py so
Von's internal MCP gateway can call Jira tools without duplicating connection
logic.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, cast

from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError
from ...utils.runtime_env import apply_repo_dotenv_overrides, clean_env_value

logger = logging.getLogger(__name__)
_LOG_TAG = "[jira_proxy]"
JIRA_PROXY_ENV_OVERRIDE_KEYS: tuple[str, ...] = (
    "ATLASSIAN_BASE_URL",
    "ATLASSIAN_SITE_BASE",
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_API_EMAIL",
    "ATLASSIAN_API_TOKEN",
)


class JiraProxyError(Exception):
    """Raised when Jira proxy operations fail."""


class JiraInvocationAuthorityError(JiraProxyError):
    """Non-enumerating denial before the configured account is queried."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _jira_resource_id(auth: Mapping[str, Any]) -> str:
    identity = f"{auth.get('base_url', '')}\n{str(auth.get('email') or '').casefold()}"
    return "jira_account_" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def _jira_account_owner(auth: Mapping[str, Any]) -> str | None:
    from ...services.von_user_authentication_service import (
        find_user_concept_by_login_email,
    )

    if not all(auth.get(key) for key in ("base_url", "email", "token_present")):
        raise JiraInvocationAuthorityError(
            "jira_configuration_unavailable",
            "Jira account configuration is unavailable.",
        )
    try:
        # This governed authentication binding is deliberately stronger than
        # contact email, imported Jira person data or organisation membership.
        owner = find_user_concept_by_login_email(auth["email"])
    except Exception as exc:
        raise JiraInvocationAuthorityError(
            "jira_account_binding_unavailable",
            "The Jira account's governed owner binding could not be verified.",
        ) from exc
    return owner.get("concept_id") if owner else None


def jira_resource_binding_for_user(user_concept_id: str | None) -> str | None:
    """Project this deployment's Jira account only for its authenticated owner."""

    if not user_concept_id:
        return None
    auth = inspect_jira_auth_config()
    try:
        owner = _jira_account_owner(auth)
    except JiraInvocationAuthorityError:
        return None
    return _jira_resource_id(auth) if owner == user_concept_id else None


def resolve_jira_read_authority(
    auth: Mapping[str, Any], *, resource_id: str | None = None
) -> dict[str, Any]:
    """Recheck the actual proxy account for direct and workflow retrieval."""

    from .gateway import (
        get_internal_mcp_actor_context_source,
        get_internal_mcp_preexisting_actor_context,
        internal_mcp_actor_context_is_trusted_local_operator,
        internal_mcp_actor_context_is_untrusted_payload_fallback,
    )
    from ...security.access_control import get_effective_user_concept_id

    actual_resource_id = _jira_resource_id(auth)
    if resource_id is not None and resource_id != actual_resource_id:
        raise JiraInvocationAuthorityError(
            "jira_resource_not_authorised", "The Jira account binding has changed."
        )
    operator = internal_mcp_actor_context_is_trusted_local_operator()
    actor_id = None
    if not operator:
        preexisting = get_internal_mcp_preexisting_actor_context()
        if not internal_mcp_actor_context_is_untrusted_payload_fallback():
            if preexisting is not None:
                actor_id = preexisting[0]
            elif get_internal_mcp_actor_context_source() is None:
                # A durable worker may call the proxy directly inside its
                # server-bound actor context. An unscoped call is not an operator.
                actor_id = get_effective_user_concept_id()
        if not actor_id:
            raise JiraInvocationAuthorityError(
                "authenticated_actor_context_required",
                "Jira reads require an authenticated actor or the trusted local operator route.",
            )
        if _jira_account_owner(auth) != actor_id:
            raise JiraInvocationAuthorityError(
                "jira_resource_not_authorised",
                "The configured Jira account is not authorised for this actor.",
            )
    return {
        "principal_kind": (
            "trusted_local_operator" if operator else "authenticated_actor"
        ),
        "actor_concept_id": actor_id,
        "resource_id": actual_resource_id,
        "grant_source": (
            "trusted_local_operator" if operator else "governed_login_email_owner"
        ),
        "access": "read_only",
    }


def _resolve_jira_auth(
    env: Mapping[str, str],
) -> tuple[str | None, str | None, str | None, dict[str, str | None]]:
    base_url_key = (
        "ATLASSIAN_BASE_URL"
        if clean_env_value(env.get("ATLASSIAN_BASE_URL"))
        else (
            "ATLASSIAN_SITE_BASE"
            if clean_env_value(env.get("ATLASSIAN_SITE_BASE"))
            else None
        )
    )
    email_key = (
        "ATLASSIAN_EMAIL"
        if clean_env_value(env.get("ATLASSIAN_EMAIL"))
        else (
            "ATLASSIAN_API_EMAIL"
            if clean_env_value(env.get("ATLASSIAN_API_EMAIL"))
            else None
        )
    )
    token_key = (
        "ATLASSIAN_API_TOKEN"
        if clean_env_value(env.get("ATLASSIAN_API_TOKEN"))
        else None
    )

    base_url = clean_env_value(env.get(base_url_key)) if base_url_key else None
    email = clean_env_value(env.get(email_key)) if email_key else None
    token = clean_env_value(env.get(token_key)) if token_key else None

    if base_url:
        base_url = base_url.rstrip("/")

    return (
        base_url,
        email,
        token,
        {
            "base_url": base_url_key,
            "email": email_key,
            "token": token_key,
        },
    )


@dataclass
class JiraProxyConfig:
    """Configuration for Jira MCP subprocess."""

    command: str
    args: list[str]
    env: Dict[str, str]
    timeout_sec: float = 30.0


class JiraMCPProxy:
    """Manages external Jira MCP server subprocess using MCP SDK client."""

    def __init__(self, config: JiraProxyConfig):
        self._auth_identity = _resolve_jira_auth(config.env)[:3]
        client_config = MCPServerConfig(
            command=config.command,
            args=config.args,
            env=config.env,
            timeout_sec=config.timeout_sec,
            log_tag=_LOG_TAG,
        )
        self._client = MCPStdIOClient(client_config)
        # Binary downloads use the existing three 60-second HTTP attempts.
        # Allow twice that declared inner bound for transfer and JSON encoding;
        # ordinary issue reads retain their current operation budget.
        self._binary_config = MCPServerConfig(
            command=config.command,
            args=config.args,
            env=config.env,
            timeout_sec=360.0,
            log_tag=_LOG_TAG,
        )
        self._binary_client = MCPStdIOClient(self._binary_config)

    def source_capture_session(self):
        """Keep one helper for a source capture; authority is checked per read."""
        return self._client.reuse_session()

    async def _call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        resource_id: str | None = None,
        client_override: MCPStdIOClient | None = None,
    ) -> Any:
        authority = None
        if tool_name == "jira_search" or tool_name.startswith("jira_get_"):
            base_url, email, token = self._auth_identity
            authority = resolve_jira_read_authority(
                {"base_url": base_url, "email": email, "token_present": bool(token)},
                resource_id=resource_id,
            )
        try:
            client = (
                self._binary_client
                if tool_name in {
                    "jira_get_migration_export",
                    "jira_get_attachment_content",
                }
                else self._client
            )
            result = await (client_override or client).call_tool(tool_name, arguments)
            if authority is not None and isinstance(result, dict):
                result = {**result, "authority": authority}
            return result
        except MCPToolClientError as exc:
            raise JiraProxyError(str(exc)) from exc

    async def search(
        self,
        *,
        jql: str,
        max_results: Optional[int] = None,
        start_at: Optional[int] = None,
        next_page_token: Optional[str] = None,
        fields: Optional[list[str]] = None,
        resource_id: str | None = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"jql": jql}
        if isinstance(max_results, int):
            arguments["max_results"] = max_results
        if isinstance(start_at, int):
            arguments["start_at"] = start_at
        if isinstance(next_page_token, str) and next_page_token.strip():
            arguments["next_page_token"] = next_page_token.strip()
        if fields:
            arguments["fields"] = fields
        return await self._call("jira_search", arguments, resource_id=resource_id)

    async def get_issue(
        self,
        *,
        issue_key: str,
        fields: Optional[list[str]] = None,
        expand: Optional[list[str]] = None,
        resource_id: str | None = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"issue_key": issue_key}
        if fields:
            arguments["fields"] = fields
        if expand:
            arguments["expand"] = expand
        return await self._call("jira_get_issue", arguments, resource_id=resource_id)

    async def get_comments(
        self,
        *,
        issue_key: str,
        start_at: Optional[int] = None,
        max_results: Optional[int] = None,
        order_by: Optional[str] = None,
        body_format: Optional[str] = None,
        resource_id: str | None = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"issue_key": issue_key}
        if isinstance(start_at, int):
            arguments["start_at"] = start_at
        if isinstance(max_results, int):
            arguments["max_results"] = max_results
        if isinstance(order_by, str) and order_by.strip():
            arguments["order_by"] = order_by.strip()
        if isinstance(body_format, str) and body_format.strip():
            arguments["body_format"] = body_format.strip()
        return await self._call("jira_get_comments", arguments, resource_id=resource_id)

    async def get_watchers(self, *, issue_key: str) -> Dict[str, Any]:
        return await self._call("jira_get_watchers", {"issue_key": issue_key})

    async def get_migration_resource(
        self,
        *,
        resource: str,
        identifier: str = "",
        secondary_id: str = "",
        start_at: int = 0,
        max_results: int = 100,
        next_page_token: str | None = None,
    ) -> Any:
        """Read a named source resource through the existing account boundary."""
        return await self._call(
            "jira_get_migration_resource",
            {
                "resource": resource,
                "identifier": identifier,
                "secondary_id": secondary_id,
                "start_at": start_at,
                "max_results": max_results,
                "next_page_token": next_page_token,
            },
        )

    async def get_migration_export(
        self, *, kind: str, export_id: str, cloud_id: str | None = None
    ):
        return await self._call(
            "jira_get_migration_export",
            {
                "kind": kind,
                "export_id": export_id,
                "cloud_id": cloud_id,
            },
        )

    async def get_attachment_content(
        self,
        *,
        attachment_id: str,
        max_size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"attachment_id": attachment_id}
        if isinstance(max_size_bytes, int):
            arguments["max_size_bytes"] = max_size_bytes
        limit = max_size_bytes if isinstance(max_size_bytes, int) else 10 * 1024 * 1024
        # The helper and caller share a private, caller-created directory. Jira
        # content cannot select a filesystem destination. Keep large base64 out
        # of MCP framing; hydrate the established return shape only after read-back.
        with tempfile.TemporaryDirectory(prefix="von-jira-attachment-") as directory:
            client = MCPStdIOClient(
                replace(
                    self._binary_config,
                    env={
                        **(self._binary_config.env or {}),
                        "VON_JIRA_ATTACHMENT_STAGING_DIR": directory,
                    },
                )
            )
            result = await self._call(
                "jira_get_attachment_content", arguments, client_override=client
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                return result
            staged = result.get("staged_file") or {}
            name = staged.get("name", "")
            if (
                not isinstance(name, str)
                or len(name) != 36
                or not name.endswith(".bin")
                or any(char not in "0123456789abcdef" for char in name[:-4])
            ):
                raise JiraProxyError("Invalid attachment staging receipt")
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
                raise JiraProxyError("Invalid attachment staging file")
            with path.open("rb") as stream:
                data = stream.read(limit + 1)
            if (
                len(data) > limit
                or len(data) != staged.get("size_bytes")
                or len(data) != result.get("size_bytes")
                or sha256(data).hexdigest() != staged.get("sha256")
            ):
                raise JiraProxyError("Attachment staging integrity mismatch")
            result = dict(result)
            result.pop("staged_file")
            result["content_base64"] = base64.b64encode(data).decode("ascii")
            return result

    async def get_transitions(self, *, issue_key: str) -> Dict[str, Any]:
        return await self._call("jira_get_transitions", {"issue_key": issue_key})

    async def add_comment(self, *, issue_key: str, comment: str) -> Dict[str, Any]:
        return await self._call(
            "jira_add_comment", {"issue_key": issue_key, "comment": comment}
        )

    async def add_attachment(
        self,
        *,
        issue_key: str,
        filename: str,
        content_base64: str,
        mime_type: str,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {
            "issue_key": issue_key,
            "filename": filename,
            "content_base64": content_base64,
            "mime_type": mime_type,
        }
        if isinstance(comment, str) and comment.strip():
            arguments["comment"] = comment.strip()
        return await self._call("jira_add_attachment", arguments)

    async def transition_issue(
        self, *, issue_key: str, transition_id: str
    ) -> Dict[str, Any]:
        return await self._call(
            "jira_transition", {"issue_key": issue_key, "transition_id": transition_id}
        )

    async def create_issue(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call("jira_create_issue", {"payload": payload})

    async def update_issue(
        self, *, issue_key: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._call(
            "jira_update_issue", {"issue_key": issue_key, "payload": payload}
        )

    async def move_issue(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call("jira_move_issue", {"payload": payload})

    async def get_bulk_operation_progress(self, *, task_id: str) -> Dict[str, Any]:
        return await self._call(
            "jira_get_bulk_operation_progress", {"task_id": task_id}
        )

    async def link_issue(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call("jira_link_issue", {"payload": payload})

    async def delete_issue_link(self, *, issue_link_id: str) -> Dict[str, Any]:
        return await self._call(
            "jira_delete_issue_link", {"issue_link_id": issue_link_id}
        )

    async def get_myself(self) -> Dict[str, Any]:
        return await self._call("jira_get_myself", {})

    async def get_project_issue_types(self, *, project_key: str) -> Dict[str, Any]:
        return await self._call(
            "jira_get_project_issue_types", {"project_key": project_key}
        )

    def get_stats(self) -> Dict[str, int]:
        return {
            "call_count": self._client.call_count,
            "error_count": self._client.error_count,
        }


def _build_jira_env() -> Dict[str, str]:
    env = os.environ.copy()
    applied_overrides = apply_repo_dotenv_overrides(
        JIRA_PROXY_ENV_OVERRIDE_KEYS,
        environ=env,
    )
    if applied_overrides:
        logger.info(
            "%s Applied repo-root .env overrides for %s Jira key(s).",
            _LOG_TAG,
            len(applied_overrides),
        )

    base_url, email, token, env_keys_used = _resolve_jira_auth(env)

    missing = [
        name
        for name, value in {
            "ATLASSIAN_BASE_URL": base_url,
            "ATLASSIAN_EMAIL": email,
            "ATLASSIAN_API_TOKEN": token,
        }.items()
        if not value
    ]

    if missing:
        raise JiraProxyError(
            "Missing Atlassian settings: " + ", ".join(missing) + ". "
            "Set them in the environment, for example: "
            "$env:ATLASSIAN_BASE_URL='https://example.atlassian.net'; "
            "$env:ATLASSIAN_EMAIL='user@example.com'; "
            "$env:ATLASSIAN_API_TOKEN='token'"
        )

    logger.info(
        "%s Jira auth resolved from keys base_url=%s email=%s token_present=%s token_length=%d",
        _LOG_TAG,
        env_keys_used.get("base_url"),
        env_keys_used.get("email"),
        bool(token),
        len(token or ""),
    )

    env["ATLASSIAN_BASE_URL"] = cast(str, base_url)  # Normalise base key used by server
    env["ATLASSIAN_EMAIL"] = cast(str, email)
    env["ATLASSIAN_API_TOKEN"] = cast(str, token)
    return env


def inspect_jira_auth_config() -> Dict[str, Any]:
    """Return the Jira auth configuration currently visible to the process.

    This is a diagnostics helper for debugging authentication issues. It does
    not contact Jira and it never returns the API token.
    """

    env = os.environ.copy()
    apply_repo_dotenv_overrides(
        JIRA_PROXY_ENV_OVERRIDE_KEYS,
        environ=env,
    )
    base_url, email, token, env_keys_used = _resolve_jira_auth(env)

    return {
        "success": True,
        "base_url": base_url,
        "email": email,
        "token_present": bool(token),
        "token_length": len(token) if token else 0,
        "env_keys_used": env_keys_used,
        "notes": (
            "This output reflects the effective Jira environment seen by the "
            "Von process after repo-root .env overrides are applied. "
            "It does not prove Jira authentication is valid."
        ),
    }


def _build_jira_config() -> JiraProxyConfig:
    env = _build_jira_env()
    script_path = Path(__file__).resolve().parents[2] / "mcp_server" / "mcp_server.py"
    if not script_path.exists():
        raise JiraProxyError(f"Jira MCP server script not found at {script_path}")
    return JiraProxyConfig(
        command=sys.executable,
        args=[str(script_path)],
        env=env,
    )


# Singleton instance
_proxy_instance: Optional[JiraMCPProxy] = None
# Catalogue calls can run on fresh event loops, so construction is loop-neutral.
_proxy_lock = threading.Lock()


async def get_jira_proxy() -> JiraMCPProxy:
    """Get or create the global Jira MCP proxy instance."""
    global _proxy_instance

    with _proxy_lock:
        config = _build_jira_config()
        # Never authorise a freshly configured account and then send the read
        # through a process still authenticated as the previous account.
        if (
            _proxy_instance is None
            or _proxy_instance._auth_identity != _resolve_jira_auth(config.env)[:3]
        ):
            _proxy_instance = JiraMCPProxy(config)
            logger.info("%s Initialised Jira MCP proxy via %s", _LOG_TAG, config.args)
        return _proxy_instance
