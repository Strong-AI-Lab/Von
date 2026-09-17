"""Canonical fixture reads and both existing coding-controller boundaries.

The optional Mac replay loads only function definitions from the operator's
source, patches a temporary copy, and substitutes all host effects. It does not
import bootstrap/configuration, access credentials, or touch the installed bridge.
"""

import ast
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_codex_von_retry import canonical_controller, config  # noqa: F401

from scripts import codex_von_retry as retry
from scripts import codex_von_worker as worker
from src.backend.services import task_execution_timing_service as timing

# Imported pytest fixtures are intentionally named in test signatures.
# ruff: noqa: F811

START = "2026-09-17T10:00:00+00:00"
END = "2026-09-17T10:05:00+00:00"


def start(api, task_id, attempt="first", started_at=START):
    return timing.start(
        task_id,
        attempt_id=attempt,
        actor_concept_id=api.config["agent_id"],
        started_at=started_at,
        source="fixture",
    )


def finish(api, task_id, attempt="first", outcome="completed", ended_at=END):
    return timing.finish(
        task_id,
        attempt_id=attempt,
        actor_concept_id=api.config["agent_id"],
        outcome=outcome,
        ended_at=ended_at,
    )


def test_canonical_start_finish_retry_reopen_and_detail(
    canonical_controller, monkeypatch
):
    _, api, create, *_ = canonical_controller
    task_id = create()
    assert api.task(task_id)["execution_timing"]["started_at"] is None
    start(api, task_id)
    start(api, task_id, started_at="2026-09-17T11:00:00Z")
    assert api.task(task_id)["execution_timing"]["started_at"] == START
    blocked = finish(api, task_id, outcome="blocked")["execution_timing"]
    assert blocked["completed_at"] is None
    assert blocked["attempts"][0]["elapsed_wall_seconds"] == 300
    assert blocked["completion_history"] == []
    assert finish(api, task_id, outcome="blocked")["execution_timing"] == blocked
    start(api, task_id, "second", "2026-09-17T10:06:00Z")
    completed = finish(api, task_id, "second", ended_at="2026-09-17T10:08:00Z")[
        "execution_timing"
    ]
    assert completed["started_at"] == START
    assert completed["completed_at"]
    assert completed["attempts"][1]["elapsed_wall_seconds"] == 120
    assert completed["cost"]["amount"] is None
    assert completed["cost"]["status"] == "unknown"
    assert (
        finish(api, task_id, "second", ended_at="2026-09-17T10:08:00Z")[
            "execution_timing"
        ]
        == completed
    )
    # Existing HTTP detail route projects the same persisted data.
    from flask import Flask

    from src.backend.server.routes import task_routes

    monkeypatch.setattr(
        task_routes, "list_task_external_resource_actions", lambda _: []
    )
    # Visibility sanitisation retains only resolvable type relationships.
    for concept_id in [
        api.tasks.TASK_SPECIFICATION_TYPE_ID,
        *api.task(task_id)["task_type_ids"],
    ]:
        api.tasks.ConceptsRepository.update_one(
            {"concept_id": concept_id},
            {"$setOnInsert": {"concept_id": concept_id, "relationships": {}}},
            upsert=True,
        )
    app = Flask(__name__)
    app.secret_key = "isolated-timing-fixture"
    app.register_blueprint(task_routes.task_bp, url_prefix="/api/tasks")
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_concept_id"] = api.config["delegator_id"]
        session["organisation_concept_id"] = api.config["organisation_id"]
    from src.backend.security.access_control import override_current_actor

    with override_current_actor(
        api.config["delegator_id"], api.config["organisation_id"]
    ):
        response = client.get("/api/tasks/" + task_id.replace("#", "%23"))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["execution_timing"] == completed
    api.tasks.update_task_status(task_id, "pending")
    reopened = api.task(task_id)["execution_timing"]
    assert reopened["completed_at"] is None
    assert reopened["completion_history"] == completed["completion_history"]
    finish(api, task_id, "second", ended_at="2026-09-17T10:08:00Z")
    assert api.task(task_id)["status"] == "pending"
    start(api, task_id, "third", "2026-09-17T10:09:00Z")
    final = finish(api, task_id, "third", ended_at="2026-09-17T10:10:00Z")[
        "execution_timing"
    ]
    assert len(final["completion_history"]) == 2
    assert len(final["attempts"]) == 3
    assert final["started_at"] == START


