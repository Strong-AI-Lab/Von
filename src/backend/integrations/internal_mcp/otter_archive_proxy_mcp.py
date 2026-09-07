"""Owner-scoped proxy for the local OtterArchive export MCP server.

Von authenticates and authorises the actor. The external stdio process is
deliberately simpler: each process is bound to one read-only SQLite index via
``OTTER_ARCHIVE_DB`` and receives neither Google credentials nor a caller-
supplied database path.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ...utils.runtime_env import clean_env_value, read_repo_dotenv_values
from .mcp_proxy_base import MCPServerConfig, MCPStdIOClient, MCPToolClientError

logger = logging.getLogger(__name__)
_LOG_TAG = "[otter_archive_proxy]"

OTTER_ARCHIVE_RESOURCE_ID_ENV = "VON_OTTER_ARCHIVE_RESOURCE_ID"
OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID_ENV = "VON_OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID"
OTTER_ARCHIVE_PROJECT_DIR_ENV = "VON_OTTER_ARCHIVE_MCP_PROJECT_DIR"
OTTER_ARCHIVE_COMMAND_ENV = "VON_OTTER_ARCHIVE_MCP_COMMAND"
OTTER_ARCHIVE_TIMEOUT_ENV = "VON_OTTER_ARCHIVE_MCP_TIMEOUT_SEC"
OTTER_ARCHIVE_DATABASE_ENV = "OTTER_ARCHIVE_DB"

DEFAULT_RESOURCE_ID = "personal_otter_archive"
_DOTENV_KEYS = (
    OTTER_ARCHIVE_RESOURCE_ID_ENV,
    OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID_ENV,
    OTTER_ARCHIVE_PROJECT_DIR_ENV,
    OTTER_ARCHIVE_COMMAND_ENV,
    OTTER_ARCHIVE_TIMEOUT_ENV,
    OTTER_ARCHIVE_DATABASE_ENV,
)
_SUBPROCESS_PASSTHROUGH_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


class OtterArchiveProxyError(Exception):
    """Raised when OtterArchive MCP configuration or transport fails."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "otter_archive_proxy_error",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})


@dataclass(frozen=True)
class OtterArchiveInvocationAuthorityError(PermissionError):
    """Fail-closed denial produced before the private MCP is launched."""

    reason_code: str
    safe_message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.safe_message


OtterArchivePrincipalKind = Literal["authenticated_actor", "trusted_local_operator"]


@dataclass(frozen=True)
class OtterArchiveInvocationAuthority:
    principal_kind: OtterArchivePrincipalKind
    resource_id: str
    actor_user_concept_id: str | None = None

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": "otter_archive_resource_authority.v1",
            "authorised": True,
            "principal_kind": self.principal_kind,
            "actor_user_concept_id": self.actor_user_concept_id,
            "resource_id": self.resource_id,
            "grant_source": (
                "deployment_owner_binding"
                if self.principal_kind == "authenticated_actor"
                else "trusted_local_operator"
            ),
            "access": "read_only",
        }


@dataclass(frozen=True)
class OtterArchiveProxyConfig:
    resource_id: str
    owner_user_concept_id: str
    project_dir: Path
    database: Path
    command: str
    args: list[str]
    env: dict[str, str]
    timeout_sec: float = 30.0


def _source_env(source_env: Mapping[str, str] | None = None) -> dict[str, str]:
    if source_env is not None:
        return dict(source_env)
    resolved = read_repo_dotenv_values(_DOTENV_KEYS)
    resolved.update(os.environ)
    return resolved


def _normalise_user_concept_id(value: Any) -> str | None:
    cleaned = clean_env_value(value if isinstance(value, str) else None)
    if cleaned is None or not cleaned.startswith("#V#"):
        return None
    return cleaned


