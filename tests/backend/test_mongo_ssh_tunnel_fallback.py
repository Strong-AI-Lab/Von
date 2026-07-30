from __future__ import annotations

import logging
import time
from urllib.parse import parse_qsl, urlsplit

from pymongo.errors import ConnectionFailure

from src.backend.db import mongo_client as mc


class _FakeAdmin:
    def __init__(self, hello: dict[str, object]) -> None:
        self.hello = hello
        self.commands: list[tuple[str, dict[str, object]]] = []

    def command(self, name: str, **kwargs):
        self.commands.append((name, dict(kwargs)))
        if name == "hello":
            return dict(self.hello)
        return {"ok": 1}


class _FakeClient:
    def __init__(self, label: str, hello: dict[str, object]) -> None:
        self.label = label
        self.admin = _FakeAdmin(hello)
        self.closed = False

    def __getitem__(self, db_name: str):
        return {"label": self.label, "db_name": db_name}

    def close(self) -> None:
        self.closed = True


def _set_real_db_test_state(monkeypatch) -> None:
    monkeypatch.setattr(mc, "is_mock_db_enabled", lambda: False)
    monkeypatch.setattr(mc, "assert_safe_database_name_for_pytest", lambda _: None)
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)
    monkeypatch.setattr(mc, "_mongo_client_real", None)
    monkeypatch.setattr(mc, "_mongo_client_mock", None)
    monkeypatch.setattr(mc, "_using_fallback_real", False)
    monkeypatch.setattr(mc, "_active_fallback_kind_real", None)
    monkeypatch.setattr(mc, "_preferred_fallback_kind", None)
    monkeypatch.setattr(mc, "_dns_fallback_preferred_until_monotonic", 0.0)
    monkeypatch.setattr(mc, "_dns_fallback_preference_reason", None)
    monkeypatch.setattr(mc, "_last_auto_recovery_check_at", 0.0)
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")


def test_tunnel_uri_preserves_encoded_userinfo_and_controls_tls() -> None:
    primary = (
        "mongodb+srv://user%40example:p%2Fass%3Fword@cluster.example/"
        "?retryWrites=true&w=majority&replicaSet=old&ssl=false"
        "&tlsInsecure=true&tlsAllowInvalidCertificates=true"
        "&readPreference=secondary&readPreferenceTags=region%3Anz"
        "&maxStalenessSeconds=120"
    )

    derived = mc._build_ssh_tunnel_mongo_uri(primary, "127.0.0.1:27018")

    assert derived.startswith(
        "mongodb://user%40example:p%2Fass%3Fword@127.0.0.1:27018/"
    )
    options = dict(parse_qsl(urlsplit(derived).query, keep_blank_values=True))
    assert options["retryWrites"] == "true"
    assert options["w"] == "majority"
    assert options["authSource"] == "admin"
    assert options["directConnection"] == "true"
    assert options["tls"] == "true"
    assert options["tlsAllowInvalidCertificates"] == "false"
    assert options["tlsAllowInvalidHostnames"] == "true"
    assert "replicaSet" not in options
    assert "ssl" not in options
    assert "tlsInsecure" not in options
    assert "readPreference" not in options
    assert "readPreferenceTags" not in options
    assert "maxStalenessSeconds" not in options


def test_tunnel_endpoints_are_loopback_only_and_rejections_do_not_leak(
    caplog,
) -> None:
    secret = "do-not-log-this"
    with caplog.at_level(logging.WARNING):
        endpoints = mc._parse_ssh_tunnel_fallback_endpoints(
            f"127.0.0.1:27018,remote.example:27019,{secret}@127.0.0.1:27020,"
            "[::1]:27021,127.0.0.1:0"
        )

    assert endpoints == ("127.0.0.1:27018", "[::1]:27021")
    assert secret not in caplog.text


