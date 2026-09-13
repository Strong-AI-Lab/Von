"""Task-authorised Android capability on an enrolled instance's test host.

The existing operator instance registry binds the route. The authenticated
agent (or its delegator) must still own a live Michael/owner-created assignment.
No caller-supplied actor, organisation, host command or SDK path is accepted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess

from . import coding_agent_instance_service as instances
from . import task_management_service as tasks


def authorised_slot(instance_id, task_id):
    from ..security.access_control import get_effective_organisation_concept_id
    from .organisation_membership_governance_service import _trusted_actor
    from .organisation_membership_service import resolve_user_organisation_membership

    actor, error = _trusted_actor()
    if error:
        raise PermissionError(error["error_code"])
    org = get_effective_organisation_concept_id()
    slot = instances.load_registry()["instances"].get(instance_id)
    if (
        not slot
        or slot["organisation_id"] != org
        or actor not in {slot["agent_id"], slot["delegator_id"]}
    ):
        raise PermissionError("No enrolled Android capability in this actor scope")
    if not resolve_user_organisation_membership(actor, org):
        raise PermissionError("Current organisation membership required")
    task = tasks.get_task(task_id)
    if (
        not task
        or task["organisation_concept_id"] != org
        or task["assignee_concept_id"] != slot["agent_id"]
        or task["created_by_concept_id"] != slot["delegator_id"]
    ):
        raise PermissionError("Current delegator-created task assignment required")
    return actor, slot, task


def coding_agent_android(
    *,
    instance_id,
    task_id,
    action="discover",
    run_id=None,
    x=None,
    y=None,
    text=None,
    fixture=None,
):
    actor, slot, task = authorised_slot(instance_id, task_id)
    actions = {
        "discover",
        "start",
        "status",
        "stop",
        "screenshot",
        "report",
        "tap",
        "text",
        "open_fixture",
    }
    if action not in actions:
        raise ValueError("Unsupported Android action")
    if action not in {"status", "stop"} and task["status"] not in {
        "pending",
        "in_progress",
    }:
        raise PermissionError("Task is not executable")
    capability = slot.get("android")
    if not capability:
        return {
            "status": "unavailable",
            "reason": "No optional Android test host enrolled",
        }
    request = {"action": action, "task_id": task_id, "run_id": run_id}
    for key, value in (("x", x), ("y", y), ("text", text), ("fixture", fixture)):
        if value is not None:
            request[key] = value
    argv = capability["provisioner_command"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(v, str) for v in argv)
    ):
        raise ValueError("Invalid fixed Android host command")
    process = subprocess.run(
        argv, input=json.dumps(request), text=True, capture_output=True
    )
    if process.returncode:
        raise RuntimeError(
            "Android host operation failed; inspect private operator log"
        )
    result = json.loads(process.stdout)
    for key in ("instance_id", "agent_id", "delegator_id", "organisation_id"):
        if result.get(key) != slot[key]:
            raise RuntimeError("Android receipt identity mismatch")
    for key in ("host", "unix_account"):
        if result.get(key) != capability[key]:
            raise RuntimeError("Android receipt test-host mismatch")
    if action != "discover" and (
        result.get("task_id") != task_id or result.get("run_id") != run_id
    ):
        raise RuntimeError("Android receipt task/run mismatch")
    artifact = result.pop("artifact", None)
    if artifact:
        raw = base64.b64decode(artifact["data_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise RuntimeError("Android evidence checksum mismatch")
        # Recheck assignment after transport and before publishing private bytes.
        authorised_slot(instance_id, task_id)
        result["attachment"] = tasks.add_task_attachment_bytes(
            task_id,
            data=raw,
            filename=artifact["filename"],
            actor_concept_id=actor,
            media_type=artifact["media_type"],
            note=json.dumps(
                {
                    "producer": slot["agent_id"],
                    "run_id": run_id,
                    "host": result["host"],
                    "sha256": artifact["sha256"],
                    "environment": result.get("environment"),
                    "coverage": "native_android_emulator",
                }
            ),
        )
    return result
