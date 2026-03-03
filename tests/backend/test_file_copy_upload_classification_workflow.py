from __future__ import annotations

import json

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.file_copy_upload_classification_workflow import (
    DEFAULT_SCHOLARLY_WORKFLOW_ID,
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE,
    build_file_copy_upload_classification_workflow,
    register_file_copy_upload_classification_actions,
)


def test_build_file_copy_upload_classification_workflow_definition_shape() -> None:
    workflow = build_file_copy_upload_classification_workflow()

    assert workflow.workflow_id == FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID
    assert workflow.initial_state == "classify"
    assert set(workflow.states.keys()) == {
        "classify",
        "persist_decision",
        "complete",
        "failed",
    }
    assert workflow.termination_states == ("complete", "failed")
    assert [a.action_id for a in workflow.states["classify"].actions] == [
        "file_copy_upload.classify"
    ]
    assert [a.action_id for a in workflow.states["persist_decision"].actions] == [
        "file_copy_upload.persist_decision"
    ]


def test_classification_selects_specialised_route_when_confident(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow._resolve_available_workflow_ids",
        lambda _payload: (DEFAULT_SCHOLARLY_WORKFLOW_ID,),
    )
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    context: dict[str, object] = {
        "file_copy_concept_id": "#V#file_copy_1",
        "original_filename": "2502.14996.pdf",
        "content_type": "application/pdf",
        "minimum_mutation_confidence": 0.8,
    }
    result = registry.execute(
        "file_copy_upload.classify",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["route_key"] == "scholarly"
    assert result.outputs["route_mode"] == "specialised"
    assert result.outputs["mutation_route"] is True
    assert result.outputs["target_workflow_available"] is True
    assert result.outputs["target_workflow_id"] == DEFAULT_SCHOLARLY_WORKFLOW_ID


def test_classification_fail_closes_low_confidence_mutation_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow._resolve_available_workflow_ids",
        lambda _payload: (DEFAULT_SCHOLARLY_WORKFLOW_ID,),
    )
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    context: dict[str, object] = {
        "file_copy_concept_id": "#V#file_copy_2",
        "original_filename": "2502.14996.pdf",
        "content_type": "application/pdf",
        "minimum_mutation_confidence": 0.95,
    }
    result = registry.execute(
        "file_copy_upload.classify",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["route_key"] == "scholarly"
    assert result.outputs["route_mode"] == "fail_closed"
    assert result.outputs["fail_closed"] is True
    assert "mutation_route_confidence_below_threshold" in result.outputs["route_reasons"]


def test_classification_marks_unsupported_specialised_route_when_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow._resolve_available_workflow_ids",
        lambda _payload: ("#V#integration_scholarly_paper_representation_workflow",),
    )
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    context: dict[str, object] = {
        "file_copy_concept_id": "#V#file_copy_unsupported_cv",
        "original_filename": "candidate-resume.pdf",
        "content_type": "application/pdf",
    }
    result = registry.execute(
        "file_copy_upload.classify",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["route_key"] == "cv"
    assert result.outputs["route_mode"] == "interpret"
    assert result.outputs["target_workflow_available"] is False
    assert result.outputs["unsupported_specialised_route"] is True
    assert result.outputs["unsupported_route_reason"] == "specialised_workflow_unavailable"
    assert "specialised_workflow_unavailable" in result.outputs["route_reasons"]


def test_persist_route_decision_writes_singleton_text_relation(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def _fake_upsert_singleton_text_relation(**kwargs):
        calls.append(dict(kwargs))
        return {"kept_relation_id": "rel-1", "replaced_count": 0}

    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow.upsert_singleton_text_relation",
        _fake_upsert_singleton_text_relation,
    )
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    context: dict[str, object] = {
        "file_copy_concept_id": "#V#file_copy_3",
        "classification_version": "file_copy_upload_classification.v1",
        "route_key": "cv",
        "route_mode": "specialised",
        "route_confidence": 0.9,
        "target_workflow_id": "#V#file_copy_cv_representation_workflow",
        "target_workflow_available": True,
        "route_reasons": ["heuristic_classifier"],
        "scored_candidates": {"cv": 0.9},
    }
    result = registry.execute(
        "file_copy_upload.persist_decision",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["route_decision_persisted"] is True
    assert result.outputs["route_decision_predicate"] == FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE
    assert len(calls) == 1
    assert calls[0]["subject_concept_id"] == "#V#file_copy_3"
    assert calls[0]["predicate"] == FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE
    persisted = json.loads(str(calls[0]["text"]))
    assert persisted["route_key"] == "cv"
    assert persisted["route_mode"] == "specialised"