def test_network_failure_selects_writable_tunnel_member_before_direct_host(
    monkeypatch,
) -> None:
    _set_real_db_test_state(monkeypatch)
    primary_uri = "mongodb+srv://user:pass@cluster.example/"
    direct_uri = "mongodb://direct.example:27017/?tls=true"
    secondary = _FakeClient(
        "secondary",
        {"ok": 1, "setName": "atlas-set", "isWritablePrimary": False},
    )
    writable = _FakeClient(
        "writable",
        {"ok": 1, "setName": "atlas-set", "isWritablePrimary": True},
    )
    created: list[str] = []

    def _fake_new_client(uri: str, **_kwargs):
        created.append(uri)
        if uri == primary_uri:
            raise ConnectionFailure("TLS handshake failed")
        if "127.0.0.1:27018" in uri:
            return secondary
        if "127.0.0.1:27019" in uri:
            return writable
        raise AssertionError(f"Unexpected URI: {mc._host_display_from_uri(uri)}")

    monkeypatch.setattr(mc, "_new_mongo_client", _fake_new_client)
    monkeypatch.setattr(mc, "MONGO_URI", primary_uri)
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", direct_uri)
    monkeypatch.setattr(
        mc,
        "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS",
        ("127.0.0.1:27018", "127.0.0.1:27019", "127.0.0.1:27020"),
    )
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET", "atlas-set")

    db = mc.get_db()

    assert db is not None
    assert db["label"] == "writable"
    assert secondary.closed is True
    assert created[0] == primary_uri
    assert "127.0.0.1:27018" in created[1]
    assert "127.0.0.1:27019" in created[2]
    assert direct_uri not in created
    state = mc.get_mongo_fallback_policy_state()
    assert state["active_fallback_kind"] == "ssh_tunnel"
    assert state["preferred_fallback_kind"] == "ssh_tunnel"
    assert state["local_fallback_allowed"] is False


def test_fallback_order_is_cost_sensitive_to_failure_kind(monkeypatch) -> None:
    monkeypatch.setattr(
        mc, "MONGO_URI", "mongodb+srv://user:pass@cluster.example/"
    )
    monkeypatch.setattr(
        mc,
        "MONGO_DNS_FALLBACK_URI",
        "mongodb://direct.example:27017/?tls=true",
    )
    monkeypatch.setattr(
        mc,
        "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS",
        ("127.0.0.1:27018", "127.0.0.1:27019"),
    )
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)

    dns_failure_order = [
        target.kind for target in mc._connection_fallback_targets(prefer_dns=True)
    ]
    route_failure_order = [
        target.kind for target in mc._connection_fallback_targets(prefer_dns=False)
    ]

    assert dns_failure_order == ["dns", "ssh_tunnel", "ssh_tunnel"]
    assert route_failure_order == ["ssh_tunnel", "ssh_tunnel", "dns"]


def test_unwritable_or_foreign_tunnels_fall_through_without_local_mongo(
    monkeypatch,
) -> None:
    _set_real_db_test_state(monkeypatch)
    primary_uri = "mongodb+srv://user:pass@cluster.example/"
    direct_uri = "mongodb://direct.example:27017/?tls=true"
    wrong_set = _FakeClient(
        "wrong-set",
        {"ok": 1, "setName": "foreign-set", "isWritablePrimary": True},
    )
    secondary = _FakeClient(
        "secondary",
        {"ok": 1, "setName": "atlas-set", "isWritablePrimary": False},
    )
    direct = _FakeClient("direct", {"ok": 1, "isWritablePrimary": True})
    created: list[str] = []

    def _fake_new_client(uri: str, **_kwargs):
        created.append(uri)
        if uri == primary_uri:
            raise ConnectionFailure("connection reset by peer")
        if "127.0.0.1:27018" in uri:
            return wrong_set
        if "127.0.0.1:27019" in uri:
            return secondary
        if uri == direct_uri:
            return direct
        raise AssertionError(f"Unexpected URI: {mc._host_display_from_uri(uri)}")

    monkeypatch.setattr(mc, "_new_mongo_client", _fake_new_client)
    monkeypatch.setattr(mc, "MONGO_URI", primary_uri)
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", direct_uri)
    monkeypatch.setattr(
        mc,
        "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS",
        ("127.0.0.1:27018", "127.0.0.1:27019"),
    )
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET", "atlas-set")
    monkeypatch.setattr(
        mc,
        "MONGO_LOCAL_URI",
        "mongodb://127.0.0.1:27017/?directConnection=true",
    )

    db = mc.get_db()

    assert db is not None
    assert db["label"] == "direct"
    assert wrong_set.closed is True
    assert secondary.closed is True
    assert created[-1] == direct_uri
    assert all("127.0.0.1:27017" not in uri for uri in created)
    assert mc.get_mongo_fallback_policy_state()["active_fallback_kind"] == "dns"


