from __future__ import annotations

from src.backend.services.context_bundle_contracts import (
    CONTEXT_ASSEMBLE_DOSSIER_ACTION_ID,
    CONTEXT_BUILD_RECONSTRUCTED_WORKSPACE_ACTION_ID,
    CONTEXT_RESOLVE_EFFECTIVE_CONTEXT_ACTION_ID,
    CONTEXT_UPDATE_REPORT_REVISION_ACTION_ID,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.context_bundle_actions import (
    register_context_bundle_actions,
)


def test_register_context_bundle_actions_registers_expected_ids() -> None:
    registry = ActionRegistry()
    register_context_bundle_actions(registry)

    assert {
        CONTEXT_RESOLVE_EFFECTIVE_CONTEXT_ACTION_ID,
        CONTEXT_ASSEMBLE_DOSSIER_ACTION_ID,
        CONTEXT_UPDATE_REPORT_REVISION_ACTION_ID,
        CONTEXT_BUILD_RECONSTRUCTED_WORKSPACE_ACTION_ID,
    }.issubset(set(registry.all_action_ids()))


def test_context_bundle_assemble_action_forwards_inputs(monkeypatch) -> None:
    from src.backend.workflows.durable import context_bundle_actions as mod

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mod,
        "assemble_context_dossier",
        lambda **kwargs: captured.update(kwargs)
        or {"success": True, "dossier_id": "#V#dossier_1", "context_dossier": {"subject_id": kwargs["subject_id"]}},
    )

    registry = ActionRegistry()
    register_context_bundle_actions(registry)
    result = registry.execute(
        CONTEXT_ASSEMBLE_DOSSIER_ACTION_ID,
        inputs={
            "name": "Concept dossier",
            "subject_kind": "concept",
            "subject_id": "#V#graph_theorist",
        },
        context={"open_questions": ["What is the stronger parent?"]},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["dossier_id"] == "#V#dossier_1"
    assert captured["subject_id"] == "#V#graph_theorist"
    assert captured["open_questions"] == ["What is the stronger parent?"]


def test_context_bundle_build_workspace_action_reports_service_failure(monkeypatch) -> None:
    from src.backend.workflows.durable import context_bundle_actions as mod

    monkeypatch.setattr(
        mod,
        "build_reconstructed_workspace",
        lambda **kwargs: {
            "success": False,
            "error": "subject_kind_and_subject_id_required",
        },
    )

    request = WorkflowActionRequest(
        action_id=CONTEXT_BUILD_RECONSTRUCTED_WORKSPACE_ACTION_ID,
        inputs={"subject_kind": "concept"},
        data={},
        environment=WorkflowEnvironment(llm_client=None),
    )

    registry = ActionRegistry()
    register_context_bundle_actions(registry)
    spec = registry.get(CONTEXT_BUILD_RECONSTRUCTED_WORKSPACE_ACTION_ID)
    assert spec is not None

    result = spec.handler(request)

    assert result.status == "failed"
    assert result.error == "subject_kind_and_subject_id_required"
