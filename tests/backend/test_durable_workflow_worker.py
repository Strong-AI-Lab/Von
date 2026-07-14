"""Focused worker regression tests for stale-running recovery hardening."""

from __future__ import annotations

import json
from threading import Thread
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from src.backend.workflows.durable.durable_executor import DurableWorkflowResult
from src.backend.workflows.durable.models import (
    WorkflowInstance,
    WorkflowInstanceStatus,
)
from src.backend.workflows.durable.worker import DurableWorkflowWorker
from src.backend.workflows.durable.registry_factory import (
    WorkflowDefinitionAuthorityTransientError,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
)


class _WorkerManagerStub:
    def __init__(self) -> None:
        self.release_lock_calls: list[tuple[str, str]] = []
        self.mark_completed_calls: list[dict[str, Any]] = []
        self.mark_failed_calls: list[dict[str, Any]] = []
        self.release_lock_error: Exception | None = None
        self.mark_completed_error: Exception | None = None
        self.mark_failed_error: Exception | None = None
        self.mark_completed_result = True
        self.mark_failed_result = True

    def release_lock(
        self,
        instance_id: str,
        worker_id: str,
        *,
        claim_token: str | None = None,
    ) -> bool:
        del claim_token
        self.release_lock_calls.append((instance_id, worker_id))
        if self.release_lock_error is not None:
            raise self.release_lock_error
        return True

    def mark_failed(self, instance_id: str, **kwargs: Any) -> bool:
        call = {"instance_id": instance_id}
        call.update(kwargs)
        self.mark_failed_calls.append(call)
        if self.mark_failed_error is not None:
            raise self.mark_failed_error
        return self.mark_failed_result

    def mark_completed(self, instance_id: str, **kwargs: Any) -> bool:
        call = {"instance_id": instance_id}
        call.update(kwargs)
        self.mark_completed_calls.append(call)
        if self.mark_completed_error is not None:
            raise self.mark_completed_error
        return self.mark_completed_result


class _PollManagerStub:
    def __init__(self, general_instances: list[WorkflowInstance]) -> None:
        self.general_instances = general_instances
        self.calls: list[dict[str, Any]] = []
        self.heartbeat_calls: list[dict[str, Any]] = []
        self.stopped_calls: list[dict[str, Any]] = []

    def find_and_claim_instance(
        self,
        worker_id: str,
        *,
        workflow_ids: list[str] | None = None,
        priority_only: bool = False,
        worker_build_identity: dict[str, Any] | None = None,
    ) -> WorkflowInstance | None:
        self.calls.append(
            {
                "worker_id": worker_id,
                "workflow_ids": workflow_ids,
                "priority_only": priority_only,
                "worker_build_identity": worker_build_identity,
            }
        )
        if priority_only:
            return None
        if not self.general_instances:
            return None
        return self.general_instances.pop(0)

    def upsert_worker_heartbeat(self, **kwargs: Any) -> bool:
        self.heartbeat_calls.append(dict(kwargs))
        return True

    def mark_worker_stopped(self, **kwargs: Any) -> bool:
        self.stopped_calls.append(dict(kwargs))
        return True


class _SuccessfulWorkerManagerStub(_WorkerManagerStub):
    def __init__(self, instances: list[WorkflowInstance]) -> None:
        super().__init__()
        self.instances = {instance.instance_id: instance for instance in instances}
        self.checkpoint_calls: list[dict[str, Any]] = []

    def get_instance(
        self,
        instance_id: str,
        *,
        for_execution: bool = False,
    ) -> WorkflowInstance | None:
        del for_execution
        return self.instances.get(instance_id)

    def is_cancelled(self, _instance_id: str) -> bool:
        return False

    def extend_lock(
        self,
        _instance_id: str,
        _worker_id: str,
        *,
        claim_token: str | None = None,
    ) -> bool:
        del claim_token
        return True

    def checkpoint(self, instance_id: str, **kwargs: Any) -> bool:
        self.checkpoint_calls.append({"instance_id": instance_id, **kwargs})
        return True


def _bind_claim(instance: WorkflowInstance, worker_id: str) -> WorkflowInstance:
    instance.status = WorkflowInstanceStatus.RUNNING
    instance.locked_by = worker_id
    instance.claim_token = f"claim-token-{worker_id}"
    return instance


