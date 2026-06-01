"""Async local spillway queue for durable blob writes.

Decouples response finalisation from remote object-storage latency:
- ``enqueue()`` writes bytes to local disk immediately (fast path).
- ``migrate_pending()`` uploads to the remote blob store in a background thread.
- ``get_local_bytes()`` serves bytes before migration completes (read fallback).

Blob location states:
  pending  — written to local spillway, awaiting migration to remote.
  migrated — uploaded to remote, local files deleted.
  dead     — failed max_retries times, moved to dead/ subdir for manual review.

Env vars:
  VON_BLOB_SPILLWAY_ENABLED          — "true" (default) / "false".
  VON_BLOB_SPILLWAY_DIR              — local root dir (default: data/blob_spillway).
  VON_BLOB_SPILLWAY_MAX_RETRIES      — int (default: 10).
  VON_BLOB_SPILLWAY_MIGRATE_INTERVAL_SECONDS — float (default: 30, used by caller).

JVNAUTOSCI-2382.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from .blob_store import BlobRef, BlobStore

logger = logging.getLogger(__name__)

_DEFAULT_SPILLWAY_DIR = "data/blob_spillway"
_MANIFEST_SUFFIX = ".manifest.json"
_DEFAULT_MAX_RETRIES = 10
_DEFAULT_MAX_PER_RUN = 50

_singleton: BlobSpillwayQueue | None = None
_singleton_lock = threading.Lock()


def _safe_key(key: str) -> str:
    """Normalise a remote blob key into a safe filesystem path.

    Mirrors blob_store._normalise_key to ensure local paths are consistent with
    the remote key used during migration.
    """
    key = key.strip().replace("\\", "/")
    if not key:
        raise ValueError("Blob key must not be empty")
    posix = PurePosixPath(key)
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        raise ValueError(f"Unsafe blob key: {key!r}")
    key = str(posix)
    while key.startswith("./"):
        key = key[2:]
    if not key or key == ".":
        raise ValueError("Blob key must not be empty")
    return key


class BlobSpillwayQueue:
    """Filesystem-backed async blob upload queue.

    Thread-safe: enqueue() and migrate_pending() are safe to call from
    separate threads (the migrator daemon and the request thread).
    """

    def __init__(self, spillway_dir: Path, max_retries: int = _DEFAULT_MAX_RETRIES):
        self._pending_dir = spillway_dir / "pending"
        self._dead_dir = spillway_dir / "dead"
        self._pending_dir.mkdir(parents=True, exist_ok=True)
        self._dead_dir.mkdir(parents=True, exist_ok=True)
        self._max_retries = max_retries

    def _blob_path(self, key: str) -> Path:
        return self._pending_dir / _safe_key(key)

    def _manifest_path(self, key: str) -> Path:
        return Path(str(self._blob_path(key)) + _MANIFEST_SUFFIX)

    def enqueue(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
    ) -> BlobRef:
        """Write bytes to local spillway; return a BlobRef immediately.

        The caller must treat the returned ref's ``backend`` value (``"spillway"``)
        as the signal that the blob is pending migration.  The read path in
        ``debug_payload_store.load_debug_payload_blob_ref`` falls back to the
        local spillway when ``backend == "spillway"`` and the remote fetch fails.
        """
        data_bytes = bytes(data)
        computed_sha = sha256 or hashlib.sha256(data_bytes).hexdigest()
        computed_size = size_bytes if size_bytes is not None else len(data_bytes)

        path = self._blob_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write: write to a temp sibling then rename.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(data_bytes)
            tmp_path.replace(path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        manifest: dict[str, object] = {
            "remote_key": key,
            "content_type": content_type,
            "metadata": metadata or {},
            "sha256": computed_sha,
            "size_bytes": computed_size,
            "retry_count": 0,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._manifest_path(key).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        logger.debug("Spillway: enqueued key=%r size=%d", key, computed_size)
        return BlobRef(
            backend="spillway",
            key=key,
            uri=str(path),
            content_type=content_type,
            size_bytes=computed_size,
        )

    def get_local_bytes(self, key: str) -> bytes:
        """Read bytes from local spillway.  Raises ``KeyError`` if not present."""
        path = self._blob_path(key)
        if not path.exists():
            raise KeyError(f"Key not in spillway: {key!r}")
        return path.read_bytes()

    def has_pending(self, key: str) -> bool:
        """Return True if this key has a pending local blob."""
        return self._blob_path(key).exists()

    def list_pending_keys(self) -> list[str]:
        """Return remote keys of all pending (unmigrated) spillway blobs."""
        keys: list[str] = []
        for manifest_path in sorted(
            self._pending_dir.rglob(f"*{_MANIFEST_SUFFIX}")
        ):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                remote_key = manifest.get("remote_key")
                if isinstance(remote_key, str) and remote_key.strip():
                    keys.append(remote_key.strip())
            except Exception as exc:
                logger.warning(
                    "Spillway: could not read manifest %s: %s", manifest_path, exc
                )
        return keys

    def migrate_pending(
        self,
        remote_store_factory: Callable[[], BlobStore],
        max_per_run: int = _DEFAULT_MAX_PER_RUN,
    ) -> tuple[int, int]:
        """Upload pending blobs to the remote blob store.

        Args:
            remote_store_factory: Callable returning a live ``BlobStore``.
            max_per_run: Maximum blobs to process per call (bounds run time).

        Returns:
            ``(succeeded, failed)`` counts for this run.
        """
        keys = self.list_pending_keys()[:max_per_run]
        if not keys:
            return 0, 0

        try:
            remote_store = remote_store_factory()
        except Exception as exc:
            logger.warning(
                "Spillway migrator: could not obtain remote store (%s) — "
                "will retry next cycle",
                exc,
            )
            return 0, len(keys)

        succeeded = failed = 0
        for key in keys:
            if self._migrate_one(key, remote_store):
                succeeded += 1
            else:
                failed += 1

        if succeeded or failed:
            remaining = max(0, len(keys) - succeeded)
            logger.info(
                "Spillway migrator: migrated=%d failed=%d remaining≥%d",
                succeeded,
                failed,
                remaining,
            )
        return succeeded, failed

    def _migrate_one(self, key: str, remote_store: BlobStore) -> bool:
        blob_path = self._blob_path(key)
        manifest_path = self._manifest_path(key)

        if not blob_path.exists():
            # Already migrated concurrently (or manually cleaned up).
            manifest_path.unlink(missing_ok=True)
            return True

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Spillway: unreadable manifest for key=%r (%s); skipping", key, exc
            )
            return False

        try:
            data = blob_path.read_bytes()
            raw_meta = manifest.get("metadata") or {}
            meta: dict[str, str] = {
                str(k): str(v) for k, v in raw_meta.items() if v is not None
            }
            remote_store.put_bytes(
                key,
                data,
                content_type=manifest.get("content_type") or None,
                metadata=meta or None,
            )
        except Exception as exc:
            retry_count = int(manifest.get("retry_count", 0)) + 1
            manifest["retry_count"] = retry_count
            manifest["last_error"] = str(exc)
            manifest["last_attempt_utc"] = datetime.now(timezone.utc).isoformat()

            if retry_count >= self._max_retries:
                logger.error(
                    "Spillway: dead-lettering key=%r after %d retries: %s",
                    key,
                    retry_count,
                    exc,
                )
                dead_blob = self._dead_dir / _safe_key(key)
                dead_manifest = Path(str(dead_blob) + _MANIFEST_SUFFIX)
                dead_blob.parent.mkdir(parents=True, exist_ok=True)
                try:
                    blob_path.rename(dead_blob)
                    manifest_path.write_text(
                        json.dumps(manifest, indent=2), encoding="utf-8"
                    )
                    manifest_path.rename(dead_manifest)
                except Exception as mv_exc:
                    logger.error(
                        "Spillway: could not dead-letter key=%r: %s", key, mv_exc
                    )
            else:
                logger.debug(
                    "Spillway: retry %d/%d for key=%r: %s",
                    retry_count,
                    self._max_retries,
                    key,
                    exc,
                )
                try:
                    manifest_path.write_text(
                        json.dumps(manifest, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass
            return False

        # Success — remove local copy.
        try:
            blob_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(
                "Spillway: cleanup failed for key=%r (migrated OK): %s", key, exc
            )
        return True


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _default_spillway_dir() -> Path:
    return Path(os.getenv("VON_BLOB_SPILLWAY_DIR", _DEFAULT_SPILLWAY_DIR))


def _default_max_retries() -> int:
    try:
        val = int(
            os.getenv("VON_BLOB_SPILLWAY_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES))
        )
        return val if val > 0 else _DEFAULT_MAX_RETRIES
    except (ValueError, TypeError):
        return _DEFAULT_MAX_RETRIES


def get_blob_spillway_queue() -> BlobSpillwayQueue:
    """Return the process-global ``BlobSpillwayQueue`` (created on first call)."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = BlobSpillwayQueue(
                    spillway_dir=_default_spillway_dir(),
                    max_retries=_default_max_retries(),
                )
    return _singleton


def is_spillway_enabled() -> bool:
    """Return True when the async blob spillway is active (default True)."""
    val = os.getenv("VON_BLOB_SPILLWAY_ENABLED", "true").strip().lower()
    return val not in {"0", "false", "no", "off"}
