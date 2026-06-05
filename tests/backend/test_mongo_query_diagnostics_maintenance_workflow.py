from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable import (
    mongo_query_diagnostics_maintenance_workflow as mod,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_mcp_tool_actions import (
    register_workflow_mcp_tool_actions,
)


class _FakeGateway:
    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            mod.MONGO_QUERY_DIAGNOSTICS_TOOL_NAME: SimpleNamespace(
                name=mod.MONGO_QUERY_DIAGNOSTICS_TOOL_NAME
            )
        }

    def get_method_definition(self, name: str) -> Any:
        if name != mod.MONGO_QUERY_DIAGNOSTICS_TOOL_NAME:
            return None
        return SimpleNamespace(
            category="read",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "allow_operator_diagnostics": {"type": "boolean"},
                    "source": {"type": "string"},
                    "sample_limit": {"type": "integer"},
                    "report_limit": {"type": "integer"},
                    "explain_samples": {"type": "integer"},
                },
            },
        )

    def invoke(self, name: str, payload: dict[str, Any]) -> Any:
        self.invocations.append((name, dict(payload)))
        return SimpleNamespace(
            duration_ms=12.0,
            payload={
                "schema_version": "von_mongo_query_diagnostics.v1",
                "success": True,
                "status": "ok",
                "mode": "read_only_redacted_diagnostics",
                "direct_index_mutation": False,
                "database": "von_db",
                "options": {
                    "source": payload.get("source"),
                    "sample_limit": payload.get("sample_limit"),
                    "report_limit": payload.get("report_limit"),
                },
                "summary": {
                    "schema_version": "mongo_query_targeting_report.v1",
                    "observed_shape_count": 1,
                    "ranking": (
                        "estimated_waste_score, max scanned/returned ratios, "
                        "total duration, count"
                    ),
                    "rows": [
                        {
                            "namespace": "von_db.text_relations",
                            "command_name": "find",
                            "estimated_waste_score": 321.0,
                            "docs_examined_per_returned": 321.0,
                            "keys_examined_per_returned": 0.0,
                            "max_duration_ms": 42.0,
                            "count": 1,
                            "recommended_next_step": (
                                "High query targeting: compare filter/sort shape "
                                "with repo-owned indexes and Atlas Performance "
                                "Advisor before creating any index."
                            ),
                        }
                    ],
                },
                "reports": {
                    "in_process": {"observed_shape_count": 1},
                    "profiler": {"status": "profiler_unavailable"},
                },
                "recommended_next_step": (
                    "Review high-ratio rows against repo-owned indexes and Atlas "
                    "Query Insights."
                ),
                "privacy": {"redacted": True},
                "attribution_jira": "JVNAUTOSCI-2450",
            },
        )


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    register_workflow_mcp_tool_actions(registry)
    mod.register_mongo_query_diagnostics_maintenance_actions(registry)
    return registry


@pytest.fixture
def _mock_workflow_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    invalidate_workflow_discovery_executability_caches()
    yield
    invalidate_workflow_discovery_executability_caches()
    authority_service.clear_workflow_type_resolution_cache()


def test_mongo_query_diagnostics_maintenance_workflow_structure() -> None:
    definition = (
        mod.build_mongo_query_diagnostics_maintenance_workflow_test_definition()
    )

    assert definition.workflow_id == mod.MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID
    assert definition.initial_state == "collect"
    assert set(definition.states.keys()) == {"collect", "finalise", "failed"}

    collect = definition.states["collect"]
    assert collect.actions[0].action_id == "workflow_mcp.invoke_tool"
    assert collect.actions[0].inputs["tool_name"] == (
        mod.MONGO_QUERY_DIAGNOSTICS_TOOL_NAME
    )
    assert collect.metadata["workflow_mcp_allowed_tools"] == [
        mod.MONGO_QUERY_DIAGNOSTICS_TOOL_NAME
    ]
    assert collect.metadata["writes_context_keys"] == ["mongo_query_diagnostics_report"]


def test_mongo_query_diagnostics_maintenance_workflow_invokes_diagnostic_tool() -> None:
    gateway = _FakeGateway()
    result = WorkflowExecutor(registry=_registry(), max_transitions=5).run(
        mod.build_mongo_query_diagnostics_maintenance_workflow_test_definition(),
        environment=WorkflowEnvironment(llm_client=None, gateway=gateway),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "finalise"
    assert gateway.invocations == [
        (
            mod.MONGO_QUERY_DIAGNOSTICS_TOOL_NAME,
            {
                "allow_operator_diagnostics": True,
                "source": "combined",
                "sample_limit": 200,
                "report_limit": 20,
                "explain_samples": 0,
            },
        )
    ]
    maintenance_result = result.data["mongo_query_diagnostics_maintenance_result"]
    assert maintenance_result["schema_version"] == (
        "mongo_query_diagnostics_maintenance_result.v1"
    )
    assert maintenance_result["success"] is True
    assert maintenance_result["direct_index_mutation"] is False
    assert maintenance_result["observed_shape_count"] == 1
    assert maintenance_result["profiler_status"] == "profiler_unavailable"
    assert maintenance_result["top_risk_rows"][0]["namespace"] == (
        "von_db.text_relations"
    )


def test_mongo_query_diagnostics_maintenance_bootstrap_publishes_vontology_graph(
    _mock_workflow_db: Any,
) -> None:
    from src.backend.services.mongo_query_diagnostics_maintenance_workflow_vontology_service import (
        bootstrap_canonical_mongo_query_diagnostics_maintenance_workflow,
    )

    report = bootstrap_canonical_mongo_query_diagnostics_maintenance_workflow()
    definition = load_workflow_definition_from_vontology(
        mod.MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID
    )

    assert report["success"] is True
    assert definition is not None
    assert definition.workflow_id == mod.MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID
    collect_actions = [
        action
        for state in definition.states.values()
        for action in state.actions
        if action.action_id == "workflow_mcp.invoke_tool"
    ]
    assert len(collect_actions) == 1
    assert collect_actions[0].inputs["tool_name"] == (
        mod.MONGO_QUERY_DIAGNOSTICS_TOOL_NAME
    )
