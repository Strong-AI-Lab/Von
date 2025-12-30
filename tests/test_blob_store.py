from __future__ import annotations

from pathlib import Path

import pytest

from src.backend.services.blob_store import (
    LocalBlobStore,
    _normalise_key,
    get_blob_store_from_env,
)


def test_normalise_key_rejects_empty():
    with pytest.raises(ValueError):
        _normalise_key("")


def test_normalise_key_rejects_absolute_or_parent_traversal():
    with pytest.raises(ValueError):
        _normalise_key("/etc/passwd")

    with pytest.raises(ValueError):
        _normalise_key("../secrets.txt")

    with pytest.raises(ValueError):
        _normalise_key("a/../b")


def test_local_blob_store_put_get_exists_delete_and_list(tmp_path: Path):
    store = LocalBlobStore(tmp_path)

    ref = store.put_bytes(
        "demo/thing.txt",
        b"hello",
        content_type="text/plain",
        metadata={"owner": "test"},
    )

    assert ref.backend == "local"
    assert store.exists("demo/thing.txt")
    assert store.get_bytes("demo/thing.txt") == b"hello"

    keys = store.list("demo")
    assert keys == ["demo/thing.txt"]

    store.delete("demo/thing.txt")
    assert not store.exists("demo/thing.txt")


def test_get_blob_store_from_env_defaults_to_local(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VON_BLOB_STORE_BACKEND", raising=False)
    monkeypatch.delenv("VON_BLOB_STORE_LOCAL_ROOT", raising=False)

    store = get_blob_store_from_env()
    assert isinstance(store, LocalBlobStore)


def test_get_blob_store_from_env_swift_requires_container(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "swift")
    monkeypatch.delenv("VON_SWIFT_CONTAINER", raising=False)

    with pytest.raises(ValueError):
        get_blob_store_from_env()
