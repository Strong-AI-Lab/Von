"""Shared runtime environment parsing helpers."""

from __future__ import annotations

import os
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