def test_interrupted_finish_reconciles_and_conflicting_receipt_is_rejected(
    canonical_controller, monkeypatch
):
    _, api, create, *_ = canonical_controller
    task_id = create()
    start(api, task_id)
    original = api.tasks.update_task_status

    def lost_ack(*a, **kw):
        original(*a, **kw)
        raise OSError("Lost acknowledgement after status persistence")

    monkeypatch.setattr(api.tasks, "update_task_status", lost_ack)
    with pytest.raises(OSError):
        finish(api, task_id)
    assert api.task(task_id)["execution_timing"]["completed_at"] is None
    monkeypatch.setattr(api.tasks, "update_task_status", original)
    accepted = finish(api, task_id)["execution_timing"]
    assert accepted["completed_at"]
    assert finish(api, task_id)["execution_timing"] == accepted
    with pytest.raises(ValueError, match="different terminal"):
        finish(api, task_id, outcome="blocked")


def test_interrupted_finish_cannot_complete_reopened_task(
    canonical_controller, monkeypatch
):
    _, api, create, *_ = canonical_controller
    task_id = create()
    start(api, task_id)
    original = api.tasks.update_task_status

    def lost_ack(*a, **kw):
        original(*a, **kw)
        raise OSError("Lost acknowledgement")

    monkeypatch.setattr(api.tasks, "update_task_status", lost_ack)
    with pytest.raises(OSError):
        finish(api, task_id)
    monkeypatch.setattr(api.tasks, "update_task_status", original)
    original(task_id, "pending")
    with pytest.raises(ValueError, match="reopened"):
        finish(api, task_id)
    assert api.task(task_id)["status"] == "pending"


def test_missing_exit_and_invalid_observations_stay_honest(canonical_controller):
    _, api, create, *_ = canonical_controller
    task_id = create()
    with pytest.raises(ValueError, match="offset"):
        start(api, task_id, started_at="2026-09-17T10:00:00")
    assert api.task(task_id)["execution_timing"]["attempts"] == []
    start(api, task_id)
    with pytest.raises(ValueError, match="precedes"):
        finish(api, task_id, ended_at="2026-09-16T10:00:00Z")
    unknown = finish(api, task_id, outcome="failed", ended_at=None)["execution_timing"]
    assert unknown["completed_at"] is None
    assert unknown["attempts"][0]["ended_at"] is None
    assert unknown["attempts"][0]["elapsed_wall_seconds"] is None


def test_dgx_supported_launch_and_result_readback(canonical_controller, monkeypatch):
    cfg, api, create, read_state, launches, fd = canonical_controller
    runner = Path(cfg["codex_command"])
    runner.write_text(
        runner.read_text().replace("'status': 'blocked'", "'status': 'completed'")
    )
    task_id = create()
    repair_observations = []

    def observe_repair(task, result):
        assert api.task(task_id)["status"] == "in_progress"
        repair_observations.append(task["status"])

    monkeypatch.setattr(api, "record_supervisor_repair", observe_repair)
    worker.tick(cfg, api, fd)
    assert repair_observations == ["in_progress"]
    _, state = read_state(task_id)
    result = api.task(task_id)["execution_timing"]
    assert result["started_at"] == state["execution_started_at"]
    assert result["attempts"][0]["ended_at"] == state["finished_at"]
    assert result["attempts"][0]["source"] == "codex_dgx"
    assert result["completed_at"]
    assert result["attempts"][0]["elapsed_wall_seconds"] >= 0
    assert state["execution_timing"] == result
    worker.write_json(
        Path(cfg["state_root"]) / "timing-readback.json",
        {
            "environment": "isolated canonical-service fixture; no live task or provider",
            "task_id": task_id,
            "execution_timing": result,
        },
    )
    message = api.messages.get_message_for_user(
        state["message_id"], cfg["delegator_id"]
    )
    content = api.messages.project_direct_message(message)["content"]
    assert result["completed_at"] in content
    assert "includes waits" in content
    worker.tick(cfg, api, fd)
    assert len(launches()) == 1
    assert api.task(task_id)["execution_timing"] == result


