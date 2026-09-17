"""The actual owner transport against a deterministic executable, never a model."""

import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="DGX owner transport uses a Unix lock")

from scripts import codex_von_app_server as transport


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / "protocol-fixture"
    path.write_text(f"#!{sys.executable}\n" + r"""
import json, os, sys, time, select
from pathlib import Path
mode = os.environ.get('FIXTURE_MODE') or (Path('fixture-mode').read_text() if Path('fixture-mode').exists() else 'accepted')
Path('arguments.json').write_text(json.dumps(sys.argv))
log = open('requests.jsonl', 'w')
def emit(value):
    print(json.dumps(value), flush=True)
def notify(method, **params):
    emit({'method':method,'params':{'threadId':'thread-owned', **params}})
def complete(status='completed'):
    result = {'status':'completed', 'summary':'Fixture complete', 'evidence':'Simulated protocol only', 'question':'', 'deploy_commit':''}
    notify('item/completed', turnId='turn-owned', item={'type':'agentMessage', 'phase':'final_answer', 'text':json.dumps(result)})
    notify('turn/completed', turn={'id':'turn-owned','status':status})
started = None
while True:
    if started and mode == 'no_input' and time.monotonic() - started > 0.08:
        complete(); started = None
    if not select.select([sys.stdin], [], [], 0.005)[0]:
        continue
    raw = bytearray()
    while True:
        char = os.read(sys.stdin.fileno(), 1)
        if not char: break
        raw.extend(char)
        if char == b'\n': break
    line = raw.decode()
    if not line: break
    req = json.loads(line); log.write(line); log.flush()
    method = req.get('method'); params = req.get('params', {})
    if method == 'initialize':
        emit({'id':req['id'],'result':{}})
    elif method == 'thread/start':
        emit({'id':req['id'],'result':{'thread':{'id':'thread-owned'}}})
    elif method == 'turn/start':
        assert params['model'] == 'gpt-6-astra' and params['effort'] == 'high'
        notify('turn/started', turn={'id':'turn-owned'})
        if mode == 'early_completion': complete()
        emit({'id':req['id'],'result':{'turn':{'id':'turn-owned'}}})
        started = time.monotonic()
    elif method == 'turn/steer':
        assert params['expectedTurnId'] == 'turn-owned'
        if mode == 'disconnect': sys.exit(0)
        if mode in {'mismatch', 'internal_error'}:
            emit({'id':req['id'],'error':{'code':-32600 if mode == 'mismatch' else -32603,'message':'fixture error'}})
        else:
            emit({'id':req['id'],'result':{'turnId':'replacement-turn' if mode == 'wrong_ack' else 'turn-owned'}})
        complete('interrupted' if mode == 'cancelled' else 'completed')
""")
    path.chmod(0o700)
    return path


def execute(executable, tmp_path, mode="accepted", profile=None, wrong_target=False):
    deliveries, bindings, checkpoints = [], [], []
    messages = [
        {
            "message_id": "source-1",
            "thread_id": "thread-owned",
            "turn_id": "wrong-turn" if wrong_target else "turn-owned",
            "input": [{"type": "text", "text": "Harmless fixture guidance"}],
        }
    ]

    def steering():
        batch = messages[:]
        messages.clear()
        return batch

    with (tmp_path / "worker.lock").open("w") as lock:
        args = dict(
            command=str(executable),
            cwd=str(tmp_path),
            model="gpt-6-astra",
            effort="high",
            prompt="Fixture initial input",
            schema={"type": "object"},
            environment={"PATH": os.environ["PATH"], "FIXTURE_MODE": mode},
            lock_fd=lock.fileno(),
            events=io.StringIO(),
            errors=io.StringIO(),
            permission_profile=profile,
            interval=0.01,
            steering=steering,
            on_delivery=lambda message, status: deliveries.append(
                (message["message_id"], status)
            ),
            on_active=lambda thread, turn: bindings.append((thread, turn)),
            checkpoint=lambda: checkpoints.append(True),
        )
        # Popen needs an actual stderr fd.
        with (tmp_path / "stderr.log").open("w") as errors:
            args["errors"] = errors
            try:
                result = transport.run_turn(**args)
            except transport.ProtocolError as exc:
                result = exc
    return result, deliveries, bindings, checkpoints


