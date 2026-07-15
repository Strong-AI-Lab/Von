"""Context-scoped MCP fault injection for isolated AgentTest execution.

This module is a generic test support surface.  Represented certification cases
own which tool should encounter which fault; Python only validates that bounded
plan, enforces the AgentTest-only boundary, and exposes typed evidence when the
gateway applies it.

There is deliberately no request-header or payload fallback.  A trusted
in-process harness must bind a plan explicitly, so an ordinary caller cannot
turn user-controlled MCP arguments into a fault-injection control plane.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from typing import Any


AGENT_TEST_MCP_FAULT_PLAN_SCHEMA_VERSION = "agent_test_mcp_fault_plan.v1"
AGENT_TEST_MCP_FAULT_EVENT_SCHEMA_VERSION = "agent_test_mcp_fault_event.v1"

_FAULT_CLASS_TIMEOUT = "timeout"
_FAULT_CLASS_RATE_LIMIT = "rate_limit"
_FAULT_CLASS_TEMPORARY_UNAVAILABILITY = "temporary_unavailability"
_SUPPORTED_FAULT_CLASSES = frozenset(
    {
        _FAULT_CLASS_TIMEOUT,
        _FAULT_CLASS_RATE_LIMIT,
        _FAULT_CLASS_TEMPORARY_UNAVAILABILITY,
    }
)
_MAX_FAULT_RULES = 32
_MAX_ACTIVATIONS_PER_RULE = 100
_MAX_RETRY_AFTER_SECONDS = 3600.0
_MAX_PLAN_ID_LENGTH = 128
_MAX_FAULT_ID_LENGTH = 128
_MAX_TOOL_NAME_LENGTH = 128
_SAFE_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9#][A-Za-z0-9#._:-]*$")
_SAFE_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

_PLAN_FIELDS = frozenset({"schema_version", "plan_id", "faults"})
_RULE_FIELDS = frozenset(
    {
        "fault_id",
        "tool_name",
        "fault_class",
        "max_activations",
        "retry_after_seconds",
    }
)


class AgentTestMCPFaultPlanError(ValueError):
    """Raised when an AgentTest MCP fault plan is unsafe or malformed."""


@dataclass
class _FaultRule:
    fault_id: str
    tool_name: str
    fault_class: str
    max_activations: int
    retry_after_seconds: float | None
    activation_count: int = 0


@dataclass(frozen=True)
class AgentTestMCPFaultOutcome:
    """One typed gateway outcome produced by a bound AgentTest fault plan."""

    error_code: str
    payload: Mapping[str, Any]
    event: Mapping[str, Any]


@dataclass
class AgentTestMCPFaultScope:
    """Mutable activation state and bounded telemetry for one bound plan."""

    plan_id: str
    plan_sha256: str
    rules: list[_FaultRule]
    _events: list[dict[str, Any]] = field(default_factory=list)

    def event_snapshot(self) -> tuple[dict[str, Any], ...]:
        """Return a caller-owned snapshot suitable for campaign telemetry."""

        return tuple(dict(event) for event in self._events)


_ACTIVE_FAULT_SCOPE: ContextVar[AgentTestMCPFaultScope | None] = ContextVar(
    "agent_test_mcp_fault_scope",
    default=None,
)


def _truthy_env(name: str) -> bool:
    value = os.getenv(name)
    return isinstance(value, str) and value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _agent_test_enabled() -> bool:
    return _truthy_env("VON_AGENT_TEST_INSTANCE")


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bounded_reference(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    cleaned = _clean_text(value)
    if (
        not cleaned
        or len(cleaned) > max_length
        or _SAFE_REFERENCE_PATTERN.fullmatch(cleaned) is None
    ):
        raise AgentTestMCPFaultPlanError(f"{field_name}_invalid")
    return cleaned


def _bounded_tool_name(value: Any, *, field_name: str) -> str:
    cleaned = _clean_text(value)
    if (
        not cleaned
        or len(cleaned) > _MAX_TOOL_NAME_LENGTH
        or _SAFE_TOOL_NAME_PATTERN.fullmatch(cleaned) is None
    ):
        raise AgentTestMCPFaultPlanError(f"{field_name}_invalid")
    return cleaned


def _bounded_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentTestMCPFaultPlanError(f"{field_name}_must_be_integer")
    if value < 1 or value > _MAX_ACTIVATIONS_PER_RULE:
        raise AgentTestMCPFaultPlanError(f"{field_name}_out_of_range")
    return value


def _bounded_retry_after(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentTestMCPFaultPlanError("retry_after_seconds_must_be_number")
    retry_after = float(value)
    if (
        not math.isfinite(retry_after)
        or retry_after < 0.0
        or retry_after > _MAX_RETRY_AFTER_SECONDS
    ):
        raise AgentTestMCPFaultPlanError("retry_after_seconds_out_of_range")
    return retry_after


def _stable_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalise_fault_plan(plan: Mapping[str, Any]) -> AgentTestMCPFaultScope:
    unknown_plan_fields = sorted(str(key) for key in set(plan) - _PLAN_FIELDS)
    if unknown_plan_fields:
        raise AgentTestMCPFaultPlanError(
            "agent_test_mcp_fault_plan_unknown_fields:" + ",".join(unknown_plan_fields)
        )
    if plan.get("schema_version") != AGENT_TEST_MCP_FAULT_PLAN_SCHEMA_VERSION:
        raise AgentTestMCPFaultPlanError("agent_test_mcp_fault_plan_schema_unsupported")

    raw_faults = plan.get("faults")
    if not isinstance(raw_faults, Sequence) or isinstance(
        raw_faults,
        (str, bytes, bytearray),
    ):
        raise AgentTestMCPFaultPlanError("agent_test_mcp_faults_must_be_list")
    if not raw_faults or len(raw_faults) > _MAX_FAULT_RULES:
        raise AgentTestMCPFaultPlanError("agent_test_mcp_fault_count_out_of_range")

    normalised_rules: list[dict[str, Any]] = []
    rules: list[_FaultRule] = []
    seen_fault_ids: set[str] = set()
    for index, raw_rule in enumerate(raw_faults, start=1):
        if not isinstance(raw_rule, Mapping):
            raise AgentTestMCPFaultPlanError(
                f"agent_test_mcp_fault_{index}_must_be_object"
            )
        unknown_rule_fields = sorted(str(key) for key in set(raw_rule) - _RULE_FIELDS)
        if unknown_rule_fields:
            raise AgentTestMCPFaultPlanError(
                f"agent_test_mcp_fault_{index}_unknown_fields:"
                + ",".join(unknown_rule_fields)
            )

        raw_fault_id = raw_rule.get("fault_id")
        fault_id = (
            _bounded_reference(
                raw_fault_id,
                field_name=f"agent_test_mcp_fault_{index}_fault_id",
                max_length=_MAX_FAULT_ID_LENGTH,
            )
            if raw_fault_id is not None
            else f"fault_{index}"
        )
        if fault_id in seen_fault_ids:
            raise AgentTestMCPFaultPlanError(
                f"agent_test_mcp_fault_id_duplicate:{fault_id}"
            )
        seen_fault_ids.add(fault_id)

        tool_name = _bounded_tool_name(
            raw_rule.get("tool_name"),
            field_name=f"agent_test_mcp_fault_{index}_tool_name",
        )
        fault_class = _clean_text(raw_rule.get("fault_class")).lower()
        if fault_class not in _SUPPORTED_FAULT_CLASSES:
            raise AgentTestMCPFaultPlanError(
                f"agent_test_mcp_fault_{index}_class_unsupported:{fault_class or 'missing'}"
            )
        max_activations = _bounded_positive_int(
            raw_rule.get("max_activations", 1),
            field_name=f"agent_test_mcp_fault_{index}_max_activations",
        )
        retry_after_seconds = _bounded_retry_after(raw_rule.get("retry_after_seconds"))
        if retry_after_seconds is not None and fault_class == _FAULT_CLASS_TIMEOUT:
            raise AgentTestMCPFaultPlanError(
                f"agent_test_mcp_fault_{index}_retry_after_not_applicable"
            )

        normalised_rule: dict[str, Any] = {
            "fault_id": fault_id,
            "tool_name": tool_name,
            "fault_class": fault_class,
            "max_activations": max_activations,
        }
        if retry_after_seconds is not None:
            normalised_rule["retry_after_seconds"] = retry_after_seconds
        normalised_rules.append(normalised_rule)
        rules.append(
            _FaultRule(
                fault_id=fault_id,
                tool_name=tool_name,
                fault_class=fault_class,
                max_activations=max_activations,
                retry_after_seconds=retry_after_seconds,
            )
        )

    raw_plan_id = plan.get("plan_id")
    plan_id = (
        _bounded_reference(
            raw_plan_id,
            field_name="agent_test_mcp_fault_plan_id",
            max_length=_MAX_PLAN_ID_LENGTH,
        )
        if raw_plan_id is not None
        else None
    )
    digest_payload = {
        "schema_version": AGENT_TEST_MCP_FAULT_PLAN_SCHEMA_VERSION,
        "plan_id": plan_id,
        "faults": normalised_rules,
    }
    plan_sha256 = _stable_digest(digest_payload)
    plan_id = plan_id or f"fault_plan_{plan_sha256[:12]}"
    return AgentTestMCPFaultScope(
        plan_id=plan_id,
        plan_sha256=plan_sha256,
        rules=rules,
    )


@contextmanager
def bind_agent_test_mcp_fault_plan(
    plan: Mapping[str, Any],
) -> Iterator[AgentTestMCPFaultScope]:
    """Bind one validated fault plan to the current AgentTest execution context.

    Any attempt to bind a plan outside an isolated AgentTest process fails
    closed.  The binding is context-local and is always removed on exit.
    """

    if not _agent_test_enabled():
        raise AgentTestMCPFaultPlanError(
            "agent_test_mcp_fault_plan_requires_agent_test_instance"
        )
    if not isinstance(plan, Mapping):
        raise AgentTestMCPFaultPlanError("agent_test_mcp_fault_plan_must_be_object")
    scope = _normalise_fault_plan(plan)
    token = _ACTIVE_FAULT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_FAULT_SCOPE.reset(token)


def _fault_response(
    *,
    rule: _FaultRule,
    event: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    if rule.fault_class == _FAULT_CLASS_TIMEOUT:
        error_code = "tool_timeout"
        message = (
            f"AgentTest fault plan injected a deterministic timeout for MCP tool "
            f"'{rule.tool_name}'."
        )
    elif rule.fault_class == _FAULT_CLASS_RATE_LIMIT:
        error_code = "rate_limit"
        message = (
            f"AgentTest fault plan injected a deterministic rate limit for MCP tool "
            f"'{rule.tool_name}'."
        )
    else:
        error_code = "tool_temporarily_unavailable"
        message = (
            f"AgentTest fault plan made MCP tool '{rule.tool_name}' temporarily "
            "unavailable."
        )

    payload: dict[str, Any] = {
        "success": False,
        "error": message,
        "error_code": error_code,
        "retryable": True,
        "agent_test_fault_event": dict(event),
    }
    if rule.retry_after_seconds is not None:
        payload["retry_after_seconds"] = rule.retry_after_seconds
    return error_code, message, payload


def maybe_inject_agent_test_mcp_fault(
    tool_name: str,
) -> AgentTestMCPFaultOutcome | None:
    """Return a typed synthetic MCP failure for the next matching activation."""

    scope = _ACTIVE_FAULT_SCOPE.get()
    if scope is None:
        return None
    if not _agent_test_enabled():
        raise AgentTestMCPFaultPlanError(
            "agent_test_mcp_fault_plan_runtime_gate_closed"
        )

    requested_tool_name = _clean_text(tool_name)
    for rule in scope.rules:
        if rule.tool_name != requested_tool_name:
            continue
        if rule.activation_count >= rule.max_activations:
            continue

        rule.activation_count += 1
        event: dict[str, Any] = {
            "schema_version": AGENT_TEST_MCP_FAULT_EVENT_SCHEMA_VERSION,
            "source": "agent_test_mcp_fault_plan",
            "agent_test_instance": True,
            "plan_id": scope.plan_id,
            "plan_sha256": scope.plan_sha256,
            "fault_id": rule.fault_id,
            "fault_class": rule.fault_class,
            "tool_name": rule.tool_name,
            "activation_index": rule.activation_count,
            "max_activations": rule.max_activations,
            "remaining_activations": rule.max_activations - rule.activation_count,
            "injected_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if rule.retry_after_seconds is not None:
            event["retry_after_seconds"] = rule.retry_after_seconds
        error_code, message, payload = _fault_response(rule=rule, event=event)
        event["error_code"] = error_code
        event["message"] = message
        payload["agent_test_fault_event"] = dict(event)
        scope._events.append(dict(event))
        return AgentTestMCPFaultOutcome(
            error_code=error_code,
            payload=payload,
            event=event,
        )
    return None


__all__ = [
    "AGENT_TEST_MCP_FAULT_EVENT_SCHEMA_VERSION",
    "AGENT_TEST_MCP_FAULT_PLAN_SCHEMA_VERSION",
    "AgentTestMCPFaultOutcome",
    "AgentTestMCPFaultPlanError",
    "AgentTestMCPFaultScope",
    "bind_agent_test_mcp_fault_plan",
    "maybe_inject_agent_test_mcp_fault",
]
