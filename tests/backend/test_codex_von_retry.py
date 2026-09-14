"""Unchanged-blocker admission and the existing controller/canonical service path.

All services use an isolated mongomock database. The subprocess is a tiny local
fixture, not Codex: no coding agent, provider, host bridge or live task is used.
"""

import copy
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import codex_von_inbox as inbox
from scripts import codex_von_retry as retry
from scripts import codex_von_worker as worker


def descriptor(**changes):
    return {
        "key": "browser_fixture",
        "kind": "dependency",
        "owner": "operator",
        "recovery": "Verify the candidate profile and authenticated fixture actor.",
        "scope_json": json.dumps({"candidate_sha": "a" * 40, "profile": "#V#profile"}),
        "required_observations_json": json.dumps(
            {"authenticated": True, "profile_bound": True}
        ),
        "probe_key": None,
        **changes,
    }


@pytest.fixture
def config(tmp_path):
    return {
        "agent_id": "#V#worker",
        "delegator_id": "#V#owner",
        "organisation_id": "#V#org",
        "state_root": str(tmp_path / "state"),
    }


def blocked_state(config, **changes):
    state = {
        "task_id": "#V#task",
        "attempt": "failed-attempt",
        "phase": "waiting",
        "finished_at": "2026-09-13T21:12:00Z",
        "result": {
            "status": "blocked",
            "summary": "Fixture prerequisite unavailable",
            "evidence": "Observed authenticated:false",
            "question": "Repair fixture",
            "blocker": descriptor(),
        },
        **changes,
    }
    retry.retain_blocker(config, state)
    return state


def repair_receipt(state):
    blocker = state["blocker"]
    return {
        "attempt": blocker["attempt"],
        "blocker_key": blocker["key"],
        "binding": copy.deepcopy(blocker["binding"]),
        "scope": copy.deepcopy(blocker["scope"]),
        "observed_at": "2026-09-13T21:17:00Z",
        "observations": {"authenticated": True, "profile_bound": True},
        "reason": "Operator verified repaired fixture",
        "evidence_reference": "#V#computer_file_copy_fixture sha256:" + "b" * 64,
    }


def repair_comment(config, state):
    return {
        "comment_id": "repair-comment",
        "author_concept_id": config["delegator_id"],
        "body": "Verified repair",
        "source": {"coding_retry": repair_receipt(state)},
    }


@pytest.mark.parametrize(
    "change",
    [
        "wrong_task",
        "wrong_actor",
        "wrong_org",
        "wrong_attempt",
        "wrong_key",
        "wrong_revision",
        "stale",
        "future",
        "auth_false",
        "auth_integer",
        "missing_check",
        "missing_evidence",
        "untrusted_author",
        "worker_author",
        "quoted_retry",
        "plain_ready",
    ],
)
def test_irrelevant_stale_and_contradictory_evidence_never_admits(config, change):
    state = blocked_state(config)
    comment = repair_comment(config, state)
    receipt = comment["source"]["coding_retry"]
    if change.startswith("wrong_"):
        field = change.removeprefix("wrong_")
        if field in {"task", "actor", "org"}:
            receipt["binding"][
                {"task": "task_id", "actor": "agent_id", "org": "organisation_id"}[
                    field
                ]
            ] = "wrong"
        elif field == "revision":
            receipt["scope"]["candidate_sha"] = "c" * 40
        else:
            receipt["blocker_key" if field == "key" else field] = "wrong"
    elif change in {"stale", "future"}:
        receipt["observed_at"] = (
            "2026-09-13T21:00:00Z" if change == "stale" else "2099-01-01T00:00:00Z"
        )
    elif change in {"auth_false", "auth_integer"}:
        # The observed PR649 failure: top-level readiness contradicted by auth.
        receipt["setup_ready"] = True
        receipt["observations"]["authenticated"] = (
            False if change == "auth_false" else 1
        )
    elif change == "missing_check":
        del receipt["observations"]["profile_bound"]
    elif change == "missing_evidence":
        del receipt["evidence_reference"]
    elif change in {"untrusted_author", "worker_author"}:
        comment["author_concept_id"] = (
            "#V#third_party" if change == "untrusted_author" else config["agent_id"]
        )
    else:
        comment.pop("source")
        comment["body"] = (
            "> /retry-coding failed-attempt please retry"
            if change == "quoted_retry"
            else "Setup ready, CI passed."
        )
    assert retry.admission(config, state, {"comments": [comment]}) is None


