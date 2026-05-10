from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from src.backend.workflows.durable.workflow_instance_submission_service import (
    WorkflowRunnableVerification,
    invalidate_workflow_runnable_verification_cache,
    submit_verified_workflow_instance,
    verify_workflow_runnable,
)
from src.backend.workflows.durable.registry_factory import (
    build_durable_action_registry,
    invalidate_shared_durable_action_registry,
    invalidate_shared_workflow_registry_read_only,
    resolve_workflow_definition_from_authority,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.workflows.workflow_registry import WorkflowRegistry
from src.backend.workflows.subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    build_subworkflow_contract,
)


@pytest.fixture(autouse=True)
def _clear_runnable_cache_between_tests():
    invalidate_workflow_runnable_verification_cache(reason="test_fixture_pre")
    invalidate_shared_workflow_registry_read_only()
    invalidate_shared_durable_action_registry()
    yield
    invalidate_workflow_runnable_verification_cache(reason="test_fixture_post")
    invalidate_shared_workflow_registry_read_only()
    invalidate_shared_durable_action_registry()


def _make_definition(
    *,
    include_action: bool,
    metadata: dict[str, object] | None = None,
) -> WorkflowDefinition:
    states = {
        "#V#start": WorkflowStateSpec(
            state_id="#V#start",
            actions=(
                [WorkflowActionInvocation(action_id="tool.initial")]
                if include_action
                else ()
            ),
            terminal=True,
        )
    }
    return WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states=states,
        termination_states=("#V#start",),
        metadata=dict(metadata or {}),
    )


def _make_registry(
    definition: WorkflowDefinition | None,
    *,
    workflow_id: str = "#V#candidate_workflow",
    extra_definitions: dict[str, WorkflowDefinition] | None = None,
) -> MagicMock:
    registry = MagicMock()
    definitions = {
        key: value
        for key, value in (
            {
                workflow_id: definition,
                **(extra_definitions or {}),
            }
        ).items()
        if value is not None
    }
    registry.get.side_effect = lambda item: definitions.get(item)
    registry.all_workflow_ids.return_value = list(definitions.keys())
    registration = MagicMock()
    registration.source = "vontology"
    registry.get_registration.return_value = registration
    return registry


def _make_action_registry(*, supports_action: bool, fallback: bool = False) -> MagicMock:
    registry = MagicMock()
    registry.has_fallback_handler.return_value = fallback
    if supports_action:
        registry.has.return_value = True
        registry.all_action_ids.return_value = ["tool.initial"]
    else:
        registry.has.return_value = False
        registry.all_action_ids.return_value = []
    return registry


def _make_verification(
    *,
    workflow_id: str = "#V#candidate_workflow",
    runnable: bool = True,
) -> WorkflowRunnableVerification:
    return WorkflowRunnableVerification(
        workflow_id=workflow_id,
        conceptual_representation_success=runnable,
        executable_registration_success=runnable,
        runnable_verification_success=runnable,
        fallback_action_routing_enabled=False,
        discovered_action_ids=(),
        unsupported_action_ids=(),
        integrity_issues=(),
        warnings=(),
        errors=(),
    )


def test_authority_resolver_uses_current_shared_registry_after_stale_registry_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_id = "#V#conversation_turn_execution_workflow"
    definition = _make_definition(include_action=False)
    definition = WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=definition.initial_state,
        states=definition.states,
        termination_states=definition.termination_states,
        metadata=definition.metadata,
    )
    stale_registry = WorkflowRegistry(definition_loader=lambda _workflow_id: None)

    import src.backend.workflows.durable.registry_factory as registry_factory

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [workflow_id])
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        lambda candidate_id: definition if candidate_id == workflow_id else None,
    )

    invalidate_shared_workflow_registry_read_only()
    resolution = resolve_workflow_definition_from_authority(
        workflow_id,
        registry=stale_registry,
        use_current_shared_registry=True,
        promote_to_registry=stale_registry,
    )

    assert resolution.success is True
    assert resolution.definition is definition
    assert resolution.registration_source == "vontology"
    assert resolution.registry is not stale_registry
    assert stale_registry.get(workflow_id) is definition
    assert (resolution.definition_identity or {}).get("source") == "vontology"


