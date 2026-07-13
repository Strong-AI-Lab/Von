from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.backend.security import access_control
from src.backend.workflows.durable.registry_factory import (
    WorkflowDefinitionAuthorityTransientError,
    register_workflow_from_vontology,
    resolve_workflow_definition_from_authority,
)
from src.backend.workflows.engine import WorkflowDefinition, WorkflowStateSpec
from src.backend.workflows.workflow_registry import (
    WorkflowRegistration,
    WorkflowRegistry,
)


def _make_definition(*, authority_probe: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                terminal=True,
            )
        },
        termination_states=("#V#start",),
        metadata={"authority_probe": authority_probe},
    )


def _owner_warmed_registry() -> tuple[WorkflowRegistry, WorkflowDefinition]:
    cached_definition = _make_definition(authority_probe="owner_warmed_cache")
    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(
            workflow_id=cached_definition.workflow_id,
            definition=cached_definition,
            source="vontology",
        )
    )
    return registry, cached_definition


def test_authority_resolver_reloads_vontology_definition_for_each_actor_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry, cached_definition = _owner_warmed_registry()
    observed_actors: list[tuple[str | None, str | None]] = []

    def _actor_scoped_loader(_workflow_id: str) -> WorkflowDefinition | None:
        actor = (
            access_control.get_effective_user_concept_id(),
            access_control.get_effective_organisation_concept_id(),
        )
        observed_actors.append(actor)
        if actor[1] == "#V#trusted_org":
            return _make_definition(authority_probe="actor_scoped_vontology")
        return None

    monkeypatch.setattr(registry_factory, "_is_agent_test_instance", lambda: False)
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        _actor_scoped_loader,
    )

    owner_resolution = resolve_workflow_definition_from_authority(
        "#V#candidate_workflow",
        registry=registry,
        use_current_shared_registry=False,
        actor_user_id="#V#owner",
        actor_org_id="#V#trusted_org",
    )
    cohort_resolution = resolve_workflow_definition_from_authority(
        "#V#candidate_workflow",
        registry=registry,
        use_current_shared_registry=False,
        actor_user_id="#V#cohort_member",
        actor_org_id="#V#trusted_org",
    )

    assert observed_actors == [
        ("#V#owner", "#V#trusted_org"),
        ("#V#cohort_member", "#V#trusted_org"),
    ]
    assert owner_resolution.success is True
    assert cohort_resolution.success is True
    assert owner_resolution.definition is not cohort_resolution.definition
    assert owner_resolution.definition is not cached_definition
    assert cohort_resolution.definition is not cached_definition
    assert owner_resolution.definition_identity == cohort_resolution.definition_identity
    assert owner_resolution.definition_identity is not None
    assert owner_resolution.definition_identity.get("definition_hash")
    assert owner_resolution.known_workflow_ids == ()
    assert cohort_resolution.known_workflow_ids == ()
    owner_diagnostics = owner_resolution.diagnostics or {}
    assert owner_diagnostics.get("actor_scoped_authoritative_load_attempted") is True
    assert owner_diagnostics.get("actor_scoped_authoritative_load_success") is True
    assert owner_diagnostics.get("shared_registry_definition_trusted") is False


def test_authority_resolver_denies_outsider_after_owner_warms_shared_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry, _cached_definition = _owner_warmed_registry()
    loader = MagicMock(return_value=None)
    monkeypatch.setattr(registry_factory, "_is_agent_test_instance", lambda: False)
    monkeypatch.setattr(
        registry_factory,
        "describe_concept_access",
        lambda _concept_id: {
            "exists": True,
            "accessible": False,
            "access_control_enforced": True,
            "restriction_families_present": ["specific_to_user"],
            "specific_to_user_restricted": True,
            "specific_to_org_restricted": False,
        },
    )
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        loader,
    )

    resolution = resolve_workflow_definition_from_authority(
        "#V#candidate_workflow",
        registry=registry,
        use_current_shared_registry=False,
        actor_user_id="#V#outsider",
        actor_org_id="#V#untrusted_org",
    )

    assert resolution.success is False
    assert resolution.definition is None
    assert resolution.registration is None
    assert resolution.definition_identity is None
    assert resolution.known_workflow_ids == ()
    assert resolution.error_code == "workflow_concept_not_accessible"
    assert (resolution.diagnostics or {}).get("workflow_access") == {
        "checked": True,
        "accessible": False,
        "access_control_enforced": True,
    }
    loader.assert_called_once_with("#V#candidate_workflow")