def test_repair_admits_once_and_cannot_apply_to_next_attempt(config):
    state = blocked_state(config)
    inputs = {"comments": [repair_comment(config, state)]}
    decision = retry.admission(config, state, inputs)
    assert decision["kind"] == "verified_repair"
    retry.consume(state, decision)
    assert retry.admission(config, state, inputs) is None
    state["attempt"] = "next-failed-attempt"
    assert retry.admission(config, state, inputs) is None


def test_legacy_text_preserves_reason_and_accepts_explicit_retry(config):
    state = blocked_state(config)
    del state["result"]["blocker"]
    del state["blocker"]
    assert (
        retry.admission(config, state, {"status": "pending", "evidence": "New archive"})
        is None
    )
    assert state["blocker"]["kind"] == "unclassified"
    assert state["blocker"]["reason"] == state["result"]["summary"]
    comment = {
        "comment_id": "manual",
        "author_concept_id": config["delegator_id"],
        "body": "/retry-coding failed-attempt Checked retained effects; continue the repaired path.",
    }
    decision = retry.admission(config, state, {"comments": [comment]})
    assert decision["kind"] == "manual_retry"
    retry.consume(state, decision)
    assert retry.admission(config, state, {"comments": [comment]}) is None


@pytest.fixture
def canonical_controller(config, tmp_path, monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_coding_retry_" + tmp_path.name)
    from src.backend.services import organisation_membership_service as memberships

    monkeypatch.setattr(
        memberships,
        "get_organisation_members",
        lambda _: {
            "members": [
                {"user_concept_id": config["agent_id"]},
                {"user_concept_id": config["delegator_id"]},
            ],
        },
    )
    api = worker.Von(config)
    monkeypatch.setattr(
        api.messages, "maybe_launch_direct_message_workflow", lambda **kw: None
    )
    for name in (
        "maybe_launch_task_created_workflow",
        "maybe_launch_task_status_workflow",
        "maybe_launch_effort_unit_completed_workflow",
    ):
        monkeypatch.setattr(api.tasks, name, lambda **kw: None)
    monkeypatch.setattr(api, "conversation", lambda _: {"available": False})
    monkeypatch.setattr(worker, "archive_checkpoint", lambda *a, **kw: True)
    source = tmp_path / "source"
    worker.command(["git", "init", "-b", "main", str(source)])
    (source / "AGENTS.md").write_text("Isolated controller fixture.\n")
    worker.command(["git", "-C", str(source), "add", "AGENTS.md"])
    worker.command(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Fixture",
        ]
    )
    worker.command(["git", "-C", str(source), "remote", "add", "origin", str(source)])
    counter = tmp_path / "launches.jsonl"
    fake = tmp_path / "fixture-codex"
    result = {
        "status": "blocked",
        "summary": "Fixture prerequisite unavailable",
        "question": "Repair fixture",
        "evidence": "Observed authenticated:false",
        "deploy_commit": "",
        "blocker": descriptor(),
    }
    fake.write_text(
        f"#!{sys.executable}\n" + "import json, sys\nfrom pathlib import Path\n"
        "result = Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "context = json.loads((result.parent / 'context.json').read_text())\n"
        f"with open({str(counter)!r}, 'a') as stream: stream.write(json.dumps(context) + '\\n')\n"
        f"result.write_text(json.dumps({result!r}))\n"
    )
    fake.chmod(0o700)
    config.update(source_repo=str(source), codex_command=str(fake))

    def create(title="Fixture task"):
        return api.tasks.create_task(
            title,
            "Authorised isolated controller acceptance",
            assignee_concept_id=config["agent_id"],
            created_by_concept_id=config["delegator_id"],
            organisation_concept_id=config["organisation_id"],
            requested_model="gpt-6-astra",
            requested_reasoning_effort="xhigh",
        )["task_concept_id"]

    def read_state(task_id):
        path = (
            Path(config["state_root"])
            / "tasks"
            / (worker.fingerprint(task_id)[:16] + ".json")
        )
        return path, json.loads(path.read_text())

    def launches():
        return (
            [json.loads(line) for line in counter.read_text().splitlines()]
            if counter.exists()
            else []
        )

    with (tmp_path / "controller.lock").open("w") as lock:
        yield config, api, create, read_state, launches, lock.fileno()