def test_durable_definition_loader_resolves_after_shared_registry_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_id = "#V#conversation_turn_execution_workflow"
    definition = _make_definition(include_action=False)
    definition = WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=definition.initial_state,
        states=definition.states,
        termination_states=definition.termination_states,
        metadata=definition.metadata,
    )
    stale_registry = WorkflowRegistry(definition_loader=lambda _workflow_id: None)

    import src.backend.server.utils_flask as utils_flask
    import src.backend.workflows.durable.registry_factory as registry_factory

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [workflow_id])
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        lambda candidate_id: definition if candidate_id == workflow_id else None,
    )
    monkeypatch.setattr(utils_flask, "_durable_workflow_registry", stale_registry)

    invalidate_shared_workflow_registry_read_only()
    loader = utils_flask._get_durable_definition_loader()

    assert loader(workflow_id) is definition
    assert utils_flask._durable_workflow_registry is not stale_registry


def test_orchestrator_definition_resolution_refreshes_stale_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_id = "#V#conversation_turn_execution_workflow"
    definition = _make_definition(include_action=False)
    definition = WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=definition.initial_state,
        states=definition.states,
        termination_states=definition.termination_states,
        metadata=definition.metadata,
    )
    stale_registry = WorkflowRegistry(definition_loader=lambda _workflow_id: None)

    import src.backend.workflows.durable.registry_factory as registry_factory
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [workflow_id])
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        lambda candidate_id: definition if candidate_id == workflow_id else None,
    )

    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._workflow_registry = stale_registry
    orchestrator._action_registry = MagicMock()
    orchestrator._workflow_executor = MagicMock()
    invalidate_shared_workflow_registry_read_only()

    registration, resolved_definition = (
        InternalMCPChatOrchestrator._resolve_workflow_registration_and_definition(
            orchestrator,
            workflow_id,
        )
    )

    assert resolved_definition is definition
    assert registration is not None
    assert registration.source == "vontology"
    assert orchestrator._workflow_registry is not stale_registry


def test_verify_workflow_runnable_rejects_initial_vacuous_step() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": None,
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "writes_variables": [],
            }
        ],
        "edges": [],
        "warnings": [],
    }

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(_make_definition(include_action=False)),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert verification.unsupported_action_ids == ()
    assert "workflow_step_contract_integrity_issue" in verification.errors
    assert len(verification.integrity_issues) == 1
    assert verification.contract_validation is not None
    issue = verification.integrity_issues[0]
    assert issue["step_id"] == "#V#start"
    previous = issue["previous_steps"][0]
    assert previous["step_id"] == "__workflow_input__"
    assert previous["step_name"] == "Workflow input"


def test_verify_workflow_runnable_reports_previous_step_context_for_middle_vacuity() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": "tool.initial",
            },
            {
                "step_id": "#V#middle",
                "name": "Middle",
                "invokes_action": None,
            },
            {
                "step_id": "#V#end",
                "name": "End",
                "invokes_action": "tool.done",
            },
        ],
        "edges": [
            {"from": "#V#start", "to": "#V#middle", "predicate": "nextStep"},
            {"from": "#V#middle", "to": "#V#end", "predicate": "nextStep"},
        ],
        "warnings": [],
    }

    states = {
        "#V#start": WorkflowStateSpec(
            state_id="#V#start",
            actions=(WorkflowActionInvocation(action_id="tool.initial"),),
        ),
        "#V#middle": WorkflowStateSpec(
            state_id="#V#middle",
        ),
        "#V#end": WorkflowStateSpec(
            state_id="#V#end",
            actions=(WorkflowActionInvocation(action_id="tool.done"),),
        ),
    }
    definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states=states,
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert len(verification.integrity_issues) == 1
    issue = verification.integrity_issues[0]
    assert issue["step_id"] == "#V#middle"
    previous = issue["previous_steps"][0]
    assert previous["step_id"] == "#V#start"
    assert previous["invokes_action"] == "tool.initial"
    assert previous["link_predicate"] == "nextStep"


