from __future__ import annotations

from scripts import offload_debug_payloads_to_blob as script
from src.backend.services.blob_store import BlobRef


class _Cursor(list):
    def limit(self, count):
        return _Cursor(self[:count])


class _Collection:
    def __init__(self, docs):
        self.docs = docs
        self.updates = []

    def find(self, *args, **kwargs):
        return _Cursor(self.docs)

    def update_one(self, query, update):
        self.updates.append({"query": query, "update": update})
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                for key, value in update.get("$set", {}).items():
                    _apply_dotted_set(doc, key, value)
        return None


def _apply_dotted_set(doc, key, value):
    parts = str(key).split(".")
    current = doc
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current.setdefault(part, {})
    final = parts[-1]
    if isinstance(current, list):
        current[int(final)] = value
    else:
        current[final] = value


class _FakeBlobStore:
    def __init__(self) -> None:
        self.writes = []

    def put_bytes(self, key, data, *, content_type=None, metadata=None):
        self.writes.append({"key": key, "data": data, "metadata": dict(metadata or {})})
        return BlobRef(
            backend="local",
            key=key,
            uri=f"local://{key}",
            content_type=content_type,
            size_bytes=len(data),
            metadata=dict(metadata or {}),
        )

    def get_bytes(self, key):
        for write in self.writes:
            if write["key"] == key:
                return write["data"]
        raise KeyError(key)

    def exists(self, key):
        return False

    def delete(self, key):
        return None

    def list(self, prefix=""):
        return []


def test_chat_history_debug_offload_dry_run_counts_candidates() -> None:
    coll = _Collection(
        [
            {
                "_id": "chat-1",
                "namespace": "#V#michael@org",
                "history": [
                    {
                        "role": "assistant",
                        "content": "ok",
                        "llm_debug_data": {
                            "request_id": "req-migrate",
                            "messages": [{"role": "tool", "content": "x" * 2000}],
                        },
                    }
                ],
            }
        ]
    )

    stats = script._scan_chat_history(
        coll=coll,
        apply=False,
        limit=None,
        threshold_bytes=512,
        tool_threshold_bytes=512,
    )

    assert stats.scanned == 1
    assert stats.candidates == 1
    assert stats.updated == 0
    assert not coll.updates


def test_chat_history_debug_offload_apply_is_idempotent(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    coll = _Collection(
        [
            {
                "_id": "chat-1",
                "namespace": "#V#michael@org",
                "history": [
                    {
                        "role": "assistant",
                        "content": "ok",
                        "llm_debug_data": {
                            "request_id": "req-migrate",
                            "messages": [{"role": "tool", "content": "x" * 2000}],
                        },
                    }
                ],
            }
        ]
    )

    first = script._scan_chat_history(
        coll=coll,
        apply=True,
        limit=None,
        threshold_bytes=512,
        tool_threshold_bytes=512,
    )
    second = script._scan_chat_history(
        coll=coll,
        apply=True,
        limit=None,
        threshold_bytes=512,
        tool_threshold_bytes=512,
    )

    assert first.updated == 1
    assert first.offloaded_payloads == 1
    assert second.updated == 0
    assert len(store.writes) == 1
    stored_debug = coll.docs[0]["history"][0]["llm_debug_data"]
    assert stored_debug["messages"]["schema_version"] == "debug_payload_blob_ref.v1"


def test_turn_execution_record_offload_apply_rewrites_large_diagnostics(
    monkeypatch,
) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    coll = _Collection(
        [
            {
                "_id": "ter-1",
                "request_id": "req-ter-migrate",
                "namespace": "#V#michael@org",
                "execution": {
                    "diagnostic_events": [
                        {
                            "event_kind": "llm_request_prepared",
                            "llm_request": {
                                "context_messages": [
                                    {"role": "tool", "content": "x" * 2000}
                                ],
                            },
                        }
                    ]
                },
            }
        ]
    )

    stats = script._scan_turn_execution_records(
        coll=coll,
        apply=True,
        limit=None,
        threshold_bytes=512,
    )

    assert stats.updated == 1
    assert stats.offloaded_payloads == 1
    stored_events = coll.docs[0]["execution"]["diagnostic_events"]
    assert stored_events["schema_version"] == "debug_payload_blob_ref.v1"
    assert coll.docs[0]["debug_payload_offload_migration"]["status"] == "applied"


def test_chat_history_apply_does_not_update_when_blob_store_fails(monkeypatch) -> None:
    class _FailingBlobStore:
        def put_bytes(self, *args, **kwargs):
            raise RuntimeError("blob store unavailable")

    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _FailingBlobStore(),
    )
    original_debug = {
        "request_id": "req-migrate",
        "messages": [{"role": "tool", "content": "x" * 2000}],
    }
    coll = _Collection(
        [
            {
                "_id": "chat-1",
                "namespace": "#V#michael@org",
                "history": [
                    {
                        "role": "assistant",
                        "content": "ok",
                        "llm_debug_data": dict(original_debug),
                    }
                ],
            }
        ]
    )

    stats = script._scan_chat_history(
        coll=coll,
        apply=True,
        limit=None,
        threshold_bytes=512,
        tool_threshold_bytes=512,
    )

    assert stats.errors == 1
    assert stats.updated == 0
    assert not coll.updates
    assert coll.docs[0]["history"][0]["llm_debug_data"] == original_debug