def _configured_resource_identity(
    source_env: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    env = _source_env(source_env)
    resource_id = (
        clean_env_value(env.get(OTTER_ARCHIVE_RESOURCE_ID_ENV)) or DEFAULT_RESOURCE_ID
    )
    owner_id = _normalise_user_concept_id(
        env.get(OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID_ENV)
    )
    return resource_id, owner_id


def otter_archive_resource_binding_for_user(
    user_concept_id: str | None,
    *,
    source_env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the private resource selector only for its configured owner.

    Callers must supply identity derived from Von's authenticated request
    boundary. The selector, rather than a path or owner id, is injected into
    the model tool call as a hidden trusted argument.
    """

    actor_id = _normalise_user_concept_id(user_concept_id)
    resource_id, owner_id = _configured_resource_identity(source_env)
    if actor_id is None or owner_id is None or actor_id != owner_id:
        return None
    return resource_id


def resolve_otter_archive_invocation_authority(
    *,
    resource_id: str,
    source_env: Mapping[str, str] | None = None,
) -> OtterArchiveInvocationAuthority:
    """Recheck trusted invocation provenance and the owner binding."""

    from .gateway import (
        get_internal_mcp_actor_context_source,
        get_internal_mcp_preexisting_actor_context,
        internal_mcp_actor_context_is_trusted_local_operator,
        internal_mcp_actor_context_is_untrusted_payload_fallback,
    )

    configured_resource_id, owner_id = _configured_resource_identity(source_env)
    requested_resource_id = str(resource_id or "").strip()
    if owner_id is None or not configured_resource_id:
        raise OtterArchiveInvocationAuthorityError(
            reason_code="otter_archive_owner_configuration_unavailable",
            safe_message="OtterArchive resource ownership is not configured.",
        )
    if requested_resource_id != configured_resource_id:
        raise OtterArchiveInvocationAuthorityError(
            reason_code="otter_archive_resource_not_authorised",
            safe_message=(
                "The requested OtterArchive resource is not authorised for this invocation."
            ),
        )

    if internal_mcp_actor_context_is_trusted_local_operator():
        return OtterArchiveInvocationAuthority(
            principal_kind="trusted_local_operator",
            resource_id=configured_resource_id,
        )

    if internal_mcp_actor_context_is_untrusted_payload_fallback():
        raise OtterArchiveInvocationAuthorityError(
            reason_code="authenticated_actor_context_required",
            safe_message=(
                "OtterArchive access requires an authenticated actor or the trusted "
                "local operator route; tool-payload identity is not authority."
            ),
        )

    source = get_internal_mcp_actor_context_source()
    preexisting_actor = get_internal_mcp_preexisting_actor_context()
    actor_id = (
        _normalise_user_concept_id(preexisting_actor[0])
        if preexisting_actor is not None
        else None
    )
    if source is None or actor_id is None:
        raise OtterArchiveInvocationAuthorityError(
            reason_code="authenticated_actor_context_required",
            safe_message="OtterArchive access requires an authenticated Von actor.",
        )
    if actor_id != owner_id:
        raise OtterArchiveInvocationAuthorityError(
            reason_code="otter_archive_resource_not_authorised",
            safe_message="The requested OtterArchive resource is not authorised for this actor.",
        )
    return OtterArchiveInvocationAuthority(
        principal_kind="authenticated_actor",
        actor_user_concept_id=actor_id,
        resource_id=configured_resource_id,
    )


class OtterArchiveMCPProxy:
    """Call a local OtterArchive MCP process bound to one configured database."""

    def __init__(self, config: OtterArchiveProxyConfig):
        self._config = config
        self._client = MCPStdIOClient(
            MCPServerConfig(
                command=config.command,
                args=config.args,
                env=config.env,
                timeout_sec=config.timeout_sec,
                log_tag=_LOG_TAG,
            )
        )

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = await self._client.call_tool(tool_name, arguments)
        except MCPToolClientError as exc:
            raise OtterArchiveProxyError(
                str(exc),
                error_code="otter_archive_mcp_call_failed",
                details={"tool_name": tool_name},
            ) from exc
        if isinstance(payload, dict):
            return dict(payload)
        return {"result": payload}

    def get_stats(self) -> dict[str, int]:
        return {
            "call_count": self._client.call_count,
            "error_count": self._client.error_count,
        }


def _default_entrypoint(project_dir: Path) -> Path:
    if os.name == "nt":
        return project_dir / ".venv" / "Scripts" / "otter-archive-mcp.exe"
    return project_dir / ".venv" / "bin" / "otter-archive-mcp"


def _build_otter_archive_config(
    *,
    resource_id: str,
    source_env: Mapping[str, str] | None = None,
) -> OtterArchiveProxyConfig:
    env_source = _source_env(source_env)
    configured_resource_id, owner_id = _configured_resource_identity(env_source)
    if owner_id is None:
        raise OtterArchiveProxyError(
            f"Set {OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID_ENV} to the owning Von user concept ID.",
            error_code="otter_archive_owner_configuration_unavailable",
        )
    if resource_id != configured_resource_id:
        raise OtterArchiveProxyError(
            "Unknown OtterArchive resource selector.",
            error_code="otter_archive_resource_configuration_unavailable",
        )

    project_raw = clean_env_value(env_source.get(OTTER_ARCHIVE_PROJECT_DIR_ENV))
    if project_raw is None:
        raise OtterArchiveProxyError(
            f"Set {OTTER_ARCHIVE_PROJECT_DIR_ENV} to the separate OtterArchiveMCP checkout.",
            error_code="otter_archive_project_configuration_unavailable",
        )
    project_dir = Path(project_raw).expanduser().resolve()
    if not project_dir.is_dir():
        raise OtterArchiveProxyError(
            f"OtterArchive MCP project directory does not exist: {project_dir}",
            error_code="otter_archive_project_unavailable",
        )

    database_raw = clean_env_value(env_source.get(OTTER_ARCHIVE_DATABASE_ENV))
    if database_raw is None:
        raise OtterArchiveProxyError(
            f"Set {OTTER_ARCHIVE_DATABASE_ENV} to the private derived SQLite index.",
            error_code="otter_archive_database_configuration_unavailable",
        )
    database = Path(database_raw).expanduser().resolve()
    if not database.is_file():
        raise OtterArchiveProxyError(
            f"OtterArchive index does not exist: {database}",
            error_code="otter_archive_database_unavailable",
        )

    command_override = clean_env_value(env_source.get(OTTER_ARCHIVE_COMMAND_ENV))
    command = command_override or str(_default_entrypoint(project_dir))
    resolved_command = (
        shutil.which(command) if not Path(command).is_absolute() else command
    )
    if not resolved_command or not Path(resolved_command).is_file():
        raise OtterArchiveProxyError(
            f"OtterArchive MCP executable does not exist: {command}",
            error_code="otter_archive_executable_unavailable",
            details={
                "recovery": (
                    f"Run 'uv sync --frozen' in {project_dir} or set "
                    f"{OTTER_ARCHIVE_COMMAND_ENV}."
                )
            },
        )

    timeout_raw = clean_env_value(env_source.get(OTTER_ARCHIVE_TIMEOUT_ENV))
    try:
        timeout_sec = float(timeout_raw) if timeout_raw is not None else 30.0
    except ValueError:
        timeout_sec = 30.0
    timeout_sec = max(5.0, min(300.0, timeout_sec))

    subprocess_env = {
        key: value
        for key in _SUBPROCESS_PASSTHROUGH_KEYS
        if (value := env_source.get(key))
    }
    subprocess_env[OTTER_ARCHIVE_DATABASE_ENV] = str(database)
    return OtterArchiveProxyConfig(
        resource_id=resource_id,
        owner_user_concept_id=owner_id,
        project_dir=project_dir,
        database=database,
        command=str(resolved_command),
        args=[],
        env=subprocess_env,
        timeout_sec=timeout_sec,
    )


_proxy_instance: OtterArchiveMCPProxy | None = None
_proxy_resource_id: str | None = None
_proxy_lock = threading.Lock()


async def get_otter_archive_proxy(*, resource_id: str) -> OtterArchiveMCPProxy:
    """Get the singleton proxy for the configured private resource."""

    global _proxy_instance, _proxy_resource_id

    with _proxy_lock:
        if _proxy_instance is None or _proxy_resource_id != resource_id:
            config = _build_otter_archive_config(resource_id=resource_id)
            _proxy_instance = OtterArchiveMCPProxy(config)
            _proxy_resource_id = resource_id
            logger.info(
                "%s Initialised owner-scoped OtterArchive MCP proxy for resource=%s",
                _LOG_TAG,
                resource_id,
            )
        return _proxy_instance


def reset_otter_archive_proxy_for_tests() -> None:
    global _proxy_instance, _proxy_resource_id
    with _proxy_lock:
        _proxy_instance = None
        _proxy_resource_id = None