def test_verify_workflow_runnable_rejects_actionless_output_only_contract() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": None,
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "reads_context_keys": [],
                "writes_variables": [],
                "writes_context_keys": [
                    "#V#workflow_context_key_validated_type_name"
                ],
            },
            {
                "step_id": "#V#complete",
                "name": "Complete",
                "invokes_action": None,
            },
        ],
        "edges": [
            {"from": "#V#start", "to": "#V#complete", "predicate": "nextStep"}
        ],
        "warnings": [],
    }
    definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                metadata={
                    "writes_context_keys": [
                        "#V#workflow_context_key_validated_type_name"
                    ]
                },
                transitions=(),
            ),
            "#V#complete": WorkflowStateSpec(
                state_id="#V#complete",
                terminal=True,
            )
        },
        termination_states=("#V#complete",),
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert "workflow_step_contract_integrity_issue" in verification.errors
    assert verification.contract_validation is not None
    assert "workflow_step_contract_vacuous" in (
        verification.contract_validation.get("errors") or []
    )
    assert verification.contract_validation.get("vacuous_state_ids") == ["#V#start"]


def test_verify_workflow_runnable_allows_workflows_with_contracts() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": "tool.initial",
            }
        ],
        "edges": [],
        "warnings": [],
    }

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(_make_definition(include_action=True)),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    assert verification.integrity_issues == ()
    assert verification.errors == ()
    assert verification.definition_identity is not None
    assert verification.contract_validation is not None
    assert verification.contract_validation.get("valid") is True


def test_verify_workflow_runnable_defers_registry_parity_work() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": "tool.initial",
            }
        ],
        "edges": [],
        "warnings": [],
    }
    registry = _make_registry(_make_definition(include_action=True))
    action_registry = _make_action_registry(supports_action=True)
    registry_kwargs: dict[str, object] = {}

    def _build_registry(**kwargs):
        registry_kwargs.update(kwargs)
        return registry

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        side_effect=_build_registry,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=action_registry,
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    assert registry_kwargs == {"defer_parity_work": True}


def test_verify_workflow_runnable_attempts_shared_registry_refresh_for_missing_workflow() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": "tool.initial",
            }
        ],
        "edges": [],
        "warnings": [],
    }
    definition = _make_definition(include_action=True)
    registration = MagicMock()
    registration.source = "vontology"
    registry = MagicMock()
    registry.get.side_effect = [None, definition]
    registry.get_registration.return_value = registration
    registry.all_workflow_ids.return_value = ["#V#candidate_workflow"]
    action_registry = _make_action_registry(supports_action=True)

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=registry,
    ), patch(
        "src.backend.workflows.durable.registry_factory.register_workflow_from_vontology",
        return_value=(True, None),
    ) as mock_register, patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=action_registry,
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    mock_register.assert_called_once_with(
        registry=registry,
        workflow_id="#V#candidate_workflow",
        actor_user_id=None,
        actor_org_id=None,
    )


def test_submit_verified_workflow_instance_uses_namespace_actor_for_authority_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_id = "#V#conversation_turn_execution_workflow"
    definition = WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=(WorkflowActionInvocation(action_id="tool.initial"),),
                terminal=True,
            )
        },
        termination_states=("#V#start",),
    )
    graph = {
        "workflow_id": workflow_id,
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": "tool.initial",
            }
        ],
        "edges": [],
        "warnings": [],
    }
    observed_actor_contexts: list[tuple[str | None, str | None]] = []

    def _load_from_vontology(candidate_id: str) -> WorkflowDefinition | None:
        from src.backend.security.access_control import (
            get_effective_organisation_concept_id,
            get_effective_user_concept_id,
        )

        actor_context = (
            get_effective_user_concept_id(),
            get_effective_organisation_concept_id(),
        )
        observed_actor_contexts.append(actor_context)
        if actor_context != (
            "#V#michael_witbrock",
            "#V#university_of_auckland_strong_ai_lab",
        ):
            return None
        return definition if candidate_id == workflow_id else None

    import src.backend.workflows.durable.registry_factory as registry_factory
    import src.backend.workflows.durable.workflow_instance_submission_service as submission_service

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [])
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        _load_from_vontology,
    )
    monkeypatch.setattr(
        submission_service,
        "build_workflow_process_graph_from_definition",
        lambda _definition: graph,
    )
    monkeypatch.setattr(
        registry_factory,
        "get_shared_durable_action_registry",
        lambda: _make_action_registry(supports_action=True),
    )

    manager = MagicMock()
    manager.create_instance.return_value = "instance-1"

    result = submit_verified_workflow_instance(
        manager=manager,
        workflow_id=workflow_id,
        user_id="#V#michael_witbrock",
        org_id="university_of_auckland_strong_ai_lab",
        namespace=(
            "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
        ),
        inputs={},
        max_retries=1,
    )

    assert result.success is True
    assert result.status == "pending"
    assert result.verification["preflight"]["runnable_verification_success"] is True
    assert (
        "#V#michael_witbrock",
        "#V#university_of_auckland_strong_ai_lab",
    ) in observed_actor_contexts
    manager.create_instance.assert_called_once()


