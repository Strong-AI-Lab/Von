"""Required-tool obligation accounting for tool-using workflow turns.

This module is deliberately policy-light.  It does not decide that a domain
task needs a particular ontology mutation; that remains authored in expected
outcome contracts, workflows, prompts, Vontology artefacts, and tool metadata.
The support role here is to preserve and evaluate the obligations once those
authority surfaces have declared required tools.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .tool_metadata_service import (
    is_tool_search_evidence,
    is_tool_verification_read,
    is_tool_write,
)

REQUIRED_TOOL_OBLIGATION_LEDGER_SCHEMA_VERSION = "required_tool_obligation_ledger.v1"

OPERATION_SEARCH_OR_RESOLUTION_READ = "search_or_resolution_read"
OPERATION_VERIFICATION_READ = "verification_read"
OPERATION_MUTATION_WRITE = "mutation_write"
OPERATION_EXTERNAL_SIDE_EFFECT = "external_side_effect"
OPERATION_WORKFLOW_EXECUTE = "workflow_execute"

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
    lowered = cleaned.lower()
    if not lowered:
        return OPERATION_EXTERNAL_SIDE_EFFECT

    if lowered == "workflow_execute":
        return OPERATION_WORKFLOW_EXECUTE
    if is_tool_write(cleaned):
        return OPERATION_MUTATION_WRITE
    if lowered.startswith(("search_", "list_", "resolve_", "find_")):
        return OPERATION_SEARCH_OR_RESOLUTION_READ
    if is_tool_verification_read(cleaned):
        return OPERATION_VERIFICATION_READ
    if is_tool_search_evidence(cleaned):
        return OPERATION_SEARCH_OR_RESOLUTION_READ

    if lowered.startswith(("create_", "add_", "upsert_", "mark_", "materialise_")):
        return OPERATION_MUTATION_WRITE
    if lowered.startswith(("get_", "fetch_", "read_")):
        return OPERATION_VERIFICATION_READ
    if lowered.startswith("workflow_"):
        return OPERATION_WORKFLOW_EXECUTE
    return OPERATION_EXTERNAL_SIDE_EFFECT


def _method_lookup(method_catalogue: Mapping[str, Any] | None) -> set[str] | None:
    if not isinstance(method_catalogue, Mapping):
        return None
    return {
        str(name).strip().lower()
        for name in method_catalogue.keys()
        if str(name).strip()
    }


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
    return raw_status or "unknown"


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
        failures_by_tool = tool_call_validation_failure_context.get(
            "failures_by_tool"
        )
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
    if not isinstance(errors, Sequence) or isinstance(
        errors, (str, bytes, bytearray)
    ):
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
    if not isinstance(errors, Sequence) or isinstance(
        errors, (str, bytes, bytearray)
    ):
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
    planned_tool_calls: Sequence[Mapping[str, Any]] | None = None,
    tool_call_validation_failure_context: Mapping[str, Any] | None = None,
    tool_call_validation_errors: Sequence[Mapping[str, Any]] | None = None,
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
    attempted_counts: dict[str, int] = {}
    successful_counts: dict[str, int] = {}
    last_status_by_tool: dict[str, str] = {}
    last_invocation_by_tool: dict[str, Mapping[str, Any]] = {}
    attempted_operation_classes: list[str] = []
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
        last_status_by_tool[lowered] = status
        last_invocation_by_tool[lowered] = invocation
        attempted_operation_classes.append(classify_required_tool_operation(tool_name))
        if status == "ok":
            successful_counts[lowered] = successful_counts.get(lowered, 0) + 1

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
        attempted_operation_classes.append(classify_required_tool_operation(tool_name))

    obligations: list[dict[str, Any]] = []
    for tool_name, sources in sources_by_tool.items():
        cleaned_tool = _safe_str(tool_name)
        if not cleaned_tool:
            continue
        lowered = cleaned_tool.lower()
        operation_class = classify_required_tool_operation(cleaned_tool)
        existing = existing_lookup.get(lowered, {})

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
        satisfied = (
            successful_count > 0
            and bool(allowed_by_policy)
            and available_on_gateway is not False
        )

        blocking_reason = ""
        failure_class = ""
        if not bool(allowed_by_policy):
            blocking_reason = (
                BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
            )
            failure_class = BLOCKER_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
        elif available_on_gateway is False:
            blocking_reason = BLOCKER_REQUIRED_TOOL_NOT_AVAILABLE_ON_GATEWAY
            failure_class = BLOCKER_REQUIRED_TOOL_NOT_AVAILABLE_ON_GATEWAY
        elif not satisfied:
            if validation_failure is not None:
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
    observed_invocation_count = len(
        [item for item in invocations or () if isinstance(item, Mapping)]
    ) + observed_equivalent_invocation_count
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