@pytest.mark.parametrize(
    "mode,receipt",
    [
        ("accepted", "accepted"),
        ("mismatch", "not_applied"),
        ("wrong_ack", "uncertain"),
        ("internal_error", "uncertain"),
        ("disconnect", "uncertain"),
        ("cancelled", "accepted"),
    ],
)
def test_owned_turn_delivery_never_retargets_or_retries(
    executable, tmp_path, mode, receipt
):
    result, deliveries, bindings, checkpoints = execute(executable, tmp_path, mode)
    assert deliveries == [("source-1", receipt)]
    assert bindings == [("thread-owned", "turn-owned"), (None, None)]
    assert checkpoints
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text().splitlines()
    ]
    assert len([r for r in requests if r.get("method") == "turn/start"]) == 1
    assert len([r for r in requests if r.get("method") == "turn/steer"]) == 1
    if mode in {"disconnect", "cancelled"}:
        assert isinstance(result, transport.ProtocolError)
    else:
        assert json.loads(result)["status"] == "completed"


def test_completed_notification_before_start_ack_is_not_lost(executable, tmp_path):
    result, deliveries, bindings, _ = execute(executable, tmp_path, "early_completion")
    assert json.loads(result)["status"] == "completed"
    assert not deliveries
    assert bindings[-1] == (None, None)


def test_wrong_turn_is_rejected_locally_and_profile_is_preserved(executable, tmp_path):
    result, deliveries, _, _ = execute(
        executable, tmp_path, "no_input", "bounded-worker", True
    )
    assert json.loads(result)["status"] == "completed"
    assert deliveries == [("source-1", "not_applied")]
    args = json.loads((tmp_path / "arguments.json").read_text())
    assert 'default_permissions="bounded-worker"' in args
    assert not any("sandbox_mode=" in arg for arg in args)
    requests = [
        json.loads(line)
        for line in (tmp_path / "requests.jsonl").read_text().splitlines()
    ]
    start = next(r for r in requests if r.get("method") == "thread/start")
    assert start["params"]["approvalPolicy"] == "never"
    assert "sandbox" not in start["params"]
    assert not any(r.get("method") == "turn/steer" for r in requests)


def test_sol_is_rejected_before_process_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda *a, **kw: pytest.fail("provider process")
    )
    with pytest.raises(ValueError, match="Sol-family"):
        transport.run_turn(
            command="forbidden",
            cwd=str(tmp_path),
            model="gpt-5.6-sol",
            effort="high",
            prompt="",
            schema={},
            environment={},
            lock_fd=0,
            events=None,
            errors=None,
        )


def test_exact_worker_launch_uses_owned_transport_and_recovers_structured_result(
    executable, tmp_path, monkeypatch
):
    from scripts import codex_von_worker as worker

    (tmp_path / "fixture-mode").write_text("no_input")
    config = {
        "codex_command": str(executable),
        "codex_transport": "app-server",
        "state_root": str(tmp_path),
        "agent_id": "#V#agent",
        "delegator_id": "#V#owner",
        "organisation_id": "#V#org",
    }
    state = {"task_id": "#V#task", "checkout_prepared": True, "worktree": str(tmp_path)}
    monkeypatch.setattr(worker, "Von", lambda config: SimpleNamespace())
    monkeypatch.setattr(worker, "referenced_tasks", lambda *a: {"results": []})
    monkeypatch.setattr(worker, "stage_file_copy_evidence", lambda *a: {"results": []})
    observed = []
    monkeypatch.setattr(
        worker, "archive_checkpoint", lambda c, s: observed.append(s.get("active_turn"))
    )
    monkeypatch.setattr(worker, "ACTIVITY_CHECKPOINT_SECONDS", 0.01)
    with (tmp_path / "worker.lock").open("w") as lock:
        worker.launch(
            config,
            state,
            {"requested_model": "gpt-6-astra", "requested_reasoning_effort": "high"},
            {"available": False},
            tmp_path / "state.json",
            lock.fileno(),
        )
    assert state["phase"] == "reporting"
    assert state["result"]["status"] == "completed"
    assert state["active_turn"] is None
    active = next(binding for binding in observed if binding)
    assert active["attempt"] == state["attempt"]
    assert active["thread_id"] == "thread-owned"
    assert active["turn_id"] == "turn-owned"