def test_verify_workflow_runnable_rejects_missing_transition_from_action_state() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": "tool.initial",
            }
        ],
        "edges": [],
        "warnings": [],
    }
    definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=(WorkflowActionInvocation(action_id="tool.initial"),),
                terminal=False,
            )
        },
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert "workflow_control_flow_incomplete" in verification.errors


def test_verify_workflow_runnable_accepts_shared_conversation_actions_via_fallback() -> None:
    workflow_action_ids = (
        "shared_conversation_create_session",
        "shared_conversation_join_session",
        "shared_conversation_invite_create",
        "shared_conversation_list_invites",
        "shared_conversation_respond_invite",
    )
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": workflow_action_ids[0],
            }
        ],
        "edges": [],
        "warnings": [],
    }
    definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=tuple(
                    WorkflowActionInvocation(action_id=action_id)
                    for action_id in workflow_action_ids
                ),
                terminal=True,
            )
        },
        termination_states=("#V#start",),
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=False, fallback=True),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service._internal_mcp_method_names",
        return_value=frozenset(workflow_action_ids),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    assert set(verification.discovered_action_ids) == set(workflow_action_ids)
    assert verification.unsupported_action_ids == ()
    assert verification.contract_validation is not None
    assert verification.contract_validation.get("valid") is True


def test_verify_workflow_runnable_accepts_turn_execution_runtime_support_actions() -> None:
    workflow_action_ids = (
        "turn_execution.critic",
        "turn_execution.completion_gate",
    )
    graph = {
        "workflow_id": "#V#conversation_turn_execution_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": workflow_action_ids[0],
            }
        ],
        "edges": [],
        "warnings": [],
    }
    definition = WorkflowDefinition(
        workflow_id="#V#conversation_turn_execution_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=tuple(
                    WorkflowActionInvocation(action_id=action_id)
                    for action_id in workflow_action_ids
                ),
                terminal=True,
            )
        },
        termination_states=("#V#start",),
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(
            definition,
            workflow_id="#V#conversation_turn_execution_workflow",
        ),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=build_durable_action_registry(),
    ):
        verification = verify_workflow_runnable("#V#conversation_turn_execution_workflow")

    assert verification.runnable_verification_success is True
    assert set(verification.discovered_action_ids) == set(workflow_action_ids)
    assert verification.unsupported_action_ids == ()
    assert verification.contract_validation is not None
    assert verification.contract_validation.get("valid") is True


def test_verify_workflow_runnable_accepts_orchestrator_registry_override() -> None:
    workflow_action_ids = (
        "tool_calling.preflight_requirements",
        "tool_calling.respond",
    )
    graph = {
        "workflow_id": "#V#tool_calling_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": workflow_action_ids[0],
            }
        ],
        "edges": [],
        "warnings": [],
    }
    definition = WorkflowDefinition(
        workflow_id="#V#tool_calling_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=tuple(
                    WorkflowActionInvocation(action_id=action_id)
                    for action_id in workflow_action_ids
                ),
                terminal=True,
            )
        },
        termination_states=("#V#start",),
    )
    shared_registry = _make_action_registry(supports_action=False, fallback=True)
    override_registry = _make_action_registry(supports_action=True, fallback=True)
    override_registry.all_action_ids.return_value = list(workflow_action_ids)

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(
            definition,
            workflow_id="#V#tool_calling_workflow",
        ),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=shared_registry,
    ):
        verification = verify_workflow_runnable(
            "#V#tool_calling_workflow",
            action_registry_override=override_registry,
        )

    assert verification.runnable_verification_success is True
    assert verification.unsupported_action_ids == ()
    assert set(verification.discovered_action_ids) == set(workflow_action_ids)
    assert verification.contract_validation is not None
    assert verification.contract_validation.get("valid") is True


