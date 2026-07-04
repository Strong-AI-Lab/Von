"""Required-tool obligation accounting for tool-using workflow turns.

This module is deliberately policy-light.  It does not decide that a domain
task needs a particular ontology mutation; that remains authored in expected
outcome contracts, workflows, prompts, Vontology artefacts, and tool metadata.
The support role here is to preserve and evaluate the obligations once those
authority surfaces have declared required tools.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .tool_metadata_service import get_tool_required_obligation_metadata
from .tool_target_contract_validation import (
    validate_tool_target_contract,
)

REQUIRED_TOOL_OBLIGATION_LEDGER_SCHEMA_VERSION = "required_tool_obligation_ledger.v1"

OPERATION_SEARCH_OR_RESOLUTION_READ = "search_or_resolution_read"
OPERATION_VERIFICATION_READ = "verification_read"
OPERATION_MUTATION_WRITE = "mutation_write"
OPERATION_EXTERNAL_SIDE_EFFECT = "external_side_effect"
OPERATION_WORKFLOW_EXECUTE = "workflow_execute"

_EQUIVALENT_EXECUTION_CONTEXT_FIELDS: tuple[str, ...] = (
    "error",
    "error_code",
    "failure_code",
    "message",
    "timeout_phase",
    "phase",
    "workflow_instance_id",
    "instance_id",
    "execution_id",
    "terminal_status",
    "final_state",
)

BLOCKER_REQUIRED_TOOL_NOT_PLANNED = "required_tool_not_planned"
BLOCKER_REQUIRED_TOOL_NOT_AVAILABLE_ON_GATEWAY = (
    "required_tool_not_available_on_gateway"
)
BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY = (
    "contract_required_tool_not_allowed_by_workflow_policy"
)
BLOCKER_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY = (
    "required_tool_not_allowed_by_workflow_policy"
)
BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED = "required_write_payload_unresolved"
BLOCKER_WRITE_POLICY_DENIED_OR_UNCONFIRMED = "write_policy_denied_or_unconfirmed"
BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS = (
    "tool_budget_exhausted_before_required_tools"
)
BLOCKER_MUTATION_SUCCEEDED_READBACK_MISSING = "mutation_succeeded_readback_missing"
BLOCKER_READBACK_ATTEMPTED_BUT_NOT_VERIFIED = "readback_attempted_but_not_verified"
BLOCKER_REQUIRED_TOOL_ATTEMPT_FAILED = "required_tool_attempt_failed"
BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED = "target_required_tool_attempt_failed"
BLOCKER_REQUIRED_TOOL_METADATA_MISSING = "required_tool_metadata_missing"


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value or "").strip()


def normalise_required_tool_names(value: Any) -> list[str]:
    """Return ordered unique non-empty tool names."""

    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return []

    normalised: list[str] = []
    seen: set[str] = set()
    for raw in values:
        tool_name = _safe_str(raw)
        if not tool_name:
            continue
        lowered = tool_name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(tool_name)
    return normalised


def normalise_required_tool_sources(
    required_tools_by_source: Mapping[str, Any] | None = None,
    *,
    required_tools: Sequence[Any] | None = None,
    default_source: str = "required_tools",
) -> dict[str, list[str]]:
    """Build a tool-name keyed source map from one or more declarations."""

    sources_by_tool: dict[str, list[str]] = {}

    def add_source(tool_name: str, source: str) -> None:
        source_name = source.strip() or default_source
        existing = sources_by_tool.setdefault(tool_name, [])
        if source_name not in existing:
            existing.append(source_name)

    for tool_name in normalise_required_tool_names(required_tools or []):
        add_source(tool_name, default_source)

    if isinstance(required_tools_by_source, Mapping):
        for raw_source, raw_tools in required_tools_by_source.items():
            source = _safe_str(raw_source) or default_source
            for tool_name in normalise_required_tool_names(raw_tools):
                add_source(tool_name, source)

    return sources_by_tool


def classify_required_tool_operation(tool_name: str) -> str:
    """Classify a required tool into a generic execution obligation class."""

    cleaned = _safe_str(tool_name)
    metadata = get_tool_required_obligation_metadata(cleaned)
    return metadata.operation_class or OPERATION_EXTERNAL_SIDE_EFFECT


def _has_required_tool_operation_metadata(tool_name: str) -> bool:
    metadata = get_tool_required_obligation_metadata(tool_name)
    return bool(metadata.operation_class)


def _method_lookup(method_catalogue: Mapping[str, Any] | None) -> set[str] | None:
    if not isinstance(method_catalogue, Mapping):
        return None
    return {
        str(name).strip().lower()
        for name in method_catalogue.keys()
        if str(name).strip()
    }


def _method_definition_for_tool(
    method_catalogue: Mapping[str, Any] | None,
    tool_name: str,
) -> Any | None:
    if not isinstance(method_catalogue, Mapping):
        return None
    lowered = tool_name.lower()
    for name, definition in method_catalogue.items():
        if str(name).strip().lower() == lowered:
            return definition
    return None


def _catalogue_operation_class(method_definition: Any | None) -> str | None:
    if method_definition is None:
        return None
    if isinstance(method_definition, Mapping):
        explicit = _safe_str(
            method_definition.get("operation_class")
            or method_definition.get("required_tool_operation_class")
            or method_definition.get("obligation_operation_class")
        )
        category = _safe_str(method_definition.get("category")).lower()
    else:
        explicit = _safe_str(
            getattr(method_definition, "operation_class", None)
            or getattr(method_definition, "required_tool_operation_class", None)
            or getattr(method_definition, "obligation_operation_class", None)
        )
        category = _safe_str(getattr(method_definition, "category", None)).lower()

    if explicit:
        return explicit
    if category == "read":
        return OPERATION_SEARCH_OR_RESOLUTION_READ
    if category == "write":
        return OPERATION_MUTATION_WRITE
    if category in {"workflow", "workflow_execute"}:
        return OPERATION_WORKFLOW_EXECUTE
    if category in {"external", "side_effect", "external_side_effect"}:
        return OPERATION_EXTERNAL_SIDE_EFFECT
    return None


def _allowed_lookup(allowed_tools: Sequence[Any] | None) -> set[str] | None:
    if allowed_tools is None:
        return None
    return {str(name).strip().lower() for name in allowed_tools if str(name).strip()}


def _tool_from_invocation(invocation: Mapping[str, Any]) -> str:
    return _safe_str(invocation.get("tool")) or _safe_str(invocation.get("method"))


def _tool_from_planned_call(call: Mapping[str, Any]) -> str:
    return (
        _safe_str(call.get("tool"))
        or _safe_str(call.get("method"))
        or _safe_str(call.get("name"))
    )


def _payload_from_planned_call(call: Mapping[str, Any]) -> Mapping[str, Any]:
    for field_name in ("effective_payload", "payload", "arguments", "input"):
        payload = call.get(field_name)
        if isinstance(payload, Mapping):
            return payload
    return {}


def _tool_from_equivalent_execution(execution: Mapping[str, Any]) -> str:
    return (
        _safe_str(execution.get("tool"))
        or _safe_str(execution.get("method"))
        or _safe_str(execution.get("action_id"))
        or _safe_str(execution.get("name"))
    )


def _normalise_equivalent_execution_record(
    execution: Mapping[str, Any],
    *,
    fallback_status: str,
) -> dict[str, Any] | None:
    tool_name = _tool_from_equivalent_execution(execution)
    if not tool_name:
        return None
    status = _safe_str(execution.get("status")) or _safe_str(execution.get("outcome"))
    record = {
        "tool": tool_name,
        "status": status or fallback_status,
        "source": _safe_str(execution.get("source")) or "equivalent_execution_surface",
        "workflow_id": _safe_str(execution.get("workflow_id")),
        "state_id": _safe_str(execution.get("state_id")),
        "action_id": _safe_str(execution.get("action_id")) or tool_name,
    }
    for field_name in _EQUIVALENT_EXECUTION_CONTEXT_FIELDS:
        value = _safe_str(execution.get(field_name))
        if value:
            record[field_name] = value
    for field_name in ("details", "error_details"):
        value = execution.get(field_name)
        if isinstance(value, Mapping):
            record[field_name] = dict(value)
    return record


def _payload_from_invocation(invocation: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = invocation.get("effective_payload")
    if isinstance(payload, Mapping):
        return payload
    payload = invocation.get("payload")
    if isinstance(payload, Mapping):
        return payload
    result = invocation.get("result")
    if isinstance(result, Mapping):
        return result
    return {}


def _arguments_from_invocation(invocation: Mapping[str, Any]) -> Mapping[str, Any]:
    arguments = invocation.get("effective_arguments")
    if isinstance(arguments, Mapping):
        return arguments
    arguments = invocation.get("arguments")
    if isinstance(arguments, Mapping):
        return arguments
    arguments = invocation.get("input")
    if isinstance(arguments, Mapping):
        return arguments
    return {}


def _invocation_status(invocation: Mapping[str, Any]) -> str:
    raw_status = _safe_str(invocation.get("status")).lower()
    payload = _payload_from_invocation(invocation)
    payload_status = _safe_str(payload.get("status")).lower()
    if raw_status == "ok" and payload_status not in {"error", "failed", "failure"}:
        if payload.get("success") is False:
            return "failed"
        return "ok"
    if raw_status in {"blocked", "error", "failed", "failure"}:
        return "blocked" if raw_status == "blocked" else "failed"
    if payload_status in {"error", "failed", "failure"}:
        return "failed"
    if payload.get("success") is False:
        return "failed"
    if payload.get("success") is True:
        return "ok"
    return raw_status or "unknown"


def _invocation_reports_false_result(invocation: Mapping[str, Any]) -> bool:
    payload = _payload_from_invocation(invocation)
    return (
        invocation.get("result") is False
        or invocation.get("result_preview") is False
        or payload.get("result") is False
    )


def _normalise_target_tokens(values: Sequence[Any]) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        if isinstance(raw_value, Sequence) and not isinstance(
            raw_value, (str, bytes, bytearray)
        ):
            nested_values = raw_value
        else:
            nested_values = [raw_value]
        for nested_value in nested_values:
            token = _safe_str(nested_value)
            if not token:
                continue
            lowered = token.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            tokens.append(token)
    return tokens


def _target_closure_key(target: str) -> str:
    cleaned = _safe_str(target)
    lowered = cleaned.lower()
    if "://" not in lowered:
        return lowered
    without_fragment = lowered.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    tail = without_fragment.rsplit("/", 1)[-1].strip()
    return tail or lowered


def _value_at_path(source: Mapping[str, Any], path: str) -> Any:
    current: Any = source
    for part in path.split("."):
        key = part.strip()
        if not key:
            return None
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _target_tokens_from_mapping(
    source: Mapping[str, Any],
    *,
    field_names: Sequence[str],
) -> list[str]:
    target_values: list[Any] = []
    for raw_field_name in field_names:
        if not isinstance(raw_field_name, str):
            continue
        field_name = raw_field_name.strip()
        if not field_name:
            continue
        value = _value_at_path(source, field_name)
        if isinstance(value, str) and value.strip():
            target_values.append(value)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            target_values.append(
                [
                    item
                    for item in value
                    if isinstance(item, str) and item.strip()
                ]
            )
    return _normalise_target_tokens(target_values)


def _target_tokens_from_invocation(
    tool_name: str,
    invocation: Mapping[str, Any],
) -> list[str]:
    explicit_targets = invocation.get("target_ids") or invocation.get("target_tokens")
    target_values: list[Any] = []
    if isinstance(explicit_targets, Sequence) and not isinstance(
        explicit_targets, (str, bytes, bytearray)
    ):
        target_values.extend(explicit_targets)

    metadata = get_tool_required_obligation_metadata(tool_name)
    arguments = _arguments_from_invocation(invocation)
    if arguments and metadata.target_argument_names:
        target_values.extend(
            _target_tokens_from_mapping(
                arguments,
                field_names=metadata.target_argument_names,
            )
        )
    if not target_values:
        payload = _payload_from_invocation(invocation)
        payload_field_names = (
            metadata.target_payload_field_names or metadata.target_argument_names
        )
        if payload and payload_field_names:
            target_values.extend(
                _target_tokens_from_mapping(
                    payload,
                    field_names=payload_field_names,
                )
            )
    return _normalise_target_tokens(target_values)


def _build_target_closure_payload(
    target_status: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    if not isinstance(target_status, Mapping) or not target_status:
        return None

    attempted_targets: list[str] = []
    successful_targets: list[str] = []
    failed_targets: list[str] = []
    unresolved_failed_targets: list[str] = []
    for entry in target_status.values():
        target = _safe_str(entry.get("target"))
        if not target:
            continue
        attempted_targets.append(target)
        if int(entry.get("successful_count") or 0) > 0:
            successful_targets.append(target)
        if int(entry.get("failed_count") or 0) > 0:
            failed_targets.append(target)
        if (
            int(entry.get("failed_count") or 0) > 0
            and int(entry.get("successful_count") or 0) <= 0
        ):
            unresolved_failed_targets.append(target)

    return {
        "attempted_target_count": len(attempted_targets),
        "successful_target_count": len(successful_targets),
        "failed_target_count": len(failed_targets),
        "unresolved_failed_target_count": len(unresolved_failed_targets),
        "unresolved_failed_targets": unresolved_failed_targets[:10],
    }


def _invocation_text(invocation: Mapping[str, Any]) -> str:
    payload = _payload_from_invocation(invocation)
    parts = [
        _safe_str(invocation.get("error")),
        _safe_str(invocation.get("result_summary")),
        _safe_str(payload.get("error")),
        _safe_str(payload.get("reason")),
        _safe_str(payload.get("result_summary")),
        _safe_str(payload.get("summary")),
    ]
    return " ".join(part for part in parts if part).lower()


def _attempt_failure_blocker(
    *,
    operation_class: str,
    invocation: Mapping[str, Any] | None,
) -> str:
    text = _invocation_text(invocation or {})
    if operation_class == OPERATION_MUTATION_WRITE:
        if any(token in text for token in ("permission", "denied", "confirm")):
            return BLOCKER_WRITE_POLICY_DENIED_OR_UNCONFIRMED
        if any(
            token in text for token in ("argument", "payload", "schema", "unresolved")
        ):
            return BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
    return BLOCKER_REQUIRED_TOOL_ATTEMPT_FAILED


def _operation_supports_target_closure(tool_name: str, operation_class: str) -> bool:
    metadata = get_tool_required_obligation_metadata(tool_name)
    if metadata.target_closure_required is not None:
        return metadata.target_closure_required
    return operation_class in {
        OPERATION_SEARCH_OR_RESOLUTION_READ,
        OPERATION_VERIFICATION_READ,
    }


def _existing_obligation_lookup(
    existing_ledger: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(existing_ledger, Mapping):
        return {}
    obligations = existing_ledger.get("obligations")
    if not isinstance(obligations, Sequence) or isinstance(
        obligations, (str, bytes, bytearray)
    ):
        return {}
    lookup: dict[str, Mapping[str, Any]] = {}
    for item in obligations:
        if not isinstance(item, Mapping):
            continue
        tool_name = _safe_str(item.get("tool_name"))
        if tool_name:
            lookup[tool_name.lower()] = item
    return lookup


def _append_validation_errors(
    lookup: dict[str, dict[str, Any]],
    *,
    tool_name: str,
    errors: Sequence[Any],
) -> None:
    cleaned_tool = _safe_str(tool_name)
    if not cleaned_tool:
        return
    lowered = cleaned_tool.lower()
    entry = lookup.setdefault(lowered, {"tool": cleaned_tool, "errors": []})
    serialised_errors = entry.setdefault("errors", [])
    if not isinstance(serialised_errors, list):
        serialised_errors = []
        entry["errors"] = serialised_errors
    for raw_error in errors:
        if isinstance(raw_error, Mapping):
            payload = {
                str(key): value
                for key, value in raw_error.items()
                if isinstance(key, str)
            }
            payload.setdefault("tool", cleaned_tool)
        else:
            message = _safe_str(raw_error)
            if not message:
                continue
            payload = {"tool": cleaned_tool, "message": message}
        serialised_errors.append(payload)


def _tool_name_from_validation_error(error: Mapping[str, Any]) -> str:
    return (
        _safe_str(error.get("tool"))
        or _safe_str(error.get("planned_tool"))
        or _safe_str(error.get("method"))
        or _safe_str(error.get("name"))
    )


def _validation_failure_lookup(
    *,
    tool_call_validation_failure_context: Mapping[str, Any] | None = None,
    tool_call_validation_errors: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}

    if isinstance(tool_call_validation_failure_context, Mapping):
        failures_by_tool = tool_call_validation_failure_context.get("failures_by_tool")
        if isinstance(failures_by_tool, Mapping):
            for raw_tool_key, raw_failure in failures_by_tool.items():
                if not isinstance(raw_failure, Mapping):
                    continue
                tool_name = _safe_str(raw_failure.get("tool")) or _safe_str(
                    raw_tool_key
                )
                errors = raw_failure.get("errors")
                if isinstance(errors, Sequence) and not isinstance(
                    errors, (str, bytes, bytearray)
                ):
                    _append_validation_errors(
                        lookup,
                        tool_name=tool_name,
                        errors=list(errors),
                    )
                else:
                    _append_validation_errors(
                        lookup,
                        tool_name=tool_name,
                        errors=[raw_failure],
                    )

        for field_name in (
            "tool_call_validation_required_tool_errors",
            "tool_call_validation_errors",
            "errors",
        ):
            raw_errors = tool_call_validation_failure_context.get(field_name)
            if not isinstance(raw_errors, Sequence) or isinstance(
                raw_errors, (str, bytes, bytearray)
            ):
                continue
            for raw_error in raw_errors:
                if not isinstance(raw_error, Mapping):
                    continue
                tool_name = _tool_name_from_validation_error(raw_error)
                _append_validation_errors(
                    lookup,
                    tool_name=tool_name,
                    errors=[raw_error],
                )

    for raw_error in tool_call_validation_errors or ():
        if not isinstance(raw_error, Mapping):
            continue
        tool_name = _tool_name_from_validation_error(raw_error)
        _append_validation_errors(lookup, tool_name=tool_name, errors=[raw_error])

    return lookup


def _validation_failure_status(failure: Mapping[str, Any] | None) -> str:
    if not isinstance(failure, Mapping):
        return ""
    errors = failure.get("errors")
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes, bytearray)):
        return "tool_call_validation_failed"
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        error_code = _safe_str(error.get("error_code"))
        if error_code:
            return error_code
    return "tool_call_validation_failed"


def _validation_failure_message(failure: Mapping[str, Any] | None) -> str:
    if not isinstance(failure, Mapping):
        return ""
    errors = failure.get("errors")
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes, bytearray)):
        return ""
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        message = _safe_str(error.get("message")) or _safe_str(error.get("error"))
        if message:
            return message
    return ""


def build_required_tool_obligation_ledger(
    *,
    required_tools: Sequence[Any] | None = None,
    required_tools_by_source: Mapping[str, Any] | None = None,
    invocations: Sequence[Mapping[str, Any]] | None = None,
    observed_equivalent_successful_tools: Sequence[Any] | None = None,
    observed_equivalent_failed_tools: Sequence[Any] | None = None,
    observed_equivalent_successful_executions: (
        Sequence[Mapping[str, Any]] | None
    ) = None,
    observed_equivalent_failed_executions: Sequence[Mapping[str, Any]] | None = None,
    planned_tool_calls: Sequence[Mapping[str, Any]] | None = None,
    tool_call_validation_failure_context: Mapping[str, Any] | None = None,
    tool_call_validation_errors: Sequence[Mapping[str, Any]] | None = None,
    target_contract_state: Any = None,
    allowed_tools: Sequence[Any] | None = None,
    method_catalogue: Mapping[str, Any] | None = None,
    max_tool_invocations: int | None = None,
    existing_ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a serialisable ledger for required tool closure."""

    sources_by_tool = normalise_required_tool_sources(
        required_tools_by_source,
        required_tools=required_tools,
    )
    existing_lookup = _existing_obligation_lookup(existing_ledger)
    for tool_name, obligation in existing_lookup.items():
        existing_sources = normalise_required_tool_names(obligation.get("sources"))
        if not existing_sources:
            existing_source = _safe_str(obligation.get("source"))
            existing_sources = [existing_source] if existing_source else []
        if existing_sources:
            sources_by_tool.setdefault(_safe_str(obligation.get("tool_name")), [])
            for source in existing_sources:
                if (
                    source
                    not in sources_by_tool[_safe_str(obligation.get("tool_name"))]
                ):
                    sources_by_tool[_safe_str(obligation.get("tool_name"))].append(
                        source
                    )

    if not sources_by_tool:
        return {
            "schema_version": REQUIRED_TOOL_OBLIGATION_LEDGER_SCHEMA_VERSION,
            "obligations": [],
            "required_tool_count": 0,
            "satisfied_count": 0,
            "unsatisfied_count": 0,
            "unsatisfied_required_tools": [],
            "blocking_reasons": [],
            "blocking_failure_codes": [],
            "failure_classes": [],
            "max_tool_invocations": max_tool_invocations,
            "observed_invocation_count": 0,
        }

    allowed = _allowed_lookup(allowed_tools)
    available = _method_lookup(method_catalogue)

    planned_counts: dict[str, int] = {}
    for call in planned_tool_calls or ():
        if not isinstance(call, Mapping):
            continue
        tool_name = _tool_from_planned_call(call)
        if tool_name:
            planned_counts[tool_name.lower()] = (
                planned_counts.get(tool_name.lower(), 0) + 1
            )

    validation_failures_by_tool = _validation_failure_lookup(
        tool_call_validation_failure_context=tool_call_validation_failure_context,
        tool_call_validation_errors=tool_call_validation_errors,
    )
    for call in planned_tool_calls or ():
        if not isinstance(call, Mapping):
            continue
        tool_name = _tool_from_planned_call(call)
        if not tool_name:
            continue
        target_validation = validate_tool_target_contract(
            tool_name=tool_name,
            payload=_payload_from_planned_call(call),
            target_contract_state=target_contract_state,
        )
        if target_validation.ok:
            continue
        _append_validation_errors(
            validation_failures_by_tool,
            tool_name=tool_name,
            errors=target_validation.errors,
        )
    attempted_counts: dict[str, int] = {}
    successful_counts: dict[str, int] = {}
    last_status_by_tool: dict[str, str] = {}
    last_invocation_by_tool: dict[str, Mapping[str, Any]] = {}
    attempted_operation_classes: list[str] = []
    target_status_by_tool: dict[str, dict[str, dict[str, Any]]] = {}
    execution_surfaces_by_tool: dict[str, list[dict[str, Any]]] = {}

    def add_execution_surface(tool_name: str, surface: Mapping[str, Any]) -> None:
        cleaned_tool_name = _safe_str(tool_name)
        if not cleaned_tool_name:
            return
        normalised = {
            "source": _safe_str(surface.get("source"))
            or "equivalent_execution_surface",
            "status": _safe_str(surface.get("status")),
            "workflow_id": _safe_str(surface.get("workflow_id")),
            "state_id": _safe_str(surface.get("state_id")),
            "action_id": _safe_str(surface.get("action_id")) or cleaned_tool_name,
        }
        for field_name in _EQUIVALENT_EXECUTION_CONTEXT_FIELDS:
            value = _safe_str(surface.get(field_name))
            if value:
                normalised[field_name] = value
        for field_name in ("details", "error_details"):
            value = surface.get(field_name)
            if isinstance(value, Mapping):
                normalised[field_name] = dict(value)
        existing = execution_surfaces_by_tool.setdefault(
            cleaned_tool_name.lower(),
            [],
        )
        fingerprint = tuple(sorted(normalised.items()))
        for item in existing:
            if tuple(sorted(item.items())) == fingerprint:
                return
        existing.append(normalised)

    for lowered, failure in validation_failures_by_tool.items():
        tool_name = _safe_str(failure.get("tool"))
        if not tool_name:
            continue
        errors = failure.get("errors")
        error_count = (
            len(errors)
            if isinstance(errors, Sequence)
            and not isinstance(errors, (str, bytes, bytearray))
            else 1
        )
        planned_counts[lowered] = max(planned_counts.get(lowered, 0), 1)
        attempted_counts[lowered] = attempted_counts.get(lowered, 0) + max(
            error_count,
            1,
        )
        last_status_by_tool[lowered] = (
            _validation_failure_status(failure) or "tool_call_validation_failed"
        )
        attempted_operation_classes.append(classify_required_tool_operation(tool_name))

    for invocation in invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        tool_name = _tool_from_invocation(invocation)
        if not tool_name:
            continue
        lowered = tool_name.lower()
        attempted_counts[lowered] = attempted_counts.get(lowered, 0) + 1
        planned_counts[lowered] = max(
            planned_counts.get(lowered, 0), attempted_counts[lowered]
        )
        status = _invocation_status(invocation)
        operation_class = classify_required_tool_operation(tool_name)
        if (
            status == "ok"
            and operation_class == OPERATION_VERIFICATION_READ
            and _invocation_reports_false_result(invocation)
        ):
            status = "failed"
        validation_payload = _arguments_from_invocation(invocation)
        if not validation_payload:
            validation_payload = _payload_from_invocation(invocation)
        target_validation = validate_tool_target_contract(
            tool_name=tool_name,
            payload=validation_payload,
            target_contract_state=target_contract_state,
        )
        if not target_validation.ok:
            _append_validation_errors(
                validation_failures_by_tool,
                tool_name=tool_name,
                errors=target_validation.errors,
            )
            status = target_validation.first_error_code() or "target_contract_failed"
        last_status_by_tool[lowered] = status
        last_invocation_by_tool[lowered] = invocation
        attempted_operation_classes.append(operation_class)
        if status == "ok":
            successful_counts[lowered] = successful_counts.get(lowered, 0) + 1
        if _operation_supports_target_closure(tool_name, operation_class):
            for target in _target_tokens_from_invocation(tool_name, invocation):
                target_key = _target_closure_key(target)
                target_entry = target_status_by_tool.setdefault(lowered, {}).setdefault(
                    target_key,
                    {
                        "target": target,
                        "attempted_count": 0,
                        "successful_count": 0,
                        "failed_count": 0,
                    },
                )
                target_entry["attempted_count"] = (
                    int(target_entry.get("attempted_count") or 0) + 1
                )
                if status == "ok":
                    target_entry["successful_count"] = (
                        int(target_entry.get("successful_count") or 0) + 1
                    )
                elif status and status != "unknown":
                    target_entry["failed_count"] = (
                        int(target_entry.get("failed_count") or 0) + 1
                    )

    observed_equivalent_invocation_count = 0
    for tool_name in normalise_required_tool_names(
        observed_equivalent_successful_tools
    ):
        lowered = tool_name.lower()
        observed_equivalent_invocation_count += 1
        attempted_counts[lowered] = attempted_counts.get(lowered, 0) + 1
        planned_counts[lowered] = max(
            planned_counts.get(lowered, 0), attempted_counts[lowered]
        )
        successful_counts[lowered] = successful_counts.get(lowered, 0) + 1
        last_status_by_tool[lowered] = "ok"
        add_execution_surface(
            tool_name,
            {
                "source": "equivalent_execution_surface",
                "status": "success",
                "action_id": tool_name,
            },
        )
        attempted_operation_classes.append(classify_required_tool_operation(tool_name))

    for tool_name in normalise_required_tool_names(observed_equivalent_failed_tools):
        lowered = tool_name.lower()
        observed_equivalent_invocation_count += 1
        attempted_counts[lowered] = attempted_counts.get(lowered, 0) + 1
        planned_counts[lowered] = max(
            planned_counts.get(lowered, 0), attempted_counts[lowered]
        )
        last_status_by_tool[lowered] = "error"
        last_invocation_by_tool[lowered] = {
            "tool": tool_name,
            "status": "error",
            "error": "Equivalent execution surface reported failure.",
        }
        add_execution_surface(
            tool_name,
            {
                "source": "equivalent_execution_surface",
                "status": "failed",
                "action_id": tool_name,
            },
        )
        attempted_operation_classes.append(classify_required_tool_operation(tool_name))

    for execution in observed_equivalent_successful_executions or ():
        if not isinstance(execution, Mapping):
            continue
        record = _normalise_equivalent_execution_record(
            execution,
            fallback_status="success",
        )
        if record is None:
            continue
        tool_name = _safe_str(record.get("tool"))
        lowered = tool_name.lower()
        observed_equivalent_invocation_count += 1
        attempted_counts[lowered] = attempted_counts.get(lowered, 0) + 1
        planned_counts[lowered] = max(
            planned_counts.get(lowered, 0), attempted_counts[lowered]
        )
        successful_counts[lowered] = successful_counts.get(lowered, 0) + 1
        last_status_by_tool[lowered] = "ok"
        add_execution_surface(tool_name, record)
        attempted_operation_classes.append(classify_required_tool_operation(tool_name))

    for execution in observed_equivalent_failed_executions or ():
        if not isinstance(execution, Mapping):
            continue
        record = _normalise_equivalent_execution_record(
            execution,
            fallback_status="failed",
        )
        if record is None:
            continue
        tool_name = _safe_str(record.get("tool"))
        lowered = tool_name.lower()
        observed_equivalent_invocation_count += 1
        attempted_counts[lowered] = attempted_counts.get(lowered, 0) + 1
        planned_counts[lowered] = max(
            planned_counts.get(lowered, 0), attempted_counts[lowered]
        )
        last_status_by_tool[lowered] = "error"
        last_error = (
            _safe_str(record.get("error"))
            or _safe_str(record.get("message"))
            or "Equivalent execution surface reported failure."
        )
        last_invocation = {
            "tool": tool_name,
            "status": "error",
            "error": last_error,
        }
        for field_name in _EQUIVALENT_EXECUTION_CONTEXT_FIELDS:
            value = _safe_str(record.get(field_name))
            if value:
                last_invocation[field_name] = value
        last_invocation_by_tool[lowered] = last_invocation
        add_execution_surface(tool_name, record)
        attempted_operation_classes.append(classify_required_tool_operation(tool_name))

    obligations: list[dict[str, Any]] = []
    for tool_name, sources in sources_by_tool.items():
        cleaned_tool = _safe_str(tool_name)
        if not cleaned_tool:
            continue
        lowered = cleaned_tool.lower()
        existing = existing_lookup.get(lowered, {})
        catalogue_operation_class = _catalogue_operation_class(
            _method_definition_for_tool(method_catalogue, cleaned_tool)
        )
        existing_operation_class = _safe_str(existing.get("operation_class"))
        existing_metadata_present = existing.get("operation_metadata_present") is True
        operation_class = (
            classify_required_tool_operation(cleaned_tool)
            if _has_required_tool_operation_metadata(cleaned_tool)
            else catalogue_operation_class
            or (existing_operation_class if existing_metadata_present else "")
            or OPERATION_EXTERNAL_SIDE_EFFECT
        )
        operation_metadata_present = bool(
            _has_required_tool_operation_metadata(cleaned_tool)
            or catalogue_operation_class
            or existing_metadata_present
        )

        if allowed is None:
            allowed_by_policy = existing.get("allowed_by_workflow_policy")
            if not isinstance(allowed_by_policy, bool):
                allowed_by_policy = True
        else:
            allowed_by_policy = lowered in allowed

        if available is None:
            available_on_gateway = existing.get("available_on_gateway")
            if not isinstance(available_on_gateway, bool):
                available_on_gateway = None
        else:
            available_on_gateway = lowered in available

        planned_count = planned_counts.get(lowered, 0)
        attempted_count = attempted_counts.get(lowered, 0)
        successful_count = successful_counts.get(lowered, 0)
        validation_failure = validation_failures_by_tool.get(lowered)
        target_closure = _build_target_closure_payload(
            target_status_by_tool.get(lowered)
        )
        unresolved_failed_target_count = (
            int(target_closure.get("unresolved_failed_target_count") or 0)
            if isinstance(target_closure, Mapping)
            else 0
        )
        availability_blocks_obligation = (
            available_on_gateway is False and attempted_count <= 0
        )
        satisfied = (
            successful_count > 0
            and bool(allowed_by_policy)
            and operation_metadata_present
            and unresolved_failed_target_count <= 0
        )

        blocking_reason = ""
        failure_class = ""
        if not bool(allowed_by_policy):
            blocking_reason = (
                BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
            )
            failure_class = BLOCKER_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
        elif availability_blocks_obligation:
            blocking_reason = BLOCKER_REQUIRED_TOOL_NOT_AVAILABLE_ON_GATEWAY
            failure_class = BLOCKER_REQUIRED_TOOL_NOT_AVAILABLE_ON_GATEWAY
        elif not operation_metadata_present:
            blocking_reason = BLOCKER_REQUIRED_TOOL_METADATA_MISSING
            failure_class = BLOCKER_REQUIRED_TOOL_METADATA_MISSING
        elif not satisfied:
            if unresolved_failed_target_count > 0:
                blocking_reason = BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED
                failure_class = blocking_reason
            elif validation_failure is not None:
                if operation_class == OPERATION_MUTATION_WRITE:
                    blocking_reason = BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
                else:
                    blocking_reason = BLOCKER_REQUIRED_TOOL_ATTEMPT_FAILED
                failure_class = blocking_reason
            elif attempted_count > 0:
                last_status = last_status_by_tool.get(lowered, "")
                if last_status == "blocked":
                    blocking_reason = BLOCKER_WRITE_POLICY_DENIED_OR_UNCONFIRMED
                    failure_class = blocking_reason
                else:
                    blocking_reason = _attempt_failure_blocker(
                        operation_class=operation_class,
                        invocation=last_invocation_by_tool.get(lowered),
                    )
                    failure_class = blocking_reason
            else:
                blocking_reason = BLOCKER_REQUIRED_TOOL_NOT_PLANNED
                failure_class = blocking_reason

        obligation = {
            "tool_name": cleaned_tool,
            "source": sources[0] if sources else "required_tools",
            "sources": list(sources),
            "operation_class": operation_class,
            "operation_metadata_present": operation_metadata_present,
            "allowed_by_workflow_policy": bool(allowed_by_policy),
            "available_on_gateway": available_on_gateway,
            "planned_count": planned_count,
            "attempted_count": attempted_count,
            "successful_count": successful_count,
            "last_attempt_status": last_status_by_tool.get(lowered, ""),
            "blocking_reason": blocking_reason,
            "failure_class": failure_class,
            "satisfied": satisfied,
        }
        if isinstance(target_closure, Mapping):
            obligation["target_closure"] = dict(target_closure)
        execution_surfaces = execution_surfaces_by_tool.get(lowered)
        if execution_surfaces:
            obligation["execution_surfaces"] = [
                dict(surface) for surface in execution_surfaces
            ]
        if validation_failure is not None:
            errors = validation_failure.get("errors")
            if isinstance(errors, Sequence) and not isinstance(
                errors, (str, bytes, bytearray)
            ):
                obligation["tool_call_validation_errors"] = [
                    dict(error) for error in errors if isinstance(error, Mapping)
                ]
            if not satisfied:
                message = _validation_failure_message(validation_failure)
                if message:
                    obligation["last_attempt_message"] = message
        obligations.append(obligation)

    if max_tool_invocations is None and isinstance(existing_ledger, Mapping):
        raw_existing_cap = existing_ledger.get("max_tool_invocations")
        if isinstance(raw_existing_cap, int):
            max_tool_invocations = raw_existing_cap
    observed_invocation_count = (
        len([item for item in invocations or () if isinstance(item, Mapping)])
        + observed_equivalent_invocation_count
    )
    exhausted_budget = (
        isinstance(max_tool_invocations, int)
        and max_tool_invocations >= 0
        and observed_invocation_count >= max_tool_invocations
    )
    attempted_classes = {
        operation_class
        for operation_class in attempted_operation_classes
        if operation_class
    }
    search_only_attempts = bool(attempted_classes) and attempted_classes.issubset(
        {OPERATION_SEARCH_OR_RESOLUTION_READ}
    )
    if exhausted_budget and search_only_attempts:
        for obligation in obligations:
            if obligation.get("satisfied"):
                continue
            if obligation.get("operation_class") == OPERATION_SEARCH_OR_RESOLUTION_READ:
                continue
            if obligation.get("blocking_reason") in {
                "",
                BLOCKER_REQUIRED_TOOL_NOT_PLANNED,
                BLOCKER_REQUIRED_TOOL_ATTEMPT_FAILED,
            }:
                obligation["blocking_reason"] = (
                    BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
                )
                obligation["failure_class"] = (
                    BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
                )

    mutation_satisfied = any(
        obligation.get("operation_class") == OPERATION_MUTATION_WRITE
        and obligation.get("satisfied")
        for obligation in obligations
    )
    if mutation_satisfied:
        for obligation in obligations:
            if obligation.get("operation_class") != OPERATION_VERIFICATION_READ:
                continue
            if obligation.get("satisfied"):
                continue
            if int(obligation.get("attempted_count") or 0) > 0:
                obligation["blocking_reason"] = (
                    BLOCKER_READBACK_ATTEMPTED_BUT_NOT_VERIFIED
                )
                obligation["failure_class"] = (
                    BLOCKER_READBACK_ATTEMPTED_BUT_NOT_VERIFIED
                )
            else:
                obligation["blocking_reason"] = (
                    BLOCKER_MUTATION_SUCCEEDED_READBACK_MISSING
                )
                obligation["failure_class"] = (
                    BLOCKER_MUTATION_SUCCEEDED_READBACK_MISSING
                )

    unsatisfied = [item for item in obligations if not bool(item.get("satisfied"))]
    blocking_reasons = normalise_required_tool_names(
        [item.get("blocking_reason") for item in unsatisfied]
    )
    failure_classes = normalise_required_tool_names(
        [item.get("failure_class") for item in unsatisfied]
    )
    blocking_failure_codes = normalise_required_tool_names(
        [
            *blocking_reasons,
            *(
                [BLOCKER_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY]
                if BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
                in blocking_reasons
                else []
            ),
        ]
    )

    return {
        "schema_version": REQUIRED_TOOL_OBLIGATION_LEDGER_SCHEMA_VERSION,
        "obligations": obligations,
        "required_tool_count": len(obligations),
        "satisfied_count": len(obligations) - len(unsatisfied),
        "unsatisfied_count": len(unsatisfied),
        "unsatisfied_required_tools": [
            _safe_str(item.get("tool_name")) for item in unsatisfied
        ],
        "blocking_reasons": blocking_reasons,
        "blocking_failure_codes": blocking_failure_codes,
        "failure_classes": failure_classes,
        "search_only_budget_exhaustion": (
            BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS in blocking_reasons
        ),
        "max_tool_invocations": max_tool_invocations,
        "observed_invocation_count": observed_invocation_count,
    }


