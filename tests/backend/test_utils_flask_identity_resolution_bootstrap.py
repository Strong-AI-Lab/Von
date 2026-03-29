"""Integration coverage for identity-resolution schedule bootstrap at startup."""

from __future__ import annotations

from unittest.mock import MagicMock


def test_start_durable_system_bootstraps_identity_schedule(monkeypatch) -> None:
    import src.backend.server.utils_flask as utils_flask
    from src.backend.workflows.durable import startup as durable_startup
    from src.backend.services import (
        episode_evaluation_workflow_vontology_service as episode_evaluation_workflow_bootstrap,
        identity_resolution_schedule_bootstrap_service as schedule_bootstrap,
        paper_representation_workflow_vontology_service as paper_workflow_bootstrap,
        parent_specificity_schedule_bootstrap_service as parent_specificity_schedule_bootstrap,
        parent_specificity_vontology_service as parent_specificity_prompt_bootstrap,
        testing_workflow_vontology_service as testing_workflow_bootstrap,
        talk_representation_workflow_vontology_service as talk_workflow_bootstrap,
        workflow_description_vontology_service as workflow_description_prompt_bootstrap,
        workflow_gap_vontology_service as workflow_gap_prompt_bootstrap,
    )

    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    # Reset globals to force fresh startup path.
    monkeypatch.setattr(utils_flask, "_durable_workflow_registry", None)
    monkeypatch.setattr(utils_flask, "_durable_action_registry", None)

    monkeypatch.setattr(utils_flask, "_build_durable_workflow_registry", lambda: object())
    monkeypatch.setattr(utils_flask, "_build_durable_action_registry", lambda: object())
    monkeypatch.setattr(utils_flask, "_get_durable_definition_loader", lambda: lambda _workflow_id: None)
    monkeypatch.setattr(
        utils_flask,
        "_bootstrap_workflow_authority_for_startup",
        lambda _logger: {
            "success": True,
            "counts": {
                "registry_workflows": 31,
                "created": 0,
                "updated": 10,
                "unchanged": 21,
                "errors": 0,
            },
            "before_authority_counts": {
                "registry_workflows": 31,
                "missing_concepts": 0,
                "missing_required_type": 10,
                "lookup_errors": 0,
                "valid": 21,
            },
            "after_authority_counts": {
                "registry_workflows": 31,
                "missing_concepts": 0,
                "missing_required_type": 0,
                "lookup_errors": 0,
                "valid": 31,
            },
        },
    )

    monkeypatch.setattr(durable_startup, "recover_orphaned_instances", lambda: 0)
    monkeypatch.setattr(
        durable_startup,
        "start_worker_and_scheduler",
        lambda **_kwargs: {"worker": "worker", "scheduler": "scheduler"},
    )
    monkeypatch.setattr(
        durable_startup,
        "get_system_status",
        lambda: {
            "worker_running": True,
            "scheduler_running": True,
            "instances": {"pending": 0},
        },
    )

    monkeypatch.setattr(
        schedule_bootstrap,
        "ensure_identity_resolution_background_schedule",
        lambda: {"success": True, "ensured": True, "created_count": 1},
    )
    monkeypatch.setattr(
        parent_specificity_prompt_bootstrap,
        "ensure_parent_specificity_prompt_support",
        lambda: {"success": True, "linked_workflow_ids": ["#V#parent_specificity_rumination_workflow"]},
    )
    monkeypatch.setattr(
        parent_specificity_schedule_bootstrap,
        "ensure_parent_specificity_background_schedule",
        lambda: {"success": True, "ensured": True, "created_count": 1},
    )
    monkeypatch.setattr(
        workflow_gap_prompt_bootstrap,
        "ensure_workflow_gap_prompt_support",
        lambda: {
            "success": True,
            "linked_workflow_ids": [
                "#V#workflow_discovery_gap_recovery_workflow",
                "#V#workflow_gap_test_workflow",
            ],
        },
    )
    monkeypatch.setattr(
        workflow_description_prompt_bootstrap,
        "ensure_workflow_description_prompt_support",
        lambda: {
            "success": True,
            "linked_workflow_ids": ["#V#enrichment_workflow"],
        },
    )
    monkeypatch.setattr(
        paper_workflow_bootstrap,
        "bootstrap_canonical_paper_representation_workflows",
        lambda: {
            "success": True,
            "workflow_ids": [
                "#V#scholarly_paper_representation_workflow",
                "#V#arxiv_paper_representation_workflow",
            ],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        episode_evaluation_workflow_bootstrap,
        "bootstrap_canonical_episode_evaluation_workflow",
        lambda: {
            "success": True,
            "workflow_ids": ["#V#episode_evaluation_workflow"],
            "publication": {"skipped": True},
            "event_bindings": {"success": True, "binding_count": 2},
        },
    )
    monkeypatch.setattr(
        testing_workflow_bootstrap,
        "bootstrap_canonical_testing_workflows",
        lambda: {
            "success": True,
            "workflow_ids": ["#V#meeting_invitation_testing_workflow"],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        talk_workflow_bootstrap,
        "bootstrap_canonical_talk_representation_workflows",
        lambda: {
            "success": True,
            "workflow_ids": ["#V#talk_representation_workflow"],
            "publication": {"skipped": True},
        },
    )

    app_logger = MagicMock()
    result = utils_flask._start_durable_workflow_system(app_logger)

    assert isinstance(result, dict)
    bootstrap_report = result.get("identity_resolution_schedule_bootstrap")
    assert isinstance(bootstrap_report, dict)
    assert bootstrap_report.get("success") is True
    assert bootstrap_report.get("created_count") == 1
    parent_prompt_report = result.get("parent_specificity_prompt_bootstrap")
    assert isinstance(parent_prompt_report, dict)
    assert parent_prompt_report.get("success") is True
    workflow_gap_prompt_report = result.get("workflow_gap_prompt_bootstrap")
    assert isinstance(workflow_gap_prompt_report, dict)
    assert workflow_gap_prompt_report.get("success") is True
    workflow_description_prompt_report = result.get(
        "workflow_description_prompt_bootstrap"
    )
    assert isinstance(workflow_description_prompt_report, dict)
    assert workflow_description_prompt_report.get("success") is True
    paper_workflow_bootstrap_report = result.get("paper_workflow_bootstrap")
    assert isinstance(paper_workflow_bootstrap_report, dict)
    assert paper_workflow_bootstrap_report.get("success") is True
    episode_evaluation_workflow_bootstrap_report = result.get(
        "episode_evaluation_workflow_bootstrap"
    )
    assert isinstance(episode_evaluation_workflow_bootstrap_report, dict)
    assert episode_evaluation_workflow_bootstrap_report.get("success") is True
    testing_workflow_bootstrap_report = result.get("testing_workflow_bootstrap")
    assert isinstance(testing_workflow_bootstrap_report, dict)
    assert testing_workflow_bootstrap_report.get("success") is True
    talk_workflow_bootstrap_report = result.get("talk_workflow_bootstrap")
    assert isinstance(talk_workflow_bootstrap_report, dict)
    assert talk_workflow_bootstrap_report.get("success") is True
    workflow_authority_bootstrap_report = result.get("workflow_authority_bootstrap")
    assert isinstance(workflow_authority_bootstrap_report, dict)
    assert workflow_authority_bootstrap_report.get("success") is True
    assert workflow_authority_bootstrap_report.get("counts", {}).get("updated") == 10
    parent_schedule_report = result.get("parent_specificity_schedule_bootstrap")
    assert isinstance(parent_schedule_report, dict)
    assert parent_schedule_report.get("success") is True
