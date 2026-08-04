"""Shared helpers for surfacing MCP tool results into workflow actions."""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from .action_registry import WorkflowActionResult
from ..integrations.internal_mcp.schemas import (
    coerce_payload_types,
    normalise_payload_aliases,
)

logger = logging.getLogger(__name__)


def _coerce_mcp_error_message(*, tool_name: str, payload: Mapping[str, Any]) -> str:
    error_text = str(
        payload.get("error_code")
        or payload.get("error")
        or payload.get("message")
        or ""
    ).strip()
    return error_text or f"mcp_tool_error:{tool_name}"


def candidate_internal_mcp_tool_names(action_id: str | None) -> tuple[str, ...]:
    """Return plausible internal MCP tool names for a workflow action ID."""

    action_text = str(action_id or "").strip()
    if not action_text:
        return ()

    candidates: list[str] = [action_text]
    underscored = action_text.replace(".", "_")
    if underscored and underscored not in candidates:
        candidates.append(underscored)
    return tuple(candidates)


def resolve_internal_mcp_tool_name(
    requested_name: str | None,
    *,
    available_tool_names: Sequence[str] | None = None,
) -> str | None:
    """Resolve the best available internal MCP tool name for ``requested_name``."""

    candidates = candidate_internal_mcp_tool_names(requested_name)
    if not candidates:
        return None
    if not available_tool_names:
        return candidates[0]

    available = {
        str(name or "").strip()
        for name in available_tool_names
        if str(name or "").strip()
    }
    for candidate in candidates:
        if candidate in available:
            return candidate
    return None


def mcp_input_schema_accepts_namespace(input_schema: Any) -> bool:
    """Return whether an MCP input schema can receive ``namespace``.

    Namespace is runtime tenancy context, not a universal tool argument. Strict
    tool schemas that do not declare it must not receive it, otherwise gateway
    validation fails before the handler can run.
    """

    if input_schema is None:
        return True
    if bool(getattr(input_schema, "allow_unknown", False)):
        return True
    if isinstance(input_schema, Mapping) and (
        bool(input_schema.get("allow_unknown"))
        or bool(input_schema.get("additionalProperties"))
    ):
        return True
    properties = (
        input_schema.get("properties") if isinstance(input_schema, Mapping) else None
    )
    if isinstance(properties, Mapping) and "namespace" in properties:
        return True

    required = (
        input_schema.get("required")
        if isinstance(input_schema, Mapping)
        else getattr(input_schema, "required", None)
    )
    optional = (
        input_schema.get("optional")
        if isinstance(input_schema, Mapping)
        else getattr(input_schema, "optional", None)
    )
    return (
        (isinstance(required, Mapping) and "namespace" in required)
        or (
            isinstance(required, Sequence)
            and not isinstance(required, (str, bytes, bytearray))
            and "namespace" in required
        )
        or (isinstance(optional, Mapping) and "namespace" in optional)
        or (
            isinstance(optional, Sequence)
            and not isinstance(optional, (str, bytes, bytearray))
            and "namespace" in optional
        )
    )


def _mcp_schema_fields(input_schema: Any) -> set[str]:
    if input_schema is None:
        return set()

    fields: set[str] = set()
    properties = (
        input_schema.get("properties") if isinstance(input_schema, Mapping) else None
    )
    if isinstance(properties, Mapping):
        fields.update(str(key) for key in properties.keys() if str(key).strip())

    required = (
        input_schema.get("required")
        if isinstance(input_schema, Mapping)
        else getattr(input_schema, "required", None)
    )
    optional = (
        input_schema.get("optional")
        if isinstance(input_schema, Mapping)
        else getattr(input_schema, "optional", None)
    )
    for value in (required, optional):
        if isinstance(value, Mapping):
            fields.update(str(key) for key in value.keys() if str(key).strip())
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            fields.update(str(item) for item in value if str(item).strip())
    return fields


def mcp_input_schema_accepts_field(input_schema: Any, field_name: str) -> bool:
    """Return whether an MCP input schema can receive ``field_name``."""

    cleaned_field_name = str(field_name or "").strip()
    if not cleaned_field_name:
        return False
    if input_schema is None:
        return True
    if bool(getattr(input_schema, "allow_unknown", False)):
        return True
    if isinstance(input_schema, Mapping) and (
        bool(input_schema.get("allow_unknown"))
        or bool(input_schema.get("additionalProperties"))
    ):
        return True
    return cleaned_field_name in _mcp_schema_fields(input_schema)


def mcp_input_schema_declares_field(input_schema: Any, field_name: str) -> bool:
    """Return whether ``field_name`` is explicitly declared by an MCP schema."""

    cleaned_field_name = str(field_name or "").strip()
    return bool(
        cleaned_field_name
        and input_schema is not None
        and cleaned_field_name in _mcp_schema_fields(input_schema)
    )


