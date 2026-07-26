"""Integration coverage for identity-resolution schedule bootstrap at startup."""

from __future__ import annotations

from unittest.mock import MagicMock


def test_build_durable_workflow_bootstrap_summary_surfaces_seed_repair_drift() -> None:
    import src.backend.server.utils_flask as utils_flask

    summary = utils_flask._build_durable_workflow_bootstrap_summary(
        {
            "conversation_turn_workflow_bootstrap": {
                "success": True,
                "publication": {
                    "materialisation_status": "current",
                    "drift_detected": False,
                },
            },
            "turn_pipeline_monitoring_workflow_bootstrap": {
                "success": True,
                "publication": {
                    "materialisation_status": "current",
                    "drift_detected": False,
                },
            },
            "concept_search_instance_retrieval_workflow_bootstrap": {
                "success": True,
                "publication": {
                    "materialisation_status": "current",
                    "drift_detected": False,
                },
            },
            "lab_status_digest_workflow_bootstrap": {
                "success": True,
                "publication": {
                    "materialisation_status": "current",
                    "drift_detected": False,
                },
            },
            "represented_artefact_creation_workflow_bootstrap": {
                "success": True,
                "publication": {
                    "materialisation_status": "current",
                    "drift_detected": False,
                },
            },
            "paper_workflow_bootstrap": {
                "success": True,
                "publication": {
                    "materialisation_status": "repaired_from_repo_seed",
                    "drift_detected": True,
                    "drift_workflow_ids": [
                        "#V#arxiv_paper_representation_workflow",
                    ],
                },
            },
            "talk_workflow_bootstrap": {
                "success": True,
                "publication": {
                    "materialisation_status": "current",
                    "skip_reason": "existing_materialisation_valid",
                    "drift_detected": False,
                },
            },
            "operational_learning_release_authority_bootstrap": {
                "success": True,
                "workflow_publication": {
                    "publication": {
                        "materialisation_status": "current",
                    }
                },
            },
        }
    )

    assert summary == {
        "conversation_turn_workflow_bootstrap": {
            "success": True,
            "materialisation_status": "current",
            "drift_detected": False,
        },
        "turn_pipeline_monitoring_workflow_bootstrap": {
            "success": True,
            "materialisation_status": "current",
            "drift_detected": False,
        },
        "concept_search_instance_retrieval_workflow_bootstrap": {
            "success": True,
            "materialisation_status": "current",
            "drift_detected": False,
        },
        "lab_status_digest_workflow_bootstrap": {
            "success": True,
            "materialisation_status": "current",
            "drift_detected": False,
        },
        "represented_artefact_creation_workflow_bootstrap": {
            "success": True,
            "materialisation_status": "current",
            "drift_detected": False,
        },
        "paper_workflow_bootstrap": {
            "success": True,
            "materialisation_status": "repaired_from_repo_seed",
            "drift_detected": True,
            "drift_workflow_ids": [
                "#V#arxiv_paper_representation_workflow",
            ],
        },
        "talk_workflow_bootstrap": {
            "success": True,
            "materialisation_status": "current",
            "drift_detected": False,
            "skip_reason": "existing_materialisation_valid",
        },
        "operational_learning_release_authority_bootstrap": {
            "success": True,
        },
    }


