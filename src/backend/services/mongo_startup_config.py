"""MongoDB hosted-startup validation and probe helpers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from ..utils.runtime_env import clean_env_value, get_env_bool, load_secret_from_env_or_file


def mongo_strict_startup_enabled() -> bool:
    return get_env_bool("VON_MONGO_STRICT_STARTUP", False)


def mongo_startup_probe_enabled() -> bool:
    strict_default = mongo_strict_startup_enabled()
    return get_env_bool("VON_MONGO_STARTUP_PROBE", strict_default)


def _resolve_mongo_uri() -> str | None:
    uri = load_secret_from_env_or_file("MONGO_URI", "MONGO_URI_FILE")
    if uri:
        return uri
    return clean_env_value(os.getenv("MONGO_URI"))


def _mongo_require_tls() -> bool:
    return get_env_bool("VON_MONGO_REQUIRE_TLS", mongo_strict_startup_enabled())


def _parse_hosts_from_uri(uri: str) -> list[str]:
    parsed = urlparse(uri)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]

    hosts: list[str] = []
    for raw_host in netloc.split(","):
        host = raw_host.strip()
        if not host:
            continue
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        hosts.append(host.lower())
    return hosts


def _query_param_is_true(values: list[str] | None) -> bool | None:
    if not values:
        return None
    value = values[0].strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _require_tls_errors(uri: str) -> list[str]:
    errors: list[str] = []
    parsed = urlparse(uri)
    query = parse_qs(parsed.query or "")
    tls_param = _query_param_is_true(query.get("tls"))
    ssl_param = _query_param_is_true(query.get("ssl"))

    if parsed.scheme == "mongodb+srv":
        if tls_param is False or ssl_param is False:
            errors.append(
                "MONGO_URI disables TLS for mongodb+srv; remove tls/ssl=false."
            )
        return errors

    if parsed.scheme == "mongodb":
        tls_enabled = tls_param is True or ssl_param is True
        if not tls_enabled:
            errors.append(
                "MONGO_URI must enable TLS explicitly for mongodb:// (set tls=true or ssl=true)."
            )
        return errors

    errors.append("MONGO_URI must start with mongodb:// or mongodb+srv://.")
    return errors


def _allowed_host_suffixes() -> tuple[str, ...]:
    raw = clean_env_value(os.getenv("VON_MONGO_ALLOWED_HOST_SUFFIXES"))
    if raw:
        suffixes = tuple(
            part.strip().lower() for part in raw.split(",") if part.strip()
        )
        if suffixes:
            return suffixes
    return (".mongodb.net",)


def collect_mongo_startup_errors() -> list[str]:
    if not mongo_strict_startup_enabled():
        return []

    errors: list[str] = []
    uri = _resolve_mongo_uri()
    if not uri:
        errors.append("Missing MONGO_URI (or MONGO_URI_FILE with readable content).")
        return errors

    parsed = urlparse(uri)
    hosts = _parse_hosts_from_uri(uri)
    if not hosts:
        errors.append("MONGO_URI does not include a resolvable host.")

    if _mongo_require_tls():
        errors.extend(_require_tls_errors(uri))

    for host in hosts:
        if host in {"localhost", "127.0.0.1"}:
            errors.append(
                "MONGO_URI must not target localhost when VON_MONGO_STRICT_STARTUP is enabled."
            )

    suffixes = _allowed_host_suffixes()
    if suffixes:
        invalid_hosts = [
            host
            for host in hosts
            if not any(host.endswith(suffix) for suffix in suffixes)
        ]
        if invalid_hosts:
            errors.append(
                "MONGO_URI host is outside VON_MONGO_ALLOWED_HOST_SUFFIXES: "
                + ", ".join(invalid_hosts)
            )

    if get_env_bool("MONGO_ALLOW_LOCAL_FALLBACK", False):
        errors.append(
            "MONGO_ALLOW_LOCAL_FALLBACK must be disabled when VON_MONGO_STRICT_STARTUP is enabled."
        )

    if parsed.scheme not in {"mongodb", "mongodb+srv"}:
        errors.append("MONGO_URI must start with mongodb:// or mongodb+srv://.")

    return errors


def validate_mongo_startup_or_raise() -> None:
    errors = collect_mongo_startup_errors()
    if not errors:
        return
    detail = " ; ".join(errors)
    raise RuntimeError(f"Mongo startup validation failed: {detail}")


def run_mongo_startup_probe() -> dict[str, object]:
    """Run a basic auth/read/write startup probe against configured MongoDB."""

    from ..db.mongo_client import get_db

    db = get_db()
    if db is None:
        raise RuntimeError(
            "Mongo startup probe failed: unable to initialise database connection."
        )

    try:
        ping_result = db.command("ping")
    except Exception as exc:  # pragma: no cover - exercised via caller error path
        raise RuntimeError(f"Mongo startup probe ping failed: {exc}") from exc

    read_collection = os.getenv("VON_MONGO_READ_PROBE_COLLECTION", "application_settings")
    try:
        db[read_collection].find_one({}, {"_id": 1})
    except Exception as exc:
        raise RuntimeError(
            f"Mongo startup probe read check failed for collection '{read_collection}': {exc}"
        ) from exc

    write_enabled = get_env_bool("VON_MONGO_STARTUP_PROBE_WRITE", True)
    write_collection = os.getenv("VON_MONGO_STARTUP_PROBE_COLLECTION", "_von_startup_probe")
    write_ok = None
    if write_enabled:
        marker_id = f"startup_probe:{datetime.now(timezone.utc).isoformat()}"
        payload = {
            "_id": marker_id,
            "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "probe": "startup",
        }
        try:
            coll = db[write_collection]
            coll.update_one({"_id": marker_id}, {"$set": payload}, upsert=True)
            found = coll.find_one({"_id": marker_id}, {"_id": 1})
            coll.delete_one({"_id": marker_id})
            if not isinstance(found, dict):
                raise RuntimeError("write marker not readable after upsert")
            write_ok = True
        except Exception as exc:
            raise RuntimeError(
                f"Mongo startup probe write check failed for collection '{write_collection}': {exc}"
            ) from exc

    return {
        "ok": True,
        "ping_ok": isinstance(ping_result, dict),
        "read_ok": True,
        "write_ok": write_ok,
        "read_collection": read_collection,
        "write_collection": write_collection if write_enabled else None,
    }
