from __future__ import annotations

import pytest

from src.backend.services.mongo_startup_config import (
    collect_mongo_startup_errors,
    run_mongo_startup_probe,
    validate_mongo_startup_or_raise,
)


def _set_strict_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VON_MONGO_STRICT_STARTUP", "1")
    monkeypatch.setenv("VON_MONGO_REQUIRE_TLS", "1")
    monkeypatch.setenv("MONGO_ALLOW_LOCAL_FALLBACK", "0")
    monkeypatch.setenv("VON_MONGO_ALLOWED_HOST_SUFFIXES", ".mongodb.net")


def test_collect_errors_empty_when_strict_startup_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_MONGO_STRICT_STARTUP", "0")
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.delenv("MONGO_URI_FILE", raising=False)

    assert collect_mongo_startup_errors() == []


def test_collect_errors_reports_missing_uri_in_strict_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_strict_defaults(monkeypatch)
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.delenv("MONGO_URI_FILE", raising=False)

    errors = collect_mongo_startup_errors()
    assert any("Missing MONGO_URI" in item for item in errors)


def test_collect_errors_accepts_valid_file_based_srv_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _set_strict_defaults(monkeypatch)
    secret_file = tmp_path / "mongo_uri.txt"
    secret_file.write_text(
        "mongodb+srv://appuser:secret@cluster0.mongodb.net/von_db?retryWrites=true&w=majority",
        encoding="utf-8",
    )
    monkeypatch.setenv("MONGO_URI_FILE", str(secret_file))
    monkeypatch.delenv("MONGO_URI", raising=False)

    assert collect_mongo_startup_errors() == []


def test_collect_errors_rejects_localhost_and_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_strict_defaults(monkeypatch)
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017/?tls=true")
    monkeypatch.setenv("MONGO_ALLOW_LOCAL_FALLBACK", "1")

    errors = collect_mongo_startup_errors()
    assert any("must not target localhost" in item for item in errors)
    assert any("MONGO_ALLOW_LOCAL_FALLBACK must be disabled" in item for item in errors)


def test_collect_errors_requires_tls_for_mongodb_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_strict_defaults(monkeypatch)
    monkeypatch.setenv("MONGO_URI", "mongodb://cluster0.mongodb.net/von_db")

    errors = collect_mongo_startup_errors()
    assert any("must enable TLS explicitly for mongodb://" in item for item in errors)


def test_validate_mongo_startup_raises_with_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_strict_defaults(monkeypatch)
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.delenv("MONGO_URI_FILE", raising=False)

    with pytest.raises(RuntimeError, match="Mongo startup validation failed"):
        validate_mongo_startup_or_raise()


class _FakeCollection:
    def __init__(self, should_fail_read: bool = False) -> None:
        self._docs: dict[str, dict[str, str]] = {}
        self._should_fail_read = should_fail_read

    def find_one(self, query: dict, projection: dict | None = None):
        if self._should_fail_read:
            raise RuntimeError("read failed")
        return self._docs.get(query.get("_id")) if "_id" in query else {"_id": "seed"}

    def update_one(self, query: dict, update: dict, upsert: bool = False) -> None:
        doc = update.get("$set", {})
        self._docs[query["_id"]] = {"_id": query["_id"], **doc}

    def delete_one(self, query: dict) -> None:
        self._docs.pop(query.get("_id"), None)


class _FakeDb:
    def __init__(self, read_should_fail: bool = False) -> None:
        self._collections: dict[str, _FakeCollection] = {
            "application_settings": _FakeCollection(should_fail_read=read_should_fail),
            "_von_startup_probe": _FakeCollection(),
        }

    def command(self, name: str) -> dict[str, int]:
        if name != "ping":
            raise RuntimeError("unexpected command")
        return {"ok": 1}

    def __getitem__(self, name: str) -> _FakeCollection:
        if name not in self._collections:
            self._collections[name] = _FakeCollection()
        return self._collections[name]


def test_run_startup_probe_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.backend.db.mongo_client.get_db", lambda: _FakeDb())
    monkeypatch.setenv("VON_MONGO_READ_PROBE_COLLECTION", "application_settings")
    monkeypatch.setenv("VON_MONGO_STARTUP_PROBE_COLLECTION", "_von_startup_probe")
    monkeypatch.setenv("VON_MONGO_STARTUP_PROBE_WRITE", "1")

    result = run_mongo_startup_probe()
    assert result["ok"] is True
    assert result["read_ok"] is True
    assert result["write_ok"] is True
    assert result["read_collection"] == "application_settings"
    assert result["write_collection"] == "_von_startup_probe"


def test_run_startup_probe_surfaces_read_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.db.mongo_client.get_db",
        lambda: _FakeDb(read_should_fail=True),
    )
    monkeypatch.setenv("VON_MONGO_READ_PROBE_COLLECTION", "application_settings")

    with pytest.raises(RuntimeError, match="read check failed"):
        run_mongo_startup_probe()
