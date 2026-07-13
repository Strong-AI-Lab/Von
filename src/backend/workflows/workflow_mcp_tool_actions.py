"""Generic durable workflow action for bounded internal MCP invocation.

This module is intentionally domain-agnostic. Workflows name the internal MCP
method and payload; Python provides only the reusable invocation, validation,
namespace propagation, and write-guardrail surface.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from .action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from .mcp_tool_bridge import (
    apply_runtime_defaults_to_mcp_payload,
    mcp_input_schema_declares_field,
    resolve_internal_mcp_tool_name,
    workflow_action_result_from_mcp_payload,
)
from .workflow_side_effect_guardrails import enforce_workflow_mcp_write_guardrails
from .write_tool_policy import (
    WRITE_RISK_DESTRUCTIVE,
    classify_write_tool_risk,
    normalise_workflow_execution_side_effect_policy,
    normalise_workflow_step_mutation_authority_spec,
)
from ..services.tool_target_contract_validation import (
    target_contract_state_from_context,
    validate_tool_target_contract,
)

logger = logging.getLogger(__name__)

WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID = "workflow_mcp.invoke_tool"

_TOOL_NAME_INPUT_KEYS: tuple[str, ...] = (
    "tool_name",
    "method_name",
    "mcp_tool",
    "mcp_method",
    "tool",
)
_PAYLOAD_INPUT_KEYS: tuple[str, ...] = (
    "tool_arguments",
    "arguments",
    "payload",
    "tool_payload",
    "mcp_payload",
)
_CONTROL_INPUT_KEYS: frozenset[str] = frozenset(
    (*_TOOL_NAME_INPUT_KEYS, *_PAYLOAD_INPUT_KEYS)
)
_WRITE_POLICY_METADATA_KEYS: tuple[str, ...] = (
    "workflow_execution_side_effect_policy",
    "side_effect_policy",
    "sandbox_policy",
)
_READ_CATEGORIES: frozenset[str] = frozenset({"", "read", "read_only", "readonly"})
_WRITE_CATEGORIES: frozenset[str] = frozenset(
    {"write", "additive", "mutating", "mutative", "destructive"}
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _normalise_string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        cleaned = value.strip()
        return (cleaned,) if cleaned else ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _clean_text(item)
        lowered = cleaned.lower()
        if not cleaned or lowered in seen:
            continue
        seen.add(lowered)
        items.append(cleaned)
    return tuple(items)


def _extract_static_tool_name(inputs: Mapping[str, Any]) -> str | None:
    for key in _TOOL_NAME_INPUT_KEYS:
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tool_name_is_context_bound(inputs: Mapping[str, Any]) -> bool:
    for key in _TOOL_NAME_INPUT_KEYS:
        value = inputs.get(key)
        if isinstance(value, Mapping) and (
            value.get("$context_key") or value.get("context_key")
        ):
            return True
    return False


def _explicit_allowed_tool_names(
    *,
    inputs: Mapping[str, Any],
    state_metadata: Mapping[str, Any],
) -> tuple[str, ...]:
    for source in (state_metadata, inputs):
        for key in (
            "workflow_mcp_allowed_tools",
            "allowed_mcp_tools",
            "allowed_tool_names",
            "tool_allowlist",
        ):
            names = _normalise_string_list(source.get(key))
            if names:
                return names
    return ()


def _normalise_tool_payload(inputs: Mapping[str, Any]) -> dict[str, Any]:
    for key in _PAYLOAD_INPUT_KEYS:
        value = inputs.get(key)
        if isinstance(value, Mapping):
            return {
                str(payload_key): payload_value
                for payload_key, payload_value in value.items()
                if isinstance(payload_key, str) and str(payload_key).strip()
            }

    return {
        str(key): value
        for key, value in inputs.items()
        if isinstance(key, str)
        and str(key).strip()
        and str(key).strip() not in _CONTROL_INPUT_KEYS
    }


def _has_explicit_write_policy(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("mutation_authority") is not None:
        return normalise_workflow_step_mutation_authority_spec(
            metadata.get("mutation_authority")
        ) is not None

    for key in _WRITE_POLICY_METADATA_KEYS:
        if normalise_workflow_execution_side_effect_policy(metadata.get(key)) is not None:
            return True
    return False


@lru_cache(maxsize=1)
def _default_mcp_method_metadata_by_name() -> dict[str, dict[str, Any]]:
    try:
        from ..integrations.internal_mcp.tool_contract_registry import (
            get_canonical_tool_registry,
        )

        metadata: dict[str, dict[str, Any]] = {}
        for name, contract in get_canonical_tool_registry().items():
            method_name = _clean_text(getattr(contract, "internal_method_name", None))
            if not method_name:
                continue
            metadata[method_name] = {
                "name": method_name,
                "category": _clean_text(getattr(contract, "category", None)) or "read",
                "requires_namespace": bool(
                    getattr(contract, "requires_namespace", False)
                ),
                "family": _clean_text(getattr(contract, "family", None)) or None,
            }
        return metadata
    except Exception:
        logger.debug("Could not load internal MCP method metadata", exc_info=True)
        return {}


def _resolve_method_metadata(
    *,
    requested_tool_name: str,
    method_metadata_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str | None, Mapping[str, Any] | None]:
    metadata_by_name = (
        method_metadata_by_name
        if isinstance(method_metadata_by_name, Mapping)
        else _default_mcp_method_metadata_by_name()
    )
    available_names = tuple(
        str(name)
        for name in metadata_by_name.keys()
        if isinstance(name, str) and str(name).strip()
    )
    resolved_name = resolve_internal_mcp_tool_name(
        requested_tool_name,
        available_tool_names=available_names,
    )
    if not resolved_name:
        return None, None
    metadata = metadata_by_name.get(resolved_name)
    return resolved_name, metadata if isinstance(metadata, Mapping) else None


def validate_workflow_mcp_invocation_contract(
    *,
    state_id: str,
    action_id: str,
    action_inputs: Mapping[str, Any],
    state_metadata: Mapping[str, Any],
    method_metadata_by_name: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return contract issues for a ``workflow_mcp.invoke_tool`` step."""

    if action_id != WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID:
        return []

    issues: list[dict[str, Any]] = []
    tool_name = _extract_static_tool_name(action_inputs)
    allowed_tools = _explicit_allowed_tool_names(
        inputs=action_inputs,
        state_metadata=state_metadata,
    )

    if not tool_name:
        reason_code = (
            "mcp_tool_name_context_bound"
            if _tool_name_is_context_bound(action_inputs)
            else "mcp_tool_name_missing"
        )
        issues.append(
            {
                "state_id": state_id,
                "action_id": action_id,
                "reason_code": reason_code,
            }
        )
        return issues

    if allowed_tools and tool_name not in allowed_tools:
        issues.append(
            {
                "state_id": state_id,
                "action_id": action_id,
                "tool_name": tool_name,
                "reason_code": "mcp_tool_not_in_step_allowlist",
                "allowed_tools": list(allowed_tools),
            }
        )

    resolved_name, method_metadata = _resolve_method_metadata(
        requested_tool_name=tool_name,
        method_metadata_by_name=method_metadata_by_name,
    )
    if not resolved_name or method_metadata is None:
        issues.append(
            {
                "state_id": state_id,
                "action_id": action_id,
                "tool_name": tool_name,
                "reason_code": "mcp_tool_not_registered",
            }
        )
        return issues

    category = _clean_text(method_metadata.get("category")).lower()
    if category in _READ_CATEGORIES:
        return issues
    if category not in _WRITE_CATEGORIES:
        issues.append(
            {
                "state_id": state_id,
                "action_id": action_id,
                "tool_name": tool_name,
                "resolved_tool_name": resolved_name,
                "category": category,
                "reason_code": "mcp_tool_category_unsupported",
            }
        )
        return issues

    if not _has_explicit_write_policy(state_metadata):
        risk_class = classify_write_tool_risk(resolved_name)
        issues.append(
            {
                "state_id": state_id,
                "action_id": action_id,
                "tool_name": tool_name,
                "resolved_tool_name": resolved_name,
                "category": category,
                "risk_class": risk_class,
                "reason_code": (
                    "mcp_destructive_write_policy_missing"
                    if risk_class == WRITE_RISK_DESTRUCTIVE
                    else "mcp_write_policy_missing"
                ),
            }
        )

    return issues


