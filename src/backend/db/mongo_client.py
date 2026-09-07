import datetime  # Added for type hinting and __main__ example
import ipaddress
import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from pymongo import ASCENDING, DESCENDING, MongoClient, monitoring
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, OperationFailure

from ..services.mongo_observability_service import (
    build_mongo_command_shape,
    extract_n_returned_from_reply,
    observe_mongo_write_command_size,
    record_mongo_command_observation,
    record_mongo_operation,
)
from ..utils.runtime_env import get_env_bool, load_secret_from_env_or_file
from .mongo_error_classification import is_mongo_transport_error

logger = logging.getLogger(__name__)


def _coll_and_shape_from_started_command(event) -> tuple[str, str]:
    """Extract collection name and a short filter-shape signature.

    The collection name is the value at ``command[command_name]`` for the vast
    majority of CRUD commands (find, aggregate, insert, update, delete, count,
    distinct, findAndModify, listIndexes, ...); ``getMore`` uses ``collection``.
    The filter shape lists the top-level keys of the ``filter``/``query``
    document so timeouts identify the kind of read without leaking values.
    """

    try:
        cmd_name = getattr(event, "command_name", "") or ""
        body = getattr(event, "command", None)
        if not isinstance(body, dict):
            return "", ""
        if cmd_name == "getMore":
            coll = body.get("collection") or ""
        else:
            coll_val = body.get(cmd_name)
            coll = coll_val if isinstance(coll_val, str) else ""
        filter_doc = body.get("filter") or body.get("query") or {}
        if isinstance(filter_doc, dict):
            shape = ",".join(sorted(k for k in filter_doc.keys() if isinstance(k, str)))
        else:
            shape = ""
        return coll, shape
    except Exception:
        return "", ""


def _command_shape_from_started_command(event) -> dict[str, object]:
    try:
        body = getattr(event, "command", None)
        shape = build_mongo_command_shape(
            str(getattr(event, "command_name", "") or ""),
            body if isinstance(body, dict) else None,
        )
        if shape.get("collection"):
            return shape
        coll, filter_shape = _coll_and_shape_from_started_command(event)
        shape["collection"] = coll
        shape["filter_shape"] = shape.get("filter_shape") or filter_shape
        return shape
    except Exception:
        coll, filter_shape = _coll_and_shape_from_started_command(event)
        return {"collection": coll, "filter_shape": filter_shape}


def _is_query_stats_diagnostic_command(event) -> bool:
    if str(getattr(event, "command_name", "") or "") != "aggregate":
        return False
    if str(getattr(event, "database_name", "") or "") != "admin":
        return False
    command = getattr(event, "command", None)
    if not isinstance(command, Mapping):
        return False
    pipeline = command.get("pipeline")
    return bool(
        isinstance(pipeline, list)
        and pipeline
        and isinstance(pipeline[0], Mapping)
        and "$queryStats" in pipeline[0]
    )


def _safe_mongo_failure_summary(failure) -> dict[str, object]:
    """Summarise a PyMongo command failure without raw command/server bodies."""

    summary: dict[str, object] = {
        "type": type(failure).__name__,
    }
    if isinstance(failure, dict):
        code = failure.get("code")
        if isinstance(code, int):
            summary["code"] = code
        code_name = failure.get("codeName")
        if isinstance(code_name, str) and code_name.strip():
            summary["code_name"] = code_name.strip()[:96]
        errmsg = failure.get("errmsg")
        if isinstance(errmsg, str) and "not authorized" in errmsg.lower():
            summary["message_class"] = "not_authorized"
        elif isinstance(errmsg, str) and errmsg.strip():
            summary["message_class"] = "mongo_command_error"
    elif failure:
        summary["message_class"] = "mongo_command_error"
    return summary


class _VonMongoFailureLogger(monitoring.CommandListener):
    """Emit a structured warning whenever a Mongo command fails.

    PyMongo raises ``NetworkTimeout`` / ``ServerSelectionTimeoutError`` with
    only the host:port and configured timeouts; the offending command name,
    database, collection, and slow-query shape are visible to the driver but
    not to call-site catch blocks. A global command listener captures that
    context for every failure with zero call-site changes and no overhead on
    the success path.
    """

    _SLOW_COMMAND_DURATION_MS = 1000
    _MAX_IN_FLIGHT = 4096  # cap to prevent unbounded growth on listener bugs

    def __init__(self) -> None:
        self._in_flight: dict[int, dict[str, object]] = {}
        self._lock = threading.Lock()

    def started(self, event):
        with self._lock:
            if len(self._in_flight) >= self._MAX_IN_FLIGHT:
                # Drop silently rather than block traffic; capacity loss is logged
                # only as missing context on subsequent failures.
                return
            shape = _command_shape_from_started_command(event)
            if _is_query_stats_diagnostic_command(event):
                shape["skip_query_shape_telemetry"] = True
            self._in_flight[event.request_id] = shape
        warning = observe_mongo_write_command_size(
            command_name=str(getattr(event, "command_name", "") or ""),
            database=str(getattr(event, "database_name", "") or ""),
            command=getattr(event, "command", None),
            command_shape=shape,
            request_id=getattr(event, "request_id", ""),
            source="command_listener_started",
        )
        if warning is not None:
            logger.warning(
                "[mongo_large_write] class=%s cmd=%s db=%s coll=%s "
                "estimated_size_bytes=%s warning_threshold_bytes=%s "
                "critical_threshold_bytes=%s request_id=%s",
                warning.get("warning_class"),
                warning.get("command_name"),
                warning.get("database"),
                warning.get("collection"),
                warning.get("estimated_size_bytes"),
                warning.get("warning_threshold_bytes"),
                warning.get("critical_threshold_bytes"),
                warning.get("request_id"),
            )

    def _pop_context(self, request_id: int) -> dict[str, object]:
        with self._lock:
            return self._in_flight.pop(request_id, {})

    def succeeded(self, event):
        shape = self._pop_context(getattr(event, "request_id", -1))
        try:
            duration_ms = float(event.duration_micros) / 1000.0
        except Exception:
            return
        command_name = str(getattr(event, "command_name", "") or "")
        record_mongo_operation(
            service="mongo_command_listener",
            collection=str(shape.get("collection") or ""),
            operation=command_name,
            elapsed_ms=duration_ms,
            success=True,
        )
        if duration_ms < self._SLOW_COMMAND_DURATION_MS:
            return
        if shape.get("skip_query_shape_telemetry"):
            return
        n_returned = extract_n_returned_from_reply(
            command_name,
            getattr(event, "reply", None),
        )
        record_mongo_command_observation(
            command_name=command_name,
            database=str(getattr(event, "database_name", "") or ""),
            command_shape=shape,
            duration_ms=duration_ms,
            request_id=getattr(event, "request_id", ""),
            n_returned=n_returned,
            source="command_listener_slow_success",
        )
        logger.warning(
            "[mongo_slow] cmd=%s db=%s coll=%s filter_shape=%s sort_shape=%s "
            "projection_shape=%s n_returned=%s duration_ms=%.1f request_id=%s",
            getattr(event, "command_name", ""),
            getattr(event, "database_name", ""),
            shape.get("collection", ""),
            shape.get("filter_shape", ""),
            shape.get("sort_shape", ""),
            shape.get("projection_shape", ""),
            n_returned,
            duration_ms,
            getattr(event, "request_id", ""),
        )

    def failed(self, event):
        shape = self._pop_context(getattr(event, "request_id", -1))
        try:
            duration_ms = float(event.duration_micros) / 1000.0
        except Exception:
            duration_ms = -1.0
        command_name = str(getattr(event, "command_name", "") or "")
        record_mongo_operation(
            service="mongo_command_listener",
            collection=str(shape.get("collection") or ""),
            operation=command_name,
            elapsed_ms=duration_ms if duration_ms >= 0 else 0.0,
            success=False,
            error_type=type(getattr(event, "failure", None)).__name__,
        )
        if not shape.get("skip_query_shape_telemetry"):
            record_mongo_command_observation(
                command_name=command_name,
                database=str(getattr(event, "database_name", "") or ""),
                command_shape=shape,
                duration_ms=duration_ms if duration_ms >= 0 else None,
                request_id=getattr(event, "request_id", ""),
                source="command_listener_failure",
            )
        logger.warning(
            "[mongo_command_failed] cmd=%s db=%s coll=%s filter_shape=%s sort_shape=%s "
            "projection_shape=%s duration_ms=%.1f request_id=%s failure_summary=%r",
            getattr(event, "command_name", ""),
            getattr(event, "database_name", ""),
            shape.get("collection", ""),
            shape.get("filter_shape", ""),
            shape.get("sort_shape", ""),
            shape.get("projection_shape", ""),
            duration_ms,
            getattr(event, "request_id", ""),
            _safe_mongo_failure_summary(getattr(event, "failure", "")),
        )


_VON_MONGO_LISTENER_REGISTERED = False


def _ensure_mongo_failure_listener_registered() -> None:
    global _VON_MONGO_LISTENER_REGISTERED
    if _VON_MONGO_LISTENER_REGISTERED:
        return
    try:
        monitoring.register(_VonMongoFailureLogger())
        _VON_MONGO_LISTENER_REGISTERED = True
        logger.info("[mongo_monitor] Registered structured failure listener.")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[mongo_monitor] Failed to register listener: %s", exc)


_ensure_mongo_failure_listener_registered()


def _debug_mongo_enabled() -> bool:
    return os.environ.get("VON_DEBUG_MONGO", "").lower() in {"1", "true", "yes", "on"}


def _redact_mongo_uri_for_log(uri: str) -> str:
    redacted = uri
    if "@" in redacted:
        prefix, rest = redacted.split("://", 1)
        if "@" in rest:
            creds, hostpart = rest.split("@", 1)
            user = creds.split(":", 1)[0] if ":" in creds else creds
            redacted = f"{prefix}://{user}:***@{hostpart}"
    return redacted


def _host_display_from_uri(uri: str) -> str:
    # Best-effort host display, safe for logs.
    redacted = _redact_mongo_uri_for_log(uri)
    if "@" in redacted:
        return redacted.split("@")[-1].split("/")[0]
    if "://" in redacted:
        return redacted.split("://", 1)[1].split("/")[0]
    return redacted.split("/")[0]


def is_mock_db_enabled() -> bool:
    """Return True if in-memory Mongo mocking is enabled.

    IMPORTANT: This reads the environment at call-time so tests can toggle it
    via monkeypatch without having to control import order.
    """

    return os.environ.get("VON_USE_MOCK_DB", "0").lower() in {"1", "true", "yes"}


# DEPRECATED: Prefer is_mock_db_enabled() which reads the environment at call-time.
# This module-level constant is retained for compatibility.
USE_MOCK_DB = is_mock_db_enabled()


def _get_mongomock_module():
    try:  # pragma: no cover - optional dependency import
        import mongomock as _mongomock

        return _mongomock
    except Exception as exc:  # pragma: no cover
        logger.warning("VON_USE_MOCK_DB set but mongomock import failed: %s", exc)
        return None


# Import colorama for colored console output
try:
    from colorama import Fore, Style, init

    # Initialize colorama for Windows compatibility
    init()
    COLORAMA_AVAILABLE = True
except ImportError:
    # Fallback if colorama is not installed
    COLORAMA_AVAILABLE = False

    class MockColor:
        BLUE = ""
        RESET_ALL = ""

    Fore = MockColor()
    Style = MockColor()

# REFACTORING_NOTE: This file is being updated to support a generalized entity model.
# The goal is to move away from a \'people\'-specific model to one that can handle
# various types of entities (types from vontology, and individuals of those types stored in DB).
# This involves adding new collections and accessors for \'entities\' and \'user_entity_tracking\'.

# --- Configuration ---
# Load environment variables from a .env file (if present) before reading MONGO_URI.
# This allows running the app without exporting env vars in the shell.
try:
    # Lazy import to avoid hard dependency if not installed
    from dotenv import load_dotenv, find_dotenv  # type: ignore

    # Prefer an explicit repo-root .env if available; otherwise rely on find_dotenv()
    _this_file = Path(__file__).resolve()
    _repo_root = (
        _this_file.parents[3] if len(_this_file.parents) >= 4 else _this_file.parent
    )
    _explicit_env = _repo_root / ".env"
    if _explicit_env.exists():
        load_dotenv(_explicit_env, override=False)
    else:
        load_dotenv(find_dotenv(), override=False)
except Exception:
    # Safe no-op if python-dotenv isn't installed or .env isn't found
    pass

# Use environment variables or default to localhost


