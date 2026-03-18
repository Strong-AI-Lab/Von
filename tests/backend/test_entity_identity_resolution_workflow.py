"""Tests for durable entity identity-resolution workflow (JVNAUTOSCI-1253)."""

from __future__ import annotations

from unittest.mock import patch


def test_workflow_definition_structure() -> None:
    from src.backend.workflows.durable.entity_identity_resolution_workflow import (
        ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        build_entity_identity_resolution_workflow_test_definition,
    )

    workflow = build_entity_identity_resolution_workflow_test_definition()
    assert workflow.workflow_id == ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
    assert workflow.initial_state == "scan"
    assert set(workflow.states.keys()) == {"scan", "apply", "complete", "failed"}
    assert workflow.states["complete"].terminal is True
    assert workflow.states["failed"].terminal is True


def test_scan_handler_builds_actionable_recommendations() -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    profiles = [
        {
            "concept_id": "#V#person_alice_1",
            "name_keys": ["alice smith"],
            "scope_key": "u:global|o:global",
            "type_ids": ["#V#person"],
            "source_refs": ["orcid:0000-1"],
            "relationship_targets": ["#V#org_nao"],
        },
        {
            "concept_id": "#V#person_alice_2",
            "name_keys": ["alice smith"],
            "scope_key": "u:global|o:global",
            "type_ids": ["#V#person"],
            "source_refs": ["orcid:0000-1"],
            "relationship_targets": ["#V#org_nao"],
        },
        {
            "concept_id": "#V#person_bob",
            "name_keys": ["bob taylor"],
            "scope_key": "u:global|o:global",
            "type_ids": ["#V#person"],
            "source_refs": [],
            "relationship_targets": [],
        },
    ]

    with patch.object(mod, "_scan_profiles", return_value=profiles):
        request = WorkflowActionRequest(
            action_id="identity_resolution.scan_candidates",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
        result = mod._handle_scan_candidates(request)

    assert result.ok
    summary = result.outputs["identity_resolution_scan_summary"]
    assert summary["scanned_profiles"] == 3
    assert summary["actionable_recommendation_count"] >= 1
    recommendations = result.outputs["duplicate_recommendations"]
    assert any(
        str(item.get("action")) in {"auto_merge", "queue_review"}
        for item in recommendations
    )
    clusters = result.outputs["duplicate_clusters"]
    assert len(clusters) >= 1


def test_apply_handler_merges_and_queues_with_metrics() -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    recommendations = [
        {
            "source_id": "#V#person_alice_dup",
            "target_id": "#V#person_alice",
            "confidence_score": 0.96,
            "action": "auto_merge",
            "rationale": ["shared evidence"],
        },
        {
            "source_id": "#V#person_pat_dup",
            "target_id": "#V#person_pat",
            "confidence_score": 0.79,
            "action": "queue_review",
            "rationale": ["name overlap only"],
        },
    ]

    with (
        patch.object(mod, "merge_concepts", return_value={"success": True, "operations": [{}, {}]}),
        patch.object(mod, "_record_merge_audit", return_value=None),
        patch.object(
            mod,
            "_queue_uncertain",
            return_value={
                "success": True,
                "assertion": {"assertion_id": "ura_test_1"},
            },
        ),
        patch.object(mod, "_cluster_count_after_apply", return_value=1),
    ):
        request = WorkflowActionRequest(
            action_id="identity_resolution.apply_resolutions",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={
                "duplicate_recommendations": recommendations,
                "duplicate_cluster_count_before": 3,
                "scan_limit_used": 200,
                "text_relation_limit_used": 60,
                "max_pair_evaluations_used": 200,
                "auto_apply_confidence_threshold": 0.93,
                "review_confidence_threshold": 0.72,
            },
        )
        result = mod._handle_apply_resolutions(request)

    assert result.ok
    summary = result.outputs["identity_resolution_apply_summary"]
    assert summary["merged_count"] == 1
    assert summary["queued_count"] == 1
    assert summary["failed_count"] == 0
    assert summary["duplicate_cluster_count_before"] == 3
    assert summary["duplicate_cluster_count_after"] == 1
    assert summary["duplicate_cluster_reduction"] == 2


def test_apply_handler_dry_run_does_not_write() -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    recommendations = [
        {
            "source_id": "#V#person_alice_dup",
            "target_id": "#V#person_alice",
            "confidence_score": 0.96,
            "action": "auto_merge",
        },
        {
            "source_id": "#V#person_pat_dup",
            "target_id": "#V#person_pat",
            "confidence_score": 0.79,
            "action": "queue_review",
        },
    ]

    with (
        patch.object(mod, "merge_concepts") as merge_mock,
        patch.object(mod, "_queue_uncertain") as queue_mock,
    ):
        request = WorkflowActionRequest(
            action_id="identity_resolution.apply_resolutions",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={
                "dry_run": True,
                "duplicate_recommendations": recommendations,
                "duplicate_cluster_count_before": 2,
            },
        )
        result = mod._handle_apply_resolutions(request)

    assert result.ok
    summary = result.outputs["identity_resolution_apply_summary"]
    assert summary["dry_run"] is True
    assert summary["would_merge_count"] == 1
    assert summary["would_queue_count"] == 1
    merge_mock.assert_not_called()
    queue_mock.assert_not_called()
