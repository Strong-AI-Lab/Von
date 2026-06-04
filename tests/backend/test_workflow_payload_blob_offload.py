from __future__ import annotations

import json
from typing import Any, Mapping

import pytest
from bson import BSON

from src.backend.services.blob_spillway import BlobSpillwayQueue
from src.backend.services.blob_store import BlobRef
from src.backend.services.workflow_payload_store import (
    compact_workflow_payload_for_storage,
    count_workflow_payload_offload_candidates,
    hydrate_workflow_payload_blob_refs,
    load_workflow_payload_blob_ref,
)
from src.backend.db.mongo_client import get_db
from src.backend.workflows import trace_store
from src.backend.workflows.durable import instance_manager as instance_manager_module
from src.backend.workflows.durable.instance_manager import WorkflowInstanceManager


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


class _FailingBlobStore:
    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        raise RuntimeError("blob store unavailable")


@pytest.fixture(autouse=True)
def _disable_spillway_by_default(monkeypatch) -> None:
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "0")


def _reset_mock_workflow_instances(monkeypatch) -> None:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    db = get_db()
    assert db is not None
    db.drop_collection(instance_manager_module.WORKFLOW_INSTANCES_COLLECTION)
    instance_manager_module._indexes_ensured = False


def test_compact_workflow_payload_offloads_known_heavy_field(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    workflow_data = {
        "paper_id": "#V#paper_1",
        "llm_step_envelope": {
            "tool_messages": [{"role": "tool", "content": "x" * 4000}],
            "rendered_prompt_variables": {"context": "y" * 2000},
        },
    }

    result = compact_workflow_payload_for_storage(
        workflow_data,
        record_family="workflow_instances.workflow_data",
        record_id="instance-1",
        namespace="#V#michael@org",
        workflow_id="#V#episode_evaluation_workflow",
        threshold_bytes=512,
        fail_soft=False,
    )

    assert result.offloaded_count == 1
    assert result.degraded_count == 0
    assert len(store.writes) == 1
    ref = result.payload["llm_step_envelope"]
    assert ref["schema_version"] == "workflow_payload_blob_ref.v1"
    assert ref["payload_kind"] == "workflow_instances.workflow_data"
    assert ref["field_path"] == "llm_step_envelope"
    assert ref["blob_ref"]["key"].startswith("workflows/payloads/")
    assert load_workflow_payload_blob_ref(ref) == workflow_data["llm_step_envelope"]


def test_compact_workflow_payload_can_enqueue_local_first_spillway(
    monkeypatch,
    tmp_path,
) -> None:
    store = _FakeBlobStore()
    queue = BlobSpillwayQueue(tmp_path / "spillway")
    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "1")
    monkeypatch.setattr(
        "src.backend.services.blob_spillway.get_blob_spillway_queue",
        lambda: queue,
    )
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    workflow_data = {
        "workflow_step_result_envelopes": [
            {"state_id": "llm", "output_payload": {"text": "z" * 3000}}
        ]
    }

    result = compact_workflow_payload_for_storage(
        workflow_data,
        record_family="workflow_instances.workflow_data",
        record_id="instance-spillway",
        threshold_bytes=512,
        fail_soft=False,
    )

    assert store.writes == []
    ref = result.payload["workflow_step_result_envelopes"]
    assert ref["blob_ref"]["backend"] == "spillway"
    assert ref["persistence_status"] == "queued_local_first"
    assert ref["remote_state"] == "pending"
    assert queue.has_pending(ref["blob_ref"]["key"])
    assert load_workflow_payload_blob_ref(ref) == workflow_data[
        "workflow_step_result_envelopes"
    ]


def test_hydrate_workflow_payload_blob_refs_restores_nested_payload(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    workflow_data = {
        "workflow_step_result_envelopes": [
            {"state_id": "llm", "output_payload": {"text": "z" * 3000}}
        ]
    }
    compacted = compact_workflow_payload_for_storage(
        workflow_data,
        record_family="workflow_instances.workflow_data",
        record_id="instance-2",
        threshold_bytes=512,
        fail_soft=False,
    )

    hydrated = hydrate_workflow_payload_blob_refs(compacted.payload, fail_soft=False)

    assert hydrated.hydrated_count == 1
    assert hydrated.error_count == 0
    assert hydrated.payload == workflow_data


def test_workflow_payload_hydration_uses_ref_backend_after_failover_read_failure(
    monkeypatch,
) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    payload = {"actions": [{"state_id": "llm", "output": "x" * 2000}]}
    compacted = compact_workflow_payload_for_storage(
        payload,
        record_family="workflow_executions",
        record_id="execution-1",
        threshold_bytes=512,
        fail_soft=False,
    )
    ref = compacted.payload["actions"]
    ref["blob_ref"]["backend"] = "s3"

    class _PrimaryReadFailStore:
        def get_bytes(self, key: str) -> bytes:
            raise RuntimeError("swift unauthorized")

    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _PrimaryReadFailStore(),
    )
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_for_backend_from_env",
        lambda backend: store if backend == "s3" else _PrimaryReadFailStore(),
    )

    assert load_workflow_payload_blob_ref(ref) == payload["actions"]