def test_start_durable_system_bootstraps_identity_schedule(monkeypatch) -> None:
    import src.backend.server.utils_flask as utils_flask
    from src.backend.workflows.durable import startup as durable_startup
    from src.backend.services import (
        ai_chat_session_source_profile_vontology_service as ai_chat_session_source_profile_bootstrap,
        benchmark_suite_vontology_service as benchmark_suite_bootstrap,
        concept_search_instance_retrieval_workflow_vontology_service as concept_search_instance_retrieval_workflow_bootstrap,
        conversation_turn_workflow_vontology_service as conversation_turn_workflow_bootstrap,
        email_source_representation_convergence_schedule_bootstrap_service as email_source_convergence_schedule_bootstrap,
        email_source_representation_convergence_workflow_vontology_service as email_source_convergence_workflow_bootstrap,
        entity_information_retrieval_workflow_vontology_service as entity_information_retrieval_workflow_bootstrap,
        entity_identity_resolution_workflow_vontology_service as entity_identity_resolution_workflow_bootstrap,
        entity_representation_workflow_vontology_service as entity_workflow_bootstrap,
        episode_evaluation_workflow_vontology_service as episode_evaluation_workflow_bootstrap,
        identity_resolution_schedule_bootstrap_service as schedule_bootstrap,
        jira_task_incremental_import_workflow_vontology_service as jira_task_incremental_import_workflow_bootstrap,
        lab_status_digest_workflow_vontology_service as lab_status_digest_workflow_bootstrap,
        mongo_query_diagnostics_maintenance_workflow_vontology_service as mongo_query_diagnostics_workflow_bootstrap,
        multilingual_concept_enrichment_schedule_bootstrap_service as multilingual_schedule_bootstrap,
        multilingual_concept_enrichment_vontology_service as multilingual_workflow_bootstrap,
        operational_certification_vontology_service as operational_certification_bootstrap,
        operational_learning_release_authority_vontology_service as operational_learning_release_bootstrap,
        paper_representation_workflow_vontology_service as paper_workflow_bootstrap,
        paper_recommendation_background_schedule_bootstrap_service as paper_recommendation_schedule_bootstrap,
        paper_recommendation_workflow_vontology_service as paper_recommendation_workflow_bootstrap,
        parent_specificity_schedule_bootstrap_service as parent_specificity_schedule_bootstrap,
        parent_specificity_vontology_service as parent_specificity_prompt_bootstrap,
        representation_workflow_routing_coverage_audit_vontology_service as representation_routing_audit_workflow_bootstrap,
        represented_artefact_creation_workflow_vontology_service as represented_artefact_creation_workflow_bootstrap,
        testing_workflow_vontology_service as testing_workflow_bootstrap,
        talk_representation_workflow_vontology_service as talk_workflow_bootstrap,
        turn_pipeline_monitoring_schedule_bootstrap_service as turn_pipeline_monitoring_schedule_bootstrap,
        turn_pipeline_monitoring_workflow_vontology_service as turn_pipeline_monitoring_workflow_bootstrap,
        workflow_capability_service as workflow_capability_service,
        workflow_authoring_vontology_service as workflow_authoring_prompt_bootstrap,
        workflow_description_vontology_service as workflow_description_prompt_bootstrap,
        workflow_model_selection_workflow_vontology_service as workflow_model_selection_bootstrap,
    )

    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    startup_events: list[str] = []

    # Reset globals to force fresh startup path.
    monkeypatch.setattr(utils_flask, "_durable_workflow_registry", None)
    monkeypatch.setattr(utils_flask, "_durable_action_registry", None)

    monkeypatch.setattr(
        utils_flask, "_build_durable_workflow_registry", lambda: object()
    )
    monkeypatch.setattr(utils_flask, "_build_durable_action_registry", lambda: object())
    monkeypatch.setattr(
        utils_flask, "_get_durable_definition_loader", lambda: lambda _workflow_id: None
    )
    monkeypatch.setattr(
        workflow_capability_service,
        "run_workflow_capability_index_startup_check",
        lambda *, workflow_registry=None, timeout_seconds=8.0: {
            "success": True,
            "ready": True,
            "status": "ready",
            "summary": "Workflow capability index ready after startup check.",
            "detail": "Indexed 31 workflows.",
            "timeout_seconds": timeout_seconds,
        },
    )
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
        "release_ineligible_worker_claims",
        lambda: 0,
    )

    def _start_worker_and_scheduler(**_kwargs):
        startup_events.append("worker_started")
        return {"worker": "worker", "scheduler": "scheduler"}

    monkeypatch.setattr(
        durable_startup,
        "start_worker_and_scheduler",
        _start_worker_and_scheduler,
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
        schedule_bootstrap,
        "ensure_identity_resolution_event_bindings",
        lambda: {
            "success": True,
            "ensured": True,
            "created_count": 1,
            "updated_count": 0,
            "binding_count": 1,
            "event_type": "identity_resolution.requested",
            "workflow_id": "#V#entity_identity_resolution_workflow",
            "bindings": [
                {
                    "binding_id": "binding_test_1",
                    "event_type": "identity_resolution.requested",
                    "workflow_id": "#V#entity_identity_resolution_workflow",
                    "enabled": True,
                }
            ],
        },
    )
    monkeypatch.setattr(
        parent_specificity_prompt_bootstrap,
        "ensure_parent_specificity_prompt_support",
        lambda: {
            "success": True,
            "linked_workflow_ids": ["#V#parent_specificity_rumination_workflow"],
        },
    )
    monkeypatch.setattr(
        parent_specificity_schedule_bootstrap,
        "ensure_parent_specificity_background_schedule",
        lambda: {"success": True, "ensured": True, "created_count": 1},
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
        workflow_authoring_prompt_bootstrap,
        "ensure_workflow_authoring_prompt_support",
        lambda: {
            "success": True,
            "linked_workflow_ids": ["#V#workflow_authoring_workflow"],
        },
    )

    def _bootstrap_entity_workflows():
        startup_events.append("entity_bootstrap")
        return {
            "success": True,
            "workflow_ids": ["#V#entity_representation_workflow"],
            "publication": {"skipped": True},
        }

    monkeypatch.setattr(
        entity_workflow_bootstrap,
        "bootstrap_canonical_entity_representation_workflows",
        _bootstrap_entity_workflows,
    )
    monkeypatch.setattr(
        entity_information_retrieval_workflow_bootstrap,
        "bootstrap_canonical_entity_information_retrieval_workflow",
        lambda: {
            "success": True,
            "workflow_ids": ["#V#entity_information_retrieval_workflow"],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        entity_identity_resolution_workflow_bootstrap,
        "bootstrap_canonical_entity_identity_resolution_workflow",
        lambda: {
            "success": True,
            "workflow_id": "#V#entity_identity_resolution_workflow",
            "prompt_concept_id": "#V#entity_duplicate_reasoning_prompt",
            "seeded_prompt_count": 0,
        },
    )
    monkeypatch.setattr(
        concept_search_instance_retrieval_workflow_bootstrap,
        "bootstrap_canonical_concept_search_instance_retrieval_workflow",
        lambda: {
            "success": True,
            "workflow_ids": ["#V#concept_search_instance_retrieval_workflow"],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        lab_status_digest_workflow_bootstrap,
        "bootstrap_canonical_lab_status_digest_workflow",
        lambda: {
            "success": True,
            "workflow_ids": ["#V#lab_project_status_digest_workflow"],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        represented_artefact_creation_workflow_bootstrap,
        "bootstrap_canonical_represented_artefact_creation_workflow",
        lambda: {
            "success": True,
            "workflow_ids": ["#V#represented_artefact_creation_workflow"],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        multilingual_workflow_bootstrap,
        "bootstrap_canonical_multilingual_concept_enrichment_workflow",
        lambda: {
            "success": True,
            "workflow_ids": ["#V#multilingual_concept_enrichment_rumination_workflow"],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        conversation_turn_workflow_bootstrap,
        "bootstrap_canonical_conversation_turn_workflows",
        lambda: {
            "success": True,
            "workflow_ids": [
                "#V#chat_assistant_workflow",
                "#V#tool_calling_workflow",
                "#V#turn_completion_gate_workflow",
                "#V#conversation_turn_execution_workflow",
            ],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        email_source_convergence_workflow_bootstrap,
        "bootstrap_canonical_email_source_representation_convergence_workflows",
        lambda: {
            "success": True,
            "workflow_ids": [
                "#V#zhan_gmail_arxiv_ingestion_workflow",
                "#V#email_arxiv_ingestion_from_message_workflow",
            ],
            "publication": {"skipped": True},
            "gmail_completion_hint": {"success": True, "entry_count": 1},
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
        jira_task_incremental_import_workflow_bootstrap,
        "bootstrap_canonical_jira_task_incremental_import_workflow",
        lambda: {
            "success": True,
            "workflow_id": "#V#jira_task_incremental_import_workflow",
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        mongo_query_diagnostics_workflow_bootstrap,
        "bootstrap_canonical_mongo_query_diagnostics_maintenance_workflow",
        lambda: {
            "success": True,
            "workflow_id": "#V#mongo_query_diagnostics_maintenance_workflow",
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        representation_routing_audit_workflow_bootstrap,
        "bootstrap_canonical_representation_workflow_routing_coverage_audit_workflow",
        lambda: {
            "success": True,
            "workflow_id": "#V#representation_workflow_routing_coverage_audit_workflow",
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        paper_recommendation_workflow_bootstrap,
        "bootstrap_canonical_paper_recommendation_workflow",
        lambda: {
            "success": True,
            "workflow_id": "#V#paper_recommendation_evaluation_workflow",
            "publication": {"skipped": True},
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
        turn_pipeline_monitoring_workflow_bootstrap,
        "bootstrap_canonical_turn_pipeline_monitoring_workflows",
        lambda: {
            "success": True,
            "workflow_ids": [
                "#V#turn_pipeline_monitoring_workflow",
                "#V#turn_pipeline_tier1_regression_workflow",
            ],
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        workflow_model_selection_bootstrap,
        "bootstrap_canonical_workflow_model_selection_workflow",
        lambda: {
            "success": True,
            "workflow_id": "#V#workflow_model_selection_workflow",
            "publication": {"skipped": True},
        },
    )
    monkeypatch.setattr(
        benchmark_suite_bootstrap,
        "ensure_canonical_benchmark_suites_from_seed_fixtures",
        lambda: {"success": True, "suite_count": 1},
    )
    monkeypatch.setattr(
        operational_certification_bootstrap,
        "bootstrap_operational_certification_authority",
        lambda: {
            "success": True,
            "evaluator_workflow_id": "#V#operational_state_evidence_evaluator",
        },
    )
    monkeypatch.setattr(
        operational_learning_release_bootstrap,
        "bootstrap_operational_learning_release_authority",
        lambda: {
            "success": True,
            "schema_version": "operational_learning_release_authority_bootstrap.v1",
            "candidate_behaviour_workflow_id": (
                "#V#operational_learning_candidate_behaviour_evaluation_workflow"
            ),
        },
    )
    monkeypatch.setattr(
        ai_chat_session_source_profile_bootstrap,
        "ensure_canonical_ai_chat_session_source_profiles_from_seed_fixture",
        lambda: {"success": True, "profile_count": 1},
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
    monkeypatch.setattr(
        turn_pipeline_monitoring_schedule_bootstrap,
        "ensure_turn_pipeline_monitoring_schedules",
        lambda: {"success": True, "ensured": True, "created_count": 2},
    )
    monkeypatch.setattr(
        email_source_convergence_schedule_bootstrap,
        "ensure_email_arxiv_representation_convergence_schedule",
        lambda: {"success": True, "ensured": True, "created_count": 1},
    )
    monkeypatch.setattr(
        paper_recommendation_schedule_bootstrap,
        "ensure_paper_recommendation_background_schedule",
        lambda: {"success": True, "ensured": True, "created_count": 1},
    )
    monkeypatch.setattr(
        multilingual_schedule_bootstrap,
        "ensure_multilingual_concept_enrichment_background_schedule",
        lambda: {"success": True, "ensured": True, "created_count": 1},
    )

    app_logger = MagicMock()
    result = utils_flask._start_durable_workflow_system(app_logger)

    assert isinstance(result, dict)
    assert startup_events[:2] == ["worker_started", "entity_bootstrap"]
    assert result.get("startup_queue_ready") == {
        "stage": "before_canonical_bootstraps",
        "recovered_orphaned_instances": 0,
        "released_ineligible_worker_claims": 0,
    }
    bootstrap_report = result.get("identity_resolution_schedule_bootstrap")
    assert isinstance(bootstrap_report, dict)
    assert bootstrap_report.get("success") is True
    assert bootstrap_report.get("created_count") == 1
    event_binding_report = result.get("identity_resolution_event_binding_bootstrap")
    assert isinstance(event_binding_report, dict)
    assert event_binding_report.get("success") is True
    assert event_binding_report.get("created_count") == 1
    assert event_binding_report.get("event_type") == "identity_resolution.requested"
    assert event_binding_report.get("workflow_id") == (
        "#V#entity_identity_resolution_workflow"
    )
    parent_prompt_report = result.get("parent_specificity_prompt_bootstrap")
    assert isinstance(parent_prompt_report, dict)
    assert parent_prompt_report.get("success") is True
    workflow_description_prompt_report = result.get(
        "workflow_description_prompt_bootstrap"
    )
    assert isinstance(workflow_description_prompt_report, dict)
    assert workflow_description_prompt_report.get("success") is True
    entity_workflow_bootstrap_report = result.get("entity_workflow_bootstrap")
    assert isinstance(entity_workflow_bootstrap_report, dict)
    assert entity_workflow_bootstrap_report.get("success") is True
    entity_information_retrieval_workflow_bootstrap_report = result.get(
        "entity_information_retrieval_workflow_bootstrap"
    )
    assert isinstance(entity_information_retrieval_workflow_bootstrap_report, dict)
    assert entity_information_retrieval_workflow_bootstrap_report.get("success") is True
    concept_search_instance_retrieval_workflow_bootstrap_report = result.get(
        "concept_search_instance_retrieval_workflow_bootstrap"
    )
    assert isinstance(
        concept_search_instance_retrieval_workflow_bootstrap_report,
        dict,
    )
    assert (
        concept_search_instance_retrieval_workflow_bootstrap_report.get("success")
        is True
    )
    lab_status_digest_workflow_bootstrap_report = result.get(
        "lab_status_digest_workflow_bootstrap"
    )
    assert isinstance(lab_status_digest_workflow_bootstrap_report, dict)
    assert lab_status_digest_workflow_bootstrap_report.get("success") is True
    represented_artefact_creation_workflow_bootstrap_report = result.get(
        "represented_artefact_creation_workflow_bootstrap"
    )
    assert isinstance(represented_artefact_creation_workflow_bootstrap_report, dict)
    assert (
        represented_artefact_creation_workflow_bootstrap_report.get("success") is True
    )
    multilingual_workflow_bootstrap_report = result.get(
        "multilingual_concept_enrichment_workflow_bootstrap"
    )
    assert isinstance(multilingual_workflow_bootstrap_report, dict)
    assert multilingual_workflow_bootstrap_report.get("success") is True
    entity_identity_resolution_workflow_bootstrap_report = result.get(
        "entity_identity_resolution_workflow_bootstrap"
    )
    assert isinstance(entity_identity_resolution_workflow_bootstrap_report, dict)
    assert entity_identity_resolution_workflow_bootstrap_report.get("success") is True
    assert (
        entity_identity_resolution_workflow_bootstrap_report.get("prompt_concept_id")
        == "#V#entity_duplicate_reasoning_prompt"
    )
    conversation_turn_workflow_bootstrap_report = result.get(
        "conversation_turn_workflow_bootstrap"
    )
    assert isinstance(conversation_turn_workflow_bootstrap_report, dict)
    assert conversation_turn_workflow_bootstrap_report.get("success") is True
    email_source_convergence_workflow_bootstrap_report = result.get(
        "email_source_convergence_workflow_bootstrap"
    )
    assert isinstance(email_source_convergence_workflow_bootstrap_report, dict)
    assert email_source_convergence_workflow_bootstrap_report.get("success") is True
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
    operational_learning_release_bootstrap_report = result.get(
        "operational_learning_release_bootstrap"
    )
    assert isinstance(operational_learning_release_bootstrap_report, dict)
    assert operational_learning_release_bootstrap_report.get("success") is True
    turn_pipeline_monitoring_workflow_bootstrap_report = result.get(
        "turn_pipeline_monitoring_workflow_bootstrap"
    )
    assert isinstance(turn_pipeline_monitoring_workflow_bootstrap_report, dict)
    assert turn_pipeline_monitoring_workflow_bootstrap_report.get("success") is True
    talk_workflow_bootstrap_report = result.get("talk_workflow_bootstrap")
    assert isinstance(talk_workflow_bootstrap_report, dict)
    assert talk_workflow_bootstrap_report.get("success") is True
    operational_learning_release_authority_report = result.get(
        "operational_learning_release_authority_bootstrap"
    )
    assert isinstance(operational_learning_release_authority_report, dict)
    assert operational_learning_release_authority_report.get("success") is True
    assert (
        operational_learning_release_authority_report.get(
            "candidate_behaviour_workflow_id"
        )
        == "#V#operational_learning_candidate_behaviour_evaluation_workflow"
    )
    workflow_authority_bootstrap_report = result.get("workflow_authority_bootstrap")
    assert isinstance(workflow_authority_bootstrap_report, dict)
    assert workflow_authority_bootstrap_report.get("success") is True
    assert workflow_authority_bootstrap_report.get("counts", {}).get("updated") == 10
    workflow_capability_index_startup_report = result.get(
        "workflow_capability_index_startup_check"
    )
    assert isinstance(workflow_capability_index_startup_report, dict)
    assert workflow_capability_index_startup_report.get("ready") is True
    turn_pipeline_monitoring_schedule_report = result.get(
        "turn_pipeline_monitoring_schedule_bootstrap"
    )
    assert isinstance(turn_pipeline_monitoring_schedule_report, dict)
    assert turn_pipeline_monitoring_schedule_report.get("success") is True
    email_source_convergence_schedule_report = result.get(
        "email_arxiv_convergence_schedule_bootstrap"
    )
    assert isinstance(email_source_convergence_schedule_report, dict)
    assert email_source_convergence_schedule_report.get("success") is True
    parent_schedule_report = result.get("parent_specificity_schedule_bootstrap")
    assert isinstance(parent_schedule_report, dict)
    assert parent_schedule_report.get("success") is True
    multilingual_schedule_report = result.get(
        "multilingual_concept_enrichment_schedule_bootstrap"
    )
    assert isinstance(multilingual_schedule_report, dict)
    assert multilingual_schedule_report.get("success") is True
