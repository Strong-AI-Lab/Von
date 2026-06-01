from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from .blob_store import BlobRef


class BlobUploadError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredBytes:
    ref: BlobRef
    sha256: str
    size_bytes: int


def enqueue_bytes_via_spillway(
    *,
    key: str,
    data: bytes,
    content_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> StoredBytes:
    """Write bytes to the local spillway queue and return immediately.

    This is the fast-path alternative to ``put_bytes_durable`` used when
    ``VON_BLOB_SPILLWAY_ENABLED=true``.  The blob is written to local disk;
    a background migrator thread uploads it to the configured remote blob store
    with retry/backoff.  The read path in ``debug_payload_store`` falls back to
    the local spillway when the remote blob is not yet available.

    Raises ``BlobUploadError`` if the local write itself fails (e.g. disk full).
    """
    from .blob_spillway import get_blob_spillway_queue

    data_bytes = bytes(data)
    computed_size = size_bytes if size_bytes is not None else len(data_bytes)
    computed_sha = (
        sha256 if sha256 is not None else hashlib.sha256(data_bytes).hexdigest()
    )

    meta: dict[str, str] = {}
    if metadata:
        for k, v in dict(metadata).items():
            if v is None:
                continue
            meta[str(k)] = str(v)
    meta.setdefault("sha256", computed_sha)
    meta.setdefault("size_bytes", str(computed_size))

    try:
        ref = get_blob_spillway_queue().enqueue(
            key=key,
            data=data_bytes,
            content_type=content_type,
            metadata=meta,
            sha256=computed_sha,
            size_bytes=computed_size,
        )
    except Exception as exc:
        raise BlobUploadError(f"Spillway enqueue failed: {exc}") from exc

    return StoredBytes(ref=ref, sha256=computed_sha, size_bytes=computed_size)


def put_bytes_durable(
    *,
    key: str,
    data: bytes,
    content_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> StoredBytes:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise BlobUploadError("No bytes provided")

    data_bytes = bytes(data)

    computed_size = size_bytes if size_bytes is not None else len(data_bytes)
    computed_sha = (
        sha256 if sha256 is not None else hashlib.sha256(data_bytes).hexdigest()
    )

    meta: dict[str, str] = {}
    if metadata:
        for k, v in dict(metadata).items():
            if v is None:
                continue
            meta[str(k)] = str(v)

    meta.setdefault("sha256", computed_sha)
    meta.setdefault("size_bytes", str(computed_size))

    # Import at call-time so tests can monkeypatch blob_store.get_blob_store_from_env.
    from .blob_store import get_blob_store_from_env

    try:
        store = get_blob_store_from_env()
    except Exception as exc:
        raise BlobUploadError(f"Blob store initialisation failed: {exc}") from exc

    try:
        ref = store.put_bytes(key, data_bytes, content_type=content_type, metadata=meta)
    except Exception as exc:
        raise BlobUploadError(f"Blob store put_bytes failed: {exc}") from exc

    if not isinstance(ref, BlobRef):
        raise BlobUploadError(
            f"Blob store returned unexpected reference type: {type(ref).__name__}"
        )

    if not ref.backend or not ref.key or not ref.uri:
        raise BlobUploadError("Blob store returned an incomplete reference")

    return StoredBytes(ref=ref, sha256=computed_sha, size_bytes=computed_size)
