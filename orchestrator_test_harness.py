from __future__ import annotations

from typing import Any, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _WorkflowModelPolicyState,
)


def _stub_stage_model_snapshot() -> dict[str, Any]:
    stages = [
        ("workflow_discovery", "Workflow discovery", None, None),
        ("workflow_dispatch", "Workflow dispatch", None, None),
        ("tool_plan", "Plan tool calls", "#V#tool_calling_workflow", "plan"),
        ("tool_execute", "Execute tool calls", "#V#tool_calling_workflow", "execute"),
        ("screen_backfill", "Summarise/backfill response", "#V#tool_calling_workflow", "backfill"),
        ("response_finalising", "Finalising response", None, None),
        ("completed", "Completed", None, None),
    ]
    return {
        "schema_version": "conversation_turn_stage_model.v1",
        "workflow_representation_id": "#V#conversation_turn_execution_workflow",
        "stages": [
            {
                "stage_id": stage_id,
                "stage_label": stage_label,
                "runtime_aliases": [stage_id],
                "workflow_id": workflow_id,
                "workflow_state_id": workflow_state_id,
            }
            for stage_id, stage_label, workflow_id, workflow_state_id in stages
        ],
    }


def _stub_stage_path(*, runtime_stages: Any, **_kwargs) -> dict[str, Any]:
    snapshot = _stub_stage_model_snapshot()
    stage_lookup = {
        str(entry.get("stage_id")): dict(entry)
        for entry in snapshot["stages"]
        if isinstance(entry, dict) and isinstance(entry.get("stage_id"), str)
    }
    ordered_runtime_stages: list[str] = []
    for item in runtime_stages or []:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned or cleaned in ordered_runtime_stages:
            continue
        if cleaned == "workflow_discovery_complete":
            cleaned = "workflow_discovery"
        elif cleaned in {"orchestrator_start", "orchestrator_end"}:
            cleaned = "workflow_dispatch"
        ordered_runtime_stages.append(cleaned)

    path = []
    for sequence_no, stage_id in enumerate(ordered_runtime_stages):
        entry: dict[str, Any] = dict(
            stage_lookup.get(stage_id) or {"stage_id": stage_id}
        )
        entry.setdefault("stage_label", stage_id.replace("_", " ").title())
        entry["sequence_no"] = sequence_no
        entry["runtime_stage"] = stage_id
        entry["runtime_stage_normalised"] = stage_id
        entry["mapping_status"] = "mapped"
        path.append(entry)

    return {
        "schema_version": "conversation_turn_stage_path.v1",
        "stage_model_schema_version": snapshot["schema_version"],
        "workflow_representation_id": snapshot["workflow_representation_id"],
        "workflow_id": None,
        "has_unmapped_runtime_stages": False,
        "unmapped_runtime_stages": [],
        "path": path,
    }


def build_db_independent_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gateway: Any,
    selector_enabled: bool = False,
    max_tool_invocations: int = 1,
    tool_batch_cap: int = 3,
) -> InternalMCPChatOrchestrator:
    """Build an orchestrator without DB/model-registry dependencies.

    These tests target routing and write-policy behaviour, not Mongo-backed
    workflow discovery or model registry resolution. Keep the harness minimal
    so orchestration-path regressions fail fast instead of hanging on startup.
    """

    env_val = "1" if selector_enabled else "0"
    monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", env_val)

    from src.backend.workflows import WorkflowRegistry, register_default_workflows
    from src.backend.workflows.action_registry import ActionRegistry

    def _build_test_registry() -> WorkflowRegistry:
        registry = WorkflowRegistry()
        register_default_workflows(registry)
        return registry

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.build_workflow_registry",
        _build_test_registry,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.build_durable_action_registry",
        lambda: ActionRegistry(),
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_tool_metadata",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_tool_salience",
        lambda *_a, **_kw: "medium",
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.is_tool_visible",
        lambda *_a, **_kw: True,
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway),
        max_tool_invocations=max_tool_invocations,
        tool_batch_cap=tool_batch_cap,
    )

    monkeypatch.setattr(
        orchestrator,
        "_load_workflow_model_policy",
        lambda *_a, **_kw: (
            _WorkflowModelPolicyState(
                enabled=False,
                policy=None,
                policy_id=None,
                predicate_id=None,
                errors=[],
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_ontology_preflight",
        lambda *_a, **_kw: type(
            "_Preflight", (), {"telemetry": None, "message": None}
        )(),
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_concept_id_by_name",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda *_a, **_kw: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.build_conversation_turn_stage_model_snapshot",
        _stub_stage_model_snapshot,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.build_conversation_turn_stage_path",
        _stub_stage_path,
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.build_conversation_turn_stage_model_snapshot",
        _stub_stage_model_snapshot,
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.build_conversation_turn_stage_path",
        _stub_stage_path,
    )

    return orchestrator
