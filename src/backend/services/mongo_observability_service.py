"""Lightweight MongoDB operation attribution for hot-path cost audits.

This module deliberately records route/service/operation metadata only. It does
not inspect query bodies, documents, prompts, email bodies, or raw diagnostics.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from typing import Any, Mapping


_ATTRIBUTION_JIRA_KEY = "JVNAUTOSCI-2391"
_SNAPSHOT_LOCK = threading.Lock()
_OPERATION_STATS: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
    lambda: {
        "count": 0,
        "failure_count": 0,
        "slow_count": 0,
        "total_elapsed_ms": 0.0,
        "max_elapsed_ms": 0.0,
        "last_error_type": None,
    }
)


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def mongo_query_attribution_enabled() -> bool:
    """Return whether MongoDB profiler comments should be attached."""

    return _parse_bool_env("VON_MONGO_QUERY_ATTRIBUTION_ENABLED", True)


def mongo_operation_audit_enabled() -> bool:
    """Return whether in-process operation counters should be recorded."""

    return _parse_bool_env("VON_MONGO_OPERATION_AUDIT_ENABLED", True)


def mongo_operation_slow_threshold_ms() -> float:
    return _positive_float_env("VON_MONGO_OPERATION_AUDIT_SLOW_MS", 500.0)


def current_mongo_route_context() -> dict[str, str]:
    """Return safe Flask route metadata when a request context is active."""

    try:
        from flask import has_request_context, request

        if not has_request_context():
            return {}
        context: dict[str, str] = {}
        method = str(getattr(request, "method", "") or "").strip()
        endpoint = str(getattr(request, "endpoint", "") or "").strip()
        path = str(getattr(request, "path", "") or "").strip()
        if method:
            context["method"] = method
        if endpoint:
            context["endpoint"] = endpoint
        if path:
            context["path"] = path
        return context
    except Exception:
        return {}


def build_mongo_operation_comment(
    *,
    service: str,
    collection: str,
    operation: str,
    detail: str | None = None,
) -> dict[str, Any] | None:
    """Build a PyMongo/Atlas profiler comment for route-service attribution."""

    if not mongo_query_attribution_enabled():
        return None

    comment: dict[str, Any] = {
        "app": "von",
        "jira": _ATTRIBUTION_JIRA_KEY,
        "service": str(service or "unknown")[:96],
        "collection": str(collection or "unknown")[:96],
        "operation": str(operation or "unknown")[:96],
    }
    route_context = current_mongo_route_context()
    if route_context:
        comment["route"] = route_context
    if isinstance(detail, str) and detail.strip():
        comment["detail"] = detail.strip()[:96]
    return comment


def record_mongo_operation(
    *,
    service: str,
    collection: str,
    operation: str,
    elapsed_ms: float,
    success: bool,
    detail: str | None = None,
    error_type: str | None = None,
) -> None:
    """Record a safe in-process counter for a MongoDB operation."""

    if not mongo_operation_audit_enabled():
        return

    route_context = current_mongo_route_context()
    route_key = (
        route_context.get("endpoint")
        or route_context.get("path")
        or "background"
    )
    key = (
        str(service or "unknown")[:96],
        str(collection or "unknown")[:96],
        str(operation or "unknown")[:96],
        str(route_key or "background")[:160],
    )
    elapsed_value = max(0.0, float(elapsed_ms or 0.0))
    slow_threshold = mongo_operation_slow_threshold_ms()

    with _SNAPSHOT_LOCK:
        row = _OPERATION_STATS[key]
        row["count"] += 1
        row["total_elapsed_ms"] += elapsed_value
        row["max_elapsed_ms"] = max(float(row["max_elapsed_ms"]), elapsed_value)
        if not success:
            row["failure_count"] += 1
            row["last_error_type"] = str(error_type or "unknown")[:96]
        if elapsed_value >= slow_threshold:
            row["slow_count"] += 1
        if isinstance(detail, str) and detail.strip():
            row["last_detail"] = detail.strip()[:96]


def observe_mongo_operation(
    *,
    service: str,
    collection: str,
    operation: str,
    started_at: float,
    success: bool,
    detail: str | None = None,
    error_type: str | None = None,
) -> None:
    record_mongo_operation(
        service=service,
        collection=collection,
        operation=operation,
        elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
        success=success,
        detail=detail,
        error_type=error_type,
    )


def get_mongo_operation_audit_snapshot(*, reset: bool = False) -> dict[str, Any]:
    """Return safe aggregate counters for recent MongoDB operations."""

    with _SNAPSHOT_LOCK:
        operations = []
        for (service, collection, operation, route), row in _OPERATION_STATS.items():
            count = int(row["count"])
            total_elapsed_ms = float(row["total_elapsed_ms"])
            operations.append(
                {
                    "service": service,
                    "collection": collection,
                    "operation": operation,
                    "route": route,
                    "count": count,
                    "failure_count": int(row["failure_count"]),
                    "slow_count": int(row["slow_count"]),
                    "total_elapsed_ms": round(total_elapsed_ms, 3),
                    "average_elapsed_ms": round(total_elapsed_ms / count, 3)
                    if count > 0
                    else 0.0,
                    "max_elapsed_ms": round(float(row["max_elapsed_ms"]), 3),
                    "last_error_type": row.get("last_error_type"),
                    "last_detail": row.get("last_detail"),
                }
            )
        if reset:
            _OPERATION_STATS.clear()

    operations.sort(
        key=lambda item: (
            int(item.get("slow_count") or 0),
            float(item.get("total_elapsed_ms") or 0.0),
            int(item.get("count") or 0),
        ),
        reverse=True,
    )
    return {
        "schema_version": "mongo_operation_audit_snapshot.v1",
        "attribution_jira": _ATTRIBUTION_JIRA_KEY,
        "query_attribution_enabled": mongo_query_attribution_enabled(),
        "operation_audit_enabled": mongo_operation_audit_enabled(),
        "slow_threshold_ms": mongo_operation_slow_threshold_ms(),
        "operations": operations,
    }


def reset_mongo_operation_audit_snapshot() -> None:
    with _SNAPSHOT_LOCK:
        _OPERATION_STATS.clear()


__all__ = [
    "build_mongo_operation_comment",
    "get_mongo_operation_audit_snapshot",
    "mongo_operation_audit_enabled",
    "mongo_operation_slow_threshold_ms",
    "mongo_query_attribution_enabled",
    "observe_mongo_operation",
    "record_mongo_operation",
    "reset_mongo_operation_audit_snapshot",
]
