from __future__ import annotations

import json

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.file_copy_upload_classification_workflow import (
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_ROUTE_DECISION_PREDICATE,
    build_file_copy_upload_classification_workflow_test_definition,
    register_file_copy_upload_classification_actions,
)

_USE_DEFAULT_ROUTE_MAP = object()


def _seed_route_map() -> dict[str, object]:
    return {
        "schema_version": "workflow_typed_subworkflow_route_map.v1",
        "default_route_key": "interpret",
        "minimum_route_score": 0.58,
        "minimum_mutation_confidence": 0.84,
        "allow_interpret_fallback": True,
        "routes": [
            {
                "route_key": "arxiv",
                "selected_route_mode": "specialised",
                "mutation_route": True,
                "candidate_workflow_ids": ["#V#arxiv_paper_representation_workflow"],
                "on_workflow_unavailable": "interpret_if_allowed_else_noop",
                "on_low_confidence": "fail_closed",
                "unsupported_reason": "specialised_workflow_unavailable",
            },
            {
                "route_key": "scholarly",
                "selected_route_mode": "specialised",
                "mutation_route": True,
                "candidate_workflow_ids": ["#V#scholarly_paper_representation_workflow"],
                "on_workflow_unavailable": "interpret_if_allowed_else_noop",
                "on_low_confidence": "fail_closed",
                "unsupported_reason": "specialised_workflow_unavailable",
            },
            {
                "route_key": "cv",
                "selected_route_mode": "specialised",
                "mutation_route": True,
                "candidate_workflow_ids": ["#V#file_copy_cv_representation_workflow"],
                "on_workflow_unavailable": "interpret_if_allowed_else_noop",
                "on_low_confidence": "fail_closed",
                "unsupported_reason": "specialised_workflow_unavailable",
            },
            {
                "route_key": "business_card",
                "selected_route_mode": "specialised",
                "mutation_route": True,
                "candidate_workflow_ids": [
                    "#V#file_copy_business_card_representation_workflow"
                ],
                "on_workflow_unavailable": "interpret_if_allowed_else_noop",
                "on_low_confidence": "fail_closed",
                "unsupported_reason": "specialised_workflow_unavailable",
            },
            {
                "route_key": "meeting",
                "selected_route_mode": "specialised",
                "mutation_route": True,
                "candidate_workflow_ids": ["#V#file_copy_meeting_representation_workflow"],
                "on_workflow_unavailable": "interpret_if_allowed_else_noop",
                "on_low_confidence": "fail_closed",
                "unsupported_reason": "specialised_workflow_unavailable",
            },
            {
                "route_key": "spreadsheet",
                "selected_route_mode": "specialised",
                "mutation_route": True,
                "candidate_workflow_ids": [
                    "#V#spreadsheet_phd_programme_representation_workflow"
                ],
                "on_workflow_unavailable": "interpret_if_allowed_else_noop",
                "on_low_confidence": "fail_closed",
                "unsupported_reason": "specialised_workflow_unavailable",
            },
            {
                "route_key": "interpret",
                "selected_route_mode": "interpret",
                "mutation_route": False,
            },
            {
                "route_key": "noop",
                "selected_route_mode": "noop",
                "mutation_route": False,
            },
        ],
    }


def _patch_route_map(
    monkeypatch,
    route_map: dict[str, object] | None | object = _USE_DEFAULT_ROUTE_MAP,
) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow.resolve_workflow_typed_subworkflow_route_map",
        lambda _workflow_id: (
            (
                route_map
                if route_map is not _USE_DEFAULT_ROUTE_MAP
                else _seed_route_map(),
                "text_relation:#V#hasWorkflowTypedSubworkflowRouteMapJson",
            )
            if route_map is not None
            else (None, "none")
        ),
    )


