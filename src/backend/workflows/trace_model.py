from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional


_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"encrypted[_-]?content", re.IGNORECASE),
)
_SAFE_TOKEN_COUNT_KEYS: frozenset[str] = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "token_count",
        "tokens_streamed",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sanitise_for_trace_storage(
    value: Any,
    *,
    max_string_chars: int = 800,
    max_list_items: int = 50,
    max_depth: int = 8,
    _depth: int = 0,
) -> Any:
    """Best-effort sanitisation/redaction for workflow traces.

    Traces can contain user prompts, tool payloads, and tool results.
    Keep them bounded and redact obvious secrets.

    This is intentionally conservative: it aims to preserve debuggability while
    reducing accidental leakage risk.
    """

    if _depth >= max_depth:
        return "[truncated: max depth reached]"

    if value is None:
        return None

    if isinstance(value, str):
        text = value
        # Redact obvious secrets by keyword presence.
        if any(p.search(text) for p in _REDACT_PATTERNS):
            # Keep a small preview but redact the bulk.
            preview = text[: min(64, len(text))]
            return f"[redacted] {preview}..."
        if len(text) > max_string_chars:
            return (
                text[:max_string_chars]
                + f"\n... [truncated {len(text) - max_string_chars} chars]"
            )
        return text

    if isinstance(value, list):
        if len(value) > max_list_items:
            head = value[: max_list_items - 1]
            tail = value[-1:]
            return [
                *[
                    sanitise_for_trace_storage(
                        v,
                        max_string_chars=max_string_chars,
                        max_list_items=max_list_items,
                        max_depth=max_depth,
                        _depth=_depth + 1,
                    )
                    for v in head
                ],
                {
                    "_truncated": True,
                    "_omitted_items": len(value) - len(head) - len(tail),
                },
                *[
                    sanitise_for_trace_storage(
                        v,
                        max_string_chars=max_string_chars,
                        max_list_items=max_list_items,
                        max_depth=max_depth,
                        _depth=_depth + 1,
                    )
                    for v in tail
                ],
            ]
        return [
            sanitise_for_trace_storage(
                v,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            for v in value
        ]

    if isinstance(value, dict):
        sanitised: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            # Redact by key name as well as by value content.
            if (
                key.lower() not in _SAFE_TOKEN_COUNT_KEYS
                and any(p.search(key) for p in _REDACT_PATTERNS)
            ):
                sanitised[key] = "[redacted]"
                continue
            sanitised[key] = sanitise_for_trace_storage(
                v,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
        return sanitised

    # Ensure datetime is serialisable.
    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value

    # Fallback: keep it stringy.
    return str(value)


@dataclass
class WorkflowStepTrace:
    step_id: str
    start_time: datetime = field(default_factory=_utcnow)
    end_time: Optional[datetime] = None
    status: str = "running"  # running|success|failed|skipped
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def finish_success(self, outputs: Mapping[str, Any] | None = None) -> None:
        self.end_time = _utcnow()
        self.status = "success"
        if outputs:
            self.outputs = dict(outputs)

    def finish_failed(self, error: str) -> None:
        self.end_time = _utcnow()
        self.status = "failed"
        self.error = error


@dataclass
class WorkflowExecutionTrace:
    workflow_id: str
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    start_time: datetime = field(default_factory=_utcnow)
    end_time: Optional[datetime] = None
    status: str = "running"  # running|completed|failed|timeout|cancelled
    instance_id: Optional[str] = None
    user_namespace: Optional[str] = None
    org_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    state_transitions: List[Dict[str, Any]] = field(default_factory=list)
    prompts: List[Dict[str, Any]] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[WorkflowStepTrace] = field(default_factory=list)

    def start_step(
        self, step_id: str, *, inputs: Mapping[str, Any] | None = None
    ) -> WorkflowStepTrace:
        step = WorkflowStepTrace(step_id=step_id, inputs=dict(inputs or {}))
        self.steps.append(step)
        return step

    def record_state_transition(
        self,
        from_state: str,
        to_state: str,
        *,
        verdict: Mapping[str, Any] | None = None,
        call_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        transition: Dict[str, Any] = {
            "from": from_state,
            "to": to_state,
            "timestamp": _utcnow(),
        }
        if verdict is not None:
            transition["verdict"] = dict(verdict)
        if call_id:
            transition["call_id"] = call_id
        if reason:
            transition["reason"] = reason
        self.state_transitions.append(transition)

    def record_prompt(
        self,
        *,
        prompt_id: str | None,
        resolved_prompt: str | None,
        variables: Mapping[str, Any] | None = None,
    ) -> None:
        entry: Dict[str, Any] = {}
        if prompt_id:
            entry["prompt_id"] = prompt_id
        if resolved_prompt:
            entry["preview"] = resolved_prompt[:400]
            entry["truncated"] = len(resolved_prompt) > 400
        if variables:
            entry["variables"] = dict(variables)
        self.prompts.append(entry)

    def record_action(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any] | None = None,
        outputs: Mapping[str, Any] | None = None,
        status: str = "success",
        error: str | None = None,
        call_id: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        action_entry: Dict[str, Any] = {
            "action_id": action_id,
            "status": status,
            "timestamp": _utcnow(),
        }
        if call_id:
            action_entry["call_id"] = call_id
        if duration_ms is not None:
            action_entry["duration_ms"] = duration_ms
        if inputs:
            action_entry["inputs"] = dict(inputs)
        if outputs:
            action_entry["outputs"] = dict(outputs)
        if error:
            action_entry["error"] = error
        self.actions.append(action_entry)

    def finish_completed(self) -> None:
        self.end_time = _utcnow()
        self.status = "completed"

    def finish_failed(self, error: str) -> None:
        self.end_time = _utcnow()
        self.status = "failed"
        # Store the error as a final synthetic step to avoid expanding top-level schema.
        failed_step = WorkflowStepTrace(step_id="_workflow_failed", inputs={})
        failed_step.finish_failed(error)
        self.steps.append(failed_step)

    def to_storage_document(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "workflow_id": self.workflow_id,
            "execution_id": self.execution_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status,
            "instance_id": self.instance_id,
            "user_namespace": self.user_namespace,
            "org_id": self.org_id,
            "metadata": self.metadata,
            "state_transitions": self.state_transitions,
            "prompts": self.prompts,
            "actions": self.actions,
            "steps": [
                {
                    "step_id": s.step_id,
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "status": s.status,
                    "inputs": sanitise_for_trace_storage(s.inputs),
                    "outputs": sanitise_for_trace_storage(s.outputs),
                    "error": sanitise_for_trace_storage(s.error) if s.error else None,
                }
                for s in self.steps
            ],
        }

        # Sanitise top-level fields that may contain strings.
        return sanitise_for_trace_storage(doc)
