"""Resolve LLM API keys and classify bounded credential failover cases."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..utils.runtime_env import load_secret_from_env_or_file

_GEMINI_API_KEY_ENV_VARS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)
_GEMINI_BACKUP_API_KEY_ENV_VARS: tuple[str, ...] = (
    "GEMINI_API_BACKUP_KEY",
    "GOOGLE_API_BACKUP_KEY",
)
_OPENAI_BACKUP_API_KEY_ENV_VARS: tuple[str, ...] = ("OPENAI_API_BACKUP_KEY",)

_AUTHENTICATION_STATUS_CODES = {401, 403}
_RATE_LIMIT_STATUS_CODES = {429}
_AUTHENTICATION_ERROR_CLASS_NAMES = {
    "authenticationerror",
    "permissiondeniederror",
    "permissiondenied",
    "unauthenticated",
}
_QUOTA_ERROR_TOKENS = {
    "billing_hard_limit_reached",
    "credit_balance_exhausted",
    "insufficient_quota",
    "quota_exceeded",
    "resource_exhausted",
}


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


def get_gemini_backup_api_key() -> str | None:
    """Return the configured Gemini backup key, including the legacy alias."""

    return _first_nonempty_env_value(_GEMINI_BACKUP_API_KEY_ENV_VARS)


def get_openai_backup_api_key() -> str | None:
    """Return the configured OpenAI backup key from env or an env-selected file."""

    return _first_nonempty_env_value(_OPENAI_BACKUP_API_KEY_ENV_VARS)


def get_openrouter_api_key() -> str | None:
    """Return the configured OpenRouter key from env or an env-selected file."""

    return _first_nonempty_env_value(("OPENROUTER_API_KEY",))


def get_meta_api_key() -> str | None:
    """Return the Meta Model API key from its sole supported secret source."""

    return _first_nonempty_env_value(("META_API_KEY",))


def _normalised_provider_token(value: Any) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    token = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return token or None


def _provider_error_tokens(exc: BaseException) -> set[str]:
    """Return bounded structured provider codes without parsing free-form messages."""

    tokens: set[str] = set()
    candidates: list[Any] = [
        getattr(exc, "code", None),
        getattr(exc, "status", None),
        getattr(exc, "type", None),
    ]
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        candidates.extend(body.get(key) for key in ("code", "status", "type"))
        nested = body.get("error")
        if isinstance(nested, Mapping):
            candidates.extend(nested.get(key) for key in ("code", "status", "type"))
    for value in candidates[:12]:
        token = _normalised_provider_token(value)
        if token:
            tokens.add(token)
    return tokens


def _provider_status_code(exc: BaseException) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "code", None),
    ):
        try:
            status_code = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= status_code <= 599:
            return status_code
    return None


def classify_api_key_failover_exception(
    provider: str,
    exc: BaseException,
) -> str | None:
    """Classify only exact provider rejections suitable for one backup-key try.

    Free-form exception messages are deliberately excluded. That prevents an
    arbitrary model/request error mentioning a key or quota from causing a
    duplicate request. The caller must still preserve the same provider,
    model, actor scope, and request payload.
    """

    provider_name = str(provider or "").strip().lower()
    if provider_name not in {"openai", "gemini"}:
        return None

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        tokens = _provider_error_tokens(current)
        if tokens.intersection(_QUOTA_ERROR_TOKENS):
            return "quota_exhausted"

        status_code = _provider_status_code(current)
        if status_code in _RATE_LIMIT_STATUS_CODES:
            return "rate_limited"
        if status_code in _AUTHENTICATION_STATUS_CODES:
            return "authentication_failed"

        class_name = type(current).__name__.strip().lower()
        if class_name in _AUTHENTICATION_ERROR_CLASS_NAMES:
            return "authentication_failed"

        current = current.__cause__ or current.__context__
    return None