def _build_instance(worker_id: str = "worker-1548") -> WorkflowInstance:
    instance = WorkflowInstance.create(
        "#V#workflow_introspection_maintenance_workflow",
        user_id="#V#user",
        org_id="#V#org",
        namespace="#V#user@org",
    )
    return _bind_claim(instance, worker_id)


def _build_pending_instance(workflow_id: str, instance_id: str) -> WorkflowInstance:
    instance = WorkflowInstance.create(
        workflow_id,
        user_id="#V#user",
        org_id="#V#org",
        namespace="#V#user@org",
    )
    instance.instance_id = instance_id
    instance.status = WorkflowInstanceStatus.RUNNING
    return instance


def test_worker_reserves_capacity_for_priority_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_DURABLE_WORKER_PRIORITY_RESERVED_SLOTS", "1")
    general_instances = [
        _build_pending_instance("#V#episode_evaluation_workflow", f"background-{index}")
        for index in range(5)
    ]
    manager = _PollManagerStub(general_instances)
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
    )
    worker._running = True
    worker._process_instance = cast(Any, lambda _instance: None)

    worker._poll_once()

    assert [call["priority_only"] for call in manager.calls] == [
        True,
        False,
        False,
        False,
        False,
    ]
    assert all(
        call["worker_build_identity"]["worker_id"] == "worker-1548"
        for call in manager.calls
    )
    assert len(worker._current_instances) == 4
    assert len(manager.general_instances) == 1
    assert manager.heartbeat_calls
    assert manager.heartbeat_calls[-1]["worker_id"] == "worker-1548"
    assert len(manager.heartbeat_calls[-1]["active_instance_ids"]) == 4


def test_worker_defers_general_claims_under_live_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a user turn is live, only the priority claim is attempted."""
    monkeypatch.setenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "1")
    monkeypatch.setenv("VON_DURABLE_WORKER_LIVE_LOAD_THRESHOLD", "1")
    general_instances = [
        _build_pending_instance("#V#episode_evaluation_workflow", f"background-{index}")
        for index in range(5)
    ]
    manager = _PollManagerStub(general_instances)
    worker = DurableWorkflowWorker(
        worker_id="worker-live-load",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
        live_load_getter=lambda: 1,
    )
    worker._running = True
    worker._process_instance = cast(Any, lambda _instance: None)

    worker._poll_once()

    # Only the priority-only claim is attempted; no general/background claims.
    assert [call["priority_only"] for call in manager.calls] == [True]
    assert len(worker._current_instances) == 0
    assert len(manager.general_instances) == 5


def test_worker_does_not_defer_when_no_live_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no live turn in flight, background claims proceed normally."""
    monkeypatch.setenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "1")
    monkeypatch.setenv("VON_DURABLE_WORKER_LIVE_LOAD_THRESHOLD", "1")
    general_instances = [
        _build_pending_instance("#V#episode_evaluation_workflow", f"background-{index}")
        for index in range(3)
    ]
    manager = _PollManagerStub(general_instances)
    worker = DurableWorkflowWorker(
        worker_id="worker-no-live-load",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
        live_load_getter=lambda: 0,
    )
    worker._running = True
    worker._process_instance = cast(Any, lambda _instance: None)

    worker._poll_once()

    assert any(call["priority_only"] is False for call in manager.calls)
    assert len(worker._current_instances) == 3


def test_worker_deferral_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferral can be turned off entirely via env."""
    monkeypatch.setenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "0")
    general_instances = [
        _build_pending_instance("#V#episode_evaluation_workflow", "background-0")
    ]
    manager = _PollManagerStub(general_instances)
    worker = DurableWorkflowWorker(
        worker_id="worker-deferral-off",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
        live_load_getter=lambda: 10,
    )
    assert worker._should_defer_background_work() is False


def test_worker_live_load_getter_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throwing live-load getter must not block background work."""
    monkeypatch.setenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "1")

    def _boom() -> int:
        raise RuntimeError("signal unavailable")

    manager = _PollManagerStub([])
    worker = DurableWorkflowWorker(
        worker_id="worker-getter-fail",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
        live_load_getter=_boom,
    )
    assert worker._should_defer_background_work() is False


