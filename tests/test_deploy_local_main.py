from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.deploy_local_main import (
    DeploymentError,
    _health_error,
    _matching_von_process_root,
    _prepare_primary,
    _prepare_runtime,
    _reconcile_startup_seed_materialisations,
    _stop_verified_predecessor,
    deploy,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        text=True,
    ).strip()


def _setup_repositories(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    primary = tmp_path / "Von"
    runtime = tmp_path / "Von-runtime-main"

    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True)
    _git(seed, "config", "user.email", "tests@example.invalid")
    _git(seed, "config", "user.name", "Von Tests")
    (seed / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "tracked.txt")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    subprocess.run(["git", "clone", str(remote), str(primary)], check=True)
    _git(primary, "worktree", "add", "-b", "runtime/main", str(runtime), "HEAD")
    return remote, seed, primary, runtime


def test_prepare_fast_forwards_primary_and_detaches_runtime(tmp_path: Path) -> None:
    _, seed, primary, runtime = _setup_repositories(tmp_path)
    (seed / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(seed, "add", "tracked.txt")
    _git(seed, "commit", "-m", "second")
    _git(seed, "push", "origin", "main")
    target = _git(seed, "rev-parse", "HEAD")

    prepared = _prepare_primary(primary.resolve(), remote="origin", branch="main")
    _prepare_runtime(primary.resolve(), runtime.resolve(), prepared)

    assert prepared == target
    assert _git(primary, "rev-parse", "HEAD") == target
    assert _git(runtime, "rev-parse", "HEAD") == target
    detached = subprocess.run(
        ["git", "-C", str(runtime), "symbolic-ref", "-q", "HEAD"],
        check=False,
    )
    assert detached.returncode != 0


def test_prepare_refuses_dirty_runtime_without_changing_its_branch(
    tmp_path: Path,
) -> None:
    _, _, primary, runtime = _setup_repositories(tmp_path)
    (runtime / "tracked.txt").write_text("local work\n", encoding="utf-8")
    original_commit = _git(runtime, "rev-parse", "HEAD")

    target = _prepare_primary(primary.resolve(), remote="origin", branch="main")
    with pytest.raises(DeploymentError, match="runtime worktree is not clean"):
        _prepare_runtime(primary.resolve(), runtime.resolve(), target)

    assert _git(runtime, "rev-parse", "HEAD") == original_commit
    assert _git(runtime, "symbolic-ref", "--short", "HEAD") == "runtime/main"


def test_prepare_refuses_primary_that_is_not_main(tmp_path: Path) -> None:
    _, _, primary, _ = _setup_repositories(tmp_path)
    _git(primary, "switch", "-c", "feature")

    with pytest.raises(DeploymentError, match="primary worktree must be on main"):
        _prepare_primary(primary.resolve(), remote="origin", branch="main")


def test_prepare_allows_registered_nested_worktree(tmp_path: Path) -> None:
    _, _, primary, _ = _setup_repositories(tmp_path)
    nested_worktree = primary / ".worktrees" / "feature"
    _git(
        primary,
        "worktree",
        "add",
        "-b",
        "feature/nested",
        str(nested_worktree),
        "HEAD",
    )
    (nested_worktree / "tracked.txt").write_text(
        "uncommitted feature work\n",
        encoding="utf-8",
    )

    prepared = _prepare_primary(primary.resolve(), remote="origin", branch="main")

    assert prepared == _git(primary, "rev-parse", "HEAD")
    assert (nested_worktree / "tracked.txt").read_text(encoding="utf-8") == (
        "uncommitted feature work\n"
    )


def test_prepare_still_refuses_other_untracked_files_beside_nested_worktree(
    tmp_path: Path,
) -> None:
    _, _, primary, _ = _setup_repositories(tmp_path)
    nested_worktree = primary / ".worktrees" / "feature"
    _git(
        primary,
        "worktree",
        "add",
        "-b",
        "feature/nested",
        str(nested_worktree),
        "HEAD",
    )
    (primary / "ordinary-untracked.txt").write_text("do not ignore me\n")

    with pytest.raises(DeploymentError, match="ordinary-untracked.txt"):
        _prepare_primary(primary.resolve(), remote="origin", branch="main")


def test_health_verification_requires_exact_clean_durable_build() -> None:
    commit = "a" * 40
    payload = {
        "status": "healthy",
        "version_details": {"git_commit": commit, "git_dirty": False},
        "runtime_authority": {
            "durable_workflows": {
                "database_connected": True,
                "worker_running": True,
                "scheduler_running": True,
            },
            "startup_seed_materialisations": {
                "ready": True,
                "state": "ready",
            },
        },
    }
    assert _health_error(payload, commit) is None

    dirty = json.loads(json.dumps(payload))
    dirty["version_details"]["git_dirty"] = True
    assert _health_error(dirty, commit) == "running git_dirty=True"

    wrong_commit = json.loads(json.dumps(payload))
    wrong_commit["version_details"]["git_commit"] = "b" * 40
    assert "running commit=" in (_health_error(wrong_commit, commit) or "")

    no_scheduler = json.loads(json.dumps(payload))
    no_scheduler["runtime_authority"]["durable_workflows"][
        "scheduler_running"
    ] = False
    assert _health_error(no_scheduler, commit) == (
        "durable workflow readiness false: scheduler_running"
    )

    stale_seed = json.loads(json.dumps(payload))
    stale_seed["runtime_authority"]["startup_seed_materialisations"] = {
        "ready": False,
        "state": "unavailable",
    }
    assert _health_error(stale_seed, commit) == (
        "startup seed materialisation readiness false: 'unavailable'"
    )


def test_runtime_reconciliation_uses_target_checkout_and_reads_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "Von-runtime-main"
    script = runtime / "scripts" / "reconcile_startup_seed_materialisations.py"
    script.parent.mkdir(parents=True)
    script.write_text("# target release command\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def _record_run(args, **kwargs):
        calls.append({"args": list(args), **kwargs})
        return subprocess.CompletedProcess(
            args,
            0,
            "diagnostic output\n"
            "VON_STARTUP_SEED_RECONCILIATION_RECEIPT="
            + json.dumps({"success": True, "state": "ready"}),
            "",
        )

    monkeypatch.setattr("scripts.deploy_local_main._run", _record_run)

    report = _reconcile_startup_seed_materialisations(runtime)

    assert report == {"success": True, "state": "ready"}
    assert calls[0]["args"][1] == script
    assert calls[0]["args"][2] == "--machine-readable"
    assert calls[0]["cwd"] == runtime


def test_deploy_reconciles_target_runtime_before_server_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = (tmp_path / "Von").resolve()
    runtime = (tmp_path / "Von-runtime-main").resolve()
    primary.mkdir()
    runtime.mkdir()
    (primary / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (runtime / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    commit = "a" * 40
    events: list[str] = []

    monkeypatch.setattr(
        "scripts.deploy_local_main._prepare_primary",
        lambda *_args, **_kwargs: commit,
    )
    monkeypatch.setattr(
        "scripts.deploy_local_main._validate_runtime",
        lambda *_args, **_kwargs: events.append("validate"),
    )
    monkeypatch.setattr(
        "scripts.deploy_local_main._stop_verified_predecessor",
        lambda **_kwargs: events.append("stop_predecessor"),
    )
    monkeypatch.setattr(
        "scripts.deploy_local_main._prepare_runtime",
        lambda *_args, **_kwargs: events.append("prepare_runtime"),
    )
    monkeypatch.setattr(
        "scripts.deploy_local_main._reconcile_startup_seed_materialisations",
        lambda *_args, **_kwargs: events.append("reconcile")
        or {"success": True},
    )

    def _record_run(args, **_kwargs):
        if "start" in args:
            events.append("start")
        elif "stop" in args:
            events.append("stop_runtime")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("scripts.deploy_local_main._run", _record_run)
    monkeypatch.setattr(
        "scripts.deploy_local_main._wait_for_verified_health",
        lambda *_args, **_kwargs: {"pid": 1234},
    )
    monkeypatch.setattr(
        "scripts.deploy_local_main._verify_workers",
        lambda *_args, **_kwargs: (2345, 3456),
    )

    result = deploy(primary_root=primary, runtime_root=runtime)

    assert result.commit == commit
    assert events.index("prepare_runtime") < events.index("reconcile")
    assert events.index("reconcile") < events.index("start")


def test_matching_von_process_root_accepts_only_exact_checkout_and_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = (tmp_path / "Von").resolve()
    runtime = (tmp_path / "Von-runtime-main").resolve()

    class _Process:
        def __init__(self, _pid: int) -> None:
            pass

        def cmdline(self) -> list[str]:
            return [
                "/usr/bin/python3",
                "-u",
                str(primary / "src/workflows/von/main.py"),
                "--port",
                "5001",
            ]

        def cwd(self) -> str:
            return str(primary)

    monkeypatch.setattr("scripts.deploy_local_main.psutil.Process", _Process)

    assert _matching_von_process_root(
        pid=4242,
        expected_port=5001,
        allowed_roots=(primary, runtime),
    ) == primary

    with pytest.raises(DeploymentError, match="does not select port 5010"):
        _matching_von_process_root(
            pid=4242,
            expected_port=5010,
            allowed_roots=(primary, runtime),
        )


def test_matching_von_process_root_rejects_foreign_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = (tmp_path / "Von").resolve()
    runtime = (tmp_path / "Von-runtime-main").resolve()
    foreign = (tmp_path / "Foreign-Von").resolve()

    class _Process:
        def __init__(self, _pid: int) -> None:
            pass

        def cmdline(self) -> list[str]:
            return [
                "/usr/bin/python3",
                str(foreign / "src/workflows/von/main.py"),
                "--port",
                "5001",
            ]

        def cwd(self) -> str:
            return str(foreign)

    monkeypatch.setattr("scripts.deploy_local_main.psutil.Process", _Process)

    with pytest.raises(DeploymentError, match="allowed deployment checkout"):
        _matching_von_process_root(
            pid=4343,
            expected_port=5001,
            allowed_roots=(primary, runtime),
        )


def test_stop_verified_predecessor_uses_exact_matching_root_and_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary = (tmp_path / "Von").resolve()
    runtime = (tmp_path / "Von-runtime-main").resolve()
    launcher = primary / "run.sh"
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "scripts.deploy_local_main._read_health",
        lambda _url: {"status": "healthy", "pid": 4444},
    )
    monkeypatch.setattr(
        "scripts.deploy_local_main._matching_von_process_root",
        lambda **_kwargs: primary,
    )

    def _record_run(args, **kwargs):
        calls.append({"args": list(args), **kwargs})
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("scripts.deploy_local_main._run", _record_run)

    def _missing_process(pid: int):
        raise __import__("psutil").NoSuchProcess(pid)

    monkeypatch.setattr(
        "scripts.deploy_local_main.psutil.Process",
        _missing_process,
    )

    assert _stop_verified_predecessor(
        primary_root=primary,
        runtime_root=runtime,
        current_launcher=launcher,
        health_url="http://127.0.0.1:5001/health",
    ) == 4444
    assert calls[0]["args"] == [
        "bash",
        launcher,
        "stop",
        "4444",
        "-NoBrowser",
    ]
    assert calls[0]["cwd"] == primary
    assert calls[0]["capture_output"] is False
    assert calls[0]["env"]["VON_LAUNCHER_ROOT"] == str(primary)


def test_run_sh_detached_worker_helper_starts_a_new_session(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    stdout_path = tmp_path / "worker.out"
    stderr_path = tmp_path / "worker.err"
    command = f"""
set -euo pipefail
cd {str(Path(__file__).resolve().parents[1])!r}
. ./run.sh help -NoBackupMigrate >/dev/null
start_python_worker_detached \
  "$(command -v python3)" \
  {str(worker)!r} \
  {str(stdout_path)!r} \
  {str(stderr_path)!r}
""".strip()
    result = subprocess.run(
        ["bash", "-c", command],
        text=True,
        capture_output=True,
        check=True,
    )
    pid = int(result.stdout.strip())
    try:
        os.kill(pid, 0)
        assert os.getsid(pid) == pid
    finally:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
