"""Forward inference and planning durable workflow (JVNAUTOSCI-924).

This workflow provides a forward-looking planning layer that generates
concrete next actions from a goal and current context, including:
1. tool call recommendations, and
2. workflow invocation recommendations.

The workflow is intentionally trace-friendly: each stage emits structured
diagnostic outputs so planning quality can be inspected and improved over
time without relying on free-form logs.

States: assess -> infer -> validate -> complete | failed
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Sequence

from ...services.prompt_template_service import PromptTemplateService
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
    WORKFLOW_STEP_EXECUTION_MODE_LLM,
)
from ..workflow_registry import WorkflowRegistration

logger = logging.getLogger(__name__)

PLANNING_WORKFLOW_ID = "#V#planning_workflow"
DEFAULT_MAX_ACTIONS = 6
DEFAULT_PLANNING_HORIZON = 3
DEFAULT_PROMPT_CONCEPT_ID = "#V#forward_inference_planning_prompt_v1"

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(?P<body>\{[\s\S]*\}|\[[\s\S]*\])\s*```",
    re.IGNORECASE,
)

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalise_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_str_list(value: Any, *, max_items: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for raw in value[:max_items]:
        text = _normalise_text(raw)
        if text:
            items.append(text)
    return items


def _coerce_mapping_list(value: Any, *, max_items: int = 50) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value[:max_items]:
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return rows


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _append_trace_event(
    context: Mapping[str, Any],
    *,
    stage: str,
    event: str,
    details: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    trace = context.get("planning_trace")
    if isinstance(trace, list):
        existing = [item for item in trace if isinstance(item, dict)]
    else:
        existing = []

    row: dict[str, Any] = {
        "timestamp_utc": _utc_now_iso(),
        "stage": stage,
        "event": event,
    }
    if isinstance(details, Mapping):
        row["details"] = dict(details)
    existing.append(row)
    return existing


def _collect_available_tool_names() -> list[str]:
    """Return tool names currently exposed by the internal MCP catalogue."""
    try:
        from ...integrations.internal_mcp.catalogue import build_default_catalogue

        catalogue = build_default_catalogue()
        methods = catalogue.list_methods()
        if not isinstance(methods, Sequence):
            return []
        return sorted(
            {
                str(name).strip()
                for name in methods
                if isinstance(name, str) and name.strip()
            }
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[planning_workflow] tool inventory lookup failed: %s", exc)
        return []


def _collect_available_workflow_ids() -> list[str]:
    """Return workflow IDs currently runnable via the unified registry."""
    try:
        # Local import prevents module-level circular imports with registry_factory.
        from .registry_factory import build_workflow_registry_read_only

        registry = build_workflow_registry_read_only()
        return sorted(
            {
                str(workflow_id).strip()
                for workflow_id in registry.all_workflow_ids()
                if isinstance(workflow_id, str) and workflow_id.strip()
            }
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[planning_workflow] workflow inventory lookup failed: %s", exc)
        return []


def _resolve_planning_prompt(context: Mapping[str, Any]) -> str | None:
    """Resolve planning prompt text from Vontology or an inline override."""
    inline_prompt = _normalise_text(context.get("prompt_template"))
    if inline_prompt:
        return inline_prompt

    prompt_concept_id = _normalise_text(context.get("prompt_concept_id"))
    if not prompt_concept_id:
        prompt_concept_id = DEFAULT_PROMPT_CONCEPT_ID

    try:
        prompt_service = PromptTemplateService(default_max_chars=6000)
        _resolved_prompt_id, prompt_text = prompt_service.resolve_prompt_text(
            [prompt_concept_id],
            fallback=None,
            max_chars=6000,
        )
        if isinstance(prompt_text, str) and prompt_text.strip():
            return prompt_text.strip()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[planning_workflow] prompt concept lookup failed: %s", exc)
    return None


def _extract_json_payload(raw_text: str) -> tuple[Any, str]:
    """Best-effort JSON extraction from raw LLM text."""
    cleaned = str(raw_text or "").strip()
    if not cleaned:
        return None, "empty"

    try:
        return json.loads(cleaned), "strict_json"
    except Exception:
        pass

    fence_match = _JSON_FENCE_RE.search(cleaned)
    if fence_match:
        candidate = fence_match.group("body")
        try:
            return json.loads(candidate), "fenced_json"
        except Exception:
            pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate), "braced_json"
        except Exception:
            pass

    return None, "unparsed"


def _normalise_action_kind(value: Any) -> str:
    normalised = str(value or "").strip().lower()
    if normalised in {"tool_call", "tool"}:
        return "tool_call"
    if normalised in {"workflow_invocation", "workflow_call", "workflow"}:
        return "workflow_invocation"
    if normalised in {"response", "answer"}:
        return "response"
    return "analysis"


def _coerce_planning_actions(raw_actions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_actions, list):
        return []

    actions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_actions):
        if not isinstance(raw, Mapping):
            continue
        action_id = _normalise_text(raw.get("id")) or f"a{index + 1}"
        payload_value = raw.get("payload")
        if payload_value is None and isinstance(raw.get("inputs"), Mapping):
            payload_value = raw.get("inputs")
        payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
        action = {
            "id": action_id,
            "title": _normalise_text(raw.get("title")) or f"Step {index + 1}",
            "kind": _normalise_action_kind(raw.get("kind")),
            "tool_name": _normalise_text(raw.get("tool_name")),
            "workflow_id": _normalise_text(raw.get("workflow_id")),
            "payload": payload,
            "dependencies": _coerce_str_list(raw.get("dependencies"), max_items=8),
            "rationale": _normalise_text(raw.get("rationale")),
            "expected_outcome": _normalise_text(raw.get("expected_outcome")),
        }
        actions.append(action)
    return actions


def _handle_assess_context(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Collect planning inputs and execution inventory for forward inference."""
    context = request.data
    goal = (
        _normalise_text(context.get("goal"))
        or _normalise_text(context.get("objective"))
        or _normalise_text(context.get("prompt"))
        or _normalise_text(context.get("user_prompt"))
    )
    if not goal:
        return WorkflowActionResult(
            status="failed",
            error="goal_required: provide goal/objective/prompt for planning",
        )

    max_actions = _coerce_int(
        context.get("max_actions"),
        default=DEFAULT_MAX_ACTIONS,
        minimum=1,
        maximum=12,
    )
    planning_horizon = _coerce_int(
        context.get("planning_horizon"),
        default=DEFAULT_PLANNING_HORIZON,
        minimum=1,
        maximum=10,
    )
    constraints = _coerce_str_list(context.get("constraints"))

    available_tools = context.get("available_tool_names")
    if not isinstance(available_tools, list):
        available_tools = _collect_available_tool_names()
    else:
        available_tools = _coerce_str_list(available_tools, max_items=600)

    available_workflows = context.get("available_workflow_ids")
    if not isinstance(available_workflows, list):
        available_workflows = _collect_available_workflow_ids()
    else:
        available_workflows = _coerce_str_list(available_workflows, max_items=600)

    planning_context = {
        "goal": goal,
        "max_actions": max_actions,
        "planning_horizon": planning_horizon,
        "constraints": constraints,
        "strict_validation": bool(context.get("strict_validation", False)),
    }
    planning_inventory = {
        "available_tool_names": available_tools,
        "available_workflow_ids": available_workflows,
    }
    planning_diagnostics = {
        "inventory": {
            "tool_count": len(available_tools),
            "workflow_count": len(available_workflows),
        }
    }

    trace = _append_trace_event(
        context,
        stage="assess",
        event="planning_context_ready",
        details={
            "goal": goal,
            "tool_count": len(available_tools),
            "workflow_count": len(available_workflows),
        },
    )

    return WorkflowActionResult(
        outputs={
            "planning_context": planning_context,
            "planning_inventory": planning_inventory,
            "planning_diagnostics": planning_diagnostics,
            "planning_prompt_text": _resolve_planning_prompt(context),
            "planning_prompt_concept_id": (
                _normalise_text(context.get("prompt_concept_id"))
                or DEFAULT_PROMPT_CONCEPT_ID
            ),
            "planning_trace": trace,
        }
    )


