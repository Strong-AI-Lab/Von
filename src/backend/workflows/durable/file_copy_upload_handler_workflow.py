"""Durable workflow-first upload handler for file-copy uploads (JVNAUTOSCI-1309).

The handler composes:
1. a dedicated classification subworkflow,
2. conditional routing to specialised workflows where available,
3. fail-closed handling for low-confidence mutation routes, and
4. persisted route outcomes for post-run inspection.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping

from ...services.text_value_service import upsert_singleton_text_relation
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
)
from ..workflow_registry import WorkflowRegistration
from .file_copy_interpretation_workflow import FILE_COPY_INTERPRETATION_WORKFLOW_ID
from .file_copy_upload_classification_workflow import (
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_CLASSIFICATION_VERSION,
)

FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID = "#V#file_copy_upload_handler_workflow"
FILE_COPY_UPLOAD_ROUTE_OUTCOME_PREDICATE = "#V#has_file_copy_upload_route_outcome_json"
FILE_COPY_UPLOAD_HANDLER_VERSION = "file_copy_upload_handler.v1"

_FILE_COPY_CONTEXT_INPUTS = {
    "concept_id": {
        "$context_key": "concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_concept_id_to_concept_id_parameter",
    },
    "file_copy_concept_id": {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_file_copy_concept_id_to_file_copy_concept_id_parameter",
    },
    "content_type": {"$context_key": "content_type"},
    "original_filename": {"$context_key": "original_filename"},
    "size_bytes": {"$context_key": "size_bytes"},
    "sha256": {"$context_key": "sha256"},
    "blob_uri": {"$context_key": "blob_uri"},
    "uploaded_at": {"$context_key": "uploaded_at"},
    "index_in_rag": {"$context_key": "index_in_rag"},
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _map_from_subworkflow_result(
    *,
    outputs: dict[str, Any],
    prefix: str,
) -> dict[str, Any]:
    payload = dict(outputs)
    mapped: dict[str, Any] = {}
    for key, value in payload.items():
        mapped[f"{prefix}{key}"] = value
    return mapped


def _handle_mark_noop(_request: WorkflowActionRequest) -> WorkflowActionResult:
    return WorkflowActionResult(
        status="success",
        outputs={
            "upload_effective_route_mode": "noop",
            "upload_route_success": True,
            "upload_route_terminal_reason": "noop_route_selected",
        },
    )


def _handle_mark_fail_closed(request: WorkflowActionRequest) -> WorkflowActionResult:
    reason = _clean_text(request.data.get("upload_route_reasons"))
    if not reason:
        reason = "low_confidence_mutation_route"
    return WorkflowActionResult(
        status="success",
        outputs={
            "upload_effective_route_mode": "fail_closed",
            "upload_route_success": False,
            "upload_fail_closed_applied": True,
            "upload_route_terminal_reason": reason,
        },
    )


def _handle_mark_specialised_failure(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    error = _clean_text(request.data.get("upload_specialised_error")) or "specialised_failed"
    return WorkflowActionResult(
        status="success",
        outputs={
            "upload_specialised_fallback_triggered": True,
            "upload_route_success": False,
            "upload_route_terminal_reason": error,
        },
    )


def _resolve_effective_mode(data: Mapping[str, Any]) -> str:
    explicit = _clean_text(data.get("upload_effective_route_mode"))
    if explicit:
        return explicit

    selected_mode = _clean_text(data.get("upload_route_mode"))
    fallback_triggered = bool(data.get("upload_specialised_fallback_triggered"))
    interpret_attempted = bool(_clean_text(data.get("upload_interpret_workflow_id")))
    if fallback_triggered and interpret_attempted:
        return "interpret_after_specialised_failure"
    if fallback_triggered:
        return "specialised_failed"
    if selected_mode:
        return selected_mode
    return "interpret"


def _resolve_route_success(data: Mapping[str, Any], effective_mode: str) -> bool:
    explicit_success = data.get("upload_route_success")
    if isinstance(explicit_success, bool):
        return explicit_success

    if effective_mode in {"fail_closed", "specialised_failed"}:
        return False
    if effective_mode == "noop":
        return True
    if effective_mode in {"interpret", "interpret_after_specialised_failure"}:
        return not bool(data.get("upload_interpret_child_failed"))
    if effective_mode == "specialised":
        return not bool(data.get("upload_specialised_child_failed"))
    return not bool(data.get("last_action_failed"))


def _resolve_final_workflow_id(data: Mapping[str, Any]) -> str | None:
    for key in (
        "upload_interpret_workflow_id",
        "upload_specialised_workflow_id",
        "upload_target_workflow_id",
    ):
        token = _clean_text(data.get(key))
        if token:
            return token
    return None


def _handle_persist_route_outcome(request: WorkflowActionRequest) -> WorkflowActionResult:
    concept_id = _clean_text(request.data.get("file_copy_concept_id")) or _clean_text(
        request.data.get("concept_id")
    )
    if not concept_id:
        return WorkflowActionResult(
            status="success",
            outputs={
                "upload_route_outcome_persisted": False,
                "upload_route_outcome_persist_error": "missing_file_copy_concept_id",
            },
        )

    effective_mode = _resolve_effective_mode(request.data)
    route_success = _resolve_route_success(request.data, effective_mode)
    final_workflow_id = _resolve_final_workflow_id(request.data)
    route_reasons_raw = request.data.get("upload_route_reasons")
    route_reasons: list[str] = []
    if isinstance(route_reasons_raw, list):
        route_reasons = [
            str(item).strip()
            for item in route_reasons_raw
            if isinstance(item, str) and str(item).strip()
        ]

    payload = {
        "handler_version": FILE_COPY_UPLOAD_HANDLER_VERSION,
        "classification_version": str(
            request.data.get("classification_version")
            or FILE_COPY_UPLOAD_CLASSIFICATION_VERSION
        ),
        "recorded_at": _utc_now_iso(),
        "route_key": request.data.get("upload_route_key"),
        "selected_route_mode": request.data.get("upload_route_mode"),
        "effective_route_mode": effective_mode,
        "route_confidence": request.data.get("upload_route_confidence"),
        "minimum_mutation_confidence": request.data.get(
            "upload_minimum_mutation_confidence"
        ),
        "mutation_route": bool(request.data.get("upload_mutation_route")),
        "fail_closed": bool(request.data.get("upload_fail_closed")),
        "route_success": bool(route_success),
        "route_reasons": route_reasons,
        "decision_persisted": bool(request.data.get("upload_route_decision_persisted")),
        "target_workflow_id": request.data.get("upload_target_workflow_id"),
        "target_workflow_available": bool(
            request.data.get("upload_target_workflow_available")
        ),
        "final_executed_workflow_id": final_workflow_id,
        "specialised_child_failed": bool(request.data.get("upload_specialised_child_failed")),
        "specialised_error": request.data.get("upload_specialised_error"),
        "specialised_final_state": request.data.get("upload_specialised_final_state"),
        "interpret_child_failed": bool(request.data.get("upload_interpret_child_failed")),
        "interpret_error": request.data.get("upload_interpret_error"),
        "interpret_final_state": request.data.get("upload_interpret_final_state"),
        "specialised_fallback_triggered": bool(
            request.data.get("upload_specialised_fallback_triggered")
        ),
    }

    try:
        upsert = upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate=FILE_COPY_UPLOAD_ROUTE_OUTCOME_PREDICATE,
            text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            lang="en-NZ",
            garbage_collect=True,
            context={
                "source": FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
                "handler_version": FILE_COPY_UPLOAD_HANDLER_VERSION,
            },
        )
        return WorkflowActionResult(
            status="success",
            outputs={
                "upload_route_outcome_persisted": True,
                "upload_route_outcome_predicate": FILE_COPY_UPLOAD_ROUTE_OUTCOME_PREDICATE,
                "upload_route_outcome_relation_id": (
                    upsert.get("kept_relation_id") if isinstance(upsert, Mapping) else None
                ),
                "upload_route_outcome_replaced_count": (
                    int(upsert.get("replaced_count") or 0)
                    if isinstance(upsert, Mapping)
                    else 0
                ),
                "upload_effective_route_mode": effective_mode,
                "upload_route_success": route_success,
                "upload_final_executed_workflow_id": final_workflow_id,
            },
        )
    except Exception as exc:
        return WorkflowActionResult(
            status="success",
            outputs={
                "upload_route_outcome_persisted": False,
                "upload_route_outcome_predicate": FILE_COPY_UPLOAD_ROUTE_OUTCOME_PREDICATE,
                "upload_route_outcome_persist_error": str(exc),
                "upload_effective_route_mode": effective_mode,
                "upload_route_success": route_success,
                "upload_final_executed_workflow_id": final_workflow_id,
            },
        )


def build_file_copy_upload_handler_workflow() -> WorkflowDefinition:
    classify = WorkflowStateSpec(
        state_id="classify",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                inputs={
                    **dict(_FILE_COPY_CONTEXT_INPUTS),
                    "workflow_id": FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
                    "failure_mode": WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
                    "minimum_route_score": {"$context_key": "minimum_route_score"},
                    "minimum_mutation_confidence": {
                        "$context_key": "minimum_mutation_confidence"
                    },
                    "force_route_key": {"$context_key": "force_route_key"},
                    "allow_interpret_fallback": {
                        "$context_key": "allow_interpret_fallback"
                    },
                    "scholarly_workflow_id": {
                        "$context_key": "scholarly_workflow_id"
                    },
                    "cv_workflow_id": {"$context_key": "cv_workflow_id"},
                    "business_card_workflow_id": {
                        "$context_key": "business_card_workflow_id"
                    },
                },
                description=(
                    "Invoke file-copy upload classification subworkflow to "
                    "determine route mode and confidence."
                ),
            ),
        ),
        metadata={
            "tool_output_context_mappings": [
                {"tool_output_field": "result.classification_version", "context_key": "classification_version"},
                {"tool_output_field": "result.route_key", "context_key": "upload_route_key"},
                {"tool_output_field": "result.route_mode", "context_key": "upload_route_mode"},
                {"tool_output_field": "result.route_confidence", "context_key": "upload_route_confidence"},
                {"tool_output_field": "result.minimum_mutation_confidence", "context_key": "upload_minimum_mutation_confidence"},
                {"tool_output_field": "result.minimum_route_score", "context_key": "upload_minimum_route_score"},
                {"tool_output_field": "result.route_reasons", "context_key": "upload_route_reasons"},
                {"tool_output_field": "result.mutation_route", "context_key": "upload_mutation_route"},
                {"tool_output_field": "result.fail_closed", "context_key": "upload_fail_closed"},
                {"tool_output_field": "result.target_workflow_id", "context_key": "upload_target_workflow_id"},
                {"tool_output_field": "result.target_workflow_available", "context_key": "upload_target_workflow_available"},
                {"tool_output_field": "result.allow_interpret_fallback", "context_key": "upload_allow_interpret_fallback"},
                {"tool_output_field": "result.route_decision_persisted", "context_key": "upload_route_decision_persisted"},
                {"tool_output_field": "child_workflow_failed", "context_key": "upload_classifier_child_failed"},
                {"tool_output_field": "subworkflow_error", "context_key": "upload_classifier_error"},
            ]
        },
        transitions=(
            WorkflowTransitionSpec(
                to_state="fail_closed",
                condition=lambda ctx: bool(ctx.get("upload_fail_closed")),
                reason="fail_closed_selected",
            ),
            WorkflowTransitionSpec(
                to_state="specialised",
                condition=lambda ctx: (
                    str(ctx.get("upload_route_mode") or "").strip().lower()
                    == "specialised"
                    and bool(str(ctx.get("upload_target_workflow_id") or "").strip())
                ),
                reason="specialised_route_selected",
            ),
            WorkflowTransitionSpec(
                to_state="interpret",
                condition=lambda ctx: (
                    str(ctx.get("upload_route_mode") or "").strip().lower() == "interpret"
                ),
                reason="interpret_route_selected",
            ),
            WorkflowTransitionSpec(
                to_state="noop",
                condition=lambda ctx: (
                    str(ctx.get("upload_route_mode") or "").strip().lower() == "noop"
                ),
                reason="noop_route_selected",
            ),
            WorkflowTransitionSpec(
                to_state="interpret",
                condition=lambda ctx: _as_bool(
                    ctx.get("upload_allow_interpret_fallback"),
                    default=True,
                ),
                reason="fallback_interpret_default",
            ),
            WorkflowTransitionSpec(
                to_state="noop",
                condition=lambda _ctx: True,
                reason="fallback_noop_default",
            ),
        ),
    )

    specialised = WorkflowStateSpec(
        state_id="specialised",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                inputs={
                    **dict(_FILE_COPY_CONTEXT_INPUTS),
                    "workflow_id": {"$context_key": "upload_target_workflow_id"},
                    "failure_mode": WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
                },
                description=(
                    "Invoke specialised workflow selected by upload "
                    "classification route."
                ),
            ),
        ),
        metadata={
            "tool_output_context_mappings": [
                {"tool_output_field": "child_workflow_failed", "context_key": "upload_specialised_child_failed"},
                {"tool_output_field": "subworkflow_error", "context_key": "upload_specialised_error"},
                {"tool_output_field": "subworkflow_invocation.child_workflow_id", "context_key": "upload_specialised_workflow_id"},
                {"tool_output_field": "subworkflow_invocation.child_final_state", "context_key": "upload_specialised_final_state"},
            ]
        },
        transitions=(
            WorkflowTransitionSpec(
                to_state="specialised_failed",
                condition=lambda ctx: bool(ctx.get("upload_specialised_child_failed")),
                reason="specialised_child_failed",
            ),
            WorkflowTransitionSpec(
                to_state="record_outcome",
                condition=lambda _ctx: True,
                reason="specialised_completed",
            ),
        ),
    )

    specialised_failed = WorkflowStateSpec(
        state_id="specialised_failed",
        actions=(
            WorkflowActionInvocation(
                action_id="file_copy_upload.mark_specialised_failure",
                description=(
                    "Record specialised-route failure and optionally fall back "
                    "to baseline interpretation."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="interpret",
                condition=lambda ctx: _as_bool(
                    ctx.get("upload_allow_interpret_fallback"),
                    default=True,
                ),
                reason="fallback_to_interpret",
            ),
            WorkflowTransitionSpec(
                to_state="record_outcome",
                condition=lambda _ctx: True,
                reason="no_fallback_after_specialised_failure",
            ),
        ),
    )

    interpret = WorkflowStateSpec(
        state_id="interpret",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                inputs={
                    **dict(_FILE_COPY_CONTEXT_INPUTS),
                    "workflow_id": FILE_COPY_INTERPRETATION_WORKFLOW_ID,
                    "failure_mode": WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
                },
                description=(
                    "Invoke baseline file-copy interpretation workflow for "
                    "safe non-specialised handling."
                ),
            ),
        ),
        metadata={
            "tool_output_context_mappings": [
                {"tool_output_field": "child_workflow_failed", "context_key": "upload_interpret_child_failed"},
                {"tool_output_field": "subworkflow_error", "context_key": "upload_interpret_error"},
                {"tool_output_field": "subworkflow_invocation.child_workflow_id", "context_key": "upload_interpret_workflow_id"},
                {"tool_output_field": "subworkflow_invocation.child_final_state", "context_key": "upload_interpret_final_state"},
            ]
        },
        transitions=(
            WorkflowTransitionSpec(
                to_state="record_outcome",
                condition=lambda _ctx: True,
                reason="interpret_completed",
            ),
        ),
    )

    noop = WorkflowStateSpec(
        state_id="noop",
        actions=(
            WorkflowActionInvocation(
                action_id="file_copy_upload.mark_noop",
                description="Record explicit no-op route for unsupported uploads.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="record_outcome",
                condition=lambda _ctx: True,
                reason="noop_recorded",
            ),
        ),
    )

    fail_closed = WorkflowStateSpec(
        state_id="fail_closed",
        actions=(
            WorkflowActionInvocation(
                action_id="file_copy_upload.mark_fail_closed",
                description=(
                    "Fail closed for low-confidence mutation routes instead of "
                    "executing specialised mutations."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="record_outcome",
                condition=lambda _ctx: True,
                reason="fail_closed_recorded",
            ),
        ),
    )

    record_outcome = WorkflowStateSpec(
        state_id="record_outcome",
        actions=(
            WorkflowActionInvocation(
                action_id="file_copy_upload.persist_route_outcome",
                description=(
                    "Persist final upload route outcome for inspectability and "
                    "post-run diagnostics."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="outcome_persisted",
            ),
        ),
    )

    return WorkflowDefinition(
        workflow_id=FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
        initial_state="classify",
        states={
            "classify": classify,
            "specialised": specialised,
            "specialised_failed": specialised_failed,
            "interpret": interpret,
            "noop": noop,
            "fail_closed": fail_closed,
            "record_outcome": record_outcome,
            "complete": WorkflowStateSpec(state_id="complete", terminal=True),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Workflow-first file upload handler with classification subworkflow, "
            "specialised routing, fail-closed guardrails, and inspectable outcomes."
        ),
    )


def get_file_copy_upload_handler_workflow_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
        definition=build_file_copy_upload_handler_workflow(),
        purpose=(
            "General workflow-first upload handler that routes file copies into "
            "specialised or baseline interpretation workflows."
        ),
        source="built_in",
    )


def register_file_copy_upload_handler_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id="file_copy_upload.mark_noop",
            handler=_handle_mark_noop,
            description="Record explicit no-op route outcome.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id="file_copy_upload.mark_fail_closed",
            handler=_handle_mark_fail_closed,
            description="Record fail-closed route outcome.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id="file_copy_upload.mark_specialised_failure",
            handler=_handle_mark_specialised_failure,
            description="Record specialised route failure before fallback.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id="file_copy_upload.persist_route_outcome",
            handler=_handle_persist_route_outcome,
            description="Persist final upload route outcome as singleton text relation.",
        )
    )


__all__ = [
    "FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID",
    "FILE_COPY_UPLOAD_ROUTE_OUTCOME_PREDICATE",
    "FILE_COPY_UPLOAD_HANDLER_VERSION",
    "build_file_copy_upload_handler_workflow",
    "get_file_copy_upload_handler_workflow_registration",
    "register_file_copy_upload_handler_actions",
]
