"""Durable worker build identity helpers.

The durable worker uses this module for generic execution provenance and
minimum-build claim gates. It deliberately knows nothing about workflow policy.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from typing import Any

from ...services.runtime_code_version_service import get_runtime_code_version_info
from .authority_snapshot_attestation import (
    DURABLE_WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION,
    EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY,
)

WORKER_BUILD_IDENTITY_SCHEMA_VERSION = "durable_worker_build_identity.v1"
WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION = DURABLE_WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION

_IDENTITY_STRING_FIELDS = (
    "version",
    "version_base",
    "source",
    "legacy_app_version",
    "git_commit",
    "git_short_commit",
    "git_branch",
    "git_commit_timestamp",
    "hostname",
    "worker_id",
)
_TOKEN_FIELDS = (
    "version",
    "version_base",
    "legacy_app_version",
    "git_commit",
    "git_short_commit",
)
_MIN_WORKER_BUILD_ENV_NAMES = (
    "VON_DURABLE_MIN_WORKER_BUILD",
    "VON_DURABLE_MIN_WORKER_GIT_SHORT_COMMIT",
)
_MAX_WORKER_CAPABILITIES = 32


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_worker_id(value: object) -> str | None:
    cleaned = _clean_text(value)
    return cleaned or None


def _commit_prefixes(value: str) -> list[str]:
    cleaned = value.strip()
    if len(cleaned) < 7:
        return []
    return [cleaned[:length] for length in range(7, len(cleaned) + 1)]


def worker_build_match_tokens(
    worker_build_identity: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Return exact tokens that may satisfy a required worker-build value."""
    if not isinstance(worker_build_identity, Mapping):
        return ()

    tokens: list[str] = []
    for field in _TOKEN_FIELDS:
        value = _clean_text(worker_build_identity.get(field))
        if value and value not in tokens:
            tokens.append(value)
        if field in {"git_commit", "git_short_commit"} and value:
            for prefix in _commit_prefixes(value):
                if prefix not in tokens:
                    tokens.append(prefix)
                prefixed_prefix = f"g{prefix}"
                if prefixed_prefix not in tokens:
                    tokens.append(prefixed_prefix)
            prefixed = f"g{value}"
            if prefixed not in tokens:
                tokens.append(prefixed)

    capabilities = worker_build_identity.get("capabilities")
    if isinstance(capabilities, (list, tuple, set, frozenset)):
        for raw_capability in capabilities:
            capability = _clean_text(raw_capability)
            if capability and capability not in tokens:
                tokens.append(capability)

    return tuple(tokens)


def normalise_worker_build_identity(
    worker_build_identity: Mapping[str, Any] | None,
    *,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Return a compact, serialisable durable-worker build identity."""
    source = (
        dict(worker_build_identity)
        if isinstance(worker_build_identity, Mapping)
        else {}
    )
    identity: dict[str, Any] = {
        "schema_version": WORKER_BUILD_IDENTITY_SCHEMA_VERSION,
    }

    for field in _IDENTITY_STRING_FIELDS:
        value = _clean_text(source.get(field))
        if value is not None:
            identity[field] = value

    worker_id_clean = _clean_worker_id(worker_id) or _clean_worker_id(
        identity.get("worker_id")
    )
    if worker_id_clean:
        identity["worker_id"] = worker_id_clean

    pid_value = source.get("pid")
    if isinstance(pid_value, int):
        identity["pid"] = pid_value
    else:
        try:
            identity["pid"] = int(str(pid_value).strip())
        except (TypeError, ValueError):
            pass

    dirty_value = source.get("git_dirty")
    if isinstance(dirty_value, bool):
        identity["git_dirty"] = dirty_value
    elif dirty_value is None:
        identity["git_dirty"] = None

    raw_capabilities = source.get("capabilities")
    if isinstance(raw_capabilities, (list, tuple, set, frozenset)):
        capabilities: list[str] = []
        for raw_capability in raw_capabilities:
            capability = _clean_text(raw_capability)
            if capability and capability not in capabilities:
                capabilities.append(capability)
            if len(capabilities) >= _MAX_WORKER_CAPABILITIES:
                break
        if capabilities:
            identity["capabilities"] = sorted(capabilities)

    identity["match_tokens"] = list(worker_build_match_tokens(identity))
    return identity


def build_worker_build_identity(worker_id: str) -> dict[str, Any]:
    """Build the current process worker identity once at worker startup."""
    version_info = dict(get_runtime_code_version_info())
    version_info["worker_id"] = worker_id
    version_info["hostname"] = socket.gethostname()
    version_info["pid"] = os.getpid()
    version_info["capabilities"] = [
        EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY,
    ]
    return normalise_worker_build_identity(version_info, worker_id=worker_id)


def build_claim_provenance(
    worker_build_identity: Mapping[str, Any] | None,
    *,
    worker_id: str,
) -> dict[str, Any]:
    """Build the provenance payload stamped on a claimed instance."""
    identity = normalise_worker_build_identity(
        worker_build_identity,
        worker_id=worker_id,
    )
    identity["schema_version"] = WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION
    return identity


def get_configured_min_worker_build() -> str | None:
    """Return the cluster-level required worker-build token, if configured."""
    for env_name in _MIN_WORKER_BUILD_ENV_NAMES:
        value = _clean_text(os.getenv(env_name))
        if value:
            return value
    return None


def worker_satisfies_required_build(
    worker_build_identity: Mapping[str, Any] | None,
    required_build: object,
) -> bool:
    """Return whether ``worker_build_identity`` satisfies ``required_build``."""
    required = _clean_text(required_build)
    if required is None:
        return True
    return required in worker_build_match_tokens(worker_build_identity)
