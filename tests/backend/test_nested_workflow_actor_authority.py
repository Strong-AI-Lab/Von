from __future__ import annotations

from unittest.mock import MagicMock

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.durable.nested_workflow_authority import (
    NESTED_WORKFLOW_ACTOR_CONTEXT_MISMATCH,
    NESTED_WORKFLOW_DEFINITION_IDENTITY_MISMATCH,
    resolve_nested_workflow_definition,
)
from src.backend.workflows.durable.registry_factory import (
    WorkflowDefinitionAuthorityResolution,
)
from src.backend.workflows.durable.subworkflow_actions import (
    register_subworkflow_actions,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
    WORKFLOW_CONTROL_ACTION_FORK_ID,
)
from src.backend.workflows.subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
)


WORKFLOW_ID = "#V#actor_scoped_child_workflow"
TRUSTED_ORG_ID = "#V#trusted_org"
OTHER_ORG_ID = "#V#other_org"
OWNER_ID = "#V#owner"
COHORT_ID = "#V#cohort_member"
OUTSIDER_ID = "#V#outsider"


def _definition(action_id: str, *, workflow_id: str = WORKFLOW_ID) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=action_id),),
                terminal=True,
            )
        },
        termination_states=("start",),
    )


def _environment(user_id: str, org_id: str) -> WorkflowEnvironment:
    return WorkflowEnvironment(
        llm_client=None,
        user_namespace=f"{user_id}@{org_id.removeprefix('#V#')}",
        user_concept_id=user_id,
        org_concept_id=org_id,
    )


def _resolution(
    definition: WorkflowDefinition | None,
    *,
    error_code: str | None = None,
) -> WorkflowDefinitionAuthorityResolution:
    return WorkflowDefinitionAuthorityResolution(
        workflow_id=WORKFLOW_ID,
        registry=None,
        registration=None,
        definition=definition,
        registration_source="vontology" if definition is not None else "unknown",
        known_workflow_ids=(),
        error_code=error_code,
        diagnostics={
            "actor_scoped_authority_required": True,
            "shared_registry_definition_trusted": False,
        },
    )


def test_subworkflow_actor_resolution_ignores_owner_warmed_loader_for_every_order(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.owner",
            handler=lambda _request: WorkflowActionResult(
                outputs={"authority_probe": "owner"}
            ),
        )
    )
    registry.register(
        ActionSpec(
            action_id="child.cohort",
            handler=lambda _request: WorkflowActionResult(
                outputs={"authority_probe": "cohort"}
            ),
        )
    )
    owner_definition = _definition("child.owner")
    cohort_definition = _definition("child.cohort")
    warmed_loader = MagicMock(return_value=owner_definition)
    authority_calls: list[dict[str, object]] = []

    def _resolve(_workflow_id: str, **kwargs):
        authority_calls.append(dict(kwargs))
        actor_user_id = kwargs.get("actor_user_id")
        actor_org_id = kwargs.get("actor_org_id")
        if actor_org_id != TRUSTED_ORG_ID:
            return _resolution(None, error_code="workflow_concept_not_accessible")
        if actor_user_id == OWNER_ID:
            return _resolution(owner_definition)
        if actor_user_id == COHORT_ID:
            return _resolution(cohort_definition)
        return _resolution(None, error_code="workflow_concept_not_accessible")

    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        _resolve,
    )
    register_subworkflow_actions(registry, definition_loader=warmed_loader)

    def _invoke(user_id: str, org_id: str):
        return registry.execute(
            WORKFLOW_SUBWORKFLOW_ACTION_ID,
            inputs={
                "workflow_id": WORKFLOW_ID,
                "__parent_workflow_id": "#V#parent",
                "__parent_state_id": "start",
            },
            context={},
            env=_environment(user_id, org_id),
        )

    owner = _invoke(OWNER_ID, TRUSTED_ORG_ID)
    outsider_after_owner = _invoke(OUTSIDER_ID, OTHER_ORG_ID)
    cohort_after_outsider = _invoke(COHORT_ID, TRUSTED_ORG_ID)
    outsider_after_cohort = _invoke(OUTSIDER_ID, OTHER_ORG_ID)

    assert owner.status == "success"
    assert owner.outputs["result"]["authority_probe"] == "owner"
    assert cohort_after_outsider.status == "success"
    assert cohort_after_outsider.outputs["result"]["authority_probe"] == "cohort"
    for denied in (outsider_after_owner, outsider_after_cohort):
        assert denied.status == "failed"
        assert "workflow_concept_not_accessible" in str(denied.error)
        projection = denied.outputs["subworkflow_authority_resolution"]
        assert projection["used_fallback_loader"] is False
    warmed_loader.assert_not_called()
    assert [call["actor_user_id"] for call in authority_calls] == [
        OWNER_ID,
        OUTSIDER_ID,
        COHORT_ID,
        OUTSIDER_ID,
    ]
    assert all("promote_to_registry" not in call for call in authority_calls)


