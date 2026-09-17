"""Existing coding controller services one task-bound Android request file.

The coding child writes a request in its workspace; credentials remain in the
controller. Retained pending intents never replay uncertain native gestures.
"""

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    from .codex_von_worker import write_json
except ImportError:
    from codex_von_worker import write_json


def prepare(config, state, worktree):
    if not config.get("android_instance_id"):
        return None
    root = Path(worktree) / ".run" / ("android-" + state["attempt"])
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def mailbox(root):
    # Walk workspace/.run/mailbox without following child-controlled symlinks.
    fd = os.open(root.parents[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in (root.parent.name, root.name):
            following = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = following
        yield fd
    finally:
        os.close(fd)


def respond(root, result):
    with mailbox(root) as directory:
        temporary = ".response-" + uuid.uuid4().hex
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream)
        os.rename(
            temporary, "response.json", src_dir_fd=directory, dst_dir_fd=directory
        )


def poll(config, state, root, receipts):
    with mailbox(root) as directory:
        try:
            fd = os.open(
                "request.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return
        with os.fdopen(fd, "rb") as stream:
            import stat

            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Android request must be a regular file")
            raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Android request too large")
    value = json.loads(raw)
    key = hashlib.sha256(raw).hexdigest()
    receipt_path = receipts / (key + ".json")
    if receipt_path.exists():
        result = json.loads(receipt_path.read_text())
        respond(root, result)
        return
    # Persist exact intent before effects. Controller interruption cannot replay
    # taps or text. The agent can inspect status and request explicit recovery.
    result = {
        "request_sha256": key,
        "status": "effect_uncertain",
        "reason": "Intent retained; inspect session before issuing another gesture.",
    }
    write_json(receipt_path, result)
    try:
        if not isinstance(value, dict) or set(value) - {
            "action",
            "x",
            "y",
            "text",
            "fixture",
            "request_id",
        }:
            raise ValueError("Only native operation fields are accepted")
        from src.backend.services.coding_agent_android_service import (
            coding_agent_android,
        )

        args = {k: v for k, v in value.items() if k != "request_id"}
        result = {
            "request_sha256": key,
            "result": coding_agent_android(
                instance_id=config["android_instance_id"],
                task_id=state["task_id"],
                run_id=state["attempt"],
                **args,
            ),
        }
    except Exception as exc:
        result.update(status="failed_or_uncertain", error=type(exc).__name__)
    write_json(receipt_path, result)
    respond(root, result)


def stop(config, state):
    from src.backend.services.coding_agent_android_service import coding_agent_android

    return coding_agent_android(
        instance_id=config["android_instance_id"],
        task_id=state["task_id"],
        run_id=state["attempt"],
        action="stop",
    )
