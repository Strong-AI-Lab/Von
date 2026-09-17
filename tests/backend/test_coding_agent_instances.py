"""Prepared instance lifecycle and its actor/host authority boundary."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl")

from scripts import codex_von_instance as host
from src.backend.services import coding_agent_instance_service as service


@pytest.fixture
def slot(tmp_path):
    return {
        "instance_id": "test-agent",
        "agent_id": "#V#test_agent",
        "agent_name": "Test Agent",
        "role": "Test coding role",
        "delegator_id": "#V#owner",
        "organisation_id": "#V#org",
        "endpoint": "http://localhost:5000",
        "host": "test-host",
        "unix_account": "test-account",
        "provisioner_command": ["/trusted/provisioner"],
        "instance_root": str(tmp_path / "instance"),
        "release_commit": "a" * 40,
        "capacity_enrolment_verified": True,
        "resource_limits_verified": True,
        "schedule_commands": {
            x: ["schedule", x] for x in ("enable", "disable", "status")
        },
        "worker_config": {
            "agent_id": "#V#test_agent",
            "delegator_id": "#V#owner",
            "organisation_id": "#V#org",
            "source_repo": str(tmp_path),
            "state_root": str(tmp_path / "instance/state"),
            "codex_command": str(tmp_path),
            "python_environment": str(tmp_path),
            "host_capacity": {"lock_path": str(tmp_path / "host.lock")},
        },
    }


@pytest.fixture
def actor(monkeypatch, slot):
    from src.backend.security import access_control
    from src.backend.services import (
        organisation_membership_governance_service as governance,
    )
    from src.backend.services import organisation_membership_service as memberships

    monkeypatch.setattr(governance, "_trusted_actor", lambda: ("#V#owner", None))
    monkeypatch.setattr(
        access_control, "get_effective_organisation_concept_id", lambda: "#V#org"
    )
    monkeypatch.setattr(
        memberships,
        "resolve_user_organisation_membership",
        lambda *_: {"role": "owner"},
    )
    monkeypatch.setattr(
        service, "load_registry", lambda: {"instances": {"test-agent": slot}}
    )


def test_normal_tool_path_lists_only_scoped_bindings(actor, slot):
    from src.backend.integrations.internal_mcp.coding_agent_instance_tools import (
        _instances,
    )

    result = _instances(action="list")
    assert result["instances"][0]["agent_id"] == "#V#test_agent"
    assert "provisioner_command" not in result["instances"][0]
    slot["organisation_id"] = "#V#other"
    assert _instances(action="list") == {"instances": []}
    assert _instances(action="resume", instance_id="test-agent")["success"] is False


def test_payload_actor_cannot_create_authority(actor, monkeypatch):
    from src.backend.services import (
        organisation_membership_governance_service as governance,
    )

    monkeypatch.setattr(
        governance,
        "_trusted_actor",
        lambda: (None, {"error_code": "client_supplied_identity_is_not_authority"}),
    )
    with pytest.raises(PermissionError, match="client_supplied"):
        service.coding_agent_instances(action="list")


def test_live_membership_required_even_for_enrolled_owner(actor, monkeypatch):
    from src.backend.services import organisation_membership_service as memberships

    monkeypatch.setattr(
        memberships,
        "resolve_user_organisation_membership",
        lambda *_: {"role": "member"},
    )
    with pytest.raises(PermissionError, match="Live organisation"):
        service.coding_agent_instances(action="resume", instance_id="test-agent")


@pytest.mark.parametrize(
    "settings",
    [
        {"command": "evil"},
        {"model": "gpt-5.6-sol"},
        {"reasoning_effort": "extra high"},
    ],
)
def test_tool_rejects_commands_and_bad_settings_before_effects(
    actor, monkeypatch, settings
):
    monkeypatch.setattr(service, "_host", lambda *_: pytest.fail("host effect"))
    with pytest.raises(ValueError):
        service.coding_agent_instances(
            action="reconcile", instance_id="test-agent", settings=settings
        )


def test_host_receipt_must_match_all_bindings(slot, monkeypatch):
    response = {
        k: slot[k]
        for k in (
            "instance_id",
            "agent_id",
            "delegator_id",
            "organisation_id",
            "host",
            "unix_account",
        )
    }
    response["organisation_id"] = "#V#wrong"
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(response)),
    )
    with pytest.raises(RuntimeError, match="does not match"):
        service._host(slot, "status", {})


def test_registry_rejects_duplicate_identity(tmp_path, slot, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"instances": {"one": slot, "two": slot}}))
    path.chmod(0o600)
    monkeypatch.setenv("VON_CODING_AGENT_REGISTRY", str(path))
    with pytest.raises(ValueError, match="Duplicate"):
        service.load_registry()
    path.chmod(0o666)
    with pytest.raises(PermissionError):
        service.load_registry()


@pytest.fixture
def prepared(slot, monkeypatch):
    Path(slot["worker_config"]["host_capacity"]["lock_path"]).touch()
    state = {"enabled": False, "prepared": 0}

    def schedule(argv):
        if argv[-1] == "enable":
            state["enabled"] = True
        elif argv[-1] == "disable":
            state["enabled"] = False
        return json.dumps({"enabled": state["enabled"]})

    def prepare(primary, root, sha):
        state["prepared"] += 1
        target = root / sha
        target.mkdir(parents=True, exist_ok=True)
        (target / "scripts").mkdir(exist_ok=True)
        (target / "scripts/codex_von_worker.py").write_text(
            "INSTANCE_PROTOCOL_VERSION = 1"
        )
        (target / "scripts/codex_von_capacity.py").touch()
        return target

    def activate(root, target):
        link = root / "current"
        link.unlink(missing_ok=True)
        link.symlink_to(target)

    monkeypatch.setattr(host, "run", schedule)
    monkeypatch.setattr(host.release, "prepare", prepare)
    monkeypatch.setattr(host.release, "activate", activate)
    return state


def test_reconcile_repeat_pause_resume_and_interrupted_activation(
    slot, prepared, monkeypatch
):
    result = host.lifecycle(
        slot, {"action": "reconcile", "settings": {"reasoning_effort": "high"}}
    )
    assert result["desired_state"] == "paused"
    assert not prepared["enabled"]
    assert result["selected_settings"]["reasoning_effort"] == "high"
    assert result["task_and_reply_readiness"] == "not_established_by_provisioning"
    host.lifecycle(slot, {"action": "reconcile"})
    root = Path(slot["instance_root"])
    assert (
        json.loads((root / "worker.json").read_text())["model_reasoning_effort"]
        == "high"
    )
    assert len(list((root / "releases").iterdir())) == 2  # one release and current link
    actual_run = host.run
    monkeypatch.setattr(
        host, "run", lambda *_: (_ for _ in ()).throw(OSError("interrupted"))
    )
    with pytest.raises(OSError):
        host.lifecycle(slot, {"action": "resume"})
    assert (
        json.loads((root / "instance.json").read_text())["desired_state"] == "running"
    )
    monkeypatch.setattr(host, "run", actual_run)
    assert host.lifecycle(slot, {"action": "resume"})["schedule"]["enabled"]
    assert not host.lifecycle(slot, {"action": "pause"})["schedule"]["enabled"]
    assert not host.lifecycle(slot, {"action": "reconcile"})["schedule"]["enabled"]


def test_active_instance_not_reconfigured(slot, prepared):
    import fcntl

    root = Path(slot["worker_config"]["state_root"])
    root.mkdir(parents=True)
    with (root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = host.lifecycle(slot, {"action": "reconcile"})
        assert result["status"] == "waiting_for_active_run"
    assert prepared["prepared"] == 0


def test_pause_before_install_cannot_activate(slot, prepared):
    assert host.lifecycle(slot, {"action": "pause"})["status"] == "new"
    with pytest.raises(ValueError, match="Reconcile"):
        host.lifecycle(slot, {"action": "resume"})


def test_old_worker_release_cannot_bypass_host_admission(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/codex_von_worker.py").write_text("OLD_WORKER = True")
    with pytest.raises(ValueError, match="does not support"):
        host.verify_instance_release(tmp_path)


def test_host_rejects_account_and_selector_changes(slot, prepared, tmp_path):
    slot["worker_config"]["organisation_id"] = "#V#wrong"
    with pytest.raises(ValueError, match="selector"):
        host.lifecycle(slot, {"action": "reconcile"})
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(slot))
    path.chmod(0o600)
    with pytest.raises(PermissionError, match="account"):
        host.binding(path)


def test_tool_reconcile_uses_canonical_identity_and_host_receipts(
    actor, slot, prepared, monkeypatch
):
    from src.backend.integrations.internal_mcp.coding_agent_instance_tools import (
        _instances,
    )
    from src.backend.services import concept_service
    from src.backend.services import (
        organisation_membership_governance_service as governance,
    )
    from src.backend.services import organisation_membership_service as memberships

    concepts, members = {}, {"#V#owner"}
    slot["allow_identity_creation"] = True
    def lookup(concept_id):
        if concept_id not in concepts:
            raise concept_service.ConceptNotFoundError(concept_id)
        return concepts[concept_id]

    monkeypatch.setattr(concept_service, "get_concept_by_concept_id", lookup)

    def create(**kwargs):
        assert kwargs["organisation_concept_id"] == "#V#org"
        assert kwargs["created_by_concept_id"] == "#V#owner"
        concepts[kwargs["concept_id"]] = {
            "relationships": {"is_an_instance_of": ["#V#coding_agent"]},
        }

    def membership(**kwargs):
        assert kwargs["organisation_concept_id"] == "#V#org"
        assert kwargs["role"] == "member"
        members.add(kwargs["user_concept_id"])
        return {"success": True}

    monkeypatch.setattr(concept_service, "create_concept", create)
    monkeypatch.setattr(governance, "manage_organisation_membership", membership)
    monkeypatch.setattr(
        memberships,
        "resolve_user_organisation_membership",
        lambda user, org: (
            {"role": "owner" if user == "#V#owner" else "member"}
            if org == "#V#org" and user in members
            else None
        ),
    )
    monkeypatch.setattr(
        service,
        "_host",
        lambda s, a, values: host.lifecycle(s, {"action": a, "settings": values}),
    )
    result = _instances(
        action="reconcile",
        instance_id="test-agent",
        settings={"model": "gpt-6-astra", "reasoning_effort": "high"},
    )
    assert result["status"] == "installed"
    assert result["agent_id"] == "#V#test_agent"
    assert result["desired_state"] == "paused"
    assert len(concepts) == 1
    assert (
        _instances(action="reconcile", instance_id="test-agent")["status"]
        == "installed"
    )
    assert len(concepts) == 1
    slot["reserved"] = True
    assert _instances(action="resume", instance_id="test-agent")["success"] is False


def test_systemd_units_are_reused_without_clobbering_other_units(
    slot, tmp_path, monkeypatch
):
    from scripts import codex_von_instance_schedule as schedule

    commands = []
    monkeypatch.setattr(
        schedule.subprocess, "run", lambda argv, **kw: commands.append(argv)
    )
    slot["systemd"] = {
        "unit_directory": str(tmp_path / "units"),
        "memory_max_bytes": 4 * 1024**3,
    }
    schedule.install(slot)
    schedule.install(slot)
    files = sorted((tmp_path / "units").iterdir())
    assert len(files) == 2
    service_unit = next(p for p in files if p.suffix == ".service")
    assert "MemoryMax=4294967296" in service_unit.read_text()
    assert "releases/current/scripts/codex_von_worker.py" in service_unit.read_text()
    assert all(command[-1] == "daemon-reload" for command in commands)
    service_unit.write_text("unrelated unit")
    with pytest.raises(PermissionError, match="unenrolled"):
        schedule.install(slot)
