"""Actor-bound access to operator-enrolled coding-agent instance slots.

Host/account/credential/identity bindings are operator configuration, never tool
arguments. Normal Von agent calls use the authenticated delegator's authority.
The host independently checks the slot and installs through the existing worker
release mechanism. This module never writes Vontology through a database handle.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


def load_registry():
    path = Path(os.environ["VON_CODING_AGENT_REGISTRY"])
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise PermissionError(
            "Instance registry must be operator-owned and not writable by group/other"
        )
    if info.st_uid not in {0, os.geteuid()}:
        raise PermissionError("Unexpected instance registry owner")
    registry = json.loads(path.read_text())
    slots = registry["instances"]
    # One configured consumer per represented identity, including existing
    # consumers enrolled as reserved slots. Endpoint does not create identity.
    identities = [slot["agent_id"] for slot in slots.values()]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate coding-agent identity in registry")
    return registry


def authorised_slots():
    from ..security.access_control import get_effective_organisation_concept_id
    from ..security.role_resolver import get_effective_permissions
    from .organisation_membership_governance_service import _trusted_actor
    from .organisation_membership_service import resolve_user_organisation_membership

    actor, error = _trusted_actor()
    if error:
        raise PermissionError(error["error_code"])
    organisation = get_effective_organisation_concept_id()
    membership = resolve_user_organisation_membership(actor, organisation)
    if not membership or "MANAGE_MEMBERS" not in get_effective_permissions(
        membership["role"]
    ):
        raise PermissionError(
            "Live organisation membership-management authority required"
        )
    registry = load_registry()
    slots = {
        key: slot
        for key, slot in registry["instances"].items()
        if slot["delegator_id"] == actor and slot["organisation_id"] == organisation
    }
    return actor, organisation, slots


def _host(slot, action, settings):
    # The entire argv is an operator binding. The model cannot append flags,
    # choose a host/account/path, substitute an endpoint or supply a shell.
    argv = slot["provisioner_command"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(x, str) for x in argv)
    ):
        raise ValueError("Invalid operator provisioner command")
    request = {"action": action, "settings": settings}
    process = subprocess.run(
        argv,
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        # A host error may contain credential paths or command output.
        raise RuntimeError("Host provisioner failed; inspect its private operator log")
    result = json.loads(process.stdout)
    if not isinstance(result, dict):
        raise TypeError("Invalid host receipt")
    for key in (
        "instance_id",
        "agent_id",
        "delegator_id",
        "organisation_id",
        "host",
        "unix_account",
    ):
        if result.get(key) != slot[key]:
            raise RuntimeError(
                "Host receipt does not match the authorised instance binding"
            )
    return result


def _identity(slot, actor, organisation):
    from . import concept_service
    from .organisation_membership_governance_service import (
        manage_organisation_membership,
    )
    from .organisation_membership_service import resolve_user_organisation_membership

    agent_id = slot["agent_id"]
    concept = concept_service.get_concept_by_concept_id(agent_id)
    if concept is None:
        if not slot.get("allow_identity_creation", False):
            raise PermissionError(
                "The enrolled agent identity must be prepared by its operator"
            )
        concept_service.create_concept(
            name=slot["agent_name"],
            concept_id=agent_id,
            description=slot["role"],
            parent_concept_ids=["#V#coding_agent"],
            create_as_instance=True,
            created_by_concept_id=actor,
            organisation_concept_id=organisation,
            visibility_scope_mode="organisation_general",
        )
        concept = concept_service.get_concept_by_concept_id(agent_id)
    if not concept or "#V#coding_agent" not in concept.get("relationships", {}).get(
        "is_an_instance_of", []
    ):
        raise PermissionError(
            "Enrolled identity is not a canonical coding-agent instance"
        )
    if not resolve_user_organisation_membership(agent_id, organisation):
        receipt = manage_organisation_membership(
            action="add",
            user_concept_id=agent_id,
            organisation_concept_id=organisation,
            role="member",
            request_id="coding-agent-instance:" + slot["instance_id"],
            reason="Provision operator-enrolled coding-agent instance",
        )
        if not receipt.get("success"):
            raise PermissionError("Canonical agent membership creation did not succeed")
    if not resolve_user_organisation_membership(agent_id, organisation):
        raise RuntimeError("Agent membership read-back failed")


def coding_agent_instances(*, action="list", instance_id=None, settings=None):
    """Discover or reconcile an enrolled instance without granting host access."""
    actor, organisation, slots = authorised_slots()
    if action == "list":
        return {
            "instances": [
                {
                    key: slot[key]
                    for key in (
                        "instance_id",
                        "agent_id",
                        "agent_name",
                        "role",
                        "delegator_id",
                        "organisation_id",
                        "endpoint",
                        "host",
                        "unix_account",
                    )
                }
                for slot in slots.values()
            ]
        }
    if instance_id not in slots:
        raise PermissionError(
            "Instance is not enrolled for the current actor and organisation"
        )
    if action not in {"status", "reconcile", "pause", "resume"}:
        raise ValueError("Use list, status, reconcile, pause or resume")
    slot = slots[instance_id]
    if slot["instance_id"] != instance_id or slot.get("reserved"):
        raise PermissionError(
            "Existing consumer is reserved; use its own operator route"
        )
    values = settings or {}
    if not isinstance(values, dict) or set(values) - {"model", "reasoning_effort"}:
        raise ValueError("Only model and reasoning_effort are selectable settings")
    if action != "reconcile" and values:
        raise ValueError("Settings can only change during reconcile")
    from ..utils.task_execution_preferences import normalise_task_execution_preference

    for key, value in values.items():
        normalised = normalise_task_execution_preference(
            value, field_name="requested_" + key
        )
        if normalised is None or normalised != value:
            raise ValueError("Specify an exact execution-setting token")
    if "sol" in str(values.get("model", "")).lower():
        raise ValueError("Sol-family models are disabled")
    # Check host readiness/binding before represented identity effects. Reconcile
    # remains paused until an explicit resume; replay never silently resumes.
    if action in {"reconcile", "resume"}:
        preflight = _host(slot, "preflight", values)
        if preflight.get("status") != "prepared_host":
            return preflight
        _identity(slot, actor, organisation)
    result = _host(slot, action, values)
    return {
        **result,
        "instance_id": instance_id,
        "agent_id": slot["agent_id"],
        "delegator_id": actor,
        "organisation_id": organisation,
    }
