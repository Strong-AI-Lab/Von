"""Creation probes retain their actor/model and expose an actual runtime failure."""

from types import SimpleNamespace

from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable import workflow_creation_workflow as creation


def test_verification_inherits_trusted_scope_and_reports_execution_error(monkeypatch):
    spec = {
        "workflow_id": "#V#slides",
        "parent_type_id": "#V#ai_workflow",
        "required_effects": ["save source-linked analysis"],
        "verification_inputs": {
            "file_copy_concept_id": "#V#source",
            "user_concept_id": "#V#wrong_actor",
            "org_concept_id": "#V#wrong_org",
        },
    }
    monkeypatch.setattr(creation, "_normalise_workflow_spec", lambda *a, **k: spec)
    monkeypatch.setattr(creation, "discover_workflow_ids", lambda: ["#V#slides"])
    monkeypatch.setattr(
        creation, "load_workflow_definition_from_vontology", lambda _: object()
    )
    monkeypatch.setattr(creation, "_build_verification_registry", lambda _: object())
    monkeypatch.setattr(
        creation, "_supported_action_ids_for_verification", lambda **k: {"llm.action"}
    )
    monkeypatch.setattr(
        creation, "validate_workflow_definition_contract", lambda **k: {"valid": True}
    )
    monkeypatch.setattr(
        creation, "_write_workflow_publication_lifecycle", lambda **k: None
    )
    captured = {}

    class ProbeExecutor:
        def __init__(self, **kwargs):
            pass

        def run(self, definition, *, environment, data):
            captured.update(data)
            return SimpleNamespace(
                completed=False,
                final_state="read_slides",
                error="source_not_accessible",
                data={},
            )

    monkeypatch.setattr(creation, "WorkflowExecutor", ProbeExecutor)
    result = creation._handle_verify_discoverability(
        WorkflowActionRequest(
            action_id="workflow_authoring.validate_workflow_definition",
            inputs={},
            environment=WorkflowEnvironment(
                llm_client=object(),
                model="gpt-5.6-luna",
                user_concept_id="#V#owner",
                org_concept_id="#V#org",
                user_namespace="#V#owner@org",
            ),
            data={"requested_client_type": "openai", "prefer_default_model": True},
        )
    )
    assert captured["file_copy_concept_id"] == "#V#source"
    assert captured["user_concept_id"] == "#V#owner"
    assert captured["org_concept_id"] == captured["organisation_concept_id"] == "#V#org"
    assert captured["namespace"] == captured["user_namespace"] == "#V#owner@org"
    assert captured["requested_model"] == "gpt-5.6-luna"
    assert captured["requested_client_type"] == "openai"
    assert captured["prefer_default_model"] is True
    assert result.status == "failed"
    assert result.outputs["verification_run"] == {
        "completed": False,
        "final_state": "read_slides",
        "error": "source_not_accessible",
    }
