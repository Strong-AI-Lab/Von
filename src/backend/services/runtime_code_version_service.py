from __future__ import annotations

import os
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

RUNTIME_CODE_VERSION_SCHEMA_VERSION = "runtime_code_version.v1"
LEGACY_APP_VERSION = "v20250421_1015_backend"
DEFAULT_APP_VERSION = "v0_unversioned_backend"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_GIT_TIMEOUT_SECONDS = 2.0


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_optional_bool(value: object) -> bool | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    lowered = cleaned.lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _run_git_command(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return None

    cleaned = completed.stdout.strip()
    return cleaned or None


def _version_base_from_commit_timestamp(timestamp: object) -> str | None:
    cleaned = _clean_text(timestamp)
    if cleaned is None:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"v{parsed.strftime('%Y%m%d_%H%M')}_backend"


@lru_cache(maxsize=1)
def get_runtime_code_version_info() -> dict[str, Any]:
    env_version = _clean_text(
        os.getenv("VON_BUILD_VERSION") or os.getenv("VON_CODE_VERSION")
    )
    env_commit = _clean_text(
        os.getenv("VON_BUILD_COMMIT") or os.getenv("VON_GIT_COMMIT")
    )
    env_branch = _clean_text(
        os.getenv("VON_BUILD_BRANCH") or os.getenv("VON_GIT_BRANCH")
    )
    env_dirty = _parse_optional_bool(
        os.getenv("VON_BUILD_DIRTY") or os.getenv("VON_GIT_DIRTY")
    )

    if env_version is not None:
        git_short_commit = env_commit[:12] if env_commit else None
        return {
            "schema_version": RUNTIME_CODE_VERSION_SCHEMA_VERSION,
            "version": env_version,
            "version_base": env_version,
            "source": "env",
            "legacy_app_version": LEGACY_APP_VERSION,
            "git_commit": env_commit,
            "git_short_commit": git_short_commit,
            "git_branch": env_branch,
            "git_dirty": env_dirty,
            "git_commit_timestamp": None,
        }

    git_commit = _run_git_command("rev-parse", "HEAD")
    git_short_commit = _run_git_command("rev-parse", "--short=12", "HEAD")
    git_branch = _run_git_command("rev-parse", "--abbrev-ref", "HEAD")
    git_status = _run_git_command("status", "--porcelain", "--untracked-files=no")
    git_commit_timestamp = _run_git_command("show", "-s", "--format=%cI", "HEAD")
    git_dirty = bool(git_status.strip()) if isinstance(git_status, str) else None

    if git_short_commit is not None:
        version_base = (
            _version_base_from_commit_timestamp(git_commit_timestamp)
            or "vunknown_backend"
        )
        version = f"{version_base}+g{git_short_commit}"
        if git_dirty is True:
            version = f"{version}.dirty"
        return {
            "schema_version": RUNTIME_CODE_VERSION_SCHEMA_VERSION,
            "version": version,
            "version_base": version_base,
            "source": "git",
            "legacy_app_version": LEGACY_APP_VERSION,
            "git_commit": git_commit,
            "git_short_commit": git_short_commit,
            "git_branch": git_branch,
            "git_dirty": git_dirty,
            "git_commit_timestamp": git_commit_timestamp,
        }

    return {
        "schema_version": RUNTIME_CODE_VERSION_SCHEMA_VERSION,
        "version": DEFAULT_APP_VERSION,
        "version_base": DEFAULT_APP_VERSION,
        "source": "static",
        "legacy_app_version": LEGACY_APP_VERSION,
        "git_commit": None,
        "git_short_commit": None,
        "git_branch": None,
        "git_dirty": None,
        "git_commit_timestamp": None,
    }


def get_runtime_code_version() -> str:
    return str(get_runtime_code_version_info().get("version") or DEFAULT_APP_VERSION)


def clear_runtime_code_version_cache() -> None:
    get_runtime_code_version_info.cache_clear()
