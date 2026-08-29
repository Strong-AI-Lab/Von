from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit


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

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str = "") -> list[str]: ...


def _first_non_empty_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
    return None


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw_value!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw_value!r}")
    return value


def _swift_config_present() -> bool:
    if not _first_non_empty_env("VON_SWIFT_CONTAINER"):
        return False
    if _first_non_empty_env("OS_CLOUD", "OS_CLOUD_NAME"):
        return True
    if (
        _first_non_empty_env("OS_AUTH_URL")
        and _first_non_empty_env("OS_APPLICATION_CREDENTIAL_ID")
        and _first_non_empty_env("OS_APPLICATION_CREDENTIAL_SECRET")
    ):
        return True
    return bool(
        _first_non_empty_env("OS_AUTH_URL")
        and _first_non_empty_env("OS_USERNAME")
        and _first_non_empty_env("OS_PASSWORD")
        and _first_non_empty_env("OS_PROJECT_NAME")
    )


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


def _normalise_s3_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    if not metadata:
        return {}

    wire_metadata: dict[str, str] = {}
    for key, value in metadata.items():
        wire_key = str(key).strip().replace("_", "-")
        if not wire_key:
            raise ValueError("S3 metadata key must not be empty")
        if wire_key in wire_metadata:
            raise ValueError(
                f"S3 metadata keys collide after normalisation: {wire_key!r}"
            )
        wire_metadata[wire_key] = str(value)
    return wire_metadata


def _exception_http_status(exc: Exception) -> int | None:
    for attr in ("http_status", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        status = response.get("status_code")
        if isinstance(status, int):
            return status
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, Mapping):
            meta_status = metadata.get("HTTPStatusCode")
            if isinstance(meta_status, int):
                return meta_status
    else:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status

    return None


def _is_openstack_not_found_exception(exc: Exception) -> bool:
    exc_type = type(exc)
    module_name = str(getattr(exc_type, "__module__", "")).lower()
    type_name = str(getattr(exc_type, "__name__", ""))
    if not module_name.startswith("openstack"):
        return False

    if type_name in {"NotFoundException", "ResourceNotFound"}:
        return True

    return _exception_http_status(exc) == 404


