"""Competing-instance and stale-dispatch regressions, without external effects."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import mongomock
import pytest

from src.backend.workflows.durable import instance_manager as im
from src.backend.workflows.durable.task_ownership import (
    bind_task_claim,
    claim_admission_validator,
    ensure_task_ownership_index,
    ownership_checked_handler,
    task_key,
)


@pytest.fixture
def manager(monkeypatch):
    db = mongomock.MongoClient(tz_aware=True).db
    monkeypatch.setattr(im, "get_db", lambda: db)
    monkeypatch.setattr(im, "_ensure_indexes", lambda: None)
    monkeypatch.setattr(im, "get_configured_min_worker_build", lambda: None)
    manager = im.WorkflowInstanceManager()
    # Model a provisioned collection; mongomock's create_index does not apply
    # partial-filter expressions when scanning pre-existing rows.
    ensure_task_ownership_index(db.workflow_instances)
    monkeypatch.setattr(manager, "_record_durable_episode_start", lambda **kw: None)
    monkeypatch.setattr(manager, "_broadcast_instance", lambda *a, **kw: None)
    return manager


def make_instance(manager, identifier, *, task="task-one", user="user-one"):
    doc = {
        "instance_id": identifier,
        "workflow_id": "test-workflow",
        "user_id": user,
        "org_id": "org",
        "namespace": user + "/org",
        "status": "pending",
        "created_at": datetime.now(UTC),
        "inputs": {"task_concept_id": task},
    }
    manager._get_instances_collection().insert_one(doc)
    return doc


def expire(manager, instance):
    manager._get_instances_collection().update_one(
        {"instance_id": instance.instance_id},
        {
            "$set": {
                "lock_expires_at": datetime.now(UTC) - timedelta(seconds=1)
            }
        },
    )


def test_atomic_task_claim_race(manager):
    docs = [make_instance(manager, str(i)) for i in range(8)]
    barrier = Barrier(8)

    def contend(doc):
        barrier.wait()
        return (
            manager.find_and_claim_instance("worker-" + doc["instance_id"]) is not None
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(contend, docs)) == 1


def test_duplicate_waits_takeover_keeps_checkpoint_and_unrelated_work_progresses(
    manager,
):
    make_instance(manager, "original")
    first = manager.find_and_claim_instance("worker-a")
    assert first
    coll = manager._get_instances_collection()
    coll.update_one(
        {"instance_id": "original"},
        {"$set": {"current_state": "saved", "step_index": 4}},
    )
    make_instance(manager, "duplicate")
    make_instance(manager, "independent", task="another-task")
    independent = manager.find_and_claim_instance("worker-b")
    assert independent.instance_id == "independent"
    expire(manager, first)
    successor = manager.find_and_claim_instance("worker-c")
    assert successor.instance_id == "original"
    assert successor.current_state == "saved" and successor.step_index == 4
    assert successor.claim_token != first.claim_token
    assert not manager.is_current_claim("original", "worker-a", first.claim_token)
    assert not manager.extend_lock(
        "original", "worker-a", claim_token=first.claim_token
    )
    assert manager.is_current_claim("original", "worker-c", successor.claim_token)


def test_scope_isolation_and_persisted_identity():
    doc = {
        "instance_id": "one",
        "user_id": "a",
        "org_id": "org",
        "namespace": "a/org",
        "inputs": {"task_concept_id": "task"},
    }
    assert task_key(doc) != task_key({**doc, "user_id": "b"})
    assert task_key(doc) != task_key({**doc, "org_id": "other"})
    assert task_key(doc) == task_key(
        {"task_ownership_key": task_key(doc), "inputs": {"blob": "ref"}}
    )


def test_handler_checks_after_transport_queue_and_never_calls_stale_owner(manager):
    make_instance(manager, "one")
    first = manager.find_and_claim_instance("worker-a")
    calls = []
    wrapped = ownership_checked_handler(
        lambda **p: calls.append(p), method_name="send", category="write"
    )
    with bind_task_claim(manager, "one", "worker-a", first.claim_token):
        expire(manager, first)  # Time spent queued in the transport.
        with pytest.raises(RuntimeError, match="durable_task_claim_lost"):
            wrapped(to="fixture")
    assert calls == []


def test_unknown_effect_prevents_takeover_and_duplicate_execution(manager):
    make_instance(manager, "one")
    first = manager.find_and_claim_instance("worker-a")

    def unknown(**payload):
        raise TimeoutError("unknown remote outcome")

    wrapped = ownership_checked_handler(unknown, method_name="send", category="write")
    with bind_task_claim(manager, "one", "worker-a", first.claim_token), pytest.raises(TimeoutError):
        wrapped(to="fixture")
    expire(manager, first)
    make_instance(manager, "duplicate")
    assert manager.find_and_claim_instance("worker-b") is None
    assert manager.find_and_claim_instance("worker-c") is None
    effect = manager._get_instances_collection().find_one({"instance_id": "one"})[
        "uncheckpointed_effects"
    ][0]
    assert effect["state"] == "started" and "to" not in effect


def test_observed_effect_is_acknowledged_only_by_checkpoint(manager):
    make_instance(manager, "one")
    first = manager.find_and_claim_instance("worker-a")
    wrapped = ownership_checked_handler(
        lambda **p: {"success": True}, method_name="write", category="write"
    )
    with bind_task_claim(manager, "one", "worker-a", first.claim_token):
        assert wrapped(value=1) == {"success": True}
    coll = manager._get_instances_collection()
    assert (
        coll.find_one({"instance_id": "one"})["uncheckpointed_effects"][0]["state"]
        == "returned"
    )
    assert manager.checkpoint(
        "one",
        current_state="next",
        workflow_data={},
        step_index=1,
        worker_id="worker-a",
        claim_token=first.claim_token,
    )
    assert coll.find_one({"instance_id": "one"})["uncheckpointed_effects"] == []
    expire(manager, first)
    assert manager.find_and_claim_instance("worker-b").instance_id == "one"


def test_legacy_claim_validator_rejects_observed_sc_build():
    # Query semantics test. Real Mongo validation and legacy findAndModify are
    # also exercised in the operator activation canary; mongomock has no validators.
    coll = mongomock.MongoClient().db.rows
    coll.insert_many(
        [
            {
                "_id": "old",
                "status": "running",
                "locked_by": "worker_SC448086_568144",
                "claimed_by_build": {"git_commit": "d5ce3b6"},
            },
            {
                "_id": "new",
                "status": "running",
                "locked_by": "worker_SC448086_new",
                "claimed_by_build": {"capabilities": ["durable_task_ownership_v1"]},
            },
            {"_id": "dgx", "status": "running", "locked_by": "worker_DGX_1"},
            {"_id": "queued", "status": "pending"},
        ]
    )
    assert {
        d["_id"] for d in coll.find(claim_admission_validator("^worker_SC448086_"))
    } == {"new", "dgx", "queued"}


def test_pause_retains_owner_and_terminal_release_allows_next_occurrence(manager):
    make_instance(manager, "one")
    first = manager.find_and_claim_instance("worker-a")
    make_instance(manager, "two")
    assert manager.release_lock("one", "worker-a", claim_token=first.claim_token)
    resumed = manager.find_and_claim_instance("worker-b")
    assert resumed.instance_id == "one"
    coll = manager._get_instances_collection()
    coll.update_one({"instance_id": "one"}, {"$set": {"status": "failed"}})
    assert manager.find_and_claim_instance("worker-c").instance_id == "two"


def test_reconciliation_requires_drain_exact_inventory_and_evidence(manager):
    make_instance(manager, "one")
    first = manager.find_and_claim_instance("worker-a")
    effect = manager.begin_claim_effect(
        "one", "worker-a", first.claim_token, "send", {}
    )
    kwargs = {
        "expected_effect_ids": [effect],
        "evidence": "provider receipt verified; old process stopped",
        "expected_claim_token": first.claim_token,
        "current_state": "next",
        "workflow_data": {},
        "step_index": 1,
    }
    assert not manager.reconcile_claim_effects("one", **kwargs)  # Active lease.
    expire(manager, first)
    assert not manager.reconcile_claim_effects(
        "one", **{**kwargs, "expected_effect_ids": ["wrong"]}
    )
    assert manager.reconcile_claim_effects("one", **kwargs)
    doc = manager._get_instances_collection().find_one({"instance_id": "one"})
    assert doc["manual_resume_required"] and doc["current_state"] == "next"
    assert not doc["uncheckpointed_effects"]
    assert manager.find_and_claim_instance("worker-b") is None


def test_actual_gateway_transport_preserves_claim_and_records_effect(manager):
    from src.backend.integrations.internal_mcp.gateway import (
        InternalMCPGateway,
        MethodCatalogue,
        MethodDefinition,
    )
    from src.backend.integrations.internal_mcp.schemas import Schema
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    make_instance(manager, "one")
    first = manager.find_and_claim_instance("worker-a")
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="fixture_write",
            handler=lambda: {"success": True},
            input_schema=Schema(required={}, optional={}, allow_unknown=False),
            category="write",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue, transport=InternalMCPTransport(), enabled=True
    )
    with bind_task_claim(manager, "one", "worker-a", first.claim_token):
        assert gateway.invoke("fixture_write", {}).payload["success"]
    doc = manager._get_instances_collection().find_one({"instance_id": "one"})
    assert doc["uncheckpointed_effects"][0]["state"] == "returned"
