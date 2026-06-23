from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.jira_task_incremental_import_workflow_vontology_service import (
    bootstrap_canonical_jira_task_incremental_import_workflow,
)
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable import jira_task_incremental_import_workflow as mod
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology
from src.backend.workflows.workflow_launch_input_contracts import (
    WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
    resolve_workflow_launch_inputs,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
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


def test_incremental_import_workflow_requires_vontology_materialisation(
    _reset_mock_db: Any,
) -> None:
    definition = load_workflow_definition_from_vontology(
        mod.JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID
    )

    assert definition is None
    assert not hasattr(
        mod,
        "build_jira_task_incremental_import_workflow_test_definition",
    )
    assert not hasattr(
        mod,
        "build_jira_task_incremental_import_workflow_test_registration",
    )


def test_incremental_import_workflow_publishes_actor_launch_contract(
    _reset_mock_db: Any,
) -> None:
    bootstrap_report = bootstrap_canonical_jira_task_incremental_import_workflow()
    assert bootstrap_report["success"] is True

    definition = load_workflow_definition_from_vontology(
        mod.JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID
    )
    assert definition is not None
    launch_contract = definition.metadata.get("launch_input_contract")
    assert (
        launch_contract["schema_version"]
        == WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION
    )
    assert launch_contract["required_inputs"] == ["actor_concept_id"]

    mappings = {
        (item["target_context_key"], item["source_expression"])
        for item in launch_contract["input_mappings"]
    }
    assert ("actor_concept_id", "inputs.actor_concept_id") in mappings
    assert ("actor_concept_id", "inputs.user_concept_id") in mappings
    assert ("organisation_concept_id", "inputs.org_concept_id") in mappings
    assert ("namespace", "inputs.user_namespace") in mappings

    resolution = resolve_workflow_launch_inputs(
        workflow_id=mod.JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
        contract=launch_contract,
        inputs={
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#university_of_auckland_strong_ai_lab",
            "user_namespace": (
                "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
            ),
        },
        contract_source=definition.metadata.get("launch_input_contract_source"),
    )

    assert dict(resolution.resolved_inputs) == {
        "actor_concept_id": "#V#michael_witbrock",
        "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
        "namespace": "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    }
    assert resolution.unresolved_required_inputs == ()
    assert resolution.diagnostics["actor_context_binding"]["status"] == "bound"


def test_incremental_import_workflow_executes_shared_runner(
    monkeypatch: pytest.MonkeyPatch,
    _reset_mock_db: Any,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run(options):
        captured["options"] = options
        return {
            "generated_at_utc": "2026-03-09T00:00:00+00:00",
            "current_scope": {
                "summary": {"total_issues": 3, "updated": 3},
                "missing_target_issue_keys": ["JVNAUTOSCI-1199"],
            },
        }

    monkeypatch.setattr(mod, "run_jira_task_migration_sync", _fake_run)

    bootstrap_report = bootstrap_canonical_jira_task_incremental_import_workflow()
    assert bootstrap_report["success"] is True
    definition = load_workflow_definition_from_vontology(
        mod.JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID
    )
    assert definition is not None

    registry = ActionRegistry()
    mod.register_jira_task_incremental_import_actions(registry)

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        ),
        data={
            "actor_concept_id": "#V#michael_witbrock",
            "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
            "project_key": "JVNAUTOSCI",
            "updated_within_hours": 24,
            "import_referenced_targets": True,
        },
    )

    assert result.completed is True
    assert result.final_state == authority_service._step_concept_id(
        workflow_id=mod.JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
        state_id="complete",
    )
    assert result.data["jira_task_migration_summary"]["total_issues"] == 3
    assert result.data["jira_task_migration_missing_target_issue_keys"] == [
        "JVNAUTOSCI-1199"
    ]
    options = captured["options"]
    assert isinstance(options, mod.JiraTaskMigrationOptions)
    assert options.actor_concept_id == "#V#michael_witbrock"
    assert (
        options.namespace
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )
    assert options.updated_within_hours == 24
    assert options.import_referenced_targets is True


def test_registry_factory_registers_incremental_import_workflow(
    monkeypatch: pytest.MonkeyPatch,
    _reset_mock_db: Any,
) -> None:
    import src.backend.workflows.durable.registry_factory as factory
    from workflow_test_support import (
        bootstrap_authoritative_support_maintenance_workflows,
    )

    bootstrap_report = bootstrap_authoritative_support_maintenance_workflows()
    graph_publication = bootstrap_report.get("graph_publication") or {}
    assert (
        mod.JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID
        in graph_publication.get("published_workflow_ids", [])
    )
    assert not (graph_publication.get("errors_by_workflow_id") or {})
    monkeypatch.setattr(
        factory,
        "discover_workflow_ids",
        lambda: [mod.JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID],
    )
    monkeypatch.setattr(
        factory,
        "build_workflow_concept_authority_report",
        lambda registry: {},
    )
    monkeypatch.setattr(factory, "_apply_workflow_parity_policy", lambda snapshot: None)

    registry = factory._build_workflow_registry(allow_bootstrap=False)
    actions = factory.build_durable_action_registry()

    assert registry.get(mod.JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID) is not None
    assert actions.has(mod.JIRA_TASK_INCREMENTAL_IMPORT_ACTION_ID) is True
