from __future__ import annotations

from types import SimpleNamespace
import pytest

from src.backend.services.workflow_file_source_service import (
    project_workflow_file_sources,
)
from src.backend.services.workflow_turn_capability_service import (
    WorkflowTurnCapability,
    build_workflow_execution_arguments,
)


@pytest.fixture(autouse=True)
def file_metadata(monkeypatch):
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service._load_file_copy_concept_doc",
        lambda **kw: {
            "concept_id": "#V#poster",
            "relationships": {"specific_to_user": ["#V#user"]},
        },
    )


def test_delayed_index_does_not_block_event_workflow_source_text(monkeypatch):
    text = "Agentic Memory Workshop\nMonday 14 September\n09:00 Opening session"
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda *a, **kw: [{"text": text}],
    )

    def must_not_run(**kwargs):
        raise AssertionError(
            "Completed extraction must not be fetched or indexed again"
        )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        must_not_run,
    )
    capability = WorkflowTurnCapability(
        name="event",
        workflow_id="#V#event",
        display_name="Event",
        description="Represent an event",
        relevance_score=1,
        input_schema={},
    )
    arguments, _ = build_workflow_execution_arguments(
        capability,
        {},
        prompt="Represent this event",
        context=[],
        conversation_situation=None,
        request_workflow_launch_inputs={"file_copy_concept_id": "#V#poster"},
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        maximum_wait_seconds=1,
    )
    inputs = arguments["inputs"]
    # The represented event consumer may use prompt alone; it receives actual
    # source content while the independent index fixture remains pending.
    assert "Agentic Memory Workshop" in inputs["prompt"]
    assert inputs["source_documents"][0]["text"] == text
    assert inputs["source_documents"][0]["source"] == "persisted_hasContent"


def test_first_read_uses_bounded_source_bytes_without_interpretation_or_indexing(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kw: {
            "success": True,
            "data": b"Workshop schedule",
            "info": SimpleNamespace(
                content_type="text/plain", original_filename="schedule.txt"
            ),
        },
    )
    result = project_workflow_file_sources(
        {"file_copy_concept_id": "#V#poster"},
        user_concept_id="#V#user",
        organisation_concept_id=None,
        namespace="#V#user",
    )
    assert result[0]["text"] == "Workshop schedule"
    assert result[0]["source"] == "file_bytes"
    assert len(result[0]["sha256"]) == 64


def test_source_failure_is_scoped_and_not_inferred_from_filename(monkeypatch):
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **kw: {"success": False, "error": "not_found"},
    )
    result = project_workflow_file_sources(
        {"file_copy_concept_id": "#V#poster"},
        user_concept_id="#V#user",
        organisation_concept_id=None,
        namespace="#V#user",
    )
    assert result == [
        {
            "file_copy_concept_id": "#V#poster",
            "status": "unavailable",
            "reason": "not_found",
        }
    ]


def test_cached_text_does_not_cross_file_actor_scope(monkeypatch):
    def must_not_read(*a, **kw):
        raise AssertionError("Foreign actor must not read cached text or bytes")

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept", must_not_read
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        must_not_read,
    )
    result = project_workflow_file_sources(
        {"file_copy_concept_id": "#V#poster"},
        user_concept_id="#V#other",
        organisation_concept_id=None,
        namespace="#V#other",
    )
    assert result[0]["status"] == "unavailable"
    assert result[0]["reason"] == "not_found"