def test_actual_controller_skips_bookkeeping_and_continues_other_tasks(
    canonical_controller,
):
    config, api, create, read_state, launches, fd = canonical_controller
    task_id = create()
    worker.tick(config, api, fd)
    _, state = read_state(task_id)
    assert api.task(task_id)["status"] == "blocked"
    assert (
        "Waiting without another coding launch" in api.task(task_id)["next_checkpoint"]
    )
    result_message = api.messages.get_message_for_user(
        state["message_id"], config["delegator_id"]
    )
    assert (
        "Recovery owner: operator"
        in api.messages.project_direct_message(result_message)["content"]
    )
    for i in range(4):
        api.tasks.update_task_fields(
            task_id,
            fields={
                "evidence": f"Verified archive {i}",
                "notes": f"CI and closeout {i}",
            },
        )
        api.tasks.add_task_comment(
            task_id, body=f"Self report {i}", author_concept_id=config["agent_id"]
        )
        api.tasks.add_task_comment(
            task_id,
            body=f"Status note {i}; CI passed",
            author_concept_id=config["delegator_id"],
        )
        api.tasks.add_task_attachment(
            task_id,
            filename=f"archive-{i}.zip",
            uri=f"https://example.invalid/archive-{i}",
        )
        api.tasks.update_task_status(task_id, "pending")
        worker.tick(config, api, fd)
    assert len(launches()) == 1
    other = create("Other eligible task")
    worker.tick(config, api, fd)
    assert [row["task_id"] for row in launches()] == [task_id, other]
    _, state = read_state(task_id)
    receipt = repair_receipt(state)
    receipt["observed_at"] = (datetime.now(UTC) + timedelta(microseconds=1)).isoformat()
    api.tasks.add_task_comment(
        task_id,
        body="Repair verified",
        author_concept_id=config["delegator_id"],
        source={"coding_retry": receipt},
    )
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    assert len(launches()) == 3
    resumed = launches()[-1]
    assert resumed["prior_result"]["status"] == "blocked"
    assert resumed["retry_admission"]["kind"] == "verified_repair"
    assert resumed["execution_settings"]["reasoning_effort"] == "xhigh"
    assert (
        resumed["capabilities"]["worktree"] == launches()[0]["capabilities"]["worktree"]
    )
    _, resumed_state = read_state(task_id)
    assert len(resumed_state["consumed_retry_tokens"]) == 1


def test_lost_result_ack_reconciles_without_another_subprocess(
    canonical_controller, monkeypatch
):
    config, api, create, read_state, launches, fd = canonical_controller
    task_id = create()
    original = api.send
    failed = False

    def lost_ack(task, key, content):
        nonlocal failed
        result = original(task, key, content)
        if key.endswith(":result") and not failed:
            failed = True
            raise OSError("simulated lost message acknowledgement")
        return result

    monkeypatch.setattr(api, "send", lost_ack)
    with pytest.raises(OSError):
        worker.tick(config, api, fd)
    _path, state = read_state(task_id)
    assert state["phase"] == "reporting"
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    assert len(launches()) == 1
    assert read_state(task_id)[1]["phase"] == "waiting"
    messages = api.messages.get_messages_for_user(
        config["delegator_id"], thread_id=task_id
    )
    assert len(messages) == 2  # pickup and one canonical result


