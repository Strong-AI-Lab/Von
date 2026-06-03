"""LLM exchange blob writer (JVNAUTOSCI-2144).

Persists full LLM request + response bodies as gzipped JSON to Von's existing
``BlobStore`` abstraction (LocalBlobStore / SwiftBlobStore / S3BlobStore). The
recorded LLM call entry in MongoDB then carries a small ``exchange_blob_ref``
pointer (backend, key, uri, size_bytes) rather than embedding the bodies in
Mongo, where they would inflate turn execution records and frustrate
forensic inspection.

This module is intentionally independent of the orchestrator so that:

* Blob-write failures cannot break the surrounding LLM call (always returns a
  result; never raises).
* It can be reused by non-orchestrator LLM call sites later (for example
  ``llm_step_executor`` or direct fallback paths in ``von_routes``).
* It can be exercised against ``LocalBlobStore`` in unit tests without
  Swift/S3 credentials.

Design (per the design comment posted on JVNAUTOSCI-2144):

* Reuse ``BlobStore`` rather than introducing a new persistence layer.
* Prefer a separate dedicated container/bucket via
  ``VON_LLM_EXCHANGE_CONTAINER`` / ``VON_LLM_EXCHANGE_BUCKET`` so LLM I/O
  retention is independent of arXiv or other Von blob workloads. If those
  overrides are not set, fall back to the shared blob store with a stable
  ``llm_exchanges/`` key prefix.
* Cap the *uncompressed* serialised payload at 5 MB. Oversized exchanges
  store a metadata-only marker with ``truncated=True`` and
  ``truncation_reason='size_cap'`` so Layer 3 (JVNAUTOSCI-2145) can still
  surface the call without hitting Mongo with multi-MB blobs.
* ``VON_LLM_EXCHANGE_DISABLE`` (truthy values: ``1``, ``true``, ``yes``,
  ``on``) bypasses blob writing entirely. The factory returns ``None`` and
  callers store nothing.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .blob_store import (
    BlobRef,
    BlobStore,
    LocalBlobStore,
    S3BlobStore,
    SwiftBlobStore,
    _first_non_empty_env,
    _s3_failover_config_present,
    _swift_config_present,
    get_blob_store_from_env,
    resolve_blob_store_backend_from_env,
)

_logger = logging.getLogger(__name__)


SCHEMA_VERSION = "llm_exchange_blob.v1"
DEFAULT_KEY_PREFIX = "llm_exchanges"
DEFAULT_SIZE_CAP_BYTES = 5 * 1024 * 1024  # 5 MB uncompressed JSON


class _BlobStoreProtocol(Protocol):  # pragma: no cover - structural only
    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef: ...


def _is_truthy_env(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_falsy_env(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in {"0", "false", "no", "off"}


def _llm_exchange_spillway_enabled() -> bool:
    raw = os.environ.get("VON_LLM_EXCHANGE_USE_SPILLWAY")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    try:
        from .blob_spillway import is_spillway_enabled

        return is_spillway_enabled()
    except Exception:
        return False


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_date_path() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}/{now.month:02d}/{now.day:02d}"


def _coerce_jsonable(value: Any, *, max_depth: int = 8) -> Any:
    """Best-effort conversion of ``value`` to a JSON-serialisable structure.

    LLM request/response payloads sometimes contain non-JSON-native objects
    (dataclasses, custom message types, etc.). The writer must never raise on
    serialisation, so unknown values fall through ``str(value)``.
    """

    if max_depth <= 0:
        try:
            return str(value)
        except Exception:
            return "<unstringable>"

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, sub in value.items():
            try:
                result[str(key)] = _coerce_jsonable(sub, max_depth=max_depth - 1)
            except Exception:
                result[str(key)] = "<uncoercible>"
        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        out: list[Any] = []
        for item in value:
            try:
                out.append(_coerce_jsonable(item, max_depth=max_depth - 1))
            except Exception:
                out.append("<uncoercible>")
        return out

    # Common dataclass-shaped objects expose ``__dict__``; many LLM client
    # response objects do too. We do not import ``dataclasses`` to avoid a
    # narrow special-case: ``__dict__`` is sufficient.
    raw_dict = getattr(value, "__dict__", None)
    if isinstance(raw_dict, Mapping) and raw_dict:
        return _coerce_jsonable(dict(raw_dict), max_depth=max_depth - 1)

    try:
        return str(value)
    except Exception:
        return "<unstringable>"


def _build_swift_store_for_container(container: str) -> SwiftBlobStore:
    prefix = os.environ.get("VON_LLM_EXCHANGE_PREFIX") or os.environ.get(
        "VON_SWIFT_PREFIX",
        "",
    )
    public_base_url = os.environ.get(
        "VON_LLM_EXCHANGE_PUBLIC_BASE_URL"
    ) or os.environ.get("VON_SWIFT_PUBLIC_BASE_URL")
    cloud = _first_non_empty_env("OS_CLOUD", "OS_CLOUD_NAME")
    return SwiftBlobStore(
        container=container,
        prefix=prefix,
        public_base_url=public_base_url,
        cloud=cloud,
    )


def _build_s3_store_for_bucket(bucket: str) -> S3BlobStore:
    endpoint_url = _first_non_empty_env("VON_S3_ENDPOINT_URL")
    if not endpoint_url:
        raise ValueError(
            "VON_S3_ENDPOINT_URL is required to build an LLM-exchange S3 store"
        )
    access_key_id = _first_non_empty_env(
        "VON_S3_ACCESS_KEY_ID",
        "AWS_ACCESS_KEY_ID",
    )
    secret_access_key = _first_non_empty_env(
        "VON_S3_SECRET_ACCESS_KEY",
        "AWS_SECRET_ACCESS_KEY",
    )
    if not access_key_id or not secret_access_key:
        raise ValueError(
            "VON_S3 credentials are required to build an LLM-exchange S3 store"
        )
    prefix = (
        os.environ.get("VON_LLM_EXCHANGE_PREFIX")
        or os.environ.get("VON_S3_PREFIX")
        or os.environ.get("VON_SWIFT_PREFIX")
        or ""
    )
    public_base_url = os.environ.get(
        "VON_LLM_EXCHANGE_PUBLIC_BASE_URL"
    ) or os.environ.get("VON_S3_PUBLIC_BASE_URL")
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
    )


def _build_dedicated_blob_store_for_llm_exchanges() -> BlobStore | None:
    """Build a separate-container store if the operator opted in.

    Returns ``None`` if no dedicated container/bucket override is configured;
    callers should then fall back to ``get_blob_store_from_env()``.
    """

    backend = resolve_blob_store_backend_from_env()

    if backend == "swift":
        container = os.environ.get("VON_LLM_EXCHANGE_CONTAINER")
        if container and container.strip():
            return _build_swift_store_for_container(container.strip())
        return None

    if backend == "s3":
        bucket = os.environ.get("VON_LLM_EXCHANGE_BUCKET")
        if bucket and bucket.strip():
            return _build_s3_store_for_bucket(bucket.strip())
        return None

    if backend == "local":
        # For local development, allow an explicit dedicated root. Otherwise
        # the shared local store is used; the ``llm_exchanges/`` key prefix
        # already provides isolation on disk.
        root = os.environ.get("VON_LLM_EXCHANGE_LOCAL_ROOT")
        if root and root.strip():
            return LocalBlobStore(Path(root.strip()))
        return None

    # Swift+S3 failover and unknown backends fall back to shared store.
    if _swift_config_present() and _s3_failover_config_present():
        return None

    return None


class LlmExchangeBlobWriter:
    """Persist full LLM request/response bodies to a ``BlobStore``.

    The writer is deliberately defensive: every public method returns a
    structured result and never raises. Blob-write failures attach an
    ``error`` field to the result, leaving the surrounding LLM call
    unaffected.
    """

    def __init__(
        self,
        *,
        store: BlobStore,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        size_cap_bytes: int = DEFAULT_SIZE_CAP_BYTES,
        use_spillway: bool = False,
    ) -> None:
        self._store = store
        normalised_prefix = (key_prefix or "").strip().strip("/")
        self._key_prefix = normalised_prefix or DEFAULT_KEY_PREFIX
        self._size_cap_bytes = max(1024, int(size_cap_bytes))
        self._use_spillway = bool(use_spillway)

    @property
    def key_prefix(self) -> str:
        return self._key_prefix

    @property
    def size_cap_bytes(self) -> int:
        return self._size_cap_bytes

    def _build_key(
        self,
        *,
        turn_execution_id: str | None,
        stage: str | None,
    ) -> str:
        date_path = _utc_date_path()
        call_uuid = uuid.uuid4().hex
        if turn_execution_id and isinstance(turn_execution_id, str):
            safe_turn = (
                "".join(
                    ch if ch.isalnum() or ch in {"-", "_"} else "_"
                    for ch in turn_execution_id.strip()
                )[:64]
                or "unknown_turn"
            )
        else:
            safe_turn = "unknown_turn"
        if stage and isinstance(stage, str) and stage.strip():
            safe_stage = (
                "".join(
                    ch if ch.isalnum() or ch in {"-", "_"} else "_"
                    for ch in stage.strip()
                )[:48]
                or "unknown_stage"
            )
        else:
            safe_stage = "unknown_stage"
        return (
            f"{self._key_prefix}/{date_path}/{safe_turn}/{safe_stage}/"
            f"{call_uuid}.json.gz"
        )

    def write(
        self,
        *,
        turn_execution_id: str | None,
        stage: str | None,
        workflow_stage_id: str | None,
        call_type: str | None,
        model: str | None,
        provider: str | None,
        request_prompt: Any,
        request_context: Any,
        response: Any,
        prepared_at_utc: str | None = None,
        sent_at_utc: str | None = None,
        first_output_at_utc: str | None = None,
        usage: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a single LLM exchange to the blob store.

        Always returns a dict suitable for storing on the recorded LLM call
        entry. On success the dict mirrors the ``BlobRef`` shape
        (``backend``, ``key``, ``uri``, ``size_bytes``) plus ``schema_version``,
        ``truncated``, and timing fields. On failure the dict contains an
        ``error`` field describing the failure.
        """

        request_payload = {
            "prompt": _coerce_jsonable(request_prompt),
            "context": _coerce_jsonable(request_context),
        }
        response_payload = _coerce_jsonable(response)

        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "captured_at_utc": _utc_iso_now(),
            "turn_execution_id": turn_execution_id,
            "stage": stage,
            "workflow_stage_id": workflow_stage_id,
            "call_type": call_type,
            "model": model,
            "provider": provider,
            "prepared_at_utc": prepared_at_utc,
            "sent_at_utc": sent_at_utc,
            "first_output_at_utc": first_output_at_utc,
            "usage": _coerce_jsonable(usage) if usage is not None else None,
            "request": request_payload,
            "response": response_payload,
        }
        if extra:
            try:
                body["extra"] = _coerce_jsonable(dict(extra))
            except Exception:
                body["extra"] = "<uncoercible_extra>"

        truncated = False
        truncation_reason: str | None = None
        try:
            serialised = json.dumps(body, ensure_ascii=False, default=str).encode(
                "utf-8"
            )
        except Exception as exc:
            return {
                "error": (
                    f"llm_exchange_serialisation_failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }

        request_size_bytes = len(
            json.dumps(request_payload, ensure_ascii=False, default=str).encode("utf-8")
        )
        response_size_bytes = len(
            json.dumps(response_payload, ensure_ascii=False, default=str).encode(
                "utf-8"
            )
        )

        if len(serialised) > self._size_cap_bytes:
            truncated = True
            truncation_reason = "size_cap"
            metadata_only = {
                **{k: v for k, v in body.items() if k not in {"request", "response"}},
                "truncated": True,
                "truncation_reason": truncation_reason,
                "request_size_bytes": request_size_bytes,
                "response_size_bytes": response_size_bytes,
                "size_cap_bytes": self._size_cap_bytes,
                "uncompressed_size_bytes": len(serialised),
            }
            try:
                serialised = json.dumps(
                    metadata_only, ensure_ascii=False, default=str
                ).encode("utf-8")
            except Exception as exc:
                return {
                    "error": (
                        f"llm_exchange_truncation_serialisation_failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                }

        try:
            compressed = gzip.compress(serialised)
        except Exception as exc:
            return {
                "error": (f"llm_exchange_gzip_failed: {type(exc).__name__}: {exc}"),
            }

        key = self._build_key(
            turn_execution_id=turn_execution_id,
            stage=stage,
        )
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "truncated": "1" if truncated else "0",
        }
        try:
            if self._use_spillway:
                from .blob_uploads import enqueue_bytes_via_spillway

                stored = enqueue_bytes_via_spillway(
                    key=key,
                    data=compressed,
                    content_type="application/json+gzip",
                    metadata=metadata,
                    size_bytes=len(compressed),
                )
                blob_ref = stored.ref
            else:
                blob_ref = self._store.put_bytes(
                    key,
                    compressed,
                    content_type="application/json+gzip",
                    metadata=metadata,
                )
        except Exception as exc:
            _logger.warning(
                "LLM exchange blob write failed (stage=%s, key=%s): %s: %s",
                stage,
                key,
                type(exc).__name__,
                exc,
            )
            return {
                "error": (f"llm_exchange_blob_put_failed: {type(exc).__name__}: {exc}"),
                "key": key,
            }

        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "backend": blob_ref.backend,
            "key": blob_ref.key,
            "uri": blob_ref.uri,
            "size_bytes": blob_ref.size_bytes,
            "uncompressed_size_bytes": len(serialised),
            "request_size_bytes": request_size_bytes,
            "response_size_bytes": response_size_bytes,
            "truncated": truncated,
            "persistence_status": (
                "queued_local_first" if blob_ref.backend == "spillway" else "committed"
            ),
            "remote_state": "pending" if blob_ref.backend == "spillway" else "committed",
        }
        if blob_ref.etag:
            result["etag"] = blob_ref.etag
        if truncation_reason:
            result["truncation_reason"] = truncation_reason
        return result


_writer_singleton: LlmExchangeBlobWriter | None = None
_writer_singleton_resolved: bool = False


def get_llm_exchange_blob_writer_from_env() -> LlmExchangeBlobWriter | None:
    """Return a process-wide writer, or ``None`` if disabled / unconfigured.

    The writer is cached after first successful resolution. Returns ``None``
    when ``VON_LLM_EXCHANGE_DISABLE`` is truthy, or when no usable blob store
    can be constructed in the current environment.
    """

    global _writer_singleton, _writer_singleton_resolved

    if _is_truthy_env("VON_LLM_EXCHANGE_DISABLE"):
        return None

    if _writer_singleton_resolved:
        return _writer_singleton

    try:
        store: BlobStore | None = _build_dedicated_blob_store_for_llm_exchanges()
        if store is None:
            store = get_blob_store_from_env()
    except Exception as exc:
        _logger.warning(
            "Could not construct LLM exchange blob store; LLM I/O capture "
            "is disabled for this process: %s: %s",
            type(exc).__name__,
            exc,
        )
        _writer_singleton = None
        _writer_singleton_resolved = True
        return None

    size_cap_raw = os.environ.get("VON_LLM_EXCHANGE_SIZE_CAP_BYTES")
    size_cap = DEFAULT_SIZE_CAP_BYTES
    if size_cap_raw and size_cap_raw.strip().isdigit():
        try:
            size_cap = max(1024, int(size_cap_raw.strip()))
        except Exception:
            size_cap = DEFAULT_SIZE_CAP_BYTES

    key_prefix = os.environ.get("VON_LLM_EXCHANGE_KEY_PREFIX") or DEFAULT_KEY_PREFIX

    _writer_singleton = LlmExchangeBlobWriter(
        store=store,
        key_prefix=key_prefix,
        size_cap_bytes=size_cap,
        use_spillway=(
            not _is_truthy_env("VON_LLM_EXCHANGE_SYNC_DURABILITY")
            and not isinstance(store, LocalBlobStore)
            and _llm_exchange_spillway_enabled()
        ),
    )
    _writer_singleton_resolved = True
    return _writer_singleton


def read_llm_exchange_blob_ref(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """Load and decode an LLM exchange blob written by :class:`LlmExchangeBlobWriter`.

    Returns the parsed exchange body (schema ``llm_exchange_blob.v1``) carrying
    the exact ``request`` (prompt + context) and ``response`` bodies, or
    ``None`` when the reference is unusable or the blob cannot be resolved.

    This is the read counterpart to :meth:`LlmExchangeBlobWriter.write`. It
    resolves the blob store the same way the writer does (dedicated container
    if configured, otherwise the shared store) and falls back to the local
    spillway queue when the blob has been enqueued but not yet migrated. Like
    the writer, it never raises; resolution failures return ``None`` so callers
    can degrade gracefully.

    Supports JVNAUTOSCI-2385: surfacing the exact prompt and response in the
    Thinking-card timestamped LLM interaction list.
    """

    if not isinstance(ref, Mapping):
        return None
    if ref.get("error"):
        return None
    key = ref.get("key")
    if not isinstance(key, str) or not key.strip():
        return None
    clean_key = key.strip()
    backend_hint = ref.get("backend")

    try:
        store: BlobStore | None = _build_dedicated_blob_store_for_llm_exchanges()
        if store is None:
            store = get_blob_store_from_env()
    except Exception:
        store = None

    compressed: bytes | None = None
    if store is not None and backend_hint != "spillway":
        try:
            compressed = store.get_bytes(clean_key)
        except Exception:
            compressed = None

    if compressed is None:
        try:
            from .blob_spillway import get_blob_spillway_queue

            compressed = get_blob_spillway_queue().get_local_bytes(clean_key)
        except Exception:
            compressed = None

    if compressed is None and store is not None and backend_hint == "spillway":
        try:
            compressed = store.get_bytes(clean_key)
        except Exception:
            compressed = None

    if compressed is None:
        return None

    try:
        raw_bytes = gzip.decompress(compressed)
    except Exception:
        raw_bytes = compressed

    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None

    if isinstance(body, dict):
        return body
    return None


def reset_llm_exchange_blob_writer_singleton_for_tests() -> None:
    """Reset the cached writer. Tests use this between fixture mutations."""

    global _writer_singleton, _writer_singleton_resolved
    _writer_singleton = None
    _writer_singleton_resolved = False


__all__ = [
    "DEFAULT_KEY_PREFIX",
    "DEFAULT_SIZE_CAP_BYTES",
    "LlmExchangeBlobWriter",
    "SCHEMA_VERSION",
    "get_llm_exchange_blob_writer_from_env",
    "read_llm_exchange_blob_ref",
    "reset_llm_exchange_blob_writer_singleton_for_tests",
]