@pytest.mark.parametrize("revise_before_acceptance", [False, True])
def test_mac_operator_patch_supported_path(
    canonical_controller, tmp_path, monkeypatch, revise_before_acceptance
):
    source = os.environ.get("CODING_TIMING_MAC_BRIDGE_SOURCE")
    if not source:
        pytest.skip(
            "Operator-owned bridge source is needed for the isolated Mac replay"
        )
    cfg, api, create, *_ = canonical_controller
    copy = tmp_path / "task-server.py"
    copy.write_text(Path(source).read_text())
    patch = (
        Path(__file__).resolve().parents[2]
        / "docs/engineering/patches/codex_vscode_task_timing.patch"
    )
    subprocess.run(
        ["patch", "--batch", "--fuzz=0", str(copy), str(patch)],
        check=True,
        capture_output=True,
    )
    parsed = ast.parse(copy.read_text())
    functions = [
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name not in {"main", "report"}
    ]
    messages = {}

    def report(key, subject, content, task_id):
        assert key not in messages or messages[key] == content
        messages[key] = content
        return {"canonical_readback": True}

    root = tmp_path / "bridge-state"
    root.mkdir()
    namespace = dict(  # noqa: C408 - the execution namespace mirrors module globals
        ROOT=root,
        CONFIG=cfg,
        LIVE={"reserved", "running", "reporting"},
        retry_policy=retry,
        json=json,
        uuid=uuid,
        os=os,
        datetime=datetime,
        timezone=timezone,
        report=report,
    )
    exec(  # noqa: S102 - only reviewed operator function definitions in an isolated fixture
        compile(ast.Module(body=functions, type_ignores=[]), str(copy), "exec"),
        namespace,
    )
    dispatch = namespace["dispatch"]
    task_id = create()
    selected = dispatch({"action": "select"}, api, worker)
    assert selected["task_id"] == task_id
    assert api.task(task_id)["execution_timing"]["started_at"] is None
    request = {"attempt": selected["attempt"], "thread_id": "fixture-thread"}
    begun = dispatch(dict(request, action="begin"), api, worker)
    assert begun["may_execute"]
    start_time = api.task(task_id)["execution_timing"]["started_at"]
    resumed = dispatch(
        dict(request, action="resume", checkpoint="Retained effects reconciled"),
        api,
        worker,
    )
    assert resumed["may_execute"]
    assert api.task(task_id)["execution_timing"]["started_at"] == start_time
    result = dict(  # noqa: C408
        status="completed",
        summary="Fixture complete",
        evidence="Canonical read-back",
        question="",
        deploy_commit="",
    )
    if revise_before_acceptance:
        original = api.finish_execution

        def unavailable(*args, **kwargs):
            raise OSError("Canonical acceptance unavailable")

        monkeypatch.setattr(api, "finish_execution", unavailable)
        with pytest.raises(OSError):
            dispatch(dict(request, action="finish", result=result), api, worker)
        api.tasks.update_task_fields(
            task_id, fields={"notes": "Revised task constraint"}
        )
        monkeypatch.setattr(api, "finish_execution", original)
        dispatch(dict(request, action="finish", result=result), api, worker)
        assert api.task(task_id)["status"] == "in_progress"
        assert api.task(task_id)["execution_timing"]["completed_at"] is None
        return
    finished = dispatch(dict(request, action="finish", result=result), api, worker)
    assert finished["phase"] == "done"
    receipt = api.task(task_id)["execution_timing"]
    assert receipt["completed_at"]
    assert receipt["attempts"][0]["source"] == "codex_vscode"
    assert (
        receipt["completed_at"]
        in messages["vscode-task:" + request["attempt"] + ":result"]
    )
    repeated = dispatch(dict(request, action="finish", result=result), api, worker)
    assert repeated == finished
    assert api.task(task_id)["execution_timing"] == receipt


def test_external_completion_is_not_attributed_to_unfinished_attempt(
    canonical_controller,
):
    _, api, create, *_ = canonical_controller
    task_id = create()
    start(api, task_id)
    api.tasks.update_task_status(task_id, "completed")
    with pytest.raises(ValueError, match="became terminal"):
        finish(api, task_id)
    assert api.task(task_id)["execution_timing"]["completed_at"] is None
