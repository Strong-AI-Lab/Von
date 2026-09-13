"""Boundary tests for optional task-scoped native Android testing."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts import codex_von_android as host
from src.backend.services import coding_agent_android_service as service


@pytest.fixture
def scope(monkeypatch):
    from src.backend.security import access_control
    from src.backend.services import (
        organisation_membership_governance_service as governance,
    )
    from src.backend.services import organisation_membership_service as membership

    slot = dict(
        instance_id="test",
        agent_id="#V#agent",
        delegator_id="#V#owner",
        organisation_id="#V#org",
        android=dict(
            host="mac", unix_account="test", provisioner_command=["/trusted/android"]
        ),
    )
    task = dict(
        organisation_concept_id="#V#org",
        assignee_concept_id="#V#agent",
        created_by_concept_id="#V#owner",
        status="in_progress",
    )
    monkeypatch.setattr(governance, "_trusted_actor", lambda: ("#V#agent", None))
    monkeypatch.setattr(
        access_control, "get_effective_organisation_concept_id", lambda: "#V#org"
    )
    monkeypatch.setattr(
        membership,
        "resolve_user_organisation_membership",
        lambda *_: {"role": "member"},
    )
    monkeypatch.setattr(
        service.instances, "load_registry", lambda: {"instances": {"test": slot}}
    )
    monkeypatch.setattr(service.tasks, "get_task", lambda *_: task)
    return slot, task


def test_assigned_agent_route_attaches_verified_bytes(scope, monkeypatch):
    slot, _ = scope
    data = b"example native evidence"
    result = {
        **{k: v for k, v in slot.items() if k != "android"},
        "host": "mac",
        "unix_account": "test",
        "task_id": "#V#task",
        "run_id": "run",
        "status": "ready",
        "artifact": dict(
            filename="screen.png",
            media_type="image/png",
            data_base64=base64.b64encode(data).decode(),
            sha256=hashlib.sha256(data).hexdigest(),
        ),
    }
    calls = []
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(result)),
    )
    monkeypatch.setattr(
        service.tasks,
        "add_task_attachment_bytes",
        lambda *a, **kw: calls.append(kw) or {"attachment_id": "verified"},
    )
    response = service.coding_agent_android(
        instance_id="test", task_id="#V#task", run_id="run", action="screenshot"
    )
    assert response["attachment"]["attachment_id"] == "verified"
    assert calls[0]["data"] == data and calls[0]["actor_concept_id"] == "#V#agent"
    assert "artifact" not in response


@pytest.mark.parametrize(
    "field,value",
    [
        ("organisation_concept_id", "#V#other"),
        ("created_by_concept_id", "#V#other"),
        ("assignee_concept_id", "#V#other"),
    ],
)
def test_other_org_owner_or_reassigned_task_denied_before_host(
    scope, monkeypatch, field, value
):
    scope[1][field] = value
    monkeypatch.setattr(
        service.subprocess, "run", lambda *a, **kw: pytest.fail("host called")
    )
    with pytest.raises(PermissionError):
        service.coding_agent_android(
            instance_id="test", task_id="#V#task", run_id="run", action="start"
        )


def test_forged_identity_and_revoked_membership(scope, monkeypatch):
    from src.backend.services import (
        organisation_membership_governance_service as governance,
    )

    monkeypatch.setattr(
        governance, "_trusted_actor", lambda: (None, {"error_code": "untrusted_actor"})
    )
    with pytest.raises(PermissionError, match="untrusted_actor"):
        service.authorised_slot("test", "#V#task")


def test_terminal_task_can_clean_up_but_cannot_launch(scope, monkeypatch):
    scope[1]["status"] = "completed"
    with pytest.raises(PermissionError, match="not executable"):
        service.coding_agent_android(
            instance_id="test", task_id="#V#task", run_id="run", action="start"
        )
    monkeypatch.setattr(
        service.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1)
    )
    with pytest.raises(RuntimeError, match="host operation"):
        service.coding_agent_android(
            instance_id="test", task_id="#V#task", run_id="run", action="stop"
        )


def test_cross_task_session_and_shell_input_are_denied():
    with pytest.raises(PermissionError):
        host.own_session(
            {"task_id": "#V#a", "run_id": "r"}, {"task_id": "#V#b", "run_id": "r"}
        )
    for value in ["$(touch /tmp/pwned)", "x'; echo secret", "a\nb"]:
        with pytest.raises(ValueError):
            host.operate({}, {}, {"action": "text", "text": value})


def test_duplicate_launch_and_stopped_attempt_never_respawn(tmp_path, monkeypatch):
    spec = {key: key for key in host.IDENTITY}
    monkeypatch.setattr(host, "validate", lambda _: ({}, tmp_path))
    monkeypatch.setattr(
        host.subprocess, "Popen", lambda *a, **kw: pytest.fail("duplicate launch")
    )
    request = {"action": "start", "task_id": "#V#task", "run_id": "same"}
    for phase in ["starting", "ready", "stopped"]:
        host.write(
            tmp_path / "session.json",
            dict(task_id="#V#task", run_id="same", status=phase),
        )
        assert host.lifecycle(spec, request, "binding")["status"] == phase
    with pytest.raises(PermissionError):
        host.lifecycle(spec, {**request, "task_id": "#V#another"}, "binding")


def test_missing_prerequisites_do_not_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host.platform, "machine", lambda: "arm64")
    with pytest.raises(ValueError, match="platform"):
        host.validate({"android": {"platform": ["Linux", "x86_64"]}})


def test_worker_request_is_fixed_to_assignment_and_deduplicated(tmp_path, monkeypatch):
    from scripts import codex_von_android_control as controller

    root = tmp_path / ".run" / "child"
    root.mkdir(parents=True)
    receipts = tmp_path / "controller"
    config = {"android_instance_id": "test"}
    state = {"task_id": "#V#fixed", "attempt": "fixed-run"}
    calls = []
    monkeypatch.setattr(
        service,
        "coding_agent_android",
        lambda **kw: calls.append(kw) or {"status": "ready"},
    )
    (root / "request.json").write_text(
        json.dumps({"action": "tap", "x": 1, "y": 2, "request_id": "gesture1"})
    )
    controller.poll(config, state, root, receipts)
    controller.poll(config, state, root, receipts)
    assert (
        len(calls) == 1
        and calls[0]["task_id"] == "#V#fixed"
        and calls[0]["run_id"] == "fixed-run"
    )
    (root / "request.json").write_text(
        json.dumps({"action": "start", "task_id": "#V#another"})
    )
    controller.poll(config, state, root, receipts)
    assert len(calls) == 1


def test_worker_lost_effect_ack_is_not_repeated(tmp_path, monkeypatch):
    from scripts import codex_von_android_control as controller

    root = tmp_path / ".run" / "child"
    root.mkdir(parents=True)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    raw = json.dumps({"action": "text", "text": "Hello", "request_id": "1"}).encode()
    (root / "request.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (receipts / (digest + ".json")).write_text(
        json.dumps({"status": "effect_uncertain"})
    )
    monkeypatch.setattr(
        service, "coding_agent_android", lambda **kw: pytest.fail("repeated effect")
    )
    controller.poll(
        {"android_instance_id": "test"},
        {"task_id": "#V#t", "attempt": "r"},
        root,
        receipts,
    )
    assert (
        json.loads((root / "response.json").read_text())["status"] == "effect_uncertain"
    )


def test_stale_socket_recovery_preserves_an_unrelated_process(tmp_path, monkeypatch):
    import socket

    spec = {key: key for key in host.IDENTITY}
    state = {
        "task_id": "#V#task",
        "run_id": "r",
        "avd_name": "owned-avd",
        "status": "ready",
        "emulator_pid": 1234,
    }
    host.write(tmp_path / "session.json", state)
    # macOS AF_UNIX addresses are shorter than pytest long temporary paths.
    import tempfile

    short = tempfile.TemporaryDirectory(prefix="von-", dir="/tmp")
    tmp_path = __import__("pathlib").Path(short.name)
    host.write(tmp_path / "session.json", state)
    endpoint = tmp_path / "control.sock"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(endpoint))
    sock.close()
    monkeypatch.setattr(host, "validate", lambda _: ({}, tmp_path))
    monkeypatch.setattr(
        host.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="unrelated-editor"),
    )
    monkeypatch.setattr(
        host.os, "kill", lambda *a: pytest.fail("killed unrelated process")
    )
    result = host.lifecycle(
        spec, {"action": "stop", "task_id": "#V#task", "run_id": "r"}, "binding"
    )
    assert result["status"] == "stopped" and not endpoint.exists()


def test_child_mailbox_cannot_redirect_controller_writes(tmp_path, monkeypatch):
    from scripts import codex_von_android_control as controller

    root = tmp_path / ".run" / "child"
    root.mkdir(parents=True)
    secret = tmp_path / "private.txt"
    secret.write_text("preserve")
    (root / "response.json").symlink_to(secret)
    controller.respond(root, {"status": "ready"})
    assert secret.read_text() == "preserve"
    (root / "request.json").symlink_to(secret)
    with pytest.raises(OSError):
        controller.poll({}, {}, root, tmp_path / "receipts")
    import shutil

    shutil.rmtree(root)
    root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        controller.respond(root, {"status": "ready"})