def test_missing_exit_reconciles_unknown_effects_without_replay(canonical_controller):
    config, api, create, read_state, launches, fd = canonical_controller
    task_id = create()
    worker.tick(config, api, fd)
    path, state = read_state(task_id)
    state["phase"] = "running"
    state.pop("result")
    state.pop("report_content", None)
    (Path(state["run_dir"]) / "exit.json").unlink()
    # Use a new attempt identity to model a process lost before any result report.
    state["attempt"] = "interrupted-attempt"
    worker.write_json(path, state)
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    assert len(launches()) == 1
    assert "Reconcile" in read_state(task_id)[1]["result"]["question"]


def test_transient_probe_backoff_and_recovery(config, tmp_path):
    state = blocked_state(config)
    state["result"]["blocker"] = descriptor(kind="transient", probe_key="fixture")
    state.pop("blocker")
    retry.retain_blocker(config, state)
    path = tmp_path / "state.json"
    receipt_path = tmp_path / "probe-output.json"
    calls = tmp_path / "probe-calls"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from pathlib import Path\n"
        + f"with open({str(calls)!r}, 'a') as f: f.write('check\\n')\n"
        + f"print(Path({str(receipt_path)!r}).read_text())\n"
    )
    config["retry_probes"] = {
        "fixture": {
            "argv": [sys.executable, str(probe)],
            "timeout_seconds": 1,
            "interval_seconds": 10,
            "max_interval_seconds": 40,
        }
    }
    bad = repair_receipt(state)
    bad["observations"]["authenticated"] = False
    worker.write_json(receipt_path, bad)
    assert worker.probe_recovery(config, state, path) is None
    assert worker.probe_recovery(config, state, path) is None
    assert len(calls.read_text().splitlines()) == 1
    state["blocker"]["next_check_at"] = "2000-01-01T00:00:00Z"
    worker.write_json(receipt_path, repair_receipt(state))
    decision = worker.probe_recovery(config, state, path)
    assert decision["kind"] == "probe_recovery"
    retry.consume(state, decision)
    assert len(state["consumed_retry_tokens"]) == 1
    assert worker.probe_recovery(config, state, path) is None


def test_natural_language_followup_is_bound_to_retained_attempt(canonical_controller):
    config, api, create, read_state, _launches, fd = canonical_controller
    task_id = create()
    worker.tick(config, api, fd)
    _, prior = read_state(task_id)
    message_doc = api.messages.create_message(
        sender_id=config["delegator_id"],
        recipient_ids=[config["agent_id"]],
        content="The operator repaired the fixture. Continue the same task.",
        org_id=config["organisation_id"],
        thread_id=task_id,
    )
    message = api.messages.project_direct_message(message_doc)
    state = {
        "phase": "reporting",
        "context": {"message": message, "tasks": [api.task(task_id)]},
        "result": {
            "action": "resume_task",
            "task_id": task_id,
            "answer": "Relevant repair verified; continue the retained work.",
            "deployment_requested": False,
        },
    }
    path = inbox.state_path(config, message["message_id"])
    inbox.finish(config, api, state, path)
    followup = inbox.task_followup(config, task_id)
    assert followup["retry"]["attempt"] == prior["attempt"]
    config["inbox_enabled"] = True
    # Exercise canonical followup reads without running the semantic inbox fixture.
    inputs = api.inputs(api.task(task_id))
    assert retry.admission(config, prior, inputs)["kind"] == "authorised_followup"
    decision = retry.admission(config, prior, inputs)
    retry.consume(prior, decision)
    prior.update(attempt="subsequent-failure", phase="waiting")
    worker.write_json(read_state(task_id)[0], prior)
    # Replay a lost-ack inbox state: do not reopen/rebind to the subsequent failure.
    state.pop("resume_applied")
    api.tasks.update_task_status(task_id, "blocked")
    inbox.finish(config, api, state, path)
    assert api.task(task_id)["status"] == "blocked"
    assert (
        inbox.task_followup(config, task_id)["retry"]["attempt"] != "subsequent-failure"
    )
    assert retry.admission(config, prior, api.inputs(api.task(task_id))) is None