def required_tool_obligation_effect(
    ledger: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a synthetic completion-gate effect for unsatisfied obligations."""

    if not isinstance(ledger, Mapping):
        return None
    obligations = ledger.get("obligations")
    if not isinstance(obligations, Sequence) or isinstance(
        obligations, (str, bytes, bytearray)
    ):
        return None
    unsatisfied = [
        item
        for item in obligations
        if isinstance(item, Mapping) and not bool(item.get("satisfied"))
    ]
    if not unsatisfied:
        return None
    failure_codes = normalise_required_tool_names(
        ledger.get("blocking_failure_codes")
    ) or [BLOCKER_REQUIRED_TOOL_NOT_PLANNED]
    unsatisfied_tools = normalise_required_tool_names(
        [item.get("tool_name") for item in unsatisfied]
    )
    not_satisfied_codes = {
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED,
        BLOCKER_WRITE_POLICY_DENIED_OR_UNCONFIRMED,
        BLOCKER_READBACK_ATTEMPTED_BUT_NOT_VERIFIED,
        BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED,
    }
    status = (
        "not_satisfied"
        if any(code in not_satisfied_codes for code in failure_codes)
        else "not_executed"
    )
    return {
        "effect_id": "effect_required_tool_obligations_1",
        "intent_origin": "required_tool_obligation_ledger",
        "effect_type": "tool_execution",
        "description": (
            "Observe contract-required tool execution and read-back closure."
        ),
        "required_tools": unsatisfied_tools,
        "required_tools_match": "all",
        "targets": [],
        "required_predicates": [],
        "postcondition_required": True,
        "postcondition_strategy": "required_tool_obligation_ledger",
        "status": status,
        "status_reason": (
            "Required tool obligations were not satisfied: "
            + ", ".join(unsatisfied_tools)
        ),
        "failure_code": failure_codes[0],
        "failure_codes": failure_codes,
        "required_tool_obligations": dict(ledger),
    }
