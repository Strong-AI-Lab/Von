"""Lightweight runtime diagnostics for namespace isolation hardening.

This module keeps in-memory counters and recent events for namespace
provenance/mismatch monitoring. It is intentionally process-local and best
effort, suitable for operator diagnostics rather than durable analytics.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Mapping

_STATE_LOCK = Lock()
_MAX_RECENT_EVENTS = 80

_COUNTERS: dict[str, int] = {
    "events_total": 0,
    "namespace_mismatch_events": 0,
    "missing_component_events": 0,
    "missing_namespace_events": 0,
    "org_scope_downgrade_events": 0,
}
_EVENT_TYPE_COUNTS: Counter[str] = Counter()
_FLOW_COUNTS: Counter[str] = Counter()
_RECENT_EVENTS: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT_EVENTS)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _is_org_scoped_namespace(namespace: str | None) -> bool:
    return isinstance(namespace, str) and namespace.strip().startswith("#V#") and (
        "@" in namespace.strip()
    )


def _increment_counters_for_event_type(event_type: str) -> None:
    _COUNTERS["events_total"] = int(_COUNTERS.get("events_total", 0)) + 1
    if event_type == "namespace_mismatch":
        _COUNTERS["namespace_mismatch_events"] = (
            int(_COUNTERS.get("namespace_mismatch_events", 0)) + 1
        )
    if event_type.startswith("missing_component"):
        _COUNTERS["missing_component_events"] = (
            int(_COUNTERS.get("missing_component_events", 0)) + 1
        )
    if event_type == "missing_component.namespace":
        _COUNTERS["missing_namespace_events"] = (
            int(_COUNTERS.get("missing_namespace_events", 0)) + 1
        )
    if event_type == "org_scope_downgrade":
        _COUNTERS["org_scope_downgrade_events"] = (
            int(_COUNTERS.get("org_scope_downgrade_events", 0)) + 1
        )


def record_namespace_isolation_event(
    *,
    flow: str,
    event_type: str,
    namespace: str | None = None,
    namespace_source: str | None = None,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one namespace-isolation diagnostic event."""
    safe_flow = _normalise_optional_text(flow) or "unknown_flow"
    safe_event_type = _normalise_optional_text(event_type) or "unknown_event"
    safe_namespace = _normalise_optional_text(namespace)
    safe_namespace_source = _normalise_optional_text(namespace_source)
    safe_user_concept_id = _normalise_optional_text(user_concept_id)
    safe_organisation_concept_id = _normalise_optional_text(organisation_concept_id)
    event: dict[str, Any] = {
        "timestamp_utc": _utc_now_iso(),
        "flow": safe_flow,
        "event_type": safe_event_type,
        "namespace": safe_namespace,
        "namespace_source": safe_namespace_source,
        "user_concept_id": safe_user_concept_id,
        "organisation_concept_id": safe_organisation_concept_id,
    }
    if isinstance(details, Mapping):
        event["details"] = dict(details)

    with _STATE_LOCK:
        _increment_counters_for_event_type(safe_event_type)
        _EVENT_TYPE_COUNTS[safe_event_type] += 1
        _FLOW_COUNTS[safe_flow] += 1
        _RECENT_EVENTS.append(event)

    return event


def record_namespace_context_observation(
    *,
    flow: str,
    namespace: str | None,
    namespace_source: str | None,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    mismatch_detected: bool = False,
    details: Mapping[str, Any] | None = None,
) -> int:
    """Record mismatch/missing-component events for one namespace decision."""
    safe_namespace = _normalise_optional_text(namespace)
    safe_namespace_source = _normalise_optional_text(namespace_source)
    safe_user_concept_id = _normalise_optional_text(user_concept_id)
    safe_organisation_concept_id = _normalise_optional_text(organisation_concept_id)

    events_emitted = 0
    if mismatch_detected:
        record_namespace_isolation_event(
            flow=flow,
            event_type="namespace_mismatch",
            namespace=safe_namespace,
            namespace_source=safe_namespace_source,
            user_concept_id=safe_user_concept_id,
            organisation_concept_id=safe_organisation_concept_id,
            details=details,
        )
        events_emitted += 1

    if safe_namespace is None:
        record_namespace_isolation_event(
            flow=flow,
            event_type="missing_component.namespace",
            namespace=safe_namespace,
            namespace_source=safe_namespace_source,
            user_concept_id=safe_user_concept_id,
            organisation_concept_id=safe_organisation_concept_id,
            details=details,
        )
        events_emitted += 1

    if safe_user_concept_id is None:
        record_namespace_isolation_event(
            flow=flow,
            event_type="missing_component.user_concept_id",
            namespace=safe_namespace,
            namespace_source=safe_namespace_source,
            user_concept_id=safe_user_concept_id,
            organisation_concept_id=safe_organisation_concept_id,
            details=details,
        )
        events_emitted += 1

    if _is_org_scoped_namespace(safe_namespace) and safe_organisation_concept_id is None:
        record_namespace_isolation_event(
            flow=flow,
            event_type="missing_component.organisation_concept_id",
            namespace=safe_namespace,
            namespace_source=safe_namespace_source,
            user_concept_id=safe_user_concept_id,
            organisation_concept_id=safe_organisation_concept_id,
            details=details,
        )
        events_emitted += 1

    if (
        safe_organisation_concept_id is not None
        and safe_namespace is not None
        and not _is_org_scoped_namespace(safe_namespace)
    ):
        record_namespace_isolation_event(
            flow=flow,
            event_type="org_scope_downgrade",
            namespace=safe_namespace,
            namespace_source=safe_namespace_source,
            user_concept_id=safe_user_concept_id,
            organisation_concept_id=safe_organisation_concept_id,
            details=details,
        )
        events_emitted += 1

    return events_emitted


def get_namespace_isolation_diagnostics_snapshot(
    *, include_recent_events: bool = False, recent_limit: int = 20
) -> dict[str, Any]:
    """Return compact counters and optional recent event diagnostics."""
    safe_recent_limit = max(1, int(recent_limit or 20))
    with _STATE_LOCK:
        counters = dict(_COUNTERS)
        event_type_counts = dict(_EVENT_TYPE_COUNTS)
        flow_counts = dict(_FLOW_COUNTS)
        recent_events = list(_RECENT_EVENTS)
        last_event_at_utc = (
            recent_events[-1].get("timestamp_utc") if recent_events else None
        )

    payload: dict[str, Any] = {
        "counters": counters,
        "event_type_counts": event_type_counts,
        "flow_counts": flow_counts,
        "last_event_at_utc": last_event_at_utc,
    }
    if include_recent_events:
        payload["recent_events"] = recent_events[-safe_recent_limit:]
    return payload


def reset_namespace_isolation_diagnostics() -> None:
    """Reset in-memory diagnostics state (primarily for tests)."""
    with _STATE_LOCK:
        for key in list(_COUNTERS.keys()):
            _COUNTERS[key] = 0
        _EVENT_TYPE_COUNTS.clear()
        _FLOW_COUNTS.clear()
        _RECENT_EVENTS.clear()