def _resolve_gateway(request: WorkflowActionRequest) -> Any:
    gateway = getattr(request.environment, "gateway", None)
    if gateway is not None:
        return gateway

    from .durable.registry_factory import _get_or_build_durable_mcp_gateway

    return _get_or_build_durable_mcp_gateway()


def _runtime_write_policy_missing(
    *,
    request: WorkflowActionRequest,
    method_definition: Any,
) -> bool:
    category = _clean_text(getattr(method_definition, "category", None)).lower()
    if category not in _WRITE_CATEGORIES:
        return False
    metadata = (
        request.workflow_state_metadata
        if isinstance(request.workflow_state_metadata, Mapping)
        else {}
    )
    return not _has_explicit_write_policy(metadata)


def _handle_workflow_mcp_invoke_tool(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    requested_tool_name = _extract_static_tool_name(inputs)
    if not requested_tool_name:
        return WorkflowActionResult(
            status="failed",
            error="workflow_mcp_tool_name_missing",
        )

    payload = _normalise_tool_payload(inputs)

    try:
        gateway = _resolve_gateway(request)
        available_tool_names = tuple(gateway.describe_methods().keys())
        resolved_tool_name = (
            resolve_internal_mcp_tool_name(
                requested_tool_name,
                available_tool_names=available_tool_names,
            )
            or requested_tool_name
        )
        method_definition = gateway.get_method_definition(resolved_tool_name)
        if method_definition is None:
            return WorkflowActionResult(
                status="failed",
                error=f"workflow_mcp_tool_not_registered:{requested_tool_name}",
                outputs={
                    "mcp_tool": requested_tool_name,
                    "mcp_requested_tool": requested_tool_name,
                },
            )

        apply_runtime_defaults_to_mcp_payload(
            payload,
            tool_name=resolved_tool_name,
            input_schema=getattr(method_definition, "input_schema", None),
            user_namespace=getattr(request.environment, "user_namespace", None),
            default_gmail_profile=getattr(
                request.environment, "default_gmail_profile", None
            ),
        )
        input_schema = getattr(method_definition, "input_schema", None)
        authoritative_actor_fields = {
            "user_id": getattr(request.environment, "user_concept_id", None),
            "user_concept_id": getattr(
                request.environment,
                "user_concept_id",
                None,
            ),
            "org_id": getattr(request.environment, "org_concept_id", None),
            "organisation_concept_id": getattr(
                request.environment,
                "org_concept_id",
                None,
            ),
            "namespace": getattr(request.environment, "user_namespace", None),
            "user_namespace": getattr(
                request.environment,
                "user_namespace",
                None,
            ),
        }
        for field_name, field_value in authoritative_actor_fields.items():
            canonical_schema_less_field = field_name in {
                "user_concept_id",
                "org_id",
                "namespace",
            }
            if not (
                (input_schema is None and canonical_schema_less_field)
                or mcp_input_schema_declares_field(input_schema, field_name)
            ):
                # Undeclared aliases are removed even for permissive schemas:
                # otherwise a handler could prefer a forged duplicate over the
                # authoritative field projected by the workflow environment.
                payload.pop(field_name, None)
                continue
            if field_value is None:
                continue
            payload[field_name] = field_value

        if _runtime_write_policy_missing(
            request=request,
            method_definition=method_definition,
        ):
            return WorkflowActionResult(
                status="failed",
                error=f"workflow_mcp_write_policy_missing:{resolved_tool_name}",
                outputs={
                    "mcp_tool": resolved_tool_name,
                    "mcp_requested_tool": requested_tool_name,
                    "mcp_resolved_tool": resolved_tool_name,
                    "mutation_guardrail_blocked": True,
                    "write_policy_reason": "workflow_mcp_write_policy_missing",
                },
            )

        blocked_result = enforce_workflow_mcp_write_guardrails(
            request=request,
            resolved_tool_name=resolved_tool_name,
            method_definition=method_definition,
        )
        if blocked_result is not None:
            return blocked_result

        target_validation = validate_tool_target_contract(
            tool_name=resolved_tool_name,
            payload=payload,
            target_contract_state=target_contract_state_from_context(request.data),
            prior_tool_invocations=(
                request.data.get("invocations")
                if isinstance(request.data.get("invocations"), Sequence)
                and not isinstance(
                    request.data.get("invocations"), (str, bytes, bytearray)
                )
                else ()
            ),
        )
        if not target_validation.ok:
            error_code = target_validation.first_error_code()
            return WorkflowActionResult(
                status="failed",
                error=(
                    f"workflow_mcp_target_contract_validation_failed:"
                    f"{resolved_tool_name}:{error_code or 'invalid_target'}"
                ),
                outputs={
                    "mcp_tool": resolved_tool_name,
                    "mcp_requested_tool": requested_tool_name,
                    "mcp_resolved_tool": resolved_tool_name,
                    "target_contract_validation_failed": True,
                    "target_contract_validation_error_code": error_code,
                    "tool_call_validation_diagnostics": list(
                        target_validation.diagnostics
                    ),
                },
            )

        from ..security.access_control import override_current_actor

        with override_current_actor(
            getattr(request.environment, "user_concept_id", None),
            getattr(request.environment, "org_concept_id", None),
        ):
            result = gateway.invoke(resolved_tool_name, payload)
        action_result = workflow_action_result_from_mcp_payload(
            tool_name=resolved_tool_name,
            payload=result.payload,
            duration_ms=result.duration_ms,
        )
        action_result.outputs["mcp_requested_tool"] = requested_tool_name
        action_result.outputs["mcp_resolved_tool"] = resolved_tool_name
        action_result.outputs["workflow_actor_scope_enforced"] = True
        return action_result
    except Exception as exc:
        logger.warning(
            "[workflow_mcp] invoke failed for %s: %s",
            requested_tool_name,
            exc,
        )
        return WorkflowActionResult(
            status="failed",
            error=f"workflow_mcp_invoke_failed:{requested_tool_name}:{exc}",
        )


def register_workflow_mcp_tool_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
            handler=_handle_workflow_mcp_invoke_tool,
            description=(
                "Invoke a bounded internal MCP method from a VWL workflow. The "
                "workflow supplies the static tool_name and tool_arguments; "
                "the action performs namespace propagation, gateway invocation, "
                "structured output handback, and write guardrail enforcement."
            ),
            input_schema={
                "type": "object",
                "required": ["tool_name"],
                "properties": {
                    "tool_name": {"type": "string"},
                    "tool_arguments": {"type": "object"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "mcp_tool": {"type": "string"},
                    "mcp_requested_tool": {"type": "string"},
                    "mcp_resolved_tool": {"type": "string"},
                    "mcp_duration_ms": {"type": "number"},
                    "mcp_result": {"type": "object"},
                    "result": {"type": "object"},
                },
            },
            side_effects="read_or_guarded_write",
        )
    )


__all__ = [
    "WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID",
    "register_workflow_mcp_tool_actions",
    "validate_workflow_mcp_invocation_contract",
]