def _build_inference_prompt(context: Mapping[str, Any]) -> str | None:
    planning_context = context.get("planning_context", {})
    planning_inventory = context.get("planning_inventory", {})
    prompt_template = _normalise_text(context.get("planning_prompt_text"))
    if not prompt_template:
        prompt_template = _resolve_planning_prompt(context)
    if not prompt_template:
        return None
    payload = {
        "goal": (
            planning_context.get("goal")
            if isinstance(planning_context, Mapping)
            else context.get("goal")
        ),
        "planning_horizon": (
            planning_context.get("planning_horizon")
            if isinstance(planning_context, Mapping)
            else context.get("planning_horizon")
        ),
        "max_actions": (
            planning_context.get("max_actions")
            if isinstance(planning_context, Mapping)
            else context.get("max_actions")
        ),
        "constraints": (
            planning_context.get("constraints")
            if isinstance(planning_context, Mapping)
            else context.get("constraints")
        ),
        "available_tool_names": (
            planning_inventory.get("available_tool_names")
            if isinstance(planning_inventory, Mapping)
            else context.get("available_tool_names")
        ),
        "available_workflow_ids": (
            planning_inventory.get("available_workflow_ids")
            if isinstance(planning_inventory, Mapping)
            else context.get("available_workflow_ids")
        ),
        "context_summary": context.get("context_summary"),
    }

    return (
        f"{prompt_template}\n\n"
        "Planning input (JSON):\n"
        f"{json.dumps(payload, ensure_ascii=True, indent=2)}\n\n"
        "Return JSON only."
    )


