#!/usr/bin/env python3
"""Operator-bound deployment and code recovery for the external Codex worker.

Paths and endpoints come only from the operator's config. The coding run may
request a merged revision, never a command, host, credential or checkout path.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

try:
    from . import deploy_local_main as runtime
    from . import codex_von_release as releases
except ImportError:
    import deploy_local_main as runtime
    import codex_von_release as releases


@contextmanager
def worker_lock(config, inherited_fd=None):
    """Serialise host activation with the existing controller, including its child."""
    path = Path(config["state_root"]) / "worker.lock"
    with path.open("a") as lock:
        fd = lock.fileno() if inherited_fd is None else inherited_fd
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (
            path.stat().st_dev,
            path.stat().st_ino,
        ):
            raise ValueError("Inherited descriptor is not the configured worker lock")
        # A passed descriptor shares the controller's lock; never unlock it here.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def select_worker(config, receipt):
    if not config.get("worker_release_root"):
        return
    root = config["worker_release_root"]
    if not receipt.get("previous_worker_release"):
        raise runtime.DeploymentError("Worker activation lacks its rollback selection")
    target = releases.prepare(config["primary_root"], root, receipt["requested_commit"])
    receipt["worker_activation"] = releases.activate(root, target)


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def verify(config, commit):
    timeout = config.get("health_timeout_seconds", 960)
    local = runtime._wait_for_verified_health(config["health_url"], commit, timeout)
    headers = None
    if config.get("public_headers_file"):
        headers = json.loads(Path(config["public_headers_file"]).read_text())
        if set(headers) != {"CF-Access-Client-Id", "CF-Access-Client-Secret"}:
            raise ValueError("Unexpected public health authentication headers")
    public = runtime._wait_for_verified_health(
        config["public_health_url"], commit, timeout, headers=headers
    )
    return {"local_pid": local["pid"], "public_pid": public["pid"], "commit": commit}


def recover(config, previous_commit):
    primary = Path(config["primary_root"]).resolve()
    checkout = Path(config["runtime_root"]).resolve()
    # Stop only the exact deployment port using the canonical launcher. Changing
    # code while a candidate process is still alive would invalidate read-back.
    launcher = checkout / "run.sh"
    port = str(urlparse(config["health_url"]).port)
    runtime._run(
        ["bash", launcher, "stop", "-Port", port, "-NoBrowser"],
        cwd=checkout,
        capture_output=False,
    )
    runtime._prepare_runtime(primary, checkout, previous_commit)
    runtime._reconcile_startup_seed_materialisations(checkout)
    runtime._run(
        [
            "bash",
            launcher,
            "restart",
            "-Port",
            port,
            "-NoBrowser",
            "-HealthTimeoutSec",
            str(config.get("health_timeout_seconds", 960)),
        ],
        cwd=checkout,
        capture_output=False,
    )
    runtime._verify_workers(checkout)
    # An older release can predate machine health authentication. Restore and
    # verify its local service even if that old public route rejects this token.
    local = runtime._wait_for_verified_health(
        config["health_url"], previous_commit, config.get("health_timeout_seconds", 960)
    )
    try:
        return verify(config, previous_commit)
    except runtime.HealthAccessError:
        return {
            "commit": previous_commit,
            "local_pid": local["pid"],
            "public_verification": "authentication_unavailable_for_previous_release",
        }


def execute(config, commit, receipt_path, *, recover_only=False):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Deployment requires a full Git commit SHA")
    primary = Path(config["primary_root"]).resolve()
    checkout = Path(config["runtime_root"]).resolve()
    runtime._validate_runtime(primary, checkout)
    receipt_path = Path(receipt_path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with (Path(config["state_root"]) / "deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = (
            json.loads(receipt_path.read_text()) if receipt_path.exists() else None
        )
        if receipt:
            if receipt["requested_commit"] != commit:
                raise ValueError("Deployment receipt belongs to another revision")
            if receipt["status"] in {"deployed", "rolled_back", "recovery_failed"}:
                return receipt
        else:
            if recover_only:
                raise ValueError("Recovery requires an existing deployment receipt")
            # Exact target and healthy predecessor are preconditions: a stale
            # request must not stop a healthy server or fast-forward its checkout.
            runtime._run(["git", "-C", primary, "fetch", "origin", "main"])
            if runtime._git(primary, "rev-parse", "origin/main") != commit:
                raise runtime.DeploymentError(
                    "Requested revision is no longer origin/main"
                )
            prior = runtime._read_health(config["health_url"])
            previous_commit = prior["version_details"]["git_commit"]
            error = runtime._health_error(prior, previous_commit)
            if error:
                raise runtime.DeploymentError("Predecessor is not ready: " + error)
            if runtime._git(primary, "show", f"{commit}:pdm.lock") != runtime._git(
                primary, "show", f"{previous_commit}:pdm.lock"
            ):
                raise runtime.DeploymentError(
                    "Dependency lock changed; prepare a recoverable environment before deployment"
                )
            receipt = {
                "status": "started",
                "requested_commit": commit,
                "previous_commit": previous_commit,
            }
            if config.get("worker_release_root"):
                previous_worker = releases.current(config["worker_release_root"])
                releases.verify(
                    previous_worker, runtime._git(previous_worker, "rev-parse", "HEAD")
                )
                # Persist rollback before the pointer can change. Preparing a new
                # immutable checkout cannot affect the loaded controller/backend.
                receipt["previous_worker_release"] = str(previous_worker)
                releases.prepare(primary, config["worker_release_root"], commit)
            save(receipt_path, receipt)
            try:
                runtime.deploy(
                    primary_root=primary,
                    runtime_root=checkout,
                    health_url=config["health_url"],
                    health_timeout_seconds=config.get("health_timeout_seconds", 960),
                    expected_commit=commit,
                )
                verification = verify(config, commit)
                select_worker(config, receipt)
                receipt.update(status="deployed", verification=verification)
                save(receipt_path, receipt)
                return receipt
            except (runtime.DeploymentError, OSError, ValueError, KeyError) as exc:
                receipt["failure_type"] = type(exc).__name__
                save(receipt_path, receipt)
        # An interrupted controller never blindly repeats a restart. Reconcile
        # the target first; if it is not serving, restore the recorded predecessor.
        try:
            current = runtime._read_health(config["health_url"])
            if not runtime._health_error(current, commit):
                verification = verify(config, commit)
                if recover_only and config.get("worker_release_root"):
                    selected = releases.current(config["worker_release_root"])
                    if runtime._git(selected, "rev-parse", "HEAD") != commit:
                        raise runtime.DeploymentError(
                            "Revoked deployment cannot start a new worker activation"
                        )
                select_worker(config, receipt)
                receipt.update(status="deployed", verification=verification)
                save(receipt_path, receipt)
                return receipt
        except (runtime.DeploymentError, OSError, ValueError, KeyError) as exc:
            receipt["reconciliation_failure_type"] = type(exc).__name__
        if receipt.get("previous_worker_release"):
            try:
                receipt["worker_activation"] = releases.activate(
                    config["worker_release_root"], receipt["previous_worker_release"]
                )
            except (runtime.DeploymentError, OSError, ValueError, KeyError) as exc:
                receipt["worker_recovery_failure_type"] = type(exc).__name__
        try:
            receipt.update(
                status="rolled_back",
                verification=recover(config, receipt["previous_commit"]),
            )
        except (runtime.DeploymentError, OSError, ValueError, KeyError) as exc:
            receipt.update(
                status="recovery_failed", recovery_failure_type=type(exc).__name__
            )
        if receipt.get("worker_recovery_failure_type"):
            receipt["status"] = "recovery_failed"
        save(receipt_path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--worker-lock-fd", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    if config.get("public_headers_file"):
        headers = json.loads(Path(config["public_headers_file"]).read_text())
        os.environ["VON_CLOUDFLARE_HEALTH_SERVICE_IDS"] = headers["CF-Access-Client-Id"]
    # Controller-only event suppression must not change the production service.
    for key in (
        "VON_EVENT_WORKFLOW_INTEGRATION_ENABLE",
        "VON_DURABLE_WORKFLOWS_ENABLE",
    ):
        os.environ.pop(key, None)
    with worker_lock(config, args.worker_lock_fd):
        result = execute(
            config, args.commit, Path(args.receipt), recover_only=args.recover_only
        )
    print(json.dumps(result), flush=True)
    return 0 if result["status"] == "deployed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