def test_verify_workflow_runnable_accepts_dot_named_actions_via_internal_tool_alias() -> None:
    workflow_action_ids = (
        "testing.prepare_arxiv_paper_ingestion_fixture",
        "testing.verify_arxiv_paper_ingestion_result",
        "testing.cleanup_arxiv_paper_ingestion_artifacts",
    )
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": workflow_action_ids[0],
            }
        ],
        "edges": [],
        "warnings": [],
    }
    definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=tuple(
                    WorkflowActionInvocation(action_id=action_id)
                    for action_id in workflow_action_ids
                ),
                terminal=True,
            )
        },
        termination_states=("#V#start",),
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=False, fallback=True),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service._internal_mcp_method_names",
        return_value=frozenset(
            {
                "testing_prepare_arxiv_paper_ingestion_fixture",
                "testing_verify_arxiv_paper_ingestion_result",
                "testing_cleanup_arxiv_paper_ingestion_artifacts",
            }
        ),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    assert set(verification.discovered_action_ids) == set(workflow_action_ids)
    assert verification.unsupported_action_ids == ()
    assert verification.contract_validation is not None
    assert verification.contract_validation.get("valid") is True


def test_verify_workflow_runnable_caches_for_identical_definition() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {"step_id": "#V#start", "name": "Start", "invokes_action": "tool.initial"}
        ],
        "edges": [],
        "warnings": [],
    }
    registry = _make_registry(_make_definition(include_action=True))
    action_registry = _make_action_registry(supports_action=True)

    invalidate_workflow_runnable_verification_cache(
        reason="test_setup",
        workflow_id="#V#candidate_workflow",
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ) as mock_graph, patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=registry,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=action_registry,
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service._internal_mcp_method_names",
        return_value=frozenset(),
    ):
        first = verify_workflow_runnable("#V#candidate_workflow")
        second = verify_workflow_runnable("#V#candidate_workflow")

    assert first.runnable_verification_success is True
    assert second.runnable_verification_success is True
    assert mock_graph.call_count == 1
    assert (first.verification_telemetry or {}).get("cache_hit") is False
    assert (second.verification_telemetry or {}).get("cache_hit") is True


def test_verify_workflow_runnable_cache_invalidation_forces_recompute() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {"step_id": "#V#start", "name": "Start", "invokes_action": "tool.initial"}
        ],
        "edges": [],
        "warnings": [],
    }
    registry = _make_registry(_make_definition(include_action=True))
    action_registry = _make_action_registry(supports_action=True)

    invalidate_workflow_runnable_verification_cache(
        reason="test_setup",
        workflow_id="#V#candidate_workflow",
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ) as mock_graph, patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=registry,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=action_registry,
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service._internal_mcp_method_names",
        return_value=frozenset(),
    ):
        verify_workflow_runnable("#V#candidate_workflow")
        invalidate_workflow_runnable_verification_cache(
            reason="test_invalidation",
            workflow_id="#V#candidate_workflow",
        )
        second = verify_workflow_runnable("#V#candidate_workflow")

    assert mock_graph.call_count == 2
    assert (second.verification_telemetry or {}).get("cache_hit") is False


def test_verify_workflow_runnable_fail_closed_on_registry_exception() -> None:
    invalidate_workflow_runnable_verification_cache(
        reason="test_setup",
        workflow_id="#V#candidate_workflow",
    )
    with patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        side_effect=RuntimeError("registry unavailable"),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert "workflow_runnable_check_failed" in verification.errors


def test_verify_workflow_runnable_reports_unresolved_subworkflow_contract() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_workflow": "#V#missing_child",
            }
        ],
        "edges": [],
        "warnings": [],
    }
    parent_definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                    ),
                ),
                terminal=True,
                metadata={
                    "subworkflow_contract": build_subworkflow_contract(
                        workflow_id="#V#missing_child",
                        input_mappings=[
                            {
                                "child_input_key": "child_input",
                                "parent_context_key": "parent_input",
                            }
                        ],
                        output_mappings=[
                            {
                                "child_output_field": "child_output",
                                "parent_context_key": "parent_output",
                            }
                        ],
                    )
                },
            )
        },
        termination_states=("#V#start",),
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(parent_definition),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.load_workflow_definition_from_vontology",
        return_value=None,
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert "workflow_subworkflow_unresolved" in verification.errors
    contract = verification.contract_validation or {}
    assert any(
        issue.get("reason_code") == "subworkflow_workflow_not_found"
        for issue in contract.get("subworkflow_contract_issues", [])
    )


