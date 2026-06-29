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
from .workflow_description_quality_service import (
    assess_workflow_description_quality,
)
from .workflow_registry import WorkflowRegistry

_PENDING_DEFINITION_IDENTITY_REASON = "lazy_definition_not_loaded"
_PENDING_DEFINITION_IDENTITY_BUILD_STATE = "pending_lazy_definition"
WORKFLOW_LISTING_METADATA_RESOLUTION_SCHEMA_VERSION = (
    "workflow_listing_metadata_resolution.v1"
)


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
    resolve_vontology_metadata: bool = True,
    metadata_mode: str | None = None,
    metadata_reason_code: str | None = None,
) -> dict[str, Any]:
    """Build a workflow-listing summary without forcing lazy definition loads."""

    registration = registry.peek_registration(workflow_id)
    definition = getattr(registration, "definition", None)

    source = registry.get_registration_source(
        workflow_id,
        resolve_lazy=False,
    ) or str(getattr(registration, "source", "") or "").strip() or "unknown"

    registration_purpose = getattr(registration, "purpose", None)
    definition_purpose = getattr(definition, "purpose", "") if definition is not None else None
    description = ""
    description_source = "none"
    if resolve_vontology_metadata:
        description, description_source = resolve_workflow_description(
            workflow_id,
            workflow_source=source,
            registration_purpose=registration_purpose,
            definition_purpose=definition_purpose,
        )
    else:
        if isinstance(registration_purpose, str) and registration_purpose.strip():
            description = registration_purpose.strip()
            description_source = "registration.purpose"
        elif isinstance(definition_purpose, str) and definition_purpose.strip():
            description = definition_purpose.strip()
            description_source = "definition.purpose"
    description_quality = assess_workflow_description_quality(
        description=description,
        description_source=description_source,
    )

    initial_state = ""
    if definition is not None:
        initial_state = str(getattr(definition, "initial_state", "") or "").strip()
    if resolve_vontology_metadata and not initial_state:
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
    if resolve_vontology_metadata and background_launch_policy is None:
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

    effective_metadata_mode = (
        "authoritative" if bool(resolve_vontology_metadata) else "fast"
    )
    requested_metadata_mode = str(metadata_mode or effective_metadata_mode).strip()
    if not requested_metadata_mode:
        requested_metadata_mode = effective_metadata_mode
    reason_code = str(metadata_reason_code or "").strip()
    if not reason_code:
        reason_code = (
            "vontology_metadata_resolved"
            if bool(resolve_vontology_metadata)
            else "fast_registration_metadata_only"
        )

    return {
        "workflow_id": workflow_id,
        "description": description,
        "description_source": description_source,
        "description_quality": description_quality,
        "initial_state": initial_state,
        "source": source,
        "background_launch_policy": background_launch_policy,
        "background_launch_policy_source": background_launch_policy_source,
        "definition_identity": definition_identity,
        "definition_loaded": definition is not None,
        "metadata_resolution": {
            "schema_version": WORKFLOW_LISTING_METADATA_RESOLUTION_SCHEMA_VERSION,
            "requested_mode": requested_metadata_mode,
            "effective_mode": effective_metadata_mode,
            "resolved_vontology_metadata": bool(resolve_vontology_metadata),
            "definition_loaded": definition is not None,
            "description_source": description_source,
            "reason_code": reason_code,
        },
    }


__all__ = [
    "WORKFLOW_LISTING_METADATA_RESOLUTION_SCHEMA_VERSION",
    "build_pending_workflow_definition_identity",
    "build_workflow_listing_entry",
]
