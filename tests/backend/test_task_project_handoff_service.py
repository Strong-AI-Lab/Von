import mongomock
import pytest
from src.backend.services import task_project_handoff_service as handoff
from src.backend.services import task_project_home_service as homes
from src.backend.services.knowledge_federation.protocol import sign

PROJECT = "#V#project"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    keyfile = tmp_path / "key"
    keyfile.write_text("01" * 32)
    keyfile.chmod(0o600)
    settings = {"enabled": True, "key_file": str(keyfile), "task_projects": [PROJECT]}
    source = {"node_id": "source", "exports": {"destination": settings}}
    destination = {"node_id": "destination", "imports": {"source": settings}}
    db = mongomock.MongoClient().test
    monkeypatch.setattr(handoff, "get_db", lambda: db)
    monkeypatch.setattr(homes, "_collection", lambda: db.task_project_homes)
    return db, source, destination


def test_source_must_release_before_activation_and_replay_cannot_roll_back(
    setup, monkeypatch
):
    db, source, destination = setup
    db.task_project_homes.insert_one(
        {
            "_id": PROJECT,
            "state": "prepared",
            "home_node_id": "destination",
            "epoch": 2,
            "snapshot_digest": "a" * 64,
            "home_url": "https://destination.invalid",
            "operations": {},
        }
    )
    receipt = {
        "_id": "receipt",
        "project_id": PROJECT,
        "home_epoch": 2,
        "source_epoch": 1,
        "snapshot_digest": "a" * 64,
        "origin": "source",
        "recipient": "destination",
    }
    db.task_project_transfer_receipts.insert_one(receipt)
    monkeypatch.setenv("VON_TASK_HOME_NODE_ID", "destination")
    prepared = handoff.publication_receipt(
        "receipt", config=destination, origin="source"
    )
    with pytest.raises(PermissionError):
        handoff.activate_destination(prepared, config=destination, origin="source")
    release = {
        "schema_version": handoff.VERSION,
        "kind": "released",
        "sender": "source",
        "recipient": "destination",
        "project_id": PROJECT,
        "home_epoch": 2,
        "snapshot_digest": "a" * 64,
        "publication_id": "receipt",
        "source_read_only": True,
        "canonical_readback": True,
    }
    signed = sign(release, bytes.fromhex("01" * 32))
    assert (
        handoff.activate_destination(signed, config=destination, origin="source")[
            "state"
        ]
        == "active"
    )
    db.task_project_homes.update_one(
        {"_id": PROJECT}, {"$set": {"epoch": 3, "state": "frozen"}}
    )
    with pytest.raises(ValueError, match="changed"):
        handoff.activate_destination(signed, config=destination, origin="source")
    assert db.task_project_homes.find_one({})["epoch"] == 3


def test_release_reconciles_frozen_source_against_destination_digest(
    setup, monkeypatch
):
    db, source, destination = setup
    monkeypatch.setenv("VON_TASK_HOME_NODE_ID", "source")
    db.task_project_homes.insert_one(
        {
            "_id": PROJECT,
            "home_node_id": "source",
            "state": "frozen",
            "epoch": 1,
            "operations": {},
        }
    )
    prepared = {
        "schema_version": handoff.VERSION,
        "kind": "prepared",
        "sender": "destination",
        "recipient": "source",
        "project_id": PROJECT,
        "source_epoch": 1,
        "home_epoch": 2,
        "snapshot_digest": "a" * 64,
        "home_url": "https://destination.invalid",
        "publication_id": "receipt",
        "canonical_readback": True,
    }
    envelope = sign(prepared, bytes.fromhex("01" * 32))
    monkeypatch.setattr(
        handoff, "capture_project", lambda *a, **k: {"payload": {"digest": "b" * 64}}
    )
    with pytest.raises(ValueError, match="changed"):
        handoff.release_source(envelope, config=source, destination="destination")
    assert db.task_project_homes.find_one({})["state"] == "frozen"
    monkeypatch.setattr(
        handoff, "capture_project", lambda *a, **k: {"payload": {"digest": "a" * 64}}
    )
    release = handoff.release_source(envelope, config=source, destination="destination")
    assert release["payload"]["source_read_only"]
    assert (
        handoff.release_source(envelope, config=source, destination="destination")
        == release
    )
    assert db.task_project_homes.find_one({})["state"] == "replica"