def test_verify_workflow_runnable_accepts_resolved_subworkflow_contract() -> None:
    child_definition = WorkflowDefinition(
        workflow_id="#V#child_workflow",
        initial_state="#V#child_start",
        states={
            "#V#child_start": WorkflowStateSpec(
                state_id="#V#child_start",
                terminal=True,
                metadata={
                    "reads_context_keys": ["child_input"],
                    "writes_context_keys": ["child_output"],
                },
            )
        },
        termination_states=("#V#child_start",),
    )
    parent_definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                    ),
                ),
                terminal=True,
                metadata={
                    "subworkflow_contract": build_subworkflow_contract(
                        workflow_id="#V#child_workflow",
                        input_mappings=[
                            {
                                "child_input_key": "child_input",
                                "parent_context_key": "parent_input",
                            }
                        ],
                        output_mappings=[
                            {
                                "child_output_field": "child_output",
                                "parent_context_key": "parent_output",
                            }
                        ],
                    )
                },
            )
        },
        termination_states=("#V#start",),
    )
    definitions = {
        "#V#candidate_workflow": parent_definition,
        "#V#child_workflow": child_definition,
    }
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_workflow": "#V#child_workflow",
            }
        ],
        "edges": [],
        "warnings": [],
    }

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph_from_definition",
        return_value=graph,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(
            parent_definition,
            extra_definitions={"#V#child_workflow": child_definition},
        ),
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.load_workflow_definition_from_vontology",
        side_effect=lambda workflow_id: definitions.get(workflow_id),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    assert "workflow_subworkflow_unresolved" not in verification.errors
    assert "workflow_subworkflow_contract_mismatch" not in verification.errors


def test_submit_verified_workflow_instance_uses_event_idempotent_creation() -> None:
    manager = MagicMock()
    manager.create_instance_for_event.return_value = ("instance-evt-1", True)
    verification = _make_verification()
    definition = _make_definition(include_action=True)

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.verify_workflow_runnable",
        side_effect=[verification, verification],
    ) as mock_verify, patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ):
        result = submit_verified_workflow_instance(
            manager=manager,
            workflow_id="#V#candidate_workflow",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            namespace="#V#user_alice/#V#org_nao",
            inputs={"seed": "abc-123"},
            source_event_type="task.created",
            source_event_id="task-1",
            event_idempotency_key="evt:task.created:task-1",
        )

    assert result.success is True
    assert result.instance_id == "instance-evt-1"
    assert result.created_new is True
    assert result.status == "pending"
    assert result.verification["runnable_verification_success"] is True
    manager.create_instance_for_event.assert_called_once()
    manager.create_instance.assert_not_called()
    assert (
        manager.create_instance_for_event.call_args.kwargs["namespace"]
        == "#V#user_alice@org_nao"
    )
    assert mock_verify.call_count == 2


def test_submit_verified_workflow_instance_preserves_idempotent_reuse_without_postflight_failure() -> None:
    manager = MagicMock()
    manager.create_instance_for_event.return_value = ("instance-evt-existing", False)
    verification = _make_verification()
    definition = _make_definition(include_action=True)

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.verify_workflow_runnable",
        return_value=verification,
    ) as mock_verify, patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ):
        result = submit_verified_workflow_instance(
            manager=manager,
            workflow_id="#V#candidate_workflow",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            namespace="#V#user_alice/#V#org_nao",
            inputs={"seed": "abc-123"},
            source_event_type="task.created",
            source_event_id="task-1",
            event_idempotency_key="evt:task.created:task-1",
        )

    assert result.success is True
    assert result.instance_id == "instance-evt-existing"
    assert result.created_new is False
    assert result.status == "reused"
    assert result.verification["postflight_passed"] is True
    manager.create_instance_for_event.assert_called_once()
    manager.create_instance.assert_not_called()
    manager.mark_failed.assert_not_called()
    assert (
        manager.create_instance_for_event.call_args.kwargs["namespace"]
        == "#V#user_alice@org_nao"
    )
    mock_verify.assert_called_once_with(
        "#V#candidate_workflow",
        action_registry_override=None,
        actor_user_id="#V#user_alice",
        actor_org_id="#V#org_nao",
    )


