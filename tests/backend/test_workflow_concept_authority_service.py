from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
)


class _DummyRegistry:
    def __init__(self, workflow_ids: list[str]) -> None:
        self._workflow_ids = workflow_ids
        self._registrations = {
            workflow_id: SimpleNamespace(
                workflow_id=workflow_id,
                source="built_in",
                purpose=f"Purpose for {workflow_id}",
            )
            for workflow_id in workflow_ids
        }

    def all_workflow_ids(self):
        return list(self._workflow_ids)

    def get_registration(self, workflow_id: str):
        return self._registrations.get(workflow_id)


def test_workflow_authority_report_classifies_missing_and_untyped(monkeypatch):
    registry = _DummyRegistry(
        workflow_ids=["#V#wf_missing", "#V#wf_untyped", "#V#wf_valid"]
    )

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow", "#V#durable_workflow"),
    )

    def _mock_load_concept(concept_id: str):
        if concept_id == "#V#wf_missing":
            return None, None
        if concept_id == "#V#wf_untyped":
            return {
                "concept_id": concept_id,
                "relationships": {"is_an_instance_of": ["#V#other_type"]},
            }, None
        if concept_id == "#V#wf_valid":
            return {
                "concept_id": concept_id,
                "relationships": {"is_an_instance_of": ["#V#durable_workflow"]},
            }, None
        return None, None

    monkeypatch.setattr(authority_service, "_load_concept", _mock_load_concept)

    report = authority_service.build_workflow_concept_authority_report(
        registry=cast(Any, registry)
    )

    assert report["drift_detected"] is True
    assert report["counts"]["missing_concepts"] == 1
    assert report["counts"]["missing_required_type"] == 1
    assert report["counts"]["valid"] == 1
    assert "#V#wf_missing" in report["missing_concept_workflow_ids"]
    assert "#V#wf_untyped" in report["missing_required_type_by_workflow_id"]