def mcp_method_accepts_namespace(method_definition: Any) -> bool:
    """Return whether a resolved MCP method can receive ``namespace``."""

    if method_definition is None:
        return False
    if isinstance(method_definition, Mapping):
        return mcp_input_schema_accepts_namespace(method_definition.get("input_schema"))
    return mcp_input_schema_accepts_namespace(
        getattr(method_definition, "input_schema", None)
    )


def apply_runtime_defaults_to_mcp_payload(
    payload: MutableMapping[str, Any],
    *,
    tool_name: str,
    input_schema: Any,
    user_namespace: str | None,
    default_gmail_profile: str | None,
    strip_unknown_fields: bool = False,
) -> list[dict[str, Any]]:
    """Apply generic runtime defaults and schema hygiene before MCP dispatch.

    ``strip_unknown_fields`` is an explicit boundary chosen by callers that are
    dispatching model-authored recovery payloads.  At that boundary, a declared
    schema is the dispatch allowlist even when the legacy schema otherwise
    permits unknown fields.  Ordinary callers keep the schema's native
    ``allow_unknown`` behaviour by leaving the flag false.
    """

    bindings: list[dict[str, Any]] = []
    schema_fields = _mcp_schema_fields(input_schema)

    if input_schema is not None:
        try:
            _, alias_warnings = normalise_payload_aliases(input_schema, payload)
        except Exception:
            alias_warnings = []
        for warning in alias_warnings:
            bindings.append(
                {
                    "field": "",
                    "source": "schema_alias",
                    "value_present": True,
                    "warning": warning,
                }
            )

    if str(tool_name or "").strip().startswith("gmail_") and mcp_input_schema_accepts_field(
        input_schema, "profile"
    ):
        default_profile = (
            str(default_gmail_profile or "").strip()
            if isinstance(default_gmail_profile, str)
            else ""
        )
        current_profile = payload.get("profile")
        current_profile_text = (
            str(current_profile or "").strip()
            if isinstance(current_profile, str)
            else ""
        )
        # Enforce the authoritative Gmail profile (request-selected, else the
        # configured default) over whatever was supplied, INCLUDING an explicit
        # profile the model chose itself. Which Gmail account to use is an
        # identity/credential decision the caller's selection owns, not the
        # model: models have picked stale/unauthorised profiles (e.g. an account
        # with an expired/revoked token) even when a valid profile was selected,
        # which hard-fails the whole turn (invalid_grant). When no authoritative
        # default is resolved (e.g. deterministic/scheduled workflows where it is
        # None) the supplied value is left untouched — nothing to enforce.
        if default_profile and current_profile_text != default_profile:
            payload["profile"] = default_profile
            if not current_profile_text:
                profile_source = "default_gmail_profile"
            elif current_profile_text in {"default", "primary"}:
                profile_source = "default_gmail_profile_placeholder_replacement"
            else:
                profile_source = "default_gmail_profile_enforced_override"
            profile_binding: dict[str, Any] = {
                "field": "profile",
                "source": profile_source,
                "value_present": True,
            }
            if (
                current_profile_text
                and current_profile_text not in {"default", "primary"}
            ):
                profile_binding["overridden_profile"] = current_profile_text
            bindings.append(profile_binding)

    namespace_binding = apply_namespace_to_mcp_payload(
        payload,
        input_schema=input_schema,
        user_namespace=user_namespace,
    )
    if namespace_binding is not None:
        bindings.append(namespace_binding)

    if input_schema is not None:
        try:
            _, coercion_warnings = coerce_payload_types(input_schema, payload)
        except Exception:
            coercion_warnings = []
        for warning in coercion_warnings:
            bindings.append(
                {
                    "field": "",
                    "source": "schema_type_coercion",
                    "value_present": True,
                    "warning": warning,
                }
            )

    if strip_unknown_fields and input_schema is not None:
        if schema_fields:
            for key in list(payload.keys()):
                if key in schema_fields:
                    continue
                payload.pop(key, None)
                bindings.append(
                    {
                        "field": key,
                        "source": "removed_for_strict_tool_schema",
                        "value_present": False,
                    }
                )

    return bindings


def apply_namespace_to_mcp_payload(
    payload: MutableMapping[str, Any],
    *,
    input_schema: Any,
    user_namespace: str | None,
) -> dict[str, Any] | None:
    """Apply or remove runtime namespace context according to the tool schema."""

    accepts_namespace = mcp_input_schema_accepts_namespace(input_schema)
    if not accepts_namespace:
        if "namespace" not in payload:
            return None
        payload.pop("namespace", None)
        return {
            "field": "namespace",
            "source": "removed_for_strict_tool_schema",
            "value_present": False,
        }

    namespace = (
        str(user_namespace or "").strip() if isinstance(user_namespace, str) else ""
    )
    if namespace and "namespace" not in payload:
        payload["namespace"] = namespace
        return {
            "field": "namespace",
            "source": "user_namespace",
            "value_present": True,
        }
    return None