def test_worker_cleanup_removes_tracking_even_if_release_lock_fails() -> None:
    manager = _WorkerManagerStub()
    manager.release_lock_error = RuntimeError("mongo_timeout")
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id, **_actor: None,
    )
    instance = _build_instance("worker-1548")
    worker._current_instances[instance.instance_id] = Thread()

    worker._process_instance(instance)

    assert instance.instance_id not in worker._current_instances
    assert manager.release_lock_calls == [(instance.instance_id, "worker-1548")]
    assert manager.mark_failed_calls
    assert manager.mark_failed_calls[0]["increment_retry"] is False


def test_worker_leaves_checkpoint_paused_result_out_of_terminal_failure_lane() -> None:
    manager = _WorkerManagerStub()
    instance = _build_instance("worker-checkpoint-pause")
    definition = WorkflowDefinition(
        workflow_id=instance.workflow_id,
        initial_state="after_pause",
        states={
            "after_pause": WorkflowStateSpec(
                state_id="after_pause",
                terminal=True,
            )
        },
        termination_states=("after_pause",),
    )
    worker = DurableWorkflowWorker(
        worker_id="worker-checkpoint-pause",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id, **_actor: definition,
    )
    worker._executor = cast(
        Any,
        SimpleNamespace(
            run_durable=lambda *_args, **_kwargs: DurableWorkflowResult(
                instance_id=instance.instance_id,
                data={},
                completed=False,
                final_state="after_pause",
                error="paused_at_checkpoint",
                execution_trace_id="trace-pause",
            )
        ),
    )

    worker._process_instance(instance)

    assert manager.mark_completed_calls == []
    assert manager.mark_failed_calls == []
    assert manager.release_lock_calls == [
        (instance.instance_id, "worker-checkpoint-pause")
    ]


def test_worker_loads_definition_under_persisted_instance_actor_scope() -> None:
    manager = _WorkerManagerStub()
    loader_calls: list[dict[str, Any]] = []

    def _actor_scoped_loader(workflow_id: str, **actor: Any) -> None:
        loader_calls.append({"workflow_id": workflow_id, **actor})
        return None

    worker = DurableWorkflowWorker(
        worker_id="worker-2580",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=_actor_scoped_loader,
    )
    instance = _build_instance("worker-2580")
    worker._current_instances[instance.instance_id] = Thread()

    worker._process_instance(instance)

    assert loader_calls == [
        {
            "workflow_id": instance.workflow_id,
            "actor_user_id": instance.user_id,
            "actor_org_id": instance.org_id,
            "actor_namespace": instance.namespace,
        }
    ]
    assert manager.mark_failed_calls[0]["error"] == (
        f"workflow_definition_not_found:{instance.workflow_id}"
    )


def test_worker_forwards_actual_loaded_definition_identity_to_executor() -> None:
    manager = _WorkerManagerStub()
    instance = _build_instance("worker-authority-identity")
    definition = WorkflowDefinition(
        workflow_id=instance.workflow_id,
        initial_state="done",
        states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
        termination_states=("done",),
    )
    definition_identity = build_workflow_definition_identity(
        workflow_id=definition.workflow_id,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )
    loaded_identity = {
        **definition_identity,
        "unexpected_diagnostic": {"must_not_be_persisted": True},
    }
    loader_calls: list[dict[str, Any]] = []

    def _authority_loader(
        workflow_id: str,
        *,
        actor_user_id: str | None = None,
        actor_org_id: str | None = None,
        actor_namespace: str | None = None,
        include_authority_resolution: bool = False,
    ) -> SimpleNamespace:
        loader_calls.append(
            {
                "workflow_id": workflow_id,
                "actor_user_id": actor_user_id,
                "actor_org_id": actor_org_id,
                "actor_namespace": actor_namespace,
                "include_authority_resolution": include_authority_resolution,
            }
        )
        return SimpleNamespace(
            definition=definition,
            definition_identity=loaded_identity,
        )

    executed: dict[str, Any] = {}

    def _run_durable(
        instance_id: str,
        loaded_definition: WorkflowDefinition,
        **kwargs: Any,
    ) -> DurableWorkflowResult:
        executed.update(
            {
                "instance_id": instance_id,
                "definition": loaded_definition,
                **kwargs,
            }
        )
        return DurableWorkflowResult(
            instance_id=instance_id,
            data={},
            completed=True,
            final_state="done",
        )

    worker = DurableWorkflowWorker(
        worker_id="worker-authority-identity",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=_authority_loader,
    )
    worker._executor = cast(Any, SimpleNamespace(run_durable=_run_durable))

    worker._process_instance(instance)

    assert loader_calls == [
        {
            "workflow_id": instance.workflow_id,
            "actor_user_id": instance.user_id,
            "actor_org_id": instance.org_id,
            "actor_namespace": instance.namespace,
            "include_authority_resolution": True,
        }
    ]
    assert executed["definition"] is definition
    assert executed["workflow_definition_identity"] == definition_identity
    assert "unexpected_diagnostic" not in executed["workflow_definition_identity"]
    assert manager.mark_completed_calls
    assert manager.mark_failed_calls == []