def _get_nonempty_env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def _normalise_loopback_tunnel_endpoint(value: str) -> str:
    """Validate and normalise a loopback host:port tunnel endpoint.

    Tunnel-derived Mongo URIs deliberately relax TLS hostname matching because
    the client connects to loopback while the certificate names the remote
    Atlas member. Restricting these endpoints to loopback keeps that exception
    inside the authenticated SSH transport.
    """

    candidate = value.strip()
    parsed = urlsplit(f"//{candidate}")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint must contain a valid TCP port") from exc
    if (
        not host
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be a loopback host and TCP port")
    is_loopback = host.lower() == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise ValueError("endpoint must resolve syntactically to loopback")
    normalised_host = f"[{host}]" if ":" in host else host
    return f"{normalised_host}:{port}"


def _parse_ssh_tunnel_fallback_endpoints(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    endpoints: list[str] = []
    for index, value in enumerate(raw.split(","), start=1):
        if not value.strip():
            continue
        try:
            endpoint = _normalise_loopback_tunnel_endpoint(value)
        except ValueError as exc:
            logger.warning(
                "[mongo_fallback] Ignoring invalid SSH tunnel endpoint #%s: %s",
                index,
                exc,
            )
            continue
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    return tuple(endpoints)


MONGO_URI = (
    load_secret_from_env_or_file("MONGO_URI", "MONGO_URI_FILE")
    or _get_nonempty_env_value("MONGO_URI")
    or "mongodb://localhost:27017/"
)
# Optional fallback URI to use when SRV DNS resolution fails (useful offline)
MONGO_LOCAL_URI = (
    _get_nonempty_env_value("MONGO_LOCAL_URI")
    or "mongodb://127.0.0.1:27017/?directConnection=true"
)
MONGO_DNS_FALLBACK_URI = _get_nonempty_env_value("MONGO_DNS_FALLBACK_URI")
MONGO_ALLOW_LOCAL_FALLBACK = get_env_bool("MONGO_ALLOW_LOCAL_FALLBACK", False)
MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS = _parse_ssh_tunnel_fallback_endpoints(
    _get_nonempty_env_value("MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS")
)
MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET = _get_nonempty_env_value(
    "MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET"
)
MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS = _get_nonempty_env_value(
    "MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS"
)


def _parse_mongo_dns_resolver_fallback_nameservers(
    raw: str | None,
) -> tuple[str, ...]:
    """Return an ordered, literal-IP resolver list without accepting URLs."""

    if not raw:
        return ()
    nameservers: list[str] = []
    for index, value in enumerate(raw.split(","), start=1):
        candidate = value.strip()
        if not candidate:
            continue
        try:
            normalised = str(ipaddress.ip_address(candidate))
        except ValueError:
            logger.warning(
                "[mongo_dns_resolver_fallback] Ignoring invalid nameserver #%s; "
                "only literal IP addresses are accepted.",
                index,
            )
            continue
        if normalised not in nameservers:
            nameservers.append(normalised)
    return tuple(nameservers)


def _mongo_seed_hosts(uri: str | None) -> frozenset[str]:
    """Extract lower-cased seed hosts without retaining URI credentials."""

    if not uri:
        return frozenset()
    try:
        authority = urlsplit(uri).netloc.rsplit("@", 1)[-1]
    except (TypeError, ValueError):
        return frozenset()
    hosts: set[str] = set()
    for raw_seed in authority.split(","):
        seed = raw_seed.strip()
        if not seed:
            continue
        try:
            host = urlsplit(f"//{seed}").hostname
        except ValueError:
            continue
        if host:
            hosts.add(host.rstrip(".").lower())
    return frozenset(hosts)


_ORIGINAL_SOCKET_GETADDRINFO = socket.getaddrinfo
_MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS = (
    _parse_mongo_dns_resolver_fallback_nameservers(
        MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS
    )
)
_MONGO_DNS_RESOLVER_FALLBACK_HOSTS = _mongo_seed_hosts(MONGO_DNS_FALLBACK_URI)
_MONGO_DNS_RESOLVER_FALLBACK_LOGGED_HOSTS: set[str] = set()
_MONGO_DNS_RESOLVER_FALLBACK_LOG_LOCK = threading.Lock()
_MONGO_DNS_RESOLVER_FALLBACK_CACHE: dict[
    tuple[str, int], tuple[float, tuple[str, ...]]
] = {}
_MONGO_DNS_RESOLVER_FALLBACK_CACHE_LOCK = threading.Lock()
_MONGO_DNS_RESOLVER_FALLBACK_INSTALLED = False


@dataclass(frozen=True)
class _MongoFallbackTarget:
    uri: str
    kind: str
    label: str
    require_writable_primary: bool = False
    expected_replica_set: str | None = None


def _get_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _get_positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _mongo_auto_recovery_enabled() -> bool:
    return get_env_bool("VON_MONGO_AUTO_RECOVERY", True)


def _mongo_auto_recovery_check_interval_seconds() -> float:
    return _get_positive_float_env(
        "VON_MONGO_AUTO_RECOVERY_CHECK_INTERVAL_SECONDS", 15.0
    )


def _mongo_auto_recovery_ping_timeout_ms() -> int:
    return _get_positive_int_env("VON_MONGO_AUTO_RECOVERY_PING_TIMEOUT_MS", 1500)


def _mongo_dns_fallback_sticky_seconds() -> float:
    """Return the fallback retry window, retaining the legacy setting name."""

    generic_value = _get_nonempty_env_value("VON_MONGO_FALLBACK_STICKY_SECONDS")
    if generic_value is not None:
        try:
            parsed = float(generic_value)
        except ValueError:
            parsed = 300.0
        return parsed if parsed > 0 else 300.0
    return _get_positive_float_env("VON_MONGO_DNS_FALLBACK_STICKY_SECONDS", 300.0)


def _mongo_server_selection_timeout_ms() -> int:
    return _get_positive_int_env("MONGO_SERVER_SELECTION_TIMEOUT_MS", 5000)


def _mongo_connect_timeout_ms() -> int:
    return _get_positive_int_env("MONGO_CONNECT_TIMEOUT_MS", 5000)


def _mongo_dns_resolver_fallback_cache_seconds() -> float:
    return _get_positive_float_env("MONGO_DNS_RESOLVER_FALLBACK_CACHE_SECONDS", 60.0)


def _mongo_dns_resolver_fallback_prefer_direct() -> bool:
    return get_env_bool("MONGO_DNS_RESOLVER_FALLBACK_PREFER_DIRECT", False)


def _resolve_mongo_host_with_fallback_nameservers(host: str, family: int) -> list[str]:
    """Resolve one configured Mongo host through explicitly selected resolvers."""

    cache_key = (host, family)
    with _MONGO_DNS_RESOLVER_FALLBACK_CACHE_LOCK:
        now = time.monotonic()
        cached = _MONGO_DNS_RESOLVER_FALLBACK_CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return list(cached[1])

        try:
            import dns.exception
            import dns.resolver
        except ImportError:
            return []

        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = list(_MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS)
        resolver.timeout = _get_positive_float_env(
            "MONGO_DNS_RESOLVER_FALLBACK_TIMEOUT_SECONDS", 2.0
        )
        resolver.lifetime = resolver.timeout
        record_types: tuple[str, ...]
        if family == socket.AF_INET:
            record_types = ("A",)
        elif family == socket.AF_INET6:
            record_types = ("AAAA",)
        else:
            record_types = ("A", "AAAA")

        addresses: list[str] = []
        for record_type in record_types:
            try:
                answer = resolver.resolve(host, record_type, search=False)
            except dns.exception.DNSException:
                continue
            for item in answer:
                address = getattr(item, "address", None)
                if isinstance(address, str) and address not in addresses:
                    addresses.append(address)
        if addresses:
            _MONGO_DNS_RESOLVER_FALLBACK_CACHE[cache_key] = (
                now + _mongo_dns_resolver_fallback_cache_seconds(),
                tuple(addresses),
            )
        return addresses


def _mongo_getaddrinfo_with_configured_fallback(
    host,
    port,
    family=0,
    type=0,
    proto=0,
    flags=0,
):
    """Use explicit DNS only for configured Mongo seeds after libc DNS fails.

    PyMongo retains ``host`` as the TLS ``server_hostname`` after consuming
    these address records, so certificate hostname verification is unchanged.
    The fallback never applies to an unlisted host or before the system
    resolver has failed.
    """

    try:
        return _ORIGINAL_SOCKET_GETADDRINFO(host, port, family, type, proto, flags)
    except socket.gaierror:
        normalised_host = host.rstrip(".").lower() if isinstance(host, str) else None
        if (
            not normalised_host
            or normalised_host not in _MONGO_DNS_RESOLVER_FALLBACK_HOSTS
            or not _MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS
        ):
            raise
        addresses = _resolve_mongo_host_with_fallback_nameservers(
            normalised_host, family
        )

        resolved: list[tuple] = []
        for address in addresses:
            try:
                candidates = _ORIGINAL_SOCKET_GETADDRINFO(
                    address, port, family, type, proto, flags
                )
            except socket.gaierror:
                continue
            for candidate in candidates:
                if candidate not in resolved:
                    resolved.append(candidate)
        if not resolved:
            raise

        with _MONGO_DNS_RESOLVER_FALLBACK_LOG_LOCK:
            if normalised_host not in _MONGO_DNS_RESOLVER_FALLBACK_LOGGED_HOSTS:
                _MONGO_DNS_RESOLVER_FALLBACK_LOGGED_HOSTS.add(normalised_host)
                logger.warning(
                    "[mongo_dns_resolver_fallback] Resolved configured Mongo "
                    "seed host after system resolver failure: %s",
                    normalised_host,
                )
        return resolved


def _install_mongo_dns_resolver_fallback() -> bool:
    """Install the opt-in, host-scoped resolver fallback once per process."""

    global _MONGO_DNS_RESOLVER_FALLBACK_INSTALLED
    if _MONGO_DNS_RESOLVER_FALLBACK_INSTALLED:
        return True
    if (
        not _MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS
        or not _MONGO_DNS_RESOLVER_FALLBACK_HOSTS
    ):
        return False
    socket.getaddrinfo = _mongo_getaddrinfo_with_configured_fallback
    _MONGO_DNS_RESOLVER_FALLBACK_INSTALLED = True
    logger.info(
        "[mongo_dns_resolver_fallback] Enabled for %s configured Mongo seed "
        "host(s) using %s explicit resolver(s).",
        len(_MONGO_DNS_RESOLVER_FALLBACK_HOSTS),
        len(_MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS),
    )
    return True


_install_mongo_dns_resolver_fallback()


def _build_ssh_tunnel_mongo_uri(primary_uri: str, endpoint: str) -> str:
    """Derive a direct loopback Mongo URI without decoding URI credentials.

    The SSH process owns transport and authentication to the relay host. Mongo
    credentials remain sourced from ``MONGO_URI`` and are only carried in this
    in-memory derived URI. Certificate-chain validation stays enabled, while
    hostname validation is relaxed solely for the validated loopback endpoint.
    """

    endpoint = _normalise_loopback_tunnel_endpoint(endpoint)
    scheme_separator = primary_uri.find("://")
    if scheme_separator < 0:
        raise ValueError("primary Mongo URI has no recognised scheme")
    scheme = primary_uri[:scheme_separator].lower()
    if scheme not in {"mongodb", "mongodb+srv"}:
        raise ValueError("primary Mongo URI must use mongodb or mongodb+srv")

    remainder = primary_uri[scheme_separator + 3 :]
    tail_positions = [
        position
        for position in (remainder.find("/"), remainder.find("?"))
        if position >= 0
    ]
    tail_start = min(tail_positions) if tail_positions else len(remainder)
    authority = remainder[:tail_start]
    tail = remainder[tail_start:]
    userinfo_prefix = f"{authority.rsplit('@', 1)[0]}@" if "@" in authority else ""

    if tail.startswith("?"):
        path = "/"
        raw_query = tail[1:]
    else:
        path, separator, raw_query = tail.partition("?")
        if not separator:
            raw_query = ""
        path = path or "/"

    controlled_options = {
        "directconnection",
        "loadbalanced",
        "maxstalenessseconds",
        "readpreference",
        "readpreferencetags",
        "replicaset",
        "ssl",
        "srvmaxhosts",
        "srvservicename",
        "tls",
        "tlsallowinvalidcertificates",
        "tlsallowinvalidhostnames",
        "tlsinsecure",
    }
    options = [
        (key, value)
        for key, value in parse_qsl(raw_query, keep_blank_values=True)
        if key.lower() not in controlled_options
    ]
    if not any(key.lower() == "authsource" for key, _ in options):
        options.append(("authSource", "admin"))
    options.extend(
        [
            ("directConnection", "true"),
            ("tls", "true"),
            ("tlsAllowInvalidCertificates", "false"),
            ("tlsAllowInvalidHostnames", "true"),
        ]
    )
    return f"mongodb://{userinfo_prefix}{endpoint}{path}?{urlencode(options)}"


def _configured_replica_set_name() -> str | None:
    """Read the expected replica-set identity from existing Mongo URI options."""

    if MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET:
        return MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET
    for uri in (MONGO_DNS_FALLBACK_URI, MONGO_URI):
        if not uri or "?" not in uri:
            continue
        raw_query = uri.split("?", 1)[1]
        for key, value in parse_qsl(raw_query, keep_blank_values=True):
            if key.lower() == "replicaset" and value.strip():
                return value.strip()
    return None


def _connection_fallback_targets(
    *,
    prefer_dns: bool,
) -> list[_MongoFallbackTarget]:
    """Return configured recovery candidates in decreasing preference order."""

    tunnel_targets: list[_MongoFallbackTarget] = []
    expected_replica_set = _configured_replica_set_name()
    for endpoint in MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS:
        try:
            uri = _build_ssh_tunnel_mongo_uri(MONGO_URI, endpoint)
        except ValueError as exc:
            logger.warning(
                "[mongo_fallback] Cannot derive SSH tunnel Mongo URI: %s", exc
            )
            break
        tunnel_targets.append(
            _MongoFallbackTarget(
                uri=uri,
                kind="ssh_tunnel",
                label="SSH tunnel",
                require_writable_primary=True,
                expected_replica_set=expected_replica_set,
            )
        )
    dns_target = (
        _MongoFallbackTarget(
            uri=MONGO_DNS_FALLBACK_URI,
            kind="dns",
            label="direct-host Atlas",
        )
        if MONGO_DNS_FALLBACK_URI
        else None
    )
    targets: list[_MongoFallbackTarget] = []
    if prefer_dns and dns_target is not None:
        targets.append(dns_target)
    targets.extend(tunnel_targets)
    if not prefer_dns and dns_target is not None:
        targets.append(dns_target)
    if MONGO_ALLOW_LOCAL_FALLBACK:
        targets.append(
            _MongoFallbackTarget(
                uri=MONGO_LOCAL_URI,
                kind="local",
                label="local",
            )
        )
    return targets


def _resolve_connection_fallback_target(
    *,
    prefer_dns: bool,
) -> tuple[str, str] | None:
    """Compatibility wrapper returning the first configured fallback."""

    targets = _connection_fallback_targets(prefer_dns=prefer_dns)
    if not targets:
        return None
    target = targets[0]
    return (target.uri, f"{target.kind} fallback")


def _is_running_under_pytest() -> bool:
    # PYTEST_CURRENT_TEST is the most reliable indicator.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return "pytest" in sys.modules


def get_configured_database_name() -> str:
    """Return the currently configured Mongo database name.

    This reads the environment at call-time (not import-time).
    """

    name = os.environ.get("VON_DB_NAME")
    if not isinstance(name, str) or not name.strip():
        return "von_db"
    return name.strip()


# DEPRECATED: Prefer get_configured_database_name() which reads the environment at
# call-time. This module-level constant is retained for compatibility.
DATABASE_NAME = get_configured_database_name()


def assert_safe_database_name_for_pytest(db_name: str) -> None:
    """Fail fast if pytest is pointed at the production DB name."""

    if not _is_running_under_pytest():
        return
    if db_name == "von_db":
        raise RuntimeError(
            "Safety guard: pytest is configured to use VON_DB_NAME=von_db. "
            "Refusing to connect because tests may delete collections. "
            "Set VON_DB_NAME=test_von_db (recommended) before running pytest."
        )


def assert_destructive_db_operation_allowed(operation: str) -> None:
    """Block destructive DB operations on the production DB by default.

    Set VON_ALLOW_PROD_DESTRUCTIVE_DB_OPS=1 to override.
    """

    db_name = get_configured_database_name()
    if db_name != "von_db":
        return
    allow = os.environ.get("VON_ALLOW_PROD_DESTRUCTIVE_DB_OPS", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not allow:
        raise RuntimeError(
            f"Safety guard: refusing destructive DB operation '{operation}' on VON_DB_NAME=von_db. "
            "Set VON_ALLOW_PROD_DESTRUCTIVE_DB_OPS=1 to override (dangerous)."
        )


# REFACTORING_NOTE: Collection names defined as per the refactoring plan.
PEOPLE_COLLECTION_NAME = "people"  # To be phased out.
ENTITIES_COLLECTION_NAME = "entities"  # DEPRECATED: Use CONCEPTS_COLLECTION_NAME
CONCEPTS_COLLECTION_NAME = "concepts"
USER_ENTITY_TRACKING_COLLECTION_NAME = "user_entity_tracking"
INTERACTIONS_COLLECTION_NAME = "interactions"  # Existing, but schema will be updated.
INTERACTION_LOG_COLLECTION_NAME = "interaction_log"  # Existing, for auditing.
INTERACTION_SESSIONS_COLLECTION_NAME = "interaction_sessions"
MIGRATIONS_LOG_COLLECTION_NAME = (
    "migrations_log"  # New collection for tracking migrations
)
APPLICATION_SETTINGS_COLLECTION_NAME = (
    "application_settings"  # New collection for application-wide settings
)
TEXT_VALUES_COLLECTION_NAME = (
    "text_values"  # New collection for language-tagged text values
)
TEXT_RELATIONS_COLLECTION_NAME = (
    "text_relations"  # New collection linking concepts to text values
)
SCOPED_KNOWLEDGE_ASSERTIONS_COLLECTION_NAME = "scoped_knowledge_assertions"
ONTOLOGY_AUTHORITY_DELEGATIONS_COLLECTION_NAME = "ontology_authority_delegations"
ONTOLOGY_MUTATION_RECEIPTS_COLLECTION_NAME = "ontology_mutation_receipts"
ORGANISATION_MEMBERSHIP_RECEIPTS_COLLECTION_NAME = (
    "organisation_membership_mutation_receipts"
)
VON_LOGIN_EMAIL_RECEIPTS_COLLECTION_NAME = "von_login_email_mutation_receipts"
META_RELATIONS_COLLECTION_NAME = "meta_relations"  # New collection for relation elicitation meta-data (JVNAUTOSCI-371)
RELATIONSHIP_EXTENT_INDEX_COLLECTION_NAME = "relationship_extent_index"

# Agent Gmail OAuth token storage (JVNAUTOSCI-801)
AGENT_GMAIL_TOKENS_COLLECTION_NAME = "agent_gmail_tokens"

# Atomic, mailbox-wide outbound Gmail operational state.  These collections do
# not own Vontology-governed identity or authority.
GMAIL_OUTBOUND_QUOTA_COLLECTION_NAME = "gmail_outbound_quota"
GMAIL_OUTBOUND_DELIVERIES_COLLECTION_NAME = "gmail_outbound_deliveries"

# Room device identity (JVNAUTOSCI-884)
ROOM_DEVICES_COLLECTION_NAME = "room_devices"

# Server-side persistence for prompts waiting behind active chat turns.
CHAT_PROMPT_QUEUE_COLLECTION_NAME = "chat_prompt_queue"
CHAT_PROMPT_QUEUE_COUNTERS_COLLECTION_NAME = "chat_prompt_queue_counters"

# Restart-safe, actor-bound per-window organisation selections.
WINDOW_SESSION_BINDINGS_COLLECTION_NAME = "window_session_bindings"

# --- Client Initialization ---
# REFACTORING_NOTE: We maintain separate clients for real MongoDB vs mongomock.
# This prevents test suites from “poisoning” the process by enabling VON_USE_MOCK_DB
# in one test and accidentally forcing *all later tests* to keep using mongomock.
_mongo_client_real: MongoClient | None = None
_mongo_client_mock = None

# Track whether we are using a fallback URI rather than the primary MONGO_URI.
_using_fallback_real = False
_active_fallback_kind_real: str | None = None
_effective_uri_real = MONGO_URI  # The URI actually used to create the real client
_last_auto_recovery_check_at = 0.0
_dns_fallback_preferred_until_monotonic = 0.0
_dns_fallback_preference_reason: str | None = None
_preferred_fallback_kind: str | None = None


@dataclass(frozen=True)
class _MongoConnectionCandidate:
    """A verified client and its route state, not yet visible to callers."""

    client: MongoClient
    uri: str
    fallback_kind: str | None
    preference_reason: str | None = None
    clear_fallback_preference: bool = False


@dataclass
class _MongoConnectionWave:
    """Single-flight state for one invalidation generation."""

    done: bool = False
    result: MongoClient | None = None
    error: BaseException | None = None
    waiter_count: int = 0


_CONNECTION_CONDITION = threading.Condition()
_connection_generation = 0
_connection_waves: dict[int, _MongoConnectionWave] = {}
_HEALTH_CHECK_LOCK = threading.Lock()
_COLLECTION_INDEXES_LOCK = threading.Lock()
_COLLECTION_INDEXES_READY: set[tuple[int, str, str]] = set()


def _collection_index_ready_key(
    db: Database, collection_name: str
) -> tuple[int, str, str]:
    client = getattr(db, "client", None)
    client_identity = id(client) if client is not None else id(db)
    db_name = str(getattr(db, "name", get_configured_database_name()))
    return (client_identity, db_name, collection_name)


def _mark_collection_indexes_ready(db: Database, collection_name: str) -> None:
    _COLLECTION_INDEXES_READY.add(_collection_index_ready_key(db, collection_name))


def _collection_indexes_are_ready(db: Database, collection_name: str) -> bool:
    return _collection_index_ready_key(db, collection_name) in _COLLECTION_INDEXES_READY


def _new_mongo_client(
    uri: str, server_selection_timeout_ms: int | None = None
) -> MongoClient:
    """Create a MongoClient with bounded connection discovery.

    Server selection and connection establishment are liveness boundaries: no
    Mongo operation can begin until they succeed.  A global ``socketTimeoutMS``
    is different because it cancels every in-flight operation after the same
    arbitrary interval, including canonical reads that are still making useful
    progress.  Leave operation duration unbounded at the driver level and use
    the command/operation observers above to surface unusually slow work.
    """
    return MongoClient(
        uri,
        serverSelectionTimeoutMS=server_selection_timeout_ms
        or _mongo_server_selection_timeout_ms(),
        connectTimeoutMS=_mongo_connect_timeout_ms(),
        retryWrites=True,
        retryReads=True,
    )


def _dns_fallback_is_preferred() -> bool:
    return bool(
        MONGO_DNS_FALLBACK_URI
        and _dns_fallback_preferred_until_monotonic > time.monotonic()
        and _preferred_fallback_kind in {None, "dns"}
    )


def _fallback_is_preferred() -> bool:
    return bool(
        _preferred_fallback_kind
        and _dns_fallback_preferred_until_monotonic > time.monotonic()
    )


def _mark_fallback_preferred(kind: str, reason: str) -> None:
    """Prefer a successful fallback kind for a bounded recovery window."""

    global _dns_fallback_preferred_until_monotonic
    global _dns_fallback_preference_reason
    global _preferred_fallback_kind
    sticky_seconds = _mongo_dns_fallback_sticky_seconds()
    if sticky_seconds <= 0:
        _dns_fallback_preferred_until_monotonic = 0.0
        _dns_fallback_preference_reason = None
        _preferred_fallback_kind = None
        return
    _dns_fallback_preferred_until_monotonic = time.monotonic() + sticky_seconds
    _dns_fallback_preference_reason = reason
    _preferred_fallback_kind = kind


def _mark_dns_fallback_preferred(reason: str) -> None:
    """Prefer the configured direct-host Atlas fallback for a bounded window."""

    if MONGO_DNS_FALLBACK_URI:
        _mark_fallback_preferred("dns", reason)


def _clear_dns_fallback_preference() -> None:
    global _dns_fallback_preferred_until_monotonic
    global _dns_fallback_preference_reason
    global _preferred_fallback_kind
    _dns_fallback_preferred_until_monotonic = 0.0
    _dns_fallback_preference_reason = None
    _preferred_fallback_kind = None


def _prefer_alternate_mongo_route_locked(reason: str) -> str | None:
    current_kind = _active_fallback_kind_real if _using_fallback_real else None
    seen_kinds: set[str] = set()
    for target in _connection_fallback_targets(prefer_dns=False):
        if target.kind in seen_kinds or target.kind == current_kind:
            continue
        seen_kinds.add(target.kind)
        _mark_fallback_preferred(target.kind, reason)
        logger.warning(
            "[mongo_recovery] Marked current Mongo route degraded; next reconnect will try %s.",
            target.label,
        )
        return target.kind
    if current_kind is not None:
        _clear_dns_fallback_preference()
        logger.warning(
            "[mongo_recovery] No alternate fallback route is configured; "
            "next reconnect will probe the direct primary."
        )
    return None


def prefer_alternate_mongo_route(reason: str) -> str | None:
    """Prefer a different configured route after an operation transport failure.

    A route may still answer ``hello`` while real operations are timing out or
    being reset. This bounded hint lets the normal reconnect path try the next
    remote transport rather than repeatedly selecting the superficially
    healthy route.
    """

    with _CONNECTION_CONDITION:
        return _prefer_alternate_mongo_route_locked(reason)


def _try_connect_uri(
    uri: str,
    *,
    fallback_label: str | None = None,
    server_selection_timeout_ms: int | None = None,
    require_writable_primary: bool = False,
    expected_replica_set: str | None = None,
) -> MongoClient:
    client = _new_mongo_client(
        uri,
        server_selection_timeout_ms=server_selection_timeout_ms,
    )
    try:
        hello = client.admin.command("hello")
        if require_writable_primary and not bool(
            hello.get("isWritablePrimary", hello.get("ismaster", False))
        ):
            raise ConnectionFailure(
                "SSH tunnel endpoint reached a replica member that is not writable"
            )
        if expected_replica_set and hello.get("setName") != expected_replica_set:
            raise ConnectionFailure(
                "SSH tunnel endpoint reached an unexpected Mongo replica set"
            )
    except Exception:
        client.close()
        raise
    return client


def _fallback_candidate(
    target: _MongoFallbackTarget,
    *,
    preference_reason: str | None,
) -> _MongoConnectionCandidate:
    client = _try_connect_uri(
        target.uri,
        fallback_label=f"{target.kind} fallback",
        server_selection_timeout_ms=3000,
        require_writable_primary=target.require_writable_primary,
        expected_replica_set=target.expected_replica_set,
    )
    return _MongoConnectionCandidate(
        client=client,
        uri=target.uri,
        fallback_kind=target.kind,
        preference_reason=preference_reason,
    )


def _try_preferred_fallback_candidate() -> tuple[
    _MongoConnectionCandidate | None, bool
]:
    """Build, but do not publish, a client for the recent fallback route."""

    preferred_kind = _preferred_fallback_kind
    if preferred_kind is None and _dns_fallback_is_preferred():
        preferred_kind = "dns"
    if not preferred_kind or (
        _dns_fallback_preferred_until_monotonic <= time.monotonic()
    ):
        return None, False

    targets = [
        target
        for target in _connection_fallback_targets(prefer_dns=True)
        if target.kind == preferred_kind
    ]
    for target in targets:
        try:
            logger.warning(
                "[mongo_fallback] Preferring recent %s route during fallback recovery window.",
                target.label,
            )
            reason = (
                f"connected via {target.kind} fallback"
                if target.kind == "dns"
                else None
            )
            return (
                _fallback_candidate(target, preference_reason=reason),
                True,
            )
        except Exception as exc:
            logger.warning(
                "[mongo_fallback] Preferred %s connection failed: %s",
                target.label,
                exc,
            )
    return None, True


def _try_configured_fallback_candidate(
    *, reason: str, prefer_dns: bool
) -> _MongoConnectionCandidate | None:
    """Build an unpublished client from configured recovery routes."""

    for target in _connection_fallback_targets(prefer_dns=prefer_dns):
        logger.warning(
            "[mongo_fallback] Attempting %s MongoDB fallback due to %s.",
            target.label,
            reason,
        )
        try:
            candidate = _fallback_candidate(
                target,
                preference_reason=f"primary {reason}",
            )
        except Exception as exc:
            logger.warning(
                "[mongo_fallback] %s connection failed: %s",
                target.label,
                exc,
            )
            continue
        logger.warning(
            "[mongo_fallback] Using %s Mongo URI instead of primary.",
            target.label,
        )
        if _debug_mongo_enabled():
            logger.debug(
                "[MongoConnect] %s fallback host: %s",
                target.kind,
                _host_display_from_uri(target.uri),
            )
        return candidate
    return None


def _try_initial_direct_dns_candidate() -> _MongoConnectionCandidate | None:
    """Start on the direct route when its resolver recovery is explicit."""

    if not (
        _mongo_dns_resolver_fallback_prefer_direct()
        and _MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS
        and _MONGO_DNS_RESOLVER_FALLBACK_HOSTS
    ):
        return None
    target = next(
        (
            item
            for item in _connection_fallback_targets(prefer_dns=True)
            if item.kind == "dns"
        ),
        None,
    )
    if target is None:
        return None
    logger.warning(
        "[mongo_fallback] Preferring the configured direct-host Atlas route "
        "to avoid an unavailable primary SRV resolver."
    )
    try:
        return _fallback_candidate(
            target,
            preference_reason="explicit direct-host resolver recovery",
        )
    except Exception as exc:
        logger.warning(
            "[mongo_fallback] Initial direct-host Atlas connection failed: %s",
            exc,
        )
        return None


def _build_real_connection_candidate() -> tuple[_MongoConnectionCandidate | None, bool]:
    """Run one complete route-selection wave without publishing partial state."""

    preferred_candidate, preferred_failed = _try_preferred_fallback_candidate()
    if preferred_candidate is not None:
        return preferred_candidate, False

    initial_direct_candidate = _try_initial_direct_dns_candidate()
    if initial_direct_candidate is not None:
        return initial_direct_candidate, False

    try:
        client = _try_connect_uri(MONGO_URI)
        return (
            _MongoConnectionCandidate(
                client=client,
                uri=MONGO_URI,
                fallback_kind=None,
                clear_fallback_preference=True,
            ),
            False,
        )
    except ConnectionFailure as exc:
        logger.warning("Error connecting to MongoDB (ConnectionFailure): %s", exc)
        if "SSL" in str(exc) and "TLSV1_ALERT_INTERNAL_ERROR" in str(exc):
            logger.warning(
                "[VON_ASSISTANT_SUGGESTION] This appears to be an SSL/TLS handshake error. "
                "This commonly occurs when the current IP address is not whitelisted in MongoDB Atlas. "
                "IMPORTANT: IP whitelists are per-project, not per-organisation. "
                "Verify your IP is whitelisted in the correct project: '%s'. "
                "Steps: Atlas Console → Select correct Organisation → Select correct Project → Security → Network Access."
                " Your IP should be listed as Active in the project that contains your cluster.",
                os.environ.get("MONGO_PROJECT", "Unknown"),
            )
        is_ssl_error = (
            "SSL" in str(exc) or "10054" in str(exc) or "handshake" in str(exc).lower()
        )
        is_dns_error = any(
            marker in str(exc).lower()
            for marker in ("dns", "resolution", "name does not exist", "srv")
        )
        if _debug_mongo_enabled():
            logger.debug(
                "[MongoConnect] Connection failed. is_ssl_error=%s", is_ssl_error
            )
            logger.debug(
                "[MongoConnect] MONGO_ALLOW_LOCAL_FALLBACK=%s",
                MONGO_ALLOW_LOCAL_FALLBACK,
            )
            logger.debug(
                "[MongoConnect] MONGO_URI starts with mongodb+srv://: %s",
                MONGO_URI.startswith("mongodb+srv://"),
            )
        should_try_fallback = (
            bool(MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS)
            or bool(MONGO_DNS_FALLBACK_URI)
            or MONGO_ALLOW_LOCAL_FALLBACK
            or MONGO_URI.strip("\"'").startswith("mongodb+srv://")
            or is_ssl_error
        )
        candidate = (
            _try_configured_fallback_candidate(
                reason="connection failure",
                prefer_dns=is_dns_error and not is_ssl_error,
            )
            if should_try_fallback
            else None
        )
    except Exception as exc:
        logger.warning(
            "An unexpected error occurred during MongoDB client initialisation: %s",
            exc,
        )
        is_dns_error = any(
            marker in str(exc).lower()
            for marker in ("dns", "resolution", "name does not exist", "srv")
        )
        should_try_fallback = (
            bool(MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS)
            or bool(MONGO_DNS_FALLBACK_URI)
            or MONGO_ALLOW_LOCAL_FALLBACK
            or is_dns_error
        )
        candidate = (
            _try_configured_fallback_candidate(
                reason=("DNS/SRV error" if is_dns_error else "initialisation failure"),
                prefer_dns=is_dns_error,
            )
            if should_try_fallback
            else None
        )
    return candidate, bool(preferred_failed and candidate is None)


def _publish_connection_candidate_locked(candidate: _MongoConnectionCandidate) -> None:
    """Atomically expose a client together with the route that created it."""

    global _mongo_client_real, _effective_uri_real, _using_fallback_real
    global _active_fallback_kind_real, _last_auto_recovery_check_at
    _mongo_client_real = candidate.client
    _effective_uri_real = candidate.uri
    _using_fallback_real = candidate.fallback_kind is not None
    _active_fallback_kind_real = candidate.fallback_kind
    _last_auto_recovery_check_at = time.monotonic()
    if candidate.clear_fallback_preference:
        _clear_dns_fallback_preference()
    elif candidate.fallback_kind and candidate.preference_reason:
        _mark_fallback_preferred(
            candidate.fallback_kind,
            candidate.preference_reason,
        )


def _close_unpublished_candidate(candidate: _MongoConnectionCandidate) -> None:
    try:
        candidate.client.close()
    except Exception as exc:  # pragma: no cover - defensive cleanup only
        logger.warning(
            "[mongo_recovery] Failed to close stale Mongo candidate: %s", exc
        )


def _get_or_connect_real_client() -> MongoClient | None:
    """Return one shared result for each physical-client connection wave."""

    while True:
        with _CONNECTION_CONDITION:
            if _mongo_client_real is not None:
                return _mongo_client_real
            generation = _connection_generation
            wave = _connection_waves.get(generation)
            if wave is not None:
                wave.waiter_count += 1
                _CONNECTION_CONDITION.notify_all()
                while not wave.done and generation == _connection_generation:
                    _CONNECTION_CONDITION.wait()
                if generation != _connection_generation:
                    continue
                if wave.error is not None:
                    raise wave.error
                return wave.result
            wave = _MongoConnectionWave()
            _connection_waves[generation] = wave

        candidate: _MongoConnectionCandidate | None = None
        clear_preference_on_failure = False
        build_error: BaseException | None = None
        try:
            candidate, clear_preference_on_failure = _build_real_connection_candidate()
        except BaseException as exc:  # noqa: BLE001 - finalise cancellation waves
            if isinstance(exc, Exception):
                # Preserve the existing connection API: ordinary construction
                # failures yield no client. Control-flow exceptions propagate
                # after every waiter has been released.
                logger.warning(
                    "Unexpected error while building MongoDB connection candidate: %s",
                    exc,
                )
            else:
                build_error = exc
        stale_candidate: _MongoConnectionCandidate | None = None
        published = False
        with _CONNECTION_CONDITION:
            if generation == _connection_generation:
                if candidate is not None and _mongo_client_real is None:
                    _publish_connection_candidate_locked(candidate)
                    published = True
                elif candidate is not None:
                    stale_candidate = candidate
                elif clear_preference_on_failure:
                    _clear_dns_fallback_preference()
            elif candidate is not None:
                stale_candidate = candidate
            wave.result = _mongo_client_real
            wave.error = build_error
            wave.done = True
            if _connection_waves.get(generation) is wave:
                _connection_waves.pop(generation, None)
            _CONNECTION_CONDITION.notify_all()
            result = _mongo_client_real

        if stale_candidate is not None:
            _close_unpublished_candidate(stale_candidate)
        if build_error is not None:
            raise build_error
        if generation != _connection_generation:
            continue
        if published:
            print_connection_info()
        return result


def _claim_due_health_check() -> bool:
    """Coalesce periodic probes without touching the ordinary healthy fast path."""

    global _last_auto_recovery_check_at
    if not _mongo_auto_recovery_enabled():
        return False
    now = time.monotonic()
    if (
        now - _last_auto_recovery_check_at
        < _mongo_auto_recovery_check_interval_seconds()
    ):
        return False
    if not _HEALTH_CHECK_LOCK.acquire(blocking=False):
        return False
    now = time.monotonic()
    if (
        now - _last_auto_recovery_check_at
        < _mongo_auto_recovery_check_interval_seconds()
    ):
        _HEALTH_CHECK_LOCK.release()
        return False
    _last_auto_recovery_check_at = now
    return True


def _invalidate_real_client_for_recovery(
    reason: str,
    *,
    expected_client: MongoClient,
    expected_generation: int,
) -> bool:
    """CAS-invalidate a failed published client without closing it."""

    global _mongo_client_real, _effective_uri_real, _using_fallback_real
    global _active_fallback_kind_real, _connection_generation
    with _CONNECTION_CONDITION:
        if (
            _mongo_client_real is not expected_client
            or _connection_generation != expected_generation
        ):
            return False
        logger.warning("[mongo_recovery] Invalidating Mongo client: %s", reason)
        _mongo_client_real = None
        _connection_generation += 1
        if _fallback_is_preferred() and _active_fallback_kind_real:
            _using_fallback_real = True
        else:
            _effective_uri_real = MONGO_URI
            _using_fallback_real = False
            _active_fallback_kind_real = None
        _CONNECTION_CONDITION.notify_all()
        return True


def _health_probe_failure_is_driver_recovery_in_progress(exc: Exception) -> bool:
    """Return whether PyMongo is already cancelling or repairing this pool.

    ``_OperationCancelled`` is a private PyMongo control-flow error raised when
    a topology or pool is being shut down. ``connection pool paused`` is the
    driver's transient state while its monitor re-establishes the server. If a
    periodic advisory ping treats either as a new route failure, dropping the
    shared client can cancel every unrelated operation using that client and
    start a repeated reconnect cycle. Let PyMongo finish its own recovery and
    probe again at the normal interval instead.

    Ordinary application operations still classify these errors as transient;
    this narrow exception applies only to the periodic health probe deciding
    whether to replace the process-wide client.
    """

    return (
        type(exc).__name__ == "_OperationCancelled"
        or "connection pool paused" in str(exc).lower()
    )


def _ensure_active_client_is_healthy() -> None:
    if _mongo_client_real is None or not _claim_due_health_check():
        return
    try:
        with _CONNECTION_CONDITION:
            client = _mongo_client_real
            generation = _connection_generation
            using_fallback = _using_fallback_real
            active_fallback_kind = _active_fallback_kind_real
        if client is None:
            return
        timeout_ms = _mongo_auto_recovery_ping_timeout_ms()

        if using_fallback and not _fallback_is_preferred():
            try:
                primary_client = _try_connect_uri(
                    MONGO_URI,
                    server_selection_timeout_ms=3000,
                )
            except Exception as exc:
                with _CONNECTION_CONDITION:
                    if (
                        _mongo_client_real is client
                        and _connection_generation == generation
                        and active_fallback_kind
                    ):
                        _mark_fallback_preferred(
                            active_fallback_kind,
                            f"primary recovery probe failed: {type(exc).__name__}",
                        )
            else:
                candidate = _MongoConnectionCandidate(
                    client=primary_client,
                    uri=MONGO_URI,
                    fallback_kind=None,
                    clear_fallback_preference=True,
                )
                with _CONNECTION_CONDITION:
                    if (
                        _mongo_client_real is client
                        and _connection_generation == generation
                    ):
                        _publish_connection_candidate_locked(candidate)
                        logger.info(
                            "[mongo_recovery] Direct primary Mongo route recovered; leaving fallback."
                        )
                        return
                _close_unpublished_candidate(candidate)
                return

        with _CONNECTION_CONDITION:
            if _mongo_client_real is not client or _connection_generation != generation:
                return
        try:
            tunnel_member_reselection_required = False
            if active_fallback_kind == "ssh_tunnel":
                hello = client.admin.command("hello", maxTimeMS=timeout_ms)
                if not bool(
                    hello.get("isWritablePrimary", hello.get("ismaster", False))
                ):
                    tunnel_member_reselection_required = True
                    raise ConnectionFailure(
                        "active SSH tunnel member is no longer writable"
                    )
                expected_replica_set = _configured_replica_set_name()
                if (
                    expected_replica_set
                    and hello.get("setName") != expected_replica_set
                ):
                    raise ConnectionFailure(
                        "active SSH tunnel member has unexpected replica-set identity"
                    )
            else:
                client.admin.command("ping", maxTimeMS=timeout_ms)
        except Exception as exc:
            if _health_probe_failure_is_driver_recovery_in_progress(exc):
                logger.warning(
                    "[mongo_recovery] Periodic ping deferred while PyMongo "
                    "recovers the active pool: %s",
                    exc,
                )
                return
            with _CONNECTION_CONDITION:
                still_current = (
                    _mongo_client_real is client
                    and _connection_generation == generation
                )
                if (
                    still_current
                    and not tunnel_member_reselection_required
                    and is_mongo_transport_error(exc)
                ):
                    prefer_alternate_mongo_route(
                        f"periodic health check {type(exc).__name__}"
                    )
            _invalidate_real_client_for_recovery(
                f"periodic ping failed: {exc}",
                expected_client=client,
                expected_generation=generation,
            )
    finally:
        _HEALTH_CHECK_LOCK.release()


def get_db() -> Database | None:
    """
    Establishes a connection to the MongoDB database if one doesn't exist,
    and returns the database instance.
    """
    global _mongo_client_mock
    db_name = get_configured_database_name()
    assert_safe_database_name_for_pytest(db_name)

    if is_mock_db_enabled():
        if _mongo_client_mock is None:
            mongomock = _get_mongomock_module()
            if mongomock is None:
                raise RuntimeError(
                    "VON_USE_MOCK_DB is enabled but mongomock is unavailable"
                )
            _mongo_client_mock = mongomock.MongoClient()
        return _mongo_client_mock[db_name]

    client = _mongo_client_real
    if client is not None:
        _ensure_active_client_is_healthy()
        client = _mongo_client_real
    if client is None:
        client = _get_or_connect_real_client()
    if client is not None:
        return client[db_name]
    return None


def print_connection_info():
    """Log helpful connection info on startup."""
    global _effective_uri_real, _active_fallback_kind_real
    uri_to_use = _effective_uri_real if _effective_uri_real else MONGO_URI
    host = _host_display_from_uri(uri_to_use)
    if _active_fallback_kind_real == "ssh_tunnel":
        logger.info(
            "[Von Database] Connecting to REMOTE MongoDB through SSH tunnel at %s",
            host,
        )
    elif "localhost" in host or "127.0.0.1" in host:
        logger.info("[Von Database] Connecting to LOCAL MongoDB at %s", host)
    else:
        logger.info("[Von Database] Connecting to REMOTE MongoDB at %s", host)


def get_effective_mongo_uri() -> str:
    """Return the URI that was effectively used to create the client (may be fallback)."""
    with _CONNECTION_CONDITION:
        return _effective_uri_real


def is_using_fallback_uri() -> bool:
    """Return True when any fallback URI is used instead of primary MONGO_URI."""
    with _CONNECTION_CONDITION:
        return _using_fallback_real


def get_mongo_fallback_policy_state() -> dict[str, object]:
    """Return paste-safe fallback/recovery state for operator diagnostics."""

    with _CONNECTION_CONDITION:
        remaining_seconds = max(
            0.0, _dns_fallback_preferred_until_monotonic - time.monotonic()
        )
        return {
            "active_fallback_kind": _active_fallback_kind_real,
            "preferred_fallback_kind": _preferred_fallback_kind,
            "fallback_sticky": bool(remaining_seconds > 0),
            "fallback_sticky_seconds_remaining": round(remaining_seconds, 3),
            "fallback_sticky_seconds_configured": _mongo_dns_fallback_sticky_seconds(),
            "fallback_preference_reason": _dns_fallback_preference_reason,
            "ssh_tunnel_fallback_configured": bool(MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS),
            "ssh_tunnel_endpoint_count": len(MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS),
            "ssh_tunnel_expected_replica_set_configured": bool(
                _configured_replica_set_name()
            ),
            "dns_fallback_configured": bool(MONGO_DNS_FALLBACK_URI),
            "dns_resolver_fallback_configured": bool(
                _MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS
                and _MONGO_DNS_RESOLVER_FALLBACK_HOSTS
            ),
            "dns_resolver_fallback_installed": (_MONGO_DNS_RESOLVER_FALLBACK_INSTALLED),
            "dns_resolver_fallback_nameserver_count": len(
                _MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS
            ),
            "dns_resolver_fallback_host_count": len(_MONGO_DNS_RESOLVER_FALLBACK_HOSTS),
            "dns_resolver_fallback_cache_seconds": (
                _mongo_dns_resolver_fallback_cache_seconds()
            ),
            "dns_resolver_fallback_prefer_direct": (
                _mongo_dns_resolver_fallback_prefer_direct()
            ),
            "dns_fallback_sticky": bool(
                remaining_seconds > 0 and _preferred_fallback_kind in {None, "dns"}
            ),
            "dns_fallback_sticky_seconds_remaining": (
                round(remaining_seconds, 3)
                if _preferred_fallback_kind in {None, "dns"}
                else 0.0
            ),
            "dns_fallback_sticky_seconds_configured": (
                _mongo_dns_fallback_sticky_seconds()
            ),
            "dns_fallback_preference_reason": (
                _dns_fallback_preference_reason
                if _preferred_fallback_kind in {None, "dns"}
                else None
            ),
            "local_fallback_allowed": MONGO_ALLOW_LOCAL_FALLBACK,
        }


def close_connection():
    """Closes the MongoDB connection."""
    global _mongo_client_real, _mongo_client_mock, _last_auto_recovery_check_at
    global _using_fallback_real, _effective_uri_real, _active_fallback_kind_real
    global _connection_generation
    with _CONNECTION_CONDITION:
        client_to_close = _mongo_client_real
        _mongo_client_real = None
        # mongomock doesn't require close(), but clear ref for correctness.
        _mongo_client_mock = None
        _using_fallback_real = False
        _effective_uri_real = MONGO_URI
        _active_fallback_kind_real = None
        _last_auto_recovery_check_at = 0.0
        _connection_generation += 1
        _clear_dns_fallback_preference()
        _CONNECTION_CONDITION.notify_all()
    if client_to_close is not None:
        client_to_close.close()
    with _COLLECTION_INDEXES_LOCK:
        _COLLECTION_INDEXES_READY.clear()


def invalidate_connection(*, prefer_alternate_reason: str | None = None):
    """Invalidate the MongoDB connection reference without calling close().

    This allows get_db() to create a fresh client on next call while avoiding
    a race condition where in-flight requests holding the old client reference
    would fail with 'Cannot use MongoClient after close'.

    When ``prefer_alternate_reason`` is supplied, selecting the alternate route
    and advancing the connection generation are one atomic operation. This
    fences an in-flight builder from publishing its old route and clearing the
    newly selected recovery route before invalidation completes.

    PyMongo's connection pool handles stale connections gracefully, so we can
    simply drop our reference and let GC clean up.
    """
    global _mongo_client_real, _mongo_client_mock, _last_auto_recovery_check_at
    global _using_fallback_real, _effective_uri_real, _active_fallback_kind_real
    global _connection_generation
    with _CONNECTION_CONDITION:
        if prefer_alternate_reason is not None:
            _prefer_alternate_mongo_route_locked(prefer_alternate_reason)
        _mongo_client_real = None
        _mongo_client_mock = None
        _using_fallback_real = False
        _effective_uri_real = MONGO_URI
        _active_fallback_kind_real = None
        _last_auto_recovery_check_at = 0.0
        _connection_generation += 1
        _CONNECTION_CONDITION.notify_all()
    with _COLLECTION_INDEXES_LOCK:
        _COLLECTION_INDEXES_READY.clear()


def test_connection(verbose: bool = False) -> bool:
    """Test MongoDB connection and return True if successful, False otherwise."""
    db = get_db()
    if db is None:
        if verbose:
            logger.warning("MongoDB client is not initialised or connection failed.")
        return False

    try:
        # Test the connection with a simple ping
        db.command("ping")
        if verbose:
            logger.info("MongoDB connection successful.")
        return True
    except ConnectionFailure as e:
        if verbose:
            logger.warning("MongoDB connection failed (ConnectionFailure): %s", e)
        return False
    except Exception as e:
        if verbose:
            logger.warning("MongoDB connection test failed: %s", e)
        return False


def _ensure_collection_indexes_once(
    db: Database,
    collection_name: str,
    ensure_indexes,
) -> Collection:
    """Ensure collection indexes once per client/database pair.

    Collection accessors sit on hot Vontology paths. Re-checking indexes on each
    access causes repeated metadata round-trips and can stall request startup.
    Fail open after the first attempt so reads can proceed even if index
    verification is temporarily unavailable.
    """

    coll = db[collection_name]
    if _collection_indexes_are_ready(db, collection_name):
        return coll

    with _COLLECTION_INDEXES_LOCK:
        if _collection_indexes_are_ready(db, collection_name):
            return coll
        try:
            ensure_indexes(coll)
        except OperationFailure as e:
            logger.warning(
                "Could not create some indexes for %s: %s", collection_name, e
            )
        except Exception as e:
            logger.warning("Index creation skipped for %s: %s", collection_name, e)
        _mark_collection_indexes_ready(db, collection_name)
    return coll


def _ensure_concepts_collection_indexes(concepts_coll: Collection) -> None:
    existing_indexes = {idx["name"] for idx in concepts_coll.list_indexes()}

    if (
        "concept_id_1_unique" not in existing_indexes
        and "concept_id_unique" not in existing_indexes
    ):
        concepts_coll.create_index(
            [("concept_id", ASCENDING)], unique=True, name="concept_id_1_unique"
        )

    if "relationships.is_a_type_of_1" not in existing_indexes:
        concepts_coll.create_index([("relationships.is_a_type_of", ASCENDING)])
    if "relationships.has_subtype_1" not in existing_indexes:
        concepts_coll.create_index([("relationships.has_subtype", ASCENDING)])
    if "relationships.is_an_instance_of_1" not in existing_indexes:
        concepts_coll.create_index([("relationships.is_an_instance_of", ASCENDING)])
    if "relationships.has_instance_1" not in existing_indexes:
        concepts_coll.create_index([("relationships.has_instance", ASCENDING)])
    if "relationships.related_to_1" not in existing_indexes:
        concepts_coll.create_index([("relationships.related_to", ASCENDING)])
    if "attributes_mcp_tool_name_1" not in existing_indexes:
        concepts_coll.create_index(
            [("attributes.mcp_tool_name", ASCENDING)],
            name="attributes_mcp_tool_name_1",
        )
    if "relationships_v_has_initial_step_camel_1" not in existing_indexes:
        concepts_coll.create_index(
            [("relationships.#V#hasInitialStep", ASCENDING)],
            name="relationships_v_has_initial_step_camel_1",
        )
    if "relationships_hasInitialStep_1" not in existing_indexes:
        concepts_coll.create_index(
            [("relationships.hasInitialStep", ASCENDING)],
            name="relationships_hasInitialStep_1",
        )
    if "relationships_v_has_initial_step_snake_1" not in existing_indexes:
        concepts_coll.create_index(
            [("relationships.#V#has_initial_step", ASCENDING)],
            name="relationships_v_has_initial_step_snake_1",
        )
    if "relationships_has_initial_step_1" not in existing_indexes:
        concepts_coll.create_index(
            [("relationships.has_initial_step", ASCENDING)],
            name="relationships_has_initial_step_1",
        )
    if "relationships_v_evidence_view_applies_to_tool_1" not in existing_indexes:
        concepts_coll.create_index(
            [("relationships.#V#evidence_view_applies_to_tool", ASCENDING)],
            name="relationships_v_evidence_view_applies_to_tool_1",
        )
    if "episode_critique_remediation_external_instance_lookup" not in existing_indexes:
        concepts_coll.create_index(
            [
                (
                    "metadata.external_references.episode_critique_remediation.external_id",
                    ASCENDING,
                ),
                ("relationships.is_an_instance_of", ASCENDING),
            ],
            name="episode_critique_remediation_external_instance_lookup",
        )

    if "metadata.concept_type_1" in existing_indexes:
        try:
            concepts_coll.drop_index("metadata.concept_type_1")
        except Exception as _e:
            logger.warning("Unable to drop retired metadata.concept_type index: %s", _e)
    if "name_text" not in existing_indexes:
        try:
            concepts_coll.create_index(
                [("name", "text")], name="name_text", default_language="none"
            )
        except Exception as _e:
            logger.warning("Unable to create text index on name: %s", _e)
    if (
        "legacy_name_exact_1" not in existing_indexes
        and "name_1" not in existing_indexes
    ):
        concepts_coll.create_index(
            [("name", ASCENDING)],
            name="legacy_name_exact_1",
            sparse=True,
        )
    if "names.name_1" not in existing_indexes:
        concepts_coll.create_index([("names.name", ASCENDING)], name="names.name_1")
    if "names.text_1" not in existing_indexes:
        concepts_coll.create_index([("names.text", ASCENDING)], name="names.text_1")
    if "task_jira_external_id_lookup" not in existing_indexes:
        concepts_coll.create_index(
            [
                ("relationships.is_an_instance_of", ASCENDING),
                ("metadata.external_references.jira.external_id", ASCENDING),
            ],
            name="task_jira_external_id_lookup",
        )
    if "task_jira_external_id_org_lookup" not in existing_indexes:
        concepts_coll.create_index(
            [
                ("relationships.is_an_instance_of", ASCENDING),
                ("metadata.organisation_concept_id", ASCENDING),
                ("metadata.external_references.jira.external_id", ASCENDING),
            ],
            name="task_jira_external_id_org_lookup",
        )
    if "timestamps.created_at_-1" not in existing_indexes:
        concepts_coll.create_index([("timestamps.created_at", DESCENDING)])
    if "timestamps.updated_at_-1" not in existing_indexes:
        concepts_coll.create_index([("timestamps.updated_at", DESCENDING)])
    if "embedding_status_updated_at_desc" not in existing_indexes:
        concepts_coll.create_index(
            [("embedding_status", ASCENDING), ("updated_at", DESCENDING)],
            name="embedding_status_updated_at_desc",
        )
    if "direct_message_idempotency_scope_unique" not in existing_indexes:
        concepts_coll.create_index(
            [
                (
                    "concept_data.metadata.delivery_idempotency_scope",
                    ASCENDING,
                )
            ],
            name="direct_message_idempotency_scope_unique",
            unique=True,
            partialFilterExpression={
                "concept_data.metadata.delivery_idempotency_scope": {
                    "$exists": True,
                    "$type": "string",
                }
            },
        )
    if "updated_at_-1" not in existing_indexes:
        concepts_coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")


def _ensure_text_values_indexes(coll: Collection) -> None:
    coll.create_index([("text", "text")], name="text_text_search")
    coll.create_index([("text", ASCENDING)], name="text_1")
    coll.create_index([("lang", ASCENDING)], name="lang_1")
    coll.create_index(
        [("fingerprint", ASCENDING), ("lang", ASCENDING)],
        name="fingerprint_lang_unique",
        unique=True,
        partialFilterExpression={"fingerprint": {"$exists": True, "$type": "string"}},
    )
    coll.create_index([("created_at", DESCENDING)], name="created_at_-1")
    coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")


def _ensure_interaction_sessions_indexes(coll: Collection) -> None:
    coll.create_index([("indexing_status", ASCENDING)], name="indexing_status_1")
    coll.create_index(
        [("indexing_status", ASCENDING), ("indexed_at", DESCENDING)],
        name="indexing_status_indexed_at_desc",
    )


def _ensure_text_relations_indexes(coll: Collection) -> None:
    coll.create_index([("subject_concept_id", ASCENDING)], name="subject_concept_id_1")
    coll.create_index([("predicate", ASCENDING)], name="predicate_1")
    coll.create_index([("object_text_id", ASCENDING)], name="object_text_id_1")
    coll.create_index(
        [
            ("subject_concept_id", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="subject_concept_id_id",
    )
    coll.create_index(
        [
            ("predicate", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="predicate_id",
    )
    coll.create_index(
        [
            ("updated_at", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="updated_at_id",
    )
    coll.create_index(
        [
            ("object_text_id", ASCENDING),
            ("predicate", ASCENDING),
            ("subject_concept_id", ASCENDING),
        ],
        name="object_predicate_subject_lookup",
    )
    coll.create_index(
        [("predicate", ASCENDING), ("created_at", DESCENDING)],
        name="predicate_created_at_desc",
    )
    coll.create_index(
        [("predicate", ASCENDING), ("updated_at", DESCENDING)],
        name="predicate_updated_at_desc",
    )
    coll.create_index(
        [
            ("subject_concept_id", ASCENDING),
            ("predicate", ASCENDING),
            ("object_text_id", ASCENDING),
        ],
        name="subject_predicate_object_unique",
        unique=True,
    )
    coll.create_index(
        [
            ("predicate", ASCENDING),
            ("context.next_run_epoch_ms", ASCENDING),
            ("subject_concept_id", ASCENDING),
        ],
        name="schedule_due_lookup",
        partialFilterExpression={
            "predicate": "#V#next_run_scheduled_for",
            "context.next_run_epoch_ms": {"$exists": True},
        },
    )
    coll.create_index(
        [
            ("context.name_type", ASCENDING),
            ("predicate", ASCENDING),
            ("text", ASCENDING),
        ],
        name="context_name_type_predicate_text",
    )
    coll.create_index([("created_at", DESCENDING)], name="created_at_-1")
    coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")


def _ensure_scoped_knowledge_assertions_indexes(coll: Collection) -> None:
    """Ensure actor-scoped assertions stay bounded and cheaply retrievable."""

    coll.create_index(
        [("assertion_id", ASCENDING)],
        name="assertion_id_unique",
        unique=True,
    )
    coll.create_index(
        [
            ("scope.audience_keys", ASCENDING),
            ("status", ASCENDING),
            ("updated_at", DESCENDING),
            ("assertion_id", ASCENDING),
        ],
        name="audience_status_updated_at_desc_assertion_id",
    )
    coll.create_index(
        [
            ("subject_concept_id", ASCENDING),
            ("scope.audience_keys", ASCENDING),
            ("status", ASCENDING),
            ("updated_at", DESCENDING),
            ("assertion_id", ASCENDING),
        ],
        name="subject_audience_status_updated_at_desc_assertion_id",
    )
    coll.create_index(
        [
            ("object_concept_id", ASCENDING),
            ("scope.audience_keys", ASCENDING),
            ("status", ASCENDING),
            ("updated_at", DESCENDING),
            ("assertion_id", ASCENDING),
        ],
        name="object_audience_status_updated_at_desc_assertion_id",
    )
    coll.create_index(
        [
            ("predicate", ASCENDING),
            ("scope.audience_keys", ASCENDING),
            ("status", ASCENDING),
            ("updated_at", DESCENDING),
            ("assertion_id", ASCENDING),
        ],
        name="predicate_audience_status_updated_at_desc_assertion_id",
    )
    coll.create_index(
        [
            ("scope.audience_keys", ASCENDING),
            ("status", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="audience_status_id",
    )
    coll.create_index(
        [
            ("subject_concept_id", ASCENDING),
            ("scope.audience_keys", ASCENDING),
            ("status", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="subject_audience_status_id",
    )
    coll.create_index(
        [
            ("predicate", ASCENDING),
            ("scope.audience_keys", ASCENDING),
            ("status", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="predicate_audience_status_id",
    )
    # concept_links and scope.audience_keys are both arrays, so a compound
    # index across them would be an invalid parallel-array index. New writes
    # also materialise the one-valued scope.audience_key for this lookup.
    coll.create_index(
        [
            ("concept_links.concept_id", ASCENDING),
            ("scope.audience_key", ASCENDING),
            ("status", ASCENDING),
            ("updated_at", DESCENDING),
            ("assertion_id", ASCENDING),
        ],
        name="concept_link_audience_status_updated_at_desc_assertion_id",
    )
    coll.create_index(
        [
            ("assertion_form", ASCENDING),
            ("object_kind", ASCENDING),
            ("status", ASCENDING),
            ("rag_index.desired_operation", ASCENDING),
            ("rag_index.status", ASCENDING),
            ("rag_index.next_attempt_at", ASCENDING),
            ("rag_index.lease_expires_at", ASCENDING),
            ("updated_at", ASCENDING),
        ],
        name="standalone_rag_queue_status_due_lease_updated_at",
    )


def _ensure_meta_relations_indexes(coll: Collection) -> None:
    existing_indexes = {idx["name"] for idx in coll.list_indexes()}
    if "type_1_subject_type_id_1" not in existing_indexes:
        coll.create_index(
            [("type", ASCENDING), ("subject_type_id", ASCENDING)],
            name="type_1_subject_type_id_1",
        )
    if "type_1_predicate_id_1" not in existing_indexes:
        coll.create_index(
            [("type", ASCENDING), ("predicate_id", ASCENDING)],
            name="type_1_predicate_id_1",
        )
    if "updated_at_-1" not in existing_indexes:
        coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")


def _ensure_ontology_authority_delegation_indexes(coll: Collection) -> None:
    """Ensure short-lived semantic-authority grants remain exact and auditable."""

    coll.create_index(
        [("delegation_id", ASCENDING)],
        name="delegation_id_unique",
        unique=True,
    )
    coll.create_index(
        [
            ("grantor_actor_concept_id", ASCENDING),
            ("status", ASCENDING),
            ("expires_at", ASCENDING),
        ],
        name="grantor_status_expiry",
    )
    coll.create_index(
        [
            ("delegate_concept_id", ASCENDING),
            ("audience", ASCENDING),
            ("status", ASCENDING),
            ("expires_at", ASCENDING),
        ],
        name="delegate_audience_status_expiry",
    )
    coll.create_index([("issued_at", DESCENDING)], name="issued_at_desc")


def _ensure_ontology_mutation_receipt_indexes(coll: Collection) -> None:
    """Ensure mutation decisions and canonical read-backs are retrievable."""

    coll.create_index(
        [("receipt_id", ASCENDING)],
        name="receipt_id_unique",
        unique=True,
    )
    # The first candidate indexed key+fingerprint, which allowed one actor to
    # reuse a key for a different effect and accidentally made identical keys
    # collide across actors. This collection is introduced with the authority
    # feature, so replace that unreleased shape with an actor-bound claim.
    existing_indexes = coll.index_information()
    if "idempotency_intent_unique" in existing_indexes:
        coll.drop_index("idempotency_intent_unique")
    coll.create_index(
        [
            ("actor_concept_id", ASCENDING),
            ("idempotency_key", ASCENDING),
        ],
        name="actor_idempotency_unique",
        unique=True,
        partialFilterExpression={
            "idempotency_key": {"$exists": True, "$type": "string"},
        },
    )
    coll.create_index(
        [
            ("actor_concept_id", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="actor_created_at_desc",
    )
    coll.create_index(
        [
            ("publication_context.kind", ASCENDING),
            ("publication_context.concept_id", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="publication_context_created_at_desc",
    )
    coll.create_index([("status", ASCENDING)], name="status_1")
    coll.create_index([("updated_at", DESCENDING)], name="updated_at_desc")


def _ensure_organisation_membership_receipt_indexes(coll: Collection) -> None:
    """Keep operational membership effects actor-bound and replayable."""

    coll.create_index(
        [("receipt_id", ASCENDING)],
        name="receipt_id_unique",
        unique=True,
    )
    coll.create_index(
        [
            ("actor_concept_id", ASCENDING),
            ("request_id", ASCENDING),
        ],
        name="actor_request_unique",
        unique=True,
    )
    coll.create_index(
        [
            ("actor_concept_id", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="actor_created_at_desc",
    )
    coll.create_index(
        [
            ("organisation_concept_id", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="organisation_created_at_desc",
    )
    coll.create_index([("status", ASCENDING)], name="status_1")


def _ensure_von_login_email_receipt_indexes(coll: Collection) -> None:
    """Keep authentication-identity effects actor-bound and replayable."""

    coll.create_index(
        [("receipt_id", ASCENDING)],
        name="receipt_id_unique",
        unique=True,
    )
    coll.create_index(
        [
            ("actor_concept_id", ASCENDING),
            ("request_id", ASCENDING),
        ],
        name="actor_request_unique",
        unique=True,
    )
    coll.create_index(
        [
            ("actor_concept_id", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="actor_created_at_desc",
    )
    coll.create_index(
        [
            ("organisation_concept_id", ASCENDING),
            ("created_at", DESCENDING),
        ],
        name="organisation_created_at_desc",
    )
    coll.create_index([("status", ASCENDING)], name="status_1")


def _ensure_relationship_extent_index_indexes(coll: Collection) -> None:
    existing = [idx for idx in coll.list_indexes() if isinstance(idx, Mapping)]
    existing_names = {str(idx.get("name") or "") for idx in existing if idx.get("name")}
    if "relation_id_1_unique" not in existing_names:
        coll.create_index(
            [("relation_id", ASCENDING)],
            name="relation_id_1_unique",
            unique=True,
        )

    target_first_keys = [
        ("target_value", ASCENDING),
        ("predicate_id", ASCENDING),
        ("source_concept_id", ASCENDING),
    ]
    target_first_exists = any(
        isinstance(index.get("key"), Mapping)
        and list(index["key"].items()) == target_first_keys
        for index in existing
    )
    if not target_first_exists:
        # A historical deployment can have the old canonical name attached to
        # predicate-first keys. Do not drop an index in this lazy accessor:
        # additive creation keeps the old protection available if Atlas fails
        # or stalls while building the target-first lookup.
        coll.create_index(
            target_first_keys,
            name="target_value_predicate_source_lookup_v2",
        )

    if "predicate_source_target_lookup" not in existing_names:
        coll.create_index(
            [
                ("predicate_id", ASCENDING),
                ("source_concept_id", ASCENDING),
                ("target_value", ASCENDING),
            ],
            name="predicate_source_target_lookup",
        )
    if "source_concept_id_1" not in existing_names:
        coll.create_index(
            [("source_concept_id", ASCENDING)], name="source_concept_id_1"
        )
    if "updated_at_-1" not in existing_names:
        coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")


def _ensure_chat_prompt_queue_indexes(coll: Collection) -> None:
    existing_indexes = {idx["name"] for idx in coll.list_indexes()}
    if "status_cancellation_requested_id" not in existing_indexes:
        # Recovery polls even when no cancellation is pending. Keep that empty
        # lookup off the full queue while preserving oldest-request ordering.
        coll.create_index(
            [
                ("status", ASCENDING),
                ("cancellation_requested_at", ASCENDING),
                ("_id", ASCENDING),
            ],
            name="status_cancellation_requested_id",
            partialFilterExpression={"cancellation_requested_at": {"$type": "date"}},
        )
    if "queue_id_1_unique" not in existing_indexes:
        coll.create_index(
            [("queue_id", ASCENDING)], name="queue_id_1_unique", unique=True
        )
    if "scope_status_enqueue_sequence" not in existing_indexes:
        coll.create_index(
            [
                ("user_concept_id", ASCENDING),
                ("organisation_concept_id", ASCENDING),
                ("namespace", ASCENDING),
                ("status", ASCENDING),
                ("enqueue_sequence", ASCENDING),
                ("created_at", ASCENDING),
                ("queue_id", ASCENDING),
            ],
            name="scope_status_enqueue_sequence",
        )
    if "scope_session_status_created_at" not in existing_indexes:
        coll.create_index(
            [
                ("user_concept_id", ASCENDING),
                ("organisation_concept_id", ASCENDING),
                ("namespace", ASCENDING),
                ("session_id", ASCENDING),
                ("status", ASCENDING),
                ("created_at", ASCENDING),
            ],
            name="scope_session_status_created_at",
        )
    if "active_conversation_key_unique" not in existing_indexes:
        coll.create_index(
            [("active_conversation_key", ASCENDING)],
            name="active_conversation_key_unique",
            unique=True,
            partialFilterExpression={
                "active_conversation_key": {"$exists": True, "$type": "string"}
            },
        )
    if "scope_enqueue_submission_id_unique" not in existing_indexes:
        coll.create_index(
            [
                ("user_concept_id", ASCENDING),
                ("organisation_concept_id", ASCENDING),
                ("namespace", ASCENDING),
                ("enqueue_submission_id", ASCENDING),
            ],
            name="scope_enqueue_submission_id_unique",
            unique=True,
            partialFilterExpression={
                "enqueue_submission_id": {"$exists": True, "$type": "string"}
            },
        )
    if "active_task_execution_key_unique" not in existing_indexes:
        coll.create_index(
            [("active_task_execution_key", ASCENDING)],
            name="active_task_execution_key_unique",
            unique=True,
            partialFilterExpression={
                "active_task_execution_key": {"$exists": True, "$type": "string"}
            },
        )
    if "handoff_source_queue_id_unique" not in existing_indexes:
        coll.create_index(
            [("handoff_source_queue_id", ASCENDING)],
            name="handoff_source_queue_id_unique",
            unique=True,
            partialFilterExpression={
                "handoff_source_queue_id": {"$exists": True, "$type": "string"}
            },
        )
    if "retry_source_queue_id_unique" not in existing_indexes:
        coll.create_index(
            [("retry_source_queue_id", ASCENDING)],
            name="retry_source_queue_id_unique",
            unique=True,
            partialFilterExpression={
                "retry_source_queue_id": {"$exists": True, "$type": "string"}
            },
        )
    if "active_legacy_submission_key_unique" not in existing_indexes:
        coll.create_index(
            [("active_legacy_submission_key", ASCENDING)],
            name="active_legacy_submission_key_unique",
            unique=True,
            partialFilterExpression={
                "active_legacy_submission_key": {
                    "$exists": True,
                    "$type": "string",
                }
            },
        )
    if "legacy_submission_expires_at" not in existing_indexes:
        coll.create_index(
            [("legacy_submission_expires_at", ASCENDING)],
            name="legacy_submission_expires_at",
        )
    if "queued_global_slot_unique" not in existing_indexes:
        coll.create_index(
            [("queued_global_slot", ASCENDING)],
            name="queued_global_slot_unique",
            unique=True,
            partialFilterExpression={"queued_global_slot": {"$exists": True}},
        )
    if "queued_user_slot_unique" not in existing_indexes:
        coll.create_index(
            [("queued_user_slot", ASCENDING)],
            name="queued_user_slot_unique",
            unique=True,
            partialFilterExpression={"queued_user_slot": {"$exists": True}},
        )
    if "conversation_status_enqueue_sequence" not in existing_indexes:
        coll.create_index(
            [
                ("conversation_key", ASCENDING),
                ("status", ASCENDING),
                ("enqueue_sequence", ASCENDING),
                ("created_at", ASCENDING),
                ("queue_id", ASCENDING),
            ],
            name="conversation_status_enqueue_sequence",
        )
    if "dispatch_status_next_enqueue_sequence" not in existing_indexes:
        coll.create_index(
            [
                ("dispatch_mode", ASCENDING),
                ("status", ASCENDING),
                ("next_dispatch_at", ASCENDING),
                ("enqueue_sequence", ASCENDING),
                ("created_at", ASCENDING),
                ("queue_id", ASCENDING),
            ],
            name="dispatch_status_next_enqueue_sequence",
        )
    if "dispatch_status_lease_expiry" not in existing_indexes:
        coll.create_index(
            [
                ("dispatch_mode", ASCENDING),
                ("status", ASCENDING),
                ("dispatch_lease_expires_at", ASCENDING),
            ],
            name="dispatch_status_lease_expiry",
        )
    if "handoff_reconciliation_due" not in existing_indexes:
        coll.create_index(
            [
                ("dispatch_mode", ASCENDING),
                ("status", ASCENDING),
                ("dispatch_ready", ASCENDING),
                ("handoff_reconciliation_next_at", ASCENDING),
                ("enqueue_sequence", ASCENDING),
                ("created_at", ASCENDING),
                ("queue_id", ASCENDING),
            ],
            name="handoff_reconciliation_due",
        )
    if "task_execution_reconciliation_due" not in existing_indexes:
        coll.create_index(
            [
                ("task_execution_reconciliation_status", ASCENDING),
                ("task_execution_reconciliation_next_at", ASCENDING),
                ("task_execution_reconciliation_lease_expires_at", ASCENDING),
                ("task_execution_reconciliation_pending_at", ASCENDING),
                ("queue_id", ASCENDING),
            ],
            name="task_execution_reconciliation_due",
        )
    if "task_launch_reconciliation_due" not in existing_indexes:
        coll.create_index(
            [
                ("dispatch_mode", ASCENDING),
                ("status", ASCENDING),
                ("dispatch_ready", ASCENDING),
                ("task_launch_reconciliation_next_at", ASCENDING),
                ("task_launch_reconciliation_lease_expires_at", ASCENDING),
                ("enqueue_sequence", ASCENDING),
                ("created_at", ASCENDING),
                ("queue_id", ASCENDING),
            ],
            name="task_launch_reconciliation_due",
        )
    if "user_status_created_at" not in existing_indexes:
        coll.create_index(
            [
                ("user_concept_id", ASCENDING),
                ("status", ASCENDING),
                ("created_at", ASCENDING),
            ],
            name="user_status_created_at",
        )
    if "request_user_namespace" not in existing_indexes:
        coll.create_index(
            [
                ("client_request_id", ASCENDING),
                ("user_concept_id", ASCENDING),
                ("namespace", ASCENDING),
            ],
            name="request_user_namespace",
        )
    if "updated_at_-1" not in existing_indexes:
        coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")
    if "purge_after_ttl" not in existing_indexes:
        coll.create_index(
            [("purge_after", ASCENDING)],
            name="purge_after_ttl",
            expireAfterSeconds=0,
            sparse=True,
        )


def _ensure_window_session_binding_indexes(coll: Collection) -> None:
    existing_indexes = {idx["name"] for idx in coll.list_indexes()}
    if "user_id_1" not in existing_indexes:
        coll.create_index([("user_id", ASCENDING)], name="user_id_1")
    if "expires_at_ttl" not in existing_indexes:
        coll.create_index(
            [("expires_at", ASCENDING)],
            name="expires_at_ttl",
            expireAfterSeconds=0,
        )


def _ensure_gmail_outbound_quota_indexes(coll: Collection) -> None:
    existing_indexes = {idx["name"] for idx in coll.list_indexes()}
    if "expires_at_ttl" not in existing_indexes:
        coll.create_index(
            [("expires_at", ASCENDING)],
            name="expires_at_ttl",
            expireAfterSeconds=0,
        )


def _ensure_gmail_outbound_delivery_indexes(coll: Collection) -> None:
    existing_indexes = {idx["name"] for idx in coll.list_indexes()}
    if "delivery_fingerprint_1_unique" not in existing_indexes:
        coll.create_index(
            [("delivery_fingerprint", ASCENDING)],
            name="delivery_fingerprint_1_unique",
            unique=True,
        )
    if "status_1_updated_at_-1" not in existing_indexes:
        coll.create_index(
            [("status", ASCENDING), ("updated_at", DESCENDING)],
            name="status_1_updated_at_-1",
        )


# --- Collection Access ---
def get_people_collection() -> Collection | None:
    """DEPRECATED: Returns the 'people' collection instance. Will be replaced by get_entities_collection.
    REFACTORING_NOTE: This accessor is deprecated and will be removed after data migration
    and all calling code is updated to use the new entity services.
    """
    db = get_db()
    if db is not None:  # Changed from 'if db:'
        return db[PEOPLE_COLLECTION_NAME]
    return None


def get_interactions_collection() -> Collection | None:
    """Returns the 'interactions' collection instance."""
    db = get_db()
    if db is not None:  # Changed from 'if db:'
        return db[INTERACTIONS_COLLECTION_NAME]
    return None


def get_interaction_log_collection() -> Collection | None:
    """Returns the 'interaction_log' collection instance."""
    db = get_db()
    if db is not None:
        return db[INTERACTION_LOG_COLLECTION_NAME]
    return None


def get_interaction_sessions_collection() -> Collection | None:
    """Returns the 'interaction_sessions' collection and ensures polling indexes."""
    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            INTERACTION_SESSIONS_COLLECTION_NAME,
            _ensure_interaction_sessions_indexes,
        )
    return None


# REFACTORING_NOTE: New accessor for the generalized \'entities\' collection.
# This will be used by new entity services for CRUD operations on individuals.


def get_concepts_collection() -> Collection | None:
    """Returns the unified 'concepts' collection instance.
    This collection contains all concepts from the previously separate
    vontology_nodes and entities collections.
    """
    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            CONCEPTS_COLLECTION_NAME,
            _ensure_concepts_collection_indexes,
        )
    return None


def get_entities_collection() -> Collection | None:
    """Returns the \'entities\' collection instance.
    REFACTORING_NOTE: Ensures indexes as per the design in concept_refactoring.md.
    """
    db = get_db()
    if db is not None:
        entities_coll = db[ENTITIES_COLLECTION_NAME]
        # REFACTORING_NOTE: Create indexes as defined in concept_refactoring.md (Sub-Task 1.2)
        # This is idempotent; MongoDB creates them only if they don\'t exist or have changed.
        try:
            entities_coll.create_index([("vontology_path", ASCENDING)])
            entities_coll.create_index(
                [
                    ("name", "text"),
                    ("description", "text"),
                    # ("attributes.$**", "text") # Index all values within the attributes object - REMOVED TO PREVENT WARNING
                ],
                name="entities_text_search_index",
                default_language="none",
            )  # Added default_language='none' for broader compatibility
            entities_coll.create_index([("system_tags", ASCENDING)])
            entities_coll.create_index([("user_tags", ASCENDING)])
            entities_coll.create_index(
                [("linked_entities.target_entity_id", ASCENDING)]
            )
            # print(f"Indexes for \'{ENTITIES_COLLECTION_NAME}\' ensured.")
        except OperationFailure as e:
            logger.warning(
                "Error creating indexes for '%s': %s. This might happen with certain MongoDB configurations (e.g., free tier Atlas).",
                ENTITIES_COLLECTION_NAME,
                e,
            )
        except Exception as e:
            logger.warning(
                "An unexpected error occurred during index creation for '%s': %s",
                ENTITIES_COLLECTION_NAME,
                e,
            )
        return entities_coll
    return None


# REFACTORING_NOTE: New accessor for the \'user_entity_tracking\' collection.
# This will be used to manage recent and key entities for the UI, and to track entity usage.
def get_user_entity_tracking_collection() -> Collection | None:
    """Returns the \'user_entity_tracking\' collection instance.
    REFACTORING_NOTE: Ensures indexes as per the design in concept_refactoring.md.
    """
    db = get_db()
    if db is not None:
        user_tracking_coll = db[USER_ENTITY_TRACKING_COLLECTION_NAME]
        # REFACTORING_NOTE: Create indexes as defined in concept_refactoring.md (Sub-Task 1.2)
        # Example: user_tracking_coll.create_index([("user_id", ASCENDING), ("entity_id", ASCENDING)], unique=True)
        # Example: user_tracking_coll.create_index([("user_id", ASCENDING), ("last_accessed_at", DESCENDING)])
        # Example: user_tracking_coll.create_index([("user_id", ASCENDING), ("is_key_entity", ASCENDING)])
        # Actual indexes to be confirmed based on query patterns. For now, ensuring collection access.
        # For Sub-Task 5.4 (concept_refactoring.md), no specific indexes were detailed for this collection,
        # but common ones would be on user_id and last_accessed_at or is_key_entity.
        # For now, let's assume a general index on user_id might be useful.
        user_tracking_coll.create_index([("user_id", ASCENDING)])
        return user_tracking_coll
    return None


# REFACTORING_NOTE: New accessor for the 'migrations_log' collection.
# This will be used to track the status of data migrations.
def get_migrations_log_collection() -> Collection | None:
    """Returns the 'migrations_log' collection instance.
    Ensures indexes for efficient querying of migration status.
    """
    db = get_db()
    if db is not None:
        migrations_log_coll = db[MIGRATIONS_LOG_COLLECTION_NAME]
        # Index on migration_name for quick lookups, should be unique.
        migrations_log_coll.create_index([("migration_name", ASCENDING)], unique=True)
        # Index on completed_at for sorting or querying by time.
        migrations_log_coll.create_index([("completed_at", DESCENDING)])
        return migrations_log_coll
    return None


def get_application_settings_collection() -> Collection | None:
    """Returns the 'application_settings' collection instance.
    Ensures indexes for efficient querying of settings.
    """
    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            APPLICATION_SETTINGS_COLLECTION_NAME,
            lambda settings_coll: settings_coll.create_index(
                [("setting_name", ASCENDING)],
                unique=True,
            ),
        )
    return None


# New accessors for text value storage (JVNAUTOSCI-333)
def get_text_values_collection() -> Collection | None:
    """Returns the 'text_values' collection instance and ensures indexes.
    Stores language-tagged text values referenced by relations.
    """
    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            TEXT_VALUES_COLLECTION_NAME,
            _ensure_text_values_indexes,
        )
    return None


def get_text_relations_collection() -> Collection | None:
    """Returns the 'text_relations' collection instance and ensures indexes.
    Stores links between concept documents and text_values by predicate.
    """
    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            TEXT_RELATIONS_COLLECTION_NAME,
            _ensure_text_relations_indexes,
        )
    return None


def get_scoped_knowledge_assertions_collection() -> Collection | None:
    """Return the non-canonical, actor-scoped knowledge assertion collection."""

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            SCOPED_KNOWLEDGE_ASSERTIONS_COLLECTION_NAME,
            _ensure_scoped_knowledge_assertions_indexes,
        )
    return None


def get_ontology_authority_delegations_collection() -> Collection | None:
    """Return revocable, bounded ontology-authority delegation records."""

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            ONTOLOGY_AUTHORITY_DELEGATIONS_COLLECTION_NAME,
            _ensure_ontology_authority_delegation_indexes,
        )
    return None


def get_ontology_mutation_receipts_collection() -> Collection | None:
    """Return provenance-bearing semantic mutation decision receipts."""

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            ONTOLOGY_MUTATION_RECEIPTS_COLLECTION_NAME,
            _ensure_ontology_mutation_receipt_indexes,
        )
    return None


def get_organisation_membership_receipts_collection() -> Collection | None:
    """Return durable receipts for governed organisation membership effects."""

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            ORGANISATION_MEMBERSHIP_RECEIPTS_COLLECTION_NAME,
            _ensure_organisation_membership_receipt_indexes,
        )
    return None


def get_von_login_email_receipts_collection() -> Collection | None:
    """Return durable receipts for governed login-email effects."""

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            VON_LOGIN_EMAIL_RECEIPTS_COLLECTION_NAME,
            _ensure_von_login_email_receipt_indexes,
        )
    return None


def get_meta_relations_collection() -> Collection | None:
    """Returns the 'meta_relations' collection instance and ensures indexes.
    Stores auxiliary mapping documents for relation elicitation:
      - suggested_relations_for_type
      - user_question_prompt_for_relation
      - understand_user_response_for_relation
    Index strategy:
      - type + subject_type_id (for suggested lists)
      - type + predicate_id (for prompt templates)
      - updated_at descending (optional admin queries)
    """
    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            META_RELATIONS_COLLECTION_NAME,
            _ensure_meta_relations_indexes,
        )
    return None


def get_relationship_extent_index_collection() -> Collection | None:
    """Returns the derived relationship extent index collection.

    The collection is a query support surface only. Canonical relationship
    authority remains in the concepts collection's ``relationships`` field.
    """

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            RELATIONSHIP_EXTENT_INDEX_COLLECTION_NAME,
            _ensure_relationship_extent_index_indexes,
        )
    return None


def get_chat_prompt_queue_collection() -> Collection | None:
    """Returns the persisted chat prompt queue collection and ensures indexes."""
    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            CHAT_PROMPT_QUEUE_COLLECTION_NAME,
            _ensure_chat_prompt_queue_indexes,
        )
    return None


def get_chat_prompt_queue_counters_collection() -> Collection | None:
    """Return atomic operational counters used to order queued prompts."""

    db = get_db()
    if db is not None:
        return db[CHAT_PROMPT_QUEUE_COUNTERS_COLLECTION_NAME]
    return None


def get_window_session_binding_collection() -> Collection | None:
    """Return durable actor-owned browser-window organisation selections."""

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            WINDOW_SESSION_BINDINGS_COLLECTION_NAME,
            _ensure_window_session_binding_indexes,
        )
    return None


def get_gmail_outbound_quota_collection() -> Collection | None:
    """Return atomic outbound Gmail counters with bounded TTL retention."""

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            GMAIL_OUTBOUND_QUOTA_COLLECTION_NAME,
            _ensure_gmail_outbound_quota_indexes,
        )
    return None


def get_gmail_outbound_deliveries_collection() -> Collection | None:
    """Return the durable, delivery-fingerprint-unique Gmail effect ledger."""

    db = get_db()
    if db is not None:
        return _ensure_collection_indexes_once(
            db,
            GMAIL_OUTBOUND_DELIVERIES_COLLECTION_NAME,
            _ensure_gmail_outbound_delivery_indexes,
        )
    return None


def get_agent_gmail_tokens_collection() -> Collection | None:
    """Returns the agent Gmail token collection and ensures indexes.

    Stores encrypted OAuth token payloads for agent Gmail profiles.
    IMPORTANT: Token content should never be logged.
    """

    db = get_db()
    if db is None:
        return None

    coll = db[AGENT_GMAIL_TOKENS_COLLECTION_NAME]
    try:
        existing_indexes = [idx["name"] for idx in coll.list_indexes()]

        if "profile_id_1_unique" not in existing_indexes:
            coll.create_index(
                [("profile_id", ASCENDING)], unique=True, name="profile_id_1_unique"
            )
        if "authorised_email_1" not in existing_indexes:
            coll.create_index(
                [("authorised_email", ASCENDING)], name="authorised_email_1"
            )
        if "updated_at_-1" not in existing_indexes:
            coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")
    except OperationFailure as e:
        logger.warning(
            "Could not create some indexes for %s: %s",
            AGENT_GMAIL_TOKENS_COLLECTION_NAME,
            e,
        )
    except Exception as e:
        logger.warning(
            "Index creation skipped for %s: %s", AGENT_GMAIL_TOKENS_COLLECTION_NAME, e
        )

    return coll


def get_room_devices_collection() -> Collection | None:
    """Returns the room devices collection and ensures indexes.

    Stores provisioned meeting-room devices that can authenticate to the server.
    """

    db = get_db()
    if db is None:
        return None

    coll = db[ROOM_DEVICES_COLLECTION_NAME]
    try:
        existing_indexes = [idx["name"] for idx in coll.list_indexes()]

        if "device_id_1_unique" not in existing_indexes:
            coll.create_index(
                [("device_id", ASCENDING)], unique=True, name="device_id_1_unique"
            )
        if "organisation_id_1" not in existing_indexes:
            coll.create_index(
                [("organisation_id", ASCENDING)], name="organisation_id_1"
            )
        if "updated_at_-1" not in existing_indexes:
            coll.create_index([("updated_at", DESCENDING)], name="updated_at_-1")
    except OperationFailure as e:
        logger.warning(
            "Could not create some indexes for %s: %s", ROOM_DEVICES_COLLECTION_NAME, e
        )
    except Exception as e:
        logger.warning(
            "Index creation skipped for %s: %s", ROOM_DEVICES_COLLECTION_NAME, e
        )

    return coll


# --- Example Usage (Optional, for testing) ---
# REFACTORING_NOTE: The __main__ block is updated to demonstrate access to the new collections
# and to reflect the new schema designs.
if __name__ == "__main__":
    # ... (keep existing people_coll access for transition demonstration) ...
    people_coll = get_people_collection()
    if people_coll is not None:
        print(
            f"Successfully accessed DEPRECATED '{PEOPLE_COLLECTION_NAME}' collection in '{get_configured_database_name()}' database."
        )
    else:
        print("Failed to access people collection. Check MongoDB connection.")

    print("\\n--- Testing Entities Collection ---")
    entities_coll = get_entities_collection()
    if entities_coll is not None:
        print(
            f"Successfully accessed '{ENTITIES_COLLECTION_NAME}' collection in '{get_configured_database_name()}' database."
        )
        # Example: Insert a test entity (if collection is empty)
        if entities_coll.count_documents({}) == 0:
            print(
                f"Attempting to insert test document into '{ENTITIES_COLLECTION_NAME}'..."
            )
            test_entity = {
                "vontology_path": "Concept/Test/TestSubject",  # Changed to string
                "name": "Test Entity Individual One",
                "description": "An entity for testing purposes.",
                "notes": "Initial test entry for generalized entities. Supports **Markdown**.",
                "attributes": {"test_attribute": "test_value", "status": "new"},
                "system_tags": ["test_data", "example"],
                "user_tags": ["important_test"],
                "linked_entities": [
                    {
                        "relationship_type": "related_to",
                        "target_entity_id": "some_other_entity_id_str",  # Placeholder
                        "target_vontology_path": "Concept/Test/AnotherTestSubject",
                        "target_name_for_display": "Another Test Entity",
                    }
                ],
                "created_at": datetime.datetime.now(datetime.timezone.utc),
                "updated_at": datetime.datetime.now(datetime.timezone.utc),
            }
            try:
                result = entities_coll.insert_one(test_entity)
                print(f"Inserted test entity with ID: {result.inserted_id}")
            except Exception as e:
                print(f"Error inserting test entity: {e}")
        else:
            print(
                f"Collection '{ENTITIES_COLLECTION_NAME}' already contains documents ({entities_coll.count_documents({})} total)."
            )
            # Optionally, print one document for inspection
            # print("Sample document:", entities_coll.find_one())
    else:
        print(f"Failed to access '{ENTITIES_COLLECTION_NAME}' collection.")

    print("\\n--- Testing User Entity Tracking Collection ---")
    user_tracking_coll = get_user_entity_tracking_collection()
    if user_tracking_coll is not None:
        print(
            f"Successfully accessed '{USER_ENTITY_TRACKING_COLLECTION_NAME}' collection in '{get_configured_database_name()}' database."
        )
        # Example: Insert a test tracking entry (if collection is empty or for a new user)
        test_user_id = "test-user-main-script"
        if user_tracking_coll.count_documents({"user_identifier": test_user_id}) == 0:
            print(
                f"Attempting to insert test document into '{USER_ENTITY_TRACKING_COLLECTION_NAME}' for user '{test_user_id}'..."
            )
            test_tracking_entry_type = {
                "user_identifier": test_user_id,
                "entity_identifier": "Concept/Test/TestType",  # Tracking a type
                "entity_kind": "type",
                "vontology_path": "Concept/Test/TestType",
                "entity_name_for_display": "Test Type",
                "last_accessed_timestamp": datetime.datetime.now(datetime.timezone.utc),
                "is_key_entity": True,
                "access_count": 5,
                "interaction_properties": {"view_mode": "list"},
            }
            test_tracking_entry_individual = {
                "user_identifier": test_user_id,
                "entity_identifier": "some_entity_id_str",  # Placeholder, would be an ObjectId string
                "entity_kind": "individual",
                "vontology_path": "Concept/Test/TestSubject",  # Type of the individual
                "entity_name_for_display": "Test Tracked Individual",
                "last_accessed_timestamp": datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=1),
                "is_key_entity": False,
                "access_count": 1,
                "interaction_properties": {"filter_active": True},
            }
            try:
                result_type = user_tracking_coll.insert_one(test_tracking_entry_type)
                print(
                    f"Inserted test type tracking entry with ID: {result_type.inserted_id}"
                )
                result_individual = user_tracking_coll.insert_one(
                    test_tracking_entry_individual
                )
                print(
                    f"Inserted test individual tracking entry with ID: {result_individual.inserted_id}"
                )
            except Exception as e:
                print(f"Error inserting test tracking entry: {e}")
        else:
            print(
                f"Collection '{USER_ENTITY_TRACKING_COLLECTION_NAME}' already contains documents for user '{test_user_id}' ({user_tracking_coll.count_documents({'user_identifier': test_user_id})} total)."
            )
            # Optionally, print one document for inspection
            # print("Sample document:", user_tracking_coll.find_one({"user_identifier": test_user_id}))
    else:
        print(f"Failed to access '{USER_ENTITY_TRACKING_COLLECTION_NAME}' collection.")

    print("\n--- Testing Migrations Log Collection ---")
    migrations_log_coll = get_migrations_log_collection()
    if migrations_log_coll is not None:
        print(f"Successfully accessed '{MIGRATIONS_LOG_COLLECTION_NAME}' collection.")
        print(
            f"Indexes on migrations_log_coll: {migrations_log_coll.index_information()}"
        )
    else:
        print(f"Failed to access '{MIGRATIONS_LOG_COLLECTION_NAME}' collection.")

    print("\\n--- Testing Application Settings Collection ---")
    app_settings_coll = get_application_settings_collection()
    if app_settings_coll is not None:
        print(
            f"Successfully accessed '{APPLICATION_SETTINGS_COLLECTION_NAME}' collection."
        )
        print(f"Indexes on app_settings_coll: {app_settings_coll.index_information()}")
        # Example: Insert or update a test setting
        try:
            app_settings_coll.update_one(
                {"setting_name": "test_setting"},
                {
                    "$set": {
                        "value": "test_value",
                        "updated_at": datetime.datetime.now(datetime.timezone.utc),
                    }
                },
                upsert=True,
            )
            print("Upserted test_setting.")
            retrieved_setting = app_settings_coll.find_one(
                {"setting_name": "test_setting"}
            )
            print(f"Retrieved test_setting: {retrieved_setting}")
        except Exception as e:
            print(f"Error interacting with test_setting: {e}")
    else:
        print(f"Failed to access '{APPLICATION_SETTINGS_COLLECTION_NAME}' collection.")

    # ... (keep existing interactions_coll and interaction_log_coll access for demonstration) ...
    interactions_coll = get_interactions_collection()
    if interactions_coll is not None:
        print(f"\\nSuccessfully accessed '{INTERACTIONS_COLLECTION_NAME}' collection.")
        # REFACTORING_NOTE: The schema for this collection will also be updated as per Sub-Task 1.4.
        # For now, just access. Future __main__ tests could reflect new schema.
    else:
        print(f"\\nFailed to access '{INTERACTIONS_COLLECTION_NAME}' collection.")

    interaction_log_coll = get_interaction_log_collection()
    if interaction_log_coll is not None:
        print(
            f"\\nSuccessfully accessed '{INTERACTION_LOG_COLLECTION_NAME}' collection."
        )
    else:
        print(f"\\nFailed to access '{INTERACTION_LOG_COLLECTION_NAME}' collection.")
