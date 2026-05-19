from __future__ import annotations

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.file_copy_typing_workflow import (
    FILE_COPY_TYPING_PREDICATE,
    FILE_COPY_TYPING_SCHEMA_VERSION,
    FILE_COPY_TYPING_WORKFLOW_ID,
    build_file_copy_typing_workflow_test_definition,
    register_file_copy_typing_actions,
)


def test_build_file_copy_typing_workflow_test_definition_definition_shape() -> None:
    workflow = build_file_copy_typing_workflow_test_definition()

    assert workflow.workflow_id == FILE_COPY_TYPING_WORKFLOW_ID
    assert workflow.initial_state == "infer"
    assert set(workflow.states.keys()) == {"infer", "persist", "complete", "failed"}
    assert workflow.termination_states == ("complete", "failed")
    assert [action.action_id for action in workflow.states["infer"].actions] == [
        "file_copy_typing.infer"
    ]
    assert [action.action_id for action in workflow.states["persist"].actions] == [
        "file_copy_typing.persist"
    ]


def test_register_file_copy_typing_actions_registers_expected_ids() -> None:
    registry = ActionRegistry()
    register_file_copy_typing_actions(registry)

    assert set(registry.all_action_ids()) == {
        "file_copy_typing.infer",
        "file_copy_typing.persist",
    }


def test_infer_action_emits_authoritative_typing_outputs() -> None:
    registry = ActionRegistry()
    register_file_copy_typing_actions(registry)

    result = registry.execute(
        "file_copy_typing.infer",
        inputs={
            "content_type": "application/pdf",
            "original_filename": "2502.14996.pdf",
            "size_bytes": 123456,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["typing_schema_version"] == FILE_COPY_TYPING_SCHEMA_VERSION
    assert result.outputs["typing_determinable"] is True
    assert result.outputs["route_hint"] == "arxiv"
    assert result.outputs["arxiv_id"] == "2502.14996"
    assert result.outputs["arxiv_ids"] == ["2502.14996"]
    assert result.outputs["primary_type_concept_id"] == "#V#scholarly_paper_file_copy"
    assert result.outputs["semantic_type_concept_id"] == "#V#scholarly_paper_file_copy"
    assert result.outputs["format_type_concept_id"] == "#V#pdf_computer_file_copy"
    assert "#V#pdf_computer_file_copy" in result.outputs["asserted_type_concept_ids"]


def test_persist_action_reports_missing_file_copy_concept_id() -> None:
    registry = ActionRegistry()
    register_file_copy_typing_actions(registry)

    result = registry.execute(
        "file_copy_typing.persist",
        inputs={},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["typing_persisted"] is False
    assert result.outputs["typing_persist_error"] == "missing_file_copy_concept_id"


def test_persist_action_forwards_persistence_outputs(monkeypatch) -> None:
    def _fake_persist_file_copy_typing(*, file_copy_concept_id, typing_result):
        assert file_copy_concept_id == "#V#uploaded_file_copy_123"
        assert typing_result["route_hint"] == "meeting"
        return {
            "success": True,
            "typing_persisted": True,
            "typing_predicate": FILE_COPY_TYPING_PREDICATE,
            "typing_relation_id": "rel-typing-1",
            "typing_replaced_count": 1,
            "asserted_type_concept_ids": [
                "#V#meeting_transcript_file_copy",
                "#V#plain_text_computer_file_copy",
            ],
            "structural_relations": [
                {
                    "predicate": "is_an_instance_of",
                    "target_id": "#V#meeting_transcript_file_copy",
                    "modified": True,
                }
            ],
            "relation_errors": [],
        }

    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_typing_workflow.persist_file_copy_typing",
        _fake_persist_file_copy_typing,
    )

    registry = ActionRegistry()
    register_file_copy_typing_actions(registry)
    result = registry.execute(
        "file_copy_typing.persist",
        inputs={},
        context={
            "file_copy_concept_id": "#V#uploaded_file_copy_123",
            "typing_result": {
                "schema_version": FILE_COPY_TYPING_SCHEMA_VERSION,
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
            }
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["typing_persisted"] is True
    assert result.outputs["typing_predicate"] == FILE_COPY_TYPING_PREDICATE
    assert result.outputs["typing_relation_id"] == "rel-typing-1"
    assert result.outputs["typing_replaced_count"] == 1
    assert result.outputs["route_hint"] == "meeting"
    assert result.outputs["typing_structural_relations"] == [
        {
            "predicate": "is_an_instance_of",
            "target_id": "#V#meeting_transcript_file_copy",
            "modified": True,
        }
    ]
