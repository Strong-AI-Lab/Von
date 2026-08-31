from __future__ import annotations

import socket
import time

import dns.resolver
import pytest

from src.backend.db import mongo_client as mc


@pytest.fixture(autouse=True)
def _disable_machine_specific_tunnel_fallback(monkeypatch) -> None:
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS", ())
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET", None)


class _FakeAdmin:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    def command(self, name: str, **kwargs):
        self.commands.append((name, dict(kwargs)))
        if name == "ismaster":
            return {"ok": 1}
        return {"ok": 1}


class _FakeClient:
    def __init__(self, label: str) -> None:
        self.label = label
        self.admin = _FakeAdmin()

    def __getitem__(self, db_name: str):
        return {"label": self.label, "db_name": db_name}


def test_get_db_uses_dns_fallback_when_local_fallback_disabled(monkeypatch) -> None:
    created: list[tuple[str, dict]] = []
    dns_client = _FakeClient(label="dns-fallback")

    def _fake_new_client(uri: str, **kwargs):
        created.append((uri, dict(kwargs)))
        if uri == "mongodb+srv://primary.example/von_db":
            raise RuntimeError("The resolution lifetime expired after 5.0 seconds")
        if uri == "mongodb://direct-host.example:27017/?tls=true":
            return dns_client
        raise AssertionError(f"Unexpected URI: {uri}")

    monkeypatch.setattr(mc, "_new_mongo_client", _fake_new_client)
    monkeypatch.setattr(mc, "is_mock_db_enabled", lambda: False)
    monkeypatch.setattr(mc, "assert_safe_database_name_for_pytest", lambda _: None)
    monkeypatch.setattr(mc, "MONGO_URI", "mongodb+srv://primary.example/von_db")
    monkeypatch.setattr(
        mc,
        "MONGO_DNS_FALLBACK_URI",
        "mongodb://direct-host.example:27017/?tls=true",
    )
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)
    monkeypatch.setattr(mc, "_mongo_client_real", None)
    monkeypatch.setattr(mc, "_mongo_client_mock", None)
    monkeypatch.setattr(mc, "_using_fallback_real", False)
    monkeypatch.setattr(mc, "_effective_uri_real", mc.MONGO_URI)
    # The sticky preference is process-global by design. Earlier tests may
    # have exercised it, but this case is specifically the initial primary
    # attempt followed by DNS fallback.
    monkeypatch.setattr(mc, "_dns_fallback_preferred_until_monotonic", 0.0)
    monkeypatch.setattr(mc, "_dns_fallback_preference_reason", None)
    monkeypatch.setenv("VON_DB_NAME", "von_db")

    db = mc.get_db()

    assert db is not None
    assert db["label"] == "dns-fallback"
    assert created[0][0] == "mongodb+srv://primary.example/von_db"
    assert created[1][0] == "mongodb://direct-host.example:27017/?tls=true"
    assert mc.is_using_fallback_uri() is True
    assert (
        mc.get_effective_mongo_uri() == "mongodb://direct-host.example:27017/?tls=true"
    )
    fallback_state = mc.get_mongo_fallback_policy_state()
    assert fallback_state["dns_fallback_sticky"] is True
    assert fallback_state["dns_fallback_configured"] is True


