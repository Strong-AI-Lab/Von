"""Support helpers for tool argument grounding before gateway dispatch."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .schemas import Schema as McpSchema
from .tool_call_contracts import validation_diagnostic


def is_unresolved_tool_argument_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate:
        return False
    lowered = candidate.casefold()
    if lowered in {"default", "primary"}:
        return True
    if lowered.startswith("user_profile_"):
        suffix = lowered.removeprefix("user_profile_")
        return bool(suffix) and suffix.isdigit()
    return False


def gmail_profile_payload_fields(
    payload: Mapping[str, Any],
    schema: McpSchema | None,
) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()

    def _add(field_name: str) -> None:
        if field_name not in seen:
            seen.add(field_name)
            fields.append(field_name)

    for field_name in ("profile_id", "profile"):
        if field_name in payload:
            _add(field_name)

    if schema is not None:
        schema_fields = set(schema.required.keys()) | set(schema.optional.keys())
        for field_name in ("profile_id", "profile"):
            if field_name in schema_fields:
                _add(field_name)
        for alias, canonical in schema.aliases.items():
            if canonical in {"profile_id", "profile"} and alias in payload:
                _add(alias)
                _add(canonical)

    return fields


def gmail_default_profile_field(
    payload: Mapping[str, Any],
    schema: McpSchema | None,
) -> str | None:
    profile_fields = gmail_profile_payload_fields(payload, schema)
    if "profile_id" in profile_fields:
        return "profile_id"
    if "profile" in profile_fields:
        return "profile"
    if schema is None:
        return "profile"
    return None


def apply_gmail_profile_default(
    payload: dict[str, Any],
    *,
    schema: McpSchema | None,
    selected_gmail_profile: str | None,
) -> dict[str, Any] | None:
    profile_field = gmail_default_profile_field(payload, schema)
    if profile_field is None:
        return None

    profile_value = payload.get(profile_field)
    profile_text = (
        str(profile_value or "").strip() if isinstance(profile_value, str) else ""
    )
    selected_profile_text = (
        str(selected_gmail_profile or "").strip()
        if isinstance(selected_gmail_profile, str)
        else ""
    )
    selected_profile_is_placeholder = is_unresolved_tool_argument_placeholder(
        selected_profile_text
    )
    profile_is_placeholder = is_unresolved_tool_argument_placeholder(profile_text)
    if not selected_profile_text or selected_profile_is_placeholder:
        return None
    if profile_text and not (
        profile_is_placeholder and selected_profile_text != profile_text
    ):
        return None

    payload[profile_field] = selected_gmail_profile
    return {
        "field": profile_field,
        "source": (
            "selected_gmail_profile_placeholder_replacement"
            if profile_is_placeholder
            else "selected_gmail_profile"
        ),
        "value_present": True,
    }


def placeholder_tool_argument_diagnostics(
    *,
    tool_name: str,
    payload: Mapping[str, Any],
    schema: McpSchema | None,
    contract: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> tuple[list[str], list[Mapping[str, Any]]]:
    if not tool_name.startswith("gmail_"):
        return [], []

    errors: list[str] = []
    diagnostics: list[Mapping[str, Any]] = []
    for field_name in gmail_profile_payload_fields(payload, schema):
        value = payload.get(field_name)
        if not is_unresolved_tool_argument_placeholder(value):
            continue
        placeholder = str(value).strip()
        error = (
            f"{tool_name}: Field '{field_name}' contains unresolved placeholder "
            f"'{placeholder}'. Resolve a concrete Gmail profile alias before "
            "calling this tool."
        )
        diagnostic = validation_diagnostic(
            tool=tool_name,
            error_code="placeholder_tool_argument_unresolved",
            message=error,
            payload=payload,
            contract=contract,
            warnings=warnings,
        )
        diagnostic["field"] = field_name
        diagnostic["placeholder_value"] = placeholder
        diagnostics.append(diagnostic)
        errors.append(error)
    return errors, diagnostics


def extend_placeholder_tool_argument_diagnostics(
    *,
    errors: list[str],
    diagnostics: list[Mapping[str, Any]],
    tool_name: str,
    payload: Mapping[str, Any],
    schema: McpSchema | None,
    contract: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> bool:
    placeholder_errors, placeholder_diagnostics = placeholder_tool_argument_diagnostics(
        tool_name=tool_name,
        payload=payload,
        schema=schema,
        contract=contract,
        warnings=warnings,
    )
    if not placeholder_errors:
        return False
    errors.extend(placeholder_errors)
    diagnostics.extend(placeholder_diagnostics)
    return True


def placeholder_tool_argument_block(
    *,
    tool_name: str,
    payload: Mapping[str, Any],
    payload_before_invoke: Mapping[str, Any],
    schema: McpSchema | None,
    contract: Mapping[str, Any] | None = None,
    call_id: Any = None,
    stage: str,
    batch_size: int,
    tool_calls_done: int,
    tool_calls_cap: int,
    tool_calls_remaining: int,
    tool_argument_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    errors, diagnostics = placeholder_tool_argument_diagnostics(
        tool_name=tool_name,
        payload=payload,
        schema=schema,
        contract=contract,
    )
    if not errors:
        return None

    message = "; ".join(errors)
    invocation_record: dict[str, Any] = {
        "tool": tool_name,
        "payload": dict(payload_before_invoke),
        "effective_arguments": dict(payload),
        "error": message,
        "blocked": True,
        "blocked_reason": "unresolved_placeholder_tool_argument",
        "diagnostics": [dict(item) for item in diagnostics],
    }
    if call_id:
        invocation_record["call_id"] = call_id

    progress_payload: dict[str, Any] = {
        "status": "tool_blocked",
        "tool": tool_name,
        "batch_size": batch_size,
        "tool_calls_done": tool_calls_done,
        "tool_calls_cap": tool_calls_cap,
        "tool_calls_remaining": tool_calls_remaining,
        "call_id": call_id,
        "error": message,
        "blocked_reason": "unresolved_placeholder_tool_argument",
    }
    if tool_argument_summary:
        progress_payload["tool_argument_summary"] = dict(tool_argument_summary)

    telemetry_payload = {
        "type": "tool_argument_resolution_blocked",
        "stage": stage,
        "tool": tool_name,
        "blocked_reason": "unresolved_placeholder_tool_argument",
        "diagnostics": [dict(item) for item in diagnostics],
    }
    return {
        "message": message,
        "invocation_record": invocation_record,
        "progress_payload": progress_payload,
        "telemetry_payload": telemetry_payload,
    }