def test_manual_retry_uses_actual_controller_and_one_continuation(canonical_controller):
    config, api, create, read_state, launches, fd = canonical_controller
    task_id = create()
    worker.tick(config, api, fd)
    _, state = read_state(task_id)
    api.tasks.add_task_comment(
        task_id,
        author_concept_id=config["delegator_id"],
        body=f"/retry-coding {state['attempt']} Reconciled retained effects; retry once.",
    )
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    assert len(launches()) == 2
    assert launches()[-1]["retry_admission"]["kind"] == "manual_retry"


def test_transient_probe_can_resume_actual_controller(
    canonical_controller, monkeypatch
):
    config, api, create, read_state, launches, fd = canonical_controller
    task_id = create()
    worker.tick(config, api, fd)
    path, state = read_state(task_id)
    state["blocker"].update(kind="transient", probe_key="fixture")
    worker.write_json(path, state)
    receipt = repair_receipt(state)
    receipt["observed_at"] = datetime.now(UTC).isoformat()
    config["retry_probes"] = {
        "fixture": {
            "argv": [sys.executable, "-c", "print(" + repr(json.dumps(receipt)) + ")"],
            "timeout_seconds": 1,
            "interval_seconds": 10,
            "max_interval_seconds": 40,
        }
    }
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    assert len(launches()) == 2
    assert launches()[-1]["retry_admission"]["kind"] == "probe_recovery"


@pytest.mark.parametrize("change", ["cancelled", "reassigned"])
def test_revocation_during_probe_prevents_coding(
    canonical_controller, monkeypatch, change
):
    config, api, create, _read_state, launches, fd = canonical_controller
    task_id = create()
    worker.tick(config, api, fd)

    def probe(*args):
        if change == "cancelled":
            api.tasks.update_task_status(task_id, "cancelled")
        else:
            api.tasks.assign_task(task_id, "#V#other")
        return {
            "kind": "probe_recovery",
            "token": "probe:fixture",
            "reason": "Repaired",
        }

    monkeypatch.setattr(worker, "probe_recovery", probe)
    worker.tick(config, api, fd)
    assert len(launches()) == 1


def test_missing_state_requires_reconciliation_and_reports_once(canonical_controller):
    config, api, create, read_state, launches, fd = canonical_controller
    task_id = create()
    api.tasks.update_task_status(task_id, "in_progress")
    worker.tick(config, api, fd)
    worker.tick(config, api, fd)
    assert not launches()
    assert api.task(task_id)["status"] == "blocked"
    assert read_state(task_id)[1]["blocker"]["kind"] == "unclassified"
    assert (
        len(
            api.messages.get_messages_for_user(
                config["delegator_id"], thread_id=task_id
            )
        )
        == 1
    )


def test_pending_report_failure_does_not_wedge_other_work(
    canonical_controller, monkeypatch
):
    config, api, create, read_state, launches, fd = canonical_controller
    task_id = create()
    worker.tick(config, api, fd)
    path, state = read_state(task_id)
    state["phase"] = "reporting"
    worker.write_json(path, state)
    original = api.finish

    def finish(state):
        if state["task_id"] == task_id:
            raise OSError("retained reporting unavailable")
        original(state)

    monkeypatch.setattr(api, "finish", finish)
    other = create("Another eligible task")
    worker.tick(config, api, fd)
    assert [row["task_id"] for row in launches()] == [task_id, other]
    assert read_state(task_id)[1]["phase"] == "reporting"
