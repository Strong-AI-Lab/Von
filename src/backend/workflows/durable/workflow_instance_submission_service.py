"""Canonical durable workflow instance submission with runnable verification.

This module is the single authoritative pathway for creating durable workflow
instances from user-facing surfaces (MCP tools, REST routes, scheduler
triggers). It prevents optimistic "started/running" claims when a workflow is
not actually runnable in the current process.

JVNAUTOSCI-1106:
- Require preflight runnable verification before instance creation.
- Re-check runnability post-create before returning success.
- Return structured verification telemetry for conceptual/executable/runnable
  states so callers can present accurate status.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Sequence

from ..engine import WorkflowDefinition
from ..vontology_loader import build_workflow_process_graph, detect_vacuous_workflow_steps
from .instance_manager import WorkflowInstanceManager
from .registry_factory import (
    build_durable_action_registry,
    build_workflow_registry_read_only,
)


@dataclass(frozen=True)
class WorkflowRunnableVerification:
    workflow_id: str
    conceptual_representation_success: bool
    executable_registration_success: bool
    runnable_verification_success: bool
    fallback_action_routing_enabled: bool
    discovered_action_ids: tuple[str, ...]
    unsupported_action_ids: tuple[str, ...]
    integrity_issues: tuple[Dict[str, Any], ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "conceptual_representation_success": self.conceptual_representation_success,
            "executable_registration_success": self.executable_registration_success,
            "runnable_verification_success": self.runnable_verification_success,
            "fallback_action_routing_enabled": self.fallback_action_routing_enabled,
            "discovered_action_ids": list(self.discovered_action_ids),
            "unsupported_action_ids": list(self.unsupported_action_ids),
            "integrity_issues": [dict(item) for item in self.integrity_issues],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class WorkflowInstanceSubmissionResult:
    success: bool
    workflow_id: str
    status: str
    instance_id: str | None
    verification: Mapping[str, Any]
    error_code: str | None = None
    error: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "success": self.success,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "verification": dict(self.verification),
        }
        if isinstance(self.instance_id, str) and self.instance_id:
            payload["instance_id"] = self.instance_id
        if isinstance(self.error_code, str) and self.error_code:
            payload["error_code"] = self.error_code
        if isinstance(self.error, str) and self.error:
            payload["error"] = self.error
        return payload


def _normalise_warning_items(items: Sequence[Any] | None) -> List[str]:
    return [
        str(item).strip()
        for item in (items or [])
        if isinstance(item, str) and str(item).strip()
    ]


def _collect_action_ids(definition: WorkflowDefinition) -> tuple[str, ...]:
    action_ids: set[str] = set()
    for state in definition.states.values():
        for action in state.actions:
            action_id = str(action.action_id or "").strip()
            if action_id:
                action_ids.add(action_id)
    return tuple(sorted(action_ids))


@lru_cache(maxsize=1)
def _internal_mcp_method_names() -> frozenset[str]:
    """Return available internal MCP tool names (best effort, cached)."""

    try:
        from ...integrations.internal_mcp.catalogue import build_default_catalogue

        return frozenset(build_default_catalogue().list_methods())
    except Exception:
        return frozenset()


def verify_workflow_runnable(workflow_id: str) -> WorkflowRunnableVerification:
    """Evaluate whether a workflow is runnable in the current runtime context."""

    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return WorkflowRunnableVerification(
            workflow_id="",
            conceptual_representation_success=False,
            executable_registration_success=False,
            runnable_verification_success=False,
            fallback_action_routing_enabled=False,
            discovered_action_ids=(),
            unsupported_action_ids=(),
            integrity_issues=(),
            warnings=(),
            errors=("invalid_workflow_id",),
        )

    graph, graph_warnings = build_workflow_process_graph(workflow_id)
    warnings = _normalise_warning_items(graph_warnings)
    conceptual_representation_success = isinstance(graph, dict)

    registry = build_workflow_registry_read_only()
    definition = registry.get(workflow_id)
    executable_registration_success = definition is not None
    if definition is None:
        error_items = ("workflow_definition_not_registered",)
        return WorkflowRunnableVerification(
            workflow_id=workflow_id,
            conceptual_representation_success=conceptual_representation_success,
            executable_registration_success=False,
            runnable_verification_success=False,
            fallback_action_routing_enabled=False,
            discovered_action_ids=(),
            unsupported_action_ids=(),
            integrity_issues=(),
            warnings=tuple(warnings),
            errors=error_items,
        )

    integrity_issues = tuple(
        detect_vacuous_workflow_steps(workflow_id=workflow_id, graph=graph)
    )
    if integrity_issues:
        warnings.append("workflow_step_contract_integrity_issue")
        # Keep concise error code at top level for compatibility with callers.
        # Detailed context remains in integrity_issues.

    action_registry = build_durable_action_registry()
    fallback_enabled = action_registry.has_fallback_handler()
    action_ids = _collect_action_ids(definition)

    unsupported: list[str] = []
    fallback_tool_names = _internal_mcp_method_names() if fallback_enabled else frozenset()
    if fallback_enabled and not fallback_tool_names:
        warnings.append("internal_tool_catalogue_unavailable")

    for action_id in action_ids:
        if action_registry.has(action_id):
            continue
        if fallback_enabled:
            if not fallback_tool_names or action_id in fallback_tool_names:
                continue
        unsupported.append(action_id)

    runnable_verification_success = len(unsupported) == 0 and not integrity_issues
    errors: list[str] = []
    if unsupported:
        errors.append("unsupported_workflow_actions")
    if integrity_issues:
        errors.append("workflow_step_contract_integrity_issue")

    return WorkflowRunnableVerification(
        workflow_id=workflow_id,
        conceptual_representation_success=conceptual_representation_success,
        executable_registration_success=executable_registration_success,
        runnable_verification_success=runnable_verification_success,
        fallback_action_routing_enabled=fallback_enabled,
        discovered_action_ids=action_ids,
        unsupported_action_ids=tuple(sorted(set(unsupported))),
        integrity_issues=integrity_issues,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def _build_submission_verification_payload(
    *,
    preflight: WorkflowRunnableVerification,
    postflight: WorkflowRunnableVerification | None,
) -> Dict[str, Any]:
    postflight_payload = postflight.to_dict() if postflight is not None else None
    postflight_passed = bool(
        postflight is not None and postflight.runnable_verification_success
    )

    return {
        "workflow_id": preflight.workflow_id,
        "conceptual_representation_success": preflight.conceptual_representation_success,
        "executable_registration_success": preflight.executable_registration_success,
        "runnable_verification_success": (
            postflight.runnable_verification_success
            if postflight is not None
            else preflight.runnable_verification_success
        ),
        "preflight_passed": preflight.runnable_verification_success,
        "postflight_passed": postflight_passed,
        "preflight": preflight.to_dict(),
        "postflight": postflight_payload,
    }


def submit_verified_workflow_instance(
    *,
    manager: WorkflowInstanceManager,
    workflow_id: str,
    user_id: str,
    org_id: str,
    namespace: str,
    inputs: Mapping[str, Any] | None = None,
    schedule_id: str | None = None,
    max_retries: int = 3,
    source_event_type: str | None = None,
    source_event_id: str | None = None,
    event_idempotency_key: str | None = None,
) -> WorkflowInstanceSubmissionResult:
    """Create a durable workflow instance only when runnable verification passes."""

    workflow_id = str(workflow_id or "").strip()
    preflight = verify_workflow_runnable(workflow_id)
    verification_payload = _build_submission_verification_payload(
        preflight=preflight,
        postflight=None,
    )

    if not preflight.runnable_verification_success:
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id=workflow_id,
            status="rejected_preflight",
            instance_id=None,
            error_code="workflow_not_runnable",
            error=(
                f"Workflow '{workflow_id}' is not runnable; "
                "instance was not created."
            ),
            verification=verification_payload,
        )

    instance_id = manager.create_instance(
        workflow_id,
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        inputs=dict(inputs or {}),
        schedule_id=schedule_id,
        max_retries=max_retries,
        source_event_type=source_event_type,
        source_event_id=source_event_id,
        event_idempotency_key=event_idempotency_key,
    )

    postflight = verify_workflow_runnable(workflow_id)
    verification_payload = _build_submission_verification_payload(
        preflight=preflight,
        postflight=postflight,
    )
    if not postflight.runnable_verification_success:
        manager.mark_failed(
            instance_id,
            error=f"workflow_postflight_not_runnable:{workflow_id}",
            increment_retry=False,
        )
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id=workflow_id,
            status="rejected_postflight",
            instance_id=instance_id,
            error_code="workflow_not_runnable_postflight",
            error=(
                f"Workflow '{workflow_id}' failed postflight runnability check; "
                "instance marked failed."
            ),
            verification=verification_payload,
        )

    return WorkflowInstanceSubmissionResult(
        success=True,
        workflow_id=workflow_id,
        status="pending",
        instance_id=instance_id,
        verification=verification_payload,
    )


__all__ = [
    "WorkflowRunnableVerification",
    "WorkflowInstanceSubmissionResult",
    "verify_workflow_runnable",
    "submit_verified_workflow_instance",
]
