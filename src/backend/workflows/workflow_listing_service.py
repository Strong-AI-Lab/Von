from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .vontology_loader import (
    resolve_workflow_background_launch_policy,
    resolve_workflow_description,
    resolve_workflow_initial_step,
)
from .workflow_definition_identity_service import (
    WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION,
    WORKFLOW_DEFINITION_IDENTITY_VERSION,
    build_workflow_definition_identity,
)
from .workflow_registry import WorkflowRegistration, WorkflowRegistry

_PENDING_DEFINITION_IDENTITY_REASON = "lazy_definition_not_loaded"
_PENDING_DEFINITION_IDENTITY_BUILD_STATE = "pending_lazy_definition"


def build_pending_workflow_definition_identity(
    *,
    workflow_id: str,
    source: str,
) -> dict[str, Any]:
    """Return a stable placeholder until a lazy workflow definition is loaded."""

    return {
        "schema_version": WORKFLOW_DEFINITION_IDENTITY_SCHEMA_VERSION,
        "version": WORKFLOW_DEFINITION_IDENTITY_VERSION,
        "workflow_id": str(workflow_id or "").strip(),
        "source": str(source or "unknown").strip() or "unknown",
        "definition_hash": None,
        "runtime_definition_hash": None,
        "authoritative_definition_hash": None,
        "hash_mismatch": False,
        "state_count": 0,
        "action_count": 0,
        "build_state": _PENDING_DEFINITION_IDENTITY_BUILD_STATE,
        "reason_code": _PENDING_DEFINITION_IDENTITY_REASON,
    }


def build_workflow_listing_entry(
    *,
    registry: WorkflowRegistry,
    workflow_id: str,
) -> dict[str, Any]:
    """Build a workflow-listing summary without forcing lazy definition loads."""

    registration = registry.peek_registration(workflow_id)
    definition = registration.definition if isinstance(registration, WorkflowRegistration) else None

    source = registry.get_registration_source(
        workflow_id,
        resolve_lazy=False,
    ) or str(getattr(registration, "source", "") or "").strip() or "unknown"

    registration_purpose = getattr(registration, "purpose", None)
    definition_purpose = getattr(definition, "purpose", "") if definition is not None else None
    description, description_source = resolve_workflow_description(
        workflow_id,
        workflow_source=source,
        registration_purpose=registration_purpose,
        definition_purpose=definition_purpose,
    )

    initial_state = ""
    if definition is not None:
        initial_state = str(getattr(definition, "initial_state", "") or "").strip()
    if not initial_state:
        initial_state = str(resolve_workflow_initial_step(workflow_id) or "").strip()

    background_launch_policy = None
    background_launch_policy_source = "none"
    definition_metadata = getattr(definition, "metadata", None)
    if isinstance(definition_metadata, Mapping):
        policy_from_definition = definition_metadata.get("background_launch_policy")
        if isinstance(policy_from_definition, dict):
            background_launch_policy = dict(policy_from_definition)
            background_launch_policy_source = str(
                definition_metadata.get("background_launch_policy_source")
                or "definition.metadata"
            )
    if background_launch_policy is None:
        (
            background_launch_policy,
            background_launch_policy_source,
        ) = resolve_workflow_background_launch_policy(workflow_id)

    if definition is not None:
        definition_identity = build_workflow_definition_identity(
            workflow_id=workflow_id,
            source=source,
            definition=definition,
            authoritative_definition=definition if source.lower() == "vontology" else None,
        )
    else:
        definition_identity = build_pending_workflow_definition_identity(
            workflow_id=workflow_id,
            source=source,
        )

    return {
        "workflow_id": workflow_id,
        "description": description,
        "description_source": description_source,
        "initial_state": initial_state,
        "source": source,
        "background_launch_policy": background_launch_policy,
        "background_launch_policy_source": background_launch_policy_source,
        "definition_identity": definition_identity,
        "definition_loaded": definition is not None,
    }


__all__ = [
    "build_pending_workflow_definition_identity",
    "build_workflow_listing_entry",
]
