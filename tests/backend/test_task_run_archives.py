"""Exact controller fixture, durable byte read-back, recovery and private reads."""

import io
import json
import zipfile
from pathlib import Path

import mongomock
import pytest
from flask import Flask

from scripts import codex_von_activity as capture
from scripts import codex_von_worker as worker
from src.backend.services import task_run_archive_service as archives


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    from src.backend.services import task_management_service as tasks
    from src.backend.services.blob_store import LocalBlobStore

    db = mongomock.MongoClient().db
    monkeypatch.setattr(archives, "get_db", lambda: db)
    actor = {"id": "#V#worker", "org": "#V#org", "source": "trusted_local_operator"}
    monkeypatch.setattr(
        archives,
        "get_effective_user_concept_id_with_source",
        lambda: (actor["id"], actor["source"]),
    )
    monkeypatch.setattr(
        archives, "get_effective_organisation_concept_id", lambda: actor["org"]
    )
    monkeypatch.setattr(
        archives,
        "resolve_user_organisation_membership",
        lambda user, org: org == "#V#org",
    )
    task = {
        "task_concept_id": "#V#task",
        "assignee_concept_id": "#V#worker",
        "created_by_concept_id": "#V#owner",
        "organisation_concept_id": "#V#org",
    }
    monkeypatch.setattr(tasks, "get_task", lambda _: task)
    store = LocalBlobStore(tmp_path / "blobs")
    monkeypatch.setattr(archives, "get_blob_store_from_env", lambda: store)
    monkeypatch.setattr(
        archives, "get_blob_store_for_backend_from_env", lambda _: store
    )
    attachments = {}

    def add_attachment(task_id, **fields):
        key = fields["source"]["external_id"]
        attachments.setdefault(
            key, {"attachment_id": key, "task_id": task_id, **fields}
        )
        return attachments[key]

    monkeypatch.setattr(tasks, "add_task_attachment", add_attachment)
    run_id = "a" * 32
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    context = {
        "task_id": "#V#task",
        "worker_identity": "#V#worker",
        "conversation": {"available": False},
        "execution_settings": {
            "model": "gpt-6-astra",
            "reasoning_effort": "high",
            "model_source": "task",
            "reasoning_effort_source": "task",
        },
    }
    (run_dir / "context.json").write_text(json.dumps(context))
    (run_dir / "result.json").write_text('{"status":"completed"}')
    (run_dir / "exit.json").write_text('{"returncode":0}')
    state = {
        "task_id": "#V#task",
        "attempt": run_id,
        "run_dir": str(run_dir),
        "result": {"status": "completed"},
    }
    return state, actor, attachments, db, store


def test_partial_line_large_output_and_restart_archive(fixture):
    state, actor, attachments, _db, _store = fixture
    path = Path(state["run_dir"]) / "events.jsonl"
    first = b'{"type":"thread.started","thread_id":"fixture-thread"}\n'
    path.write_bytes(first + b'{"type":"item.completed","item":')
    capture.checkpoint({}, state)
    live = archives.read_activity(state["attempt"])
    assert live["next_cursor"] == len(first)
    assert live["run"]["capture_status"] == "running"
    assert live["run"]["thread_id"] == "fixture-thread"
    large = {
        "type": "command_execution",
        "command": "fixture",
        "aggregated_output": "x" * 1200000,
        "exit_code": 0,
    }
    with path.open("ab") as stream:
        stream.write(json.dumps(large).encode() + b"}\n")
        stream.write(
            b'{"type":"item.completed","item":{"type":"reasoning","summary":"Available summary"}}\n'
        )
    # Drop all local capture metadata: service cursor is authoritative.
    state.pop("capture")
    capture.checkpoint({}, state, terminal=True)
    assert state["capture"]["status"] == "complete"
    data = archives.download_archive(state["attempt"], task_id=state["task_id"])
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        manifest = json.loads(package.read("manifest.json"))
        assert manifest["final_cursor"] == path.stat().st_size
        assert manifest["event_count"] == 3
        assert manifest["reasoning_summaries"]["exposed_records"] == 1
        assert manifest["source_complete"]
        assert len(package.read("events.jsonl")) > 1200000
    capture.checkpoint({}, state, terminal=True)
    assert len(attachments) == 1
    actor["id"] = "#V#owner"
    assert archives.download_archive(state["attempt"], task_id=state["task_id"]) == data
    actor["id"] = "#V#unrelated"
    assert archives.list_runs(state["task_id"])["runs"] == []
    with pytest.raises(PermissionError):
        archives.download_archive(state["attempt"], task_id=state["task_id"])


