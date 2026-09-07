from types import SimpleNamespace

import pytest

from src.backend.security.access_control import override_current_actor
from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable import task_execution_actions as actions


def _request(**inputs):
    return WorkflowActionRequest(
        action_id="task.submit_execution",
        inputs={
            "task_concept_id": "#V#responsibility",
            "model": "gpt-5.6-luna",
            "model_provider": "openai",
            **inputs,
        },
        data={"user_concept_id": "#V#untrusted", "namespace": "#V#untrusted@other"},
        environment=WorkflowEnvironment(llm_client=None),
        trace=SimpleNamespace(instance_id="durable-occurrence-1"),
    )


def test_scheduled_task_uses_trusted_actor_stable_instance_and_selected_model(
    monkeypatch,
):
    calls = []

    def submit(task_id, **kwargs):
        calls.append((task_id, kwargs))
        return {
            "queue_item": {
                "queue_id": "queue-1",
                "session_id": "chat-1",
                "task_execution_concept_id": "#V#execution_1",
                "status": "queued",
            }
        }, 201

    monkeypatch.setattr(actions, "submit_task_execution", submit)
    with override_current_actor("#V#alice", "#V#lab"):
        first = actions.submit_task_execution_action(_request())
        actions.submit_task_execution_action(_request())
    assert first.status == "success"
    assert first.outputs["domain_completion_claim"] is False
    assert calls[0] == calls[1]
    task_id, kwargs = calls[0]
    assert task_id == "#V#responsibility"
    assert kwargs["actor_context"] == {
        "actor_concept_id": "#V#alice",
        "organisation_concept_id": "#V#lab",
        "namespace": "#V#alice@lab",
    }
    assert (
        kwargs["payload"]["launch_request_id"] == "workflow-task:durable-occurrence-1"
    )
    assert kwargs["model"] == "gpt-5.6-luna"
    assert kwargs["model_provider"] == "openai"


def test_scheduled_task_rejects_actor_claims_without_ambient_authority(monkeypatch):
    monkeypatch.setattr(
        actions,
        "submit_task_execution",
        lambda *_a, **_k: pytest.fail("must not submit"),
    )
    with override_current_actor(None, None):
        result = actions.submit_task_execution_action(
            _request(user_concept_id="#V#alice", org_concept_id="#V#lab")
        )
    assert result.status == "failed"


def test_scheduled_task_requires_explicit_model_before_submission(monkeypatch):
    monkeypatch.setattr(
        actions,
        "submit_task_execution",
        lambda *_a, **_k: pytest.fail("must not submit"),
    )
    with override_current_actor("#V#alice", "#V#lab"):
        result = actions.submit_task_execution_action(_request(model=None))
    assert result.error == "task_execution_explicit_model_and_provider_required"


def test_release_bundle_uses_registered_task_submission_action():
    from src.backend.workflows.durable.registry_factory import (
        build_durable_action_registry,
    )
    from src.backend.workflows.workflow_concept_authority_service import (
        build_repo_seed_workflow_definitions,
    )

    definition = build_repo_seed_workflow_definitions(
        target_workflow_ids=["#V#task_continuation_workflow"]
    )["#V#task_continuation_workflow"]
    action_ids = [
        a.action_id for state in definition.states.values() for a in state.actions
    ]
    assert action_ids == ["task.submit_execution"]
    registry = build_durable_action_registry()
    assert registry.get("task.submit_execution") is not None
    from src.backend.workflows.workflow_definition_identity_service import (
        validate_workflow_definition_contract,
    )

    report = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=action_ids,
        enforce_supported_actions=True,
    )
    assert report["valid"], report["errors"]