def test_all_tunnel_failures_never_downgrade_to_disabled_local_mongo(
    monkeypatch,
) -> None:
    _set_real_db_test_state(monkeypatch)
    primary_uri = "mongodb://remote.example:27017/?tls=true"
    created: list[str] = []

    def _fake_new_client(uri: str, **_kwargs):
        created.append(uri)
        raise ConnectionFailure("simulated route failure")

    monkeypatch.setattr(mc, "_new_mongo_client", _fake_new_client)
    monkeypatch.setattr(mc, "MONGO_URI", primary_uri)
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", None)
    monkeypatch.setattr(
        mc,
        "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS",
        ("127.0.0.1:27018", "127.0.0.1:27019"),
    )
    monkeypatch.setattr(
        mc,
        "MONGO_LOCAL_URI",
        "mongodb://127.0.0.1:27017/?directConnection=true",
    )

    assert mc.get_db() is None
    assert len(created) == 3
    assert all("127.0.0.1:27017" not in uri for uri in created)
    assert mc.is_using_fallback_uri() is False


def test_active_tunnel_reselects_after_replica_election(monkeypatch) -> None:
    _set_real_db_test_state(monkeypatch)
    old_primary = _FakeClient(
        "old-primary",
        {"ok": 1, "setName": "atlas-set", "isWritablePrimary": False},
    )
    secondary = _FakeClient(
        "secondary",
        {"ok": 1, "setName": "atlas-set", "isWritablePrimary": False},
    )
    new_primary = _FakeClient(
        "new-primary",
        {"ok": 1, "setName": "atlas-set", "isWritablePrimary": True},
    )
    endpoint_clients = iter((secondary, new_primary))

    def _fake_new_client(uri: str, **_kwargs):
        if "127.0.0.1:" in uri:
            return next(endpoint_clients)
        raise AssertionError("Direct primary must not be probed inside sticky window")

    monkeypatch.setattr(mc, "_new_mongo_client", _fake_new_client)
    monkeypatch.setattr(mc, "MONGO_URI", "mongodb+srv://user:pass@cluster.example/")
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", None)
    monkeypatch.setattr(
        mc,
        "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS",
        ("127.0.0.1:27018", "127.0.0.1:27019"),
    )
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET", "atlas-set")
    monkeypatch.setattr(mc, "_mongo_client_real", old_primary)
    monkeypatch.setattr(mc, "_using_fallback_real", True)
    monkeypatch.setattr(mc, "_active_fallback_kind_real", "ssh_tunnel")
    monkeypatch.setattr(mc, "_preferred_fallback_kind", "ssh_tunnel")
    monkeypatch.setattr(
        mc, "_dns_fallback_preferred_until_monotonic", time.monotonic() + 60
    )
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY_CHECK_INTERVAL_SECONDS", "0.001")

    db = mc.get_db()

    assert db is not None
    assert db["label"] == "new-primary"
    assert secondary.closed is True
    assert mc.get_mongo_fallback_policy_state()["active_fallback_kind"] == "ssh_tunnel"


def test_healthy_tunnel_returns_to_direct_primary_after_sticky_window(
    monkeypatch,
) -> None:
    _set_real_db_test_state(monkeypatch)
    tunnel_client = _FakeClient(
        "tunnel",
        {"ok": 1, "setName": "atlas-set", "isWritablePrimary": True},
    )
    primary_client = _FakeClient("direct", {"ok": 1, "isWritablePrimary": True})
    primary_uri = "mongodb+srv://user:pass@cluster.example/"

    monkeypatch.setattr(
        mc,
        "_new_mongo_client",
        lambda uri, **_kwargs: (
            primary_client
            if uri == primary_uri
            else (_ for _ in ()).throw(AssertionError("Unexpected fallback probe"))
        ),
    )
    monkeypatch.setattr(mc, "MONGO_URI", primary_uri)
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", None)
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS", ())
    monkeypatch.setattr(mc, "_mongo_client_real", tunnel_client)
    monkeypatch.setattr(mc, "_using_fallback_real", True)
    monkeypatch.setattr(mc, "_active_fallback_kind_real", "ssh_tunnel")
    monkeypatch.setattr(mc, "_preferred_fallback_kind", "ssh_tunnel")
    monkeypatch.setattr(mc, "_dns_fallback_preferred_until_monotonic", 0.0)
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY_CHECK_INTERVAL_SECONDS", "0.001")

    db = mc.get_db()

    assert db is not None
    assert db["label"] == "direct"
    assert tunnel_client.closed is False
    assert mc.is_using_fallback_uri() is False
    assert mc.get_mongo_fallback_policy_state()["active_fallback_kind"] is None