def test_workflow_authority_report_accepts_required_type_via_subtype(monkeypatch):
    registry = _DummyRegistry(workflow_ids=["#V#wf_durable"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )

    def _mock_load_concept(concept_id: str):
        if concept_id == "#V#wf_durable":
            return {
                "concept_id": concept_id,
                "relationships": {"is_an_instance_of": ["#V#durable_workflow"]},
            }, None
        if concept_id == "#V#durable_workflow":
            return {
                "concept_id": concept_id,
                "relationships": {"is_a_type_of": ["#V#ai_workflow"]},
            }, None
        return None, None

    monkeypatch.setattr(authority_service, "_load_concept", _mock_load_concept)

    report = authority_service.build_workflow_concept_authority_report(
        registry=cast(Any, registry)
    )

    assert report["drift_detected"] is False
    assert report["counts"]["missing_required_type"] == 0
    assert report["valid_workflow_ids"] == ["#V#wf_durable"]


def test_bootstrap_workflow_concepts_creates_missing(monkeypatch):
    registry = _DummyRegistry(workflow_ids=["#V#wf_create"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )
    monkeypatch.setattr(authority_service, "_load_concept", lambda _cid: (None, None))

    created_payloads: list[dict] = []

    def _mock_create_concept(**kwargs):
        created_payloads.append(kwargs)
        return {"concept_id": kwargs.get("concept_id")}

    monkeypatch.setattr(
        authority_service.concept_service,
        "create_concept",
        _mock_create_concept,
    )

    report = authority_service.bootstrap_workflow_concepts(
        registry=cast(Any, registry)
    )

    assert report["counts"]["created"] == 1
    assert report["created_workflow_ids"] == ["#V#wf_create"]
    assert created_payloads
    payload = next(
        item for item in created_payloads if item.get("concept_id") == "#V#wf_create"
    )
    assert payload["concept_id"] == "#V#wf_create"
    assert payload["parent_concept_ids"] == ["#V#ai_workflow"]
    assert payload["create_as_instance"] is True


def test_bootstrap_workflow_concepts_enforces_required_type(monkeypatch):
    registry = _DummyRegistry(workflow_ids=["#V#wf_retype"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )
    monkeypatch.setattr(
        authority_service,
        "_load_concept",
        lambda _cid: (
            {
                "concept_id": "#V#wf_retype",
                "relationships": {"is_an_instance_of": ["#V#legacy_type"]},
            },
            None,
        ),
    )

    update_payloads: list[dict] = []

    def _mock_update_concept(concept_id: str, payload: dict):
        update_payloads.append({"concept_id": concept_id, "payload": payload})
        return {"concept_id": concept_id, "relationships": payload["relationships"]}

    monkeypatch.setattr(
        authority_service.concept_service,
        "update_concept",
        _mock_update_concept,
    )

    report = authority_service.bootstrap_workflow_concepts(
        registry=cast(Any, registry)
    )

    assert report["counts"]["updated"] == 1
    assert report["updated_workflow_ids"] == ["#V#wf_retype"]
    assert update_payloads
    payload = update_payloads[0]["payload"]
    assert "#V#ai_workflow" in payload["relationships"]["is_an_instance_of"]


def test_bootstrap_workflow_concepts_preserves_valid_subtype_typing(monkeypatch):
    registry = _DummyRegistry(workflow_ids=["#V#wf_durable"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )

    def _mock_load_concept(concept_id: str):
        if concept_id == "#V#wf_durable":
            return {
                "concept_id": concept_id,
                "relationships": {"is_an_instance_of": ["#V#durable_workflow"]},
            }, None
        if concept_id == "#V#durable_workflow":
            return {
                "concept_id": concept_id,
                "relationships": {"is_a_type_of": ["#V#ai_workflow"]},
            }, None
        return None, None

    monkeypatch.setattr(authority_service, "_load_concept", _mock_load_concept)

    monkeypatch.setattr(
        authority_service.concept_service,
        "update_concept",
        lambda *args, **kwargs: pytest.fail("update_concept should not be called"),
    )

    report = authority_service.bootstrap_workflow_concepts(
        registry=cast(Any, registry)
    )

    assert report["counts"]["updated"] == 0
    assert report["unchanged_workflow_ids"] == ["#V#wf_durable"]


@pytest.fixture
def _reset_mock_workflow_graph_db(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db
    from src.backend.services.workflow_discovery_service import (
        invalidate_workflow_discovery_executability_caches,
    )

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    invalidate_workflow_discovery_executability_caches()
    yield
    invalidate_workflow_discovery_executability_caches()
    authority_service.clear_workflow_type_resolution_cache()


def test_bootstrap_publishes_canonical_chat_graphs_with_loader_runtime_parity(
    _reset_mock_workflow_graph_db,
):
    from src.backend.services.workflow_discovery_service import (
        EXECUTABILITY_EXECUTABLE_NOW,
        classify_workflow_concept_executability,
        invalidate_workflow_discovery_executability_caches,
    )
    from src.backend.workflows.durable.registry_factory import (
        build_workflow_registry_read_only,
    )
    from src.backend.workflows.vontology_loader import (
        build_workflow_process_graph,
        load_workflow_definition_from_vontology,
    )

    registry = build_workflow_registry_read_only()
    expected_published_ids = {
        workflow_id
        for workflow_id in (
            *authority_service.CANONICAL_CHAT_WORKFLOW_IDS,
            *authority_service.CANONICAL_DURABLE_WORKFLOW_IDS,
        )
        if registry.get_registration(workflow_id) is not None
    }

    report = authority_service.bootstrap_workflow_concepts(registry=cast(Any, registry))
    graph_publication = report.get("graph_publication") or {}
    counts = graph_publication.get("counts") or {}
    assert counts.get("workflows_published") == len(expected_published_ids)
    assert counts.get("errors") == 0

    invalidate_workflow_discovery_executability_caches()

    for workflow_id in sorted(expected_published_ids):
        graph, warnings = build_workflow_process_graph(workflow_id)
        assert isinstance(graph, dict), f"{workflow_id}: graph_missing {warnings}"
        assert warnings == []

        steps = graph.get("steps")
        assert isinstance(steps, list)
        assert len(steps) > 0
        step_ids = {
            step.get("step_id")
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("step_id"), str)
        }
        assert graph.get("initial_step") in step_ids

        loaded_definition = load_workflow_definition_from_vontology(workflow_id)
        assert loaded_definition is not None
        built_in_definition = registry.get(workflow_id)
        assert built_in_definition is not None

        expected_initial_state = authority_service._step_concept_id(
            workflow_id=workflow_id,
            state_id=built_in_definition.initial_state,
        )
        assert loaded_definition.initial_state == expected_initial_state

        expected_loaded_state_ids = {
            authority_service._step_concept_id(
                workflow_id=workflow_id,
                state_id=state_id,
            )
            for state_id in built_in_definition.states.keys()
        }
        assert set(loaded_definition.states.keys()) == expected_loaded_state_ids

        for state_id, built_state in built_in_definition.states.items():
            loaded_state_id = authority_service._step_concept_id(
                workflow_id=workflow_id,
                state_id=state_id,
            )
            loaded_state = loaded_definition.states[loaded_state_id]
            assert tuple(action.action_id for action in loaded_state.actions) == tuple(
                action.action_id for action in built_state.actions
            )
            assert [dict(action.inputs) for action in loaded_state.actions] == [
                dict(action.inputs) for action in built_state.actions
            ]
            assert {transition.to_state for transition in loaded_state.transitions} == {
                authority_service._step_concept_id(
                    workflow_id=workflow_id,
                    state_id=transition.to_state,
                )
                for transition in built_state.transitions
            }

        is_executable, reason, detail = classify_workflow_concept_executability(
            workflow_id
        )
        assert is_executable is True, f"{workflow_id}: {reason}: {detail}"
        assert reason == EXECUTABILITY_EXECUTABLE_NOW


def test_canonical_workflow_runtime_identity_matches_authoritative_loader(
    _reset_mock_workflow_graph_db,
):
    from src.backend.workflows.definitions import register_default_workflows
    from src.backend.workflows.vontology_loader import (
        load_workflow_definition_from_vontology,
    )
    from src.backend.workflows.workflow_registry import WorkflowRegistry

    registry = WorkflowRegistry()
    register_default_workflows(registry)
    authority_service.bootstrap_workflow_concepts(registry=cast(Any, registry))

    from src.backend.workflows.durable.registry_factory import build_workflow_registry_read_only

    runtime_registry = build_workflow_registry_read_only()

    for workflow_id in authority_service.CANONICAL_CHAT_WORKFLOW_IDS:
        registration = runtime_registry.get_registration(workflow_id)
        assert registration is not None
        assert str(registration.source or "").strip().lower() == "vontology"

        authoritative_definition = load_workflow_definition_from_vontology(workflow_id)
        assert authoritative_definition is not None

        identity = build_workflow_definition_identity(
            workflow_id=workflow_id,
            source=registration.source,
            definition=registration.definition,
            authoritative_definition=authoritative_definition,
        )
        assert identity["runtime_definition_hash"]
        assert identity["authoritative_definition_hash"]
        assert identity["hash_mismatch"] is False


def test_publish_canonical_graphs_reports_validation_failures(
    _reset_mock_workflow_graph_db,
    monkeypatch,
):
    from src.backend.workflows.definitions import register_default_workflows
    from src.backend.workflows.workflow_registry import WorkflowRegistry

    registry = WorkflowRegistry()
    register_default_workflows(registry)
    authority_service.bootstrap_workflow_concepts(registry=cast(Any, registry))

    monkeypatch.setattr(
        authority_service,
        "validate_workflow_definition_contract",
        lambda **_kwargs: {
            "valid": False,
            "errors": ["workflow_control_flow_incomplete"],
            "unsupported_action_ids": [],
            "missing_transition_state_ids": ["#V#state"],
            "unknown_transition_targets": [],
            "vacuous_state_ids": [],
            "unresolved_input_mapping_states": [],
            "invalid_input_mapping_specs": [],
            "unresolved_output_mapping_states": [],
            "invalid_output_mapping_specs": [],
        },
    )

    report = authority_service.publish_canonical_chat_workflow_graphs(
        registry=cast(Any, registry)
    )
    counts = report.get("counts") or {}
    assert counts.get("validation_failures") == len(
        authority_service.CANONICAL_CHAT_WORKFLOW_IDS
    )
    assert counts.get("workflows_published") == 0
    validation_failures = report.get("validation_failures_by_workflow_id") or {}
    assert validation_failures
    errors = report.get("errors_by_workflow_id") or {}
    assert all(
        str(errors.get(workflow_id, "")).startswith("publication_validation_failed:")
        for workflow_id in authority_service.CANONICAL_CHAT_WORKFLOW_IDS
    )
