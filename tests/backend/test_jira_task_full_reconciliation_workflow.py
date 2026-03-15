from __future__ import annotations

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable import (
    jira_task_full_reconciliation_workflow as mod,
)


def test_gap_scan_action_executes_and_exposes_branching_outputs(monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "scan_next_jira_task_gap_block_sync",
        lambda _options: {
            "gap_block_found": True,
            "missing_issue_count": 12,
            "remaining_block_count_after_selected_block": 3,
            "highest_observed_issue_number": 1450,
            "selected_gap_block": {
                "min_issue_number": 1200,
                "max_issue_number": 1299,
                "issue_count": 100,
                "issue_keys": [f"JVNAUTOSCI-{number}" for number in range(1200, 1300)],
            },
        },
    )

    registry = ActionRegistry()
    mod.register_jira_task_full_reconciliation_actions(registry)
    context: dict[str, object] = {"project_key": "JVNAUTOSCI", "include_done": True}

    result = registry.execute(
        mod.JIRA_TASK_SCAN_NEXT_GAP_BLOCK_ACTION_ID,
        inputs={"gap_block_size": 100},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.ok is True
    assert context["last_action_succeeded"] is True
    assert result.outputs["result"] is True
    assert result.outputs["min_issue_number"] == 1200
    assert result.outputs["max_issue_number"] == 1299
    assert result.outputs["jira_reconciliation_gap_block_min_issue_number"] == 1200
    assert result.outputs["jira_reconciliation_gap_block_max_issue_number"] == 1299
    assert result.outputs["jira_reconciliation_gap_block_issue_count"] == 100
    assert result.outputs["jira_reconciliation_gap_missing_issue_count"] == 12
    assert result.outputs["jira_reconciliation_gap_remaining_block_count"] == 3
    assert result.outputs["jira_reconciliation_highest_observed_issue_number"] == 1450
    assert len(result.outputs["jira_reconciliation_gap_block_issue_keys_sample"]) == 20


def test_registry_factory_registers_full_reconciliation_support(monkeypatch) -> None:
    import src.backend.workflows.durable.registry_factory as factory

    monkeypatch.setattr(factory, "discover_workflow_ids", lambda: [])
    monkeypatch.setattr(
        factory,
        "build_workflow_concept_authority_report",
        lambda registry: {},
    )
    monkeypatch.setattr(factory, "_apply_workflow_parity_policy", lambda snapshot: None)

    actions = factory.build_durable_action_registry()

    assert actions.has(mod.JIRA_TASK_SCAN_NEXT_GAP_BLOCK_ACTION_ID) is True