def test_get_db_prefers_recent_successful_dns_fallback(monkeypatch) -> None:
    created: list[tuple[str, dict]] = []
    fallback_client = _FakeClient(label="dns-fallback")

    def _fake_new_client(uri: str, **kwargs):
        created.append((uri, dict(kwargs)))
        if uri == "mongodb://direct-host.example:27017/?tls=true":
            return fallback_client
        if uri == "mongodb+srv://primary.example/von_db":
            raise AssertionError("Primary SRV URI should not be retried during sticky fallback window.")
        raise AssertionError(f"Unexpected URI: {uri}")

    monkeypatch.setattr(mc, "_new_mongo_client", _fake_new_client)
    monkeypatch.setattr(mc, "is_mock_db_enabled", lambda: False)
    monkeypatch.setattr(mc, "assert_safe_database_name_for_pytest", lambda _: None)
    monkeypatch.setattr(mc, "MONGO_URI", "mongodb+srv://primary.example/von_db")
    monkeypatch.setattr(
        mc,
        "MONGO_DNS_FALLBACK_URI",
        "mongodb://direct-host.example:27017/?tls=true",
    )
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)
    monkeypatch.setattr(mc, "_mongo_client_real", None)
    monkeypatch.setattr(mc, "_mongo_client_mock", None)
    monkeypatch.setattr(mc, "_using_fallback_real", False)
    monkeypatch.setattr(mc, "_effective_uri_real", mc.MONGO_URI)
    monkeypatch.setattr(
        mc, "_dns_fallback_preferred_until_monotonic", time.monotonic() + 60.0
    )
    monkeypatch.setattr(
        mc,
        "_dns_fallback_preference_reason",
        "primary DNS/SRV error",
    )
    monkeypatch.setenv("VON_DB_NAME", "von_db")

    db = mc.get_db()

    assert db is not None
    assert db["label"] == "dns-fallback"
    assert created == [
        (
            "mongodb://direct-host.example:27017/?tls=true",
            {"server_selection_timeout_ms": 3000},
        )
    ]
    assert mc.is_using_fallback_uri() is True
    assert (
        mc.get_effective_mongo_uri() == "mongodb://direct-host.example:27017/?tls=true"
    )


def test_resolver_fallback_configuration_is_literal_ip_and_host_scoped() -> None:
    nameservers = mc._parse_mongo_dns_resolver_fallback_nameservers(
        "8.8.8.8, resolver.example, 2001:4860:4860::8888,8.8.8.8"
    )
    hosts = mc._mongo_seed_hosts(
        "mongodb://user:do-not-log@Seed-A.Example:27017,seed-b.example:27017/?tls=true"
    )

    assert nameservers == ("8.8.8.8", "2001:4860:4860::8888")
    assert hosts == frozenset({"seed-a.example", "seed-b.example"})


def test_getaddrinfo_falls_back_only_after_system_failure_for_configured_host(
    monkeypatch,
) -> None:
    calls: list[str] = []
    system_error = socket.gaierror(socket.EAI_NONAME, "not known")

    def _fake_original(host, port, family, type, proto, flags):
        calls.append(str(host))
        if host == "seed.example":
            raise system_error
        if host == "203.0.113.7":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("203.0.113.7", port),
                )
            ]
        raise AssertionError(f"Unexpected host: {host}")

    monkeypatch.setattr(mc, "_ORIGINAL_SOCKET_GETADDRINFO", _fake_original)
    monkeypatch.setattr(
        mc, "_MONGO_DNS_RESOLVER_FALLBACK_HOSTS", frozenset({"seed.example"})
    )
    monkeypatch.setattr(mc, "_MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS", ("8.8.8.8",))
    monkeypatch.setattr(
        mc,
        "_resolve_mongo_host_with_fallback_nameservers",
        lambda host, family: ["203.0.113.7"],
    )
    monkeypatch.setattr(mc, "_MONGO_DNS_RESOLVER_FALLBACK_LOGGED_HOSTS", set())

    result = mc._mongo_getaddrinfo_with_configured_fallback(
        "seed.example",
        27017,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
        0,
        0,
    )

    assert result[0][4] == ("203.0.113.7", 27017)
    assert calls == ["seed.example", "203.0.113.7"]


def test_getaddrinfo_never_bypasses_system_dns_for_unconfigured_host(
    monkeypatch,
) -> None:
    system_error = socket.gaierror(socket.EAI_NONAME, "not known")

    def _fake_original(*_args):
        raise system_error

    monkeypatch.setattr(mc, "_ORIGINAL_SOCKET_GETADDRINFO", _fake_original)
    monkeypatch.setattr(
        mc, "_MONGO_DNS_RESOLVER_FALLBACK_HOSTS", frozenset({"seed.example"})
    )
    monkeypatch.setattr(mc, "_MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS", ("8.8.8.8",))
    monkeypatch.setattr(
        mc,
        "_resolve_mongo_host_with_fallback_nameservers",
        lambda *_args: pytest.fail("Fallback resolver must not be called"),
    )

    with pytest.raises(socket.gaierror) as exc_info:
        mc._mongo_getaddrinfo_with_configured_fallback(
            "other.example", 27017, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, 0
        )

    assert exc_info.value is system_error


