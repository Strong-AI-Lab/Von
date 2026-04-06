from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Protocol


@dataclass(frozen=True)
class BlobRef:
    backend: str
    key: str
    uri: str
    content_type: str | None = None
    size_bytes: int | None = None
    etag: str | None = None
    metadata: dict[str, str] | None = None


class BlobStore(Protocol):
    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str = "") -> list[str]: ...


def _normalise_key(key: str) -> str:
    key = key.strip().replace("\\", "/")
    if not key:
        raise ValueError("Blob key must not be empty")

    posix = PurePosixPath(key)
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        raise ValueError(f"Unsafe blob key: {key!r}")

    # Avoid leading './'
    key = str(posix)
    while key.startswith("./"):
        key = key[2:]

    if not key or key == ".":
        raise ValueError("Blob key must not be empty")

    return key


class LocalBlobStore:
    def __init__(self, root_dir: Path):
        self._root_dir = root_dir

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    def _resolve_path(self, key: str) -> Path:
        safe_key = _normalise_key(key)
        return self._root_dir / safe_key

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        path = self._resolve_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

        # Metadata is stored as a sibling JSON file. This is intentionally simple and
        # cross-platform; it also keeps the abstraction compatible with object stores.
        if metadata:
            meta_path = path.with_suffix(path.suffix + ".meta.json")
            import json

            meta_path.write_text(json.dumps(dict(metadata), indent=2), encoding="utf-8")

        return BlobRef(
            backend="local",
            key=_normalise_key(key),
            uri=str(path),
            content_type=content_type,
            size_bytes=len(data),
            metadata=dict(metadata) if metadata else None,
        )

    def get_bytes(self, key: str) -> bytes:
        path = self._resolve_path(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve_path(key).exists()

    def delete(self, key: str) -> None:
        path = self._resolve_path(key)
        if path.exists():
            path.unlink()

        meta_path = path.with_suffix(path.suffix + ".meta.json")
        if meta_path.exists():
            meta_path.unlink()

    def list(self, prefix: str = "") -> list[str]:
        safe_prefix = _normalise_key(prefix) if prefix else ""
        base = self._root_dir / safe_prefix
        if not base.exists():
            return []

        keys: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(".meta.json"):
                continue

            rel = path.relative_to(self._root_dir).as_posix()
            keys.append(rel)

        keys.sort()
        return keys


class SwiftBlobStore:
    """OpenStack Swift-backed blob store.

    This is intentionally a generic capability (not arXiv-specific). arXiv is simply
    one client that stores PDFs here.

    Authentication uses standard OpenStack environment variables (OS_*) or OS_CLOUD.

    Required env vars (unless using OS_CLOUD):
    - OS_AUTH_URL
    - OS_USERNAME
    - OS_PASSWORD
    - OS_PROJECT_NAME
    - OS_USER_DOMAIN_NAME (often 'Default')
    - OS_PROJECT_DOMAIN_NAME (often 'Default')

    Required for this store:
    - VON_SWIFT_CONTAINER

    Optional:
    - VON_SWIFT_PREFIX (e.g., 'von/')
    - VON_SWIFT_PUBLIC_BASE_URL (used to construct a stable HTTP URL if available)
    - OS_REGION_NAME
    """

    def __init__(
        self,
        *,
        container: str,
        prefix: str = "",
        public_base_url: str | None = None,
        cloud: str | None = None,
    ):
        self._container = container
        self._prefix = prefix.strip("/")
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._cloud = cloud

        self._conn = self._create_connection()

    def _create_connection_from_envvars(self, connection_mod):
        def _require(name: str) -> str:
            value = os.environ.get(name)
            if not value:
                raise ValueError(
                    f"Missing required environment variable for Swift auth: {name}"
                )
            return value

        auth_url = _require("OS_AUTH_URL")
        username = _require("OS_USERNAME")
        password = _require("OS_PASSWORD")
        project_name = _require("OS_PROJECT_NAME")
        user_domain_name = os.environ.get("OS_USER_DOMAIN_NAME", "Default")
        project_domain_name = os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")
        region_name = os.environ.get("OS_REGION_NAME")

        return connection_mod.Connection(
            auth_url=auth_url,
            username=username,
            password=password,
            project_name=project_name,
            user_domain_name=user_domain_name,
            project_domain_name=project_domain_name,
            region_name=region_name,
        )

    def _create_connection(self):
        # Import lazily via importlib so type-checking doesn't require
        # openstacksdk unless Swift support is actually used.
        try:
            import importlib

            openstack = importlib.import_module("openstack")
            connection_mod = importlib.import_module("openstack.connection")
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "OpenStack Swift backend requires 'openstacksdk' in the active "
                "runtime environment. Rebuild the release virtualenv and ensure "
                "the dependency is installed (for local development, install it "
                "via PDM)."
            ) from exc

        if self._cloud:
            try:
                return openstack.connect(cloud=self._cloud)
            except Exception as exc:
                # openstacksdk raises ConfigException when OS_CLOUD doesn't match any
                # configured clouds. Provide a more actionable error message.
                if exc.__class__.__name__ == "ConfigException":
                    available: list[str] = []
                    try:
                        config_mod = importlib.import_module("openstack.config")
                        OpenStackConfig = getattr(config_mod, "OpenStackConfig")
                        cfg = OpenStackConfig()
                        clouds = cfg.get_all_clouds()
                        names = [
                            getattr(c, "name", None)
                            for c in clouds
                            if getattr(c, "name", None)
                        ]
                        available = sorted({n for n in names if isinstance(n, str)})
                    except Exception:
                        available = []

                    # If explicit cloud profile lookup fails but envvars profile is
                    # present, fall back to environment-variable auth. This keeps
                    # uploads resilient when OS_CLOUD is stale/mistyped.
                    if "envvars" in {str(name).strip().lower() for name in available}:
                        try:
                            return self._create_connection_from_envvars(connection_mod)
                        except Exception as env_exc:
                            msg = (
                                "OpenStack cloud was not found for OS_CLOUD="
                                f"{self._cloud!r}, and fallback to env-var auth failed: "
                                f"{env_exc}. "
                                "Set OS_CLOUD to a configured cloud name in clouds.yaml "
                                "(or set OS_CLIENT_CONFIG_FILE to point at clouds.yaml), "
                                "or configure OS_AUTH_URL/OS_USERNAME/etc instead."
                            )
                            if available:
                                msg += f" Available clouds: {available!r}."
                            raise ValueError(msg) from env_exc

                    msg = (
                        "OpenStack cloud was not found for OS_CLOUD="
                        f"{self._cloud!r}. "
                        "Set OS_CLOUD to a configured cloud name in clouds.yaml "
                        "(or set OS_CLIENT_CONFIG_FILE to point at clouds.yaml), "
                        "or configure OS_AUTH_URL/OS_USERNAME/etc instead."
                    )
                    if available:
                        msg += f" Available clouds: {available!r}."
                    raise ValueError(msg) from exc
                raise

        return self._create_connection_from_envvars(connection_mod)

    def _full_key(self, key: str) -> str:
        safe_key = _normalise_key(key)
        if not self._prefix:
            return safe_key
        return f"{self._prefix}/{safe_key}"

    def _build_uri(self, key: str) -> str:
        full_key = self._full_key(key)
        if self._public_base_url:
            return f"{self._public_base_url}/{self._container}/{full_key}"
        return f"swift://{self._container}/{full_key}"

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        full_key = self._full_key(key)

        # openstacksdk supports both bytes and file-like objects.
        self._conn.object_store.create_object(
            container=self._container,
            name=full_key,
            data=data,
            content_type=content_type,
            metadata=dict(metadata) if metadata else None,
        )

        return BlobRef(
            backend="swift",
            key=_normalise_key(key),
            uri=self._build_uri(key),
            content_type=content_type,
            size_bytes=len(data),
            metadata=dict(metadata) if metadata else None,
        )

    def get_bytes(self, key: str) -> bytes:
        full_key = self._full_key(key)
        # openstacksdk's proxy API expects the object identifier as the first
        # positional argument ('obj'). Some older versions also accepted 'name='
        # keyword usage; keep a fallback for compatibility.
        try:
            return self._conn.object_store.download_object(
                full_key,
                container=self._container,
            )
        except TypeError:
            return self._conn.object_store.download_object(
                name=full_key,
                container=self._container,
            )

    def exists(self, key: str) -> bool:
        full_key = self._full_key(key)
        try:
            obj = self._conn.object_store.get_object(
                full_key,
                container=self._container,
            )
        except TypeError:
            obj = self._conn.object_store.get_object(
                name=full_key,
                container=self._container,
            )
        return obj is not None

    def delete(self, key: str) -> None:
        full_key = self._full_key(key)
        try:
            self._conn.object_store.delete_object(
                full_key,
                container=self._container,
                ignore_missing=True,
            )
        except TypeError:
            self._conn.object_store.delete_object(
                name=full_key,
                container=self._container,
                ignore_missing=True,
            )

    def list(self, prefix: str = "") -> list[str]:
        safe_prefix = _normalise_key(prefix) if prefix else ""
        full_prefix = self._full_key(safe_prefix) if safe_prefix else self._prefix
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix = full_prefix + "/"

        keys: list[str] = []
        for obj in self._conn.object_store.objects(
            container=self._container,
            prefix=full_prefix or None,
        ):
            name = getattr(obj, "name", None)
            if not name or not isinstance(name, str):
                continue

            # Strip store-level prefix to return stable keys.
            if self._prefix:
                store_prefix = self._prefix + "/"
                if name.startswith(store_prefix):
                    name = name[len(store_prefix) :]

            keys.append(_normalise_key(name))

        keys.sort()
        return keys


def get_blob_store_from_env() -> BlobStore:
    backend = (os.environ.get("VON_BLOB_STORE_BACKEND") or "local").strip().lower()

    if backend == "local":
        root = os.environ.get("VON_BLOB_STORE_LOCAL_ROOT")
        if root:
            root_dir = Path(root)
        else:
            workspace_root = Path(__file__).parent.parent.parent.parent
            root_dir = workspace_root / "data" / "blob_store"
        return LocalBlobStore(root_dir)

    if backend == "swift":
        container = os.environ.get("VON_SWIFT_CONTAINER")
        if not container:
            raise ValueError("VON_SWIFT_CONTAINER is required when backend=swift")

        prefix = os.environ.get("VON_SWIFT_PREFIX", "")
        public_base_url = os.environ.get("VON_SWIFT_PUBLIC_BASE_URL")
        cloud = os.environ.get("OS_CLOUD")

        return SwiftBlobStore(
            container=container,
            prefix=prefix,
            public_base_url=public_base_url,
            cloud=cloud,
        )

    raise ValueError(
        "Unsupported VON_BLOB_STORE_BACKEND. Expected 'local' or 'swift'. "
        f"Got: {backend!r}"
    )
