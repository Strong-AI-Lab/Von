"""Build compact, typed tool-observation ledgers for turn diagnostics.

The ledger is an execution-observation surface.  It records what the runtime
actually attempted or observed without judging whether the final answer used
that evidence correctly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from ..workflows.terminal_outcome_receipts import (
    build_terminal_outcome_receipt_projection,
)

TOOL_OBSERVATION_LEDGER_SCHEMA_VERSION = "tool_observation_ledger.v1"

_EMPTY_RESULT_MARKERS = (
    "no results",
    "no result",
    "nothing found",
    "empty result",
    "returned 0",
    "0 results",
)
_INVALID_ARGUMENT_MARKERS = (
    "input validation error",
    "schema_validation_failed",
    "validation_failed",
    "invalid argument",
    "invalid arguments",
    "missing required field",
    "not one of",
)
_AUTH_FAILURE_MARKERS = (
    "auth failed",
    "authentication failed",
    "not authenticated",
    "unauthorized",
    "unauthorised",
    "forbidden",
    "oauth",
    "credential",
    "permission denied",
    "401",
    "403",
)
_UNAVAILABLE_MARKERS = (
    "tool unavailable",
    "unavailable",
    "unknown tool",
    "not available",
    "not found in catalogue",
    "not found in catalog",
    "no such tool",
)
_TIMEOUT_MARKERS = ("timeout", "timed out", "deadline exceeded")
_CANCELLED_MARKERS = ("cancelled", "canceled", "aborted")

_EMPTY_CONTAINER_KEYS = (
    "results",
    "items",
    "rows",
    "records",
    "documents",
    "matches",
    "messages",
    "papers",
    "concepts",
    "relations",
    "data",
)
_COUNT_KEYS = ("count", "total", "total_count", "result_count", "match_count")


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _lower_text(*values: Any) -> str:
    return " ".join(_safe_str(value) for value in values if _safe_str(value)).lower()


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _bounded_text(value: Any, *, max_chars: int = 300) -> str | None:
    cleaned = _safe_str(value)
    if not cleaned:
        return None
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - 3)] + "..."


def _payload_fingerprint(value: Any) -> str | None:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    except Exception:
        encoded = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def _mapping_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def _normalise_tool_name(entry: Mapping[str, Any]) -> str:
    return _safe_str(
        _mapping_value(entry, "tool", "tool_name", "method", "name")
    )


def _normalise_call_id(entry: Mapping[str, Any]) -> str | None:
    return _bounded_text(_mapping_value(entry, "call_id", "callId"), max_chars=120)


def _normalise_result_summary(entry: Mapping[str, Any]) -> str | None:
    direct = _bounded_text(
        _mapping_value(entry, "result_summary", "resultSummary", "summary"),
        max_chars=300,
    )
    if direct:
        return direct
    for key in ("effective_payload", "result", "payload"):
        value = entry.get(key)
        if not isinstance(value, Mapping):
            continue
        nested = _bounded_text(
            _mapping_value(value, "result_summary", "resultSummary", "summary"),
            max_chars=300,
        )
        if nested:
            return nested
    return None


def _normalise_error(entry: Mapping[str, Any]) -> str | None:
    direct = _bounded_text(
        _mapping_value(entry, "error", "message", "latest_error"),
        max_chars=300,
    )
    if direct:
        return direct
    for key in ("effective_payload", "result", "payload"):
        value = entry.get(key)
        if not isinstance(value, Mapping):
            continue
        nested = _bounded_text(
            _mapping_value(value, "error", "message", "error_message"),
            max_chars=300,
        )
        if nested:
            return nested
    return None


def _extract_result_payload(entry: Mapping[str, Any]) -> Any:
    if "effective_payload" in entry:
        return entry.get("effective_payload")
    if "result" in entry:
        return entry.get("result")
    return None


def _normalise_explicit_status(entry: Mapping[str, Any]) -> str:
    raw_status = _safe_str(
        _mapping_value(
            entry,
            "status",
            "action_status",
            "outcome",
            "terminal_status",
            "final_status",
        )
    ).lower()
    success = entry.get("success")
    if not raw_status and isinstance(success, bool):
        raw_status = "ok" if success else "error"
    return raw_status


def _result_emptiness(value: Any, *, result_summary: str | None = None) -> bool | None:
    if result_summary and _contains_any(result_summary, _EMPTY_RESULT_MARKERS):
        return True
    if value is None:
        return None
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return len(value) == 0
    if not isinstance(value, Mapping):
        return None

    explicit_success = value.get("success")
    status_text = _lower_text(value.get("status"), value.get("error"))
    if explicit_success is False or _contains_any(status_text, _INVALID_ARGUMENT_MARKERS):
        return None

    observed_container = False
    observed_non_empty = False
    for key in _EMPTY_CONTAINER_KEYS:
        item = value.get(key)
        if isinstance(item, Mapping):
            observed_container = True
            observed_non_empty = observed_non_empty or bool(item)
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            observed_container = True
            observed_non_empty = observed_non_empty or bool(item)

    if observed_non_empty:
        return False
    if observed_container:
        return True

    for key in _COUNT_KEYS:
        raw_count = value.get(key)
        if isinstance(raw_count, bool):
            continue
        if isinstance(raw_count, (int, float)):
            return int(raw_count) <= 0

    return None


def _classify_status(
    entry: Mapping[str, Any],
    *,
    result_summary: str | None,
    error: str | None,
    result_empty: bool | None,
    terminal_context: Mapping[str, Any] | None = None,
) -> str:
    explicit_status = _normalise_explicit_status(entry)
    status_text = _lower_text(
        explicit_status,
        error,
        result_summary,
        entry.get("error_code"),
        entry.get("blocked_reason"),
        entry.get("write_policy_blocked_reason"),
    )
    if terminal_context:
        status_text = f"{status_text} {_lower_text(terminal_context.get('status'), terminal_context.get('phase'), terminal_context.get('liveness_reason'))}"

    if _contains_any(status_text, _INVALID_ARGUMENT_MARKERS):
        return "invalid_args"
    if _contains_any(status_text, _AUTH_FAILURE_MARKERS):
        return "auth_failed"
    if _contains_any(status_text, _UNAVAILABLE_MARKERS):
        return "unavailable"
    if _contains_any(status_text, _TIMEOUT_MARKERS):
        return "timeout"
    if _contains_any(status_text, _CANCELLED_MARKERS):
        return "cancelled"
    if bool(entry.get("blocked")) or explicit_status in {"blocked", "tool_blocked"}:
        return "blocked"
    if explicit_status in {"tool_call_start", "pending", "running", "started"}:
        if terminal_context and _contains_any(_lower_text(terminal_context), _CANCELLED_MARKERS):
            return "cancelled"
        return "pending"
    if explicit_status in {"error", "failed", "failure", "tool_failed"} or error:
        return "error"
    if result_empty is True:
        return "empty_result"
    if result_empty is False:
        return "non_empty_result"
    if explicit_status in {"ok", "success", "succeeded", "tool_invoked", "true"}:
        return "ok"
    if explicit_status in {"false"}:
        return "error"
    return "unknown"


def _compact_argument_summary(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    summary = entry.get("tool_argument_summary")
    if isinstance(summary, Mapping):
        return {
            str(key): value
            for key, value in summary.items()
            if isinstance(key, str)
            and isinstance(value, (str, int, float, bool, type(None)))
        }
    return None


def _observation_from_entry(
    source: str,
    entry: Mapping[str, Any],
    *,
    terminal_context: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    tool_name = _normalise_tool_name(entry)
    if not tool_name:
        return None

    result_summary = _normalise_result_summary(entry)
    error = _normalise_error(entry)
    result_payload = _extract_result_payload(entry)
    result_empty = _result_emptiness(result_payload, result_summary=result_summary)
    status = _classify_status(
        entry,
        result_summary=result_summary,
        error=error,
        result_empty=result_empty,
        terminal_context=terminal_context,
    )

    observation: dict[str, Any] = {
        "source": source,
        "tool": tool_name,
        "attempted": True,
        "status": status,
    }
    call_id = _normalise_call_id(entry)
    if call_id:
        observation["call_id"] = call_id
    stage = _bounded_text(_mapping_value(entry, "stage", "phase"), max_chars=120)
    if stage:
        observation["stage"] = stage
    phase = _bounded_text(entry.get("phase"), max_chars=120)
    if phase and phase != stage:
        observation["phase"] = phase
    if result_summary:
        observation["result_summary"] = result_summary
    if error:
        observation["error"] = error
    error_code = _bounded_text(entry.get("error_code"), max_chars=120)
    if error_code:
        observation["error_code"] = error_code
    if result_empty is not None:
        observation["result_empty"] = bool(result_empty)
    argument_summary = _compact_argument_summary(entry)
    if argument_summary:
        observation["argument_summary"] = argument_summary
    fingerprint = _payload_fingerprint(result_payload)
    if fingerprint:
        observation["result_fingerprint"] = fingerprint
    return observation


def _append_observation(
    observations: list[dict[str, Any]],
    seen: set[tuple[Any, ...]],
    observation: Mapping[str, Any] | None,
) -> None:
    if not isinstance(observation, Mapping):
        return
    key = (
        observation.get("source"),
        observation.get("tool"),
        observation.get("call_id"),
        observation.get("status"),
        observation.get("result_summary"),
        observation.get("error"),
    )
    if key in seen:
        return
    seen.add(key)
    observations.append({str(item_key): item_value for item_key, item_value in observation.items() if isinstance(item_key, str)})


def _iter_diagnostic_tool_entries(
    turn_execution_diagnostics: Mapping[str, Any] | None,
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    if not isinstance(turn_execution_diagnostics, Mapping):
        return [], {}

    latest_progress = turn_execution_diagnostics.get("latest_progress")
    terminal_context = latest_progress if isinstance(latest_progress, Mapping) else {}
    entries: list[Mapping[str, Any]] = []
    for entry in turn_execution_diagnostics.get("tool_history") or []:
        if isinstance(entry, Mapping):
            entries.append(entry)
    if isinstance(latest_progress, Mapping):
        for entry in latest_progress.get("tool_history") or []:
            if isinstance(entry, Mapping):
                entries.append(entry)
        for entry in latest_progress.get("diagnostic_events") or []:
            if not isinstance(entry, Mapping):
                continue
            if _normalise_tool_name(entry):
                entries.append(entry)
    return entries, terminal_context


def _iter_validation_failure_observations(
    aux_llm_calls: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for entry in aux_llm_calls or ():
        if not isinstance(entry, Mapping):
            continue
        entry_type = _safe_str(entry.get("type")).lower()
        if entry_type != "tool_contract_attempt":
            continue
        diagnostics = entry.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(
            diagnostics, (str, bytes, bytearray)
        ):
            for diagnostic in diagnostics:
                if not isinstance(diagnostic, Mapping):
                    continue
                tool_name = _normalise_tool_name(diagnostic)
                if not tool_name:
                    continue
                observations.append(
                    {
                        "source": "aux_llm_calls.tool_contract_attempt",
                        "tool": tool_name,
                        "attempted": True,
                        "status": "invalid_args",
                        "error_code": _bounded_text(
                            diagnostic.get("error_code"), max_chars=120
                        )
                        or "schema_validation_failed",
                        "error": _bounded_text(
                            diagnostic.get("message"), max_chars=300
                        )
                        or "Tool call validation failed before execution.",
                    }
                )
            continue

        tool_calls = entry.get("tool_calls")
        if not isinstance(tool_calls, Sequence) or isinstance(
            tool_calls, (str, bytes, bytearray)
        ):
            continue
        validation_errors = [
            _safe_str(error)
            for error in (entry.get("validation_errors") or [])
            if _safe_str(error)
        ]
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                continue
            tool_name = _normalise_tool_name(tool_call)
            if not tool_name:
                continue
            matching_errors = [
                error
                for error in validation_errors
                if error.lower().startswith(f"{tool_name.lower()}:")
            ]
            for error in matching_errors:
                observations.append(
                    {
                        "source": "aux_llm_calls.tool_contract_attempt",
                        "tool": tool_name,
                        "attempted": True,
                        "status": "invalid_args",
                        "error_code": "schema_validation_failed",
                        "error": _bounded_text(error, max_chars=300),
                    }
                )
    return observations


def build_tool_observation_ledger(
    *,
    tool_invocations: Sequence[Mapping[str, Any]] | None = None,
    tool_observations: Sequence[Mapping[str, Any]] | None = None,
    turn_execution_diagnostics: Mapping[str, Any] | None = None,
    aux_llm_calls: Sequence[Mapping[str, Any]] | None = None,
    existing_ledger: Mapping[str, Any] | None = None,
    terminal_outcome_receipt: Mapping[str, Any] | None = None,
    terminal_outcome_receipt_validation: Mapping[str, Any] | None = None,
    max_observations: int = 32,
) -> dict[str, Any]:
    """Return a compact, versioned ledger of tool execution observations."""

    observations: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    if isinstance(existing_ledger, Mapping):
        for entry in existing_ledger.get("observations") or []:
            if isinstance(entry, Mapping):
                _append_observation(observations, seen, entry)

    for entry in _iter_validation_failure_observations(aux_llm_calls):
        _append_observation(observations, seen, entry)

    for entry in tool_invocations or ():
        if isinstance(entry, Mapping):
            _append_observation(
                observations,
                seen,
                _observation_from_entry("llm_debug.tool_invocations", entry),
            )

    for entry in tool_observations or ():
        if isinstance(entry, Mapping):
            source = _safe_str(entry.get("source")) or "runtime.tool_observations"
            _append_observation(
                observations,
                seen,
                _observation_from_entry(source, entry),
            )

    diagnostic_entries, terminal_context = _iter_diagnostic_tool_entries(
        turn_execution_diagnostics
    )
    for entry in diagnostic_entries:
        _append_observation(
            observations,
            seen,
            _observation_from_entry(
                "turn_execution_diagnostics.tool_history",
                entry,
                terminal_context=terminal_context,
            ),
        )

    if isinstance(max_observations, int) and max_observations > 0:
        observations = observations[-max_observations:]

    observed_tools: list[str] = []
    status_counts: dict[str, int] = {}
    for observation in observations:
        tool_name = _safe_str(observation.get("tool"))
        if tool_name and tool_name not in observed_tools:
            observed_tools.append(tool_name)
        status = _safe_str(observation.get("status")) or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    ledger = {
        "schema_version": TOOL_OBSERVATION_LEDGER_SCHEMA_VERSION,
        "observation_count": len(observations),
        "observed_tools": observed_tools,
        "status_counts": status_counts,
        "has_observed_tool": bool(observed_tools),
        "has_invalid_args": bool(status_counts.get("invalid_args")),
        "has_unavailable_or_auth_failure": bool(
            status_counts.get("auth_failed") or status_counts.get("unavailable")
        ),
        "has_empty_observation": bool(status_counts.get("empty_result")),
        "has_non_empty_observation": bool(status_counts.get("non_empty_result")),
        "has_timeout_or_cancellation": bool(
            status_counts.get("timeout")
            or status_counts.get("cancelled")
            or status_counts.get("pending")
        ),
        "observations": observations,
    }
    existing_receipt_projection = (
        existing_ledger.get("terminal_outcome_receipt_projection")
        if isinstance(existing_ledger, Mapping)
        and isinstance(
            existing_ledger.get("terminal_outcome_receipt_projection"), Mapping
        )
        else None
    )
    effective_receipt = terminal_outcome_receipt
    effective_validation = terminal_outcome_receipt_validation
    if not isinstance(effective_receipt, Mapping) and isinstance(
        existing_receipt_projection, Mapping
    ):
        existing_receipt = existing_receipt_projection.get("receipt")
        existing_validation = existing_receipt_projection.get("validation")
        effective_receipt = (
            existing_receipt if isinstance(existing_receipt, Mapping) else None
        )
        effective_validation = (
            existing_validation if isinstance(existing_validation, Mapping) else None
        )
    receipt_projection = build_terminal_outcome_receipt_projection(
        effective_receipt,
        validation=effective_validation,
        source="tool_observation_ledger",
    )
    if receipt_projection.get("available"):
        ledger["terminal_outcome_receipt_projection"] = receipt_projection
    return ledger