def test_submit_verified_workflow_instance_rejects_unresolvable_namespace() -> None:
    manager = MagicMock()

    result = submit_verified_workflow_instance(
        manager=manager,
        workflow_id="#V#candidate_workflow",
        user_id="user-1",
        org_id="org-1",
        namespace="user-1/org-1",
        inputs={"seed": "abc-123"},
    )

    assert result.success is False
    assert result.status == "rejected_preflight"
    assert result.error_code == "invalid_namespace"
    manager.create_instance_for_event.assert_not_called()
    manager.create_instance.assert_not_called()


def test_submit_verified_workflow_instance_supports_user_only_namespace() -> None:
    manager = MagicMock()
    manager.create_instance.return_value = "instance-user-only-1"
    verification = _make_verification()
    definition = _make_definition(include_action=True)

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.verify_workflow_runnable",
        side_effect=[verification, verification],
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ):
        result = submit_verified_workflow_instance(
            manager=manager,
            workflow_id="#V#candidate_workflow",
            user_id="#V#user_alice",
            org_id=None,
            namespace="#V#user_alice",
            inputs={"seed": "abc-123"},
        )

    assert result.success is True
    assert result.instance_id == "instance-user-only-1"
    assert result.status == "pending"
    manager.create_instance_for_event.assert_not_called()
    manager.create_instance.assert_called_once()
    assert manager.create_instance.call_args.kwargs["user_id"] == "#V#user_alice"
    assert manager.create_instance.call_args.kwargs["org_id"] is None
    assert manager.create_instance.call_args.kwargs["namespace"] == "#V#user_alice"


def test_submit_verified_workflow_instance_applies_launch_input_contract() -> None:
    manager = MagicMock()
    manager.create_instance.return_value = "instance-1"
    verification = _make_verification()
    definition = _make_definition(
        include_action=True,
        metadata={
            "launch_input_contract": {
                "schema_version": "workflow_launch_input_contract.v1",
                "input_mappings": [
                    {
                        "target_context_key": "prompt_text",
                        "source_expression": "inputs.prompt",
                        "extractor": "identity",
                        "required": True,
                    }
                ],
            },
            "launch_input_contract_source": "test_contract",
        },
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.verify_workflow_runnable",
        side_effect=[verification, verification],
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ):
        result = submit_verified_workflow_instance(
            manager=manager,
            workflow_id="#V#candidate_workflow",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            namespace="#V#user_alice/#V#org_nao",
            inputs={"prompt": "Represent this paper."},
        )

    assert result.success is True
    assert result.status == "pending"
    create_inputs = manager.create_instance.call_args.kwargs["inputs"]
    assert create_inputs["prompt"] == "Represent this paper."
    assert create_inputs["prompt_text"] == "Represent this paper."
    resolution = create_inputs["workflow_launch_input_resolution"]
    assert resolution["status"] == "resolved"
    assert resolution["contract_source"] == "test_contract"
    assert resolution["resolved_inputs"] == ["prompt_text"]
    assert (
        result.verification["workflow_launch_input_resolution"]["resolved_inputs"]
        == ["prompt_text"]
    )


def test_submit_verified_workflow_instance_rejects_unresolved_required_launch_input() -> None:
    manager = MagicMock()
    verification = _make_verification()
    definition = _make_definition(
        include_action=True,
        metadata={
            "launch_input_contract": {
                "schema_version": "workflow_launch_input_contract.v1",
                "input_mappings": [
                    {
                        "target_context_key": "prompt_text",
                        "source_expression": "inputs.prompt",
                        "extractor": "identity",
                        "required": True,
                    }
                ],
            },
            "launch_input_contract_source": "test_contract",
        },
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.verify_workflow_runnable",
        return_value=verification,
    ), patch(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ):
        result = submit_verified_workflow_instance(
            manager=manager,
            workflow_id="#V#candidate_workflow",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            namespace="#V#user_alice/#V#org_nao",
            inputs={"seed": "abc-123"},
        )

    assert result.success is False
    assert result.status == "rejected_launch_input_contract"
    assert result.error_code == "workflow_launch_input_resolution_failed"
    launch_resolution = result.verification["workflow_launch_input_resolution"]
    assert launch_resolution["status"] == "failed"
    assert launch_resolution["unresolved_required_inputs"] == ["prompt_text"]
    manager.create_instance_for_event.assert_not_called()
    manager.create_instance.assert_not_called()
