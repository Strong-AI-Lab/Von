from __future__ import annotations

from src.backend.db import mongo_client as mc


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
