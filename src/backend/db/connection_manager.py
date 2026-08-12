import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from pymongo.errors import ConnectionFailure

from . import mongo_client as _mc
from .mongo_error_classification import is_mongo_transport_error
from .mongo_uri_redaction import build_safe_mongo_connection_location

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_monitor_started = False
_monitor_thread: Optional[threading.Thread] = None

# Metrics / state
_state = {
    "reconnect_attempts_total": 0,
    "reconnect_success_total": 0,
    "reconnect_failure_total": 0,
    "last_error": None,
    "last_connected_at": None,  # epoch seconds
    "last_attempt_started_at": None,
    "consecutive_failures": 0,
}


@dataclass
class _ReconnectAttempt:
    """One physical reconnect attempt shared by concurrent callers."""

    route_degraded: bool
    degradation_reason: str | None
    sequence: int
    done: bool = False
    followers: int = 0
    reconnect_performed: bool = False
    route_preference_applied: bool = False
    connected: bool = False
    error_message: str | None = None
    error_type: str | None = None
    transport_error: bool = False
    error: BaseException | None = None


_reconnect_condition = threading.Condition(_lock)
_active_reconnect_attempt: _ReconnectAttempt | None = None
_last_completed_reconnect_attempt: _ReconnectAttempt | None = None
_reconnect_attempt_sequence = 0


def _completed_reconnect_sequence() -> int:
    completed = _last_completed_reconnect_attempt
    return completed.sequence if completed is not None else 0


def _now() -> float:
    return time.time()


def get_db():
    db = _mc.get_db()
    if db is not None and _state["last_connected_at"] is None:
        _state["last_connected_at"] = _now()
    return db


def _build_health_summary(
    *,
    connected: bool,
    route_degraded: bool = False,
    health_error_type: str | None = None,
) -> Dict[str, Any]:
    """Build the public health payload without issuing another Mongo probe."""

    uptime = None
    if connected and _state["last_connected_at"]:
        uptime = _now() - _state["last_connected_at"]
    # Shallow copy metrics, ensure JSON-serializable (convert exceptions to str)
    metrics: Dict[str, Any] = {}
    for k, v in _state.items():
        if (
            k.startswith("reconnect_")
            or k.startswith("last_")
            or k == "consecutive_failures"
        ):
            if isinstance(v, BaseException):
                metrics[k] = str(v)
            else:
                metrics[k] = v
    # Safely derive fallback/URI values (handle patched MagicMocks)
    try:
        using_fallback_raw = getattr(_mc, "is_using_fallback_uri", lambda: None)()
        using_fallback = (
            bool(using_fallback_raw)
            if isinstance(using_fallback_raw, (bool, int))
            or using_fallback_raw is not None
            else None
        )
    except Exception:
        using_fallback = None
    try:
        eff_uri_raw = getattr(_mc, "get_effective_mongo_uri", lambda: None)()
        effective_uri = str(eff_uri_raw) if eff_uri_raw is not None else None
    except Exception:
        effective_uri = None
    try:
        fallback_policy = getattr(_mc, "get_mongo_fallback_policy_state", lambda: {})()
    except Exception:
        fallback_policy = {}
    fallback_kind = (
        fallback_policy.get("active_fallback_kind")
        if isinstance(fallback_policy, dict)
        else None
    )
    connection_location = build_safe_mongo_connection_location(
        effective_uri,
        using_fallback=using_fallback,
        fallback_kind=fallback_kind if isinstance(fallback_kind, str) else None,
        fallback_target_uri=getattr(_mc, "MONGO_URI", None),
    )
    effective_uri_sanitized = connection_location.get("sanitized_uri")
    return {
        "connected": connected,
        "route_degraded": route_degraded,
        "health_error_type": health_error_type,
        "using_fallback": using_fallback,
        # Backwards-compatible key. This is intentionally sanitized only.
        "effective_uri": effective_uri_sanitized,
        "effective_uri_sanitized": effective_uri_sanitized,
        "effective_mongo_location": connection_location,
        "fallback_policy": fallback_policy,
        "uptime_seconds": uptime,
        "metrics": metrics,
    }


