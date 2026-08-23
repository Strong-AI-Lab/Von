"""Shared Mongo error classification for retry and route-recovery decisions."""

from __future__ import annotations

from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    NetworkTimeout,
    NotPrimaryError,
    PyMongoError,
    ServerSelectionTimeoutError,
)

_TRANSIENT_MONGO_ERROR_TYPES = (
    NetworkTimeout,
    ServerSelectionTimeoutError,
    AutoReconnect,
    ConnectionFailure,
    PyMongoError,
)
_MONGO_TRANSPORT_ERROR_TYPES = (
    NetworkTimeout,
    ServerSelectionTimeoutError,
    AutoReconnect,
    ConnectionFailure,
)
_TRANSIENT_MONGO_ERROR_MARKERS = (
    "timed out",
    "no primary",
    "replicasetnoprimary",
    "connection pool paused",
    "server selection timeout",
    "networktimeout",
    "temporarily unavailable",
    # Repository accessors return ``None`` when the shared client cannot be
    # established.  Write wrappers surface that state with this bounded,
    # retryable message rather than a PyMongo exception.
    "collection not available",
)
_MONGO_TRANSPORT_ERROR_MARKERS = tuple(
    marker
    for marker in _TRANSIENT_MONGO_ERROR_MARKERS
    if marker != "collection not available"
)


def is_transient_mongo_error(exc: Exception | str | None) -> bool:
    """Return True when ``exc`` looks like a retryable Mongo failure."""

    if exc is None:
        return False
    if isinstance(exc, _TRANSIENT_MONGO_ERROR_TYPES):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_MONGO_ERROR_MARKERS)


def is_mongo_transport_error(exc: Exception | str | None) -> bool:
    """Return True when trying another configured Mongo route is justified."""

    if exc is None:
        return False
    # A normal replica election needs a topology refresh/reconnect on the
    # current route, not abandonment of an otherwise healthy transport.
    if isinstance(exc, NotPrimaryError):
        return False
    if isinstance(exc, _MONGO_TRANSPORT_ERROR_TYPES):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _MONGO_TRANSPORT_ERROR_MARKERS)
