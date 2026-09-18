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


def test_result_reports_preserve_owner_and_recover_supervisor_readback(
    canonical_controller, monkeypatch
):
    config, api, create, read_state, launches, fd = canonical_controller
    configure(config, monkeypatch)
    source = create()
    task = api.task(source)
    original_read = api.messages.get_message_for_user
    failed = False

    def interrupted(mid, actor):
        nonlocal failed
        row = original_read(mid, actor)
        projected = api.messages.project_direct_message(row) if row else {}
        if projected.get("recipient_ids") == ["#V#supervisor"] and not failed:
            failed = True
            raise OSError("Lost supervisor read-back")
        return row

    monkeypatch.setattr(api.messages, "get_message_for_user", interrupted)
    with pytest.raises(OSError):
        api.send(task, "completion-report-test", "Completed; retained result verified.")
    owner_mid = api.send(
        task, "completion-report-test", "Completed; retained result verified."
    )
    assert original_read(owner_mid, config["delegator_id"])
    reports = list(
        (__import__("pathlib").Path(config["state_root"]) / "supervisor-reports").glob(
            "*.json"
        )
    )
    assert len(reports) == 1
    intent = json.loads(reports[0].read_text())
    rb = api.messages.project_direct_message(
        original_read(intent["message_id"], "#V#supervisor")
    )
    assert rb["content"] == "Completed; retained result verified."
    assert rb["recipient_ids"] == ["#V#supervisor"]
    assert rb["thread_id"] == source
    assert len(api.messages.get_messages_for_user(config["agent_id"])) == 2


def test_explicit_completed_report_recovery_never_replays_task(
    canonical_controller, monkeypatch
):
    from scripts import codex_von_inbox as inbox

    config, api, create, read_state, launches, fd = canonical_controller
    configure(config, monkeypatch)
    source = create()
    task = api.task(source)
    # Retain the canonical result of an older controller, before supervisor delivery.
    config["supervision_enabled"] = False
    mid = api.send(
        task, "old-attempt:result", "Completed with verified retained artifact."
    )
    api.tasks.update_task_status(
        source, "completed", actor_concept_id=config["agent_id"]
    )
    path = (
        __import__("pathlib").Path(config["state_root"])
        / "tasks"
        / (worker.fingerprint(source)[:16] + ".json")
    )
    worker.write_json(
        path,
        {
            "task_id": source,
            "attempt": "old-attempt",
            "phase": "done",
            "message_id": mid,
            "report_content": "Completed with verified retained artifact.",
        },
    )
    config["supervision_enabled"] = True
    selection = source + "=old-attempt"
    recovered = supervision.reconcile_completed_report(config, api, selection)
    assert supervision.reconcile_completed_report(config, api, selection) == recovered
    assert recovered["coding_replayed"] is False
    assert not launches()
    assert api.task(source)["status"] == "completed"
    row = api.messages.project_direct_message(
        api.messages.get_message_for_user(recovered["message_id"], "#V#supervisor")
    )
    supervisor_config = dict(config, agent_id="#V#supervisor")
    assert supervision.is_task_report(supervisor_config, api, row)
    assert (
        inbox.context_for(supervisor_config, api, row)["source_kind"]
        == "supervisor_task_report"
    )
    with pytest.raises(PermissionError):
        supervision.reconcile_completed_report(
            config, api, source + "=different-attempt"
        )
    assert len(api.messages.get_messages_for_user(config["agent_id"])) == 2