def test_control_flow_for_each_uses_actor_authority_before_warmed_loader(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.cohort",
            handler=lambda request: WorkflowActionResult(
                outputs={"item": request.data.get("current_item")}
            ),
        )
    )
    cohort_definition = _definition("child.cohort")
    warmed_loader = MagicMock(return_value=_definition("child.owner"))

    def _resolve(_workflow_id: str, **kwargs):
        if kwargs.get("actor_org_id") == TRUSTED_ORG_ID:
            return _resolution(cohort_definition)
        return _resolution(None, error_code="workflow_concept_not_accessible")

    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        _resolve,
    )
    register_control_flow_actions(registry, definition_loader=warmed_loader)

    trusted = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={"workflow_id": WORKFLOW_ID, "items": ["A", "B"]},
        context={},
        env=_environment(COHORT_ID, TRUSTED_ORG_ID),
    )
    denied = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={"workflow_id": WORKFLOW_ID, "items": ["A"]},
        context={},
        env=_environment(OUTSIDER_ID, OTHER_ORG_ID),
    )

    assert trusted.status == "success"
    assert trusted.outputs["successful_results"] == [{"item": "A"}, {"item": "B"}]
    assert trusted.outputs["for_each_authority_resolution"][
        "used_fallback_loader"
    ] is False
    assert denied.status == "failed"
    assert "workflow_concept_not_accessible" in str(denied.error)
    warmed_loader.assert_not_called()


def test_control_flow_fork_fails_closed_for_actor_denied_child(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry = ActionRegistry()
    warmed_loader = MagicMock(return_value=_definition("child.owner"))
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        lambda _workflow_id, **_kwargs: _resolution(
            None,
            error_code="workflow_concept_not_accessible",
        ),
    )
    register_control_flow_actions(registry, definition_loader=warmed_loader)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FORK_ID,
        inputs={
            "fork_id": "actor_scope",
            "branches": [{"branch_id": "denied", "workflow_id": WORKFLOW_ID}],
        },
        context={},
        env=_environment(OUTSIDER_ID, OTHER_ORG_ID),
    )

    assert result.status == "failed"
    branch = result.outputs["fork_branch_results"][0]
    assert "workflow_concept_not_accessible" in branch["error"]
    assert branch["authority_resolution"]["used_fallback_loader"] is False
    warmed_loader.assert_not_called()


def test_nested_resolution_rejects_namespace_actor_mismatch_before_any_loader(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    authority_resolver = MagicMock()
    fallback_loader = MagicMock(return_value=_definition("child.owner"))
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        authority_resolver,
    )

    resolution = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=f"{OWNER_ID}@trusted_org",
            user_concept_id=OUTSIDER_ID,
            org_concept_id=TRUSTED_ORG_ID,
        ),
        fallback_loader=fallback_loader,
    )

    assert resolution.success is False
    assert resolution.error_code == NESTED_WORKFLOW_ACTOR_CONTEXT_MISMATCH
    assert resolution.actor_context.mismatch_fields == ("user_id",)
    authority_resolver.assert_not_called()
    fallback_loader.assert_not_called()


def test_nested_resolution_accepts_user_only_namespace_with_parent_org(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    definition = _definition("child.cohort")
    fallback_loader = MagicMock(return_value=_definition("child.owner"))
    authority_resolver = MagicMock(return_value=_resolution(definition))
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        authority_resolver,
    )

    resolution = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace=COHORT_ID,
            user_concept_id=COHORT_ID,
            org_concept_id=TRUSTED_ORG_ID,
        ),
        fallback_loader=fallback_loader,
    )

    assert resolution.success is True
    assert resolution.definition is definition
    assert resolution.actor_context.namespace == COHORT_ID
    assert resolution.actor_context.org_id == TRUSTED_ORG_ID
    authority_resolver.assert_called_once_with(
        WORKFLOW_ID,
        use_current_shared_registry=True,
        register_authoritative_fallback=True,
        actor_user_id=COHORT_ID,
        actor_org_id=TRUSTED_ORG_ID,
    )
    fallback_loader.assert_not_called()


