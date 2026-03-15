"""Shared runtime environment parsing helpers."""

from __future__ import annotations

import os
from collections.abc import Iterable, MutableMapping
from pathlib import Path


def truthy(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def clean_env_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value or None


def get_project_root() -> Path:
    """Return the repository root for repo-local runtime assets such as `.env`."""

    return Path(__file__).resolve().parents[3]


def read_repo_dotenv_values(keys: Iterable[str]) -> dict[str, str]:
    """Read selected repo-root `.env` values without mutating `os.environ`."""

    requested_keys = [str(key).strip() for key in keys if str(key).strip()]
    if not requested_keys:
        return {}

    try:
        from dotenv import dotenv_values  # type: ignore
    except Exception:
        return {}

    env_path = get_project_root() / ".env"
    if not env_path.exists():
        return {}

    try:
        values = dotenv_values(env_path)
    except Exception:
        return {}

    selected: dict[str, str] = {}
    for key in requested_keys:
        cleaned = clean_env_value(
            None if values.get(key) is None else str(values.get(key))
        )
        if cleaned is not None:
            selected[key] = cleaned
    return selected


def apply_repo_dotenv_overrides(
    keys: Iterable[str],
    *,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Apply selected repo-root `.env` values into the target environment mapping."""

    target_env = os.environ if environ is None else environ
    selected = read_repo_dotenv_values(keys)
    for key, value in selected.items():
        target_env[key] = value
    return selected


def read_secret_file(path: str | None) -> str | None:
    clean_path = clean_env_value(path)
    if not clean_path:
        return None
    try:
        data = Path(clean_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return data or None


def load_secret_from_env_or_file(env_name: str, file_env_name: str) -> str | None:
    """Resolve a secret value from ENV directly or from a file path ENV."""
    direct = clean_env_value(os.getenv(env_name))
    if direct:
        return direct
    return read_secret_file(os.getenv(file_env_name))


def get_env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return truthy(raw)
