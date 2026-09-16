import mongomock
import pytest

from src.backend.services import task_project_home_service as homes


@pytest.fixture
def store(monkeypatch):
    collection = mongomock.MongoClient().test[homes.COLLECTION]
    monkeypatch.setattr(homes, "_collection", lambda: collection)
    monkeypatch.setenv("VON_TASK_HOME_NODE_ID", "atlas-mac")
    homes.register_project_home(
        project_id="#V#project",
        node_id="atlas-mac",
        home_url="https://source.test",
        source_identity={"site": "jira.test", "id": "1"},
        evidence={"inventory_verified": True},
    )
    return collection


def test_freeze_drains_admitted_writes_and_blocks_later_writes(store):
    with homes.project_write("#V#project"):
        with homes.project_write("#V#project"):
            assert len(store.find_one({})["operations"]) == 1
        with pytest.raises(homes.TaskProjectHomeError, match="not_quiescent"):
            homes.freeze_project_home(
                project_id="#V#project", expected_epoch=1, evidence={"checked": True}
            )
    assert store.find_one({})["operations"] == {}
    frozen = homes.freeze_project_home(
        project_id="#V#project", expected_epoch=1, evidence={"checked": True}
    )
    assert frozen["state"] == "frozen"
    with pytest.raises(homes.TaskProjectHomeError, match="read_only_replica"):
        with homes.project_write("#V#project"):
            pytest.fail("The frozen source accepted a write")


def test_interrupted_write_requires_exact_effect_reconciliation(store):
    with pytest.raises(RuntimeError):
        with homes.project_write("#V#project"):
            raise RuntimeError("lost database acknowledgement")
    operations = store.find_one({})["operations"]
    operation_id, operation = next(iter(operations.items()))
    with pytest.raises(homes.TaskProjectHomeError):
        homes.freeze_project_home(
            project_id="#V#project", expected_epoch=1, evidence={"checked": True}
        )
    with pytest.raises(ValueError):
        homes.reconcile_project_operation(
            project_id="#V#project",
            operation_id=operation_id,
            expected_operation=operation,
            evidence={"process_drained": True},
        )
    assert homes.reconcile_project_operation(
        project_id="#V#project",
        operation_id=operation_id,
        expected_operation=operation,
        evidence={
            "process_drained": True,
            "effects_reconciled": True,
            "readback": "receipt",
        },
    )
    homes.freeze_project_home(
        project_id="#V#project", expected_epoch=1, evidence={"checked": True}
    )


def test_transfer_is_fenced_idempotent_and_rejects_stale_rollback(store, monkeypatch):
    homes.freeze_project_home(
        project_id="#V#project", expected_epoch=1, evidence={"checked": True}
    )
    args = dict(
        project_id="#V#project",
        expected_epoch=1,
        node_id="dgx",
        home_url="https://destination.test",
        snapshot_digest="a" * 64,
        receipt={"canonical_readback": True, "snapshot_digest": "a" * 64},
    )
    moved = homes.transfer_frozen_home(**args)
    assert homes.transfer_frozen_home(**args) == moved
    assert moved["epoch"] == 2
    with pytest.raises(homes.TaskProjectHomeError):
        with homes.project_write("#V#project"):
            pytest.fail("Old home accepted an independent write")
    with pytest.raises(homes.TaskProjectHomeError):
        homes.transfer_frozen_home(
            **{**args, "node_id": "atlas-mac", "snapshot_digest": "b" * 64}
        )
    # Even identifying as the new node cannot activate an unverified replica.
    monkeypatch.setenv("VON_TASK_HOME_NODE_ID", "dgx")
    with pytest.raises(homes.TaskProjectHomeError):
        with homes.project_write("#V#project"):
            pytest.fail("A replica became authoritative by environment selection")


def test_source_identity_collision_is_not_an_overwrite(store):
    with pytest.raises(homes.TaskProjectHomeError, match="registration_conflict"):
        homes.register_project_home(
            project_id="#V#project",
            node_id="atlas-mac",
            home_url="https://source.test",
            source_identity={"site": "another.test", "id": "1"},
            evidence={"checked": True},
        )
    assert store.find_one({})["source_identity"]["site"] == "jira.test"
