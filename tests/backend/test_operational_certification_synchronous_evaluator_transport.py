from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scripts import run_operational_certification as certification_script
from src.backend.languagemodels import llm_interface
from src.backend.services import prompt_template_service
from src.backend.workflows import trace_store
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
)
from src.backend.workflows.durable import durable_executor, registry_factory
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)


WORKFLOW_ID = "#V#operational_state_evidence_evaluator"
NAMESPACE = "org:test-org:user:test-user"
USER_ID = "#V#test_user"
ORG_ID = "#V#test_org"
PROMPT_ID = "#V#operational_certification_evaluator_prompt"


def _evaluator_result() -> dict[str, Any]:
    return {
        "schema_version": "represented_operational_evaluator_result.v1",
        "evaluator_id": WORKFLOW_ID,
        "scenario_id": "scenario-a",
        "trial_index": 1,
        "verdict": "pass",
        "terminal_state": "verified",
        "typed_terminal_outcome_available": True,
        "causal_stage_evidence_complete": True,
        "explicitly_inconclusive": False,
        "recoverable_fault_recovered": True,
        "evidence": ["sha256:observation"],
        "fabricated_evidence": [],
        "forbidden_effects": [],
        "namespace_violations": [],
        "false_success_claims": [],
        "reason": "Verified from represented evidence.",
    }


def _workflow_definition() -> tuple[WorkflowDefinition, ActionRegistry]:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="test.produce_evaluator_result",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={
                    "represented_operational_evaluator_result": _evaluator_result(),
                    "llm_step_envelope": {
                        "selected_prompt_id": PROMPT_ID,
                        "selected_model": "gpt-test",
                    },
                },
            ),
        )
    )
    definition = WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        initial_state="evaluate",
        states={
            "evaluate": WorkflowStateSpec(
                state_id="evaluate",
                actions=(
                    WorkflowActionInvocation(
                        action_id="test.produce_evaluator_result",
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="completed",
                        condition=lambda _context: True,
                    ),
                ),
            ),
            "completed": WorkflowStateSpec(
                state_id="completed",
                terminal=True,
            ),
        },
        termination_states=("completed",),
    )
    return definition, registry


def _patch_synchronous_runtime(
    monkeypatch,
    *,
    registration_source: str = "vontology",
    persist_trace: bool = True,
) -> dict[str, Any]:
    definition, action_registry = _workflow_definition()
    captures: dict[str, Any] = {}
    definition_identity = {
        "schema_version": "workflow_definition_identity.v1",
        "workflow_id": WORKFLOW_ID,
        "definition_sha256": "a" * 64,
    }

    def _build_snapshot(*, workflow_ids):
        captures["snapshot_workflow_ids"] = list(workflow_ids)
        return object()

    def _resolve(workflow_id, **kwargs):
        captures["resolution_workflow_id"] = workflow_id
        captures["resolution_kwargs"] = dict(kwargs)
        return SimpleNamespace(
            definition=definition,
            registration_source=registration_source,
            definition_identity=definition_identity,
            error_code=None,
            to_dict=lambda: {
                "success": True,
                "workflow_id": workflow_id,
                "registration_source": registration_source,
                "definition_identity": definition_identity,
            },
        )

    def _insert_trace(document):
        captures["trace_document"] = document
        return document["execution_id"] if persist_trace else None

    def _get_trace(execution_id):
        document = captures.get("trace_document")
        if not persist_trace or not isinstance(document, dict):
            return None
        assert document["execution_id"] == execution_id
        return dict(document)

    monkeypatch.setattr(
        registry_factory,
        "build_vontology_workflow_registry_snapshot",
        _build_snapshot,
    )
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        _resolve,
    )
    monkeypatch.setattr(
        registry_factory,
        "get_shared_durable_action_registry",
        lambda: action_registry,
    )
    monkeypatch.setattr(
        registry_factory,
        "_get_or_build_durable_mcp_gateway",
        lambda: object(),
    )
    monkeypatch.setattr(
        llm_interface,
        "get_active_model_name",
        lambda **_kwargs: "gpt-test",
    )

    def _get_llm_client(**kwargs):
        captures["llm_client_kwargs"] = dict(kwargs)
        return object()

    monkeypatch.setattr(llm_interface, "get_llm_client", _get_llm_client)
    monkeypatch.setattr(
        durable_executor,
        "_resolve_instance_runtime_model_context",
        lambda *, context, inputs: (
            context.get("requested_model") or None,
            "ollama" if context.get("requested_model") else None,
            {},
        ),
    )
    monkeypatch.setattr(
        llm_interface,
        "resolve_provider_from_model_concept",
        lambda _model: "openai",
    )
    monkeypatch.setattr(
        prompt_template_service.PromptTemplateService,
        "resolve_prompt_text",
        lambda _self, concept_ids, **_kwargs: (
            list(concept_ids)[0],
            "Represented evaluator prompt text.",
        ),
    )
    monkeypatch.setattr(trace_store, "insert_workflow_execution_trace", _insert_trace)
    monkeypatch.setattr(trace_store, "get_workflow_execution_trace", _get_trace)
    return captures