def test_credentials_opaque_state_and_inert_hostile_history(fixture, monkeypatch):
    state, *_ = fixture
    monkeypatch.setenv("CONTROLLER_ACCESS_TOKEN", "actual-sentinel-credential")
    event = {
        "type": "item.completed",
        "item": {
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "opaque-test-only",
        },
        "password": "secret-in-json",
        "tool_output": "<script>alert(1)</script> actual-sentinel-credential ghp_abcdefghijklmnop",
    }
    path = Path(state["run_dir"]) / "events.jsonl"
    path.write_text(json.dumps(event) + "\n")
    capture.checkpoint({}, state, terminal=True)
    data = archives.download_archive(state["attempt"], task_id=state["task_id"])
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        events = package.read("events.jsonl").decode()
        assert all(
            secret not in events
            for secret in [
                "secret-in-json",
                "opaque-test-only",
                "actual-sentinel-credential",
                "ghp_abcdefghijklmnop",
            ]
        )
        assert "<script>" in events  # inert text, not lost source content
        manifest = json.loads(package.read("manifest.json"))
        assert manifest["redactions"]["opaque_reasoning_omitted"] == 1
        assert manifest["reasoning_summaries"]["exposed_records"] == 0
    assert "actual-sentinel-credential" in path.read_text()


def test_failed_finalisation_retries_without_duplicate_attachment(fixture, monkeypatch):
    state, _, attachments, db, _ = fixture
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(
        b'{"type":"turn.completed"}\n'
    )
    original = archives.collection
    fail_once = {"yes": True}

    class FailFinalUpdate:
        def __getattr__(self, name):
            return getattr(original(), name)

        def update_one(self, query, update, **kwargs):
            if (
                update.get("$set", {}).get("capture_status") == "complete"
                and fail_once["yes"]
            ):
                fail_once["yes"] = False
                raise OSError("simulated database outage after attachment")
            return original().update_one(query, update, **kwargs)

    monkeypatch.setattr(archives, "collection", lambda: FailFinalUpdate())
    assert not capture.try_checkpoint({}, state, terminal=True)
    assert state["capture"]["status"] == "pending"
    assert len(attachments) == 1
    assert capture.try_checkpoint({}, state, terminal=True)
    assert len(attachments) == 1
    assert db.task_run_archives.find_one()["capture_status"] == "complete"


def test_changed_source_and_mismatched_task_never_complete(fixture):
    state, *_ = fixture
    path = Path(state["run_dir"]) / "events.jsonl"
    path.write_bytes(b'{"type":"turn.started"}\n')
    capture.checkpoint({}, state)
    path.write_bytes(b'{"type":"turn.changed"}\n')
    assert not capture.try_checkpoint({}, state, terminal=True)
    assert state["capture"]["status"] == "pending"
    with pytest.raises(PermissionError):
        archives.read_activity(state["attempt"], task_id="#V#other")
    with pytest.raises(ValueError):
        archives.register_run(
            task_id="#V#other", run_id=state["attempt"], provenance={}
        )


