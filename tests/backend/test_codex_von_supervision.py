"""Canonical-service/controller acceptance; isolated DB and tiny fixture process.

No live coding worker or provider is launched. The same canonical task/message
services and DGX loop used in production perform the effects under test.
"""

import copy
import json
from datetime import UTC, datetime

import pytest
from test_codex_von_retry import canonical_controller as controller_fixture
from test_codex_von_retry import config as config_fixture
from test_codex_von_retry import repair_receipt

from scripts import codex_von_supervision as supervision
from scripts import codex_von_worker as worker
from src.backend.db.repositories.concepts_repository import ConceptsRepository

canonical_controller = controller_fixture
config = config_fixture


def configure(config, monkeypatch):
    config["supervision_enabled"] = True
    config["display_name"] = "Fixture source"
    for cid, targets in (
        (config["agent_id"], ["#V#supervisor"]),
        ("#V#supervisor", [config["delegator_id"]]),
        (config["delegator_id"], []),
    ):
        ConceptsRepository.insert_one(
            {"concept_id": cid, "relationships": {"#V#has_supervisor": targets}}
        )
    # Membership policy itself has dedicated service tests. Here all three
    # principals are isolated authorised members; test denial separately.
    from src.backend.services import message_service

    monkeypatch.setattr(
        message_service,
        "authorise_direct_message_participants",
        lambda **kw: (True, None),
    )


def test_lost_create_ack_two_hops_and_verified_continuation(
    canonical_controller, monkeypatch
):
    config, api, create, read_state, launches, fd = canonical_controller
    configure(config, monkeypatch)
    source = create()
    worker.tick(config, api, fd)
    original_create = api.tasks.create_task
    failed = False

    def lost_ack(*args, **kwargs):
        nonlocal failed
        created = original_create(*args, **kwargs)
        if kwargs.get("agent_creation_fingerprint") and not failed:
            failed = True
            raise OSError("Lost create acknowledgement after canonical effect")
        return created

    monkeypatch.setattr(api.tasks, "create_task", lost_ack)
    worker.tick(config, api, fd)
    assert failed
    for _ in range(3):
        worker.tick(config, api, fd)
    _path, state = read_state(source)
    episode = state["supervision"]
    repair = api.task(episode["task_id"])
    assert repair["assignee_concept_id"] == "#V#supervisor"
    assert repair["created_by_concept_id"] == config["delegator_id"]
    assert len(launches()) == 1
    assert episode["linked"] and episode["message_id"]
    assert "Repair task:" in api.task(source)["next_checkpoint"]
    assert api.messages.get_message_for_user(
        episode["message_id"], config["delegator_id"]
    )
    tasks = api.tasks.list_tasks(
        organisation_concept_id=config["organisation_id"], limit=None
    )
    assert (
        len(
            [
                t
                for t in tasks
                if (t.get("external_references") or {}).get("coding_supervision")
            ]
        )
        == 1
    )
    # The linked repair's completed status alone does not admit the source.
    api.tasks.update_task_status(repair["task_concept_id"], "completed")
    worker.tick(config, api, fd)
    assert len(launches()) == 1
    api.tasks.update_task_status(repair["task_concept_id"], "in_progress")
    supconfig = dict(config, agent_id="#V#supervisor")
    supapi = worker.Von(supconfig)
    receipt = repair_receipt(state)
    receipt["observed_at"] = datetime.now(UTC).isoformat()
    outcome = {"status": "completed", "repair_receipt_json": json.dumps(receipt)}
    row = supapi.record_supervisor_repair(
        supapi.task(repair["task_concept_id"]), outcome
    )
    again = supapi.record_supervisor_repair(
        supapi.task(repair["task_concept_id"]), outcome
    )
    assert row["comment_id"] == again["comment_id"]
    worker.tick(config, api, fd)
    assert len(launches()) == 2
    assert read_state(source)[1]["launch_admission"]["kind"] == "verified_repair"
    # A later blocked supervisor goes to the human using the same controller,
    # not a process launched in the human's identity.
    supstate = copy.deepcopy(state)
    supstate.update(task_id=repair["task_concept_id"], attempt="supervisor-failed")
    supstate.pop("blocker", None)
    supstate.pop("supervision", None)
    result = supervision.reconcile(
        supconfig,
        supapi,
        supapi.task(repair["task_concept_id"]),
        supstate,
        lambda: None,
    )
    human_task = api.task(result["task_id"])
    assert human_task["assignee_concept_id"] == config["delegator_id"]
    assert not worker.authorised_task(human_task, config)
    assert not worker.authorised_task(human_task, supconfig)


