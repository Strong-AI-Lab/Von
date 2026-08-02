#!/usr/bin/env python3
"""Reconcile named Atlas database IP access-list entries for changing egress IPs.

This is a standalone operational tool. It does not import or modify Von runtime
code. Each node observes its own public IPv4 address, and an authenticated SSH
peer can execute the narrowly bounded Atlas operation when the caller's Atlas
Admin API path is unavailable.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

ATLAS_API_BASE = "https://cloud.mongodb.com/api/atlas/v2"
ATLAS_TOKEN_URL = "https://cloud.mongodb.com/api/oauth/token"
ATLAS_ACCEPT = "application/vnd.atlas.2025-03-12+json"
DEFAULT_CONFIG_PATH = Path.home() / ".config/von/atlas-egress-reconciler.json"
DEFAULT_IP_OBSERVERS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
)
_PROJECT_ID_RE = re.compile(r"^[a-f0-9]{24}$")
_CLIENT_ID_RE = re.compile(r"^mdb_sa_id_[A-Fa-f0-9]{24}$")
_NODE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SSH_DESTINATION_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9._-]*@"
    r"(?:[A-Za-z0-9_][A-Za-z0-9._-]*|\[[0-9A-Fa-f:]+\])$"
)
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")


class ConfigurationError(ValueError):
    """The reconciler configuration is unsafe or incomplete."""


class ObservationError(RuntimeError):
    """No trustworthy public-IP observation was available."""


class AtlasAPIError(RuntimeError):
    """An Atlas Admin API operation failed without exposing credentials."""

    def __init__(self, operation: str, detail: str, status: int | None = None):
        super().__init__(f"{operation}: {detail}")
        self.operation = operation
        self.detail = detail
        self.status = status


class ProbeError(RuntimeError):
    """The target node could not prove its Atlas database network path."""


class PeerError(RuntimeError):
    """The authenticated peer operation failed."""


@dataclass(frozen=True)
class SecretSource:
    source: str
    path: Path | None = None
    service: str | None = None
    account: str | None = None


@dataclass(frozen=True)
class DatabaseProbe:
    host: str
    port: int = 27017
    timeout_seconds: float = 8.0


@dataclass(frozen=True)
class PeerConfig:
    node: str
    ssh_destination: str
    identity_file: Path
    known_hosts_file: Path
    remote_python: str
    remote_script: str
    remote_config: str
    connect_timeout_seconds: int = 10


@dataclass(frozen=True)
class Config:
    path: Path
    project_id: str
    client_id: str
    secret: SecretSource
    local_node: str
    managed_entries: Mapping[str, str]
    allowed_peer_nodes: frozenset[str]
    ip_observers: tuple[str, ...]
    min_observer_agreement: int
    database_probe: DatabaseProbe
    activation_timeout_seconds: float
    state_path: Path
    lock_path: Path
    notification_interval_seconds: int
    peer: PeerConfig | None

    @classmethod
    def load(cls, path: Path) -> Config:
        path = path.expanduser()
        _validate_private_file(path, "configuration", allow_public_read=False)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read configuration: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ConfigurationError("configuration schema_version must be 1")

        atlas = _required_mapping(raw, "atlas")
        project_id = _required_string(atlas, "project_id")
        client_id = _required_string(atlas, "client_id")
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise ConfigurationError("atlas.project_id must be 24 lowercase hex digits")
        if not _CLIENT_ID_RE.fullmatch(client_id):
            raise ConfigurationError(
                "atlas.client_id is not an Atlas service-account ID"
            )
        secret = _parse_secret(_required_mapping(atlas, "client_secret"))

        local_node = _required_string(raw, "local_node")
        if not _NODE_RE.fullmatch(local_node):
            raise ConfigurationError("local_node has an invalid node name")
        raw_entries = _required_mapping(raw, "managed_entries")
        managed_entries: dict[str, str] = {}
        for node, comment in raw_entries.items():
            if not isinstance(node, str) or not _NODE_RE.fullmatch(node):
                raise ConfigurationError(
                    "managed_entries contains an invalid node name"
                )
            if not isinstance(comment, str) or not comment.strip() or len(comment) > 80:
                raise ConfigurationError(
                    "managed entry comments must contain 1 to 80 characters"
                )
            managed_entries[node] = comment
        if local_node not in managed_entries:
            raise ConfigurationError("local_node must have a managed_entries comment")
        if len(set(managed_entries.values())) != len(managed_entries):
            raise ConfigurationError("managed entry comments must be unique")

        raw_allowed = raw.get("allowed_peer_nodes", [])
        if not isinstance(raw_allowed, list) or not all(
            isinstance(node, str) and _NODE_RE.fullmatch(node) for node in raw_allowed
        ):
            raise ConfigurationError("allowed_peer_nodes must be a list of node names")
        if any(node not in managed_entries for node in raw_allowed):
            raise ConfigurationError(
                "every allowed_peer_nodes value must have a managed entry"
            )

        raw_observers = raw.get("ip_observers", list(DEFAULT_IP_OBSERVERS))
        if not isinstance(raw_observers, list) or len(raw_observers) < 2:
            raise ConfigurationError(
                "ip_observers must contain at least two HTTPS URLs"
            )
        observers = tuple(_validate_observer_url(value) for value in raw_observers)
        agreement = raw.get("min_observer_agreement", 2)
        if not isinstance(agreement, int) or not 2 <= agreement <= len(observers):
            raise ConfigurationError(
                "min_observer_agreement must be between 2 and the observer count"
            )

        probe_raw = _required_mapping(raw, "database_probe")
        probe_host = _required_string(probe_raw, "host")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", probe_host):
            raise ConfigurationError("database_probe.host is invalid")
        if not probe_host.endswith(".mongodb.net"):
            raise ConfigurationError("database_probe.host must end with .mongodb.net")
        probe_port = probe_raw.get("port", 27017)
        probe_timeout = probe_raw.get("timeout_seconds", 8.0)
        if not isinstance(probe_port, int) or not 1 <= probe_port <= 65535:
            raise ConfigurationError("database_probe.port must be a valid TCP port")
        if not isinstance(probe_timeout, (int, float)) or not 1 <= probe_timeout <= 30:
            raise ConfigurationError(
                "database_probe.timeout_seconds must be between 1 and 30"
            )

        activation_timeout = raw.get("activation_timeout_seconds", 90.0)
        if (
            not isinstance(activation_timeout, (int, float))
            or not 5 <= activation_timeout <= 300
        ):
            raise ConfigurationError(
                "activation_timeout_seconds must be between 5 and 300"
            )
        state_path = _absolute_path(
            raw.get(
                "state_path",
                str(Path.home() / ".local/state/von/atlas-egress-reconciler.json"),
            ),
            "state_path",
        )
        lock_path = _absolute_path(
            raw.get(
                "lock_path",
                str(Path.home() / ".local/state/von/atlas-egress-reconciler.lock"),
            ),
            "lock_path",
        )
        notification_interval = raw.get("notification_interval_seconds", 86400)
        if not isinstance(notification_interval, int) or notification_interval < 300:
            raise ConfigurationError(
                "notification_interval_seconds must be at least 300"
            )

        peer = _parse_peer(raw.get("peer"), managed_entries)
        if peer is not None and peer.node == local_node:
            raise ConfigurationError("peer.node must differ from local_node")

        return cls(
            path=path,
            project_id=project_id,
            client_id=client_id,
            secret=secret,
            local_node=local_node,
            managed_entries=managed_entries,
            allowed_peer_nodes=frozenset(raw_allowed),
            ip_observers=observers,
            min_observer_agreement=agreement,
            database_probe=DatabaseProbe(
                host=probe_host,
                port=probe_port,
                timeout_seconds=float(probe_timeout),
            ),
            activation_timeout_seconds=float(activation_timeout),
            state_path=state_path,
            lock_path=lock_path,
            notification_interval_seconds=notification_interval,
            peer=peer,
        )


def _required_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be an object")
    return value


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value


def _absolute_path(value: Any, description: str) -> Path:
    if not isinstance(value, str):
        raise ConfigurationError(f"{description} must be a string path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{description} must be an absolute path")
    return path


def _validate_private_file(
    path: Path, description: str, *, allow_public_read: bool
) -> None:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{description} must be an existing regular file")
    stat_result = path.stat()
    if stat_result.st_uid != os.getuid():
        raise ConfigurationError(f"{description} must be owned by the current user")
    forbidden = 0o022 if allow_public_read else 0o077
    if stat_result.st_mode & forbidden:
        raise ConfigurationError(f"{description} has unsafe permissions")


def _parse_secret(raw: Mapping[str, Any]) -> SecretSource:
    source = _required_string(raw, "source")
    if source == "file":
        return SecretSource(
            source=source, path=_absolute_path(raw.get("path"), "secret path")
        )
    if source == "macos-keychain":
        service = _required_string(raw, "service")
        account = _required_string(raw, "account")
        if any(char in service + account for char in "\r\n\0"):
            raise ConfigurationError("Keychain service and account must be single-line")
        return SecretSource(source=source, service=service, account=account)
    raise ConfigurationError("client_secret.source must be file or macos-keychain")


def _parse_peer(raw: Any, managed_entries: Mapping[str, str]) -> PeerConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigurationError("peer must be an object")
    node = _required_string(raw, "node")
    if node not in managed_entries:
        raise ConfigurationError("peer.node must have a managed entry")
    destination = _required_string(raw, "ssh_destination")
    if not _SSH_DESTINATION_RE.fullmatch(destination):
        raise ConfigurationError("peer.ssh_destination must be a simple USER@HOST")
    identity = _absolute_path(raw.get("identity_file"), "peer.identity_file")
    known_hosts = _absolute_path(raw.get("known_hosts_file"), "peer.known_hosts_file")
    _validate_private_file(identity, "peer identity file", allow_public_read=False)
    _validate_private_file(known_hosts, "peer known-hosts file", allow_public_read=True)
    remote_python = _required_string(raw, "remote_python")
    remote_script = _required_string(raw, "remote_script")
    remote_config = _required_string(raw, "remote_config")
    for description, value in (
        ("remote_python", remote_python),
        ("remote_script", remote_script),
        ("remote_config", remote_config),
    ):
        if not _REMOTE_PATH_RE.fullmatch(value):
            raise ConfigurationError(
                f"peer.{description} must be a simple absolute path"
            )
    timeout = raw.get("connect_timeout_seconds", 10)
    if not isinstance(timeout, int) or not 2 <= timeout <= 30:
        raise ConfigurationError("peer.connect_timeout_seconds must be 2 to 30")
    return PeerConfig(
        node=node,
        ssh_destination=destination,
        identity_file=identity,
        known_hosts_file=known_hosts,
        remote_python=remote_python,
        remote_script=remote_script,
        remote_config=remote_config,
        connect_timeout_seconds=timeout,
    )


def _validate_observer_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("each IP observer must be a URL string")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "IP observers must be credential-free HTTPS URLs without query strings"
        )
    return value


def _public_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ObservationError("observer did not return an IP address") from exc
    if address.version != 4 or not address.is_global:
        raise ObservationError(
            "observer did not return a globally routable IPv4 address"
        )
    return str(address)


def observe_public_ip(
    config: Config,
    *,
    fetch: Callable[[str, float], str] | None = None,
) -> dict[str, Any]:
    fetch = fetch or _fetch_observer
    observations: list[str] = []
    failures = 0
    for url in config.ip_observers:
        try:
            observations.append(_public_ipv4(fetch(url, 6.0)))
        except (OSError, RuntimeError, urllib.error.URLError, ObservationError):
            failures += 1
    counts = Counter(observations)
    if not counts:
        raise ObservationError("all public-IP observers failed")
    address, agreement = counts.most_common(1)[0]
    if agreement < config.min_observer_agreement:
        raise ObservationError(
            f"public-IP observers did not reach {config.min_observer_agreement}-source consensus"
        )
    return {
        "ip_address": address,
        "agreement": agreement,
        "observer_count": len(config.ip_observers),
        "observer_failures": failures,
    }


def _fetch_observer(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/plain", "User-Agent": "von-atlas-egress-reconciler/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read(256)
    return content.decode("ascii", errors="strict").strip()


def resolve_client_secret(source: SecretSource) -> str:
    if source.source == "file":
        assert source.path is not None
        _validate_private_file(
            source.path, "Atlas client-secret file", allow_public_read=False
        )
        secret = source.path.read_text(encoding="utf-8").strip()
    else:
        assert source.service is not None and source.account is not None
        if not Path("/usr/bin/security").is_file():
            raise ConfigurationError("macOS Keychain is not available on this host")
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-w",
                "-s",
                source.service,
                "-a",
                source.account,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise ConfigurationError("Atlas client secret was not found in Keychain")
        secret = result.stdout.strip()
    if not secret or any(char in secret for char in "\r\n\0"):
        raise ConfigurationError("Atlas client secret is empty or malformed")
    return secret


class AtlasClient:
    def __init__(
        self,
        config: Config,
        *,
        secret_resolver: Callable[[SecretSource], str] = resolve_client_secret,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.config = config
        self._secret_resolver = secret_resolver
        self._opener = opener
        self._token: str | None = None

    def _access_token(self) -> str:
        if self._token is not None:
            return self._token
        secret = self._secret_resolver(self.config.secret)
        credentials = base64.b64encode(
            f"{self.config.client_id}:{secret}".encode()
        ).decode("ascii")
        request = urllib.request.Request(
            ATLAS_TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "von-atlas-egress-reconciler/1",
            },
            method="POST",
        )
        payload = self._open_json(request, "obtain Atlas access token")
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise AtlasAPIError(
                "obtain Atlas access token", "response omitted access_token"
            )
        self._token = token
        return token

    def _request(self, method: str, path: str, payload: Any | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": ATLAS_ACCEPT,
            "User-Agent": "von-atlas-egress-reconciler/1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{ATLAS_API_BASE}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        return self._open_json(request, f"Atlas {method} {path.split('?')[0]}")

    def _open_json(self, request: urllib.request.Request, operation: str) -> Any:
        try:
            with self._opener(request, timeout=20.0) as response:
                body = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            detail = _safe_atlas_http_error(exc)
            raise AtlasAPIError(operation, detail, exc.code) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise AtlasAPIError(
                operation, f"transport failure ({type(exc).__name__})"
            ) from exc
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AtlasAPIError(operation, "response was not valid JSON") from exc

    @property
    def _access_list_path(self) -> str:
        return f"/groups/{self.config.project_id}/accessList"

    def list_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._request(
                "GET", f"{self._access_list_path}?itemsPerPage=500&pageNum={page}"
            )
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or not all(
                isinstance(item, dict) for item in results
            ):
                raise AtlasAPIError("list Atlas access entries", "malformed results")
            entries.extend(results)
            total = payload.get("totalCount", len(entries))
            if not isinstance(total, int) or len(entries) >= total or not results:
                return entries
            page += 1
            if page > 20:
                raise AtlasAPIError(
                    "list Atlas access entries", "pagination limit exceeded"
                )

    def create_entry(self, ip_address: str, comment: str) -> None:
        self._request(
            "POST",
            self._access_list_path,
            [{"ipAddress": ip_address, "comment": comment}],
        )

    def get_entry(self, entry_value: str) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"{self._access_list_path}/{urllib.parse.quote(entry_value, safe='')}",
        )
        if not isinstance(payload, dict):
            raise AtlasAPIError("read Atlas access entry", "malformed response")
        return payload

    def status(self, entry_value: str) -> str:
        payload = self._request(
            "GET",
            f"{self._access_list_path}/{urllib.parse.quote(entry_value, safe='')}/status",
        )
        status = payload.get("STATUS") if isinstance(payload, dict) else None
        if status not in {"PENDING", "FAILED", "ACTIVE"}:
            raise AtlasAPIError("read Atlas access entry status", "malformed status")
        return status

    def wait_until_active(self, entry_value: str) -> None:
        deadline = time.monotonic() + self.config.activation_timeout_seconds
        while True:
            status = self.status(entry_value)
            if status == "ACTIVE":
                return
            if status == "FAILED":
                raise AtlasAPIError(
                    "activate Atlas access entry", "Atlas reported FAILED"
                )
            if time.monotonic() >= deadline:
                raise AtlasAPIError(
                    "activate Atlas access entry", "timed out in PENDING"
                )
            time.sleep(2.0)

    def delete_entry(self, entry_value: str) -> None:
        self._request(
            "DELETE",
            f"{self._access_list_path}/{urllib.parse.quote(entry_value, safe='')}",
        )


def _safe_atlas_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read(65536))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return f"HTTP {exc.code}"
    if not isinstance(payload, dict):
        return f"HTTP {exc.code}"
    error_code = payload.get("errorCode")
    reason = payload.get("reason")
    parts = [f"HTTP {exc.code}"]
    if isinstance(error_code, str) and len(error_code) <= 80:
        parts.append(error_code)
    if isinstance(reason, str) and len(reason) <= 160:
        parts.append(reason.replace("\n", " ").replace("\r", " "))
    return ": ".join(parts)


def _entry_value(entry: Mapping[str, Any]) -> str | None:
    for key in ("ipAddress", "cidrBlock"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return None


def _entry_ipv4(entry: Mapping[str, Any]) -> str | None:
    value = _entry_value(entry)
    if value is None:
        return None
    try:
        if "/" in value:
            network = ipaddress.ip_network(value, strict=False)
            if network.version == 4 and network.prefixlen == 32:
                return str(network.network_address)
            return None
        address = ipaddress.ip_address(value)
        return str(address) if address.version == 4 else None
    except ValueError:
        return None


class Reconciler:
    def __init__(self, config: Config, atlas: AtlasClient) -> None:
        self.config = config
        self.atlas = atlas

    def ensure(
        self, node: str, ip_address: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        ip_address = _public_ipv4(ip_address)
        comment = self._comment(node)
        entries = self.atlas.list_entries()
        target_entries = [
            entry for entry in entries if _entry_ipv4(entry) == ip_address
        ]
        exact = [entry for entry in target_entries if entry.get("comment") == comment]
        conflicting = [
            entry for entry in target_entries if entry.get("comment") != comment
        ]
        if conflicting:
            raise AtlasAPIError(
                "ensure Atlas access entry",
                "target IP already exists under a different comment; no entry was changed",
            )
        created = False
        if not exact:
            if dry_run:
                return {
                    "node": node,
                    "ip_address": ip_address,
                    "created": True,
                    "active": None,
                    "dry_run": True,
                }
            try:
                self.atlas.create_entry(ip_address, comment)
            except AtlasAPIError:
                # A timed-out POST can still have succeeded. Reconcile by read-back
                # before deciding whether it is safe to retry on a later cycle.
                refreshed = self.atlas.list_entries()
                if not any(
                    _entry_ipv4(entry) == ip_address and entry.get("comment") == comment
                    for entry in refreshed
                ):
                    raise
            created = True
        if not dry_run:
            self.atlas.wait_until_active(ip_address)
            canonical = self.atlas.get_entry(ip_address)
            if (
                _entry_ipv4(canonical) != ip_address
                or canonical.get("comment") != comment
            ):
                raise AtlasAPIError(
                    "verify Atlas access entry", "canonical read-back did not match"
                )
        return {
            "node": node,
            "ip_address": ip_address,
            "created": created,
            "active": None if dry_run else True,
            "dry_run": dry_run,
        }

    def finalise(
        self,
        node: str,
        ip_address: str,
        *,
        target_probe_verified: bool,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        ip_address = _public_ipv4(ip_address)
        if not target_probe_verified:
            raise ProbeError(
                "old entries are retained until the target-node probe succeeds"
            )
        comment = self._comment(node)
        entries = self.atlas.list_entries()
        target = [
            entry
            for entry in entries
            if _entry_ipv4(entry) == ip_address and entry.get("comment") == comment
        ]
        if (not dry_run and len(target) != 1) or (dry_run and len(target) > 1):
            raise AtlasAPIError(
                "finalise Atlas access entry",
                "exactly one active target entry was not found; old entries were retained",
            )
        if not dry_run:
            self.atlas.wait_until_active(ip_address)
        old_values = [
            value
            for entry in entries
            if entry.get("comment") == comment
            and _entry_ipv4(entry) != ip_address
            and (value := _entry_value(entry)) is not None
        ]
        if not dry_run:
            for value in old_values:
                self.atlas.delete_entry(value)
            remaining = self.atlas.list_entries()
            if any(
                entry.get("comment") == comment and _entry_ipv4(entry) != ip_address
                for entry in remaining
            ):
                raise AtlasAPIError(
                    "verify Atlas access-list cleanup", "an old managed entry remains"
                )
        return {
            "node": node,
            "ip_address": ip_address,
            "deleted_old_entries": len(old_values),
            "dry_run": dry_run,
        }

    def full(
        self,
        node: str,
        ip_address: str,
        *,
        probe: Callable[[], None],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        ensured = self.ensure(node, ip_address, dry_run=dry_run)
        if not dry_run:
            probe()
        finalised = self.finalise(
            node,
            ip_address,
            target_probe_verified=True,
            dry_run=dry_run,
        )
        return {"ensure": ensured, "finalise": finalised, "probe_verified": not dry_run}

    def _comment(self, node: str) -> str:
        try:
            return self.config.managed_entries[node]
        except KeyError as exc:
            raise ConfigurationError("requested node has no managed entry") from exc


def probe_atlas_database(probe: DatabaseProbe) -> None:
    context = ssl.create_default_context()
    try:
        with (
            socket.create_connection(
                (probe.host, probe.port), timeout=probe.timeout_seconds
            ) as raw_socket,
            context.wrap_socket(raw_socket, server_hostname=probe.host),
        ):
            return
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise ProbeError(
            f"TLS probe to {probe.host}:{probe.port} failed ({type(exc).__name__})"
        ) from exc


class PeerClient:
    def __init__(self, peer: PeerConfig) -> None:
        self.peer = peer

    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        command = [
            "/usr/bin/ssh",
            "-F",
            "/dev/null",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.peer.known_hosts_file}",
            "-o",
            f"ConnectTimeout={self.peer.connect_timeout_seconds}",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ControlMaster=no",
            "-i",
            str(self.peer.identity_file),
            self.peer.ssh_destination,
            self.peer.remote_python,
            self.peer.remote_script,
            "--config",
            self.peer.remote_config,
            "peer-server",
        ]
        try:
            result = subprocess.run(
                command,
                input=json.dumps(payload, separators=(",", ":")),
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PeerError(
                f"peer SSH transport failed ({type(exc).__name__})"
            ) from exc
        if result.returncode != 0:
            raise PeerError(f"peer operation exited with status {result.returncode}")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PeerError("peer returned malformed JSON") from exc
        if not isinstance(response, dict):
            raise PeerError("peer returned malformed response")
        if response.get("success") is not True:
            error = response.get("error")
            detail = error if isinstance(error, str) else "peer reported failure"
            raise PeerError(detail[:240])
        return response


def _peer_payload(operation: str, **values: Any) -> dict[str, Any]:
    return {"schema_version": 1, "operation": operation, **values}


def _local_full_reconcile(
    config: Config,
    atlas: AtlasClient,
    node: str,
    ip_address: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    return Reconciler(config, atlas).full(
        node,
        ip_address,
        probe=lambda: probe_atlas_database(config.database_probe),
        dry_run=dry_run,
    )


def _reconcile_peer_with_local_admin(
    config: Config,
    atlas: AtlasClient,
    peer_client: PeerClient,
    peer_ip: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    assert config.peer is not None
    reconciler = Reconciler(config, atlas)
    ensured = reconciler.ensure(config.peer.node, peer_ip, dry_run=dry_run)
    if dry_run:
        probe_verified = False
    else:
        probe_result = peer_client.request(
            _peer_payload("probe", node=config.peer.node, ip_address=peer_ip)
        )
        probe_verified = probe_result.get("probe_verified") is True
        if not probe_verified:
            raise ProbeError("peer did not attest its database TLS probe")
    finalised = reconciler.finalise(
        config.peer.node,
        peer_ip,
        target_probe_verified=probe_verified or dry_run,
        dry_run=dry_run,
    )
    return {"ensure": ensured, "finalise": finalised, "probe_verified": probe_verified}


def _rescue_local_via_peer(
    config: Config,
    peer_client: PeerClient,
    local_ip: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {"dry_run": True, "peer_mutation_skipped": True}
    ensured = peer_client.request(
        _peer_payload("admin-ensure", node=config.local_node, ip_address=local_ip)
    )
    probe_atlas_database(config.database_probe)
    finalised = peer_client.request(
        _peer_payload(
            "admin-finalise",
            node=config.local_node,
            ip_address=local_ip,
            target_probe_verified=True,
        )
    )
    return {"ensure": ensured, "finalise": finalised, "probe_verified": True}


def run_cycle(config: Config, *, dry_run: bool = False) -> dict[str, Any]:
    observation = observe_public_ip(config)
    local_ip = observation["ip_address"]
    atlas = AtlasClient(config)
    peer_client = PeerClient(config.peer) if config.peer else None
    result: dict[str, Any] = {
        "success": True,
        "mode": "cycle",
        "local_node": config.local_node,
        "local_observation": observation,
        "dry_run": dry_run,
        "nodes": {},
    }

    local_admin_available = True
    try:
        result["nodes"][config.local_node] = {
            "path": "local-admin-api",
            **_local_full_reconcile(
                config, atlas, config.local_node, local_ip, dry_run=dry_run
            ),
        }
    except AtlasAPIError as exc:
        local_admin_available = False
        if peer_client is None:
            result["success"] = False
            result["nodes"][config.local_node] = {
                "success": False,
                "local_admin_error": str(exc),
                "peer_rescue": "not configured",
            }
        else:
            try:
                result["nodes"][config.local_node] = {
                    "path": "peer-admin-api",
                    "local_admin_error": str(exc),
                    **_rescue_local_via_peer(
                        config, peer_client, local_ip, dry_run=dry_run
                    ),
                }
            except (AtlasAPIError, ObservationError, PeerError, ProbeError) as peer_exc:
                result["success"] = False
                result["nodes"][config.local_node] = {
                    "success": False,
                    "local_admin_error": str(exc),
                    "peer_rescue_error": str(peer_exc),
                }
    except (ObservationError, PeerError, ProbeError) as exc:
        result["success"] = False
        result["nodes"][config.local_node] = {
            "success": False,
            "error": str(exc),
        }

    if peer_client is not None and config.peer is not None:
        try:
            peer_observation = peer_client.request(_peer_payload("observe"))
            peer_ip = _public_ipv4(str(peer_observation.get("ip_address", "")))
            if local_admin_available:
                peer_result = _reconcile_peer_with_local_admin(
                    config, atlas, peer_client, peer_ip, dry_run=dry_run
                )
                peer_path = "local-admin-api-with-peer-probe"
            elif dry_run:
                peer_result = {"dry_run": True, "peer_mutation_skipped": True}
                peer_path = "peer-self-admin-api"
            else:
                peer_result = peer_client.request(_peer_payload("self-reconcile"))
                peer_path = "peer-self-admin-api"
            result["nodes"][config.peer.node] = {
                "path": peer_path,
                "observation": peer_observation,
                **peer_result,
            }
        except (AtlasAPIError, ObservationError, PeerError, ProbeError) as exc:
            result["success"] = False
            result["nodes"][config.peer.node] = {
                "success": False,
                "error": str(exc),
            }
    return result


def run_preflight(config: Config, *, include_peer: bool = True) -> dict[str, Any]:
    observation = observe_public_ip(config)
    entries = AtlasClient(config).list_entries()
    probe_atlas_database(config.database_probe)
    result: dict[str, Any] = {
        "success": True,
        "mode": "preflight",
        "local_node": config.local_node,
        "local_observation": observation,
        "atlas_admin_api": "available",
        "database_tls_probe": "available",
        "managed_entry_count": sum(
            entry.get("comment") in set(config.managed_entries.values())
            for entry in entries
        ),
    }
    if include_peer and config.peer is not None:
        result["peer"] = PeerClient(config.peer).request(_peer_payload("preflight"))
    return result


def _handle_peer_request(config: Config, payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != 1:
        raise PeerError("unsupported peer request schema")
    operation = payload.get("operation")
    if operation == "observe":
        observation = observe_public_ip(config)
        return {"success": True, "node": config.local_node, **observation}
    if operation == "preflight":
        return run_preflight(config, include_peer=False)
    if operation == "self-reconcile":
        observation = observe_public_ip(config)
        result = _local_full_reconcile(
            config,
            AtlasClient(config),
            config.local_node,
            observation["ip_address"],
            dry_run=False,
        )
        return {
            "success": True,
            "node": config.local_node,
            "observation": observation,
            **result,
        }

    node = payload.get("node")
    ip_address = payload.get("ip_address")
    if not isinstance(node, str) or node not in config.managed_entries:
        raise PeerError("peer request named an unknown node")
    if not isinstance(ip_address, str):
        raise PeerError("peer request omitted ip_address")
    ip_address = _public_ipv4(ip_address)
    if operation == "probe":
        if node != config.local_node:
            raise PeerError("peer may only probe its own node")
        observed = observe_public_ip(config)["ip_address"]
        if observed != ip_address:
            raise PeerError("peer IP changed before database probe")
        probe_atlas_database(config.database_probe)
        return {"success": True, "node": node, "probe_verified": True}
    if node not in config.allowed_peer_nodes:
        raise PeerError("peer is not authorised to reconcile the requested node")
    reconciler = Reconciler(config, AtlasClient(config))
    if operation == "admin-ensure":
        result = reconciler.ensure(node, ip_address)
    elif operation == "admin-finalise":
        if payload.get("target_probe_verified") is not True:
            raise PeerError("admin-finalise requires target probe attestation")
        result = reconciler.finalise(node, ip_address, target_probe_verified=True)
    else:
        raise PeerError("unsupported peer operation")
    return {"success": True, "node": node, **result}


def _read_peer_request() -> Mapping[str, Any]:
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        raise PeerError("peer request exceeded 4096 bytes")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PeerError("peer request was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PeerError("peer request must be a JSON object")
    return payload


def _manual_recovery(config: Config, result: Mapping[str, Any]) -> str:
    observation = result.get("local_observation")
    local_ip = (
        observation.get("ip_address") if isinstance(observation, dict) else "UNKNOWN"
    )
    comment = config.managed_entries[config.local_node]
    address_instruction = (
        f"add {local_ip}/32"
        if local_ip != "UNKNOWN"
        else "determine this node's current public IPv4 address and add it as a /32"
    )
    return (
        "Atlas egress reconciliation is degraded. "
        f"For {config.local_node}, open Atlas Project > Security > Network Access, "
        f"{address_instruction} with comment '{comment}', wait until ACTIVE, then rerun "
        f"this tool. Do not remove the previous '{comment}' entry until the TLS probe succeeds."
    )


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise RuntimeError("state path must not be a symbolic link")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise RuntimeError("state directory must have owner-only permissions")
    data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _record_and_maybe_notify(
    config: Config, result: dict[str, Any], *, notify: bool
) -> None:
    previous: dict[str, Any] = {}
    if config.state_path.is_file() and not config.state_path.is_symlink():
        try:
            loaded = json.loads(config.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous = {}
    now = int(time.time())
    result["recorded_at_epoch"] = now
    if result.get("success") is not True:
        manual = _manual_recovery(config, result)
        result["manual_recovery"] = manual
        print(manual, file=sys.stderr)
        last_notification = previous.get("last_notification_at", 0)
        should_notify = (
            notify
            and isinstance(last_notification, int)
            and now - last_notification >= config.notification_interval_seconds
        )
        if should_notify:
            _macos_notification("Von Atlas access needs attention", manual[:240])
            result["last_notification_at"] = now
        elif isinstance(last_notification, int) and last_notification:
            result["last_notification_at"] = last_notification
    elif previous.get("success") is False and notify:
        _macos_notification(
            "Von Atlas access recovered",
            f"Managed egress entries are healthy for {config.local_node}.",
        )
    _write_private_json(config.state_path, result)


def _macos_notification(title: str, message: str) -> None:
    if not Path("/usr/bin/osascript").is_file():
        return
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    subprocess.run(
        ["/usr/bin/osascript", "-e", script, "--", title, message],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


class _RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.stat().st_mode & 0o077:
            raise RuntimeError("lock directory must have owner-only permissions")
        self.handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise RuntimeError("another reconciler cycle is already running") from exc
        return self

    def __exit__(self, *_args: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--no-peer", action="store_true")
    cycle = subparsers.add_parser("cycle")
    cycle.add_argument("--dry-run", action="store_true")
    cycle.add_argument("--notify", action="store_true")
    subparsers.add_parser("observe")
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--ip-address")
    reconcile.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("peer-server")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = Config.load(args.config)
    if args.command == "peer-server":
        response = _handle_peer_request(config, _read_peer_request())
        print(json.dumps(response, sort_keys=True))
        return 0
    if args.command == "observe":
        print(
            json.dumps({"success": True, **observe_public_ip(config)}, sort_keys=True)
        )
        return 0
    if args.command == "preflight":
        result = run_preflight(config, include_peer=not args.no_peer)
        print(json.dumps(result, sort_keys=True))
        return 0
    with _RunLock(config.lock_path):
        if args.command == "reconcile":
            observation = (
                {"ip_address": _public_ipv4(args.ip_address), "source": "argument"}
                if args.ip_address
                else observe_public_ip(config)
            )
            result = {
                "success": True,
                "mode": "reconcile",
                "local_node": config.local_node,
                "local_observation": observation,
                **_local_full_reconcile(
                    config,
                    AtlasClient(config),
                    config.local_node,
                    observation["ip_address"],
                    dry_run=args.dry_run,
                ),
            }
            _record_and_maybe_notify(config, result, notify=False)
        else:
            try:
                result = run_cycle(config, dry_run=args.dry_run)
            except (AtlasAPIError, ObservationError, PeerError, ProbeError) as exc:
                result = {
                    "success": False,
                    "mode": "cycle",
                    "local_node": config.local_node,
                    "dry_run": args.dry_run,
                    "error": str(exc),
                }
            _record_and_maybe_notify(config, result, notify=args.notify)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("success") is True else 2


def main() -> int:
    try:
        return run()
    except (
        AtlasAPIError,
        ConfigurationError,
        ObservationError,
        PeerError,
        ProbeError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps(
                {"success": False, "error": str(exc), "error_type": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