def test_workflow_payload_hydration_retries_backend_read_once(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    payload = {"actions": [{"state_id": "llm", "output": "x" * 2000}]}
    compacted = compact_workflow_payload_for_storage(
        payload,
        record_family="workflow_executions",
        record_id="execution-1",
        threshold_bytes=512,
        fail_soft=False,
    )
    ref = compacted.payload["actions"]
    ref["blob_ref"]["backend"] = "s3"

    class _PrimaryReadFailStore:
        def get_bytes(self, key: str) -> bytes:
            raise RuntimeError("swift unauthorized")

    class _FlakyBackendStore:
        def __init__(self) -> None:
            self.calls = 0

        def get_bytes(self, key: str) -> bytes:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("response stream incomplete")
            return store.get_bytes(key)

    backend_store = _FlakyBackendStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _PrimaryReadFailStore(),
    )
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_for_backend_from_env",
        lambda backend: backend_store,
    )

    assert load_workflow_payload_blob_ref(ref) == payload["actions"]
    assert backend_store.calls == 2


def test_workflow_payload_hydration_rejects_stored_size_mismatch(
    monkeypatch,
) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    payload = {"actions": [{"state_id": "llm", "output": "x" * 2000}]}
    compacted = compact_workflow_payload_for_storage(
        payload,
        record_family="workflow_executions",
        record_id="execution-1",
        threshold_bytes=512,
        fail_soft=False,
    )
    ref = dict(compacted.payload["actions"])
    blob_ref = dict(ref["blob_ref"])
    blob_ref["size_bytes"] = int(blob_ref["size_bytes"]) + 1
    ref["blob_ref"] = blob_ref

    with pytest.raises(ValueError, match="stored size mismatch"):
        load_workflow_payload_blob_ref(ref)


