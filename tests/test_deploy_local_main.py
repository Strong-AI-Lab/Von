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
    _stop_verified_predecessor,
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
            }
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