def _handle_infer_plan(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Infer candidate forward actions via LLM and parse as structured plan."""
    context = request.data
    llm = request.environment.llm_client
    if llm is None:
        from ...languagemodels.llm_interface import get_llm_client

        llm = get_llm_client()

    prompt = _build_inference_prompt(context)
    if not prompt:
        diagnostics = dict(context.get("planning_diagnostics", {}))
        diagnostics["prompt"] = {
            "available": False,
            "prompt_concept_id": (
                _normalise_text(context.get("planning_prompt_concept_id"))
                or _normalise_text(context.get("prompt_concept_id"))
                or DEFAULT_PROMPT_CONCEPT_ID
            ),
        }
        return WorkflowActionResult(
            status="failed",
            error="planning_prompt_unavailable",
            outputs={"planning_diagnostics": diagnostics},
        )
    raw_response = llm.generate(prompt, llm_params={"max_tokens": 1600})
    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    if not isinstance(parsed_payload, Mapping):
        return WorkflowActionResult(
            status="failed",
            error=f"planning_json_parse_failed:{parse_mode}",
        )

    actions = _coerce_planning_actions(parsed_payload.get("actions"))
    if not actions:
        fallback_goal = (
            _normalise_text(parsed_payload.get("goal"))
            or _normalise_text((context.get("planning_context") or {}).get("goal"))
            or "Clarify objective"
        )
        actions = [
            {
                "id": "a1",
                "title": f"Clarify and decompose goal: {fallback_goal}",
                "kind": "analysis",
                "tool_name": None,
                "workflow_id": None,
                "payload": {},
                "dependencies": [],
                "rationale": "No actionable plan was produced by the model.",
                "expected_outcome": "A concrete plan with executable actions.",
            }
        ]

    assumptions = _coerce_str_list(parsed_payload.get("assumptions"))
    planning_goal = _normalise_text(parsed_payload.get("goal")) or _normalise_text(
        (context.get("planning_context") or {}).get("goal")
    )

    trace = _append_trace_event(
        context,
        stage="infer",
        event="plan_inferred",
        details={
            "parse_mode": parse_mode,
            "actions_count": len(actions),
        },
    )

    diagnostics = dict(context.get("planning_diagnostics", {}))
    diagnostics["inference"] = {
        "parse_mode": parse_mode,
        "actions_count": len(actions),
        "assumptions_count": len(assumptions),
        "response_preview": str(raw_response or "")[:300],
    }

    return WorkflowActionResult(
        outputs={
            "planning_goal": planning_goal,
            "planning_assumptions": assumptions,
            "planning_actions": actions,
            "planning_raw_plan": dict(parsed_payload),
            "planning_trace": trace,
            "planning_diagnostics": diagnostics,
        }
    )


def _build_ready_payload(action: Mapping[str, Any]) -> dict[str, Any]:
    kind = _normalise_action_kind(action.get("kind"))
    payload = dict(action.get("payload", {}) or {})
    if kind == "tool_call":
        return {
            "kind": kind,
            "tool_name": _normalise_text(action.get("tool_name")),
            "payload": payload,
        }
    if kind == "workflow_invocation":
        return {
            "kind": kind,
            "workflow_id": _normalise_text(action.get("workflow_id")),
            "inputs": payload,
        }
    return {
        "kind": kind,
        "instruction": _normalise_text(action.get("title")) or "Review next step",
        "metadata": payload,
    }


def _handle_validate_plan(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Validate actionability of inferred actions against live inventory."""
    context = request.data
    planning_context = _coerce_mapping(context.get("planning_context"))
    inventory = _coerce_mapping(context.get("planning_inventory"))
    actions = _coerce_planning_actions(context.get("planning_actions"))
    max_actions = _coerce_int(
        planning_context.get("max_actions"),
        default=DEFAULT_MAX_ACTIONS,
        minimum=1,
        maximum=12,
    )
    strict_validation = bool(planning_context.get("strict_validation", False))

    available_tools = set(
        _coerce_str_list(inventory.get("available_tool_names"), max_items=1200)
    )
    available_workflows = set(
        _coerce_str_list(inventory.get("available_workflow_ids"), max_items=1200)
    )

    retained_actions = actions[:max_actions]
    known_action_ids = {str(action.get("id")) for action in retained_actions}
    validated_actions: list[dict[str, Any]] = []
    actionable_actions: list[dict[str, Any]] = []

    for action in retained_actions:
        kind = _normalise_action_kind(action.get("kind"))
        issues: list[str] = []
        actionable = True

        if kind == "tool_call":
            tool_name = _normalise_text(action.get("tool_name"))
            if not tool_name:
                actionable = False
                issues.append("missing_tool_name")
            elif tool_name not in available_tools:
                actionable = False
                issues.append(f"unknown_tool:{tool_name}")
        elif kind == "workflow_invocation":
            workflow_id = _normalise_text(action.get("workflow_id"))
            if not workflow_id:
                actionable = False
                issues.append("missing_workflow_id")
            elif workflow_id not in available_workflows:
                actionable = False
                issues.append(f"unknown_workflow:{workflow_id}")

        dependencies = _coerce_str_list(action.get("dependencies"), max_items=8)
        valid_dependencies = [
            dep for dep in dependencies if dep in known_action_ids and dep != action.get("id")
        ]
        if len(valid_dependencies) != len(dependencies):
            issues.append("dropped_unknown_dependencies")

        validated = dict(action)
        validated["kind"] = kind
        validated["dependencies"] = valid_dependencies
        validated["actionable"] = actionable
        validated["validation_issues"] = issues
        validated["ready_payload"] = _build_ready_payload(validated)
        validated_actions.append(validated)
        if actionable:
            actionable_actions.append(validated)

    total_actions = len(validated_actions)
    non_actionable = total_actions - len(actionable_actions)
    passed = non_actionable == 0 or not strict_validation

    trace = _append_trace_event(
        context,
        stage="validate",
        event="plan_validated",
        details={
            "total_actions": total_actions,
            "actionable_actions": len(actionable_actions),
            "non_actionable_actions": non_actionable,
            "strict_validation": strict_validation,
            "passed": passed,
        },
    )

    diagnostics = _coerce_mapping(context.get("planning_diagnostics"))
    diagnostics["validation"] = {
        "total_actions": total_actions,
        "actionable_actions": len(actionable_actions),
        "non_actionable_actions": non_actionable,
        "strict_validation": strict_validation,
        "passed": passed,
    }

    outputs = {
        "validated_planning_actions": validated_actions,
        "actionable_next_actions": actionable_actions,
        "planning_validation": diagnostics["validation"],
        "planning_trace": trace,
        "planning_diagnostics": diagnostics,
    }
    if not passed:
        return WorkflowActionResult(
            status="failed",
            error="planning_validation_failed:non_actionable_actions_present",
            outputs=outputs,
        )
    return WorkflowActionResult(outputs=outputs)


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Build final workflow output payload for downstream execution/debugging."""
    context = request.data
    planning_context = _coerce_mapping(context.get("planning_context"))
    planning_validation = _coerce_mapping(context.get("planning_validation"))
    validated_actions = _coerce_mapping_list(context.get("validated_planning_actions"))
    actionable_actions = _coerce_mapping_list(context.get("actionable_next_actions"))
    planning_trace = _coerce_mapping_list(context.get("planning_trace"), max_items=200)

    planning_result = {
        "success": bool(planning_validation.get("passed", True)),
        "goal": _normalise_text(context.get("planning_goal"))
        or _normalise_text(planning_context.get("goal")),
        "assumptions": _coerce_str_list(context.get("planning_assumptions")),
        "total_actions": len(validated_actions),
        "actionable_actions": len(actionable_actions),
        "validated_actions": validated_actions,
        "next_actions": actionable_actions,
        "validation": planning_validation,
        "trace": planning_trace,
        "generated_at_utc": _utc_now_iso(),
    }

    return WorkflowActionResult(
        outputs={
            "planning_result": planning_result,
        }
    )


def build_planning_workflow_test_definition() -> WorkflowDefinition:
    """Build the forward inference and planning workflow definition."""
    assess = WorkflowStateSpec(
        state_id="assess",
        actions=(
            WorkflowActionInvocation(
                action_id="planning.assess_context",
                description="Collect goal, constraints, and runnable action inventory.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="infer",
                condition=lambda ctx: bool(ctx.get("planning_context")),
                reason="context_ready",
            ),
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: True,
                reason="missing_context",
            ),
        ),
    )

    infer = WorkflowStateSpec(
        state_id="infer",
        actions=(
            WorkflowActionInvocation(
                action_id="planning.infer_plan",
                description="Infer forward actions from context and inventory.",
                execution_mode=WORKFLOW_STEP_EXECUTION_MODE_LLM,
                llm_policy={
                    "tool_mode": "none",
                    "policy_stage": "planning",
                    "prompt_text_context_key": "planning_prompt_text",
                    "response_contract_text": "Return JSON only.",
                    "context_fields": [
                        {
                            "label": "Planning input (JSON)",
                            "context_key": "planning_context",
                        },
                        {
                            "label": "Available execution inventory (JSON)",
                            "context_key": "planning_inventory",
                        },
                        {
                            "label": "Context summary",
                            "context_key": "context_summary",
                        },
                    ],
                },
                validation_policy={"output_format": "planning_plan_json"},
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="validate",
                condition=lambda ctx: True,
                reason="plan_inferred",
            ),
        ),
    )

    validate = WorkflowStateSpec(
        state_id="validate",
        actions=(
            WorkflowActionInvocation(
                action_id="planning.validate_plan",
                description="Validate inferred actions for concrete executability.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda ctx: bool(ctx.get("planning_validation")),
                reason="validated",
            ),
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: True,
                reason="validation_missing",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="planning.finalise",
                description="Publish final planning result payload.",
            ),
        ),
        terminal=True,
    )

    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=PLANNING_WORKFLOW_ID,
        initial_state="assess",
        states={
            "assess": assess,
            "infer": infer,
            "validate": validate,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Forward inference workflow that proposes concrete, validated next actions "
            "including tool calls and workflow invocations."
        ),
    )


def build_planning_workflow_test_registration() -> WorkflowRegistration:
    """Return registration for the built-in planning workflow."""
    return WorkflowRegistration(
        workflow_id=PLANNING_WORKFLOW_ID,
        definition=build_planning_workflow_test_definition(),
        purpose=(
            "Forward inference workflow that proposes concrete, validated next actions "
            "including tool calls and workflow invocations."
        ),
        source="built_in",
    )


def register_planning_actions(registry: ActionRegistry) -> None:
    """Register planning workflow action handlers."""
    specs = [
        ActionSpec(
            action_id="planning.assess_context",
            handler=_handle_assess_context,
            description="Collect planning goal, constraints, and runtime inventory.",
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="planning.infer_plan",
            handler=_handle_infer_plan,
            description="Infer concrete forward actions with LLM support.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="planning.validate_plan",
            handler=_handle_validate_plan,
            description="Validate actionability of inferred planning steps.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="planning.finalise",
            handler=_handle_finalise,
            description="Emit final structured planning output.",
            side_effects="none",
        ),
    ]
    for spec in specs:
        try:
            registry.register(spec)
        except ValueError:
            pass  # Idempotent behaviour for hot reload paths.