def test_nested_resolution_rejects_wrong_definition_identity_without_fallback(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    fallback_loader = MagicMock(return_value=_definition("child.owner"))
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        lambda _workflow_id, **_kwargs: _resolution(
            _definition("child.owner", workflow_id="#V#wrong_child")
        ),
    )

    resolution = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=_environment(OWNER_ID, TRUSTED_ORG_ID),
        fallback_loader=fallback_loader,
    )

    assert resolution.success is False
    assert resolution.error_code == NESTED_WORKFLOW_DEFINITION_IDENTITY_MISMATCH
    fallback_loader.assert_not_called()


def test_nested_resolution_caches_success_for_same_environment_and_scope(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    definition = _definition("child.cohort")
    authority_resolver = MagicMock(return_value=_resolution(definition))
    fallback_loader = MagicMock(return_value=_definition("child.owner"))
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        authority_resolver,
    )
    environment = _environment(COHORT_ID, TRUSTED_ORG_ID)

    first = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=environment,
        fallback_loader=fallback_loader,
    )
    second = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=environment,
        fallback_loader=fallback_loader,
    )

    assert first.success is True
    assert second is first
    authority_resolver.assert_called_once_with(
        WORKFLOW_ID,
        use_current_shared_registry=True,
        register_authoritative_fallback=True,
        actor_user_id=COHORT_ID,
        actor_org_id=TRUSTED_ORG_ID,
    )
    assert tuple(environment._nested_workflow_resolution_cache) == (
        (
            WORKFLOW_ID,
            COHORT_ID,
            TRUSTED_ORG_ID,
            first.actor_context.namespace,
        ),
    )
    fallback_loader.assert_not_called()


def test_nested_resolution_cache_is_not_reused_across_actor_scopes(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    owner_definition = _definition("child.owner")
    cohort_definition = _definition("child.cohort")
    authority_calls: list[tuple[object, object]] = []

    def _resolve(_workflow_id: str, **kwargs):
        actor_scope = (
            kwargs.get("actor_user_id"),
            kwargs.get("actor_org_id"),
        )
        authority_calls.append(actor_scope)
        if actor_scope == (OWNER_ID, TRUSTED_ORG_ID):
            return _resolution(owner_definition)
        if actor_scope == (COHORT_ID, TRUSTED_ORG_ID):
            return _resolution(cohort_definition)
        return _resolution(None, error_code="workflow_concept_not_accessible")

    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        _resolve,
    )
    owner_environment = _environment(OWNER_ID, TRUSTED_ORG_ID)
    cohort_environment = _environment(COHORT_ID, TRUSTED_ORG_ID)

    owner = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=owner_environment,
        fallback_loader=None,
    )
    cohort = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=cohort_environment,
        fallback_loader=None,
    )

    assert owner.definition is owner_definition
    assert cohort.definition is cohort_definition
    assert authority_calls == [
        (OWNER_ID, TRUSTED_ORG_ID),
        (COHORT_ID, TRUSTED_ORG_ID),
    ]
    assert owner_environment._nested_workflow_resolution_cache is not (
        cohort_environment._nested_workflow_resolution_cache
    )


def test_nested_resolution_does_not_cache_authority_failure(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    definition = _definition("child.cohort")
    authority_resolver = MagicMock(
        side_effect=[
            _resolution(None, error_code="workflow_concept_not_accessible"),
            _resolution(definition),
        ]
    )
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        authority_resolver,
    )
    environment = _environment(COHORT_ID, TRUSTED_ORG_ID)

    denied = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=environment,
        fallback_loader=None,
    )
    accepted = resolve_nested_workflow_definition(
        workflow_id=WORKFLOW_ID,
        environment=environment,
        fallback_loader=None,
    )

    assert denied.success is False
    assert denied.error_code == "workflow_concept_not_accessible"
    assert accepted.success is True
    assert accepted.definition is definition
    assert authority_resolver.call_count == 2