def test_explicit_reporting_and_cycles_keep_other_work_moving(
    canonical_controller, monkeypatch
):
    config, api, create, read_state, launches, fd = canonical_controller
    configure(config, monkeypatch)
    source = create()
    # An explicit reporting choice is preserved and no longer confused with
    # execution delegation. Existing creator/org/assignee checks still apply.
    api.tasks.update_task_fields(
        source, fields={"report_to_concept_id": config["delegator_id"]}
    )
    assert worker.authorised_task(api.task(source), config)
    assert (
        supervision.resolution(api.task(source))["concept_id"] == config["delegator_id"]
    )
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    assert (
        api.task(read_state(source)[1]["supervision"]["task_id"])["assignee_concept_id"]
        == config["delegator_id"]
    )
    other = create("Another task")
    api.tasks.update_task_fields(
        other, fields={"report_to_concept_id": config["agent_id"]}
    )
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    assert (
        read_state(other)[1]["supervision_error"]["route"]["status"]
        == "supervision_cycle"
    )
    third = create("Eligible work despite cycle")
    worker.tick(config, api, fd)
    assert [x["task_id"] for x in launches()] == [source, other, third]


def test_changed_source_and_nonmember_supervisor_denied(
    canonical_controller, monkeypatch
):
    config, api, create, read_state, launches, fd = canonical_controller
    configure(config, monkeypatch)
    source = create()
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    _, state = read_state(source)
    repair = api.task(state["supervision"]["task_id"])
    supapi = worker.Von(dict(config, agent_id="#V#supervisor"))
    receipt = repair_receipt(state)
    receipt["observed_at"] = datetime.now(UTC).isoformat()
    api.tasks.update_task_fields(
        source, fields={"description": "Changed user instruction"}
    )
    with pytest.raises(PermissionError):
        supapi.record_supervisor_repair(
            repair, {"status": "completed", "repair_receipt_json": json.dumps(receipt)}
        )
    monkeypatch.setattr(
        api.messages,
        "authorise_direct_message_participants",
        lambda **kw: (False, "membership revoked"),
    )
    with pytest.raises(PermissionError):
        supervision.reconcile(config, api, api.task(source), state, lambda: None)
    assert len(launches()) == 1


def test_same_unresolved_blocker_reuses_repair_after_manual_retry(
    canonical_controller, monkeypatch
):
    cfg, api, create, read_state, launches, fd = canonical_controller
    configure(cfg, monkeypatch)
    source = create()
    worker.tick(cfg, api, fd)
    worker.tick(cfg, api, fd)
    _, first = read_state(source)
    repair_id = first["supervision"]["task_id"]
    api.tasks.add_task_comment(
        source,
        body="/retry-coding "
        + first["attempt"]
        + " reviewed retained effects; diagnose further",
        author_concept_id=cfg["delegator_id"],
    )
    worker.tick(cfg, api, fd)
    worker.tick(cfg, api, fd)
    _, second = read_state(source)
    assert len(launches()) == 2
    assert second["attempt"] != first["attempt"]
    assert second["supervision"]["task_id"] == repair_id
    parent = api.task(repair_id)["external_references"]["coding_supervision"]
    assert parent["blocker"]["attempt"] == second["attempt"]
    assert first["attempt"] in parent["prior_attempts"]