def _get_openstack_config_candidate_paths() -> list[Path]:
    candidates: list[Path] = []

    explicit = _first_non_empty_env("OS_CLIENT_CONFIG_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    home = Path.home().expanduser()
    userprofile = Path(os.environ.get("USERPROFILE", str(home))).expanduser()

    for root in (home, userprofile):
        for relative in (
            Path(".config") / "openstack" / "clouds.yaml",
            Path(".config") / "openstack" / "clouds-public.yaml",
        ):
            candidate = root / relative
            if candidate not in candidates:
                candidates.append(candidate)

    return candidates


def _summarise_openstack_endpoint() -> str:
    details: list[str] = []

    auth_url = _first_non_empty_env("OS_AUTH_URL")
    if auth_url:
        parsed = urlsplit(auth_url)
        endpoint_hint = f"{parsed.scheme or 'https'}://{parsed.netloc or parsed.path}"
        if parsed.path and parsed.netloc:
            endpoint_hint += parsed.path
        details.append(f"auth_url={endpoint_hint!r}")

    region_name = _first_non_empty_env("OS_REGION_NAME")
    if region_name:
        details.append(f"region={region_name!r}")

    return ", ".join(details)


def _summarise_openstack_env_auth_mode() -> str:
    endpoint_hint = _summarise_openstack_endpoint()
    suffix = f" ({endpoint_hint})" if endpoint_hint else ""

    app_cred_id = _first_non_empty_env("OS_APPLICATION_CREDENTIAL_ID")
    app_cred_secret = _first_non_empty_env("OS_APPLICATION_CREDENTIAL_SECRET")
    if app_cred_id or app_cred_secret:
        missing = [
            name
            for name in (
                "OS_AUTH_URL",
                "OS_APPLICATION_CREDENTIAL_ID",
                "OS_APPLICATION_CREDENTIAL_SECRET",
            )
            if not _first_non_empty_env(name)
        ]
        if missing:
            return (
                "Environment-variable fallback is configured for application-credential "
                f"auth but is missing: {missing!r}{suffix}."
            )
        return (
            "Environment-variable fallback is configured for application-credential "
            f"auth{suffix}."
        )

    if any(
        _first_non_empty_env(name)
        for name in ("OS_AUTH_URL", "OS_USERNAME", "OS_PASSWORD", "OS_PROJECT_NAME")
    ):
        missing = [
            name
            for name in ("OS_AUTH_URL", "OS_USERNAME", "OS_PASSWORD", "OS_PROJECT_NAME")
            if not _first_non_empty_env(name)
        ]
        if missing:
            return (
                "Environment-variable fallback is configured for password auth but is "
                f"missing: {missing!r}{suffix}."
            )
        return f"Environment-variable fallback is configured for password auth{suffix}."

    return "No complete environment-variable fallback credentials are visible."


def _is_networkish_openstack_failure(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "connecttimeout",
        "connectionerror",
        "connectfailure",
        "max retries exceeded",
        "failed to establish a new connection",
        "name or service not known",
        "temporary failure in name resolution",
        "proxyerror",
        "ssl",
        "timed out",
        "connection refused",
        "network is unreachable",
    )
    return any(marker in text for marker in markers)


def _is_auth_openstack_failure(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "unauthorized",
        "forbidden",
        "401",
        "403",
        "authentication",
        "invalid application credential",
        "applicationcredential",
    )
    return any(marker in text for marker in markers)


def _is_endpoint_openstack_failure(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "endpointnotfound",
        "catalog",
        "no suitable endpoint",
        "object-store endpoint",
        "object store endpoint",
        "container not found",
    )
    return any(marker in text for marker in markers)


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

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        source = Path(path)
        destination = self._resolve_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if metadata:
            meta_path = destination.with_suffix(destination.suffix + ".meta.json")
            import json

            meta_path.write_text(json.dumps(dict(metadata), indent=2), encoding="utf-8")
        return BlobRef(
            backend="local",
            key=_normalise_key(key),
            uri=str(destination),
            content_type=content_type,
            size_bytes=source.stat().st_size,
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

    Supported env-var auth modes (unless using OS_CLOUD):
    - Application credentials:
      - OS_AUTH_URL
      - OS_APPLICATION_CREDENTIAL_ID
      - OS_APPLICATION_CREDENTIAL_SECRET
    - Password auth:
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

    def _build_connection_kwargs_from_envvars(self) -> tuple[dict[str, Any], str]:
        auth_url = _first_non_empty_env("OS_AUTH_URL")
        region_name = _first_non_empty_env("OS_REGION_NAME")
        interface = _first_non_empty_env("OS_INTERFACE")

        app_cred_id = _first_non_empty_env("OS_APPLICATION_CREDENTIAL_ID")
        app_cred_secret = _first_non_empty_env("OS_APPLICATION_CREDENTIAL_SECRET")
        if app_cred_id or app_cred_secret:
            missing = [
                name
                for name in (
                    "OS_AUTH_URL",
                    "OS_APPLICATION_CREDENTIAL_ID",
                    "OS_APPLICATION_CREDENTIAL_SECRET",
                )
                if not _first_non_empty_env(name)
            ]
            if missing:
                raise ValueError(
                    "Missing required environment variables for Swift "
                    f"application-credential auth: {', '.join(missing)}"
                )

            kwargs: dict[str, Any] = {
                "auth_url": auth_url,
                "auth_type": "v3applicationcredential",
                "application_credential_id": app_cred_id,
                "application_credential_secret": app_cred_secret,
                "region_name": region_name,
            }
            if interface:
                kwargs["interface"] = interface
            return kwargs, "application-credential"

        missing = [
            name
            for name in ("OS_AUTH_URL", "OS_USERNAME", "OS_PASSWORD", "OS_PROJECT_NAME")
            if not _first_non_empty_env(name)
        ]
        if missing:
            raise ValueError(
                "Missing required environment variables for Swift password auth: "
                + ", ".join(missing)
            )

        kwargs = {
            "auth_url": auth_url,
            "username": os.environ["OS_USERNAME"],
            "password": os.environ["OS_PASSWORD"],
            "project_name": os.environ["OS_PROJECT_NAME"],
            "user_domain_name": os.environ.get("OS_USER_DOMAIN_NAME", "Default"),
            "project_domain_name": os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default"),
            "region_name": region_name,
        }
        if interface:
            kwargs["interface"] = interface
        return kwargs, "password"

    def _create_connection_from_envvars(self, connection_mod):
        kwargs, _mode = self._build_connection_kwargs_from_envvars()
        return connection_mod.Connection(**kwargs)

    def _list_available_cloud_names(self, config_mod) -> list[str]:
        OpenStackConfig = getattr(config_mod, "OpenStackConfig")
        cfg = OpenStackConfig()
        names = cfg.get_cloud_names()
        return sorted({name for name in names if isinstance(name, str)})

    def _build_cloud_resolution_error(
        self,
        *,
        available_clouds: list[str],
        env_exc: Exception | None = None,
    ) -> ValueError:
        parts = [
            "OpenStack cloud profile resolution failed for "
            f"OS_CLOUD={self._cloud!r}. This happened before Von could make a "
            "Swift network request."
        ]

        if available_clouds:
            parts.append(
                f"Configured cloud names visible to openstacksdk: {available_clouds!r}."
            )
        else:
            parts.append("No configured cloud names were visible to openstacksdk.")

        explicit_config = _first_non_empty_env("OS_CLIENT_CONFIG_FILE")
        if explicit_config:
            config_path = Path(explicit_config).expanduser()
            if config_path.exists():
                parts.append(
                    "OS_CLIENT_CONFIG_FILE points to "
                    f"{config_path}, but that file does not define the requested "
                    f"cloud name {self._cloud!r}."
                )
            else:
                parts.append(
                    "OS_CLIENT_CONFIG_FILE points to "
                    f"{config_path}, but that file does not exist on this machine."
                )
        else:
            existing_paths = [
                path for path in _get_openstack_config_candidate_paths() if path.exists()
            ]
            if existing_paths:
                parts.append(
                    "No OS_CLIENT_CONFIG_FILE is set. OpenStack config files found at: "
                    + ", ".join(str(path) for path in existing_paths)
                    + "."
                )
            else:
                parts.append(
                    "No OS_CLIENT_CONFIG_FILE is set, and no clouds.yaml was found at "
                    "the default Windows/OpenStack locations: "
                    + ", ".join(str(path) for path in _get_openstack_config_candidate_paths())
                    + "."
                )

        parts.append(_summarise_openstack_env_auth_mode())

        if env_exc is not None:
            parts.append(f"Environment-variable fallback also failed: {env_exc}.")

        parts.append(
            "On Windows/PowerShell, either set OS_CLIENT_CONFIG_FILE to the full "
            "clouds.yaml path, or place clouds.yaml under "
            "%USERPROFILE%\\.config\\openstack\\clouds.yaml, then restart Von."
        )
        return ValueError(" ".join(parts))

    def _wrap_operation_exception(self, action: str, exc: Exception) -> RuntimeError:
        endpoint_hint = _summarise_openstack_endpoint()
        endpoint_suffix = f" ({endpoint_hint})" if endpoint_hint else ""

        if _is_networkish_openstack_failure(exc):
            return RuntimeError(
                "Swift network/connectivity failure during "
                f"{action}{endpoint_suffix}. Von had already resolved enough "
                "configuration to attempt a remote OpenStack request, so this is "
                "not a local clouds.yaml/profile lookup failure. Check outbound "
                "network access, firewall/VPN/proxy rules, and the configured "
                "region/endpoint. Underlying error: "
                f"{type(exc).__name__}: {exc}"
            )

        if _is_auth_openstack_failure(exc):
            return RuntimeError(
                "Swift authentication/authorisation failure during "
                f"{action}{endpoint_suffix}. Check the configured application "
                "credential or password-based OpenStack credentials and confirm "
                "that the project has Object Storage access. Underlying error: "
                f"{type(exc).__name__}: {exc}"
            )

        if _is_endpoint_openstack_failure(exc):
            return RuntimeError(
                "Swift endpoint/container resolution failure during "
                f"{action}{endpoint_suffix}. Check OS_REGION_NAME, the Object "
                "Storage endpoint for this project, and that "
                f"container={self._container!r} exists. Underlying error: "
                f"{type(exc).__name__}: {exc}"
            )

        return RuntimeError(
            f"Swift operation failed during {action}{endpoint_suffix}: "
            f"{type(exc).__name__}: {exc}"
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
                        available = self._list_available_cloud_names(config_mod)
                    except Exception:
                        available = []

                    # If explicit cloud profile lookup fails but envvars profile is
                    # present, fall back to environment-variable auth. This keeps
                    # uploads resilient when OS_CLOUD is stale/mistyped.
                    if "envvars" in {str(name).strip().lower() for name in available}:
                        try:
                            return self._create_connection_from_envvars(connection_mod)
                        except Exception as env_exc:
                            raise self._build_cloud_resolution_error(
                                available_clouds=available,
                                env_exc=env_exc,
                            ) from env_exc

                    raise self._build_cloud_resolution_error(
                        available_clouds=available,
                    ) from exc
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
        try:
            self._conn.object_store.create_object(
                container=self._container,
                name=full_key,
                data=data,
                content_type=content_type,
                metadata=dict(metadata) if metadata else None,
            )
        except Exception as exc:
            raise self._wrap_operation_exception("put_bytes", exc) from exc

        return BlobRef(
            backend="swift",
            key=_normalise_key(key),
            uri=self._build_uri(key),
            content_type=content_type,
            size_bytes=len(data),
            metadata=dict(metadata) if metadata else None,
        )

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        source = Path(path)
        full_key = self._full_key(key)
        try:
            with source.open("rb") as handle:
                self._conn.object_store.create_object(
                    container=self._container,
                    name=full_key,
                    data=handle,
                    content_type=content_type,
                    metadata=dict(metadata) if metadata else None,
                )
        except Exception as exc:
            raise self._wrap_operation_exception("put_file", exc) from exc
        return BlobRef(
            backend="swift",
            key=_normalise_key(key),
            uri=self._build_uri(key),
            content_type=content_type,
            size_bytes=source.stat().st_size,
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
            try:
                return self._conn.object_store.download_object(
                    name=full_key,
                    container=self._container,
                )
            except Exception as exc:
                raise self._wrap_operation_exception("get_bytes", exc) from exc
        except Exception as exc:
            raise self._wrap_operation_exception("get_bytes", exc) from exc

    def exists(self, key: str) -> bool:
        full_key = self._full_key(key)
        try:
            obj = self._conn.object_store.get_object(
                full_key,
                container=self._container,
            )
        except TypeError:
            try:
                obj = self._conn.object_store.get_object(
                    name=full_key,
                    container=self._container,
                )
            except Exception as exc:
                if _is_openstack_not_found_exception(exc):
                    return False
                raise
        except Exception as exc:
            if _is_openstack_not_found_exception(exc):
                return False
            raise
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
            try:
                self._conn.object_store.delete_object(
                    name=full_key,
                    container=self._container,
                    ignore_missing=True,
                )
            except Exception as exc:
                raise self._wrap_operation_exception("delete", exc) from exc
        except Exception as exc:
            raise self._wrap_operation_exception("delete", exc) from exc

    def list(self, prefix: str = "") -> list[str]:
        safe_prefix = _normalise_key(prefix) if prefix else ""
        full_prefix = self._full_key(safe_prefix) if safe_prefix else self._prefix
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix = full_prefix + "/"

        keys: list[str] = []
        try:
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
        except Exception as exc:
            raise self._wrap_operation_exception("list", exc) from exc

        keys.sort()
        return keys


class S3BlobStore:
    """S3-compatible blob store.

    This is intended for object stores that expose an S3-compatible API over
    HTTPS, including Catalyst Cloud's object-storage endpoint on port 443.

    Required for this store:
    - bucket
    - endpoint_url
    - access_key_id
    - secret_access_key

    Optional:
    - prefix
    - public_base_url
    - region_name
    - session_token
    - addressing_style (defaults to 'path')
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        prefix: str = "",
        public_base_url: str | None = None,
        region_name: str | None = None,
        access_key_id: str,
        secret_access_key: str,
        session_token: str | None = None,
        addressing_style: str = "path",
        connect_timeout_seconds: int = 10,
        read_timeout_seconds: int = 30,
        max_attempts: int = 3,
    ):
        self._bucket = bucket
        self._endpoint_url = endpoint_url.rstrip("/")
        self._prefix = prefix.strip("/")
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._region_name = region_name
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._addressing_style = addressing_style.strip().lower() or "path"
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._max_attempts = max_attempts

        self._client = self._create_client()

    def _create_client(self):
        try:
            import importlib

            boto3 = importlib.import_module("boto3")
            config_mod = importlib.import_module("botocore.config")
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "S3 blob backend requires 'boto3' in the active runtime environment. "
                "Rebuild the release virtualenv and ensure the dependency is installed "
                "(for local development, install it via PDM)."
            ) from exc

        Config = getattr(config_mod, "Config")
        session = boto3.session.Session()
        return session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region_name,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            aws_session_token=self._session_token,
            config=Config(
                signature_version="s3v4",
                connect_timeout=self._connect_timeout_seconds,
                read_timeout=self._read_timeout_seconds,
                retries={"max_attempts": self._max_attempts, "mode": "standard"},
                s3={"addressing_style": self._addressing_style},
            ),
        )

    def _full_key(self, key: str) -> str:
        safe_key = _normalise_key(key)
        if not self._prefix:
            return safe_key
        return f"{self._prefix}/{safe_key}"

    def _build_uri(self, key: str) -> str:
        full_key = self._full_key(key)
        base_url = self._public_base_url or self._endpoint_url
        if base_url:
            return f"{base_url}/{self._bucket}/{full_key}"
        return f"s3://{self._bucket}/{full_key}"

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        full_key = self._full_key(key)
        put_kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": full_key,
            "Body": data,
        }
        if content_type:
            put_kwargs["ContentType"] = content_type
        if metadata:
            put_kwargs["Metadata"] = _normalise_s3_metadata(metadata)

        self._client.put_object(**put_kwargs)

        return BlobRef(
            backend="s3",
            key=_normalise_key(key),
            uri=self._build_uri(key),
            content_type=content_type,
            size_bytes=len(data),
            metadata=dict(metadata) if metadata else None,
        )

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        source = Path(path)
        full_key = self._full_key(key)
        extra_args: dict[str, Any] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        if metadata:
            extra_args["Metadata"] = _normalise_s3_metadata(metadata)
        with source.open("rb") as handle:
            upload_fileobj = getattr(self._client, "upload_fileobj", None)
            if callable(upload_fileobj):
                kwargs = {"ExtraArgs": extra_args} if extra_args else {}
                upload_fileobj(handle, self._bucket, full_key, **kwargs)
            else:
                put_kwargs: dict[str, Any] = {
                    "Bucket": self._bucket,
                    "Key": full_key,
                    "Body": handle,
                }
                put_kwargs.update(extra_args)
                self._client.put_object(**put_kwargs)
        return BlobRef(
            backend="s3",
            key=_normalise_key(key),
            uri=self._build_uri(key),
            content_type=content_type,
            size_bytes=source.stat().st_size,
            metadata=dict(metadata) if metadata else None,
        )

    def get_bytes(self, key: str) -> bytes:
        full_key = self._full_key(key)
        response = self._client.get_object(Bucket=self._bucket, Key=full_key)
        body = response.get("Body")
        if hasattr(body, "read"):
            try:
                data = bytes(body.read())
                expected_size = response.get("ContentLength")
                if not isinstance(expected_size, int) or len(data) == expected_size:
                    return data
            except Exception:
                pass
            return self._get_bytes_by_range(full_key)
        raise RuntimeError("S3 get_object response did not include a readable Body")

    def _get_bytes_by_range(self, full_key: str, *, chunk_size: int = 64 * 1024) -> bytes:
        head = self._client.head_object(Bucket=self._bucket, Key=full_key)
        content_length = head.get("ContentLength")
        if not isinstance(content_length, int) or content_length < 0:
            raise RuntimeError("S3 head_object response did not include ContentLength")

        chunks: list[bytes] = []
        start = 0
        while start < content_length:
            end = min(content_length - 1, start + max(1, int(chunk_size)) - 1)
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=full_key,
                Range=f"bytes={start}-{end}",
            )
            body = response.get("Body")
            if not hasattr(body, "read"):
                raise RuntimeError("S3 ranged get_object response did not include a readable Body")
            chunks.append(bytes(body.read()))
            start = end + 1
        data = b"".join(chunks)
        if len(data) != content_length:
            raise RuntimeError(
                "S3 ranged get_object returned an unexpected byte count: "
                f"expected {content_length}, got {len(data)}"
            )
        return data

    def exists(self, key: str) -> bool:
        full_key = self._full_key(key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=full_key)
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            status = (
                (response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
                if isinstance(response, dict)
                else None
            )
            code = str(error.get("Code") or "").strip()
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        full_key = self._full_key(key)
        self._client.delete_object(Bucket=self._bucket, Key=full_key)

    def list(self, prefix: str = "") -> list[str]:
        safe_prefix = _normalise_key(prefix) if prefix else ""
        full_prefix = self._full_key(safe_prefix) if safe_prefix else self._prefix
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix = full_prefix + "/"

        paginator = self._client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=self._bucket,
            Prefix=full_prefix or "",
        )

        keys: list[str] = []
        for page in pages:
            for obj in page.get("Contents") or []:
                name = obj.get("Key")
                if not name or not isinstance(name, str):
                    continue

                if self._prefix:
                    store_prefix = self._prefix + "/"
                    if name.startswith(store_prefix):
                        name = name[len(store_prefix) :]

                keys.append(_normalise_key(name))

        keys.sort()
        return keys


class FailoverBlobStore:
    """Primary/secondary blob store wrapper for transport failover.

    This is intended for cases where the same underlying object namespace can be
    reached via two protocols, such as Catalyst Swift and Catalyst's
    S3-compatible interface.
    """

    def __init__(
        self,
        *,
        primary: BlobStore,
        secondary: BlobStore,
        primary_name: str,
        secondary_name: str,
    ):
        self._primary = primary
        self._secondary = secondary
        self._primary_name = primary_name
        self._secondary_name = secondary_name
        self._failed_over = False

    def _should_fail_over(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        if not message:
            return False

        network_markers = (
            "timed out",
            "timeout",
            "connection",
            "connect failure",
            "max retries exceeded",
            "temporary failure",
            "name or service not known",
            "keystone",
            "auth/tokens",
            ":5000",
            "cloud was not found",
            "discovery failure",
            "service unavailable",
            "unauthorized",
        )
        not_found_markers = (
            "not found",
            "404",
            "nosuchkey",
            "no such key",
            "notfound",
        )
        if any(marker in message for marker in not_found_markers):
            return False
        return any(marker in message for marker in network_markers)

    def _run(self, op_name: str, *args: Any, **kwargs: Any):
        if self._failed_over:
            return getattr(self._secondary, op_name)(*args, **kwargs)

        try:
            return getattr(self._primary, op_name)(*args, **kwargs)
        except Exception as exc:
            if not self._should_fail_over(exc):
                raise
            self._failed_over = True
            return getattr(self._secondary, op_name)(*args, **kwargs)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        return self._run(
            "put_bytes",
            key,
            data,
            content_type=content_type,
            metadata=metadata,
        )

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        return self._run(
            "put_file",
            key,
            path,
            content_type=content_type,
            metadata=metadata,
        )

    def get_bytes(self, key: str) -> bytes:
        return self._run("get_bytes", key)

    def exists(self, key: str) -> bool:
        return self._run("exists", key)

    def delete(self, key: str) -> None:
        self._run("delete", key)

    def list(self, prefix: str = "") -> list[str]:
        return self._run("list", prefix)


def _build_s3_blob_store_from_env() -> S3BlobStore:
    bucket = _first_non_empty_env("VON_S3_BUCKET", "VON_SWIFT_CONTAINER")
    if not bucket:
        raise ValueError(
            "VON_S3_BUCKET is required when backend=s3. "
            "If you are targeting the same Catalyst container as Swift, "
            "VON_SWIFT_CONTAINER may be reused."
        )

    endpoint_url = _first_non_empty_env("VON_S3_ENDPOINT_URL")
    if not endpoint_url:
        raise ValueError("VON_S3_ENDPOINT_URL is required for S3-compatible access")

    access_key_id = _first_non_empty_env("VON_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    secret_access_key = _first_non_empty_env(
        "VON_S3_SECRET_ACCESS_KEY",
        "AWS_SECRET_ACCESS_KEY",
    )
    if not access_key_id or not secret_access_key:
        raise ValueError(
            "S3-compatible access requires credentials via AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY (or VON_S3_ACCESS_KEY_ID / "
            "VON_S3_SECRET_ACCESS_KEY)."
        )

    prefix = _first_non_empty_env("VON_S3_PREFIX", "VON_SWIFT_PREFIX") or ""
    public_base_url = _first_non_empty_env("VON_S3_PUBLIC_BASE_URL")
    region_name = (
        _first_non_empty_env(
            "VON_S3_REGION_NAME",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "OS_REGION_NAME",
        )
        or "us-east-1"
    )
    session_token = _first_non_empty_env("VON_S3_SESSION_TOKEN", "AWS_SESSION_TOKEN")
    addressing_style = _first_non_empty_env("VON_S3_ADDRESSING_STYLE") or "path"
    connect_timeout_seconds = _positive_int_env("VON_S3_CONNECT_TIMEOUT_SECONDS", 10)
    read_timeout_seconds = _positive_int_env("VON_S3_READ_TIMEOUT_SECONDS", 30)
    max_attempts = _positive_int_env("VON_S3_MAX_ATTEMPTS", 3)

    return S3BlobStore(
        bucket=bucket,
        endpoint_url=endpoint_url,
        prefix=prefix,
        public_base_url=public_base_url,
        region_name=region_name,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token,
        addressing_style=addressing_style,
        connect_timeout_seconds=connect_timeout_seconds,
        read_timeout_seconds=read_timeout_seconds,
        max_attempts=max_attempts,
    )


def _swift_s3_failover_enabled() -> bool:
    value = os.environ.get("VON_SWIFT_S3_FAILOVER_ENABLE")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _s3_failover_config_present() -> bool:
    return bool(
        _first_non_empty_env("VON_S3_ENDPOINT_URL")
        and _first_non_empty_env("VON_S3_BUCKET", "VON_SWIFT_CONTAINER")
        and _first_non_empty_env("VON_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
        and _first_non_empty_env("VON_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    )


def resolve_blob_store_backend_from_env() -> str:
    explicit = _first_non_empty_env("VON_BLOB_STORE_BACKEND")
    if explicit:
        return explicit.strip().lower()
    if _swift_config_present():
        return "swift"
    if _s3_failover_config_present():
        return "s3"
    return "local"


def get_blob_store_from_env() -> BlobStore:
    backend = resolve_blob_store_backend_from_env()

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
        cloud = _first_non_empty_env("OS_CLOUD", "OS_CLOUD_NAME")

        swift_kwargs = {
            "container": container,
            "prefix": prefix,
            "public_base_url": public_base_url,
            "cloud": cloud,
        }

        if _swift_s3_failover_enabled() and _s3_failover_config_present():
            try:
                primary = SwiftBlobStore(**swift_kwargs)
            except Exception as exc:
                if "not found" not in str(exc).strip().lower():
                    return _build_s3_blob_store_from_env()
                raise
            secondary = _build_s3_blob_store_from_env()
            return FailoverBlobStore(
                primary=primary,
                secondary=secondary,
                primary_name="swift",
                secondary_name="s3",
            )

        return SwiftBlobStore(**swift_kwargs)

    if backend == "s3":
        return _build_s3_blob_store_from_env()

    raise ValueError(
        "Unsupported VON_BLOB_STORE_BACKEND. Expected 'local', 'swift', or 's3'. "
        f"Got: {backend!r}"
    )


def get_blob_store_for_backend_from_env(backend: str) -> BlobStore:
    """Build a blob store for a concrete backend recorded in a blob reference.

    A deployment may use ``FailoverBlobStore`` for writes, where a successful
    write can return ``backend='s3'`` even though the configured primary backend
    is Swift.  Readers that already have a backend-specific reference should be
    able to go directly to that backend instead of retrying the primary first.
    """

    cleaned = str(backend or "").strip().lower()
    if cleaned == "local":
        root = os.environ.get("VON_BLOB_STORE_LOCAL_ROOT")
        if root:
            root_dir = Path(root)
        else:
            workspace_root = Path(__file__).parent.parent.parent.parent
            root_dir = workspace_root / "data" / "blob_store"
        return LocalBlobStore(root_dir)

    if cleaned == "s3":
        return _build_s3_blob_store_from_env()

    if cleaned == "swift":
        container = os.environ.get("VON_SWIFT_CONTAINER")
        if not container:
            raise ValueError("VON_SWIFT_CONTAINER is required when backend=swift")
        return SwiftBlobStore(
            container=container,
            prefix=os.environ.get("VON_SWIFT_PREFIX", ""),
            public_base_url=os.environ.get("VON_SWIFT_PUBLIC_BASE_URL"),
            cloud=_first_non_empty_env("OS_CLOUD", "OS_CLOUD_NAME"),
        )

    return get_blob_store_from_env()
