"""Coherent, versioned backend/worker installations for operator-bound deployment.

The caller owns the existing worker lock. Never overwrite a loaded release;
only the next invocation resolves the atomically replaced current symlink.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

try:
    from . import deploy_local_main as runtime
except ImportError:
    import deploy_local_main as runtime


def current(release_root):
    pointer = Path(release_root).resolve() / "current"
    if not pointer.is_symlink():
        raise runtime.DeploymentError(
            "Worker release current must be an operator-prepared symlink"
        )
    target = pointer.resolve(strict=True)
    if target.parent != pointer.parent or not target.is_dir():
        raise runtime.DeploymentError("Worker release must be inside its release root")
    return target


def verify(target, commit):
    target = Path(target).resolve()
    if runtime._git(target, "rev-parse", "HEAD") != commit:
        raise runtime.DeploymentError("Worker release revision mismatch")
    if runtime._git(target, "status", "--porcelain", "--untracked-files=no"):
        raise runtime.DeploymentError("Worker release has modified tracked files")
    for name in (
        "scripts/codex_von_worker.py",
        "scripts/codex_von_inbox.py",
        "scripts/codex_von_worker_prompt.md",
        "scripts/codex_von_inbox_prompt.md",
        "scripts/codex_von_deploy.py",
        "scripts/codex_von_release.py",
        "scripts/deploy_local_main.py",
        "src/backend/services/task_management_service.py",
    ):
        if not (target / name).is_file():
            raise runtime.DeploymentError("Incomplete worker release: " + name)
    return {
        "commit": commit,
        "backend_root": str(target),
        "worker_script": str(target / "scripts/codex_von_worker.py"),
    }


def prepare(primary, release_root, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Worker release requires a full Git SHA")
    root = Path(release_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / commit
    if not target.exists():
        # Fetch only the exact reviewed commit into independent Git metadata.
        # No environment, credentials, private config or source alternates are copied.
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root) as staging:
            checkout = Path(staging) / "checkout"
            runtime._run(["git", "init", "--quiet", checkout])
            runtime._run(
                [
                    "git",
                    "-C",
                    checkout,
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    Path(primary).resolve(),
                    commit,
                ]
            )
            runtime._run(
                ["git", "-C", checkout, "checkout", "--quiet", "--detach", commit]
            )
            verify(checkout, commit)
            checkout.rename(target)
    verify(target, commit)
    return target


def activate(release_root, target):
    root = Path(release_root).resolve()
    target = Path(target).resolve(strict=True)
    if target.parent != root:
        raise runtime.DeploymentError(
            "Worker activation target is outside release root"
        )
    commit = runtime._git(target, "rev-parse", "HEAD")
    evidence = verify(target, commit)
    # A private temporary directory avoids collisions or following an old link.
    with tempfile.TemporaryDirectory(prefix=".switch-", dir=root) as staging:
        link = Path(staging) / "current"
        link.symlink_to(target)
        link.replace(root / "current")
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if current(root) != target:
        raise runtime.DeploymentError("Worker release pointer read-back mismatch")
    return {**evidence, "status": "selected_for_next_invocation"}


def main():
    """Operator bootstrap; no polling, model execution or service restart."""
    try:
        from .codex_von_deploy import save, worker_lock
    except ImportError:
        from codex_von_deploy import save, worker_lock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Operator deployment config")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    path = Path(args.receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    with worker_lock(config):
        primary = Path(config["primary_root"]).resolve()
        runtime._run(["git", "-C", primary, "fetch", "origin", "main"])
        if runtime._git(primary, "rev-parse", "origin/main") != args.commit:
            raise runtime.DeploymentError(
                "Worker installation requires current origin/main"
            )
        root = Path(config["worker_release_root"]).resolve()
        target = prepare(primary, root, args.commit)
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt["requested_commit"] != args.commit:
                raise ValueError("Installation receipt belongs to another revision")
        else:
            receipt = {
                "status": "prepared",
                "requested_commit": args.commit,
                "previous_worker_release": (
                    str(current(root)) if (root / "current").is_symlink() else None
                ),
            }
            save(path, receipt)
        receipt.update(activate(root, target))
        save(path, receipt)
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
