from src.backend.workflows.conversation_turn_stage_model import (
    build_conversation_turn_stage_model_snapshot,
    build_conversation_turn_stage_path,
)
from src.backend.workflows.definitions import (
    CHAT_BUTTONIFY_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)


def test_stage_model_snapshot_exposes_formal_and_non_formal_stages() -> None:
    snapshot = build_conversation_turn_stage_model_snapshot()

    assert snapshot["schema_version"] == "conversation_turn_stage_model.v1"
    assert snapshot["workflow_representation_id"] == "#V#conversation_turn_execution_workflow"
    stages = snapshot["stages"]
    assert isinstance(stages, list)
    assert any(stage.get("stage_kind") == "formal" for stage in stages)
    assert any(stage.get("stage_kind") == "non_formal" for stage in stages)
    orders = [int(stage.get("order")) for stage in stages if stage.get("order") is not None]
    assert orders == sorted(orders)


def test_stage_path_maps_known_runtime_stages_to_catalogue_entries() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=[
            "workflow_discovery",
            "tool_execute",
            "completion_gate",
            "completed",
        ],
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
    )

    assert result["schema_version"] == "conversation_turn_stage_path.v1"
    assert result["has_unmapped_runtime_stages"] is False
    path = result["path"]
    assert [entry["stage_id"] for entry in path] == [
        "workflow_discovery",
        "tool_execute",
        "completion_gate",
        "completed",
    ]
    assert all(entry["mapping_status"] == "mapped" for entry in path)


def test_stage_path_preserves_unmapped_runtime_stage_fallback() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=["workflow_discovery", "brand_new_runtime_stage"],
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
    )

    assert result["has_unmapped_runtime_stages"] is True
    assert result["unmapped_runtime_stages"] == ["brand_new_runtime_stage"]
    path = result["path"]
    assert path[-1]["mapping_status"] == "fallback_unmapped_runtime_stage"
    assert path[-1]["runtime_stage_normalised"] == "brand_new_runtime_stage"
    assert path[-1]["stage_id"] is None


def test_stage_path_maps_buttonify_runtime_stage() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=["buttonify"],
        workflow_id=CHAT_BUTTONIFY_WORKFLOW_ID,
    )

    assert result["has_unmapped_runtime_stages"] is False
    path = result["path"]
    assert len(path) == 1
    assert path[0]["stage_id"] == "buttonify"
    assert path[0]["workflow_id"] == CHAT_BUTTONIFY_WORKFLOW_ID


def test_stage_path_maps_response_finalising_runtime_stage() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=["response_finalising"],
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
    )

    assert result["has_unmapped_runtime_stages"] is False
    path = result["path"]
    assert len(path) == 1
    assert path[0]["stage_id"] == "response_finalising"
    assert path[0]["runtime_stage_normalised"] == "response_finalising"