def test_interrupted_tail_and_missing_exit_are_accounted(fixture):
    state, *_ = fixture
    (Path(state["run_dir"]) / "exit.json").unlink()
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(
        b'{"type":"turn.started"}\n{"broken":'
    )
    capture.checkpoint({}, state, terminal=True)
    result = archives.read_activity(state["attempt"])["run"]["manifest"]
    assert not result["source_complete"]
    assert result["missing_files"] == ["exit.json"]
    assert result["invalid_records"] == 1
    assert result["event_count"] == 2
    state.pop("capture")
    capture.checkpoint({}, state, terminal=True)
    assert state["capture"]["source_complete"] is False


def test_source_conversation_revocation_and_org_scope(fixture, monkeypatch):
    from src.backend.services import shared_conversation_service as shares

    state, actor, *_ = fixture
    context_path = Path(state["run_dir"]) / "context.json"
    context = json.loads(context_path.read_text())
    context["conversation"] = {"available": True, "session_id": "private-source"}
    context_path.write_text(json.dumps(context))
    monkeypatch.setattr(shares, "resolve_conversation_owner", lambda **kw: "#V#owner")
    invited = {"yes": True}
    monkeypatch.setattr(
        shares,
        "get_accepted_invite_for_user_session",
        lambda **kw: {"organisation_concept_id": "#V#org"} if invited["yes"] else None,
    )
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(b"{}\n")
    capture.checkpoint({}, state, terminal=True)
    invited["yes"] = False
    with pytest.raises(PermissionError):
        archives.read_activity(state["attempt"])
    actor["id"] = "#V#owner"
    assert archives.read_activity(state["attempt"])["records"]
    actor["org"] = "#V#other"
    with pytest.raises(PermissionError):
        archives.read_activity(state["attempt"])


def test_new_read_routes_deny_forged_actor_and_download_scope(fixture, monkeypatch):
    from src.backend.server.routes import task_routes as routes

    state, actor, *_ = fixture
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(b"{}\n")
    capture.checkpoint({}, state, terminal=True)
    monkeypatch.setattr(routes, "_get_current_user_concept_id", lambda: actor["id"])
    monkeypatch.setattr(routes, "_get_current_org_concept_id", lambda: actor["org"])
    app = Flask(__name__)
    app.register_blueprint(routes.task_bp, url_prefix="/api/tasks")
    client = app.test_client()
    base = "/api/tasks/%23V%23task/runs/" + state["attempt"]
    assert client.get(base).status_code == 200
    assert (
        client.get(base + "/download").headers["Cache-Control"] == "private, no-store"
    )
    actor["id"] = "#V#unrelated"
    assert (
        client.get(
            base + "/download", headers={"X-User-Concept-ID": "#V#owner"}
        ).status_code
        == 403
    )
    actor["id"] = "#V#owner"
    actor["source"] = archives.LEGACY_IDENTITY_HEADER_ACTOR_SOURCE
    assert client.get(base).status_code == 403


def test_hash_readback_detects_storage_corruption(fixture):
    state, _, _, db, store = fixture
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(b"{}\n")
    capture.checkpoint({}, state, terminal=True)
    receipt = db.task_run_archives.find_one()["archive"]
    store.put_bytes(receipt["key"], b"corrupt")
    with pytest.raises(RuntimeError):
        archives.download_archive(state["attempt"], task_id=state["task_id"])


