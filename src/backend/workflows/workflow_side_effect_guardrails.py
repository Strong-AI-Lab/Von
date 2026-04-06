"""Shared workflow-side-effect guardrails for MCP fallback execution."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Mapping, Sequence

from .action_registry import WorkflowActionRequest, WorkflowActionResult
from .write_tool_policy import (
    build_mutation_guardrail_events,
    compute_workflow_execution_write_policy,
    resolve_workflow_execution_side_effect_policy,
)


def enforce_workflow_mcp_write_guardrails(
    *,
    request: WorkflowActionRequest,
    resolved_tool_name: str,
    method_definition: Any,
) -> WorkflowActionResult | None:
    category = str(getattr(method_definition, "category", "") or "").strip().lower()
    if category != "write":
        return None

    workflow_state_metadata = (
        dict(request.workflow_state_metadata)
        if isinstance(request.workflow_state_metadata, Mapping)
        else {}
    )
    execution_side_effect_policy = resolve_workflow_execution_side_effect_policy(
        workflow_context=request.data if isinstance(request.data, Mapping) else None,
        workflow_state_metadata=workflow_state_metadata,
    )

    user_mutation_authority, global_mutation_authority = (
        _resolve_runtime_mutation_authority_sources(request.environment)
    )
    decision = compute_workflow_execution_write_policy(
        requested_tools=[resolved_tool_name],
        user_mutation_authority=user_mutation_authority,
        workflow_mutation_authority=workflow_state_metadata.get("mutation_authority"),
        global_mutation_authority=global_mutation_authority,
        execution_side_effect_policy=execution_side_effect_policy,
    )
    if resolved_tool_name in decision.allowed_tools:
        return None

    mutation_guardrail_events = build_mutation_guardrail_events(
        policy_decision=decision,
        guardrail_surface="workflow_mcp_fallback",
        stage=request.workflow_state_id,
        workflow_id=request.workflow_id,
        workflow_step_id=request.workflow_state_id,
        action_id=request.action_id,
    )
    _record_mutation_guardrail_events(
        events=mutation_guardrail_events,
        context=request.data if isinstance(request.data, MutableMapping) else None,
    )

    tool_decision = decision.decision_for_tool(resolved_tool_name)
    return WorkflowActionResult(
        status="failed",
        error=(
            "mutation_guardrail_blocked:"
            f"{resolved_tool_name}:{decision.reason or 'write_blocked'}"
        ),
        outputs={
            "mcp_tool": resolved_tool_name,
            "mutation_guardrail_blocked": True,
            "mutation_guardrail_events": [dict(event) for event in mutation_guardrail_events],
            "write_policy_reason": decision.reason,
            "write_policy_decision_basis": decision.decision_basis,
            "write_policy_outcome": decision.outcome,
            "write_policy_effective_mutation_authority": (
                decision.effective_mutation_authority
            ),
            "write_policy_authority_sources": dict(decision.authority_sources),
            "write_policy_tool_outcomes": {
                item.tool_name: item.outcome for item in decision.tool_decisions
            },
            "write_policy_blocked_reasons": {
                item.tool_name: item.blocked_reason for item in decision.tool_decisions
            },
            "write_policy_execution_side_effect_policy": (
                dict(execution_side_effect_policy or {})
            ),
            "write_policy_tool_decision": {
                "tool_name": resolved_tool_name,
                "outcome": tool_decision.outcome if tool_decision is not None else None,
                "blocked_reason": (
                    tool_decision.blocked_reason if tool_decision is not None else None
                ),
                "authority_block_source": (
                    tool_decision.authority_block_source
                    if tool_decision is not None
                    else None
                ),
            },
        },
    )


def _resolve_runtime_mutation_authority_sources(
    environment: Any,
) -> tuple[str | None, str | None]:
    user_concept_id = str(getattr(environment, "user_concept_id", "") or "").strip()
    try:
        from ..services.settings_service import (
            get_global_mutation_authority_level,
            get_user_mutation_authority_level,
        )

        user_mutation_authority = (
            get_user_mutation_authority_level(user_concept_id)
            if user_concept_id
            else None
        )
        global_mutation_authority = get_global_mutation_authority_level()
        return user_mutation_authority, global_mutation_authority
    except Exception:
        return None, None


def _record_mutation_guardrail_events(
    *,
    events: Sequence[Mapping[str, Any]] | None,
    context: MutableMapping[str, Any] | None = None,
) -> None:
    if not isinstance(events, Sequence):
        return
    context_events = None
    if isinstance(context, MutableMapping):
        existing = context.get("mutation_guardrail_events")
        if isinstance(existing, list):
            context_events = existing
        else:
            context_events = []
            context["mutation_guardrail_events"] = context_events
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_payload = {
            str(key): value for key, value in event.items() if isinstance(key, str)
        }
        if isinstance(context_events, list):
            context_events.append(dict(event_payload))
        try:
            from .workflow_baseline_telemetry import record_mutation_guardrail_event

            record_mutation_guardrail_event(event_payload)
        except Exception:
            pass
