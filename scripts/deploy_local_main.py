#!/usr/bin/env python3
"""Deploy origin/main into the dedicated local Von runtime worktree."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


class DeploymentError(RuntimeError):
    """A deployment precondition or verification failed."""


@dataclass(frozen=True)
class DeploymentResult:
    primary_root: Path
    runtime_root: Path
    commit: str
    server_pid: int
    rag_worker_pid: int
    concept_index_worker_pid: int


def _run(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture_output: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [os.fspath(value) for value in args]
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=capture_output,
        env=env,
        check=False,
    )
    if check and result.returncode != 0:
        detail = ((result.stderr or result.stdout) if capture_output else "").strip()
        if detail:
            raise DeploymentError(f"Command failed: {' '.join(command)}: {detail}")
        raise DeploymentError(
            f"Command failed with exit {result.returncode}: {' '.join(command)}"
        )
    return result


def _git(root: Path, *args: str, check: bool = True) -> str:
    return _run(["git", "-C", root, *args], check=check).stdout.strip()


def _require_clean(root: Path, label: str) -> None:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        first_entries = ", ".join(status.splitlines()[:5])
        raise DeploymentError(
            f"Refusing to deploy: {label} worktree is not clean ({first_entries})"
        )


def _git_common_dir(root: Path) -> Path:
    raw = Path(_git(root, "rev-parse", "--git-common-dir"))
    if not raw.is_absolute():
        raw = root / raw
    return raw.resolve()


def _prepare_primary(
    primary_root: Path,
    *,
    remote: str,
    branch: str,
) -> str:
    actual_root = Path(_git(primary_root, "rev-parse", "--show-toplevel")).resolve()
    if actual_root != primary_root:
        raise DeploymentError(
            f"Primary path resolves to {actual_root}, expected {primary_root}"
        )

    current_branch = _git(primary_root, "symbolic-ref", "--short", "HEAD", check=False)
    if current_branch != branch:
        shown = current_branch or "detached HEAD"
        raise DeploymentError(
            f"Refusing to deploy: primary worktree must be on {branch}, found {shown}"
        )
    _require_clean(primary_root, "primary")

    _run(["git", "-C", primary_root, "fetch", remote, branch])
    local_commit = _git(primary_root, "rev-parse", "HEAD")
    remote_ref = f"{remote}/{branch}"
    target_commit = _git(primary_root, "rev-parse", remote_ref)
    if local_commit != target_commit:
        merge_base = _git(primary_root, "merge-base", local_commit, target_commit)
        if merge_base != local_commit:
            raise DeploymentError(
                "Refusing to deploy: primary branch is ahead of or diverged from "
                f"{remote_ref} (local={local_commit}, remote={target_commit})"
            )
        _run(["git", "-C", primary_root, "merge", "--ff-only", remote_ref])
        local_commit = _git(primary_root, "rev-parse", "HEAD")

    if local_commit != target_commit:
        raise DeploymentError(
            f"Primary checkout did not reach {remote_ref} ({local_commit} != {target_commit})"
        )
    _require_clean(primary_root, "primary")
    return target_commit


def _validate_runtime(primary_root: Path, runtime_root: Path) -> None:
    if runtime_root == primary_root:
        raise DeploymentError("Runtime worktree must be distinct from the primary checkout")
    if not runtime_root.is_dir():
        raise DeploymentError(
            "Dedicated runtime worktree does not exist or is not a directory: "
            f"{runtime_root}"
        )
    actual_root = Path(_git(runtime_root, "rev-parse", "--show-toplevel")).resolve()
    if actual_root != runtime_root:
        raise DeploymentError(
            f"Runtime path resolves to {actual_root}, expected {runtime_root}"
        )
    if _git_common_dir(runtime_root) != _git_common_dir(primary_root):
        raise DeploymentError(
            "Runtime path is a checkout of a different Git repository: "
            f"{runtime_root}"
        )
    _require_clean(runtime_root, "runtime")


def _prepare_runtime(
    primary_root: Path,
    runtime_root: Path,
    target_commit: str,
) -> None:
    _validate_runtime(primary_root, runtime_root)
    _run(["git", "-C", runtime_root, "switch", "--detach", target_commit])

    _require_clean(runtime_root, "runtime")
    runtime_commit = _git(runtime_root, "rev-parse", "HEAD")
    if runtime_commit != target_commit:
        raise DeploymentError(
            f"Runtime checkout is at {runtime_commit}, expected {target_commit}"
        )
    if _git(runtime_root, "symbolic-ref", "-q", "HEAD", check=False):
        raise DeploymentError("Runtime checkout must be detached, but it is on a branch")


def _read_health(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"Health read failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeploymentError("Health response was not a JSON object")
    return payload


def _health_error(payload: dict[str, Any], target_commit: str) -> str | None:
    if payload.get("status") != "healthy":
        return f"status={payload.get('status')!r}"

    version = payload.get("version_details")
    if not isinstance(version, dict):
        return "version_details missing"
    if version.get("git_commit") != target_commit:
        return (
            f"running commit={version.get('git_commit')!r}, expected={target_commit!r}"
        )
    if version.get("git_dirty") is not False:
        return f"running git_dirty={version.get('git_dirty')!r}"

    authority = payload.get("runtime_authority")
    durable = authority.get("durable_workflows") if isinstance(authority, dict) else None
    if not isinstance(durable, dict):
        return "durable workflow health missing"
    required = ("database_connected", "worker_running", "scheduler_running")
    missing = [name for name in required if durable.get(name) is not True]
    if missing:
        return f"durable workflow readiness false: {', '.join(missing)}"
    return None


def _wait_for_verified_health(
    url: str,
    target_commit: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "health endpoint not read"
    while time.monotonic() < deadline:
        try:
            payload = _read_health(url)
            last_error = _health_error(payload, target_commit) or ""
            if not last_error:
                return payload
        except DeploymentError as exc:
            last_error = str(exc)
        time.sleep(1)
    raise DeploymentError(
        f"Runtime did not pass exact health verification within {timeout_seconds}s: "
        f"{last_error}"
    )


def _pid_from_file(path: Path, label: str) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DeploymentError(f"{label} PID file unavailable: {path}: {exc}") from exc
    for line in lines:
        if line.startswith("PID=") and line[4:].isdigit():
            return int(line[4:])
    raise DeploymentError(f"{label} PID file has no valid PID: {path}")


def _verify_worker(pid_file: Path, script_name: str, label: str) -> int:
    pid = _pid_from_file(pid_file, label)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise DeploymentError(f"{label} PID {pid} is not alive") from exc

    command = _run(["ps", "-o", "command=", "-p", str(pid)]).stdout.strip()
    if script_name not in command.replace("\\", "/"):
        raise DeploymentError(
            f"{label} PID {pid} does not run {script_name}: {command or '<empty>'}"
        )
    return pid


def _verify_workers(runtime_root: Path) -> tuple[int, int]:
    # A short second observation catches helpers that only survived the launcher.
    time.sleep(2)
    rag_pid = _verify_worker(
        runtime_root / ".run" / "rag_worker.pid",
        "src/backend/utilities/rag_indexing_worker.py",
        "RAG worker",
    )
    concept_pid = _verify_worker(
        runtime_root / ".run" / "concept_index_worker.pid",
        "src/backend/utilities/concept_index_worker.py",
        "concept-index worker",
    )
    return rag_pid, concept_pid


def deploy(
    *,
    primary_root: Path,
    runtime_root: Path,
    remote: str = "origin",
    branch: str = "main",
    health_url: str = "http://127.0.0.1:5001/health",
    health_timeout_seconds: int = 180,
) -> DeploymentResult:
    primary_root = primary_root.resolve()
    runtime_root = runtime_root.resolve()
    target_commit = _prepare_primary(primary_root, remote=remote, branch=branch)
    current_launcher = primary_root / "run.sh"
    if not current_launcher.is_file():
        raise DeploymentError(f"Current launcher missing: {current_launcher}")

    # Validate before stopping, then change code only while the server is down.
    _validate_runtime(primary_root, runtime_root)
    stop_environment = os.environ.copy()
    stop_environment["VON_LAUNCHER_ROOT"] = str(runtime_root)
    _run(
        ["bash", current_launcher, "stop", "-NoBrowser"],
        cwd=runtime_root,
        capture_output=False,
        env=stop_environment,
    )
    _prepare_runtime(primary_root, runtime_root, target_commit)
    launcher = runtime_root / "run.sh"
    if not launcher.is_file():
        raise DeploymentError(f"Runtime launcher missing after checkout: {launcher}")
    _run(
        [
            "bash",
            launcher,
            "start",
            "-NoBrowser",
            "-HealthTimeoutSec",
            str(health_timeout_seconds),
        ],
        cwd=runtime_root,
        capture_output=False,
    )

    health = _wait_for_verified_health(
        health_url,
        target_commit,
        health_timeout_seconds,
    )
    rag_pid, concept_pid = _verify_workers(runtime_root)
    server_pid = health.get("pid")
    if not isinstance(server_pid, int) or server_pid <= 0:
        raise DeploymentError(f"Health response has invalid server PID: {server_pid!r}")

    return DeploymentResult(
        primary_root=primary_root,
        runtime_root=runtime_root,
        commit=target_commit,
        server_pid=server_pid,
        rag_worker_pid=rag_pid,
        concept_index_worker_pid=concept_pid,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fast-forward the primary main checkout, detach the dedicated runtime "
            "worktree at the exact origin/main commit, restart Von, and verify it."
        )
    )
    parser.add_argument("--primary-root", type=Path, required=True)
    parser.add_argument("--runtime-worktree", type=Path, required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--health-url", default="http://127.0.0.1:5001/health")
    parser.add_argument("--health-timeout-seconds", type=int, default=180)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.health_timeout_seconds <= 0:
        print("deploy-main: health timeout must be positive", file=sys.stderr)
        return 2
    try:
        result = deploy(
            primary_root=args.primary_root,
            runtime_root=args.runtime_worktree,
            remote=args.remote,
            branch=args.branch,
            health_url=args.health_url,
            health_timeout_seconds=args.health_timeout_seconds,
        )
    except DeploymentError as exc:
        print(f"deploy-main: ERROR: {exc}", file=sys.stderr)
        return 1

    print("deploy-main: VERIFIED")
    print(f"  primary: {result.primary_root} ({result.commit})")
    print(f"  runtime: {result.runtime_root} (detached at {result.commit})")
    print(f"  server: PID {result.server_pid}, healthy exact build")
    print(f"  RAG worker: PID {result.rag_worker_pid}")
    print(f"  concept-index worker: PID {result.concept_index_worker_pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