def workflow_action_result_from_mcp_payload(
    *,
    tool_name: str,
    payload: Any,
    duration_ms: float | None,
    transport_metadata: Mapping[str, Any] | None = None,
) -> WorkflowActionResult:
    """Convert an MCP gateway payload into a workflow action result.

    Internal MCP handlers standardise tool-level errors as mapping payloads with
    ``success=false``. Workflow execution needs those responses to fail closed
    rather than continuing as if the tool succeeded merely because transport
    execution completed.
    """

    workflow_payload, projection_metadata = _workflow_visible_mcp_payload(
        tool_name=tool_name,
        payload=payload,
    )
    outputs = {
        "mcp_result": workflow_payload,
        "mcp_tool": tool_name,
        "mcp_duration_ms": duration_ms,
        "result": workflow_payload,
    }
    if isinstance(transport_metadata, Mapping):
        bounded_transport_metadata = {
            str(key): value
            for key, value in transport_metadata.items()
            if isinstance(key, str)
            and key
            in {
                "schema_version",
                "execution_id",
                "outcome",
                "duration_ms",
                "timeout_sec",
                "advisory_timeout_sec",
                "advisory_budget_exceeded",
                "queue_duration_ms",
                "handler_duration_ms",
                "handler_elapsed_ms",
                "transport_overhead_ms",
                "timeout_phase",
                "late_result_policy",
            }
        }
        outputs["mcp_transport"] = bounded_transport_metadata
        outputs["mcp_queue_duration_ms"] = bounded_transport_metadata.get(
            "queue_duration_ms"
        )
        outputs["mcp_handler_duration_ms"] = bounded_transport_metadata.get(
            "handler_duration_ms"
        )
        outputs["mcp_transport_overhead_ms"] = bounded_transport_metadata.get(
            "transport_overhead_ms"
        )
    outputs.update(projection_metadata)
    if isinstance(payload, Mapping) and payload.get("success") is False:
        mutation_outcome = str(payload.get("mutation_outcome") or "").strip().lower()
        error_code = str(payload.get("error_code") or "").strip().lower()
        action_status = (
            "unknown"
            if mutation_outcome in {"unknown", "indeterminate"}
            or error_code
            in {
                "tool_timeout_outcome_unknown",
                "tool_timeout_after_durable_submission",
            }
            else "failed"
        )
        return WorkflowActionResult(
            status=action_status,
            error=_coerce_mcp_error_message(tool_name=tool_name, payload=payload),
            outputs=dict(outputs),
            duration_ms=duration_ms,
        )

    return WorkflowActionResult(
        status="success",
        outputs=dict(outputs),
        duration_ms=duration_ms,
    )


def _workflow_visible_mcp_payload(
    *,
    tool_name: str,
    payload: Any,
) -> tuple[Any, dict[str, Any]]:
    """Return the payload safe for workflow context and result envelopes.

    When Vontology declares a tool evidence projection, workflows should carry
    that compact represented view rather than the raw MCP payload. The raw
    payload may contain large or sensitive source-specific objects; keeping it
    out of workflow context prevents later LLM render stages from seeing
    accidental authority-free bulk data.
    """

    if not isinstance(payload, Mapping) or payload.get("success") is False:
        return payload, {}

    try:
        from ..services.tool_evidence_projection_service import (
            project_tool_payload_for_llm,
        )

        projected = project_tool_payload_for_llm(tool_name, payload)
    except Exception:
        logger.debug(
            "[workflow_mcp] tool evidence projection unavailable for %s",
            tool_name,
            exc_info=True,
        )
        return payload, {}

    if not isinstance(projected, Mapping):
        return payload, {}

    telemetry = projected.get("_tool_evidence_projection")
    metadata: dict[str, Any] = {
        "mcp_result_projection_applied": True,
        "mcp_raw_result_omitted_from_workflow_context": True,
    }
    if isinstance(telemetry, Mapping):
        metadata["mcp_result_projection"] = dict(telemetry)
    return dict(projected), metadata


__all__ = [
    "apply_runtime_defaults_to_mcp_payload",
    "apply_namespace_to_mcp_payload",
    "candidate_internal_mcp_tool_names",
    "mcp_input_schema_accepts_field",
    "mcp_input_schema_declares_field",
    "mcp_input_schema_accepts_namespace",
    "mcp_method_accepts_namespace",
    "resolve_internal_mcp_tool_name",
    "workflow_action_result_from_mcp_payload",
]
