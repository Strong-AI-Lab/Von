"""Tests for the db/info TTL cache helpers (JVNAUTOSCI-2383)."""

from __future__ import annotations

import pytest

from src.backend.server.routes import settings_routes


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    with settings_routes._DB_INFO_CACHE_LOCK:
        settings_routes._DB_INFO_CACHE.clear()
    yield
    with settings_routes._DB_INFO_CACHE_LOCK:
        settings_routes._DB_INFO_CACHE.clear()


def test_cache_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VON_DB_INFO_CACHE_TTL_SECONDS", "60")
    payload = {"ping_ok": True, "classification": "atlas"}
    settings_routes._write_cached_db_info(payload)
    cached = settings_routes._read_cached_db_info(bypass_cache=False)
    assert cached == payload
    # Returned copy must be independent of the stored payload.
    assert cached is not payload


def test_cache_bypass_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VON_DB_INFO_CACHE_TTL_SECONDS", "60")
    settings_routes._write_cached_db_info({"ping_ok": True})
    assert settings_routes._read_cached_db_info(bypass_cache=True) is None


def test_cache_disabled_when_ttl_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VON_DB_INFO_CACHE_TTL_SECONDS", "0")
    settings_routes._write_cached_db_info({"ping_ok": True})
    assert settings_routes._read_cached_db_info(bypass_cache=False) is None


def test_cache_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VON_DB_INFO_CACHE_TTL_SECONDS", "60")
    settings_routes._write_cached_db_info({"ping_ok": True})

    real_monotonic = settings_routes.time.monotonic
    monkeypatch.setattr(
        settings_routes.time,
        "monotonic",
        lambda: real_monotonic() + 120.0,
    )
    assert settings_routes._read_cached_db_info(bypass_cache=False) is None


def test_invalid_ttl_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VON_DB_INFO_CACHE_TTL_SECONDS", "not-a-number")
    assert (
        settings_routes._read_db_info_cache_ttl_seconds()
        == settings_routes._DB_INFO_CACHE_TTL_SECONDS_DEFAULT
    )
