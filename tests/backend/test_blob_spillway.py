"""Tests for the async local blob spillway queue.

Tests exercise:
- enqueue() writes local file + manifest
- get_local_bytes() returns enqueued bytes
- has_pending() / list_pending_keys()
- migrate_pending(): success path (uploads to remote, deletes local)
- migrate_pending(): retry on failure (increments count, keeps local)
- migrate_pending(): dead-letters after max_retries
- enqueue_bytes_via_spillway() wrapper in blob_uploads
- load_debug_payload_blob_ref() fallback to local spillway when remote fails
- is_spillway_enabled() / env override
- compact_debug_payload_for_storage() routes through spillway when enabled

JVNAUTOSCI-2382.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services.blob_spillway import (
    BlobSpillwayQueue,
    get_blob_spillway_queue,
    is_spillway_enabled,
    _safe_key,
)
from src.backend.services.blob_store import BlobRef
from src.backend.services.blob_uploads import (
    BlobUploadError,
    StoredBytes,
    enqueue_bytes_via_spillway,
)
from src.backend.services.debug_payload_store import (
    compact_debug_payload_for_storage,
    load_debug_payload_blob_ref,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue(tmp_path: Path, max_retries: int = 3) -> BlobSpillwayQueue:
    return BlobSpillwayQueue(spillway_dir=tmp_path / "spillway", max_retries=max_retries)


def _make_remote_store(fail: bool = False, fail_times: int = 0) -> MagicMock:
    """Return a mock BlobStore that succeeds or fails put_bytes."""
    store = MagicMock()
    store.get_bytes = MagicMock(side_effect=KeyError("not in remote"))

    if fail:
        store.put_bytes.side_effect = RuntimeError("remote unavailable")
    elif fail_times:
        calls: list[int] = [0]

        def _put_bytes(key: str, data: bytes, **kw: Any) -> BlobRef:
            calls[0] += 1
            if calls[0] <= fail_times:
                raise RuntimeError(f"remote unavailable (attempt {calls[0]})")
            return BlobRef(
                backend="s3", key=key, uri=f"s3://bucket/{key}", size_bytes=len(data)
            )

        store.put_bytes.side_effect = _put_bytes
    else:
        store.put_bytes.return_value = BlobRef(
            backend="s3",
            key="migrated-key",
            uri="s3://bucket/migrated-key",
            size_bytes=10,
        )
    return store


# ---------------------------------------------------------------------------
# _safe_key
# ---------------------------------------------------------------------------


def test_safe_key_preserves_path_structure() -> None:
    key = "debug/turns/abc/req-1/chat_history.llm_debug_data/messages.abc123.json.gz"
    result = _safe_key(key)
    assert result == key


def test_safe_key_rejects_dotdot() -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        _safe_key("debug/../secret")


def test_safe_key_rejects_absolute() -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        _safe_key("/etc/passwd")


# ---------------------------------------------------------------------------
# enqueue / basic accessors
# ---------------------------------------------------------------------------


def test_enqueue_writes_local_files(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    data = b"hello spillway"
    ref = q.enqueue(key="test/key.json.gz", data=data, content_type="application/json")

    assert ref.backend == "spillway"
    assert ref.key == "test/key.json.gz"
    assert ref.size_bytes == len(data)

    # Both blob and manifest should exist on disk.
    blob_path = q._blob_path("test/key.json.gz")
    manifest_path = q._manifest_path("test/key.json.gz")
    assert blob_path.exists()
    assert manifest_path.exists()
    assert blob_path.read_bytes() == data

    manifest = json.loads(manifest_path.read_text())
    assert manifest["remote_key"] == "test/key.json.gz"
    assert manifest["retry_count"] == 0


def test_get_local_bytes_returns_enqueued(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    data = b"payload bytes"
    q.enqueue(key="some/key.gz", data=data)
    assert q.get_local_bytes("some/key.gz") == data


def test_get_local_bytes_raises_key_error_when_missing(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    with pytest.raises(KeyError):
        q.get_local_bytes("nonexistent/key.gz")


def test_has_pending_true_after_enqueue(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    q.enqueue(key="k/a.gz", data=b"x")
    assert q.has_pending("k/a.gz") is True


def test_has_pending_false_when_not_enqueued(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    assert q.has_pending("k/missing.gz") is False


def test_list_pending_keys(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    keys = ["a/b.gz", "c/d.gz", "e/f.gz"]
    for k in keys:
        q.enqueue(key=k, data=b"data")
    pending = q.list_pending_keys()
    assert sorted(pending) == sorted(keys)


def test_list_pending_keys_empty_queue(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    assert q.list_pending_keys() == []


# ---------------------------------------------------------------------------
# migrate_pending — success path
# ---------------------------------------------------------------------------


def test_migrate_pending_uploads_and_deletes_local(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    data = b"payload to migrate"
    key = "debug/turns/req1/payload.json.gz"
    meta = {"sha256": "abc", "size_bytes": str(len(data))}
    q.enqueue(key=key, data=data, content_type="application/json", metadata=meta)

    remote = _make_remote_store()
    succeeded, failed = q.migrate_pending(lambda: remote)

    assert succeeded == 1
    assert failed == 0

    # Remote put_bytes called with correct args.
    call_args = remote.put_bytes.call_args
    assert call_args[0][0] == key
    assert call_args[0][1] == data
    assert call_args[1]["content_type"] == "application/json"

    # Local files cleaned up.
    assert not q._blob_path(key).exists()
    assert not q._manifest_path(key).exists()
    assert not q.has_pending(key)
    assert q.list_pending_keys() == []


def test_migrate_pending_handles_empty_queue(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    remote = _make_remote_store()
    succeeded, failed = q.migrate_pending(lambda: remote)
    assert (succeeded, failed) == (0, 0)
    remote.put_bytes.assert_not_called()


def test_migrate_pending_respects_max_per_run(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    for i in range(5):
        q.enqueue(key=f"key/{i}.gz", data=b"x")
    remote = _make_remote_store()
    succeeded, failed = q.migrate_pending(lambda: remote, max_per_run=2)
    assert succeeded == 2
    assert len(q.list_pending_keys()) == 3  # 3 still pending


# ---------------------------------------------------------------------------
# migrate_pending — retry/failure paths
# ---------------------------------------------------------------------------


def test_migrate_pending_increments_retry_on_failure(tmp_path: Path) -> None:
    q = _make_queue(tmp_path, max_retries=5)
    key = "fail/key.gz"
    q.enqueue(key=key, data=b"data")

    remote = _make_remote_store(fail=True)
    succeeded, failed = q.migrate_pending(lambda: remote)

    assert succeeded == 0
    assert failed == 1

    # Local blob still present.
    assert q.has_pending(key)
    manifest = json.loads(q._manifest_path(key).read_text())
    assert manifest["retry_count"] == 1
    assert "last_error" in manifest


def test_migrate_pending_multiple_retries(tmp_path: Path) -> None:
    q = _make_queue(tmp_path, max_retries=5)
    key = "retry/key.gz"
    q.enqueue(key=key, data=b"data")

    remote = _make_remote_store(fail=True)
    for expected_count in range(1, 4):
        q.migrate_pending(lambda: remote)
        manifest = json.loads(q._manifest_path(key).read_text())
        assert manifest["retry_count"] == expected_count
        assert q.has_pending(key)


def test_migrate_pending_dead_letters_after_max_retries(tmp_path: Path) -> None:
    max_retries = 3
    q = _make_queue(tmp_path, max_retries=max_retries)
    key = "dead/key.gz"
    data = b"doomed"
    q.enqueue(key=key, data=data)

    remote = _make_remote_store(fail=True)
    for _ in range(max_retries):
        q.migrate_pending(lambda: remote)

    # After max_retries, blob should be dead-lettered.
    assert not q.has_pending(key)
    assert q.list_pending_keys() == []

    dead_blob = q._dead_dir / _safe_key(key)
    assert dead_blob.exists()
    assert dead_blob.read_bytes() == data


def test_migrate_pending_succeeds_after_transient_failure(tmp_path: Path) -> None:
    q = _make_queue(tmp_path, max_retries=5)
    key = "transient/key.gz"
    data = b"will succeed eventually"
    q.enqueue(key=key, data=data)

    # Fail once, then succeed.
    remote = _make_remote_store(fail_times=1)
    succeeded, failed = q.migrate_pending(lambda: remote)
    assert (succeeded, failed) == (0, 1)
    assert q.has_pending(key)

    succeeded, failed = q.migrate_pending(lambda: remote)
    assert (succeeded, failed) == (1, 0)
    assert not q.has_pending(key)


def test_migrate_pending_store_factory_failure(tmp_path: Path) -> None:
    """When the remote store factory itself fails, report all as failed."""
    q = _make_queue(tmp_path)
    q.enqueue(key="k.gz", data=b"x")

    def _bad_factory():
        raise RuntimeError("cannot connect to S3")

    succeeded, failed = q.migrate_pending(_bad_factory)
    assert succeeded == 0
    assert failed == 1
    assert q.has_pending("k.gz")


# ---------------------------------------------------------------------------
# enqueue_bytes_via_spillway (blob_uploads wrapper)
# ---------------------------------------------------------------------------


def test_enqueue_bytes_via_spillway_returns_stored_bytes(tmp_path: Path, monkeypatch) -> None:
    q = _make_queue(tmp_path)
    monkeypatch.setattr(
           "src.backend.services.blob_spillway.get_blob_spillway_queue",
        lambda: q,
    )

    result = enqueue_bytes_via_spillway(
        key="debug/turns/req1/payload.json.gz",
        data=b"compressed payload",
        content_type="application/json",
        metadata={"sha256": "abc"},
    )

    assert isinstance(result, StoredBytes)
    assert result.ref.backend == "spillway"
    assert result.ref.key == "debug/turns/req1/payload.json.gz"
    assert result.size_bytes == len(b"compressed payload")
    assert result.sha256 == hashlib.sha256(b"compressed payload").hexdigest()


def test_enqueue_bytes_via_spillway_raises_blob_upload_error_on_disk_failure(
    tmp_path: Path, monkeypatch
) -> None:
    bad_queue = MagicMock()
    bad_queue.enqueue.side_effect = OSError("disk full")
    monkeypatch.setattr(
           "src.backend.services.blob_spillway.get_blob_spillway_queue",
        lambda: bad_queue,
    )

    with pytest.raises(BlobUploadError, match="Spillway enqueue failed"):
        enqueue_bytes_via_spillway(key="k.gz", data=b"x")


# ---------------------------------------------------------------------------
# is_spillway_enabled
# ---------------------------------------------------------------------------


def test_is_spillway_enabled_default(monkeypatch) -> None:
    monkeypatch.delenv("VON_BLOB_SPILLWAY_ENABLED", raising=False)
    assert is_spillway_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "False", "NO"])
def test_is_spillway_enabled_false(val: str, monkeypatch) -> None:
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", val)
    assert is_spillway_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "True"])
def test_is_spillway_enabled_true(val: str, monkeypatch) -> None:
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", val)
    assert is_spillway_enabled() is True


# ---------------------------------------------------------------------------
# load_debug_payload_blob_ref: local spillway fallback
# ---------------------------------------------------------------------------


class _FakeRemoteStore:
    """Fake remote store that raises for any get_bytes call."""

    def get_bytes(self, key: str) -> bytes:
        raise RuntimeError(f"Remote unavailable: {key}")

    def put_bytes(self, key: str, data: bytes, **kw: Any) -> BlobRef:
        return BlobRef(backend="s3", key=key, uri=f"s3://b/{key}", size_bytes=len(data))

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None:
        pass

    def list(self, prefix: str = "") -> list[str]:
        return []


def test_load_debug_payload_blob_ref_falls_back_to_spillway(
    tmp_path: Path, monkeypatch
) -> None:
    """When backend=spillway and remote fails, local spillway bytes are returned."""
    q = _make_queue(tmp_path)
    payload = {"messages": ["hello", "world"]}
    raw = json.dumps(payload).encode("utf-8")
    compressed = gzip.compress(raw)
    sha256 = hashlib.sha256(compressed).hexdigest()
    raw_sha256 = hashlib.sha256(raw).hexdigest()

    key = "debug/turns/req/messages.json.gz"
    q.enqueue(key=key, data=compressed, content_type="application/json")

    monkeypatch.setattr(
           "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _FakeRemoteStore(),
    )
    monkeypatch.setattr(
        "src.backend.services.blob_spillway.get_blob_spillway_queue",
        lambda: q,
    )
    # Also patch in debug_payload_store's import of get_blob_spillway_queue
    monkeypatch.setattr(
        "src.backend.services.debug_payload_store.get_blob_spillway_queue",
        lambda: q,
        raising=False,
    )

    ref_payload = {
        "schema_version": "debug_payload_blob_ref.v1",
        "offloaded": True,
        "payload_kind": "chat_history.llm_debug_data",
        "field_path": "messages",
        "content_type": "application/json",
        "compression": "gzip",
        "content_encoding": "utf-8",
        "original_size_bytes": len(raw),
        "stored_size_bytes": len(compressed),
        "raw_sha256": raw_sha256,
        "sha256": sha256,
        "summary": {"type": "object"},
        "blob_ref": {
            "backend": "spillway",
            "key": key,
            "uri": str(q._blob_path(key)),
        },
        "created_at_utc": "2026-06-01T00:00:00+00:00",
    }

    result = load_debug_payload_blob_ref(ref_payload)
    assert result == payload


def test_load_debug_payload_blob_ref_no_fallback_for_non_spillway_backend(
    tmp_path: Path, monkeypatch
) -> None:
    """When backend is not 'spillway', remote failure is re-raised immediately."""
    monkeypatch.setattr(
           "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _FakeRemoteStore(),
    )

    key = "debug/turns/req/messages.json.gz"
    ref_payload = {
        "schema_version": "debug_payload_blob_ref.v1",
        "offloaded": True,
        "payload_kind": "chat_history.llm_debug_data",
        "field_path": "messages",
        "content_type": "application/json",
        "compression": "gzip",
        "content_encoding": "utf-8",
        "original_size_bytes": 100,
        "stored_size_bytes": 50,
        "raw_sha256": "abc",
        "sha256": "def",
        "summary": {},
        "blob_ref": {
            "backend": "s3",  # NOT spillway
            "key": key,
            "uri": f"s3://bucket/{key}",
        },
        "created_at_utc": "2026-06-01T00:00:00+00:00",
    }

    with pytest.raises(RuntimeError, match="Remote unavailable"):
        load_debug_payload_blob_ref(ref_payload)


# ---------------------------------------------------------------------------
# compact_debug_payload_for_storage routes through spillway when enabled
# ---------------------------------------------------------------------------


def test_compact_routes_through_spillway_when_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    """compact_debug_payload_for_storage uses spillway instead of remote when enabled."""
    q = _make_queue(tmp_path)
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "true")
    monkeypatch.setattr(
        "src.backend.services.blob_spillway.get_blob_spillway_queue",
        lambda: q,
    )
        # Remote store should NOT be called.
    remote = MagicMock()
    remote.put_bytes.side_effect = RuntimeError("should not call remote")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: remote,
    )

    large_payload = {"messages": ["x" * 5000]}
    result = compact_debug_payload_for_storage(
        large_payload,
        root_kind="chat_history.llm_debug_data",
        namespace="#V#test",
        request_id="req-spillway-1",
        threshold_bytes=512,
        fail_soft=False,
    )

    # Should have offloaded (blob ref returned, no remote call).
    assert result.offloaded_count == 1
    remote.put_bytes.assert_not_called()

    # Spillway should have a pending key.
    pending = q.list_pending_keys()
    assert len(pending) == 1

    # The walk replaces the "messages" value with a blob-ref payload.
    messages_ref = result.payload.get("messages")
    assert isinstance(messages_ref, dict), f"Expected blob-ref dict, got: {messages_ref!r}"
    blob_ref = messages_ref.get("blob_ref")
    assert isinstance(blob_ref, dict)
    assert blob_ref.get("backend") == "spillway"


def test_compact_falls_back_to_remote_when_spillway_disabled(monkeypatch) -> None:
    """When spillway is disabled, put_bytes_durable is called as before."""
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "false")

    from src.backend.services.blob_store import BlobRef

    remote = MagicMock()
    remote.put_bytes.return_value = BlobRef(
        backend="s3", key="debug/turns/req/messages.abc.json.gz",
        uri="s3://b/debug/turns/req/messages.abc.json.gz", size_bytes=50
    )
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: remote,
    )

    large_payload = {"messages": ["x" * 5000]}
    result = compact_debug_payload_for_storage(
        large_payload,
        root_kind="chat_history.llm_debug_data",
        namespace="#V#test",
        request_id="req-remote-1",
        threshold_bytes=512,
        fail_soft=False,
    )

    assert result.offloaded_count == 1
    remote.put_bytes.assert_called_once()

    messages_ref = result.payload.get("messages")
    assert isinstance(messages_ref, dict)
    blob_ref = messages_ref.get("blob_ref")
    assert isinstance(blob_ref, dict)
    assert blob_ref.get("backend") == "s3"
