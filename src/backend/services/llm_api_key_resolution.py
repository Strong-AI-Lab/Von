"""Helpers for resolving LLM API keys from environment variables."""

from __future__ import annotations

from ..utils.runtime_env import load_secret_from_env_or_file

_GEMINI_API_KEY_ENV_VARS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


def _first_nonempty_env_value(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = load_secret_from_env_or_file(name, f"{name}_FILE")
        if value:
            return value
    return None


def get_gemini_api_key() -> str | None:
    """Return the configured Gemini API key.

    Prefer ``GEMINI_API_KEY`` (or ``GEMINI_API_KEY_FILE``) while still
    accepting the legacy ``GOOGLE_API_KEY`` pair so existing developer
    environments keep working during the migration.
    """

    return _first_nonempty_env_value(_GEMINI_API_KEY_ENV_VARS)


def get_openrouter_api_key() -> str | None:
    """Return the configured OpenRouter key from env or an env-selected file."""

    return _first_nonempty_env_value(("OPENROUTER_API_KEY",))