def test_workflow_payload_hydration_rejects_stored_sha_mismatch(
    monkeypatch,
) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    payload = {"actions": [{"state_id": "llm", "output": "x" * 2000}]}
    compacted = compact_workflow_payload_for_storage(
        payload,
        record_family="workflow_executions",
        record_id="execution-1",
        threshold_bytes=512,
        fail_soft=False,
    )
    ref = dict(compacted.payload["actions"])
    ref["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="stored SHA-256 mismatch"):
        load_workflow_payload_blob_ref(ref)


def test_workflow_payload_candidate_count_skips_existing_refs(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    payload = {
        "actions": [{"result": "x" * 2000}],
        "steps": [{"state": "short"}],
    }
    compacted = compact_workflow_payload_for_storage(
        payload,
        record_family="workflow_executions",
        record_id="execution-1",
        threshold_bytes=512,
        fail_soft=False,
    )

    assert count_workflow_payload_offload_candidates(payload, threshold_bytes=512) == 1
    assert (
        count_workflow_payload_offload_candidates(compacted.payload, threshold_bytes=512)
        == 0
    )
    assert "x" * 1000 not in json.dumps(compacted.payload)


def test_instance_manager_compacts_workflow_payload_field(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    monkeypatch.setenv("VON_WORKFLOW_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")
    workflow_data = {
        "llm_step_envelope": {"tool_messages": [{"content": "x" * 2000}]}
    }

    compacted = WorkflowInstanceManager._compact_instance_payload_field(
        workflow_data,
        field="workflow_data",
        instance_id="instance-1",
        namespace="#V#michael@org",
        workflow_id="#V#episode_evaluation_workflow",
    )

    assert compacted["llm_step_envelope"]["schema_version"] == (
        "workflow_payload_blob_ref.v1"
    )
    hydrated = hydrate_workflow_payload_blob_refs(compacted, fail_soft=False)
    assert hydrated.payload == workflow_data


def test_instance_manager_checkpoint_stores_bounded_blob_ref_and_hydrates(
    monkeypatch,
) -> None:
    _reset_mock_workflow_instances(monkeypatch)
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    monkeypatch.setenv("VON_WORKFLOW_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")

    manager = WorkflowInstanceManager()
    instance_id = manager.create_instance(
        "#V#episode_evaluation_workflow",
        user_id="user-1",
        org_id="org-1",
        namespace="#V#michael@org",
    )
    workflow_data = {
        "paper_id": "#V#paper_1",
        "last_action_outputs": {
            "tool": "evidence_loader",
            "messages": [{"role": "tool", "content": "x" * 10_000}],
        },
    }

    assert manager.checkpoint(
        instance_id,
        current_state="collect_evidence",
        workflow_data=workflow_data,
        step_index=2,
    )
    db = get_db()
    assert db is not None
    raw_doc = db[instance_manager_module.WORKFLOW_INSTANCES_COLLECTION].find_one(
        {"instance_id": instance_id}
    )
    assert raw_doc is not None
    raw_payload = raw_doc["workflow_data"]
    original_bson_size = len(BSON.encode({"workflow_data": workflow_data}))
    stored_bson_size = len(BSON.encode({"workflow_data": raw_payload}))

    assert raw_payload["last_action_outputs"]["schema_version"] == (
        "workflow_payload_blob_ref.v1"
    )
    assert stored_bson_size < original_bson_size
    assert stored_bson_size < 32_000
    assert store.writes

    hydrated_instance = manager.get_instance(instance_id)
    assert hydrated_instance is not None
    assert hydrated_instance.workflow_data == workflow_data


def test_instance_manager_completed_outputs_store_bounded_ref_and_hydrate(
    monkeypatch,
) -> None:
    _reset_mock_workflow_instances(monkeypatch)
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    monkeypatch.setenv("VON_WORKFLOW_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")

    manager = WorkflowInstanceManager()
    instance_id = manager.create_instance(
        "#V#episode_evaluation_workflow",
        user_id="user-1",
        org_id="org-1",
        namespace="#V#michael@org",
    )
    outputs = {
        "workflow_result_envelope": {
            "status": "completed",
            "diagnostic_evidence": [{"content": "y" * 10_000}],
        },
    }

    assert manager.mark_completed(
        instance_id,
        outputs=outputs,
        final_state="done",
    )
    db = get_db()
    assert db is not None
    raw_doc = db[instance_manager_module.WORKFLOW_INSTANCES_COLLECTION].find_one(
        {"instance_id": instance_id}
    )
    assert raw_doc is not None
    raw_payload = raw_doc["outputs"]

    assert raw_payload["workflow_result_envelope"]["schema_version"] == (
        "workflow_payload_blob_ref.v1"
    )
    assert len(BSON.encode({"outputs": raw_payload})) < len(
        BSON.encode({"outputs": outputs})
    )

    hydrated_instance = manager.get_instance(instance_id)
    assert hydrated_instance is not None
    assert hydrated_instance.outputs == outputs


def test_instance_manager_checkpoint_preserves_payload_when_blob_store_unavailable(
    monkeypatch,
) -> None:
    _reset_mock_workflow_instances(monkeypatch)
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: _FailingBlobStore(),
    )
    monkeypatch.setenv("VON_WORKFLOW_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")

    manager = WorkflowInstanceManager()
    instance_id = manager.create_instance(
        "#V#episode_evaluation_workflow",
        user_id="user-1",
        org_id="org-1",
        namespace="#V#michael@org",
    )
    workflow_data = {
        "last_action_outputs": {
            "tool": "evidence_loader",
            "messages": [{"role": "tool", "content": "x" * 2000}],
        },
    }

    assert manager.checkpoint(
        instance_id,
        current_state="collect_evidence",
        workflow_data=workflow_data,
    )

    instance = manager.get_instance(instance_id)
    assert instance is not None
    assert instance.workflow_data == workflow_data


def test_trace_store_compacts_and_hydrates_execution_trace(monkeypatch) -> None:
    store = _FakeBlobStore()
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )
    monkeypatch.setenv("VON_WORKFLOW_PAYLOAD_BLOB_THRESHOLD_BYTES", "512")

    class _Collection:
        def __init__(self) -> None:
            self.inserted: dict[str, Any] | None = None

        def create_index(self, *args, **kwargs) -> None:
            return None

        def insert_one(self, doc: dict[str, Any]) -> None:
            self.inserted = dict(doc)

        def find_one(self, query, projection=None):
            if self.inserted and self.inserted.get("execution_id") == query.get(
                "execution_id"
            ):
                return dict(self.inserted)
            return None

    class _Db:
        def __init__(self) -> None:
            self.collection = _Collection()

        def __getitem__(self, name: str) -> _Collection:
            assert name == trace_store.WORKFLOW_EXECUTIONS_COLLECTION_NAME
            return self.collection

    fake_db = _Db()
    monkeypatch.setattr(trace_store, "get_db", lambda: fake_db)
    monkeypatch.setattr(trace_store, "_indexes_ensured", False)
    actions = [{"state_id": "llm", "output": "x" * 2000}]

    stored_id = trace_store.insert_workflow_execution_trace(
        {
            "execution_id": "execution-1",
            "workflow_id": "#V#episode_evaluation_workflow",
            "user_namespace": "#V#michael@org",
            "start_time": "2026-05-29T00:00:00+00:00",
            "actions": actions,
        }
    )

    assert stored_id == "execution-1"
    assert fake_db.collection.inserted is not None
    assert fake_db.collection.inserted["actions"]["schema_version"] == (
        "workflow_payload_blob_ref.v1"
    )
    loaded = trace_store.get_workflow_execution_trace("execution-1")
    assert loaded is not None
    assert loaded["actions"] == actions
