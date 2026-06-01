from __future__ import annotations

from pymongo.errors import AutoReconnect

from scripts import offload_workflow_payloads_to_blob as script
from src.backend.services.blob_store import BlobRef


class _Cursor(list):
    def batch_size(self, count):
        return self

    def skip(self, count):
        return _Cursor(self[count:])

    def limit(self, count):
        return _Cursor(self[:count])

    def sort(self, key, direction):
        reverse = direction < 0
        return _Cursor(sorted(self, key=lambda item: item.get(key), reverse=reverse))


class _InterruptingCursor(_Cursor):
    def __init__(self, docs, interrupt_after):
        super().__init__(docs)
        self.interrupt_after = interrupt_after

    def batch_size(self, count):
        return self

    def skip(self, count):
        return _InterruptingCursor(self[count:], self.interrupt_after)

    def limit(self, count):
        return _InterruptingCursor(self[:count], self.interrupt_after)

    def __iter__(self):
        for index, doc in enumerate(list.__iter__(self)):
            if index >= self.interrupt_after:
                raise AutoReconnect("simulated cursor interruption")
            yield doc


class _UpdateResult:
    matched_count = 1


class _Collection:
    def __init__(self, docs):
        self.docs = docs
        self.updates = []

    def find(self, query=None, projection=None):
        query = dict(query or {})
        docs = []
        for doc in self.docs:
            include = True
            for key, expected in query.items():
                value = doc.get(key)
                if isinstance(expected, dict) and "$gt" in expected:
                    include = value > expected["$gt"]
                else:
                    include = value == expected
                if not include:
                    break
            if include:
                docs.append(doc)
        return _Cursor(docs)

    def update_one(self, query, update):
        self.updates.append({"query": query, "update": update})
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                for key, value in update.get("$set", {}).items():
                    _apply_dotted_set(doc, key, value)
        return _UpdateResult()


class _InterruptingCollection(_Collection):
    def __init__(self, docs, interrupt_after):
        super().__init__(docs)
        self.interrupt_after = interrupt_after

    def find(self, query=None, projection=None):
        return _InterruptingCursor(
            super().find(query=query, projection=projection),
            self.interrupt_after,
        )


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


def test_workflow_instance_offload_dry_run_counts_candidates() -> None:
    coll = _Collection(
        [
            {
                "_id": "instance-doc-1",
                "instance_id": "instance-1",
                "workflow_id": "#V#episode_evaluation_workflow",
                "namespace": "#V#michael@org",
                "status": "completed",
                "workflow_data": {
                    "llm_step_envelope": {"tool_messages": [{"content": "x" * 2000}]}
                },
            }
        ]
    )

    stats = script._scan_workflow_instances(
        coll=coll,
        apply=False,
        limit=None,
        threshold_bytes=512,
    )

    assert stats.scanned == 1
    assert stats.candidates == 1
    assert stats.updated == 0
    assert not coll.updates


def test_workflow_instance_page_by_id_resumes_after_id() -> None:
    coll = _Collection(
        [
            {
                "_id": "instance-doc-1",
                "instance_id": "instance-1",
                "workflow_id": "#V#workflow",
                "namespace": "#V#namespace",
                "status": "completed",
                "workflow_data": {"text": "x" * 2000},
            },
            {
                "_id": "instance-doc-2",
                "instance_id": "instance-2",
                "workflow_id": "#V#workflow",
                "namespace": "#V#namespace",
                "status": "completed",
                "workflow_data": {"text": "y" * 2000},
            },
        ]
    )

    stats = script._scan_workflow_instances(
        coll=coll,
        apply=False,
        limit=1,
        page_by_id=True,
        page_size=1,
        after_id="instance-doc-1",
        threshold_bytes=512,
    )

    assert stats.scanned == 1
    assert stats.candidates == 1
    assert stats.last_scanned_id == "instance-doc-2"


