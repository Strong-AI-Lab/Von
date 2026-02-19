"""Canonical conversation-turn stage model and runtime mapping helpers.

This module defines one authoritative stage catalogue for turn orchestration,
covering both formal workflow-engine states and non-formal route/orchestrator
phases. Runtime telemetry can then map stage tokens into a single,
query-oriented representation rooted at
``#V#conversation_turn_execution_workflow``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
)

CONVERSATION_TURN_STAGE_MODEL_SCHEMA_VERSION = "conversation_turn_stage_model.v1"
CONVERSATION_TURN_STAGE_PATH_SCHEMA_VERSION = "conversation_turn_stage_path.v1"


@dataclass(frozen=True)
class _StageSpec:
    stage_id: str
    stage_label: str
    order: int
    stage_kind: str  # "formal" | "non_formal"
    boundary_type: str
    stage_concept_id: str
    workflow_id: str | None = None
    workflow_state_id: str | None = None
    runtime_aliases: tuple[str, ...] = ()


_STAGE_SPECS: tuple[_StageSpec, ...] = (
    _StageSpec(
        stage_id="context_build",
        stage_label="Build context",
        order=10,
        stage_kind="non_formal",
        boundary_type="preflight",
        stage_concept_id="#V#conversation_turn_stage_context_build",
        runtime_aliases=("context_build",),
    ),
    _StageSpec(
        stage_id="workflow_discovery",
        stage_label="Workflow discovery",
        order=20,
        stage_kind="non_formal",
        boundary_type="routing",
        stage_concept_id="#V#conversation_turn_stage_workflow_discovery",
        runtime_aliases=("workflow_discovery",),
    ),
    _StageSpec(
        stage_id="workflow_dispatch",
        stage_label="Workflow dispatch",
        order=30,
        stage_kind="non_formal",
        boundary_type="routing",
        stage_concept_id="#V#conversation_turn_stage_workflow_dispatch",
        runtime_aliases=("workflow_dispatch",),
    ),
    _StageSpec(
        stage_id="tool_calling",
        stage_label="Tool-calling route",
        order=40,
        stage_kind="non_formal",
        boundary_type="route",
        stage_concept_id="#V#conversation_turn_stage_tool_calling",
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        runtime_aliases=("tool_calling",),
    ),
    _StageSpec(
        stage_id="write_policy",
        stage_label="Write-tool policy",
        order=45,
        stage_kind="formal",
        boundary_type="policy",
        stage_concept_id="#V#conversation_turn_stage_write_policy",
        workflow_id=WRITE_TOOL_POLICY_WORKFLOW_ID,
        workflow_state_id="decide",
        runtime_aliases=("write_policy",),
    ),
    _StageSpec(
        stage_id="tool_plan",
        stage_label="Plan tool calls",
        order=50,
        stage_kind="formal",
        boundary_type="execution",
        stage_concept_id="#V#conversation_turn_stage_tool_plan",
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        workflow_state_id="plan",
        runtime_aliases=("tool_plan", "plan"),
    ),
    _StageSpec(
        stage_id="tool_validate",
        stage_label="Validate tool calls",
        order=60,
        stage_kind="formal",
        boundary_type="execution",
        stage_concept_id="#V#conversation_turn_stage_tool_validate",
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        workflow_state_id="validate",
        runtime_aliases=("validate",),
    ),
    _StageSpec(
        stage_id="tool_execute",
        stage_label="Execute tool calls",
        order=70,
        stage_kind="formal",
        boundary_type="execution",
        stage_concept_id="#V#conversation_turn_stage_tool_execute",
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        workflow_state_id="execute",
        runtime_aliases=("tool_execute", "execute"),
    ),
    _StageSpec(
        stage_id="screen_backfill",
        stage_label="Summarise/backfill response",
        order=80,
        stage_kind="formal",
        boundary_type="execution",
        stage_concept_id="#V#conversation_turn_stage_screen_backfill",
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        workflow_state_id="backfill",
        runtime_aliases=("screen_backfill", "backfill"),
    ),
    _StageSpec(
        stage_id="postcondition_critic",
        stage_label="Postcondition critic",
        order=90,
        stage_kind="formal",
        boundary_type="postcondition",
        stage_concept_id="#V#conversation_turn_stage_postcondition_critic",
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        workflow_state_id="postcondition_critic",
        runtime_aliases=("postcondition_critic",),
    ),
    _StageSpec(
        stage_id="postcondition_critic",
        stage_label="Postcondition critic",
        order=90,
        stage_kind="formal",
        boundary_type="postcondition",
        stage_concept_id="#V#conversation_turn_stage_postcondition_critic",
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="critic",
        runtime_aliases=("critic",),
    ),
    _StageSpec(
        stage_id="completion_gate",
        stage_label="Completion gate",
        order=100,
        stage_kind="formal",
        boundary_type="completion_gate",
        stage_concept_id="#V#conversation_turn_stage_completion_gate",
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        workflow_state_id="completion_gate",
        runtime_aliases=("completion_gate",),
    ),
    _StageSpec(
        stage_id="completion_gate",
        stage_label="Completion gate",
        order=100,
        stage_kind="formal",
        boundary_type="completion_gate",
        stage_concept_id="#V#conversation_turn_stage_completion_gate",
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="completion_gate",
        runtime_aliases=("completion_gate",),
    ),
    _StageSpec(
        stage_id="completion_gate",
        stage_label="Completion gate",
        order=100,
        stage_kind="formal",
        boundary_type="completion_gate",
        stage_concept_id="#V#conversation_turn_stage_completion_gate",
        workflow_id=TURN_COMPLETION_GATE_WORKFLOW_ID,
        workflow_state_id="decide",
        runtime_aliases=("decide",),
    ),
    _StageSpec(
        stage_id="narration",
        stage_label="Narration rendering",
        order=120,
        stage_kind="non_formal",
        boundary_type="render",
        stage_concept_id="#V#conversation_turn_stage_narration",
        workflow_id=CHAT_NARRATION_WORKFLOW_ID,
        runtime_aliases=("narration",),
    ),
    _StageSpec(
        stage_id="plain_response",
        stage_label="Plain-response routing",
        order=130,
        stage_kind="non_formal",
        boundary_type="route",
        stage_concept_id="#V#conversation_turn_stage_plain_response",
        workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
        runtime_aliases=("plain_response",),
    ),
    _StageSpec(
        stage_id="completed",
        stage_label="Completed",
        order=190,
        stage_kind="formal",
        boundary_type="terminal",
        stage_concept_id="#V#conversation_turn_stage_completed",
        runtime_aliases=("completed",),
    ),
    _StageSpec(
        stage_id="failed",
        stage_label="Failed",
        order=191,
        stage_kind="formal",
        boundary_type="terminal",
        stage_concept_id="#V#conversation_turn_stage_failed",
        runtime_aliases=("failed", "error", "workflow_exception"),
    ),
    _StageSpec(
        stage_id="cancelled",
        stage_label="Cancelled",
        order=192,
        stage_kind="non_formal",
        boundary_type="terminal",
        stage_concept_id="#V#conversation_turn_stage_cancelled",
        runtime_aliases=("cancelled",),
    ),
    _StageSpec(
        stage_id="terminated",
        stage_label="Terminated",
        order=193,
        stage_kind="non_formal",
        boundary_type="terminal",
        stage_concept_id="#V#conversation_turn_stage_terminated",
        runtime_aliases=("terminated", "workflow_lookup"),
    ),
)


def _normalise_runtime_stage(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    cleaned = cleaned.replace("-", "_").replace(" ", "_")
    return cleaned


def _normalise_workflow_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def _build_alias_index() -> dict[str, tuple[_StageSpec, ...]]:
    alias_index: dict[str, list[_StageSpec]] = {}
    for spec in _STAGE_SPECS:
        candidates = {spec.stage_id, *spec.runtime_aliases}
        if spec.workflow_state_id:
            candidates.add(spec.workflow_state_id)
        for token in candidates:
            normalised = _normalise_runtime_stage(token)
            if not normalised:
                continue
            alias_index.setdefault(normalised, []).append(spec)
    return {token: tuple(specs) for token, specs in alias_index.items()}


_ALIAS_INDEX = _build_alias_index()


def _serialise_stage_spec(spec: _StageSpec) -> dict[str, Any]:
    return {
        "stage_id": spec.stage_id,
        "stage_label": spec.stage_label,
        "order": spec.order,
        "stage_kind": spec.stage_kind,
        "boundary_type": spec.boundary_type,
        "stage_concept_id": spec.stage_concept_id,
        "workflow_id": spec.workflow_id,
        "workflow_state_id": spec.workflow_state_id,
        "runtime_aliases": list(spec.runtime_aliases),
    }


def build_conversation_turn_stage_model_snapshot() -> dict[str, Any]:
    """Return the canonical conversation-turn stage catalogue."""
    return {
        "schema_version": CONVERSATION_TURN_STAGE_MODEL_SCHEMA_VERSION,
        "workflow_representation_id": CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        "stages": [_serialise_stage_spec(spec) for spec in _STAGE_SPECS],
    }


def resolve_conversation_turn_stage(
    *,
    runtime_stage: Any,
    workflow_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one runtime stage token into the canonical stage catalogue."""
    normalised_stage = _normalise_runtime_stage(runtime_stage)
    if not normalised_stage:
        return None

    candidates = _ALIAS_INDEX.get(normalised_stage, ())
    if not candidates:
        return None

    normalised_workflow = _normalise_workflow_id(workflow_id)
    if normalised_workflow:
        for spec in candidates:
            if _normalise_workflow_id(spec.workflow_id) == normalised_workflow:
                return _serialise_stage_spec(spec)

    for spec in candidates:
        if spec.workflow_id is None:
            return _serialise_stage_spec(spec)

    return _serialise_stage_spec(candidates[0])


