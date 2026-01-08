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

    store = get_blob_store_from_env()

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