def _execute(
    *,
    requested_model: str | None = None,
    execution_request_id: str | None = None,
) -> dict[str, Any]:
    return certification_script._execute_represented_workflow_synchronously(
        workflow_id=WORKFLOW_ID,
        inputs={
            "scenario_contract": {"scenario_id": "scenario-a"},
            "trial_index": 1,
            "trial_observation": {"namespace": "foreign"},
            "namespace": "foreign",
            "user_id": "#V#foreign_user",
            "org_id": "#V#foreign_org",
            "user_concept_id": "#V#foreign_user",
            "org_concept_id": "#V#foreign_org",
        },
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        source_event_type="operational_certification_evaluation",
        source_event_id="campaign:scenario-a:1",
        event_idempotency_key="evaluator:digest",
        execution_metadata={
            "campaign_execution_id": "campaign",
            "scenario_id": "scenario-a",
            "trial_index": 1,
            "observation_sha256": "b" * 64,
        },
        requested_model=requested_model,
        execution_request_id=execution_request_id,
    )


def test_synchronous_evaluator_uses_live_authority_exact_scope_and_persisted_trace(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    captures = _patch_synchronous_runtime(monkeypatch)

    execution = _execute()

    assert execution["success"] is True
    assert execution["workflow_authority_source"] == "vontology"
    assert execution["trace_persisted"] is True
    assert execution["execution_trace_id"]
    assert execution["workflow_output"]["namespace"] == NAMESPACE
    assert execution["workflow_output"]["user_id"] == USER_ID
    assert execution["workflow_output"]["org_id"] == ORG_ID
    assert execution["workflow_output"]["user_concept_id"] == USER_ID
    assert execution["workflow_output"]["org_concept_id"] == ORG_ID
    assert captures["snapshot_workflow_ids"] == [WORKFLOW_ID]
    assert captures["resolution_kwargs"]["use_current_shared_registry"] is False
    assert captures["resolution_kwargs"]["actor_user_id"] == USER_ID
    assert captures["resolution_kwargs"]["actor_org_id"] == ORG_ID

    trace_document = captures["trace_document"]
    assert trace_document["status"] == "completed"
    assert trace_document["user_namespace"] == NAMESPACE
    assert trace_document["org_id"] == ORG_ID
    assert trace_document["metadata"]["exact_scope"] == execution["exact_scope"]
    assert (
        trace_document["metadata"]["exact_scope_sha256"]
        == execution["exact_scope_sha256"]
    )
    assert (
        trace_document["metadata"]["workflow_inputs_sha256"]
        == execution["workflow_inputs_sha256"]
    )
    assert trace_document["metadata"]["execution_metadata"] == {
        "campaign_execution_id": "campaign",
        "scenario_id": "scenario-a",
        "trial_index": 1,
        "observation_sha256": "b" * 64,
    }
    assert trace_document["metadata"]["workflow_definition_identity"] == (
        execution["workflow_definition_identity"]
    )

    represented_result = (
        certification_script._represented_evaluator_result_from_execution(execution)
    )
    assert represented_result is not None
    assert represented_result["verdict"] == "pass"
    assert represented_result["execution_evidence"]["execution_trace_id"] == (
        execution["execution_trace_id"]
    )
    assert represented_result["execution_evidence"]["exact_scope_sha256"] == (
        execution["exact_scope_sha256"]
    )


def test_synchronous_evaluator_binds_caller_execution_request_id_to_trace(
    monkeypatch,
) -> None:
    captures = _patch_synchronous_runtime(monkeypatch)
    execution_request_id = "represented-decision-request-1"

    execution = _execute(execution_request_id=execution_request_id)

    assert execution["success"] is True
    assert execution["caller_supplied_execution_request_id"] == execution_request_id
    assert execution["execution_trace_id"] == execution_request_id
    assert execution["attempted_execution_trace_id"] == execution_request_id
    assert captures["trace_document"]["execution_id"] == execution_request_id


def test_synchronous_evaluator_rejects_blank_caller_execution_request_id() -> None:
    execution = _execute(execution_request_id="   ")

    assert execution["success"] is False
    assert execution["error_code"] == (
        "synchronous_workflow_execution_request_id_invalid"
    )


def test_synchronous_evaluator_rejects_non_vontology_definition(monkeypatch) -> None:
    captures = _patch_synchronous_runtime(
        monkeypatch,
        registration_source="repo_seed_agent_test",
    )

    execution = _execute()

    assert execution["success"] is False
    assert execution["error_code"] == (
        "represented_workflow_not_live_vontology_authority"
    )
    assert execution["trace_persisted"] is True
    assert captures["trace_document"]["status"] == "failed"
    assert (
        certification_script._represented_evaluator_result_from_execution(execution)
        is None
    )


def test_synchronous_evaluator_applies_campaign_model_override(monkeypatch) -> None:
    captures = _patch_synchronous_runtime(monkeypatch)

    execution = _execute(requested_model="qwen3:8b")

    assert execution["success"] is True
    assert execution["workflow_output"]["requested_model"] == "qwen3:8b"
    assert captures["llm_client_kwargs"]["client_type"] == "ollama"
    assert captures["trace_document"]["metadata"]["requested_model"] == {
        "redacted": True,
        "length": len("qwen3:8b"),
        "sha256": "ef26034c2d11fff5222732b5903a72edf894e9c37673cf082a3726ac42e6c92b",
    }
    assert (
        captures["trace_document"]["metadata"]["requested_model_override_applied"]
        is True
    )


def test_synchronous_evaluator_fails_closed_when_trace_is_not_persisted(
    monkeypatch,
) -> None:
    _patch_synchronous_runtime(monkeypatch, persist_trace=False)

    execution = _execute()

    assert execution["workflow_completed"] is True
    assert execution["trace_persisted"] is False
    assert execution["success"] is False
    assert execution["error_code"] == "workflow_trace_persistence_failed"
    assert (
        certification_script._represented_evaluator_result_from_execution(execution)
        is None
    )


def test_synchronous_evaluator_fails_closed_when_trace_persistence_raises(
    monkeypatch,
) -> None:
    _patch_synchronous_runtime(monkeypatch)

    def _raise_trace_error(_document):
        raise RuntimeError("trace-store-unavailable")

    monkeypatch.setattr(
        trace_store,
        "insert_workflow_execution_trace",
        _raise_trace_error,
    )

    execution = _execute()

    assert execution["workflow_completed"] is True
    assert execution["trace_persisted"] is False
    assert execution["success"] is False
    assert execution["error_code"] == "workflow_trace_persistence_exception"
    assert "trace-store-unavailable" in execution["trace_persistence_error"]


def test_synchronous_evaluator_fails_closed_on_trace_readback_digest_mismatch(
    monkeypatch,
) -> None:
    _patch_synchronous_runtime(monkeypatch)

    def _tampered_trace(_execution_id):
        return {
            "execution_id": _execution_id,
            "workflow_id": WORKFLOW_ID,
            "status": "completed",
            "certification_trace_payload_sha256": "0" * 64,
        }

    monkeypatch.setattr(
        trace_store,
        "get_workflow_execution_trace",
        _tampered_trace,
    )

    execution = _execute()

    assert execution["trace_persisted"] is False
    assert execution["trace_readback_verified"] is False
    assert execution["success"] is False
    assert execution["error_code"] == "workflow_trace_readback_failed"


def test_evaluator_result_extraction_requires_the_terminal_output_key() -> None:
    evaluator_result = _evaluator_result()
    execution = {
        "success": True,
        "trace_persisted": True,
        "workflow_authority_source": "vontology",
        "transport": "synchronous_workflow_executor",
        "execution_trace_id": "trace-1",
        "workflow_output_sha256": "c" * 64,
        "exact_scope": {"namespace": NAMESPACE},
        "workflow_output": {"nested": evaluator_result},
    }

    assert (
        certification_script._represented_evaluator_result_from_execution(execution)
        is None
    )
