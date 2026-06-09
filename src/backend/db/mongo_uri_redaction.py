from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def _coerce_uri(uri: object) -> str | None:
    if not isinstance(uri, str):
        return None
    text = uri.strip()
    return text or None


def _authority_from_uri(uri: str) -> tuple[str | None, str]:
    """Return scheme and authority without path/query/fragment."""

    try:
        parsed = urlsplit(uri)
        scheme = parsed.scheme or None
        authority = parsed.netloc
        if authority:
            return scheme, authority
    except Exception:
        pass

    scheme = None
    rest = uri
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
        scheme = scheme or None
    rest = rest.split("#", 1)[0].split("?", 1)[0].split("/", 1)[0]
    return scheme, rest


def _strip_userinfo(authority: str) -> str:
    # Passwords may contain '@', so keep only the final host list segment.
    return authority.rsplit("@", 1)[-1].strip()


def _has_userinfo_marker_outside_authority(uri: str, authority: str) -> bool:
    if "@" in authority or "://" not in uri:
        return False
    rest = uri.split("://", 1)[1]
    before_query = rest.split("#", 1)[0].split("?", 1)[0]
    return "@" in before_query


def _host_name(host_entry: str) -> str:
    entry = host_entry.strip()
    if entry.startswith("["):
        end = entry.find("]")
        if end != -1:
            return entry[1:end].lower()
    return entry.split(":", 1)[0].lower()


def _hosts_from_authority(authority: str) -> list[str]:
    return [part.strip() for part in authority.split(",") if part.strip()]


def is_local_mongo_host(host_entry: str) -> bool:
    host = _host_name(host_entry)
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def sanitize_mongo_uri_for_display(uri: object) -> str | None:
    """Return only the Mongo connection location safe for operator display."""

    text = _coerce_uri(uri)
    if text is None:
        return None

    scheme, authority = _authority_from_uri(text)
    if _has_userinfo_marker_outside_authority(text, authority):
        return None
    host_authority = _strip_userinfo(authority)
    hosts = _hosts_from_authority(host_authority)
    if not hosts:
        return None
    safe_authority = ",".join(hosts)
    if scheme:
        return f"{scheme}://{safe_authority}"
    return safe_authority


def classify_mongo_connection_location(
    sanitized_uri: object,
    *,
    scheme: str | None = None,
    hosts: list[str] | None = None,
) -> str:
    text = _coerce_uri(sanitized_uri) or ""
    if hosts is None:
        _scheme, authority = _authority_from_uri(text)
        scheme = scheme or _scheme
        hosts = _hosts_from_authority(_strip_userinfo(authority))
    scheme_text = (scheme or "").lower()
    if any(is_local_mongo_host(host) for host in hosts):
        return "local"
    if scheme_text == "mongodb+srv" or any(
        "mongodb.net" in host.lower() for host in hosts
    ):
        return "atlas"
    if hosts:
        return "remote"
    return "unknown"


def build_safe_mongo_connection_location(
    uri: object,
    *,
    using_fallback: bool | None = None,
) -> dict[str, Any]:
    text = _coerce_uri(uri)
    scheme = None
    hosts: list[str] = []
    sanitized_uri = None
    if text is not None:
        scheme, authority = _authority_from_uri(text)
        if not _has_userinfo_marker_outside_authority(text, authority):
            hosts = _hosts_from_authority(_strip_userinfo(authority))
        sanitized_uri = sanitize_mongo_uri_for_display(text)
        if sanitized_uri is None:
            hosts = []

    classification = classify_mongo_connection_location(
        sanitized_uri, scheme=scheme, hosts=hosts
    )
    return {
        "available": sanitized_uri is not None,
        "scheme": scheme,
        "host": hosts[0] if hosts else None,
        "hosts": hosts,
        "host_count": len(hosts),
        "sanitized_uri": sanitized_uri,
        "classification": classification,
        "is_atlas": classification == "atlas",
        "is_local": classification == "local",
        "using_fallback": using_fallback,
    }
