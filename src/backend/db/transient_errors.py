"""Shared transient Mongo error classification and bounded retry helpers."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import TypeVar

from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    NetworkTimeout,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from .connection_manager import attempt_reconnect

T = TypeVar("T")

_TRANSIENT_MONGO_ERROR_TYPES = (
    NetworkTimeout,
    ServerSelectionTimeoutError,
    AutoReconnect,
    ConnectionFailure,
    PyMongoError,
)
_TRANSIENT_MONGO_ERROR_MARKERS = (
    "timed out",
    "no primary",
    "replicasetnoprimary",
    "connection pool paused",
    "server selection timeout",
    "networktimeout",
    "temporarily unavailable",
)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def is_transient_mongo_error(exc: Exception | str | None) -> bool:
    """Return True when ``exc`` looks like a retryable Mongo transport failure."""

    if exc is None:
        return False
    if isinstance(exc, _TRANSIENT_MONGO_ERROR_TYPES):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_MONGO_ERROR_MARKERS)


def run_with_transient_mongo_retry(
    operation: Callable[[], T],
    *,
    operation_name: str,
    logger_obj: logging.Logger | None = None,
    max_attempts: int | None = None,
    initial_delay_seconds: float | None = None,
    max_delay_seconds: float | None = None,
) -> T:
    """Run ``operation`` with bounded retries for transient Mongo failures."""

    resolved_max_attempts = max(
        1,
        int(
            max_attempts
            or _positive_int_env("VON_TRANSIENT_MONGO_RETRY_MAX_ATTEMPTS", 4)
        ),
    )
    resolved_initial_delay = max(
        0.0,
        float(
            initial_delay_seconds
            or _positive_float_env(
                "VON_TRANSIENT_MONGO_RETRY_INITIAL_DELAY_SECONDS",
                0.5,
            )
        ),
    )
    resolved_max_delay = max(
        resolved_initial_delay,
        float(
            max_delay_seconds
            or _positive_float_env(
                "VON_TRANSIENT_MONGO_RETRY_MAX_DELAY_SECONDS",
                5.0,
            )
        ),
    )

    attempt = 1
    while True:
        try:
            return operation()
        except Exception as exc:
            if not is_transient_mongo_error(exc) or attempt >= resolved_max_attempts:
                raise
            delay_seconds = min(
                resolved_initial_delay * (2 ** (attempt - 1)),
                resolved_max_delay,
            )
            if logger_obj is not None:
                logger_obj.warning(
                    "Transient Mongo error during %s (attempt %d/%d): %s; retrying in %.2fs",
                    operation_name,
                    attempt,
                    resolved_max_attempts,
                    exc,
                    delay_seconds,
                )
            try:
                attempt_reconnect(force=True, max_attempts=1)
            except Exception:
                if logger_obj is not None:
                    logger_obj.debug(
                        "Best-effort reconnect failed during %s retry.",
                        operation_name,
                        exc_info=True,
                    )
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            attempt += 1
