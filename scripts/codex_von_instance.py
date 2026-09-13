"""Trusted host provisioner for one operator-enrolled coding-agent slot.

Use a fixed command/account/config binding, with the request on stdin. A caller
cannot choose a config path, account, command or credential in its request.
The operator prepares the Unix account, Python/Codex authentication, repository,
service unit and shared capacity lock. No account creation or general shell.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path

try:
    from . import codex_von_instance_schedule as schedule
    from . import codex_von_release as release
    from .codex_von_worker import (
        bind_backend_root,
        resolve_execution_settings,
        write_json,
    )
except ImportError:
    import codex_von_instance_schedule as schedule
    import codex_von_release as release
    from codex_von_worker import (
        bind_backend_root,
        resolve_execution_settings,
        write_json,
    )


def run(argv):
    return subprocess.run(
        argv, check=True, text=True, capture_output=True
    ).stdout.strip()


def schedule_action(spec, action):
    if "systemd" in spec:
        return schedule.control(spec, action)
    return run(spec["schedule_commands"][action])


def verify_instance_release(target):
    """Reject older workers that would silently ignore aggregate admission."""
    target = Path(target)
    worker = ast.parse((target / "scripts/codex_von_worker.py").read_text())
    supported = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(name, ast.Name) and name.id == "INSTANCE_PROTOCOL_VERSION"
            for name in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value == 1
        for node in worker.body
    )
    if not supported or not (target / "scripts/codex_von_capacity.py").is_file():
        raise ValueError("Release does not support instance pause and host admission")


def binding(path):
    path = Path(path)
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o022
        or info.st_uid not in {0, os.geteuid()}
    ):
        raise PermissionError("Host instance binding is not operator-owned")
    value = json.loads(path.read_text())
    if value["unix_account"] != pwd.getpwuid(os.geteuid()).pw_name:
        raise PermissionError("Prepared Unix account does not match")
    if value["host"] != socket.gethostname():
        raise PermissionError("Prepared host does not match")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value["instance_id"]):
        raise ValueError("Invalid enrolled instance ID")
    return value


def preflight(spec):
    config = spec["worker_config"]
    for key in ("agent_id", "delegator_id", "organisation_id"):
        if config[key] != spec[key]:
            raise ValueError("Worker selector differs from enrolled binding")
    if config.get("report_recipient_id", spec["delegator_id"]) != spec["delegator_id"]:
        raise ValueError("Report recipient must be the authorised delegator")
    root = Path(spec["instance_root"]).resolve()
    if Path(config["state_root"]).resolve() != root / "state":
        raise ValueError("Instance state must be private to its enrolled root")
    if not Path(config["source_repo"]).is_dir():
        raise ValueError("Repository is not prepared")
    for field in ("codex_command", "python_environment"):
        if not Path(config[field]).exists():
            raise ValueError("Codex launcher or Python environment is not prepared")
    capacity = config["host_capacity"]
    if not Path(capacity["lock_path"]).is_file():
        raise ValueError("Host-wide capacity lock is not prepared")
    if not spec.get("capacity_enrolment_verified"):
        raise ValueError("Operator must enrol all host consumers in shared admission")
    if not spec.get("resource_limits_verified"):
        raise ValueError("Operator must prepare the service resource limits")
    if not re.fullmatch(r"[0-9a-f]{40}", spec["release_commit"]):
        raise ValueError("A reviewed full release SHA is required")
    if "systemd" not in spec:
        for action in ("enable", "disable", "status"):
            argv = spec["schedule_commands"][action]
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(x, str) for x in argv)
            ):
                raise ValueError("Invalid fixed schedule command")
    resolve_execution_settings(config, {})
    return root


def lifecycle(spec, request):
    if set(request) - {"action", "settings"}:
        raise ValueError("Unrecognised host request fields")
    action = request["action"]
    if action not in {"preflight", "reconcile", "resume", "pause", "status"}:
        raise ValueError("Unknown instance action")
    settings = request.get("settings", {})
    if not isinstance(settings, dict) or set(settings) - {"model", "reasoning_effort"}:
        raise ValueError("Invalid execution settings")
    if settings and action not in {"preflight", "reconcile"}:
        raise ValueError("Settings are only accepted for reconciliation")
    root = preflight(spec)
    identity = {
        key: spec[key]
        for key in (
            "instance_id",
            "agent_id",
            "delegator_id",
            "organisation_id",
            "host",
            "unix_account",
        )
    }
    if action == "preflight":
        return {**identity, "status": "prepared_host"}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / "provision.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = root / "instance.json"
        state = (
            json.loads(state_path.read_text())
            if state_path.exists()
            else {
                **identity,
                "desired_state": "paused",
                "phase": "new",
            }
        )
        if any(state.get(key) != value for key, value in identity.items()):
            raise ValueError("Retained instance belongs to another binding")
        if action == "reconcile":
            config = dict(spec["worker_config"])
            config["agent_name"] = spec["agent_name"]
            config["instance_state_path"] = str(state_path)
            settings = {**state.get("execution_overrides", {}), **settings}
            for key, dest in (
                ("model", "model"),
                ("reasoning_effort", "model_reasoning_effort"),
            ):
                if key in settings:
                    config[dest] = settings[key]
            selected = resolve_execution_settings(config, {})
            digest = hashlib.sha256(
                json.dumps(config, sort_keys=True).encode()
            ).hexdigest()
            # Never change configuration under an active run. Pausing the timer
            # does not kill a running consumer, and the worker lock is independent.
            state_root = Path(config["state_root"])
            state_root.mkdir(parents=True, exist_ok=True)
            with (state_root / "worker.lock").open("a") as worker_lock:
                try:
                    fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {**identity, "status": "waiting_for_active_run"}
                state.update(
                    phase="installing",
                    selected_release=spec["release_commit"],
                    selected_settings=selected,
                    execution_overrides=settings,
                )
                write_json(state_path, state)
                target = release.prepare(
                    config["source_repo"], root / "releases", spec["release_commit"]
                )
                verify_instance_release(target)
                write_json(root / "worker.json", config)
                release.activate(root / "releases", target)
                if "systemd" in spec:
                    schedule.install(spec)
                state.update(phase="installed", config_sha256=digest)
                write_json(state_path, state)
        elif action == "resume":
            if state.get("phase") not in {"installed", "scheduled"}:
                raise ValueError("Reconcile the instance before resume")
            release.current(root / "releases")
            if not (root / "worker.json").is_file():
                raise ValueError("Instance configuration is not installed")
            # Retain intent before schedule effects; retry reconciles a fixed unit.
            state["desired_state"] = "running"
            write_json(state_path, state)
        elif action == "pause":
            state["desired_state"] = "paused"
            write_json(state_path, state)
        if action != "status":
            command = "enable" if state["desired_state"] == "running" else "disable"
            schedule_action(spec, command)
            state["phase"] = (
                "scheduled"
                if command == "enable"
                else ("installed" if (root / "worker.json").is_file() else "new")
            )
            write_json(state_path, state)
        schedule_state = json.loads(schedule_action(spec, "status"))
        if not isinstance(schedule_state, dict) or not isinstance(
            schedule_state.get("enabled"), bool
        ):
            raise TypeError("Invalid schedule status receipt")
        if action != "status" and schedule_state["enabled"] != (
            state["desired_state"] == "running"
        ):
            raise RuntimeError("Schedule read-back does not match retained intent")
        runtime_path = root / "state/runtime.json"
        runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
        runtime = {
            key: runtime[key]
            for key in (
                "observed_at",
                "pid",
                "commit",
                "backend_root",
                "worker_script",
                "task_service_module",
            )
            if key in runtime
        }
        schedule_state = {
            key: schedule_state[key]
            for key in ("enabled", "active", "unit")
            if key in schedule_state
        }
        # Explicitly separate scheduling from a worker's actual runtime receipt.
        return {
            **identity,
            "status": state["phase"],
            "desired_state": state["desired_state"],
            "selected_release": state.get("selected_release"),
            "selected_settings": state.get("selected_settings"),
            "schedule": schedule_state,
            "runtime_observed": runtime,
            "task_and_reply_readiness": "not_established_by_provisioning",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    bind_backend_root(Path(__file__).resolve().parents[1])
    print(json.dumps(lifecycle(binding(args.binding), json.load(sys.stdin))))


if __name__ == "__main__":
    main()
