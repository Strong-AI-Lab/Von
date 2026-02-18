import threading
import time
import random
import logging
from typing import Optional, Dict, Any
from pymongo.errors import ConnectionFailure

from . import mongo_client as _mc

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


def _now() -> float:
    return time.time()


def get_db():
    db = _mc.get_db()
    if db is not None and _state["last_connected_at"] is None:
        _state["last_connected_at"] = _now()
    return db


def health_summary() -> Dict[str, Any]:
    db = get_db()
    connected = False
    if db is not None:
        try:
            # Use lightweight ping; ignore actual response object
            db.command("ping")  # type: ignore[call-arg]
            connected = True
        except Exception:
            connected = False
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
    return {
        "connected": connected,
        "using_fallback": using_fallback,
        "effective_uri": effective_uri,
        "uptime_seconds": uptime,
        "metrics": metrics,
    }


def _exponential_backoff(base_ms: int, attempt: int, max_ms: int) -> float:
    cap = min(max_ms, base_ms * (2 ** (attempt - 1)))
    jitter = random.uniform(0, cap)
    return jitter / 1000.0


def attempt_reconnect(
    force: bool = False, max_attempts: int = 5, base_ms: int = 1000, max_ms: int = 30000
) -> Dict[str, Any]:
    with _lock:
        # If already healthy and not forced, short circuit
        h = health_summary()
        if h["connected"] and not force:
            return {"skipped": True, "reason": "already_connected", **h}
    # Outside lock to avoid blocking other threads while connecting
    attempt_num = 0
    last_err = None
    while attempt_num < max_attempts:
        attempt_num += 1
        with _lock:
            _state["reconnect_attempts_total"] += 1
            _state["last_attempt_started_at"] = _now()
        try:
            _mc.invalidate_connection()
            db = _mc.get_db()
            if db is None:
                raise ConnectionFailure("get_db returned None")
            # Ping to confirm
            db.command("ping")
            with _lock:
                _state["reconnect_success_total"] += 1
                _state["consecutive_failures"] = 0
                _state["last_connected_at"] = _now()
            return {
                "skipped": False,
                "reconnected": True,
                "attempts": attempt_num,
                **health_summary(),
            }
        except Exception as e:  # Broad catch; we classify as failure
            last_err = str(e)
            with _lock:
                _state["reconnect_failure_total"] += 1
                _state["consecutive_failures"] += 1
                _state["last_error"] = last_err
            if attempt_num >= max_attempts:
                break
            time.sleep(_exponential_backoff(base_ms, attempt_num, max_ms))
    # Failed
    h2 = health_summary()
    h2["last_error"] = last_err
    h2["reconnected"] = False
    h2["attempts"] = attempt_num
    return h2


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