def test_exact_worker_launch_publishes_before_exit_and_downloads_after(
    fixture, tmp_path, monkeypatch
):
    import sys

    from src.backend.server.routes import task_routes as routes

    _, actor, attachments, _db, _ = fixture
    monkeypatch.setattr(worker, "ACTIVITY_CHECKPOINT_SECONDS", 0.05)
    executable = tmp_path / "exec-fixture"
    executable.write_text(f"#!{sys.executable}\n" + """import json, sys, time
from pathlib import Path
sys.stdin.read()
result = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
print(json.dumps({'type':'thread.started','thread_id':'exact-worker-fixture'}), flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'fixture-test','aggregated_output':'Passed','exit_code':0}}), flush=True)
for _ in range(500):
    if (result.parent / 'capture-seen').exists():
        break
    time.sleep(0.01)
else:
    raise RuntimeError('No live canonical activity was observed')
result.write_text(json.dumps({'status':'completed','summary':'Fixture complete','evidence':'Live capture observed','question':'','deploy_commit':''}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':0,'output_tokens':0}}), flush=True)
""")
    executable.chmod(0o700)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = {"task_id": "#V#task", "checkout_prepared": True, "worktree": str(checkout)}
    config = {
        "state_root": str(tmp_path),
        "agent_id": "#V#worker",
        "codex_command": str(executable),
    }
    monkeypatch.setattr(routes, "_get_current_user_concept_id", lambda: actor["id"])
    monkeypatch.setattr(routes, "_get_current_org_concept_id", lambda: actor["org"])
    app = Flask(__name__)
    app.register_blueprint(routes.task_bp, url_prefix="/api/tasks")
    client = app.test_client()
    original = worker.archive_checkpoint
    before_exit = []

    def observe(config, state, **kwargs):
        success = original(config, state, **kwargs)
        response = client.get("/api/tasks/%23V%23task/runs/" + state["attempt"])
        body = response.get_json()
        if response.status_code == 200 and body["records"]:
            assert not (Path(state["run_dir"]) / "exit.json").exists()
            before_exit.append(body["run"]["event_count"])
            (Path(state["run_dir"]) / "capture-seen").touch()
        return success

    monkeypatch.setattr(worker, "archive_checkpoint", observe)
    state_path = tmp_path / "checkpoint.json"
    with (tmp_path / "worker.lock").open("w") as lock:
        worker.launch(
            config,
            state,
            {"requested_model": "gpt-6-astra", "requested_reasoning_effort": "high"},
            {"available": False},
            state_path,
            lock.fileno(),
        )
    assert before_exit and before_exit[0] == 2
    assert state["result"]["status"] == "completed"
    capture.checkpoint(config, state, terminal=True)
    actor["id"] = "#V#owner"
    response = client.get(state["capture"]["archive_url"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as package:
        manifest = json.loads(package.read("manifest.json"))
        assert manifest["event_count"] == 3
        assert manifest["thread_id"] == "exact-worker-fixture"
        assert manifest["source_complete"]
        assert (
            manifest["provenance"]["execution_settings"]["reasoning_effort_source"]
            == "task"
        )
    assert len(attachments) == 1


def test_pending_capture_keeps_coding_outcome_and_retries_reporting(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    state = {
        "task_id": "#V#task",
        "attempt": "b" * 32,
        "result": {
            "status": "completed",
            "summary": "Code finished",
            "evidence": "Tests passed",
            "question": "",
        },
    }
    sent, finished = [], []
    api = SimpleNamespace(
        send=lambda *args: sent.append(args),
        finish=lambda state: finished.append(state["result"]),
    )
    monkeypatch.setattr(worker, "archive_checkpoint", lambda *a, **kw: False)
    path = tmp_path / "state.json"
    worker.finish_captured({}, api, state, path)
    worker.finish_captured({}, api, state, path)
    assert state["phase"] == "archive_pending"
    assert state["result"]["status"] == "completed"
    assert len(sent) == 1 and not finished
    monkeypatch.setattr(worker, "archive_checkpoint", lambda *a, **kw: True)
    worker.finish_captured({}, api, state, path)
    assert len(finished) == 1


def test_bounded_backfill_is_idempotent_and_never_launches(fixture, monkeypatch):
    from types import SimpleNamespace

    state, _, attachments, *_ = fixture
    run_dir = Path(state["run_dir"])
    (run_dir / "events.jsonl").write_bytes(b"{}\n")
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "summary": "Done",
                "evidence": "Passed",
                "question": "",
            }
        )
    )
    config = {
        "state_root": str(run_dir.parents[1]),
        "agent_id": "#V#worker",
        "delegator_id": "#V#owner",
        "organisation_id": "#V#org",
    }
    task = {
        "task_concept_id": "#V#task",
        "assignee_concept_id": "#V#worker",
        "created_by_concept_id": "#V#owner",
        "organisation_concept_id": "#V#org",
    }
    api = SimpleNamespace(task=lambda _: task, native_writer=lambda _: True)
    monkeypatch.setattr(
        worker, "launch", lambda *a: pytest.fail("Backfill must not launch")
    )
    first = worker.backfill_run(config, api, state["task_id"] + "=" + state["attempt"])
    second = worker.backfill_run(config, api, state["task_id"] + "=" + state["attempt"])
    assert first["capture"]["archive_url"] == second["capture"]["archive_url"]
    assert len(attachments) == 1
    with pytest.raises(ValueError):
        worker.backfill_run(config, api, "#V#other=" + state["attempt"])