def health_summary() -> Dict[str, Any]:
    db = get_db()
    connected = False
    route_degraded = False
    health_error_type = None
    if db is not None:
        try:
            # Use lightweight ping; ignore actual response object
            db.command("ping")  # type: ignore[call-arg]
            connected = True
        except Exception as exc:
            connected = False
            route_degraded = is_mongo_transport_error(exc)
            health_error_type = type(exc).__name__
    return _build_health_summary(
        connected=connected,
        route_degraded=route_degraded,
        health_error_type=health_error_type,
    )


def _exponential_backoff(base_ms: int, attempt: int, max_ms: int) -> float:
    cap = min(max_ms, base_ms * (2 ** (attempt - 1)))
    jitter = random.uniform(0, cap)
    return jitter / 1000.0


def _completed_attempt_satisfies_intent(
    reconnect_attempt: _ReconnectAttempt,
    *,
    force: bool,
    route_degraded: bool,
) -> bool:
    """Return whether a completed attempt fulfilled this caller's intent."""

    if force and not reconnect_attempt.reconnect_performed:
        return False
    if route_degraded and not reconnect_attempt.route_preference_applied:
        return False
    return True


def _new_reconnect_attempt(
    *,
    route_degraded: bool,
    degradation_reason: str | None,
    sequence: int,
) -> _ReconnectAttempt:
    return _ReconnectAttempt(
        route_degraded=route_degraded,
        degradation_reason=degradation_reason,
        sequence=sequence,
    )


def _execute_reconnect_attempt(reconnect_attempt: _ReconnectAttempt) -> None:
    with _lock:
        _state["reconnect_attempts_total"] += 1
        _state["last_attempt_started_at"] = _now()
    try:
        reconnect_attempt.reconnect_performed = True
        if reconnect_attempt.route_degraded:
            reconnect_attempt.route_preference_applied = True
            _mc.invalidate_connection(
                prefer_alternate_reason=reconnect_attempt.degradation_reason
                or "transient operation transport failure"
            )
        else:
            _mc.invalidate_connection()
        db = _mc.get_db()
        if db is None:
            raise ConnectionFailure("get_db returned None")
        db.command("ping")
        reconnect_attempt.connected = True
        with _lock:
            _state["reconnect_success_total"] += 1
            _state["consecutive_failures"] = 0
            _state["last_connected_at"] = _now()
    except Exception as exc:  # Broad catch; we classify as failure
        reconnect_attempt.error_message = str(exc)
        reconnect_attempt.error_type = type(exc).__name__
        reconnect_attempt.transport_error = is_mongo_transport_error(exc)
        with _lock:
            _state["reconnect_failure_total"] += 1
            _state["consecutive_failures"] += 1
            _state["last_error"] = reconnect_attempt.error_message


