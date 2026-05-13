"""Durable support actions for failure-case prompt improvement workflows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...services.failure_case_intake_service import (
    FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
    collect_failure_case_intake,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)


def _safe_mapping(value: Any) -> dict[str, Any]:
    return (
        {str(key): item for key, item in value.items()}
        if isinstance(value, Mapping)
        else {}
    )


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _failure_case_intake_handler(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = _safe_mapping(request.inputs)
    context = _safe_mapping(request.data)
    environment = request.environment

    namespace = (
        _safe_str(inputs.get("namespace"))
        or _safe_str(context.get("namespace"))
        or _safe_str(getattr(environment, "user_namespace", None))
    )
    user_concept_id = (
        _safe_str(inputs.get("user_concept_id"))
        or _safe_str(context.get("user_concept_id"))
        or _safe_str(context.get("user_id"))
        or _safe_str(getattr(environment, "user_concept_id", None))
    )
    organisation_concept_id = (
        _safe_str(inputs.get("organisation_concept_id"))
        or _safe_str(inputs.get("org_concept_id"))
        or _safe_str(context.get("organisation_concept_id"))
        or _safe_str(context.get("org_concept_id"))
        or _safe_str(context.get("org_id"))
        or _safe_str(getattr(environment, "org_concept_id", None))
    )

    mcp_invoker = inputs.get("mcp_invoker")
    if mcp_invoker is None:
        mcp_invoker = getattr(environment, "gateway", None)

    payload = collect_failure_case_intake(
        conversation_ref=inputs.get("conversation_ref"),
        chat_history_lookup=inputs.get("chat_history_lookup"),
        request_id=_safe_str(inputs.get("request_id"))
        or _safe_str(context.get("request_id")),
        session_id=_safe_str(inputs.get("session_id"))
        or _safe_str(context.get("session_id")),
        conversation_session_id=_safe_str(inputs.get("conversation_session_id")),
        namespace=namespace,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        target_model=_safe_str(inputs.get("target_model")),
        comparator_model=_safe_str(inputs.get("comparator_model")),
        workflow_id=_safe_str(inputs.get("workflow_id")),
        stage_id=_safe_str(inputs.get("stage_id")),
        include_legacy=inputs.get("include_legacy"),
        history_tail_limit=inputs.get("history_tail_limit"),
        max_text_chars=inputs.get("max_text_chars", 4000),
        mcp_invoker=mcp_invoker,
    )
    success = bool(payload.get("success"))
    return WorkflowActionResult(
        status="success" if success else "failed",
        outputs=dict(payload),
        error=(
            None
            if success
            else _safe_str(payload.get("error")) or _safe_str(payload.get("error_code"))
        ),
    )


def register_failure_case_prompt_improvement_actions(registry: ActionRegistry) -> None:
    """Register support actions used by represented prompt-improvement workflows."""

    registry.register_if_absent(
        ActionSpec(
            action_id=FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
            handler=_failure_case_intake_handler,
            description=(
                "Collect compact turn, prompt, tool, critic, and completion-gate "
                "evidence for a failed turn without classifying the failure or "
                "generating prompt hypotheses."
            ),
        )
    )


__all__ = [
    "register_failure_case_prompt_improvement_actions",
]