def test_worker_rejects_stale_loaded_definition_identity() -> None:
    manager = _WorkerManagerStub()
    instance = _build_instance("worker-stale-authority-identity")
    definition = WorkflowDefinition(
        workflow_id=instance.workflow_id,
        initial_state="done",
        states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
        termination_states=("done",),
    )
    stale_identity = build_workflow_definition_identity(
        workflow_id=definition.workflow_id,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )
    stale_identity["definition_hash"] = "0" * 64
    observed_identity: list[dict[str, Any] | None] = []

    def _authority_loader(
        _workflow_id: str,
        *,
        include_authority_resolution: bool = False,
        **_actor: Any,
    ) -> SimpleNamespace:
        assert include_authority_resolution is True
        return SimpleNamespace(
            definition=definition,
            definition_identity=stale_identity,
        )

    def _run_durable(
        instance_id: str,
        _loaded_definition: WorkflowDefinition,
        **kwargs: Any,
    ) -> DurableWorkflowResult:
        observed_identity.append(kwargs.get("workflow_definition_identity"))
        return DurableWorkflowResult(
            instance_id=instance_id,
            data={},
            completed=True,
            final_state="done",
        )

    worker = DurableWorkflowWorker(
        worker_id="worker-stale-authority-identity",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=_authority_loader,
    )
    worker._executor = cast(Any, SimpleNamespace(run_durable=_run_durable))

    worker._process_instance(instance)

    assert observed_identity == [None]
    assert manager.mark_completed_calls
    assert manager.mark_failed_calls == []


@pytest.mark.parametrize("completed", [True, False])
def test_worker_suppresses_terminal_callbacks_when_claim_fence_rejects_write(
    completed: bool,
) -> None:
    manager = _WorkerManagerStub()
    manager.mark_completed_result = False
    manager.mark_failed_result = False
    instance = _build_instance("worker-terminal-fence")
    result = DurableWorkflowResult(
        instance_id=instance.instance_id,
        data={},
        completed=completed,
        final_state="done",
        error=None if completed else "synthetic_failure",
    )
    worker = DurableWorkflowWorker(
        worker_id="worker-terminal-fence",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda workflow_id, **_actor: SimpleNamespace(
            workflow_id=workflow_id
        ),
    )
    worker._executor = cast(
        Any,
        SimpleNamespace(run_durable=lambda *_args, **_kwargs: result),
    )
    completed_callbacks: list[str] = []
    failed_callbacks: list[str] = []
    worker.set_callbacks(
        on_completed=lambda instance_id, _result: completed_callbacks.append(
            instance_id
        ),
        on_failed=lambda instance_id, _error: failed_callbacks.append(instance_id),
    )

    worker._process_instance(instance)

    assert completed_callbacks == []
    assert failed_callbacks == []