def test_authority_resolver_does_not_trust_owner_warmed_jit_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    cached_definition = _make_definition(authority_probe="owner_warmed_jit_cache")
    registry = WorkflowRegistry()
    registry.register(
        WorkflowRegistration(
            workflow_id=cached_definition.workflow_id,
            definition=cached_definition,
            source="jit_discovery",
        )
    )
    loader = MagicMock(return_value=None)
    monkeypatch.setattr(registry_factory, "_is_agent_test_instance", lambda: False)
    monkeypatch.setattr(
        registry_factory,
        "describe_concept_access",
        lambda _concept_id: {
            "exists": True,
            "accessible": False,
            "access_control_enforced": True,
            "restriction_families_present": ["specific_to_org"],
        },
    )
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        loader,
    )

    resolution = resolve_workflow_definition_from_authority(
        cached_definition.workflow_id,
        registry=registry,
        use_current_shared_registry=False,
        actor_user_id="#V#outsider",
        actor_org_id="#V#untrusted_org",
    )

    assert resolution.success is False
    assert resolution.definition is None
    assert resolution.error_code == "workflow_concept_not_accessible"
    assert resolution.known_workflow_ids == ()
    assert (resolution.diagnostics or {}).get("shared_registry_definition_trusted") is False
    loader.assert_called_once_with(cached_definition.workflow_id)


def test_authority_resolver_denies_anonymous_request_after_owner_warms_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry, _cached_definition = _owner_warmed_registry()
    monkeypatch.setattr(registry_factory, "_is_agent_test_instance", lambda: False)
    monkeypatch.setattr(
        registry_factory,
        "should_enforce_access_control",
        lambda: True,
    )
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        lambda _workflow_id: None,
    )
    monkeypatch.setattr(
        registry_factory,
        "describe_concept_access",
        lambda _concept_id: {
            "exists": True,
            "accessible": False,
            "access_control_enforced": True,
            "restriction_families_present": ["specific_to_org"],
            "specific_to_user_restricted": False,
            "specific_to_org_restricted": True,
        },
    )

    resolution = resolve_workflow_definition_from_authority(
        "#V#candidate_workflow",
        registry=registry,
        use_current_shared_registry=False,
    )

    assert resolution.success is False
    assert resolution.definition is None
    assert resolution.error_code == "workflow_concept_not_accessible"
    assert resolution.known_workflow_ids == ()
    assert (resolution.diagnostics or {}).get("actor_scoped_authority_required") is True


def test_authority_resolver_rejects_actor_incomplete_graph_instead_of_cached_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry, _cached_definition = _owner_warmed_registry()
    monkeypatch.setattr(registry_factory, "_is_agent_test_instance", lambda: False)
    monkeypatch.setattr(
        registry_factory,
        "describe_concept_access",
        lambda _concept_id: {
            "exists": True,
            "accessible": True,
            "access_control_enforced": True,
            "restriction_families_present": [],
        },
    )
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        lambda _workflow_id: None,
    )

    resolution = resolve_workflow_definition_from_authority(
        "#V#candidate_workflow",
        registry=registry,
        use_current_shared_registry=False,
        actor_user_id="#V#actor",
        actor_org_id="#V#trusted_org",
    )

    assert resolution.success is False
    assert resolution.definition is None
    assert resolution.definition_identity is None
    assert resolution.known_workflow_ids == ()
    assert resolution.error_code == "workflow_definition_not_loadable_for_actor"
    assert (resolution.diagnostics or {}).get(
        "actor_scoped_authoritative_load_success"
    ) is False


