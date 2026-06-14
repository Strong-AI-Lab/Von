import pytest

from src.backend.workflows import conversation_turn_stage_model as stage_model
from src.backend.workflows.conversation_turn_stage_model import (
    build_conversation_turn_stage_model_snapshot,
    build_conversation_turn_stage_path,
)
from src.backend.workflows.definitions import (
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_ASSISTANT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
)


@pytest.fixture(autouse=True)
def _patch_stage_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = (
        stage_model._StageSpec(
            stage_id="workflow_discovery",
            stage_label="Workflow discovery",
            order=20,
            stage_kind="non_formal",
            boundary_type="routing",
            stage_concept_id="#V#conversation_turn_stage_workflow_discovery",
            runtime_aliases=("workflow_discovery",),
        ),
        stage_model._StageSpec(
            stage_id="context_adjudication",
            stage_label="Adjudicate prior context",
            order=21,
            stage_kind="formal",
            boundary_type="policy",
            stage_concept_id="#V#conversation_turn_stage_context_adjudication",
            workflow_id=TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
            workflow_state_id="context_adjudication_decision",
            runtime_aliases=("context_adjudication", "context_adjudication_decision"),
        ),
        stage_model._StageSpec(
            stage_id="expected_outcome_inference",
            stage_label="Infer expected outcome",
            order=22,
            stage_kind="formal",
            boundary_type="policy",
            stage_concept_id="#V#conversation_turn_stage_expected_outcome_inference",
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            workflow_state_id="expected_outcome_inference",
            runtime_aliases=("expected_outcome_inference",),
        ),
        stage_model._StageSpec(
            stage_id="workflow_dispatch_prepare",
            stage_label="Workflow dispatch preparation",
            order=25,
            stage_kind="non_formal",
            boundary_type="routing",
            stage_concept_id="#V#conversation_turn_stage_workflow_dispatch_prepare",
            runtime_aliases=("workflow_dispatch_prepare", "orchestrator_start"),
        ),
        stage_model._StageSpec(
            stage_id="selector_preparation",
            stage_label="Prepare selector context",
            order=27,
            stage_kind="formal",
            boundary_type="routing",
            stage_concept_id="#V#conversation_turn_stage_selector_preparation",
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            workflow_state_id="selector_preparation",
            runtime_aliases=("selector_preparation",),
        ),
        stage_model._StageSpec(
            stage_id="selector_decision",
            stage_label="Select workflow",
            order=29,
            stage_kind="formal",
            boundary_type="routing",
            stage_concept_id="#V#conversation_turn_stage_selector_decision",
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            workflow_state_id="selector_decision",
            runtime_aliases=("selector_decision",),
        ),
        stage_model._StageSpec(
            stage_id="workflow_dispatch",
            stage_label="Workflow dispatch",
            order=30,
            stage_kind="non_formal",
            boundary_type="routing",
            stage_concept_id="#V#conversation_turn_stage_workflow_dispatch",
            runtime_aliases=("workflow_dispatch",),
        ),
        stage_model._StageSpec(
            stage_id="tool_plan",
            stage_label="Tool-call planning",
            order=60,
            stage_kind="non_formal",
            boundary_type="planning",
            stage_concept_id="#V#conversation_turn_stage_tool_plan",
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            runtime_aliases=("tool_plan", "plan"),
        ),
        stage_model._StageSpec(
            stage_id="tool_execute",
            stage_label="Execute tool calls",
            order=70,
            stage_kind="formal",
            boundary_type="execution",
            stage_concept_id="#V#conversation_turn_stage_tool_execute",
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            workflow_state_id="execute",
            runtime_aliases=("tool_execute", "execute"),
        ),
        stage_model._StageSpec(
            stage_id="completion_gate",
            stage_label="Completion gate",
            order=100,
            stage_kind="formal",
            boundary_type="completion_gate",
            stage_concept_id="#V#conversation_turn_stage_completion_gate",
            workflow_id=TURN_COMPLETION_GATE_WORKFLOW_ID,
            workflow_state_id="completion_gate",
            runtime_aliases=("completion_gate",),
        ),
        stage_model._StageSpec(
            stage_id="recovery_tool_batch_execute",
            stage_label="Execute recovery tool batch",
            order=108,
            stage_kind="non_formal",
            boundary_type="recovery",
            stage_concept_id="#V#conversation_turn_stage_recovery_tool_batch_execute",
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            workflow_state_id="apply_recovery_tool_batch",
            runtime_aliases=(
                "apply_recovery_tool_batch",
                "recovery_tool_batch_execute",
            ),
        ),
        stage_model._StageSpec(
            stage_id="recovery_answer_prepare",
            stage_label="Prepare recovery answer",
            order=109,
            stage_kind="non_formal",
            boundary_type="recovery",
            stage_concept_id="#V#conversation_turn_stage_recovery_answer_prepare",
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            workflow_state_id="apply_recovery_answer",
            runtime_aliases=("apply_recovery_answer", "recovery_answer_prepare"),
        ),
        stage_model._StageSpec(
            stage_id="buttonify",
            stage_label="Buttonify output transformation",
            order=125,
            stage_kind="non_formal",
            boundary_type="render",
            stage_concept_id="#V#conversation_turn_stage_buttonify",
            workflow_id=CHAT_BUTTONIFY_WORKFLOW_ID,
            runtime_aliases=("buttonify",),
        ),
        stage_model._StageSpec(
            stage_id="response_finalising",
            stage_label="Finalising response",
            order=128,
            stage_kind="non_formal",
            boundary_type="postprocess",
            stage_concept_id="#V#conversation_turn_stage_response_finalising",
            runtime_aliases=("response_finalising",),
        ),
        stage_model._StageSpec(
            stage_id="completed",
            stage_label="Completed",
            order=190,
            stage_kind="formal",
            boundary_type="terminal",
            stage_concept_id="#V#conversation_turn_stage_completed",
            runtime_aliases=("completed",),
        ),
    )
    alias_index = stage_model._build_alias_index(specs)
    monkeypatch.setattr(stage_model, "_load_stage_specs", lambda: specs)
    monkeypatch.setattr(stage_model, "_load_alias_index", lambda: alias_index)


