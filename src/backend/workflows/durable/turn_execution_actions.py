"""Durable actions for explicit turn verification workflows.

The universal conversation-turn controller and its reconstructed completion
gate have been retired.  The remaining critic is used only by explicit
workflows that independently opt into postcondition assessment.
"""

from __future__ import annotations

from typing import Any

from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from .turn_execution_runtime_support import run_turn_execution_critic

TURN_EXECUTION_CRITIC_ACTION_ID = "turn_execution.critic"


def _build_turn_execution_critic_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        return run_turn_execution_critic(
            request,
            annotation_component="durable_turn_execution_actions",
            annotation_function="_build_turn_execution_critic_handler",
        )

    return _handle


def register_turn_execution_actions(registry: ActionRegistry) -> None:
    """Register the explicit-workflow postcondition critic."""

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_CRITIC_ACTION_ID,
            handler=_build_turn_execution_critic_handler(),
            description=(
                "Evaluate required effects and postcondition checks for an "
                "explicit workflow."
            ),
        )
    )