def test_build_file_copy_upload_classification_workflow_test_definition_definition_shape() -> None:
    workflow = build_file_copy_upload_classification_workflow_test_definition()

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
    _patch_route_map(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow._resolve_available_workflow_ids",
        lambda _payload: ("#V#arxiv_paper_representation_workflow",),
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
    assert result.outputs["route_key"] == "arxiv"
    assert result.outputs["route_mode"] == "specialised"
    assert result.outputs["mutation_route"] is True
    assert result.outputs["target_workflow_available"] is True
    assert result.outputs["target_workflow_id"] == "#V#arxiv_paper_representation_workflow"
    assert (
        result.outputs["typing_primary_type_concept_id"]
        == "#V#scholarly_paper_file_copy"
    )
    assert (
        result.outputs["typing_format_type_concept_id"] == "#V#pdf_computer_file_copy"
    )
    assert "#V#scholarly_paper_file_copy" in result.outputs[
        "typing_asserted_type_concept_ids"
    ]


def test_classification_does_not_mutate_from_xlsx_format_alone(
    monkeypatch,
) -> None:
    _patch_route_map(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow._resolve_available_workflow_ids",
        lambda _payload: ("#V#spreadsheet_phd_programme_representation_workflow",),
    )
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    result = registry.execute(
        "file_copy_upload.classify",
        inputs={},
        context={
            "file_copy_concept_id": "#V#file_copy_sheet",
            "original_filename": "programme.xlsx",
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["route_key"] == "interpret"
    assert result.outputs["route_mode"] == "interpret"
    assert result.outputs["mutation_route"] is False
    assert result.outputs["target_workflow_id"] is None


def test_explicit_spreadsheet_route_can_select_programme_workflow(
    monkeypatch,
) -> None:
    _patch_route_map(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow._resolve_available_workflow_ids",
        lambda _payload: ("#V#spreadsheet_phd_programme_representation_workflow",),
    )
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    result = registry.execute(
        "file_copy_upload.classify",
        inputs={},
        context={
            "file_copy_concept_id": "#V#file_copy_sheet",
            "original_filename": "programme.xlsx",
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "force_route_key": "spreadsheet",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.outputs["route_key"] == "spreadsheet"
    assert result.outputs["route_mode"] == "specialised"
    assert result.outputs["mutation_route"] is True
    assert (
        result.outputs["target_workflow_id"]
        == "#V#spreadsheet_phd_programme_representation_workflow"
    )


def test_classification_fail_closes_low_confidence_mutation_route(monkeypatch) -> None:
    _patch_route_map(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow._resolve_available_workflow_ids",
        lambda _payload: ("#V#arxiv_paper_representation_workflow",),
    )
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    context: dict[str, object] = {
        "file_copy_concept_id": "#V#file_copy_2",
        "original_filename": "2502.14996.pdf",
        "content_type": "application/pdf",
        "minimum_mutation_confidence": 0.95,
        "typing_result": {
            "schema_version": "file_copy_typing.v1",
            "route_hint": "arxiv",
            "route_confidence": 0.9,
            "route_scores": {"arxiv": 0.9},
            "primary_type_concept_id": "#V#scholarly_paper_file_copy",
            "semantic_type_concept_id": "#V#scholarly_paper_file_copy",
            "format_type_concept_id": "#V#pdf_computer_file_copy",
            "asserted_type_concept_ids": [
                "#V#scholarly_paper_file_copy",
                "#V#pdf_computer_file_copy",
            ],
        },
    }
    result = registry.execute(
        "file_copy_upload.classify",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["route_key"] == "arxiv"
    assert result.outputs["route_mode"] == "fail_closed"
    assert result.outputs["fail_closed"] is True
    assert "mutation_route_confidence_below_threshold" in result.outputs["route_reasons"]


def test_classification_marks_unsupported_specialised_route_when_unavailable(
    monkeypatch,
) -> None:
    _patch_route_map(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_classification_workflow._resolve_available_workflow_ids",
        lambda _payload: ("#V#scholarly_paper_representation_workflow",),
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


def test_classification_prefers_supplied_typing_result_for_meeting_route(
    monkeypatch,
) -> None:
    _patch_route_map(monkeypatch)
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    context: dict[str, object] = {
        "file_copy_concept_id": "#V#file_copy_meeting_1",
        "original_filename": "generic-upload.bin",
        "content_type": "application/octet-stream",
        "available_workflow_ids": ["#V#file_copy_meeting_representation_workflow"],
        "typing_result": {
            "schema_version": "file_copy_typing.v1",
            "route_hint": "meeting",
            "route_confidence": 0.91,
            "route_scores": {"meeting": 0.91},
            "primary_type_concept_id": "#V#meeting_transcript_file_copy",
            "semantic_type_concept_id": "#V#meeting_transcript_file_copy",
            "format_type_concept_id": "#V#plain_text_computer_file_copy",
            "asserted_type_concept_ids": [
                "#V#meeting_transcript_file_copy",
                "#V#plain_text_computer_file_copy",
            ],
        },
    }
    result = registry.execute(
        "file_copy_upload.classify",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["route_key"] == "meeting"
    assert result.outputs["route_mode"] == "specialised"
    assert result.outputs["target_workflow_id"] == (
        "#V#file_copy_meeting_representation_workflow"
    )
    assert "typed_file_copy_context" in result.outputs["route_reasons"]
    assert result.outputs["typed_subworkflow_route_map_schema_version"] == (
        "workflow_typed_subworkflow_route_map.v1"
    )


def test_classification_noops_when_route_map_missing(monkeypatch) -> None:
    _patch_route_map(monkeypatch, route_map=None)
    registry = ActionRegistry()
    register_file_copy_upload_classification_actions(registry)

    context: dict[str, object] = {
        "file_copy_concept_id": "#V#file_copy_missing_route_map",
        "original_filename": "2502.14996.pdf",
        "content_type": "application/pdf",
    }
    result = registry.execute(
        "file_copy_upload.classify",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["route_mode"] == "noop"
    assert result.outputs["unsupported_route_reason"] == "typed_subworkflow_route_map_missing"
    assert "typed_subworkflow_route_map_missing" in result.outputs["route_reasons"]
    assert result.outputs["target_workflow_id"] is None


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
        "typing_schema_version": "file_copy_typing.v1",
        "typing_primary_type_concept_id": "#V#curriculum_vitae_file_copy",
        "typing_semantic_type_concept_id": "#V#curriculum_vitae_file_copy",
        "typing_format_type_concept_id": "#V#pdf_computer_file_copy",
        "typing_asserted_type_concept_ids": [
            "#V#curriculum_vitae_file_copy",
            "#V#pdf_computer_file_copy",
        ],
        "typed_subworkflow_route_map_source": "text_relation:#V#hasWorkflowTypedSubworkflowRouteMapJson",
        "typed_subworkflow_route_map_schema_version": "workflow_typed_subworkflow_route_map.v1",
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
    assert persisted["typing_schema_version"] == "file_copy_typing.v1"
    assert persisted["typing_primary_type_concept_id"] == "#V#curriculum_vitae_file_copy"
    assert persisted["typing_format_type_concept_id"] == "#V#pdf_computer_file_copy"
    assert (
        persisted["typed_subworkflow_route_map_source"]
        == "text_relation:#V#hasWorkflowTypedSubworkflowRouteMapJson"
    )
    assert (
        persisted["typed_subworkflow_route_map_schema_version"]
        == "workflow_typed_subworkflow_route_map.v1"
    )
