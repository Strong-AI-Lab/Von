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


def test_generic_concept_and_text_writes_obey_the_same_home(store, monkeypatch):
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.db.repositories.text_value_repository import (
        TextRelationsRepository,
    )

    database = store.database
    monkeypatch.setattr(ConceptsRepository, "collection", lambda: database.concepts)
    monkeypatch.setattr(
        TextRelationsRepository, "collection", lambda: database.text_relations
    )
    database.concepts.insert_one(
        {"concept_id": "#V#task", "metadata": {"project_concept_id": "#V#project"}}
    )
    relation_id = database.text_relations.insert_one(
        {
            "subject_concept_id": "#V#task",
            "predicate": "hasName",
            "object_text_id": "before",
        }
    ).inserted_id
    homes.freeze_project_home(
        project_id="#V#project", expected_epoch=1, evidence={"checked": True}
    )
    with pytest.raises(homes.TaskProjectHomeError):
        ConceptsRepository.update_one(
            {"concept_id": "#V#task"},
            {"$set": {"metadata.project_concept_id": "#V#elsewhere"}},
        )
    with pytest.raises(homes.TaskProjectHomeError):
        TextRelationsRepository.delete_one({"_id": relation_id})
    with pytest.raises(homes.TaskProjectHomeError):
        TextRelationsRepository.update_one(
            {"subject_concept_id": "#V#task", "predicate": "hasDescription"},
            {"$set": {"object_text_id": "new"}},
            upsert=True,
        )
    assert database.text_relations.find_one({"_id": relation_id})
    assert (
        database.concepts.find_one({"concept_id": "#V#task"})["metadata"][
            "project_concept_id"
        ]
        == "#V#project"
    )
    # Derived indexing remains available on a read-only replica.
    ConceptsRepository.update_one(
        {"concept_id": "#V#task"}, {"$set": {"embedding_status": "indexed"}}
    )
    assert (
        database.concepts.find_one({"concept_id": "#V#task"})["embedding_status"]
        == "indexed"
    )


def test_workflow_actions_recheck_the_home_after_claim(store, monkeypatch):
    store.database.concepts.insert_one(
        {"concept_id": "#V#task", "metadata": {"project_concept_id": "#V#project"}}
    )
    workflow = {"inputs": {"task_concept_id": "#V#task"}}
    assert homes.workflow_home_allows_execution(workflow, database=store.database)
    homes.freeze_project_home(
        project_id="#V#project", expected_epoch=1, evidence={"checked": True}
    )
    assert not homes.workflow_home_allows_execution(workflow, database=store.database)
    assert homes.workflow_home_allows_execution({"inputs": {}}, database=store.database)


def test_a_concurrent_project_move_defeats_the_repository_write_preimage(store):
    concepts = store.database.concepts
    concepts.insert_one(
        {"concept_id": "#V#task", "metadata": {"project_concept_id": "#V#project"}}
    )
    with homes.concept_mutation(concepts, {"concept_id": "#V#task"}) as query:
        concepts.update_one(
            {"concept_id": "#V#task"},
            {"$set": {"metadata.project_concept_id": "#V#other"}},
        )
        assert (
            concepts.update_one(
                query, {"$set": {"metadata.notes": "stale writer"}}
            ).matched_count
            == 0
        )
