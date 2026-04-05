"""Reusable durable workflow actions for context bundles and dossiers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...services.context_bundle_contracts import (
    CONTEXT_ASSEMBLE_DOSSIER_ACTION_ID,
    CONTEXT_BUILD_RECONSTRUCTED_WORKSPACE_ACTION_ID,
    CONTEXT_RESOLVE_EFFECTIVE_CONTEXT_ACTION_ID,
    CONTEXT_UPDATE_REPORT_REVISION_ACTION_ID,
)
from ...services.context_bundle_service import (
    assemble_context_dossier,
    build_reconstructed_workspace,
    resolve_effective_context,
    update_context_report_revision,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _result_from_payload(payload: Mapping[str, Any] | None) -> WorkflowActionResult:
    data = dict(payload) if isinstance(payload, Mapping) else {}
    success = bool(data.get("success"))
    error = _safe_str(data.get("error")) or _safe_str(data.get("error_code")) or None
    return WorkflowActionResult(
        status="success" if success else "failed",
        outputs=data,
        error=None if success else error,
    )


def _handle_context_resolve_effective_context(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    context = _mapping_or_empty(request.data)
    result = resolve_effective_context(
        subject_kind=_safe_str(inputs.get("subject_kind") or context.get("subject_kind")),
        subject_id=_safe_str(inputs.get("subject_id") or context.get("subject_id")),
        explicit_bundle_ids=inputs.get("explicit_bundle_ids")
        or context.get("explicit_bundle_ids")
        or (),
        workflow_step_bundle_ids=inputs.get("workflow_step_bundle_ids")
        or context.get("workflow_step_bundle_ids")
        or (),
        local_default_bundle_ids=inputs.get("local_default_bundle_ids")
        or context.get("local_default_bundle_ids")
        or (),
        include_type_hierarchy=bool(inputs.get("include_type_hierarchy", True)),
    )
    return _result_from_payload(result)


def _handle_context_assemble_context_dossier(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    context = _mapping_or_empty(request.data)
    result = assemble_context_dossier(
        name=_safe_str(inputs.get("name") or "Context dossier"),
        dossier_id=_safe_str(inputs.get("dossier_id")) or None,
        subject_kind=_safe_str(inputs.get("subject_kind") or context.get("subject_kind")),
        subject_id=_safe_str(inputs.get("subject_id") or context.get("subject_id")),
        dossier_kind=_safe_str(inputs.get("dossier_kind")) or None,
        effective_context_bundle_ids=inputs.get("effective_context_bundle_ids")
        or context.get("effective_context_bundle_ids")
        or (),
        open_questions=inputs.get("open_questions") or context.get("open_questions") or (),
        evidence_receipts=inputs.get("evidence_receipts")
        or context.get("evidence_receipts")
        or (),
        immediate_context=inputs.get("immediate_context")
        or context.get("immediate_context"),
        search_history=inputs.get("search_history") or context.get("search_history") or (),
        testing_theory_ids=inputs.get("testing_theory_ids")
        or context.get("testing_theory_ids")
        or (),
        local_assertions=inputs.get("local_assertions")
        or context.get("local_assertions")
        or (),
        hypotheses=inputs.get("hypotheses") or context.get("hypotheses") or (),
        promotion_candidates=inputs.get("promotion_candidates")
        or context.get("promotion_candidates")
        or (),
        branch_specs=inputs.get("branch_specs") or context.get("branch_specs") or (),
        report_text=_safe_str(inputs.get("report_text")) or None,
        report_title=_safe_str(inputs.get("report_title")) or None,
        report_summary=inputs.get("report_summary"),
        namespace=_safe_str(inputs.get("namespace") or context.get("namespace")) or None,
        user_id=_safe_str(inputs.get("user_id") or context.get("user_id")) or None,
        org_id=_safe_str(inputs.get("org_id") or context.get("org_id")) or None,
    )
    return _result_from_payload(result)


def _handle_context_update_report_revision(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    context = _mapping_or_empty(request.data)
    result = update_context_report_revision(
        dossier_id=_safe_str(inputs.get("dossier_id") or context.get("context_dossier_id")),
        report_text=_safe_str(inputs.get("report_text") or context.get("report_text")),
        revision_id=_safe_str(inputs.get("revision_id")) or None,
        title=_safe_str(inputs.get("title")) or None,
        summary=inputs.get("summary"),
        open_questions=inputs.get("open_questions") or context.get("open_questions") or (),
        evidence_receipts=inputs.get("evidence_receipts")
        or context.get("evidence_receipts")
        or (),
        branch_id=_safe_str(inputs.get("branch_id")) or None,
        status=_safe_str(inputs.get("status")) or None,
        namespace=_safe_str(inputs.get("namespace") or context.get("namespace")) or None,
        user_id=_safe_str(inputs.get("user_id") or context.get("user_id")) or None,
        org_id=_safe_str(inputs.get("org_id") or context.get("org_id")) or None,
    )
    return _result_from_payload(result)


def _handle_context_build_reconstructed_workspace(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    context = _mapping_or_empty(request.data)
    result = build_reconstructed_workspace(
        subject_kind=_safe_str(inputs.get("subject_kind") or context.get("subject_kind")),
        subject_id=_safe_str(inputs.get("subject_id") or context.get("subject_id")),
        question=_safe_str(inputs.get("question")) or None,
        task=_safe_str(inputs.get("task")) or None,
        dossier_id=_safe_str(inputs.get("dossier_id") or context.get("context_dossier_id"))
        or None,
        report_revision_id=_safe_str(
            inputs.get("report_revision_id") or context.get("report_revision_id")
        )
        or None,
        effective_context_bundle_ids=inputs.get("effective_context_bundle_ids")
        or context.get("effective_context_bundle_ids")
        or (),
        immediate_context=inputs.get("immediate_context")
        or context.get("immediate_context"),
        open_questions=inputs.get("open_questions") or context.get("open_questions") or (),
        evidence_receipts=inputs.get("evidence_receipts")
        or context.get("evidence_receipts")
        or (),
        search_history=inputs.get("search_history") or context.get("search_history") or (),
        interaction_budget=inputs.get("interaction_budget")
        or context.get("interaction_budget"),
        termination_status=inputs.get("termination_status")
        or context.get("termination_status"),
        reconstruction_round=inputs.get("reconstruction_round"),
        compression_decisions=inputs.get("compression_decisions")
        or context.get("compression_decisions")
        or (),
        guardrail_events=inputs.get("guardrail_events")
        or context.get("guardrail_events")
        or (),
        promotion_attempts=inputs.get("promotion_attempts")
        or context.get("promotion_attempts")
        or (),
        branch_transitions=inputs.get("branch_transitions")
        or context.get("branch_transitions")
        or (),
    )
    return _result_from_payload(result)


def register_context_bundle_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=CONTEXT_RESOLVE_EFFECTIVE_CONTEXT_ACTION_ID,
            handler=_handle_context_resolve_effective_context,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=CONTEXT_ASSEMBLE_DOSSIER_ACTION_ID,
            handler=_handle_context_assemble_context_dossier,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=CONTEXT_UPDATE_REPORT_REVISION_ACTION_ID,
            handler=_handle_context_update_report_revision,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=CONTEXT_BUILD_RECONSTRUCTED_WORKSPACE_ACTION_ID,
            handler=_handle_context_build_reconstructed_workspace,
        )
    )


__all__ = ["register_context_bundle_actions"]
