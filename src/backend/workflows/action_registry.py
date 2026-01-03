"""Declarative action catalogue for workflow execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional


@dataclass(frozen=True)
class WorkflowEnvironment:
    """Runtime dependencies available to workflow actions."""

    llm_client: Any
    gateway: Any | None = None
    model: str | None = None
    user_namespace: str | None = None
    auxiliary_system_prompt: str | None = None
    max_tool_invocations: int | None = None
    default_gmail_profile: str | None = None


@dataclass(frozen=True)
class WorkflowActionRequest:
    action_id: str
    inputs: Mapping[str, Any]
    environment: WorkflowEnvironment
    data: Dict[str, Any]
    trace: Any | None = None


@dataclass
class WorkflowActionResult:
    status: str = "success"
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    call_id: str | None = None
    duration_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    handler: Callable[[WorkflowActionRequest], WorkflowActionResult]
    description: str | None = None
    concept_id: str | None = None
    input_schema: Mapping[str, Any] | None = None
    side_effects: str | None = None


class ActionRegistry:
    """Registry of declarative workflow actions."""

    def __init__(self) -> None:
        self._actions: Dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        if not isinstance(spec, ActionSpec):
            raise TypeError("spec must be an ActionSpec")
        if spec.action_id in self._actions:
            raise ValueError(f"action already registered: {spec.action_id}")
        self._actions[spec.action_id] = spec

    def get(self, action_id: str) -> ActionSpec | None:
        return self._actions.get(action_id)

    def execute(
        self,
        action_id: str,
        *,
        inputs: Mapping[str, Any],
        context: Dict[str, Any],
        env: WorkflowEnvironment,
        trace: Any | None = None,
    ) -> WorkflowActionResult:
        spec = self.get(action_id)
        if spec is None:
            return WorkflowActionResult(
                status="failed", error=f"action_not_registered:{action_id}"
            )
        try:
            request = WorkflowActionRequest(
                action_id=action_id,
                inputs=inputs,
                environment=env,
                data=context,
                trace=trace,
            )
            return spec.handler(request)
        except Exception as exc:
            return WorkflowActionResult(status="failed", error=str(exc))