def test_stage_model_snapshot_exposes_formal_and_non_formal_stages() -> None:
    snapshot = build_conversation_turn_stage_model_snapshot()

    assert snapshot["schema_version"] == "conversation_turn_stage_model.v1"
    assert (
        snapshot["workflow_representation_id"]
        == "#V#conversation_turn_execution_workflow"
    )
    stages = snapshot["stages"]
    assert isinstance(stages, list)
    assert any(stage.get("stage_kind") == "formal" for stage in stages)
    assert any(stage.get("stage_kind") == "non_formal" for stage in stages)
    orders = [
        int(stage.get("order")) for stage in stages if stage.get("order") is not None
    ]
    assert orders == sorted(orders)


def test_stage_path_maps_known_runtime_stages_to_catalogue_entries() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=[
            "context_adjudication_decision",
            "expected_outcome_inference",
            "workflow_discovery",
            "workflow_dispatch_prepare",
            "selector_preparation",
            "selector_decision",
            "tool_plan",
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
        "context_adjudication",
        "expected_outcome_inference",
        "workflow_discovery",
        "workflow_dispatch_prepare",
        "selector_preparation",
        "selector_decision",
        "tool_plan",
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


def test_stage_path_maps_recovery_answer_runtime_stage() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=["apply_recovery_answer"],
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    )

    assert result["has_unmapped_runtime_stages"] is False
    path = result["path"]
    assert len(path) == 1
    assert path[0]["stage_id"] == "recovery_answer_prepare"
    assert path[0]["runtime_stage_normalised"] == "apply_recovery_answer"


def test_stage_path_maps_recovery_tool_batch_runtime_stage() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=["apply_recovery_tool_batch"],
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    )

    assert result["has_unmapped_runtime_stages"] is False
    path = result["path"]
    assert len(path) == 1
    assert path[0]["stage_id"] == "recovery_tool_batch_execute"
    assert path[0]["runtime_stage_normalised"] == "apply_recovery_tool_batch"


def test_stage_path_prefers_mapped_execution_workflow_over_route_hint() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=["workflow_dispatch", "tool_execute"],
        workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
    )

    assert result["workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert result["workflow_id_source"] == "mapped_stage_consensus"
    assert result["observed_workflow_ids"] == [TOOL_CALLING_WORKFLOW_ID]
    path = result["path"]
    assert len(path) == 2
    assert path[0]["stage_id"] == "workflow_dispatch"
    assert path[1]["stage_id"] == "tool_execute"
    assert path[1]["workflow_id"] == TOOL_CALLING_WORKFLOW_ID


def test_stage_path_preserves_selected_workflow_identity_when_only_auxiliary_workflow_is_observed() -> (
    None
):
    result = build_conversation_turn_stage_path(
        runtime_stages=[
            "workflow_dispatch_prepare",
            "buttonify",
            "response_finalising",
        ],
        workflow_id=CHAT_BUTTONIFY_WORKFLOW_ID,
        selected_workflow_id="#V#arxiv_paper_representation_workflow",
    )

    assert result["workflow_id"] == "#V#arxiv_paper_representation_workflow"
    assert result["workflow_id_source"] == "selected_workflow"
    assert result["selected_workflow_id"] == "#V#arxiv_paper_representation_workflow"
    assert result["observed_workflow_ids"] == [CHAT_BUTTONIFY_WORKFLOW_ID]
    assert result["auxiliary_workflow_ids"] == [CHAT_BUTTONIFY_WORKFLOW_ID]


def test_stage_path_maps_orchestrator_start_alias_to_dispatch_prepare() -> None:
    result = build_conversation_turn_stage_path(
        runtime_stages=["orchestrator_start", "workflow_dispatch"],
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
    )

    assert result["has_unmapped_runtime_stages"] is False
    path = result["path"]
    assert [entry["stage_id"] for entry in path] == [
        "workflow_dispatch_prepare",
        "workflow_dispatch",
    ]
