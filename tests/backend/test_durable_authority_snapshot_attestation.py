"""Regressions for durable exact-authority snapshot provenance.

These tests keep the exact-snapshot bridge fail closed across mixed worker
builds, caller-controlled launch context, and durable resume boundaries.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from src.backend.workflows.durable.authority_snapshot_attestation import (
    DURABLE_AUTHORITY_CHECKPOINT_ATTESTATION_SCHEMA_VERSION,
    DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY,
    DURABLE_WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION,
    EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY,
    PROMPT_CONTEXT_DIAGNOSTICS_KEY,
    WORKFLOW_AUTHORITY_OUTPUT_KEY,
    authority_payload_sha256,
    build_authority_checkpoint_attestation,
    validate_authority_checkpoint_attestation,
    worker_claim_supports_exact_authority_snapshot,
)
from src.backend.workflows.durable.durable_executor import (
    DurableWorkflowExecutor,
    _definition_exact_snapshot_dependency_ineligibility_reasons,
)
from src.backend.workflows.durable.instance_manager import WorkflowInstanceManager
from src.backend.workflows.durable.models import (
    WorkflowInstance,
    WorkflowInstanceStatus,
)
from src.backend.workflows.durable.worker_identity import (
    WORKER_BUILD_IDENTITY_SCHEMA_VERSION,
    build_claim_provenance,
    build_worker_build_identity,
    worker_build_match_tokens,
    worker_satisfies_required_build,
)
from src.backend.workflows.durable import workflow_instance_submission_service
from src.backend.workflows.durable.workflow_instance_submission_service import (
    WorkflowRunnableVerification,
    submit_verified_workflow_instance,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
    build_transition_condition,
)
from src.backend.workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
)


WORKFLOW_ID = "#V#authority_snapshot_attestation_test_workflow"
WORKER_ID = "worker-authority-attestation-test"
CLAIM_TOKEN = "claim-authority-attestation-test"


@pytest.fixture(autouse=True)
def _stub_durable_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        lambda **_kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda **_kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
        lambda _trace: "trace-authority-attestation-test",
    )
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step",
        lambda _request: WorkflowActionResult(outputs={}),
    )


def _claim_provenance(
    *,
    worker_id: str = WORKER_ID,
    include_capability: bool = True,
) -> dict[str, Any]:
    capabilities = (
        [EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY] if include_capability else []
    )
    return build_claim_provenance(
        {
            "version": "v20260715_1200_backend+gabcdef123456",
            "git_commit": "abcdef1234567890abcdef1234567890abcdef12",
            "git_short_commit": "abcdef123456",
            "capabilities": capabilities,
        },
        worker_id=worker_id,
    )


def _terminal_definition(
    *,
    revision: str = "A",
    actions: tuple[WorkflowActionInvocation, ...] | None = None,
) -> WorkflowDefinition:
    if actions is None:
        actions = (
            WorkflowActionInvocation(
                action_id="llm.action",
                execution_mode="llm",
                llm_policy={"tool_mode": "none"},
            ),
        )
    return WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=actions,
                terminal=True,
            )
        },
        termination_states=("done",),
        metadata={"test_definition_revision": revision},
    )


def _definition_identity(definition: WorkflowDefinition) -> dict[str, Any]:
    return build_workflow_definition_identity(
        workflow_id=definition.workflow_id,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )


def _running_instance(
    *,
    inputs: dict[str, Any] | None = None,
) -> WorkflowInstance:
    return WorkflowInstance(
        instance_id="authority-attestation-instance",
        workflow_id=WORKFLOW_ID,
        user_id="#V#test_user",
        org_id="#V#test_org",
        namespace="#V#test_user@test_org",
        status=WorkflowInstanceStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        locked_by=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        claimed_by_build=_claim_provenance(),
        inputs=dict(inputs or {}),
    )


def _executor_manager(instance: WorkflowInstance) -> MagicMock:
    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True
    return manager


def _terminal_checkpoint(manager: MagicMock) -> tuple[dict[str, Any], dict[str, Any]]:
    checkpoint_call = manager.checkpoint.call_args_list[-1]
    workflow_data = checkpoint_call.kwargs["workflow_data"]
    attestation = checkpoint_call.kwargs["authority_checkpoint_attestation"]
    assert isinstance(workflow_data, dict)
    assert isinstance(attestation, dict)
    return workflow_data, attestation


def test_worker_identity_builds_strict_capability_bearing_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.worker_identity.get_runtime_code_version_info",
        lambda: {
            "version": "v20260715_1200_backend+gabcdef123456",
            "version_base": "v20260715_1200_backend",
            "git_commit": "abcdef1234567890abcdef1234567890abcdef12",
            "git_short_commit": "abcdef123456",
        },
    )

    identity = build_worker_build_identity(WORKER_ID)
    claim = build_claim_provenance(identity, worker_id=WORKER_ID)

    assert identity["schema_version"] == WORKER_BUILD_IDENTITY_SCHEMA_VERSION
    assert identity["worker_id"] == WORKER_ID
    assert identity["capabilities"] == [EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY]
    assert claim["schema_version"] == (DURABLE_WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION)
    assert worker_claim_supports_exact_authority_snapshot(
        claim,
        expected_worker_id=WORKER_ID,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda claim: claim.pop("schema_version"),
        lambda claim: claim.__setitem__("schema_version", "legacy-claim.v0"),
        lambda claim: claim.pop("worker_id"),
        lambda claim: claim.__setitem__("worker_id", "worker-other"),
        lambda claim: claim.pop("capabilities"),
        lambda claim: claim.__setitem__(
            "capabilities",
            EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY,
        ),
        lambda claim: claim.__setitem__("capabilities", ["different-capability"]),
        lambda claim: [
            claim.pop(field, None)
            for field in (
                "version",
                "version_base",
                "legacy_app_version",
                "git_commit",
                "git_short_commit",
            )
        ],
    ],
)
def test_worker_claim_rejects_missing_schema_build_or_capability(
    mutation,
) -> None:
    claim = _claim_provenance()
    mutation(claim)

    assert not worker_claim_supports_exact_authority_snapshot(
        claim,
        expected_worker_id=WORKER_ID,
    )


def test_exact_snapshot_capability_is_a_worker_build_match_and_admission_token() -> (
    None
):
    identity = {
        "schema_version": WORKER_BUILD_IDENTITY_SCHEMA_VERSION,
        "worker_id": WORKER_ID,
        "version": "v20260715_1200_backend+gabcdef123456",
        "git_short_commit": "abcdef123456",
        "capabilities": [EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY],
    }

    tokens = worker_build_match_tokens(identity)

    assert EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY in tokens
    assert "abcdef1" in tokens
    assert "gabcdef1" in tokens
    assert worker_satisfies_required_build(
        identity,
        EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY,
    )
    assert not worker_satisfies_required_build(
        {**identity, "capabilities": []},
        EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY,
    )


def test_checkpoint_attestation_binds_payload_claim_token_worker_and_definition() -> (
    None
):
    workflow_data = {
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: {
            "definition_hash": "a" * 64,
        },
        WORKFLOW_AUTHORITY_OUTPUT_KEY: {
            "schema_version": "represented_output.v1",
            "candidate_id": "candidate-1",
        },
        PROMPT_CONTEXT_DIAGNOSTICS_KEY: {
            "prompt_content_sha256": "b" * 64,
        },
    }
    attestation = build_authority_checkpoint_attestation(
        instance_id="instance-1",
        workflow_id=WORKFLOW_ID,
        current_state="done",
        step_index=2,
        claim_token=CLAIM_TOKEN,
        worker_id=WORKER_ID,
        workflow_data=workflow_data,
        exact_snapshot_eligible=True,
    )

    assert attestation is not None
    assert attestation["schema_version"] == (
        DURABLE_AUTHORITY_CHECKPOINT_ATTESTATION_SCHEMA_VERSION
    )
    assert attestation["authority_payload_sha256"] == authority_payload_sha256(
        workflow_data
    )
    verified, error = validate_authority_checkpoint_attestation(
        attestation,
        instance_id="instance-1",
        workflow_id=WORKFLOW_ID,
        current_state="done",
        step_index=2,
        workflow_data=workflow_data,
        expected_claim_token=CLAIM_TOKEN,
        expected_worker_id=WORKER_ID,
        require_exact_eligible=True,
    )
    assert verified == attestation
    assert error is None

    for expected_claim_token, expected_worker_id in (
        ("different-claim", WORKER_ID),
        (CLAIM_TOKEN, "different-worker"),
    ):
        rejected, rejection = validate_authority_checkpoint_attestation(
            attestation,
            instance_id="instance-1",
            workflow_id=WORKFLOW_ID,
            current_state="done",
            step_index=2,
            workflow_data=workflow_data,
            expected_claim_token=expected_claim_token,
            expected_worker_id=expected_worker_id,
        )
        assert rejected is None
        assert rejection == "authority_checkpoint_attestation_binding_mismatch"

    mutated_data = copy.deepcopy(workflow_data)
    mutated_data[WORKFLOW_AUTHORITY_OUTPUT_KEY]["candidate_id"] = "candidate-2"
    rejected, rejection = validate_authority_checkpoint_attestation(
        attestation,
        instance_id="instance-1",
        workflow_id=WORKFLOW_ID,
        current_state="done",
        step_index=2,
        workflow_data=mutated_data,
    )
    assert rejected is None
    assert rejection == "authority_checkpoint_attestation_payload_mismatch"


def test_ineligible_attestation_is_preserved_for_diagnosis_but_not_exact_use() -> None:
    workflow_data = {
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: {
            "definition_hash": "a" * 64,
        },
    }
    attestation = build_authority_checkpoint_attestation(
        instance_id="instance-1",
        workflow_id=WORKFLOW_ID,
        current_state="done",
        step_index=1,
        claim_token=CLAIM_TOKEN,
        worker_id=WORKER_ID,
        workflow_data=workflow_data,
        exact_snapshot_eligible=True,
        ineligibility_reasons=["worker_exact_snapshot_capability_missing"],
    )
    assert attestation is not None
    assert attestation["exact_snapshot_eligible"] is False

    diagnostic, error = validate_authority_checkpoint_attestation(
        attestation,
        instance_id="instance-1",
        workflow_id=WORKFLOW_ID,
        current_state="done",
        step_index=1,
        workflow_data=workflow_data,
    )
    assert diagnostic == attestation
    assert error is None

    exact, error = validate_authority_checkpoint_attestation(
        attestation,
        instance_id="instance-1",
        workflow_id=WORKFLOW_ID,
        current_state="done",
        step_index=1,
        workflow_data=workflow_data,
        require_exact_eligible=True,
    )
    assert exact is None
    assert error == "authority_checkpoint_attestation_ineligible"


def test_manager_atomically_requires_capability_bearing_claim_for_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_data = {
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: {
            "definition_hash": "a" * 64,
        },
        WORKFLOW_AUTHORITY_OUTPUT_KEY: {"candidate_id": "candidate-1"},
    }
    attestation = build_authority_checkpoint_attestation(
        instance_id="instance-1",
        workflow_id=WORKFLOW_ID,
        current_state="done",
        step_index=1,
        claim_token=CLAIM_TOKEN,
        worker_id=WORKER_ID,
        workflow_data=workflow_data,
        exact_snapshot_eligible=True,
    )
    assert attestation is not None

    manager = object.__new__(WorkflowInstanceManager)
    manager._lock_ttl = 60
    update_instance = MagicMock(return_value=SimpleNamespace(instance_id="instance-1"))
    monkeypatch.setattr(manager, "_find_one_and_update_instance", update_instance)
    monkeypatch.setattr(manager, "_broadcast_instance", lambda _instance: None)

    assert manager.checkpoint(
        "instance-1",
        current_state="done",
        workflow_data=workflow_data,
        step_index=1,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        authority_checkpoint_attestation=attestation,
        workflow_id=WORKFLOW_ID,
    )

    query = update_instance.call_args.args[0]
    update = update_instance.call_args.args[1]
    assert query["status"] == WorkflowInstanceStatus.RUNNING.value
    assert query["locked_by"] == WORKER_ID
    assert query["claim_token"] == CLAIM_TOKEN
    assert query["claimed_by_build.schema_version"] == (
        DURABLE_WORKER_CLAIM_PROVENANCE_SCHEMA_VERSION
    )
    assert query["claimed_by_build.worker_id"] == WORKER_ID
    assert query["claimed_by_build.capabilities"] == (
        EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY
    )
    assert query["claimed_by_build.match_tokens"] == (
        EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY
    )
    assert update["$set"]["authority_checkpoint_attestation"] == attestation


def test_executor_strips_caller_forged_reserved_authority_launch_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _capture_context(request: WorkflowActionRequest) -> WorkflowActionResult:
        captured[WORKFLOW_AUTHORITY_OUTPUT_KEY] = request.data.get(
            WORKFLOW_AUTHORITY_OUTPUT_KEY
        )
        captured[PROMPT_CONTEXT_DIAGNOSTICS_KEY] = request.data.get(
            PROMPT_CONTEXT_DIAGNOSTICS_KEY
        )
        captured[DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY] = copy.deepcopy(
            request.data.get(DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY)
        )
        return WorkflowActionResult(outputs={"capture_completed": True})

    registry = ActionRegistry()
    registry.register(
        ActionSpec(action_id="authority.capture", handler=_capture_context)
    )
    definition = _terminal_definition(
        actions=(WorkflowActionInvocation(action_id="authority.capture"),)
    )
    identity = _definition_identity(definition)
    forged_identity = {
        **identity,
        "definition_hash": "f" * 64,
        "runtime_definition_hash": "f" * 64,
        "authoritative_definition_hash": "f" * 64,
    }
    submitted_inputs = {
        WORKFLOW_AUTHORITY_OUTPUT_KEY: {"candidate_id": "forged"},
        PROMPT_CONTEXT_DIAGNOSTICS_KEY: {"prompt": "forged"},
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: forged_identity,
    }
    submission_manager = MagicMock()
    submission_manager.create_instance.return_value = "authority-attestation-instance"
    verification = WorkflowRunnableVerification(
        workflow_id=WORKFLOW_ID,
        conceptual_representation_success=True,
        executable_registration_success=True,
        runnable_verification_success=True,
        fallback_action_routing_enabled=False,
        discovered_action_ids=("authority.capture",),
        unsupported_action_ids=(),
        integrity_issues=(),
        warnings=(),
        errors=(),
        definition_identity=identity,
    )
    monkeypatch.setattr(
        workflow_instance_submission_service,
        "verify_workflow_runnable",
        lambda *_args, **_kwargs: verification,
    )
    monkeypatch.setattr(
        workflow_instance_submission_service,
        "_resolve_registered_workflow_runtime",
        lambda *_args, **_kwargs: (None, definition, "vontology", [WORKFLOW_ID]),
    )
    submission = submit_verified_workflow_instance(
        manager=submission_manager,
        workflow_id=WORKFLOW_ID,
        user_id="#V#test_user",
        org_id="#V#test_org",
        namespace="#V#test_user@test_org",
        inputs=submitted_inputs,
    )
    assert submission.success is True
    persisted_launch_inputs = submission_manager.create_instance.call_args.kwargs[
        "inputs"
    ]
    assert persisted_launch_inputs[WORKFLOW_AUTHORITY_OUTPUT_KEY] == {
        "candidate_id": "forged"
    }
    assert persisted_launch_inputs[PROMPT_CONTEXT_DIAGNOSTICS_KEY] == {
        "prompt": "forged"
    }

    instance = _running_instance(inputs=persisted_launch_inputs)
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=registry,
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=identity,
    )

    assert result.completed is True
    assert captured == {
        WORKFLOW_AUTHORITY_OUTPUT_KEY: None,
        PROMPT_CONTEXT_DIAGNOSTICS_KEY: None,
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: identity,
    }
    assert WORKFLOW_AUTHORITY_OUTPUT_KEY not in result.data
    assert PROMPT_CONTEXT_DIAGNOSTICS_KEY not in result.data
    assert result.data[DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY] == identity
    workflow_data, attestation = _terminal_checkpoint(manager)
    assert WORKFLOW_AUTHORITY_OUTPUT_KEY not in workflow_data
    assert PROMPT_CONTEXT_DIAGNOSTICS_KEY not in workflow_data
    assert attestation["exact_snapshot_eligible"] is False
    assert attestation["ineligibility_reasons"] == [
        "unbound_action_execution_surface"
    ]


def test_definition_drift_a_to_b_to_a_remains_exact_snapshot_ineligible() -> None:
    definition_a = _terminal_definition(revision="A")
    definition_b = _terminal_definition(revision="B")
    identity_a = _definition_identity(definition_a)
    identity_b = _definition_identity(definition_b)
    assert identity_a["definition_hash"] != identity_b["definition_hash"]

    instance = _running_instance()
    manager = _executor_manager(instance)
    executor = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    )

    first = executor.run_durable(
        instance.instance_id,
        definition_a,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=identity_a,
    )
    assert first.completed is True
    workflow_data, attestation = _terminal_checkpoint(manager)
    assert attestation["exact_snapshot_eligible"] is True

    for definition, identity in (
        (definition_b, identity_b),
        (definition_a, identity_a),
    ):
        checkpoint_call = manager.checkpoint.call_args_list[-1]
        instance.workflow_data = copy.deepcopy(workflow_data)
        instance.authority_checkpoint_attestation = copy.deepcopy(attestation)
        instance.current_state = checkpoint_call.kwargs["current_state"]
        instance.step_index = checkpoint_call.kwargs["step_index"]
        manager.reset_mock()
        manager.get_instance.return_value = instance
        manager.is_cancelled.return_value = False
        manager.extend_lock.return_value = True
        manager.checkpoint.return_value = True

        resumed = executor.run_durable(
            instance.instance_id,
            definition,
            worker_id=WORKER_ID,
            claim_token=CLAIM_TOKEN,
            resume_from_checkpoint=True,
            workflow_definition_identity=identity,
        )
        assert resumed.completed is True
        workflow_data, attestation = _terminal_checkpoint(manager)
        assert attestation["exact_snapshot_eligible"] is False
        assert (
            "workflow_definition_drift_on_resume"
            in attestation["ineligibility_reasons"]
        )


def test_authority_output_cannot_be_reattested_by_a_successor_claim() -> None:
    """A successor must not adopt predecessor-produced authority as its own."""
    definition = _terminal_definition()
    identity = _definition_identity(definition)
    instance = _running_instance()
    workflow_data = {
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: identity,
        WORKFLOW_AUTHORITY_OUTPUT_KEY: {
            "schema_version": "represented_output.v1",
            "candidate_id": "candidate-cross-claim",
        },
        PROMPT_CONTEXT_DIAGNOSTICS_KEY: {
            "prompt_content_sha256": "b" * 64,
        },
    }
    attestation = build_authority_checkpoint_attestation(
        instance_id=instance.instance_id,
        workflow_id=WORKFLOW_ID,
        current_state="done",
        step_index=1,
        claim_token=CLAIM_TOKEN,
        worker_id=WORKER_ID,
        workflow_data=workflow_data,
        exact_snapshot_eligible=True,
    )
    assert attestation is not None
    instance.workflow_data = copy.deepcopy(workflow_data)
    instance.authority_checkpoint_attestation = copy.deepcopy(attestation)
    instance.current_state = "done"
    instance.step_index = 1
    manager = _executor_manager(instance)
    executor = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    )

    for successor_worker, successor_token in (
        ("worker-successor", "claim-successor"),
        (WORKER_ID, "claim-returned-to-original-worker"),
    ):
        instance.workflow_data = copy.deepcopy(workflow_data)
        instance.authority_checkpoint_attestation = copy.deepcopy(attestation)
        instance.current_state = str(attestation["current_state"])
        instance.step_index = int(attestation["step_index"])
        instance.locked_by = successor_worker
        instance.claim_token = successor_token
        instance.claimed_by_build = _claim_provenance(worker_id=successor_worker)
        manager.reset_mock()
        manager.get_instance.return_value = instance
        manager.is_cancelled.return_value = False
        manager.extend_lock.return_value = True
        manager.checkpoint.return_value = True

        resumed = executor.run_durable(
            instance.instance_id,
            definition,
            worker_id=successor_worker,
            claim_token=successor_token,
            resume_from_checkpoint=True,
            workflow_definition_identity=identity,
        )
        assert resumed.completed is True
        workflow_data, attestation = _terminal_checkpoint(manager)
        assert attestation["exact_snapshot_eligible"] is False
        assert (
            "authority_producer_claim_changed_on_resume"
            in attestation["ineligibility_reasons"]
        )
        assert (
            "authority_output_resumed_without_producer_lineage"
            in attestation["ineligibility_reasons"]
        )


@pytest.mark.parametrize(
    ("worker_id", "claim_token"),
    ((WORKER_ID, None), (None, CLAIM_TOKEN)),
)
def test_executor_rejects_asymmetric_claim_fence(
    worker_id: str | None,
    claim_token: str | None,
) -> None:
    definition = _terminal_definition()
    instance = _running_instance()
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=worker_id,
        claim_token=claim_token,
        resume_from_checkpoint=False,
        workflow_definition_identity=_definition_identity(definition),
    )

    assert result.completed is False
    assert result.error == "durable_lock_lost"
    manager.checkpoint.assert_not_called()


def test_direct_terminal_write_cannot_bypass_an_attested_worker_claim() -> None:
    """A valid checkpoint remains worker-fenced through terminal persistence."""
    manager = WorkflowInstanceManager()
    workflow_id = f"{WORKFLOW_ID}_{uuid.uuid4()}"
    instance_id = manager.create_instance(
        workflow_id,
        user_id="#V#test_user",
        org_id="#V#test_org",
        namespace="#V#test_user@test_org",
    )
    claim = manager.find_and_claim_instance(
        WORKER_ID,
        workflow_ids=[workflow_id],
        worker_build_identity={
            "version": "v20260715_1200_backend+gabcdef123456",
            "git_short_commit": "abcdef123456",
            "capabilities": [EXACT_AUTHORITY_SNAPSHOT_WORKER_CAPABILITY],
        },
    )
    assert claim is not None
    assert claim.claim_token
    workflow_data = {
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: {
            "definition_hash": "a" * 64,
        },
        WORKFLOW_AUTHORITY_OUTPUT_KEY: {"candidate_id": "candidate-fenced"},
    }
    attestation = build_authority_checkpoint_attestation(
        instance_id=instance_id,
        workflow_id=workflow_id,
        current_state="done",
        step_index=1,
        claim_token=claim.claim_token,
        worker_id=WORKER_ID,
        workflow_data=workflow_data,
        exact_snapshot_eligible=True,
    )
    assert attestation is not None
    assert manager.checkpoint(
        instance_id,
        current_state="done",
        workflow_data=workflow_data,
        step_index=1,
        worker_id=WORKER_ID,
        claim_token=claim.claim_token,
        authority_checkpoint_attestation=attestation,
        workflow_id=workflow_id,
    )

    assert manager.mark_completed(instance_id, outputs={"writer": "direct"}) is False
    still_running = manager.get_instance(instance_id)
    assert still_running is not None
    assert still_running.status == WorkflowInstanceStatus.RUNNING
    assert still_running.outputs is None

    assert manager.mark_completed(
        instance_id,
        outputs={"writer": "worker"},
        final_state="done",
        worker_id=WORKER_ID,
        claim_token=claim.claim_token,
    )
    completed = manager.get_instance(instance_id)
    assert completed is not None
    assert completed.status == WorkflowInstanceStatus.COMPLETED
    assert completed.outputs == {"writer": "worker"}
    assert completed.claim_token == claim.claim_token
    assert completed.authority_checkpoint_attestation == attestation


def test_unbound_subworkflow_definition_is_exact_snapshot_ineligible() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="subworkflow.invoke",
            handler=lambda _request: WorkflowActionResult(outputs={}),
        )
    )
    definition = _terminal_definition(
        actions=(
            WorkflowActionInvocation(
                action_id="subworkflow.invoke",
                execution_mode="subworkflow",
                subworkflow_id="#V#unattested_child_workflow",
            ),
        )
    )
    identity = _definition_identity(definition)
    instance = _running_instance()
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=registry,
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=identity,
    )

    assert result.completed is True
    workflow_data, attestation = _terminal_checkpoint(manager)
    assert attestation["exact_snapshot_eligible"] is False
    assert attestation["ineligibility_reasons"] == ["unbound_child_execution_surface"]
    exact, error = validate_authority_checkpoint_attestation(
        attestation,
        instance_id=instance.instance_id,
        workflow_id=definition.workflow_id,
        current_state="done",
        step_index=1,
        workflow_data=workflow_data,
        expected_claim_token=CLAIM_TOKEN,
        expected_worker_id=WORKER_ID,
        require_exact_eligible=True,
    )
    assert exact is None
    assert error == "authority_checkpoint_attestation_ineligible"


@pytest.mark.parametrize(
    ("action_id", "execution_mode", "expected_reason"),
    (
        (
            "workflow_mcp.invoke_tool",
            "deterministic",
            "unbound_action_execution_surface",
        ),
        (
            "custom.dynamic_registry_action",
            "deterministic",
            "unbound_action_execution_surface",
        ),
        (
            "llm.action",
            "llm",
            "unbound_tool_execution_surface",
        ),
    ),
)
def test_unbound_tool_and_dynamic_actions_are_exact_snapshot_ineligible(
    action_id: str,
    execution_mode: str,
    expected_reason: str,
) -> None:
    llm_policy = {"tool_mode": "auto"} if execution_mode == "llm" else None
    definition = _terminal_definition(
        actions=(
            WorkflowActionInvocation(
                action_id=action_id,
                execution_mode=execution_mode,
                llm_policy=llm_policy,
            ),
        )
    )

    assert _definition_exact_snapshot_dependency_ineligibility_reasons(
        definition
    ) == {expected_reason}


def test_workflow_mcp_action_cannot_produce_an_exact_authority_snapshot() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="workflow_mcp.invoke_tool",
            handler=lambda _request: WorkflowActionResult(
                outputs={WORKFLOW_AUTHORITY_OUTPUT_KEY: {"status": "tool_authored"}}
            ),
        )
    )
    definition = _terminal_definition(
        actions=(
            WorkflowActionInvocation(action_id="workflow_mcp.invoke_tool"),
        )
    )
    identity = _definition_identity(definition)
    instance = _running_instance()
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=registry,
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=identity,
    )

    assert result.completed is True
    workflow_data, attestation = _terminal_checkpoint(manager)
    assert workflow_data[WORKFLOW_AUTHORITY_OUTPUT_KEY] == {
        "status": "tool_authored"
    }
    assert attestation["exact_snapshot_eligible"] is False
    assert attestation["ineligibility_reasons"] == [
        "authority_producer_invocation_missing",
        "unbound_action_execution_surface"
    ]


def test_self_contained_llm_action_with_tool_mode_none_remains_exact_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step",
        lambda _request: WorkflowActionResult(
            outputs={
                WORKFLOW_AUTHORITY_OUTPUT_KEY: {"status": "authored"},
                PROMPT_CONTEXT_DIAGNOSTICS_KEY: {
                    "prompt_content_sha256": "a" * 64
                },
            }
        ),
    )
    definition = _terminal_definition(
        actions=(
            WorkflowActionInvocation(
                action_id="llm.action",
                execution_mode="llm",
                llm_policy={"tool_mode": "none"},
            ),
        )
    )
    assert not _definition_exact_snapshot_dependency_ineligibility_reasons(definition)
    identity = _definition_identity(definition)
    instance = _running_instance()
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=identity,
    )

    assert result.completed is True
    workflow_data, attestation = _terminal_checkpoint(manager)
    assert workflow_data[WORKFLOW_AUTHORITY_OUTPUT_KEY] == {"status": "authored"}
    assert attestation["exact_snapshot_eligible"] is True
    assert attestation["ineligibility_reasons"] == []


def test_output_mapping_cannot_overwrite_executor_prompt_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legitimate_diagnostics = {"prompt_content_sha256": "a" * 64}
    forged_diagnostics = {"prompt_content_sha256": "f" * 64}
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step",
        lambda _request: WorkflowActionResult(
            outputs={
                "validated_json": {
                    "candidate_id": "candidate-mapped",
                    "forged_diagnostics": forged_diagnostics,
                },
                PROMPT_CONTEXT_DIAGNOSTICS_KEY: legitimate_diagnostics,
            }
        ),
    )
    definition = WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        execution_mode="llm",
                        llm_policy={"tool_mode": "none"},
                    ),
                ),
                terminal=True,
                metadata={
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "validated_json",
                            "context_key": WORKFLOW_AUTHORITY_OUTPUT_KEY,
                        },
                        {
                            "tool_output_field": (
                                "validated_json.forged_diagnostics"
                            ),
                            "context_key": PROMPT_CONTEXT_DIAGNOSTICS_KEY,
                        },
                    ]
                },
            )
        },
        termination_states=("done",),
    )
    assert "authority_lineage_mapping_override" in (
        _definition_exact_snapshot_dependency_ineligibility_reasons(definition)
    )
    instance = _running_instance()
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=_definition_identity(definition),
    )

    assert result.completed is True
    workflow_data, attestation = _terminal_checkpoint(manager)
    assert workflow_data[WORKFLOW_AUTHORITY_OUTPUT_KEY]["candidate_id"] == (
        "candidate-mapped"
    )
    assert workflow_data[PROMPT_CONTEXT_DIAGNOSTICS_KEY] == legitimate_diagnostics
    assert attestation["exact_snapshot_eligible"] is False
    assert "authority_lineage_mapping_override" in attestation[
        "ineligibility_reasons"
    ]


def test_lossy_authority_projection_is_ineligible_in_attestation_and_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_traces: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step",
        lambda _request: WorkflowActionResult(
            outputs={
                WORKFLOW_AUTHORITY_OUTPUT_KEY: {
                    "status": "authored",
                    "oversized_evidence": "x" * (300 * 1024),
                },
                PROMPT_CONTEXT_DIAGNOSTICS_KEY: {
                    "prompt_content_sha256": "a" * 64
                },
            }
        ),
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
        lambda trace: persisted_traces.append(copy.deepcopy(trace))
        or "trace-lossy-authority-projection",
    )
    definition = _terminal_definition(
        actions=(
            WorkflowActionInvocation(
                action_id="llm.action",
                execution_mode="llm",
                llm_policy={"tool_mode": "none"},
            ),
        )
    )
    identity = _definition_identity(definition)
    instance = _running_instance()
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=identity,
    )

    assert result.completed is True
    workflow_data, attestation = _terminal_checkpoint(manager)
    projection = workflow_data["workflow_checkpoint_context_projection"]
    assert any(
        record["key"] == WORKFLOW_AUTHORITY_OUTPUT_KEY
        for record in projection["projected_keys"]
    )
    assert attestation["exact_snapshot_eligible"] is False
    assert "authority_checkpoint_projection_lossy" in attestation[
        "ineligibility_reasons"
    ]
    assert len(persisted_traces) == 1
    trace_metadata = persisted_traces[0]["metadata"]
    assert trace_metadata["exact_authority_snapshot_eligible"] is False
    assert "authority_checkpoint_projection_lossy" in trace_metadata[
        "exact_authority_snapshot_ineligibility_reasons"
    ]


def test_two_self_contained_llm_actions_have_ambiguous_authority_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_count = 0

    def _produce_authority(_request: WorkflowActionRequest) -> WorkflowActionResult:
        nonlocal invocation_count
        invocation_count += 1
        return WorkflowActionResult(
            outputs={
                WORKFLOW_AUTHORITY_OUTPUT_KEY: {
                    "status": "authored",
                    "producer_sequence": invocation_count,
                },
                PROMPT_CONTEXT_DIAGNOSTICS_KEY: {
                    "prompt_content_sha256": f"{invocation_count:x}" * 64,
                },
            }
        )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step",
        _produce_authority,
    )
    llm_action = WorkflowActionInvocation(
        action_id="llm.action",
        execution_mode="llm",
        llm_policy={"tool_mode": "none"},
    )
    definition = _terminal_definition(actions=(llm_action, llm_action))
    assert _definition_exact_snapshot_dependency_ineligibility_reasons(
        definition
    ) == {"ambiguous_authority_output_producer"}
    identity = _definition_identity(definition)
    instance = _running_instance()
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=identity,
    )

    assert result.completed is True
    assert invocation_count == 2
    workflow_data, attestation = _terminal_checkpoint(manager)
    assert workflow_data[WORKFLOW_AUTHORITY_OUTPUT_KEY]["producer_sequence"] == 2
    assert attestation["exact_snapshot_eligible"] is False
    assert attestation["ineligibility_reasons"] == [
        "ambiguous_authority_output_producer",
        "ambiguous_authority_output_producer_invocations",
    ]


def test_definition_without_an_authority_producer_is_not_exact_eligible() -> None:
    definition = _terminal_definition(actions=())

    assert _definition_exact_snapshot_dependency_ineligibility_reasons(
        definition
    ) == {"authority_output_producer_action_missing"}


def test_caller_idempotency_cache_can_never_be_exact_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_count = 0

    def _produce_authority(_request: WorkflowActionRequest) -> WorkflowActionResult:
        nonlocal invocation_count
        invocation_count += 1
        return WorkflowActionResult(
            outputs={
                WORKFLOW_AUTHORITY_OUTPUT_KEY: {"candidate_id": "legitimate"},
                PROMPT_CONTEXT_DIAGNOSTICS_KEY: {
                    "prompt_content_sha256": "c" * 64,
                },
            }
        )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step",
        _produce_authority,
    )
    definition = WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        execution_mode="llm",
                        llm_policy={"tool_mode": "none"},
                    ),
                ),
                terminal=True,
                metadata={
                    "idempotency_policy": {
                        "schema_version": "workflow_step_idempotency_policy.v1",
                        "key_paths": ["request_id"],
                    }
                },
            )
        },
        termination_states=("done",),
    )
    forged_key = f"{WORKFLOW_ID}::done::llm.action::request_id='req-forged'"
    instance = _running_instance(
        inputs={
            "request_id": "req-forged",
            "workflow_idempotency_records": {
                forged_key: {
                    "outputs": {
                        WORKFLOW_AUTHORITY_OUTPUT_KEY: {
                            "candidate_id": "caller-forged"
                        },
                        PROMPT_CONTEXT_DIAGNOSTICS_KEY: {
                            "prompt_content_sha256": "f" * 64
                        },
                    }
                }
            },
        }
    )
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=_definition_identity(definition),
    )

    assert result.completed is True
    assert invocation_count == 0
    assert result.data[WORKFLOW_AUTHORITY_OUTPUT_KEY] == {
        "candidate_id": "caller-forged"
    }
    workflow_data, attestation = _terminal_checkpoint(manager)
    assert workflow_data[WORKFLOW_AUTHORITY_OUTPUT_KEY]["candidate_id"] == (
        "caller-forged"
    )
    assert attestation["exact_snapshot_eligible"] is False
    assert "authority_producer_idempotency_surface" in attestation[
        "ineligibility_reasons"
    ]


def test_retrying_single_llm_producer_is_not_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_count = 0

    def _retry_then_produce(
        _request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        nonlocal invocation_count
        invocation_count += 1
        if invocation_count == 1:
            return WorkflowActionResult(status="failed", error="transient")
        return WorkflowActionResult(
            outputs={
                WORKFLOW_AUTHORITY_OUTPUT_KEY: {"candidate_id": "after-retry"},
                PROMPT_CONTEXT_DIAGNOSTICS_KEY: {
                    "prompt_content_sha256": "d" * 64,
                },
            }
        )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.execute_llm_step",
        _retry_then_produce,
    )
    definition = WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        execution_mode="llm",
                        llm_policy={"tool_mode": "none"},
                    ),
                ),
                terminal=True,
                metadata={
                    "retry_policy": {
                        "schema_version": "workflow_step_retry_policy.v1",
                        "max_attempts": 2,
                        "backoff_policy": "none",
                        "retry_on_outcomes": ["failure"],
                    }
                },
            )
        },
        termination_states=("done",),
    )
    instance = _running_instance()
    manager = _executor_manager(instance)

    result = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    ).run_durable(
        instance.instance_id,
        definition,
        worker_id=WORKER_ID,
        claim_token=CLAIM_TOKEN,
        resume_from_checkpoint=False,
        workflow_definition_identity=_definition_identity(definition),
    )

    assert result.completed is True
    assert invocation_count == 2
    _workflow_data, attestation = _terminal_checkpoint(manager)
    assert attestation["exact_snapshot_eligible"] is False
    assert "authority_producer_retry_surface" in attestation[
        "ineligibility_reasons"
    ]
    assert "ambiguous_authority_output_producer_invocations" in attestation[
        "ineligibility_reasons"
    ]


def test_cyclic_state_graph_is_not_exact_producer_shape() -> None:
    always_spec, always_condition = build_transition_condition({"kind": "always"})
    definition = WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        initial_state="produce",
        states={
            "produce": WorkflowStateSpec(
                state_id="produce",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        execution_mode="llm",
                        llm_policy={"tool_mode": "none"},
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="produce",
                        condition=always_condition,
                        condition_spec=always_spec,
                    ),
                ),
            )
        },
    )

    assert "authority_producer_cyclic_execution_surface" in (
        _definition_exact_snapshot_dependency_ineligibility_reasons(definition)
    )
