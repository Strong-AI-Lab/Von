"""Conversation-turn stage model derived from workflow metadata.

Formal stage entries are derived from the authoritative workflow registry so
state-to-stage mapping follows Vontology-authored workflow definitions instead
of a duplicated static state catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence

from .definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
)
from .durable.registry_factory import build_workflow_registry_read_only
from .engine import WorkflowDefinition

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


@dataclass(frozen=True)
class _DerivedFormalStageProfile:
    stage_id: str
    stage_label: str
    order: int
    boundary_type: str
    runtime_aliases: tuple[str, ...] = ()


_NON_FORMAL_STAGE_SPECS: tuple[_StageSpec, ...] = (
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
        stage_id="buttonify",
        stage_label="Buttonify output transformation",
        order=125,
        stage_kind="non_formal",
        boundary_type="render",
        stage_concept_id="#V#conversation_turn_stage_buttonify",
        workflow_id=CHAT_BUTTONIFY_WORKFLOW_ID,
        runtime_aliases=("buttonify",),
    ),
    _StageSpec(
        stage_id="response_finalising",
        stage_label="Finalising response",
        order=128,
        stage_kind="non_formal",
        boundary_type="postprocess",
        stage_concept_id="#V#conversation_turn_stage_response_finalising",
        runtime_aliases=("response_finalising",),
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

_GLOBAL_TERMINAL_FORMAL_SPECS: tuple[_StageSpec, ...] = (
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
)

_FORMAL_STAGE_PROFILE_BY_ACTION_ID: dict[str, _DerivedFormalStageProfile] = {
    "write_policy.decide": _DerivedFormalStageProfile(
        stage_id="write_policy",
        stage_label="Write-tool policy",
        order=45,
        boundary_type="policy",
        runtime_aliases=("write_policy",),
    ),
    "tool_calling.plan": _DerivedFormalStageProfile(
        stage_id="tool_plan",
        stage_label="Plan tool calls",
        order=50,
        boundary_type="execution",
        runtime_aliases=("tool_plan", "plan"),
    ),
    "tool_calling.validate": _DerivedFormalStageProfile(
        stage_id="tool_validate",
        stage_label="Validate tool calls",
        order=60,
        boundary_type="execution",
        runtime_aliases=("validate",),
    ),
    "tool_calling.execute": _DerivedFormalStageProfile(
        stage_id="tool_execute",
        stage_label="Execute tool calls",
        order=70,
        boundary_type="execution",
        runtime_aliases=("tool_execute", "execute"),
    ),
    "tool_calling.backfill": _DerivedFormalStageProfile(
        stage_id="screen_backfill",
        stage_label="Summarise/backfill response",
        order=80,
        boundary_type="execution",
        runtime_aliases=("screen_backfill", "backfill"),
    ),
    "turn_execution.critic": _DerivedFormalStageProfile(
        stage_id="postcondition_critic",
        stage_label="Postcondition critic",
        order=90,
        boundary_type="postcondition",
        runtime_aliases=("postcondition_critic", "critic"),
    ),
    "turn_execution.completion_gate": _DerivedFormalStageProfile(
        stage_id="completion_gate",
        stage_label="Completion gate",
        order=100,
        boundary_type="completion_gate",
        runtime_aliases=("completion_gate",),
    ),
}

_CANONICAL_CONVERSATION_STAGE_WORKFLOW_IDS: tuple[str, ...] = (
    WRITE_TOOL_POLICY_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
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


def _titleise_stage_token(token: str) -> str:
    words = [part for part in token.replace("-", "_").split("_") if part]
    if not words:
        return "Stage"
    return " ".join(word.capitalize() for word in words)


def _iter_action_ids(definition: WorkflowDefinition, state_id: str) -> tuple[str, ...]:
    state_spec = definition.states.get(state_id)
    if state_spec is None:
        return ()
    action_ids: list[str] = []
    for action in state_spec.actions:
        action_id = str(action.action_id or "").strip()
        if action_id:
            action_ids.append(action_id)
    return tuple(action_ids)


def _derive_formal_stage_spec(
    *,
    workflow_id: str,
    state_id: str,
    action_ids: Iterable[str],
) -> _StageSpec | None:
    normalised_state = _normalise_runtime_stage(state_id)
    if normalised_state in {"completed", "failed"}:
        return None

    profile: _DerivedFormalStageProfile | None = None
    for action_id in action_ids:
        profile = _FORMAL_STAGE_PROFILE_BY_ACTION_ID.get(action_id)
        if profile is not None:
            break

    if profile is None:
        fallback_stage_id = normalised_state or "unknown_stage"
        profile = _DerivedFormalStageProfile(
            stage_id=fallback_stage_id,
            stage_label=_titleise_stage_token(fallback_stage_id),
            order=160,
            boundary_type="execution",
            runtime_aliases=(fallback_stage_id,),
        )

    alias_tokens = list(profile.runtime_aliases)
    if normalised_state and normalised_state not in alias_tokens:
        alias_tokens.append(normalised_state)

    return _StageSpec(
        stage_id=profile.stage_id,
        stage_label=profile.stage_label,
        order=profile.order,
        stage_kind="formal",
        boundary_type=profile.boundary_type,
        stage_concept_id=f"#V#conversation_turn_stage_{profile.stage_id}",
        workflow_id=workflow_id,
        workflow_state_id=state_id,
        runtime_aliases=tuple(alias_tokens),
    )


def _iter_conversation_stage_workflow_ids(
    *,
    registry_workflow_ids: Sequence[str],
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for workflow_id in _CANONICAL_CONVERSATION_STAGE_WORKFLOW_IDS:
        if workflow_id in registry_workflow_ids and workflow_id not in seen:
            ordered.append(workflow_id)
            seen.add(workflow_id)
    return tuple(ordered)


def _derive_formal_stage_specs() -> tuple[_StageSpec, ...]:
    registry = build_workflow_registry_read_only()
    workflow_ids = tuple(registry.all_workflow_ids())
    candidates = _iter_conversation_stage_workflow_ids(registry_workflow_ids=workflow_ids)
    stage_specs: list[_StageSpec] = []
    seen_keys: set[tuple[str, str, str | None]] = set()

    for workflow_id in candidates:
        definition = registry.get(workflow_id)
        if definition is None:
            continue
        for state_id in definition.states.keys():
            action_ids = _iter_action_ids(definition, state_id)
            stage_spec = _derive_formal_stage_spec(
                workflow_id=workflow_id,
                state_id=state_id,
                action_ids=action_ids,
            )
            if stage_spec is None:
                continue
            dedupe_key = (
                stage_spec.stage_id,
                stage_spec.workflow_id or "",
                stage_spec.workflow_state_id,
            )
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            stage_specs.append(stage_spec)

    stage_specs.sort(
        key=lambda spec: (
            spec.order,
            str(spec.workflow_id or ""),
            str(spec.workflow_state_id or spec.stage_id),
        )
    )
    return tuple(stage_specs)


@lru_cache(maxsize=1)
def _load_stage_specs() -> tuple[_StageSpec, ...]:
    formal_stage_specs = _derive_formal_stage_specs()
    combined_specs = (
        *_NON_FORMAL_STAGE_SPECS,
        *formal_stage_specs,
        *_GLOBAL_TERMINAL_FORMAL_SPECS,
    )
    return tuple(
        sorted(
            combined_specs,
            key=lambda spec: (
                spec.order,
                0 if spec.workflow_id is None else 1,
                str(spec.workflow_id or ""),
                str(spec.workflow_state_id or spec.stage_id),
            ),
        )
    )


def _build_alias_index(
    stage_specs: Sequence[_StageSpec],
) -> dict[str, tuple[_StageSpec, ...]]:
    alias_index: dict[str, list[_StageSpec]] = {}
    for spec in stage_specs:
        candidates = {spec.stage_id, *spec.runtime_aliases}
        if spec.workflow_state_id:
            candidates.add(spec.workflow_state_id)
        for token in candidates:
            normalised = _normalise_runtime_stage(token)
            if not normalised:
                continue
            alias_index.setdefault(normalised, []).append(spec)
    return {token: tuple(specs) for token, specs in alias_index.items()}


@lru_cache(maxsize=1)
def _load_alias_index() -> dict[str, tuple[_StageSpec, ...]]:
    return _build_alias_index(_load_stage_specs())


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
    """Return the conversation-turn stage catalogue."""
    return {
        "schema_version": CONVERSATION_TURN_STAGE_MODEL_SCHEMA_VERSION,
        "workflow_representation_id": CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        "stages": [_serialise_stage_spec(spec) for spec in _load_stage_specs()],
    }


def resolve_conversation_turn_stage(
    *,
    runtime_stage: Any,
    workflow_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one runtime stage token into the stage catalogue."""
    normalised_stage = _normalise_runtime_stage(runtime_stage)
    if not normalised_stage:
        return None

    candidates = _load_alias_index().get(normalised_stage, ())
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
    """Map a runtime stage sequence into stage catalogue entries."""

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