def test_worker_keeps_cached_definition_prompt_reads_under_persisted_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A globally warmed definition must not make a hidden prompt readable."""
    import src.backend.security.access_control as access_control
    from src.backend.services import prompt_template_service

    hidden_prompt_id = "#V#owner_only_prompt"

    class _Concepts:
        def find_one(self, query, _projection=None):
            if query.get("concept_id") != hidden_prompt_id:
                return None
            return {
                "concept_id": hidden_prompt_id,
                "relationships": {"specific_to_user": ["#V#prompt_owner"]},
            }

    monkeypatch.setattr(
        access_control,
        "get_concepts_collection",
        lambda: _Concepts(),
    )
    monkeypatch.setattr(
        prompt_template_service,
        "get_texts_for_concept",
        lambda concept_id: (
            [{"predicate": "hasContent", "text": "owner secret"}]
            if access_control.can_access_concept(concept_id)
            else []
        ),
    )

    # Model a process-global registry entry warmed by the owner. The same object
    # is then returned to a worker processing another actor's queued instance.
    warmed_definition = SimpleNamespace(
        workflow_id="#V#cached_workflow",
        prompt_concept_id=hidden_prompt_id,
    )
    with access_control.override_current_actor("#V#prompt_owner", "#V#owner_org"):
        assert access_control.can_access_concept(hidden_prompt_id) is True

    instance = WorkflowInstance.create(
        warmed_definition.workflow_id,
        user_id="#V#queued_outsider",
        org_id="#V#other_org",
        namespace="#V#queued_outsider@other_org",
    )
    instance.status = WorkflowInstanceStatus.RUNNING
    _bind_claim(instance, "worker-2580-prompt-scope")
    manager = _WorkerManagerStub()
    observed: dict[str, Any] = {}

    def _run_durable(*_args: Any, **_kwargs: Any) -> DurableWorkflowResult:
        observed["user"] = access_control.get_effective_user_concept_id()
        observed["org"] = access_control.get_effective_organisation_concept_id()
        observed["workflow_definition_identity"] = _kwargs.get(
            "workflow_definition_identity"
        )
        observed["prompt"] = prompt_template_service.PromptTemplateService().resolve_prompt_text(
            [warmed_definition.prompt_concept_id]
        )
        return DurableWorkflowResult(
            instance_id=instance.instance_id,
            data={},
            completed=True,
            final_state="done",
        )

    worker = DurableWorkflowWorker(
        worker_id="worker-2580-prompt-scope",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id, **_actor: warmed_definition,
    )
    worker._executor = cast(Any, SimpleNamespace(run_durable=_run_durable))

    worker._process_instance(instance)

    assert observed == {
        "user": "#V#queued_outsider",
        "org": "#V#other_org",
        "workflow_definition_identity": None,
        "prompt": (None, None),
    }
    assert manager.mark_completed_calls


def test_worker_completes_same_org_instances_with_each_persisted_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_id = "#V#same_org_actor_propagation_workflow"
    instances = [
        WorkflowInstance.create(
            workflow_id,
            user_id=user_id,
            org_id="#V#trusted_org",
            namespace=f"{user_id}@trusted_org",
            inputs={
                "user_concept_id": "#V#forged_input_actor",
                "org_concept_id": "#V#forged_org",
                "namespace": "#V#forged_input_actor@forged_org",
            },
        )
        for user_id in ("#V#owner", "#V#cohort_member")
    ]
    for instance in instances:
        _bind_claim(instance, "worker-2580-same-org")

    manager = _SuccessfulWorkerManagerStub(instances)
    loader_calls: list[dict[str, Any]] = []
    action_calls: list[dict[str, Any]] = []

    def _load_definition(workflow_id_arg: str, **actor: Any) -> WorkflowDefinition:
        loader_calls.append({"workflow_id": workflow_id_arg, **actor})
        return WorkflowDefinition(
            workflow_id=workflow_id_arg,
            initial_state="#V#done",
            states={
                "#V#done": WorkflowStateSpec(
                    state_id="#V#done",
                    actions=(WorkflowActionInvocation(action_id="actor.probe"),),
                    terminal=True,
                )
            },
            termination_states=("#V#done",),
        )

    def _actor_probe(request: WorkflowActionRequest) -> WorkflowActionResult:
        action_calls.append(
            {
                "environment_user": request.environment.user_concept_id,
                "environment_org": request.environment.org_concept_id,
                "environment_namespace": request.environment.user_namespace,
                "context_user": request.data.get("user_concept_id"),
                "context_org": request.data.get("org_concept_id"),
                "context_namespace": request.data.get("namespace"),
            }
        )
        return WorkflowActionResult(outputs={"actor_probe_completed": True})

    registry = ActionRegistry()
    registry.register(ActionSpec(action_id="actor.probe", handler=_actor_probe))
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda **_kwargs: "synthetic-model",
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
        lambda _trace: "trace-same-org-actor",
    )
    worker = DurableWorkflowWorker(
        worker_id="worker-2580-same-org",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=registry,
        definition_loader=_load_definition,
    )

    for instance in instances:
        worker._process_instance(instance)

    assert loader_calls == [
        {
            "workflow_id": workflow_id,
            "actor_user_id": instance.user_id,
            "actor_org_id": instance.org_id,
            "actor_namespace": instance.namespace,
        }
        for instance in instances
    ]
    assert action_calls == [
        {
            "environment_user": instance.user_id,
            "environment_org": instance.org_id,
            "environment_namespace": instance.namespace,
            "context_user": instance.user_id,
            "context_org": instance.org_id,
            "context_namespace": instance.namespace,
        }
        for instance in instances
    ]
    assert [call["instance_id"] for call in manager.mark_completed_calls] == [
        instance.instance_id for instance in instances
    ]
    assert manager.mark_failed_calls == []


def test_worker_retries_transient_actor_scoped_definition_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _WorkerManagerStub()
    loader_calls: list[dict[str, Any]] = []

    def _transient_loader(workflow_id: str, **actor: Any) -> None:
        loader_calls.append({"workflow_id": workflow_id, **actor})
        raise WorkflowDefinitionAuthorityTransientError(
            "workflow_definition_authority temporarily unavailable:synthetic"
        )

    monkeypatch.setenv("VON_TRANSIENT_MONGO_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(
        "src.backend.db.transient_errors.attempt_reconnect",
        lambda **_kwargs: {"reconnected": True},
    )
    monkeypatch.setattr("src.backend.db.transient_errors.time.sleep", lambda _delay: None)
    worker = DurableWorkflowWorker(
        worker_id="worker-2580-transient",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=_transient_loader,
    )
    instance = _build_instance("worker-2580-transient")
    worker._current_instances[instance.instance_id] = Thread()

    worker._process_instance(instance)

    assert len(loader_calls) == 3
    assert all(call["actor_user_id"] == instance.user_id for call in loader_calls)
    assert all(call["actor_org_id"] == instance.org_id for call in loader_calls)
    assert manager.mark_failed_calls[0]["increment_retry"] is True
    assert manager.mark_failed_calls[0]["error"].startswith(
        "worker_exception:workflow_definition_authority temporarily unavailable"
    )


def test_worker_swallows_mark_failed_errors_during_exception_path() -> None:
    manager = _WorkerManagerStub()
    manager.mark_failed_error = RuntimeError("mongo_timeout")
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=cast(
            Any,
            lambda _workflow_id, **_actor: SimpleNamespace(
                workflow_id=_workflow_id
            ),
        ),
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()
    worker._executor = cast(
        Any,
        SimpleNamespace(
            run_durable=lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("executor blew up")
            )
        ),
    )

    worker._process_instance(instance)

    assert instance.instance_id not in worker._current_instances
    assert manager.release_lock_calls == [(instance.instance_id, "worker-1548")]
    assert manager.mark_failed_calls
    assert manager.mark_failed_calls[0]["error"].startswith("worker_exception:")


def test_worker_persists_bounded_failed_outputs_from_result_context() -> None:
    manager = _WorkerManagerStub()
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=cast(
            Any,
            lambda _workflow_id, **_actor: SimpleNamespace(
                workflow_id=_workflow_id
            ),
        ),
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()
    raw_response = "{" + ("x" * 3000)
    worker._executor = cast(
        Any,
        SimpleNamespace(
            run_durable=lambda *args, **kwargs: DurableWorkflowResult(
                instance_id=instance.instance_id,
                data={
                    "last_metadata_validation": {
                        "state_id": "infer_expected_outcome",
                        "ok": False,
                        "reason_code": "metadata_write_context_key_missing",
                        "details": {"symbol": "turn_expected_grounding_requirement"},
                    },
                    "workflow_metadata_validation_events": [
                        {"state_id": "infer_expected_outcome", "ok": False}
                    ],
                    "workflow_step_result_envelopes": [
                        {
                            "state_id": "infer_expected_outcome",
                            "action_id": "llm.action",
                            "action_status": "failed",
                            "action_outcome": "failure",
                            "diagnostics": {"error": "json_parse_failed:unparsed"},
                            "output_payload": {
                                "llm_step_response": raw_response,
                                "validated_json_raw_response": raw_response,
                                "prompt": "do not persist the full prompt",
                                "api_token": "secret-token-value",
                            },
                        }
                    ],
                    "last_workflow_step_result_envelope": {
                        "state_id": "infer_expected_outcome",
                        "action_id": "llm.action",
                        "action_status": "failed",
                        "action_outcome": "failure",
                        "diagnostics": {"error": "json_parse_failed:unparsed"},
                        "output_payload": {
                            "llm_step_response": raw_response,
                            "validated_json_raw_response": raw_response,
                            "prompt": "do not persist the full prompt",
                            "api_token": "secret-token-value",
                        },
                    },
                },
                completed=False,
                final_state="infer_expected_outcome",
                error=(
                    "metadata_validation_failed:"
                    "metadata_write_context_key_missing:"
                    "infer_expected_outcome:"
                    "turn_expected_grounding_requirement"
                ),
                execution_trace_id="trace-failed-1",
            )
        ),
    )

    worker._process_instance(instance)

    assert manager.mark_failed_calls
    outputs = manager.mark_failed_calls[0]["outputs"]
    assert outputs["schema_version"] == "workflow_failed_outputs.v1"
    assert outputs["error_step"] == "infer_expected_outcome"
    diagnostics = outputs["failed_action_diagnostics"]
    assert diagnostics["state_id"] == "infer_expected_outcome"
    assert diagnostics["action_id"] == "llm.action"
    assert diagnostics["output_keys"] == [
        "api_token",
        "llm_step_response",
        "prompt",
        "validated_json_raw_response",
    ]
    compact_payload = diagnostics["output_payload"]
    assert compact_payload["llm_step_response"]["truncated"] is True
    assert compact_payload["llm_step_response"]["char_count"] == len(raw_response)
    assert len(compact_payload["llm_step_response"]["text_preview"]) == 2000
    assert compact_payload["api_token"] == "[redacted]"
    assert compact_payload["prompt"]["suppressed"] is True
    assert (
        outputs["metadata_validation"]["last_event"]["reason_code"]
        == "metadata_write_context_key_missing"
    )


def test_worker_persists_bounded_completed_outputs_from_declared_payload() -> None:
    manager = _WorkerManagerStub()
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=cast(
            Any,
            lambda _workflow_id, **_actor: SimpleNamespace(
                workflow_id=_workflow_id
            ),
        ),
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()
    raw_message_body = "private message body " + ("x" * 20_000)
    raw_document_text = "document text " + ("y" * 20_000)
    result_envelope = {
        "schema_version": "workflow_result_envelope.v1",
        "workflow_id": "#V#large_payload_workflow",
        "completed": True,
        "terminal_status": "completed",
        "final_state": "done",
        "declared_output_payload": {
            "artefact_concept_id": "#V#artefact_1",
            "file_copy_concept_id": "#V#file_1",
            "completion_marker": {
                "marker_name": "processed",
                "source_item_id": "message-1",
            },
            "evidence_note": "e" * 3_000,
        },
    }
    worker._executor = cast(
        Any,
        SimpleNamespace(
            run_durable=lambda *args, **kwargs: DurableWorkflowResult(
                instance_id=instance.instance_id,
                data={
                    "raw_source_item": {
                        "body": raw_message_body,
                        "headers": {"authorization": "Bearer secret"},
                    },
                    "raw_document_text": raw_document_text,
                    "workflow_result_envelope": result_envelope,
                    "workflow_step_result_envelopes": [
                        {
                            "state_id": "mark_done",
                            "action_id": "workflow_mcp.invoke_tool",
                            "action_status": "success",
                            "action_outcome": "success",
                        }
                    ],
                },
                completed=True,
                final_state="done",
                result_envelope=result_envelope,
                execution_trace_id="trace-completed-1",
            )
        ),
    )

    worker._process_instance(instance)

    assert manager.mark_completed_calls
    outputs = manager.mark_completed_calls[0]["outputs"]
    assert outputs["schema_version"] == "workflow_completed_outputs.v1"
    assert outputs["terminal_status"] == "completed"
    assert outputs["artefact_concept_id"] == "#V#artefact_1"
    assert outputs["file_copy_concept_id"] == "#V#file_1"
    assert outputs["completion_marker"] == {
        "marker_name": "processed",
        "source_item_id": "message-1",
    }
    assert outputs["evidence_note"]["truncated"] is True
    assert outputs["terminal_output_source"] == "declared_output_payload"
    assert "raw_source_item" not in outputs
    assert "raw_document_text" not in outputs
    serialised = json.dumps(outputs, sort_keys=True)
    assert raw_message_body not in serialised
    assert raw_document_text not in serialised
    assert len(serialised) < 10_000
