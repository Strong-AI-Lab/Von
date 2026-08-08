from __future__ import annotations

import json
from typing import Any, Mapping

from src.backend.services.blob_store import BlobRef
from src.backend.services.debug_payload_store import (
    compact_debug_payload_for_storage,
    hydrate_debug_payload_blob_refs,
    load_debug_payload_blob_ref,
    resolve_debug_payload_blob_ref,
)


class _FakeBlobStore:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        self.writes.append(
            {
                "key": key,
                "data": data,
                "content_type": content_type,
                "metadata": dict(metadata or {}),
            }
        )
        return BlobRef(
            backend="local",
            key=key,
            uri=f"local://{key}",
            content_type=content_type,
            size_bytes=len(data),
            etag="etag",
            metadata=dict(metadata or {}),
        )

    def get_bytes(self, key: str) -> bytes:
        for write in self.writes:
            if write["key"] == key:
                return write["data"]
        raise KeyError(key)

    def exists(self, key: str) -> bool:
        return any(write["key"] == key for write in self.writes)

    def delete(self, key: str) -> None:
        self.writes = [write for write in self.writes if write["key"] != key]

    def list(self, prefix: str = "") -> list[str]:
        return [
            write["key"] for write in self.writes if write["key"].startswith(prefix)
        ]


def test_compact_debug_payload_offloads_oversized_field(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "0")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )

    payload = {
        "request_id": "req-blob-1",
        "messages": [{"role": "tool", "content": "x" * 2000}],
        "context_stats": {"sent_to_llm": {"message_count": 1}},
    }

    result = compact_debug_payload_for_storage(
        payload,
        root_kind="chat_history.llm_debug_data",
        namespace="#V#michael@org",
        request_id="req-blob-1",
        threshold_bytes=512,
    )

    assert result.offloaded_count == 1
    assert result.degraded_count == 0
    assert len(store.writes) == 1
    ref = result.payload["messages"]
    assert ref["schema_version"] == "debug_payload_blob_ref.v1"
    assert ref["field_path"] == "messages"
    assert ref["blob_ref"]["key"].startswith("debug/turns/")
    assert ref["summary"]["type"] == "array"
    assert ref["summary"]["item_count"] == 1
    assert load_debug_payload_blob_ref(ref) == payload["messages"]


def test_compact_debug_payload_degrades_without_reinlining_on_blob_failure(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "0")

    def _raise_store():
        raise RuntimeError("blob store unavailable")

    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        _raise_store,
    )

    result = compact_debug_payload_for_storage(
        {"messages": [{"role": "tool", "content": "x" * 2000}]},
        root_kind="chat_history.llm_debug_data",
        threshold_bytes=512,
        fail_soft=True,
    )

    degraded = result.payload["messages"]
    assert result.offloaded_count == 0
    assert result.degraded_count == 1
    assert degraded["schema_version"] == "debug_payload_offload_degraded.v1"
    assert degraded["reason"] == "blob_upload_failed"
    assert "x" * 1000 not in json.dumps(degraded)


def test_hydrate_debug_payload_blob_refs_restores_nested_payload(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "0")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )

    payload = {
        "request_id": "req-blob-2",
        "turn_execution_diagnostics": {
            "stage_diagnostics": [{"detail": "x" * 2000}]
        },
    }
    compacted = compact_debug_payload_for_storage(
        payload,
        root_kind="chat_history.llm_debug_data",
        namespace="#V#michael@org",
        request_id="req-blob-2",
        threshold_bytes=512,
    )

    hydrated = hydrate_debug_payload_blob_refs(compacted.payload, fail_soft=False)

    assert hydrated.hydrated_count == 1
    assert hydrated.error_count == 0
    assert hydrated.payload == payload


def test_hydration_recurses_through_nested_offload_layers(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "0")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )

    inner_payload = {"stage_diagnostics": [{"detail": "x" * 2000}]}
    compacted_inner = compact_debug_payload_for_storage(
        inner_payload,
        root_kind="turn_execution_diagnostics",
        namespace="#V#michael@org",
        request_id="req-nested-inner",
        threshold_bytes=512,
    )
    payload = {
        "turn_execution_diagnostics": {
            "nested": compacted_inner.payload,
            "padding": "y" * 2000,
        }
    }
    compacted_outer = compact_debug_payload_for_storage(
        payload,
        root_kind="chat_history.llm_debug_data",
        namespace="#V#michael@org",
        request_id="req-nested-outer",
        threshold_bytes=512,
    )

    hydrated = hydrate_debug_payload_blob_refs(
        compacted_outer.payload,
        fail_soft=False,
    )

    assert compacted_inner.offloaded_count == 1
    assert compacted_outer.offloaded_count == 1
    assert hydrated.hydrated_count == 2
    assert hydrated.error_count == 0
    assert hydrated.payload == {
        "turn_execution_diagnostics": {
            "nested": inner_payload,
            "padding": "y" * 2000,
        }
    }


def test_resolve_debug_payload_blob_ref_reports_typed_cache_states(
    monkeypatch,
    tmp_path,
) -> None:
    from src.backend.services.blob_spillway import BlobSpillwayQueue

    queue = BlobSpillwayQueue(tmp_path / "spillway")
    payload = {"messages": [{"role": "tool", "content": "cached"}]}
    raw = json.dumps(payload).encode("utf-8")
    import gzip
    import hashlib

    compressed = gzip.compress(raw)
    stored_sha = hashlib.sha256(compressed).hexdigest()
    raw_sha = hashlib.sha256(raw).hexdigest()
    key = "debug/turns/ns/req/messages.json.gz"
    queue.enqueue(key=key, data=compressed, sha256=stored_sha)

    class _RemoteStore:
        calls = 0

        def get_bytes(self, requested_key: str) -> bytes:
            self.calls += 1
            if requested_key == "debug/turns/ns/req/remote.json.gz":
                return compressed
            raise KeyError(requested_key)

    remote = _RemoteStore()
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "true")
    monkeypatch.setattr(
        "src.backend.services.blob_spillway.get_blob_spillway_queue",
        lambda: queue,
    )
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: remote,
    )

    def _ref(ref_key: str, backend: str = "spillway") -> dict[str, Any]:
        return {
            "schema_version": "debug_payload_blob_ref.v1",
            "offloaded": True,
            "payload_kind": "chat_history.llm_debug_data",
            "field_path": "messages",
            "compression": "gzip",
            "raw_sha256": raw_sha,
            "sha256": stored_sha,
            "blob_ref": {
                "backend": backend,
                "key": ref_key,
                "size_bytes": len(compressed),
            },
        }

    local_pending = resolve_debug_payload_blob_ref(_ref(key))
    queue.cache_committed(key=key, data=compressed, sha256=stored_sha)
    migrated_local = resolve_debug_payload_blob_ref(_ref(key))
    remote_fill = resolve_debug_payload_blob_ref(
        _ref("debug/turns/ns/req/remote.json.gz", backend="s3")
    )
    local_after_remote_fill = resolve_debug_payload_blob_ref(
        _ref("debug/turns/ns/req/remote.json.gz", backend="s3")
    )
    missing = resolve_debug_payload_blob_ref(_ref("debug/turns/ns/req/missing.json.gz"))
    unsafe = resolve_debug_payload_blob_ref(_ref("../secret.json.gz"))

    assert local_pending.status == "pending_local"
    assert migrated_local.status == "remote_committed_local_hit"
    assert remote_fill.status == "local_miss_remote_hit"
    assert local_after_remote_fill.status == "remote_committed_local_hit"
    assert missing.status == "missing_both"
    assert unsafe.status == "local_corrupt"
