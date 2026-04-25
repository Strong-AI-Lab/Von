from __future__ import annotations

from src.backend.services import turn_execution_record_service as service
from src.backend.services.blob_store import BlobRef


class _FakeResult:
    modified_count = 1
    matched_count = 1
    upserted_id = None


class _FakeCollection:
    def __init__(self) -> None:
        self.payload = None

    def list_indexes(self):
        return [{"name": "request_id_unique"}, {"name": "namespace_created_desc"}]

    def update_one(self, _filter, update, upsert=False):
        self.payload = update["$set"]
        return _FakeResult()


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
        raise KeyError(key)

    def exists(self, key):
        return False

    def delete(self, key):
        return None

    def list(self, prefix=""):
        return []


def test_turn_execution_record_projection_offloads_large_diagnostics(
    monkeypatch,
) -> None:
    coll = _FakeCollection()
    store = _FakeBlobStore()
    monkeypatch.setenv("VON_DEBUG_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")
    monkeypatch.setattr(service, "get_turn_execution_records_collection", lambda: coll)
    monkeypatch.setattr(
        service,
        "ensure_turn_execution_record_execution_correctness",
        lambda record: dict(record),
    )
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )

    result = service.upsert_turn_execution_record_projection(
        record={
            "request_id": "req-ter-blob",
            "execution": {
                "diagnostic_events": [
                    {
                        "event_kind": "tool_call_end",
                        "llm_request": {
                            "context_messages": [
                                {"role": "tool", "content": "x" * 2000}
                            ]
                        },
                    }
                ]
            },
        },
        namespace="#V#michael@org",
        session_id="session-blob",
    )

    assert result["updated"] is True
    assert store.writes
    assert isinstance(coll.payload, dict)
    diagnostic_events = coll.payload["execution"]["diagnostic_events"]
    assert diagnostic_events["schema_version"] == "debug_payload_blob_ref.v1"
    assert diagnostic_events["field_path"] == "execution.diagnostic_events"
