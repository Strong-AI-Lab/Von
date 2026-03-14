"""Helpers for resolving LLM API keys from environment variables."""

from __future__ import annotations

import os

_GEMINI_API_KEY_ENV_VARS: tuple[str, ...] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def _first_nonempty_env_value(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return None


def get_gemini_api_key() -> str | None:
    """Return the configured Gemini API key.

    Prefer `GEMINI_API_KEY` while still accepting the legacy `GOOGLE_API_KEY`
    name so existing developer environments keep working during the migration.
    """

    return _first_nonempty_env_value(_GEMINI_API_KEY_ENV_VARS)
