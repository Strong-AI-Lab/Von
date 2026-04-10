from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.services.turn_pipeline_monitoring_workflow_contracts import (
    TURN_PIPELINE_MONITORING_WORKFLOW_ID,
    TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID,
)
from src.backend.services.turn_pipeline_monitoring_workflow_vontology_service import (
    bootstrap_canonical_turn_pipeline_monitoring_workflows,
)
from src.backend.workflows import ActionRegistry, WorkflowEnvironment, WorkflowExecutor
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    yield
    authority_service.clear_workflow_type_resolution_cache()


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _build_orchestrator_with_gateway(gateway: InternalMCPGateway):
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    with patch.object(InternalMCPChatOrchestrator, "__init__", lambda self: None):
        orchestrator = InternalMCPChatOrchestrator()  # type: ignore[call-arg]
        orchestrator._gateway = gateway  # type: ignore[attr-defined]
        orchestrator._logger = MagicMock()
    return orchestrator


def test_bootstrap_materialises_turn_pipeline_monitoring_workflows(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_turn_pipeline_monitoring_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}

    assert report.get("success") is True
    assert counts.get("errors") == 0
    assert counts.get("workflows_published") == 2

    monitoring_definition = load_workflow_definition_from_vontology(
        TURN_PIPELINE_MONITORING_WORKFLOW_ID
    )
    assert monitoring_definition is not None
    health_step_id = authority_service._step_concept_id(
        workflow_id=TURN_PIPELINE_MONITORING_WORKFLOW_ID,
        state_id="run_health_check",
    )
    health_action = monitoring_definition.states[health_step_id].actions[0]
    assert health_action.action_id == "workflow_mcp_health_check"

    dashboard_step_id = authority_service._step_concept_id(
        workflow_id=TURN_PIPELINE_MONITORING_WORKFLOW_ID,
        state_id="build_dashboard",
    )
    dashboard_action = monitoring_definition.states[dashboard_step_id].actions[0]
    assert dashboard_action.action_id == "turn_execution_build_dashboard"

    regression_definition = load_workflow_definition_from_vontology(
        TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID
    )
    assert regression_definition is not None
    regression_step_id = authority_service._step_concept_id(
        workflow_id=TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID,
        state_id="build_selector_benchmark",
    )
    regression_action = regression_definition.states[regression_step_id].actions[0]
    assert regression_action.action_id == "turn_execution_build_selector_benchmark"


def test_turn_pipeline_monitoring_workflow_executes_via_gateway(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.integrations.internal_mcp.catalogue as catalogue

    bootstrap_canonical_turn_pipeline_monitoring_workflows()
    definition = load_workflow_definition_from_vontology(
        TURN_PIPELINE_MONITORING_WORKFLOW_ID
    )
    assert definition is not None

    monkeypatch.setattr(
        catalogue,
        "_workflow_mcp_health_check",
        lambda **_kwargs: {
            "success": True,
            "checked_tools": ["workflow_list_definitions"],
            "failed_tools": [],
            "checks": [{"tool": "workflow_list_definitions", "ok": True}],
            "capability_matrix": {"workflow_list_definitions": {"supported": True}},
        },
    )
    monkeypatch.setattr(
        catalogue,
        "_turn_execution_search_failures",
        lambda **kwargs: {
            "success": True,
            "namespace": kwargs.get("namespace"),
            "likely_failure_count": 2,
            "failure_mode_counts": {"false_completion_gate_state": 1},
            "recommendations": [
                "Review completion-gate blockers before claiming success."
            ],
        },
    )
    monkeypatch.setattr(
        catalogue,
        "_turn_execution_build_dashboard",
        lambda **kwargs: {
            "success": True,
            "namespace": kwargs.get("namespace"),
            "summary_cards": [
                {"card_id": "false_success_rate", "status": "fail"},
            ],
            "regression_views": {
                "active_regressions": [
                    {
                        "source_surface": "turn_execution",
                        "metric": "false_success_rate_pct",
                    }
                ]
            },
            "benchmark_signals": [
                {
                    "signal_id": "false_success_rate_not_worse_than_baseline",
                    "status": "fail",
                }
            ],
        },
    )

    gateway = _build_gateway()
    orchestrator = _build_orchestrator_with_gateway(gateway)
    registry = ActionRegistry()
    registry.set_fallback_handler(orchestrator._action_mcp_tool_invoke)

    result = WorkflowExecutor(registry=registry, max_transitions=12).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#system@default",
        ),
        data={
            "namespace": "#V#system@default",
            "limit": 20,
            "max_cases": 3,
            "selector_case_set": "phase1_seed",
            "selector_max_cases": 15,
            "baseline_false_success_rate_pct": 0.0,
            "baseline_selector_accuracy_pct": 90.0,
            "regression_tolerance_pct": 1.0,
            "latency_regression_tolerance_pct": 10.0,
        },
    )

    assert result.completed is True
    assert result.error is None
    assert result.final_state == authority_service._step_concept_id(
        workflow_id=TURN_PIPELINE_MONITORING_WORKFLOW_ID,
        state_id="complete",
    )
    assert result.data["health_check_failed_tools"] == []
    assert result.data["likely_failure_count"] == 2
    assert result.data["failure_mode_counts"]["false_completion_gate_state"] == 1
    assert result.data["active_regressions"][0]["metric"] == "false_success_rate_pct"
    assert result.data["dashboard_summary_cards"][0]["card_id"] == "false_success_rate"


def test_turn_pipeline_tier1_regression_workflow_executes_via_gateway(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.integrations.internal_mcp.catalogue as catalogue

    bootstrap_canonical_turn_pipeline_monitoring_workflows()
    definition = load_workflow_definition_from_vontology(
        TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID
    )
    assert definition is not None

    monkeypatch.setattr(
        catalogue,
        "_turn_execution_build_selector_benchmark",
        lambda **kwargs: {
            "success": True,
            "corpus": {"case_set": kwargs.get("case_set") or "phase1_seed"},
            "metrics": {"selector_accuracy_pct": 100.0},
            "benchmark_signals": [
                {
                    "signal_id": "selector_accuracy_not_worse_than_baseline",
                    "status": "pass",
                }
            ],
        },
    )

    gateway = _build_gateway()
    orchestrator = _build_orchestrator_with_gateway(gateway)
    registry = ActionRegistry()
    registry.set_fallback_handler(orchestrator._action_mcp_tool_invoke)

    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#system@default",
        ),
        data={"case_set": "phase1_seed", "max_cases": 12},
    )

    assert result.completed is True
    assert result.error is None
    assert result.final_state == authority_service._step_concept_id(
        workflow_id=TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID,
        state_id="complete",
    )
    assert result.data["selector_accuracy_pct"] == 100.0
    assert result.data["selector_case_set_resolved"] == "phase1_seed"
    assert result.data["selector_benchmark_signals"][0]["status"] == "pass"
