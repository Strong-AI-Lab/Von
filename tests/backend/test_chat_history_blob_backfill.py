from __future__ import annotations

from typing import Any, Mapping

from src.backend.services.blob_store import BlobRef


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def _matches_query(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, value in query.items():
            if isinstance(value, dict) and "$in" in value:
                if doc.get(key) not in value["$in"]:
                    return False
                continue
            if doc.get(key) != value:
                return False
        return True

    def find(self, query: dict[str, Any]):
        return iter([doc for doc in self._docs if self._matches_query(doc, query)])

    def find_one(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        for doc in self._docs:
            if self._matches_query(doc, query):
                return doc
        return None

    def update_one(self, query: dict[str, Any], update: dict[str, Any]):
        for doc in self._docs:
            if not self._matches_query(doc, query):
                continue
            for key, value in (update.get("$set") or {}).items():
                doc[key] = value

            class _Result:
                matched_count = 1
                modified_count = 1

            return _Result()

        class _EmptyResult:
            matched_count = 0
            modified_count = 0

        return _EmptyResult()


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
        self.writes.append({"key": key, "data": data, "metadata": dict(metadata or {})})
        return BlobRef(
            backend="local",
            key=key,
            uri=f"local://{key}",
            content_type=content_type,
            size_bytes=len(data),
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
        return [write["key"] for write in self.writes if write["key"].startswith(prefix)]


def test_chat_history_blob_backfill_dry_run_reports_candidates(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "doc-1",
            "user_id": "#V#u",
            "namespace": "#V#u",
            "session_id": "s1",
            "history": [{"role": "assistant", "content": "x" * 5000}],
        }
    ]
    collection = _FakeCollection(docs)
    store = _FakeBlobStore()

    monkeypatch.setenv("VON_DEBUG_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: collection,
    )

    result = chat_history_service.backfill_chat_history_blob_payloads(
        user_concept_id="#V#u",
        dry_run=True,
    )

    assert result["status"] == "ok"
    assert result["sessions_examined"] == 1
    assert result["candidate_entries"] == 1
    assert result["entries_updated"] == 1
    assert isinstance(docs[0]["history"][0]["content"], str)


def test_chat_history_blob_backfill_rewrites_and_reads_back(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "doc-1",
            "user_id": "#V#u",
            "namespace": "#V#u",
            "session_id": "s1",
            "history": [{"role": "assistant", "content": "x" * 5000}],
        }
    ]
    collection = _FakeCollection(docs)
    store = _FakeBlobStore()

    monkeypatch.setenv("VON_DEBUG_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: collection,
    )

    result = chat_history_service.backfill_chat_history_blob_payloads(
        user_concept_id="#V#u",
        dry_run=False,
    )

    assert result["sessions_updated"] == 1
    assert result["entries_updated"] == 1
    assert docs[0]["history"][0]["content"]["schema_version"] == "debug_payload_blob_ref.v1"

    history = chat_history_service.get_chat_history(user_id="#V#u", session_id="s1")
    assert history[0]["content"] == "x" * 5000


def test_chat_history_blob_backfill_is_idempotent(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "doc-1",
            "user_id": "#V#u",
            "namespace": "#V#u",
            "session_id": "s1",
            "history": [{"role": "assistant", "content": "x" * 5000}],
        }
    ]
    collection = _FakeCollection(docs)
    store = _FakeBlobStore()

    monkeypatch.setenv("VON_DEBUG_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: collection,
    )

    first = chat_history_service.backfill_chat_history_blob_payloads(
        user_concept_id="#V#u",
        dry_run=False,
    )
    second = chat_history_service.backfill_chat_history_blob_payloads(
        user_concept_id="#V#u",
        dry_run=False,
    )

    assert first["entries_updated"] == 1
    assert second["entries_updated"] == 0
    assert second["candidate_entries"] == 0