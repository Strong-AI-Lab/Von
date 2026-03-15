"""Declarative action catalogue for workflow execution.

SCHEMA DESIGN NOTES
===================
Action IDs are plain strings (e.g. ``"narration.render"``, ``"search_concepts"``).
When a Vontology-defined workflow references an MCP tool name as ``invokes_action``,
the engine looks up that name in this registry.  If no explicit ``ActionSpec`` is
registered, the **fallback handler** (if set) routes the call through the MCP
gateway.  See ``set_fallback_handler`` and JVNAUTOSCI-922 Phase 3.1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .execution_contracts import (
    WORKFLOW_CONTROL_SIGNAL_ERROR,
    resolve_control_signal_from_outputs,
    stamp_control_signal_context,
)

logger = logging.getLogger(__name__)


WORKFLOW_ACTION_OUTCOME_SUCCESS = "success"
WORKFLOW_ACTION_OUTCOME_FAILURE = "failure"
WORKFLOW_ACTION_OUTCOME_UNKNOWN = "unknown"


def normalise_action_outcome(status: str | None) -> str:
    """Map raw action statuses onto canonical workflow outcomes.

    The workflow engine reasons over a tri-state envelope:
    ``success | failure | unknown``. Unknown remains first-class so callers can
    route explicitly (for example via ``on_unknown`` transitions) instead of
    silently treating ambiguous tool responses as success.
    """
    raw_status = str(status or "").strip().lower()
    if raw_status == "success":
        return WORKFLOW_ACTION_OUTCOME_SUCCESS
    if raw_status in {"failed", "failure", "error"}:
        return WORKFLOW_ACTION_OUTCOME_FAILURE
    return WORKFLOW_ACTION_OUTCOME_UNKNOWN


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
    action_target_id: str | None = None
    contract_concept_id: str | None = None
    execution_mode: str | None = None
    prompt_contract: Mapping[str, Any] | None = None
    llm_policy: Mapping[str, Any] | None = None
    validation_policy: Mapping[str, Any] | None = None


@dataclass
class WorkflowActionResult:
    status: str = "success"
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    call_id: str | None = None
    duration_ms: float | None = None

    @property
    def outcome(self) -> str:
        return normalise_action_outcome(self.status)

    @property
    def ok(self) -> bool:
        return self.outcome == WORKFLOW_ACTION_OUTCOME_SUCCESS


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    handler: Callable[[WorkflowActionRequest], WorkflowActionResult]
    description: str | None = None
    concept_id: str | None = None
    input_schema: Mapping[str, Any] | None = None
    output_schema: Mapping[str, Any] | None = None
    side_effects: str | None = None
    postconditions: Sequence[str] = ()


def _apply_action_outcome_context(
    *,
    context: Dict[str, Any],
    action_target_id: str,
    resolved_action_id: str | None,
    contract_concept_id: str | None,
    result: WorkflowActionResult,
) -> None:
    """Stamp canonical action outcome flags into workflow context.

    JVNAUTOSCI-1087: `on_failure` transitions depend on `last_action_failed`.
    Keep these keys centralised here so every execution path (conversation-turn
    and durable) observes identical post-action state.
    """
    outcome = normalise_action_outcome(result.status)
    context["last_action_id"] = action_target_id
    context["last_action_target_id"] = action_target_id
    context["last_action_registry_action_id"] = (
        resolved_action_id or action_target_id
    )
    context["last_action_contract_concept_id"] = contract_concept_id
    context["last_action_status"] = result.status
    context["last_action_outcome"] = outcome
    context["last_action_succeeded"] = outcome == WORKFLOW_ACTION_OUTCOME_SUCCESS
    context["last_action_failed"] = outcome == WORKFLOW_ACTION_OUTCOME_FAILURE
    context["last_action_unknown"] = outcome == WORKFLOW_ACTION_OUTCOME_UNKNOWN
    context["last_step_ok"] = outcome == WORKFLOW_ACTION_OUTCOME_SUCCESS
    context["last_step_outcome"] = outcome
    context["last_action_error"] = result.error
    context["last_action_call_id"] = result.call_id
    context["last_action_duration_ms"] = result.duration_ms

    control_signal, control_scope, return_payload = resolve_control_signal_from_outputs(
        action_outcome=outcome,
        outputs=result.outputs,
    )
    stamp_control_signal_context(
        context=context,
        signal=control_signal,
        scope=control_scope,
        return_payload=return_payload,
    )
    if control_signal == WORKFLOW_CONTROL_SIGNAL_ERROR:
        context["last_action_failed"] = True
        context["last_step_ok"] = False


class ActionRegistry:
    """Registry of declarative workflow actions.

    Supports both strict registration (raises on duplicate) and idempotent
    registration via ``register_if_absent``.  Use ``merge`` to combine
    registries from different subsystems (e.g. orchestrator + durable).
    """

    def __init__(self) -> None:
        self._actions: Dict[str, ActionSpec] = {}
        self._actions_by_concept_id: Dict[str, ActionSpec] = {}
        self._fallback_handler: (
            Callable[[WorkflowActionRequest], WorkflowActionResult] | None
        ) = None

    # --- registration -------------------------------------------------------

    def register(self, spec: ActionSpec) -> None:
        if not isinstance(spec, ActionSpec):
            raise TypeError("spec must be an ActionSpec")
        if spec.action_id in self._actions:
            raise ValueError(f"action already registered: {spec.action_id}")
        concept_id = str(spec.concept_id or "").strip()
        if concept_id:
            existing = self._actions_by_concept_id.get(concept_id)
            if existing is not None and existing.action_id != spec.action_id:
                raise ValueError(
                    "action concept already registered: "
                    f"{concept_id} -> {existing.action_id}"
                )
        self._actions[spec.action_id] = spec
        if concept_id:
            self._actions_by_concept_id[concept_id] = spec

    def register_if_absent(self, spec: ActionSpec) -> bool:
        """Register *spec* only if its ``action_id`` is not already present.

        Returns True if the action was registered, False if skipped.
        """
        if not isinstance(spec, ActionSpec):
            raise TypeError("spec must be an ActionSpec")
        if spec.action_id in self._actions:
            return False
        concept_id = str(spec.concept_id or "").strip()
        if concept_id:
            existing = self._actions_by_concept_id.get(concept_id)
            if existing is not None and existing.action_id != spec.action_id:
                raise ValueError(
                    "action concept already registered: "
                    f"{concept_id} -> {existing.action_id}"
                )
        self._actions[spec.action_id] = spec
        if concept_id:
            self._actions_by_concept_id[concept_id] = spec
        return True

    def merge(self, other: "ActionRegistry", *, overwrite: bool = False) -> None:
        """Merge all actions from *other* into this registry.

        Args:
            other: Source registry whose actions will be copied in.
            overwrite: If True, existing actions with the same ID are
                replaced.  If False (default), existing actions are kept
                and duplicates from *other* are silently skipped.
        """
        for action_id, spec in other._actions.items():
            if overwrite or action_id not in self._actions:
                self._actions[action_id] = spec
                concept_id = str(spec.concept_id or "").strip()
                if concept_id:
                    self._actions_by_concept_id[concept_id] = spec

    def set_fallback_handler(
        self,
        handler: Callable[[WorkflowActionRequest], WorkflowActionResult],
    ) -> None:
        """Set a catch-all handler for action IDs without explicit registration.

        When the engine calls ``execute()`` with an unregistered action ID,
        this handler is invoked instead of returning an error.  This is the
        hook used by the orchestrator to route Vontology-defined MCP tool
        actions through the gateway.  See JVNAUTOSCI-922 Phase 3.1.
        """
        self._fallback_handler = handler

    def has_fallback_handler(self) -> bool:
        """Return True when an unregistered-action fallback is configured."""
        return self._fallback_handler is not None

    # --- lookup -------------------------------------------------------------

    def get(self, action_id: str) -> ActionSpec | None:
        return self._actions.get(action_id)

    def get_by_concept_id(self, concept_id: str) -> ActionSpec | None:
        return self._actions_by_concept_id.get(concept_id)

    def resolve_action_spec(self, action_target_id: str) -> ActionSpec | None:
        target = str(action_target_id or "").strip()
        if not target:
            return None
        spec = self.get(target)
        if spec is not None:
            return spec
        if target.startswith("#V#"):
            return self.get_by_concept_id(target)
        return None

    def has(self, action_id: str) -> bool:
        """Return True if *action_id* is registered."""
        return action_id in self._actions

    def has_action_target(self, action_target_id: str) -> bool:
        return self.resolve_action_spec(action_target_id) is not None

    def all_action_ids(self) -> list[str]:
        """Return all registered action IDs."""
        return list(self._actions.keys())

    def execute(
        self,
        action_id: str,
        *,
        inputs: Mapping[str, Any],
        context: Dict[str, Any],
        env: WorkflowEnvironment,
        trace: Any | None = None,
    ) -> WorkflowActionResult:
        action_target_id = str(action_id or "").strip()
        spec = self.resolve_action_spec(action_target_id)
        handler: Callable[[WorkflowActionRequest], WorkflowActionResult] | None = (
            spec.handler if spec is not None else self._fallback_handler
        )
        if handler is None:
            result = WorkflowActionResult(
                status="failed", error=f"action_not_registered:{action_target_id}"
            )
            _apply_action_outcome_context(
                context=context,
                action_target_id=action_target_id,
                resolved_action_id=None,
                contract_concept_id=None,
                result=result,
            )
            return result
        resolved_action_id = (
            str(spec.action_id or "").strip() if spec is not None else action_target_id
        )
        contract_concept_id = (
            str(spec.concept_id or "").strip() if spec is not None else None
        ) or None
        try:
            request = WorkflowActionRequest(
                action_id=resolved_action_id or action_target_id,
                inputs=inputs,
                environment=env,
                data=context,
                trace=trace,
                action_target_id=action_target_id or None,
                contract_concept_id=contract_concept_id,
            )
            raw_result = handler(request)
            if isinstance(raw_result, WorkflowActionResult):
                result = raw_result
            else:
                result = WorkflowActionResult(
                    status="failed",
                    error=(
                        "invalid_action_result_type:"
                        f"{action_target_id}:{type(raw_result).__name__}"
                    ),
                )
        except Exception as exc:
            result = WorkflowActionResult(status="failed", error=str(exc))

        _apply_action_outcome_context(
            context=context,
            action_target_id=action_target_id,
            resolved_action_id=resolved_action_id or action_target_id,
            contract_concept_id=contract_concept_id,
            result=result,
        )
        return result
