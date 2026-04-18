from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backend.services.blob_store import (
    FailoverBlobStore,
    LocalBlobStore,
    S3BlobStore,
    SwiftBlobStore,
    _normalise_key,
    get_blob_store_from_env,
    resolve_blob_store_backend_from_env,
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
    monkeypatch.delenv("VON_SWIFT_CONTAINER", raising=False)
    monkeypatch.delenv("OS_CLOUD", raising=False)
    monkeypatch.delenv("OS_AUTH_URL", raising=False)
    monkeypatch.delenv("OS_USERNAME", raising=False)
    monkeypatch.delenv("OS_PASSWORD", raising=False)
    monkeypatch.delenv("OS_PROJECT_NAME", raising=False)
    monkeypatch.delenv("VON_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("VON_S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    store = get_blob_store_from_env()
    assert isinstance(store, LocalBlobStore)


def test_resolve_blob_store_backend_from_env_prefers_swift_when_remote_config_present(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("VON_BLOB_STORE_BACKEND", raising=False)
    monkeypatch.setenv("VON_SWIFT_CONTAINER", "von-artifacts")
    monkeypatch.setenv("OS_CLOUD", "catalyst")

    assert resolve_blob_store_backend_from_env() == "swift"


def test_get_blob_store_from_env_prefers_remote_failover_when_backend_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("VON_BLOB_STORE_BACKEND", raising=False)
    monkeypatch.setenv("VON_SWIFT_CONTAINER", "von-artifacts")
    monkeypatch.setenv("OS_CLOUD", "catalyst")
    monkeypatch.setenv(
        "VON_S3_ENDPOINT_URL", "https://object-storage.nz-por-1.catalystcloud.io"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "demo-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "demo-secret")

    class _SwiftSentinel:
        pass

    class _S3Sentinel:
        pass

    monkeypatch.setattr(
        "src.backend.services.blob_store.SwiftBlobStore",
        lambda **kwargs: _SwiftSentinel(),
    )
    monkeypatch.setattr(
        "src.backend.services.blob_store._build_s3_blob_store_from_env",
        lambda: _S3Sentinel(),
    )

    store = get_blob_store_from_env()
    assert isinstance(store, FailoverBlobStore)


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


def test_swift_blob_store_exists_returns_false_for_openstack_not_found():
    class _DummyResponse:
        status_code = 404

    class _NotFoundException(Exception):
        __module__ = "openstack.exceptions"

        def __init__(self, message: str, *, response=None):
            super().__init__(message)
            self.response = response

    class _DummyObjectStore:
        def get_object(self, obj, container=None, **attrs):
            raise _NotFoundException("Not Found", response=_DummyResponse())

    class _DummyConn:
        def __init__(self):
            self.object_store = _DummyObjectStore()

    store = SwiftBlobStore.__new__(SwiftBlobStore)
    store._container = "demo-container"
    store._prefix = ""
    store._public_base_url = None
    store._cloud = None
    store._conn = _DummyConn()

    assert store.exists("missing.txt") is False


def test_swift_blob_store_delete_then_exists_returns_false():
    class _NotFoundException(Exception):
        __module__ = "openstack.exceptions"

        def __init__(self, message: str):
            super().__init__(message)
            self.http_status = 404

    class _DummyObjectStore:
        def __init__(self):
            self.objects: set[str] = set()

        def create_object(
            self,
            *,
            container=None,
            name=None,
            data=None,
            content_type=None,
            metadata=None,
        ):
            self.objects.add(str(name))

        def get_object(self, obj, container=None, **attrs):
            if obj not in self.objects:
                raise _NotFoundException("Not Found")
            return {"name": obj}

        def delete_object(self, obj, ignore_missing=True, container=None, **attrs):
            self.objects.discard(str(obj))

    class _DummyConn:
        def __init__(self):
            self.object_store = _DummyObjectStore()

    store = SwiftBlobStore.__new__(SwiftBlobStore)
    store._container = "demo-container"
    store._prefix = ""
    store._public_base_url = None
    store._cloud = None
    store._conn = _DummyConn()

    store.put_bytes("demo/thing.txt", b"hello")
    assert store.exists("demo/thing.txt") is True

    store.delete("demo/thing.txt")
    assert store.exists("demo/thing.txt") is False


def test_swift_blob_store_exists_propagates_non_not_found_failures():
    class _TransportException(Exception):
        __module__ = "openstack.exceptions"

        def __init__(self, message: str):
            super().__init__(message)
            self.http_status = 503

    class _DummyObjectStore:
        def get_object(self, obj, container=None, **attrs):
            raise _TransportException("Service unavailable")

    class _DummyConn:
        def __init__(self):
            self.object_store = _DummyObjectStore()

    store = SwiftBlobStore.__new__(SwiftBlobStore)
    store._container = "demo-container"
    store._prefix = ""
    store._public_base_url = None
    store._cloud = None
    store._conn = _DummyConn()

    with pytest.raises(_TransportException, match="Service unavailable"):
        store.exists("demo/thing.txt")


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

    class _DummyOpenStackConfig:
        def get_cloud_names(self):
            return ["envvars"]

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


def test_swift_blob_store_cloud_not_found_falls_back_to_application_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    import importlib as _importlib

    class ConfigException(Exception):
        pass

    class _DummyConnection:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _DummyOpenStackConfig:
        def get_cloud_names(self):
            return ["envvars"]

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
    monkeypatch.setenv("OS_AUTH_URL", "https://api.nz-por-1.catalystcloud.io:5000/v3")
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_ID", "dummy-app-cred-id")
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_SECRET", "dummy-app-cred-secret")
    monkeypatch.setenv("OS_REGION_NAME", "nz-por-1")

    store = SwiftBlobStore(container="demo-container", cloud="catalystcloud")
    assert isinstance(store._conn, _DummyConnection)
    assert (
        store._conn.kwargs["auth_url"]
        == "https://api.nz-por-1.catalystcloud.io:5000/v3"
    )
    assert store._conn.kwargs["auth_type"] == "v3applicationcredential"
    assert store._conn.kwargs["application_credential_id"] == "dummy-app-cred-id"
    assert (
        store._conn.kwargs["application_credential_secret"]
        == "dummy-app-cred-secret"
    )
    assert store._conn.kwargs["region_name"] == "nz-por-1"


def test_swift_blob_store_put_bytes_classifies_network_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    class _DummyObjectStore:
        def create_object(self, **kwargs):
            raise RuntimeError("Max retries exceeded with url: /v1/AUTH_demo")

    class _DummyConn:
        def __init__(self):
            self.object_store = _DummyObjectStore()

    monkeypatch.setenv("OS_AUTH_URL", "https://api.nz-por-1.catalystcloud.io:5000/v3")
    monkeypatch.setenv("OS_REGION_NAME", "nz-por-1")

    store = SwiftBlobStore.__new__(SwiftBlobStore)
    store._container = "demo-container"
    store._prefix = ""
    store._public_base_url = None
    store._cloud = "catalystcloud"
    store._conn = _DummyConn()

    with pytest.raises(RuntimeError) as exc_info:
        store.put_bytes("demo/thing.txt", b"hello")

    message = str(exc_info.value)
    assert "network/connectivity failure" in message
    assert "not a local clouds.yaml/profile lookup failure" in message
    assert "nz-por-1" in message


def test_s3_blob_store_methods_use_s3_client_signatures():
    class _DummyBody:
        def read(self):
            return b"hello"

    class _DummyPaginator:
        def paginate(self, **kwargs):
            assert kwargs == {"Bucket": "demo-bucket", "Prefix": "demo/"}
            return [
                {
                    "Contents": [
                        {"Key": "demo/alpha.txt"},
                        {"Key": "demo/nested/beta.txt"},
                    ]
                }
            ]

    class _DummyClient:
        def __init__(self):
            self.put_calls = []
            self.get_calls = []
            self.head_calls = []
            self.delete_calls = []

        def put_object(self, **kwargs):
            self.put_calls.append(kwargs)

        def get_object(self, **kwargs):
            self.get_calls.append(kwargs)
            return {"Body": _DummyBody()}

        def head_object(self, **kwargs):
            self.head_calls.append(kwargs)
            return {"ETag": "etag"}

        def delete_object(self, **kwargs):
            self.delete_calls.append(kwargs)

        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return _DummyPaginator()

    store = S3BlobStore.__new__(S3BlobStore)
    store._bucket = "demo-bucket"
    store._endpoint_url = "https://object-storage.nz-por-1.catalystcloud.io"
    store._prefix = "demo"
    store._public_base_url = None
    store._region_name = "nz-por-1"
    store._access_key_id = "key"
    store._secret_access_key = "secret"
    store._session_token = None
    store._addressing_style = "path"
    store._client = _DummyClient()

    ref = store.put_bytes(
        "alpha.txt",
        b"hello",
        content_type="text/plain",
        metadata={"owner": "test"},
    )
    assert ref.backend == "s3"
    assert (
        ref.uri
        == "https://object-storage.nz-por-1.catalystcloud.io/demo-bucket/demo/alpha.txt"
    )
    assert store._client.put_calls == [
        {
            "Bucket": "demo-bucket",
            "Key": "demo/alpha.txt",
            "Body": b"hello",
            "ContentType": "text/plain",
            "Metadata": {"owner": "test"},
        }
    ]

    assert store.get_bytes("alpha.txt") == b"hello"
    assert store._client.get_calls == [
        {"Bucket": "demo-bucket", "Key": "demo/alpha.txt"}
    ]

    assert store.exists("alpha.txt") is True
    assert store._client.head_calls == [
        {"Bucket": "demo-bucket", "Key": "demo/alpha.txt"}
    ]

    store.delete("alpha.txt")
    assert store._client.delete_calls == [
        {"Bucket": "demo-bucket", "Key": "demo/alpha.txt"}
    ]

    assert store.list() == ["alpha.txt", "nested/beta.txt"]


def test_s3_blob_store_exists_returns_false_for_missing_object():
    class _MissingObject(Exception):
        def __init__(self):
            self.response = {
                "Error": {"Code": "404"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }

    class _DummyClient:
        def head_object(self, **kwargs):
            raise _MissingObject()

    store = S3BlobStore.__new__(S3BlobStore)
    store._bucket = "demo-bucket"
    store._endpoint_url = "https://object-storage.nz-por-1.catalystcloud.io"
    store._prefix = ""
    store._public_base_url = None
    store._region_name = "nz-por-1"
    store._access_key_id = "key"
    store._secret_access_key = "secret"
    store._session_token = None
    store._addressing_style = "path"
    store._client = _DummyClient()

    assert store.exists("missing.txt") is False


def test_get_blob_store_from_env_s3_supports_swift_container_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    import importlib as _importlib

    captured_client_kwargs: dict[str, object] = {}

    class _DummySession:
        def client(self, service_name, **kwargs):
            assert service_name == "s3"
            captured_client_kwargs.update(kwargs)
            return SimpleNamespace()

    class _DummyConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    boto3_mod = SimpleNamespace(session=SimpleNamespace(Session=lambda: _DummySession()))
    config_mod = SimpleNamespace(Config=_DummyConfig)
    real_import_module = _importlib.import_module

    def _fake_import_module(name: str, package: str | None = None):
        if name == "boto3":
            return boto3_mod
        if name == "botocore.config":
            return config_mod
        return real_import_module(name, package)

    monkeypatch.setattr(_importlib, "import_module", _fake_import_module)
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "s3")
    monkeypatch.setenv("VON_SWIFT_CONTAINER", "von-artifacts")
    monkeypatch.setenv(
        "VON_S3_ENDPOINT_URL", "https://object-storage.nz-por-1.catalystcloud.io"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "demo-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "demo-secret")
    monkeypatch.setenv("OS_REGION_NAME", "nz-por-1")
    monkeypatch.setenv("VON_SWIFT_PREFIX", "von")

    store = get_blob_store_from_env()
    assert isinstance(store, S3BlobStore)
    assert store._bucket == "von-artifacts"
    assert store._prefix == "von"
    assert captured_client_kwargs["endpoint_url"] == (
        "https://object-storage.nz-por-1.catalystcloud.io"
    )
    assert captured_client_kwargs["region_name"] == "nz-por-1"
    assert captured_client_kwargs["aws_access_key_id"] == "demo-key"


def test_get_blob_store_from_env_swift_can_fail_over_to_s3_on_initialisation_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "swift")
    monkeypatch.setenv("VON_SWIFT_CONTAINER", "von-artifacts")
    monkeypatch.setenv(
        "VON_S3_ENDPOINT_URL", "https://object-storage.nz-por-1.catalystcloud.io"
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "demo-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "demo-secret")

    monkeypatch.setattr(
        "src.backend.services.blob_store.SwiftBlobStore",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Connect timeout to :5000")),
    )

    sentinel = SimpleNamespace(kind="s3")
    monkeypatch.setattr(
        "src.backend.services.blob_store._build_s3_blob_store_from_env",
        lambda: sentinel,
    )

    store = get_blob_store_from_env()
    assert store is sentinel


def test_failover_blob_store_uses_secondary_after_transport_failure():
    class _Primary:
        def __init__(self):
            self.calls = []

        def put_bytes(self, key, data, *, content_type=None, metadata=None):
            raise AssertionError("unexpected")

        def get_bytes(self, key):
            self.calls.append(key)
            raise RuntimeError("Connect timeout to Keystone on :5000")

        def exists(self, key):
            raise AssertionError("unexpected")

        def delete(self, key):
            raise AssertionError("unexpected")

        def list(self, prefix=""):
            raise AssertionError("unexpected")

    class _Secondary:
        def __init__(self):
            self.calls = []

        def put_bytes(self, key, data, *, content_type=None, metadata=None):
            raise AssertionError("unexpected")

        def get_bytes(self, key):
            self.calls.append(key)
            return b"hello"

        def exists(self, key):
            raise AssertionError("unexpected")

        def delete(self, key):
            raise AssertionError("unexpected")

        def list(self, prefix=""):
            raise AssertionError("unexpected")

    primary = _Primary()
    secondary = _Secondary()
    store = FailoverBlobStore(
        primary=primary,
        secondary=secondary,
        primary_name="swift",
        secondary_name="s3",
    )

    assert store.get_bytes("demo.txt") == b"hello"
    assert store.get_bytes("demo.txt") == b"hello"
    assert primary.calls == ["demo.txt"]
    assert secondary.calls == ["demo.txt", "demo.txt"]


def test_failover_blob_store_does_not_fail_over_on_not_found():
    class _Primary:
        def put_bytes(self, key, data, *, content_type=None, metadata=None):
            raise AssertionError("unexpected")

        def get_bytes(self, key):
            raise AssertionError("unexpected")

        def exists(self, key):
            raise RuntimeError("404 Not Found")

        def delete(self, key):
            raise AssertionError("unexpected")

        def list(self, prefix=""):
            raise AssertionError("unexpected")

    class _Secondary:
        def __init__(self):
            self.called = False

        def put_bytes(self, key, data, *, content_type=None, metadata=None):
            raise AssertionError("unexpected")

        def get_bytes(self, key):
            raise AssertionError("unexpected")

        def exists(self, key):
            self.called = True
            return True

        def delete(self, key):
            raise AssertionError("unexpected")

        def list(self, prefix=""):
            raise AssertionError("unexpected")

    secondary = _Secondary()
    store = FailoverBlobStore(
        primary=_Primary(),
        secondary=secondary,
        primary_name="swift",
        secondary_name="s3",
    )

    with pytest.raises(RuntimeError, match="404 Not Found"):
        store.exists("demo.txt")
    assert secondary.called is False
