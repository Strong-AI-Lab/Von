"""Metadata-driven validation helpers for workflow execution.

These checks operationalise Vontology workflow metadata at runtime:
- preconditions / reads are validated before executing a state's actions
- effects / writes are validated after executing a state's actions

The result objects intentionally carry structured check details so executors can
emit deterministic diagnostics now and feed stronger inference layers later.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, MutableMapping, Sequence, Tuple

REASON_PRECONDITION_UNSATISFIED = "metadata_precondition_unsatisfied"
REASON_READ_VARIABLE_MISSING = "metadata_read_variable_missing"
REASON_READ_CONTEXT_KEY_MISSING = "metadata_read_context_key_missing"
REASON_EFFECT_UNSATISFIED = "metadata_effect_unsatisfied"
REASON_WRITE_VARIABLE_MISSING = "metadata_write_variable_missing"
REASON_WRITE_CONTEXT_KEY_MISSING = "metadata_write_context_key_missing"

WORKFLOW_METADATA_EVENTS_KEY = "workflow_metadata_validation_events"
LAST_METADATA_EVENT_KEY = "last_metadata_validation"
METADATA_VALIDATION_MODE_ENV_VAR = "VON_WORKFLOW_METADATA_VALIDATION_MODE"
METADATA_VALIDATION_MODE_ENFORCE = "enforce"
METADATA_VALIDATION_MODE_WARN = "warn"
METADATA_VALIDATION_MODE_OFF = "off"
_METADATA_VALIDATION_MODES: Tuple[str, ...] = (
    METADATA_VALIDATION_MODE_ENFORCE,
    METADATA_VALIDATION_MODE_WARN,
    METADATA_VALIDATION_MODE_OFF,
)

_NORMALISE_SYMBOL_RE = re.compile(r"[^0-9A-Za-z_]+")
_NESTED_CONTEXT_KEYS: Tuple[str, ...] = (
    "facts",
    "state",
    "flags",
    "variables",
    "effects",
    "preconditions",
    "conditions",
)


@dataclass(frozen=True)
class MetadataValidationFailure:
    reason_code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetadataValidationResult:
    state_id: str
    phase: str
    ok: bool
    applied: bool
    checks: Sequence[Mapping[str, Any]] = ()
    failure: MetadataValidationFailure | None = None
    skipped: bool = False
    skip_reason: str | None = None
    mode: str = METADATA_VALIDATION_MODE_ENFORCE
    enforced: bool = True

    def to_trace_verdict(self) -> Dict[str, Any]:
        verdict: Dict[str, Any] = {
            "status": "metadata_validation",
            "phase": self.phase,
            "ok": self.ok,
            "applied": self.applied,
            "mode": self.mode,
            "enforced": self.enforced,
        }
        if self.checks:
            verdict["checks"] = [dict(item) for item in self.checks]
        if self.skipped:
            verdict["skipped"] = True
            if self.skip_reason:
                verdict["skip_reason"] = self.skip_reason
        if self.failure is not None:
            verdict["reason_code"] = self.failure.reason_code
            verdict["message"] = self.failure.message
            if self.failure.details:
                verdict["details"] = dict(self.failure.details)
        return verdict

    def to_context_event(self) -> Dict[str, Any]:
        event = self.to_trace_verdict()
        event["state_id"] = self.state_id
        return event


def resolve_metadata_validation_mode(mode: str | None) -> str:
    candidate = (
        mode.strip().lower()
        if isinstance(mode, str)
        else METADATA_VALIDATION_MODE_ENFORCE
    )
    if candidate in _METADATA_VALIDATION_MODES:
        return candidate
    return METADATA_VALIDATION_MODE_ENFORCE


def get_metadata_validation_mode() -> str:
    return resolve_metadata_validation_mode(
        os.getenv(METADATA_VALIDATION_MODE_ENV_VAR)
    )


def metadata_validation_failures_are_enforced(mode: str | None = None) -> bool:
    resolved = (
        get_metadata_validation_mode()
        if mode is None
        else resolve_metadata_validation_mode(mode)
    )
    return resolved == METADATA_VALIDATION_MODE_ENFORCE


def apply_metadata_validation_mode(
    *,
    result: MetadataValidationResult,
    mode: str | None = None,
) -> MetadataValidationResult:
    resolved = (
        get_metadata_validation_mode()
        if mode is None
        else resolve_metadata_validation_mode(mode)
    )
    return replace(
        result,
        mode=resolved,
        enforced=(resolved == METADATA_VALIDATION_MODE_ENFORCE),
    )


def _normalise_metadata_items(value: Any) -> List[str]:
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []
    if isinstance(value, list):
        normalised: List[str] = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    normalised.append(stripped)
        return normalised
    return []


def _symbol_candidates(symbol: str) -> List[str]:
    raw = symbol.strip()
    if not raw:
        return []

    candidates: List[str] = [raw]

    if raw.startswith("#V#") and len(raw) > 3:
        candidates.append(raw[3:])
    if raw.startswith("#"):
        stripped = raw.lstrip("#")
        if stripped:
            candidates.append(stripped)

    expanded = list(candidates)
    for candidate in expanded:
        normalised = _NORMALISE_SYMBOL_RE.sub("_", candidate).strip("_")
        if normalised:
            candidates.append(normalised)
            candidates.append(normalised.lower())
            if normalised.startswith("workflow_context_key_"):
                suffix = normalised[len("workflow_context_key_") :]
                if suffix:
                    candidates.append(suffix)
                    candidates.append(suffix.lower())
            if normalised.startswith("context_key_"):
                suffix = normalised[len("context_key_") :]
                if suffix:
                    candidates.append(suffix)
                    candidates.append(suffix.lower())

    unique: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _lookup_symbol(
    context: Mapping[str, Any],
    symbol: str,
) -> tuple[bool, Any, str | None, str | None]:
    candidates = _symbol_candidates(symbol)
    if not candidates:
        return False, None, None, None

    for key in candidates:
        if key in context:
            return True, context[key], key, "context"

    for nested_key in _NESTED_CONTEXT_KEYS:
        container = context.get(nested_key)
        if isinstance(container, Mapping):
            for key in candidates:
                if key in container:
                    return True, container[key], key, nested_key
        if isinstance(container, (list, tuple, set)):
            for key in candidates:
                if key in container:
                    return True, True, key, nested_key

    return False, None, None, None


def _build_check(
    *,
    check_type: str,
    symbol: str,
    status: str,
    matched_key: str | None = None,
    source: str | None = None,
    value: Any = None,
    changed: bool | None = None,
) -> Dict[str, Any]:
    check: Dict[str, Any] = {
        "type": check_type,
        "symbol": symbol,
        "status": status,
    }
    if matched_key:
        check["matched_key"] = matched_key
    if source:
        check["source"] = source
    if value is not None:
        check["value"] = bool(value)
    if changed is not None:
        check["changed"] = changed
    return check


def _failure(
    *,
    state_id: str,
    phase: str,
    reason_code: str,
    symbol: str,
    check_type: str,
    checks: List[Mapping[str, Any]],
) -> MetadataValidationResult:
    message = (
        f"{phase} metadata validation failed for state '{state_id}' "
        f"({check_type} '{symbol}')"
    )
    return MetadataValidationResult(
        state_id=state_id,
        phase=phase,
        ok=False,
        applied=True,
        checks=tuple(checks),
        failure=MetadataValidationFailure(
            reason_code=reason_code,
            message=message,
            details={
                "state_id": state_id,
                "phase": phase,
                "symbol": symbol,
                "check_type": check_type,
            },
        ),
    )


def validate_state_metadata_pre_action(
    *,
    state_id: str,
    metadata: Mapping[str, Any] | None,
    context: Mapping[str, Any],
) -> MetadataValidationResult:
    metadata = metadata or {}
    preconditions = _normalise_metadata_items(metadata.get("preconditions"))
    reads = _normalise_metadata_items(metadata.get("reads_variables"))
    reads_context_keys = _normalise_metadata_items(metadata.get("reads_context_keys"))
    applied = bool(preconditions or reads or reads_context_keys)
    checks: List[Mapping[str, Any]] = []

    for symbol in preconditions:
        found, value, matched_key, source = _lookup_symbol(context, symbol)
        if not found:
            checks.append(
                _build_check(
                    check_type="precondition",
                    symbol=symbol,
                    status="missing",
                )
            )
            return _failure(
                state_id=state_id,
                phase="pre_action",
                reason_code=REASON_PRECONDITION_UNSATISFIED,
                symbol=symbol,
                check_type="precondition",
                checks=checks,
            )
        satisfied = bool(value)
        checks.append(
            _build_check(
                check_type="precondition",
                symbol=symbol,
                status="satisfied" if satisfied else "unsatisfied",
                matched_key=matched_key,
                source=source,
                value=value,
            )
        )
        if not satisfied:
            return _failure(
                state_id=state_id,
                phase="pre_action",
                reason_code=REASON_PRECONDITION_UNSATISFIED,
                symbol=symbol,
                check_type="precondition",
                checks=checks,
            )

    for symbol in reads:
        found, _, matched_key, source = _lookup_symbol(context, symbol)
        checks.append(
            _build_check(
                check_type="read_variable",
                symbol=symbol,
                status="available" if found else "missing",
                matched_key=matched_key,
                source=source,
            )
        )
        if not found:
            return _failure(
                state_id=state_id,
                phase="pre_action",
                reason_code=REASON_READ_VARIABLE_MISSING,
                symbol=symbol,
                check_type="read_variable",
                checks=checks,
            )

    for symbol in reads_context_keys:
        found, _, matched_key, source = _lookup_symbol(context, symbol)
        checks.append(
            _build_check(
                check_type="read_context_key",
                symbol=symbol,
                status="available" if found else "missing",
                matched_key=matched_key,
                source=source,
            )
        )
        if not found:
            return _failure(
                state_id=state_id,
                phase="pre_action",
                reason_code=REASON_READ_CONTEXT_KEY_MISSING,
                symbol=symbol,
                check_type="read_context_key",
                checks=checks,
            )

    return MetadataValidationResult(
        state_id=state_id,
        phase="pre_action",
        ok=True,
        applied=applied,
        checks=tuple(checks),
    )


def validate_state_metadata_post_action(
    *,
    state_id: str,
    metadata: Mapping[str, Any] | None,
    context_before: Mapping[str, Any],
    context_after: Mapping[str, Any],
) -> MetadataValidationResult:
    metadata = metadata or {}
    effects = _normalise_metadata_items(metadata.get("effects"))
    writes = _normalise_metadata_items(metadata.get("writes_variables"))
    writes_context_keys = _normalise_metadata_items(
        metadata.get("writes_context_keys")
    )
    applied = bool(effects or writes or writes_context_keys)
    checks: List[Mapping[str, Any]] = []

    for symbol in effects:
        found, value, matched_key, source = _lookup_symbol(context_after, symbol)
        if not found:
            checks.append(
                _build_check(
                    check_type="effect",
                    symbol=symbol,
                    status="missing",
                )
            )
            return _failure(
                state_id=state_id,
                phase="post_action",
                reason_code=REASON_EFFECT_UNSATISFIED,
                symbol=symbol,
                check_type="effect",
                checks=checks,
            )
        satisfied = bool(value)
        checks.append(
            _build_check(
                check_type="effect",
                symbol=symbol,
                status="satisfied" if satisfied else "unsatisfied",
                matched_key=matched_key,
                source=source,
                value=value,
            )
        )
        if not satisfied:
            return _failure(
                state_id=state_id,
                phase="post_action",
                reason_code=REASON_EFFECT_UNSATISFIED,
                symbol=symbol,
                check_type="effect",
                checks=checks,
            )

    for symbol in writes:
        after_found, after_value, matched_key, source = _lookup_symbol(
            context_after, symbol
        )
        if not after_found:
            checks.append(
                _build_check(
                    check_type="write_variable",
                    symbol=symbol,
                    status="missing",
                )
            )
            return _failure(
                state_id=state_id,
                phase="post_action",
                reason_code=REASON_WRITE_VARIABLE_MISSING,
                symbol=symbol,
                check_type="write_variable",
                checks=checks,
            )
        before_found, before_value, _, _ = _lookup_symbol(context_before, symbol)
        changed = (not before_found) or before_value != after_value
        checks.append(
            _build_check(
                check_type="write_variable",
                symbol=symbol,
                status="available",
                matched_key=matched_key,
                source=source,
                changed=changed,
            )
        )

    for symbol in writes_context_keys:
        after_found, after_value, matched_key, source = _lookup_symbol(
            context_after, symbol
        )
        if not after_found:
            checks.append(
                _build_check(
                    check_type="write_context_key",
                    symbol=symbol,
                    status="missing",
                )
            )
            return _failure(
                state_id=state_id,
                phase="post_action",
                reason_code=REASON_WRITE_CONTEXT_KEY_MISSING,
                symbol=symbol,
                check_type="write_context_key",
                checks=checks,
            )
        before_found, before_value, _, _ = _lookup_symbol(context_before, symbol)
        changed = (not before_found) or before_value != after_value
        checks.append(
            _build_check(
                check_type="write_context_key",
                symbol=symbol,
                status="available",
                matched_key=matched_key,
                source=source,
                changed=changed,
            )
        )

    return MetadataValidationResult(
        state_id=state_id,
        phase="post_action",
        ok=True,
        applied=applied,
        checks=tuple(checks),
    )


def skipped_metadata_validation(
    *,
    state_id: str,
    phase: str,
    reason: str,
    mode: str | None = None,
) -> MetadataValidationResult:
    resolved_mode = (
        get_metadata_validation_mode()
        if mode is None
        else resolve_metadata_validation_mode(mode)
    )
    return MetadataValidationResult(
        state_id=state_id,
        phase=phase,
        ok=True,
        applied=True,
        checks=(),
        skipped=True,
        skip_reason=reason,
        mode=resolved_mode,
        enforced=False,
    )


def append_metadata_validation_event(
    *,
    context: MutableMapping[str, Any],
    result: MetadataValidationResult,
) -> None:
    if not result.applied and result.ok:
        return
    events = context.get(WORKFLOW_METADATA_EVENTS_KEY)
    if not isinstance(events, list):
        events = []
        context[WORKFLOW_METADATA_EVENTS_KEY] = events
    event = result.to_context_event()
    events.append(event)
    context[LAST_METADATA_EVENT_KEY] = event


def format_metadata_validation_error(result: MetadataValidationResult) -> str:
    if result.failure is None:
        return "metadata_validation_failed"
    symbol = result.failure.details.get("symbol")
    if isinstance(symbol, str) and symbol:
        return (
            "metadata_validation_failed:"
            f"{result.failure.reason_code}:{result.state_id}:{symbol}"
        )
    return (
        "metadata_validation_failed:"
        f"{result.failure.reason_code}:{result.state_id}"
    )

