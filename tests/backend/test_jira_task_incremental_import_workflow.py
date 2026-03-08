from __future__ import annotations

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable import jira_task_incremental_import_workflow as mod
from src.backend.workflows.engine import WorkflowExecutor


def test_incremental_import_workflow_executes_shared_runner(monkeypatch) -> None:
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

    registration = mod.get_jira_task_incremental_import_workflow_registration()
    registry = ActionRegistry()
    mod.register_jira_task_incremental_import_actions(registry)

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        registration.definition,
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
    assert result.final_state == "complete"
    assert result.data["jira_task_migration_summary"]["total_issues"] == 3
    assert result.data["jira_task_migration_missing_target_issue_keys"] == [
        "JVNAUTOSCI-1199"
    ]
    options = captured["options"]
    assert isinstance(options, mod.JiraTaskMigrationOptions)
    assert options.actor_concept_id == "#V#michael_witbrock"
    assert options.namespace == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    assert options.updated_within_hours == 24
    assert options.import_referenced_targets is True


def test_registry_factory_registers_incremental_import_workflow(monkeypatch) -> None:
    import src.backend.workflows.durable.registry_factory as factory

    monkeypatch.setattr(factory, "discover_workflow_ids", lambda: [])
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