def attempt_reconnect(
    force: bool = False,
    max_attempts: int = 5,
    base_ms: int = 1000,
    max_ms: int = 30000,
    route_degraded: bool = False,
    degradation_reason: str | None = None,
) -> Dict[str, Any]:
    global _active_reconnect_attempt
    global _last_completed_reconnect_attempt, _reconnect_attempt_sequence

    with _reconnect_condition:
        # Snapshot before any health preflight. A reconnect that completes
        # while this caller is probing is fresh work that the caller should
        # reuse instead of invalidating the recovered client again.
        observed_sequence = _completed_reconnect_sequence()

    # Forced and route-degraded callers must attempt recovery directly. A
    # healthy non-forced caller retains the legacy short-circuit.
    if not force and not route_degraded:
        health = health_summary()
        if health["connected"]:
            return {"skipped": True, "reason": "already_connected", **health}

    attempt_num = 0
    last_error = None
    last_error_type = None
    effective_route_degraded = route_degraded
    effective_degradation_reason = degradation_reason

    while attempt_num < max_attempts:
        requested_route_degraded = effective_route_degraded
        with _reconnect_condition:
            reconnect_attempt = _active_reconnect_attempt
            if reconnect_attempt is not None:
                reconnect_attempt.followers += 1
                _reconnect_condition.notify_all()
                is_leader = False
            elif (
                _last_completed_reconnect_attempt is not None
                and _last_completed_reconnect_attempt.sequence > observed_sequence
            ):
                # A faster sibling may finish the attempt between this caller
                # observing the previous result and reclaiming the condition.
                # Reuse that fresh result instead of fanning out another probe.
                reconnect_attempt = _last_completed_reconnect_attempt
                is_leader = False
            else:
                _reconnect_attempt_sequence += 1
                reconnect_attempt = _new_reconnect_attempt(
                    route_degraded=requested_route_degraded,
                    degradation_reason=effective_degradation_reason,
                    sequence=_reconnect_attempt_sequence,
                )
                _active_reconnect_attempt = reconnect_attempt
                is_leader = True

            if not is_leader:
                while not reconnect_attempt.done:
                    _reconnect_condition.wait()
                if reconnect_attempt.error is not None:
                    raise reconnect_attempt.error
                observed_sequence = max(
                    observed_sequence,
                    reconnect_attempt.sequence,
                )
                if not _completed_attempt_satisfies_intent(
                    reconnect_attempt,
                    force=force,
                    route_degraded=requested_route_degraded,
                ):
                    # This physical attempt did not fulfil stronger force/route
                    # intent. Start or join one suitable attempt before this
                    # caller consumes an attempt from its own retry budget.
                    continue

        if is_leader:
            try:
                if attempt_num > 0:
                    # Publish first, then let one leader own the backoff so all
                    # concurrent callers join the same next physical attempt.
                    time.sleep(_exponential_backoff(base_ms, attempt_num, max_ms))
                _execute_reconnect_attempt(reconnect_attempt)
                error = None
            except BaseException as exc:  # noqa: BLE001 - release waiting callers
                error = exc

            with _reconnect_condition:
                reconnect_attempt.error = error
                reconnect_attempt.done = True
                _last_completed_reconnect_attempt = reconnect_attempt
                if _active_reconnect_attempt is reconnect_attempt:
                    _active_reconnect_attempt = None
                _reconnect_condition.notify_all()

            if error is not None:
                raise error

        observed_sequence = max(observed_sequence, reconnect_attempt.sequence)

        attempt_num += 1
        if reconnect_attempt.connected:
            return {
                "skipped": False,
                "reconnected": True,
                "attempts": attempt_num,
                **_build_health_summary(connected=True),
            }

        last_error = reconnect_attempt.error_message
        last_error_type = reconnect_attempt.error_type
        if reconnect_attempt.transport_error:
            effective_route_degraded = True
            effective_degradation_reason = f"reconnect ping {last_error_type}"

    # Do not call health_summary here: its get_db/ping would exceed this
    # caller's declared retry budget after the final failed shared attempt.
    result = _build_health_summary(
        connected=False,
        route_degraded=effective_route_degraded,
        health_error_type=last_error_type,
    )
    result["last_error"] = last_error
    result["reconnected"] = False
    result["attempts"] = attempt_num
    return result


def _monitor_loop(interval_sec: int, base_ms: int, max_ms: int, max_attempts: int):
    while True:
        time.sleep(interval_sec)
        try:
            h = health_summary()
            if not h["connected"]:
                attempt_reconnect(
                    force=False,
                    max_attempts=max_attempts,
                    base_ms=base_ms,
                    max_ms=max_ms,
                    route_degraded=bool(h.get("route_degraded")),
                    degradation_reason=(
                        f"monitor ping {h.get('health_error_type')}"
                        if h.get("route_degraded")
                        else None
                    ),
                )
        except Exception as exc:
            with _lock:
                _state["consecutive_failures"] += 1
                _state["last_error"] = f"monitor_loop_error: {exc}"
            try:
                logger.warning("Mongo reconnect monitor loop error: %s", exc)
            except Exception:
                pass


def ensure_monitor_started(
    interval_sec: int = 15,
    base_ms: int = 1000,
    max_ms: int = 30000,
    max_attempts: int = 5,
):
    global _monitor_started, _monitor_thread
    with _lock:
        if _monitor_started:
            return
        _monitor_started = True
    t = threading.Thread(
        target=_monitor_loop,
        args=(interval_sec, base_ms, max_ms, max_attempts),
        daemon=True,
        name="MongoReconnectMonitor",
    )
    t.start()
    _monitor_thread = t
