"""Unit tests for chat_history_service segment ordering/splitting.

These tests use a fake collection so they don't require Mongo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from unittest.mock import MagicMock, patch

from src.backend.services.blob_store import BlobRef
from src.backend.services.debug_payload_store import compact_debug_payload_for_storage


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def _evaluate_expression(self, doc, expr):
        if isinstance(expr, str) and expr.startswith("$"):
            return doc.get(expr[1:])
        if isinstance(expr, dict) and "$ifNull" in expr:
            args = expr["$ifNull"]
            if isinstance(args, list) and len(args) == 2:
                value = self._evaluate_expression(doc, args[0])
                if value is not None:
                    return value
                return self._evaluate_expression(doc, args[1])
        return expr

    def _matches_query(self, doc, query):
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches_query(doc, clause) for clause in value):
                    return False
                continue

            if isinstance(value, dict):
                if "$exists" in value:
                    exists = key in doc
                    if bool(value["$exists"]) != exists:
                        return False
                if "$eq" in value:
                    if doc.get(key) != value["$eq"]:
                        return False
                if "$in" in value:
                    if doc.get(key) not in value["$in"]:
                        return False
            else:
                if doc.get(key) != value:
                    return False
        return True

    def find_one(self, query, projection=None):
        for doc in self._docs:
            if not self._matches_query(doc, query):
                continue
            if isinstance(projection, dict):
                history_proj = projection.get("history")
                if isinstance(history_proj, dict) and "$slice" in history_proj:
                    slice_spec = history_proj["$slice"]
                    history = list(doc.get("history") or [])
                    if isinstance(slice_spec, list) and len(slice_spec) == 2:
                        start, count = slice_spec
                        history = history[start : start + count]
                    elif isinstance(slice_spec, int):
                        history = history[slice_spec:]
                    trimmed = dict(doc)
                    trimmed["history"] = history
                    return trimmed
            return doc
        return None

    def aggregate(self, pipeline):
        docs = list(self._docs)
        for stage in pipeline:
            if "$match" in stage:
                query = stage["$match"]
                docs = [doc for doc in docs if self._matches_query(doc, query)]
                continue
            if "$project" in stage:
                projection = stage["$project"]
                projected = []
                for doc in docs:
                    out = {}
                    for key, spec in projection.items():
                        if isinstance(spec, dict) and "$slice" in spec:
                            slice_spec = spec["$slice"]
                            history = list(doc.get("history") or [])
                            if isinstance(slice_spec, list):
                                if len(slice_spec) == 2:
                                    source, count = slice_spec
                                    source_value = self._evaluate_expression(doc, source)
                                    if isinstance(source_value, list):
                                        history = list(source_value)
                                        if isinstance(count, int):
                                            history = history[count:] if count < 0 else history[:count]
                                        else:
                                            history = []
                                    else:
                                        start, count = slice_spec
                                        if isinstance(start, int) and start < 0:
                                            start = len(history) + start
                                        history = history[start : start + count]
                                elif len(slice_spec) == 3:
                                    source, start, count = slice_spec
                                    source_value = self._evaluate_expression(doc, source)
                                    if isinstance(source_value, list):
                                        history = list(source_value)
                                    if isinstance(start, int) and start < 0:
                                        start = len(history) + start
                                    history = history[start : start + count]
                            elif isinstance(slice_spec, int):
                                history = history[slice_spec:]
                            out[key] = history
                            continue
                        if isinstance(spec, dict) and "$size" in spec:
                            target = self._evaluate_expression(doc, spec["$size"])
                            out[key] = len(target) if isinstance(target, list) else 0
                            continue
                        if spec in (1, True):
                            out[key] = doc.get(key)
                            continue
                        if isinstance(spec, str) and spec.startswith("$"):
                            out[key] = doc.get(spec[1:])
                            continue
                        out[key] = spec
                    projected.append(out)
                docs = projected
                continue
        return iter(docs)


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
        return [
            write["key"] for write in self.writes if write["key"].startswith(prefix)
        ]


def test_get_chat_history_segments_skips_empty_segments(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "history": [
                {"role": "system", "content": "__RESET__"},
                {
                    "role": "user",
                    "content": "hi",
                    "timestamp": datetime(2025, 1, 1, tzinfo=timezone.utc),
                },
                {"role": "system", "content": "__RESET__"},
                {"role": "system", "content": "__RESET__"},
                {
                    "role": "assistant",
                    "content": "ok",
                    "timestamp": datetime(2025, 1, 1, tzinfo=timezone.utc),
                },
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    segments_result = chat_history_service.get_chat_history_segments("#V#u", "s1")
    assert isinstance(segments_result, list)
    segments = segments_result

    # Should skip the leading empty segment and the double-reset empties.
    assert len(segments) == 2
    assert [m["content"] for m in segments[0]] == ["hi"]
    assert [m["content"] for m in segments[1]] == ["ok"]


def test_get_chat_history_segments_scopes_to_requested_session(monkeypatch):
    from src.backend.services import chat_history_service

    older = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
    newer = datetime(2025, 1, 2, 10, 0, tzinfo=timezone.utc)

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s_old",
            "updated_at": newer,  # misleading: updated_at is newer
            "history": [
                {"role": "user", "content": "old", "timestamp": older},
            ],
        },
        {
            "_id": "2",
            "user_id": "#V#u",
            "session_id": "s_new",
            "updated_at": older,  # misleading: updated_at is older
            "history": [
                {"role": "user", "content": "new", "timestamp": newer},
            ],
        },
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    segments_result = chat_history_service.get_chat_history_segments("#V#u", "s_new")
    assert isinstance(segments_result, list)
    segments = segments_result

    assert len(segments) == 1
    assert segments[0][0]["content"] == "new"


def test_get_chat_history_segments_chunks_large_segments(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {"role": "user", "content": "m1"},
                {"role": "assistant", "content": "m2"},
                {"role": "user", "content": "m3"},
                {"role": "assistant", "content": "m4"},
                {"role": "user", "content": "m5"},
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    segments_result = chat_history_service.get_chat_history_segments(
        "#V#u", "s1", segment_size=2
    )
    assert isinstance(segments_result, list)
    segments = segments_result

    assert [len(segment) for segment in segments] == [2, 2, 1]
    assert [m["content"] for m in segments[0]] == ["m1", "m2"]
    assert [m["content"] for m in segments[1]] == ["m3", "m4"]
    assert [m["content"] for m in segments[2]] == ["m5"]


def test_get_chat_history_segments_strips_debug_when_requested(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {
                    "role": "assistant",
                    "content": "ok",
                    "llm_debug_data": {"model": "demo"},
                }
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    segments_result = chat_history_service.get_chat_history_segments(
        "#V#u",
        "s1",
        include_locations=True,
        include_debug=False,
    )
    assert isinstance(segments_result, list)
    segments = segments_result

    assert len(segments) == 1
    assert len(segments[0]) == 1
    assert "llm_debug_data" not in segments[0][0]


def test_get_chat_history_debug_entry_hydrates_blob_refs(monkeypatch):
    from src.backend.services import chat_history_service

    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    original_debug = {
        "request_id": "req-hydrate",
        "messages": [{"role": "tool", "content": "x" * 2000}],
    }
    compacted = compact_debug_payload_for_storage(
        original_debug,
        root_kind="chat_history.llm_debug_data",
        namespace="#V#u",
        request_id="req-hydrate",
        threshold_bytes=512,
    )
    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {
                    "role": "assistant",
                    "content": "ok",
                    "llm_debug_data": compacted.payload,
                }
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    debug = chat_history_service.get_chat_history_debug_entry(
        user_id="#V#u",
        session_id="s1",
        history_index=0,
    )

    assert debug == original_debug


def test_add_message_to_history_offloads_large_content_and_get_chat_history_hydrates(
    monkeypatch,
):
    from src.backend.services import chat_history_service

    store = _FakeBlobStore()
    mock_coll = MagicMock()

    monkeypatch.setenv("VON_DEBUG_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )

    docs: list[dict[str, Any]] = []

    def _capture_update_one(*args, **kwargs):
        payload = args[1]["$push"]["history"]
        docs[:] = [
            {
                "_id": "history-1",
                "user_id": "#V#u",
                "session_id": "s1",
                "history": [payload],
            }
        ]
        return MagicMock()

    mock_coll.update_one.side_effect = _capture_update_one

    def _get_collection(*args, **kwargs):
        if kwargs.get("read_only"):
            return _FakeCollection(docs)
        return mock_coll

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            side_effect=_get_collection,
        ),
        patch(
            "src.backend.services.chat_history_service.get_session_context",
            return_value={"namespace": "#V#u"},
        ),
        patch("src.backend.services.chat_history_service.get_rag_service", None),
    ):
        large_content = "x" * 5000
        chat_history_service.add_message_to_history(
            user_id="#V#u",
            session_id="s1",
            message={"role": "assistant", "content": large_content},
        )

        stored_entry = docs[0]["history"][0]
        assert isinstance(stored_entry["content"], dict)
        assert stored_entry["content"]["schema_version"] == "debug_payload_blob_ref.v1"

        history = chat_history_service.get_chat_history(user_id="#V#u", session_id="s1")

    assert history[0]["content"] == large_content


def test_get_chat_history_segments_reports_truncation(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {"role": "user", "content": "m1"},
                {"role": "assistant", "content": "m2"},
                {"role": "user", "content": "m3"},
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    result = chat_history_service.get_chat_history_segments(
        "#V#u",
        "s1",
        history_tail_limit=2,
        return_meta=True,
    )
    assert isinstance(result, tuple)
    segments, meta = result

    assert isinstance(segments, list)
    assert meta["history_truncated"] is True


def test_get_chat_history_segments_handles_missing_history_field_with_tail_limit(
    monkeypatch,
):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    result = chat_history_service.get_chat_history_segments(
        "#V#u",
        "s1",
        history_tail_limit=25,
        return_meta=True,
    )

    assert result == ([], {"history_truncated": False})


def test_get_chat_history_segments_offsets_locations_with_tail_limit(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {"role": "user", "content": "m1"},
                {"role": "assistant", "content": "m2"},
                {"role": "user", "content": "m3"},
                {"role": "assistant", "content": "m4"},
                {"role": "user", "content": "m5"},
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    segments_result = chat_history_service.get_chat_history_segments(
        "#V#u",
        "s1",
        include_locations=True,
        history_tail_limit=2,
    )
    assert isinstance(segments_result, list)
    segments = segments_result

    assert len(segments) == 1
    assert [m["content"] for m in segments[0]] == ["m4", "m5"]
    assert [m["history_location"]["history_index"] for m in segments[0]] == [3, 4]


def test_get_chat_history_debug_entry_returns_payload(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok", "llm_debug_data": {"model": "demo"}},
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    debug_data = chat_history_service.get_chat_history_debug_entry(
        user_id="#V#u",
        session_id="s1",
        history_index=1,
    )

    assert debug_data == {"model": "demo"}


def test_get_chat_history_prefers_exact_namespace_document_over_legacy_fallback(
    monkeypatch,
):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "legacy",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [{"role": "assistant", "content": "legacy"}],
        },
        {
            "_id": "exact",
            "user_id": "#V#u",
            "session_id": "s1",
            "namespace": "#V#u@org",
            "history": [{"role": "assistant", "content": "exact"}],
        },
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    history = chat_history_service.get_chat_history(
        "#V#u",
        "s1",
        namespace="#V#u@org",
    )

    assert [entry["content"] for entry in history] == ["exact"]


def test_get_chat_history_debug_entry_prefers_exact_namespace_document_over_legacy_fallback(
    monkeypatch,
):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "legacy",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {
                    "role": "assistant",
                    "content": "legacy",
                    "llm_debug_data": {"model": "legacy-model"},
                }
            ],
        },
        {
            "_id": "exact",
            "user_id": "#V#u",
            "session_id": "s1",
            "namespace": "#V#u@org",
            "history": [
                {
                    "role": "assistant",
                    "content": "exact",
                    "llm_debug_data": {"model": "exact-model"},
                }
            ],
        },
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    debug_data = chat_history_service.get_chat_history_debug_entry(
        user_id="#V#u",
        session_id="s1",
        history_index=0,
        namespace="#V#u@org",
    )

    assert debug_data == {"model": "exact-model"}