def test_authority_resolver_preserves_transient_actor_store_failure_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry, _cached_definition = _owner_warmed_registry()
    monkeypatch.setattr(registry_factory, "_is_agent_test_instance", lambda: False)

    def _transient_loader(_workflow_id: str) -> WorkflowDefinition | None:
        raise RuntimeError("server selection timeout while loading workflow graph")

    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        _transient_loader,
    )

    with pytest.raises(
        WorkflowDefinitionAuthorityTransientError,
        match="workflow_definition_authority temporarily unavailable",
    ):
        resolve_workflow_definition_from_authority(
            "#V#candidate_workflow",
            registry=registry,
            use_current_shared_registry=False,
            actor_user_id="#V#actor",
            actor_org_id="#V#trusted_org",
        )


def test_actor_scoped_registration_cannot_pollute_unpartitioned_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry = WorkflowRegistry()
    loader = MagicMock(
        return_value=_make_definition(authority_probe="actor_scoped_vontology")
    )
    monkeypatch.setattr(registry_factory, "_is_agent_test_instance", lambda: False)
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        loader,
    )

    registered, error_code = register_workflow_from_vontology(
        registry=registry,
        workflow_id="#V#candidate_workflow",
        actor_user_id="#V#actor",
        actor_org_id="#V#trusted_org",
    )

    assert registered is False
    assert error_code == "actor_scoped_registration_requires_authority_resolution"
    assert registry.get("#V#candidate_workflow") is None
    loader.assert_not_called()


def test_durable_server_loader_forwards_and_validates_persisted_actor_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.utils_flask as utils_flask
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry = WorkflowRegistry()
    definition = _make_definition(authority_probe="durable_actor_scope")
    resolution_calls: list[dict[str, object]] = []

    def _resolve(workflow_id: str, **kwargs: object) -> SimpleNamespace:
        resolution_calls.append({"workflow_id": workflow_id, **kwargs})
        return SimpleNamespace(
            registry=registry,
            definition=definition,
            error_code=None,
        )

    monkeypatch.setattr(utils_flask, "_durable_workflow_registry", registry)
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        _resolve,
    )
    loader = utils_flask._get_durable_definition_loader()

    loaded = loader(
        definition.workflow_id,
        actor_user_id="#V#cohort_member",
        actor_org_id="#V#trusted_org",
        actor_namespace="#V#cohort_member@trusted_org",
    )

    assert loaded is definition
    assert resolution_calls[0]["actor_user_id"] == "#V#cohort_member"
    assert resolution_calls[0]["actor_org_id"] == "#V#trusted_org"

    denied = loader(
        definition.workflow_id,
        actor_user_id="#V#outsider",
        actor_org_id="#V#untrusted_org",
        actor_namespace="#V#different_user@untrusted_org",
    )

    assert denied is None
    assert len(resolution_calls) == 1


def test_durable_server_loader_accepts_user_only_namespace_with_persisted_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.utils_flask as utils_flask
    import src.backend.workflows.durable.registry_factory as registry_factory

    registry = WorkflowRegistry()
    definition = _make_definition(authority_probe="durable_actor_scope")
    resolution_calls: list[dict[str, object]] = []

    def _resolve(workflow_id: str, **kwargs: object) -> SimpleNamespace:
        resolution_calls.append({"workflow_id": workflow_id, **kwargs})
        return SimpleNamespace(
            registry=registry,
            definition=definition,
            error_code=None,
        )

    monkeypatch.setattr(utils_flask, "_durable_workflow_registry", registry)
    monkeypatch.setattr(
        registry_factory,
        "resolve_workflow_definition_from_authority",
        _resolve,
    )

    loaded = utils_flask._get_durable_definition_loader()(
        definition.workflow_id,
        actor_user_id="#V#cohort_member",
        actor_org_id="#V#trusted_org",
        actor_namespace="#V#cohort_member",
    )

    assert loaded is definition
    assert resolution_calls[0]["actor_user_id"] == "#V#cohort_member"
    assert resolution_calls[0]["actor_org_id"] == "#V#trusted_org"