def build_conversation_turn_stage_path(
    *,
    runtime_stages: Sequence[Any],
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Map a runtime stage sequence into canonical stage entries.

    Unknown stage tokens are preserved using an explicit fallback entry so
    introspection can still show full execution paths.
    """

    path: list[dict[str, Any]] = []
    unmapped_runtime_stages: list[str] = []
    last_dedupe_key: str | None = None

    for raw_stage in runtime_stages:
        normalised_stage = _normalise_runtime_stage(raw_stage)
        if not normalised_stage:
            continue

        resolved = resolve_conversation_turn_stage(
            runtime_stage=normalised_stage,
            workflow_id=workflow_id,
        )
        if isinstance(resolved, dict):
            dedupe_key = f"mapped:{resolved.get('stage_id')}:{resolved.get('workflow_id')}"
            if dedupe_key == last_dedupe_key:
                continue
            path.append(
                {
                    "sequence_no": len(path),
                    "runtime_stage": str(raw_stage).strip(),
                    "runtime_stage_normalised": normalised_stage,
                    "mapping_status": "mapped",
                    **resolved,
                }
            )
            last_dedupe_key = dedupe_key
            continue

        dedupe_key = f"fallback:{normalised_stage}"
        if dedupe_key == last_dedupe_key:
            continue
        path.append(
            {
                "sequence_no": len(path),
                "runtime_stage": str(raw_stage).strip(),
                "runtime_stage_normalised": normalised_stage,
                "mapping_status": "fallback_unmapped_runtime_stage",
                "stage_id": None,
                "stage_label": None,
                "order": None,
                "stage_kind": "unmapped",
                "boundary_type": "unmapped",
                "stage_concept_id": None,
                "workflow_id": workflow_id,
                "workflow_state_id": None,
                "runtime_aliases": [],
            }
        )
        last_dedupe_key = dedupe_key
        if normalised_stage not in unmapped_runtime_stages:
            unmapped_runtime_stages.append(normalised_stage)

    return {
        "schema_version": CONVERSATION_TURN_STAGE_PATH_SCHEMA_VERSION,
        "stage_model_schema_version": CONVERSATION_TURN_STAGE_MODEL_SCHEMA_VERSION,
        "workflow_representation_id": CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        "workflow_id": workflow_id,
        "path": path,
        "has_unmapped_runtime_stages": bool(unmapped_runtime_stages),
        "unmapped_runtime_stages": unmapped_runtime_stages,
    }