def test_getaddrinfo_preserves_system_result_without_fallback(monkeypatch) -> None:
    expected = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("192.0.2.9", 27017),
        )
    ]
    fallback_calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        mc, "_ORIGINAL_SOCKET_GETADDRINFO", lambda *_args: list(expected)
    )
    monkeypatch.setattr(
        mc, "_MONGO_DNS_RESOLVER_FALLBACK_HOSTS", frozenset({"seed.example"})
    )
    monkeypatch.setattr(mc, "_MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS", ("8.8.8.8",))
    monkeypatch.setattr(
        mc,
        "_resolve_mongo_host_with_fallback_nameservers",
        lambda host, family: fallback_calls.append((host, family)) or [],
    )

    result = mc._mongo_getaddrinfo_with_configured_fallback(
        "seed.example", 27017, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, 0
    )

    assert result == expected
    assert fallback_calls == []


def test_explicit_resolver_results_are_briefly_cached(monkeypatch) -> None:
    class _Address:
        address = "203.0.113.8"

    calls: list[tuple[str, str]] = []

    class _Resolver:
        nameservers: list[str]
        timeout: float
        lifetime: float

        def __init__(self, *, configure: bool) -> None:
            assert configure is False

        def resolve(self, host: str, record_type: str, *, search: bool):
            assert search is False
            calls.append((host, record_type))
            return [_Address()]

    monkeypatch.setattr(dns.resolver, "Resolver", _Resolver)
    monkeypatch.setattr(mc, "_MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS", ("8.8.8.8",))
    monkeypatch.setattr(mc, "_MONGO_DNS_RESOLVER_FALLBACK_CACHE", {})

    first = mc._resolve_mongo_host_with_fallback_nameservers(
        "seed.example", socket.AF_INET
    )
    second = mc._resolve_mongo_host_with_fallback_nameservers(
        "seed.example", socket.AF_INET
    )

    assert first == ["203.0.113.8"]
    assert second == first
    assert calls == [("seed.example", "A")]


def test_direct_route_preference_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv("MONGO_DNS_RESOLVER_FALLBACK_PREFER_DIRECT", "0")
    assert mc._mongo_dns_resolver_fallback_prefer_direct() is False

    monkeypatch.setenv("MONGO_DNS_RESOLVER_FALLBACK_PREFER_DIRECT", "1")
    assert mc._mongo_dns_resolver_fallback_prefer_direct() is True


def test_initial_direct_route_uses_only_dns_fallback_target(monkeypatch) -> None:
    direct_target = mc._MongoFallbackTarget(
        uri="mongodb://direct-host.example:27017/?tls=true",
        kind="dns",
        label="direct-host Atlas",
    )
    tunnel_target = mc._MongoFallbackTarget(
        uri="mongodb://127.0.0.1:27018/?tls=true",
        kind="ssh_tunnel",
        label="SSH tunnel",
    )
    calls: list[mc._MongoFallbackTarget] = []
    expected = object()

    monkeypatch.setenv("MONGO_DNS_RESOLVER_FALLBACK_PREFER_DIRECT", "1")
    monkeypatch.setattr(mc, "_MONGO_DNS_RESOLVER_FALLBACK_NAMESERVERS", ("8.8.8.8",))
    monkeypatch.setattr(
        mc, "_MONGO_DNS_RESOLVER_FALLBACK_HOSTS", frozenset({"direct-host.example"})
    )
    monkeypatch.setattr(
        mc,
        "_connection_fallback_targets",
        lambda *, prefer_dns: [direct_target, tunnel_target],
    )

    def _fake_fallback_candidate(target, *, preference_reason):
        assert preference_reason == "explicit direct-host resolver recovery"
        calls.append(target)
        return expected

    monkeypatch.setattr(mc, "_fallback_candidate", _fake_fallback_candidate)

    assert mc._try_initial_direct_dns_candidate() is expected
    assert calls == [direct_target]
