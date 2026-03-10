from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backend.services.blob_store import (
    LocalBlobStore,
    SwiftBlobStore,
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


def test_swift_blob_store_get_bytes_uses_download_object_obj_signature():
    class _DummyObjectStore:
        def __init__(self):
            self.calls: list[tuple[object, object, object]] = []

        def download_object(self, obj, container=None, **attrs):
            self.calls.append((obj, container, attrs))
            return b"hello"

    class _DummyConn:
        def __init__(self):
            self.object_store = _DummyObjectStore()

    store = SwiftBlobStore.__new__(SwiftBlobStore)
    store._container = "demo-container"
    store._prefix = ""
    store._public_base_url = None
    store._cloud = None
    store._conn = _DummyConn()

    assert store.get_bytes("demo/thing.txt") == b"hello"
    assert store._conn.object_store.calls == [("demo/thing.txt", "demo-container", {})]


def test_swift_blob_store_exists_uses_get_object_obj_signature():
    class _DummyObjectStore:
        def __init__(self):
            self.calls: list[tuple[object, object]] = []

        def get_object(self, obj, container=None, **attrs):
            self.calls.append((obj, container))
            return {"name": obj}

    class _DummyConn:
        def __init__(self):
            self.object_store = _DummyObjectStore()

    store = SwiftBlobStore.__new__(SwiftBlobStore)
    store._container = "demo-container"
    store._prefix = ""
    store._public_base_url = None
    store._cloud = None
    store._conn = _DummyConn()

    assert store.exists("demo/thing.txt") is True
    assert store._conn.object_store.calls == [("demo/thing.txt", "demo-container")]


def test_swift_blob_store_delete_uses_delete_object_obj_signature():
    class _DummyObjectStore:
        def __init__(self):
            self.calls: list[tuple[object, object, object]] = []

        def delete_object(self, obj, ignore_missing=True, container=None, **attrs):
            self.calls.append((obj, container, ignore_missing))

    class _DummyConn:
        def __init__(self):
            self.object_store = _DummyObjectStore()

    store = SwiftBlobStore.__new__(SwiftBlobStore)
    store._container = "demo-container"
    store._prefix = ""
    store._public_base_url = None
    store._cloud = None
    store._conn = _DummyConn()

    store.delete("demo/thing.txt")
    assert store._conn.object_store.calls == [
        ("demo/thing.txt", "demo-container", True)
    ]


def test_swift_blob_store_cloud_not_found_falls_back_to_envvars(
    monkeypatch: pytest.MonkeyPatch,
):
    import importlib as _importlib

    class ConfigException(Exception):
        pass

    class _DummyConnection:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _DummyCloud:
        def __init__(self, name: str):
            self.name = name

    class _DummyOpenStackConfig:
        def get_all_clouds(self):
            return [_DummyCloud("envvars")]

    def _fake_connect(*, cloud):
        raise ConfigException(f"Cloud {cloud} was not found.")

    openstack_mod = SimpleNamespace(connect=_fake_connect)
    connection_mod = SimpleNamespace(Connection=_DummyConnection)
    config_mod = SimpleNamespace(OpenStackConfig=_DummyOpenStackConfig)

    real_import_module = _importlib.import_module

    def _fake_import_module(name: str, package: str | None = None):
        if name == "openstack":
            return openstack_mod
        if name == "openstack.connection":
            return connection_mod
        if name == "openstack.config":
            return config_mod
        return real_import_module(name, package)

    monkeypatch.setattr(_importlib, "import_module", _fake_import_module)
    monkeypatch.setenv("OS_AUTH_URL", "https://identity.example/v3")
    monkeypatch.setenv("OS_USERNAME", "demo-user")
    monkeypatch.setenv("OS_PASSWORD", "demo-pass")
    monkeypatch.setenv("OS_PROJECT_NAME", "demo-project")
    monkeypatch.setenv("OS_USER_DOMAIN_NAME", "Default")
    monkeypatch.setenv("OS_PROJECT_DOMAIN_NAME", "Default")

    store = SwiftBlobStore(container="demo-container", cloud="catalystcloud")
    assert isinstance(store._conn, _DummyConnection)
    assert store._conn.kwargs["auth_url"] == "https://identity.example/v3"
    assert store._conn.kwargs["username"] == "demo-user"
    assert store._conn.kwargs["project_name"] == "demo-project"