def test_reassigned_task_keeps_private_run_without_new_attachment(fixture, monkeypatch):
    from src.backend.services import task_management_service as tasks

    state, _, attachments, *_ = fixture
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(b"{}\n")
    capture.checkpoint({}, state)
    monkeypatch.setattr(
        tasks, "get_task", lambda _: {"assignee_concept_id": "#V#replacement"}
    )
    capture.checkpoint({}, state, terminal=True)
    assert not attachments
    run = archives.read_activity(state["attempt"])["run"]
    assert run["capture_status"] == "complete"
    assert run["attachment_status"] == "task_changed_or_unavailable"
    assert archives.download_archive(state["attempt"], task_id=state["task_id"])


def test_batching_does_not_write_per_event(fixture, monkeypatch):
    state, _, _, db, _ = fixture
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(
        b'{"type":"item.completed"}\n' * 1000
    )
    coll = db.task_run_archives
    writes = []
    original = coll.update_one

    def counted(*args, **kwargs):
        writes.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(coll, "update_one", counted)
    capture.checkpoint({}, state, terminal=True)
    row = coll.find_one()
    assert row["event_count"] == 1000
    assert len(row["batches"]) == 1
    assert len(writes) == 3  # registration, one batch, verified final reference


def test_conflicting_batch_retry_is_rejected_without_advancing(fixture):
    state, *_ = fixture
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(b"{}\n")
    capture.checkpoint({}, state)
    with pytest.raises(ValueError, match="Conflicting"):
        archives.append_batch(
            state["attempt"],
            start=0,
            end=3,
            data=b'{"changed":true}\n',
            source_sha256=archives.digest(b"{}\n"),
            event_count=1,
        )
    assert archives.read_activity(state["attempt"])["run"]["cursor"] == 3


def test_real_session_and_trusted_controller_context_resolve_access(
    fixture, monkeypatch
):
    from src.backend.security import access_control as access
    from src.backend.server.routes import task_routes as routes

    state, *_ = fixture
    monkeypatch.setattr(
        archives,
        "get_effective_user_concept_id_with_source",
        access.get_effective_user_concept_id_with_source,
    )
    monkeypatch.setattr(
        archives,
        "get_effective_organisation_concept_id",
        access.get_effective_organisation_concept_id,
    )
    (Path(state["run_dir"]) / "events.jsonl").write_bytes(b"{}\n")
    with access.override_current_actor("#V#worker", "#V#org"):
        capture.checkpoint({}, state, terminal=True)
    app = Flask(__name__)
    app.secret_key = "local-test-fixture-only"
    app.register_blueprint(routes.task_bp, url_prefix="/api/tasks")
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_concept_id"] = "#V#owner"
        session["organisation_concept_id"] = "#V#org"
    assert client.get(state["capture"]["archive_url"]).status_code == 200
    with client.session_transaction() as session:
        session["user_concept_id"] = "#V#unrelated"
    assert client.get(state["capture"]["archive_url"]).status_code == 403
