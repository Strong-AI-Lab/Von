import mongomock
import pytest

from scripts import codex_von_worker as worker
from src.backend.services import task_dispatch_authority_service as authority
from src.backend.services import task_project_home_service as homes
from src.backend.services import task_project_service as projects


@pytest.mark.parametrize("organisation", [None, "#V#org"])
def test_transferred_assignment_needs_current_actor_receipt_without_rewriting_creator(
    monkeypatch, organisation
):
    collection = mongomock.MongoClient().test.receipts
    monkeypatch.setattr(authority, "_collection", lambda: collection)
    config = {
        "agent_id": "#V#agent",
        "delegator_id": "#V#owner",
        "organisation_id": "#V#org",
    }
    task = {
        "task_concept_id": "#V#task",
        "assignee_concept_id": "#V#agent",
        "created_by_concept_id": "#V#historical_author",
        "organisation_concept_id": "#V#org",
        "project_concept_id": "#V#project",
        "federation": {"origin": "source"},
    }
    task["organisation_concept_id"] = organisation
    assert not worker.authorised_task(task, config)
    task["dispatch_requested"] = True
    assert not worker.authorised_task(task, config)
    monkeypatch.setattr(authority, "get_effective_user_concept_id_with_source", lambda: ("#V#other", authority.TRUSTED_IN_PROCESS_ACTOR_SOURCE))
    authority.record_assignment(task)
    assert not worker.authorised_task(task, config)
    monkeypatch.setattr(authority, "get_effective_user_concept_id_with_source", lambda: ("#V#owner", authority.TRUSTED_IN_PROCESS_ACTOR_SOURCE))
    authority.record_assignment(task)
    assert worker.authorised_task(task, config)
    assert task["created_by_concept_id"] == "#V#historical_author"
    authority.revoke_assignment(task["task_concept_id"])
    assert not worker.authorised_task(task, config)
    # Even a copied task authored by the current owner needs a new assignment.
    task["created_by_concept_id"] = "#V#owner"
    task["dispatch_requested"] = False
    assert not worker.authorised_task(task, config)
    task.pop("federation")
    task.pop("project_concept_id")
    assert not worker.authorised_task(task, config)


@pytest.mark.parametrize(
    "state,node,allowed",
    [
        ("prepared", "dgx", False),
        ("replica", "dgx", False),
        ("active", "atlas", False),
        ("active", "dgx", True),
    ],
)
def test_only_active_local_home_can_dispatch(monkeypatch, state, node, allowed):
    monkeypatch.setenv("VON_TASK_HOME_NODE_ID", "dgx")
    monkeypatch.setattr(
        projects,
        "task_execution_home",
        lambda pid: {
            "writer": "von",
            "write_home": {"writable_here": state == "active" and node == "dgx"},
        },
    )
    api = object.__new__(worker.Von)
    assert (
        api.native_writer(
            {"task_concept_id": "#V#task", "project_concept_id": "#V#project"}
        )
        is allowed
    )


def test_legacy_identity_header_cannot_grant_dispatch(monkeypatch):
    collection = mongomock.MongoClient().test.receipts
    monkeypatch.setattr(authority, "_collection", lambda: collection)
    monkeypatch.setattr(authority, "get_effective_user_concept_id_with_source", lambda: ("#V#owner", "legacy_identity_header"))
    assert authority.record_assignment({"task_concept_id": "#V#task"}) is None
    assert collection.find_one({"_id": "#V#task"})["revoked"] is True