def test_workflow_instance_scan_reports_cursor_interruption() -> None:
    coll = _InterruptingCollection(
        [
            {
                "_id": "instance-doc-1",
                "instance_id": "instance-1",
                "workflow_id": "#V#workflow",
                "namespace": "#V#namespace",
                "status": "completed",
                "workflow_data": {"text": "x" * 2000},
            },
            {
                "_id": "instance-doc-2",
                "instance_id": "instance-2",
                "workflow_id": "#V#workflow",
                "namespace": "#V#namespace",
                "status": "completed",
                "workflow_data": {"text": "y" * 2000},
            },
        ],
        interrupt_after=1,
    )

    stats = script._scan_workflow_instances(
        coll=coll,
        apply=False,
        limit=2,
        threshold_bytes=512,
    )

    assert stats.scanned == 1
    assert stats.candidates == 1
    assert stats.errors == 1
    assert stats.last_scanned_id == "instance-doc-1"


def test_workflow_instance_offload_apply_is_idempotent(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    coll = _Collection(
        [
            {
                "_id": "instance-doc-1",
                "instance_id": "instance-1",
                "workflow_id": "#V#episode_evaluation_workflow",
                "namespace": "#V#michael@org",
                "status": "completed",
                "workflow_data": {
                    "llm_step_envelope": {"tool_messages": [{"content": "x" * 2000}]}
                },
            }
        ]
    )

    first = script._scan_workflow_instances(
        coll=coll,
        apply=True,
        limit=None,
        threshold_bytes=512,
    )
    second = script._scan_workflow_instances(
        coll=coll,
        apply=True,
        limit=None,
        threshold_bytes=512,
    )

    assert first.updated == 1
    assert first.offloaded_payloads == 1
    assert second.updated == 0
    assert len(store.writes) == 1
    stored = coll.docs[0]["workflow_data"]["llm_step_envelope"]
    assert stored["schema_version"] == "workflow_payload_blob_ref.v1"
    assert coll.docs[0]["workflow_payload_offload_migration"]["status"] == "applied"


def test_workflow_execution_offload_apply_rewrites_large_actions(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    coll = _Collection(
        [
            {
                "_id": "execution-doc-1",
                "execution_id": "execution-1",
                "instance_id": "instance-1",
                "workflow_id": "#V#episode_evaluation_workflow",
                "user_namespace": "#V#michael@org",
                "status": "completed",
                "actions": [{"state_id": "llm", "output": "x" * 2000}],
            }
        ]
    )

    stats = script._scan_workflow_executions(
        coll=coll,
        apply=True,
        limit=None,
        threshold_bytes=512,
    )

    assert stats.updated == 1
    assert stats.offloaded_payloads == 1
    stored = coll.docs[0]["actions"]
    assert stored["schema_version"] == "workflow_payload_blob_ref.v1"
    assert coll.updates[0]["query"] == {"_id": "execution-doc-1"}


def test_workflow_offload_does_not_update_when_blob_store_fails(monkeypatch) -> None:
    class _FailingBlobStore:
        def put_bytes(self, *args, **kwargs):
            raise RuntimeError("blob store unavailable")

    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _FailingBlobStore(),
    )
    original_workflow_data = {
        "llm_step_envelope": {"tool_messages": [{"content": "x" * 2000}]}
    }
    coll = _Collection(
        [
            {
                "_id": "instance-doc-1",
                "instance_id": "instance-1",
                "workflow_id": "#V#episode_evaluation_workflow",
                "namespace": "#V#michael@org",
                "status": "completed",
                "workflow_data": dict(original_workflow_data),
            }
        ]
    )

    stats = script._scan_workflow_instances(
        coll=coll,
        apply=True,
        limit=None,
        threshold_bytes=512,
    )

    assert stats.errors == 1
    assert stats.updated == 0
    assert not coll.updates
    assert coll.docs[0]["workflow_data"] == original_workflow_data