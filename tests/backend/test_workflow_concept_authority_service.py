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


def test_bootstrap_workflow_concept_identities_creates_missing(monkeypatch):
    registry = _DummyRegistry(workflow_ids=["#V#wf_create"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )
    monkeypatch.setattr(
        authority_service,
        "_concept_exists_fast",
        lambda concept_id: concept_id == "#V#ai_workflow",
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

    report = authority_service.bootstrap_workflow_concept_identities(
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


def test_bootstrap_workflow_concept_identities_skips_missing_parent_types(
    monkeypatch,
):
    registry = _DummyRegistry(workflow_ids=["#V#wf_create"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )
    monkeypatch.setattr(authority_service, "_concept_exists_fast", lambda _cid: False)
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

    report = authority_service.bootstrap_workflow_concept_identities(
        registry=cast(Any, registry)
    )

    assert report["counts"]["created"] == 1
    payload = next(
        item for item in created_payloads if item.get("concept_id") == "#V#wf_create"
    )
    assert payload["parent_concept_ids"] == []


def test_bootstrap_workflow_concepts_can_skip_optional_side_effects(monkeypatch):
    registry = _DummyRegistry(workflow_ids=["#V#wf_create"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )
    monkeypatch.setattr(authority_service, "_load_concept", lambda _cid: (None, None))
    monkeypatch.setattr(
        authority_service.concept_service,
        "create_concept",
        lambda **kwargs: {"concept_id": kwargs.get("concept_id")},
    )
    monkeypatch.setattr(
        authority_service,
        "ensure_effort_unit_ontology",
        lambda: pytest.fail("ensure_effort_unit_ontology should not be called"),
    )
    monkeypatch.setattr(
        authority_service,
        "publish_canonical_chat_workflow_graphs",
        lambda **_kwargs: pytest.fail(
            "publish_canonical_chat_workflow_graphs should not be called"
        ),
    )

    report = authority_service.bootstrap_workflow_concepts(
        registry=cast(Any, registry),
        ensure_effort_unit_ontology_bootstrap=False,
        publish_canonical_graphs=False,
    )

    assert report["counts"]["created"] == 1
    assert report["created_workflow_ids"] == ["#V#wf_create"]
    assert report["effort_unit_ontology"]["attempted"] is False
    assert report["effort_unit_ontology"]["reason"] == "disabled"
    assert report["graph_publication"]["attempted"] is False
    assert report["graph_publication"]["reason"] == "disabled"
    assert report["graph_publication"]["counts"]["workflows_targeted"] == 0


def test_ensure_concept_exists_recovers_from_duplicate_key(monkeypatch):
    load_results = iter(
        [
            (None, None),
            ({"concept_id": "#V#duplicate_concept"}, None),
        ]
    )

    monkeypatch.setattr(
        authority_service,
        "_load_concept",
        lambda _concept_id: next(load_results),
    )

    def _raise_duplicate(**_kwargs):
        raise RuntimeError("E11000 duplicate key error collection: von_db.concepts")

    monkeypatch.setattr(
        authority_service.concept_service,
        "create_concept",
        _raise_duplicate,
    )

    concept_doc, created, error = authority_service._ensure_concept_exists(
        concept_id="#V#duplicate_concept",
        name="Duplicate concept",
    )

    assert concept_doc == {"concept_id": "#V#duplicate_concept"}
    assert created is False
    assert error is None


def test_ensure_tool_output_mapping_concept_recovers_from_duplicate_key(monkeypatch):
    mapping_id = "#V#workflow_mapping_tool_field_test_to_parent_output"
    load_results = iter(
        [
            (None, None),
            (
                {
                    "concept_id": mapping_id,
                    "concept_data": {},
                },
                None,
            ),
        ]
    )

    monkeypatch.setattr(
        authority_service,
        "_load_concept",
        lambda _concept_id: next(load_results),
    )

    def _raise_duplicate(**_kwargs):
        raise RuntimeError("E11000 duplicate key error collection: von_db.concepts")

    monkeypatch.setattr(
        authority_service.concept_service,
        "create_concept",
        _raise_duplicate,
    )
    monkeypatch.setattr(
        authority_service.concept_service,
        "update_concept",
        lambda *_args, **_kwargs: {"updated": True},
    )

    created, error = authority_service._ensure_tool_output_mapping_concept(
        step_concept_id="#V#workflow_step_test",
        mapping_target_id="#V#child_workflow",
        mapping_spec=authority_service._CanonicalToolOutputMappingSpec(
            concept_id=mapping_id,
            tool_output_field="result.test",
            context_key="parent_output",
        ),
    )

    assert created is False
    assert error is None


def test_ensure_context_input_mapping_concept_recovers_from_duplicate_key(monkeypatch):
    mapping_id = "#V#workflow_mapping_context_key_test_to_tool_parameter"
    load_results = iter(
        [
            (None, None),
            (
                {
                    "concept_id": mapping_id,
                    "concept_data": {},
                },
                None,
            ),
        ]
    )

    monkeypatch.setattr(
        authority_service,
        "_load_concept",
        lambda _concept_id: next(load_results),
    )

    def _raise_duplicate(**_kwargs):
        raise RuntimeError("E11000 duplicate key error collection: von_db.concepts")

    monkeypatch.setattr(
        authority_service.concept_service,
        "create_concept",
        _raise_duplicate,
    )
    monkeypatch.setattr(
        authority_service.concept_service,
        "update_concept",
        lambda *_args, **_kwargs: {"updated": True},
    )

    created, error = authority_service._ensure_context_input_mapping_concept(
        step_concept_id="#V#workflow_step_test",
        mapping_target_id="#V#child_workflow",
        mapping_spec=authority_service._CanonicalContextInputMappingSpec(
            concept_id=mapping_id,
            context_key="test_context_key",
            tool_param="workflow_id",
        ),
    )

    assert created is False
    assert error is None


def test_bootstrap_workflow_concept_identities_enforces_required_type(monkeypatch):
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

    report = authority_service.bootstrap_workflow_concept_identities(
        registry=cast(Any, registry)
    )

    assert report["counts"]["updated"] == 1
    assert report["updated_workflow_ids"] == ["#V#wf_retype"]
    assert update_payloads
    payload = update_payloads[0]["payload"]
    assert "#V#ai_workflow" in payload["relationships"]["is_an_instance_of"]


def test_bootstrap_workflow_concept_identities_preserves_valid_subtype_typing(
    monkeypatch,
):
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

    report = authority_service.bootstrap_workflow_concept_identities(
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


def _comparable_state_metadata(state: Any) -> dict[str, Any]:
    metadata = getattr(state, "metadata", None)
    if not isinstance(metadata, dict):
        return {}
    comparable: dict[str, Any] = {}
    for key in (
        "invokes_workflow",
        "reads_context_keys",
        "writes_context_keys",
        "tool_output_context_mappings",
        "subworkflow_contract",
    ):
        value = metadata.get(key)
        if value:
            comparable[key] = value
    return comparable


def _built_metadata_subset_matches(*, loaded_state: Any, built_state: Any) -> bool:
    def _metadata_value_matches(*, loaded_value: Any, expected_value: Any) -> bool:
        if loaded_value == expected_value:
            return True
        if isinstance(loaded_value, dict) and isinstance(expected_value, dict):
            for key, value in expected_value.items():
                if key not in loaded_value:
                    return False
                if (
                    key in {"mapping_concept_id", "workflow_id_mapping_concept_id"}
                    and value == ""
                    and isinstance(loaded_value.get(key), str)
                    and str(loaded_value.get(key) or "").strip()
                ):
                    continue
                if not _metadata_value_matches(
                    loaded_value=loaded_value.get(key),
                    expected_value=value,
                ):
                    return False
            extra_keys = set(loaded_value.keys()) - set(expected_value.keys())
            return extra_keys.issubset(
                {
                    "mapping_concept_id",
                    "workflow_id_mapping_concept_id",
                }
            )
        if isinstance(loaded_value, list) and isinstance(expected_value, list):
            if len(loaded_value) != len(expected_value):
                return False
            return all(
                _metadata_value_matches(loaded_value=item, expected_value=expected)
                for item, expected in zip(loaded_value, expected_value)
            )
        return False

    expected = _comparable_state_metadata(built_state)
    if not expected:
        return True
    loaded = _comparable_state_metadata(loaded_state)
    return all(
        _metadata_value_matches(
            loaded_value=loaded.get(key),
            expected_value=value,
        )
        for key, value in expected.items()
    )


def _built_action_inputs_match(*, loaded_state: Any, built_state: Any) -> bool:
    def _input_value_matches(*, loaded_value: Any, built_value: Any) -> bool:
        if loaded_value == built_value:
            return True
        if not isinstance(loaded_value, dict) or not isinstance(built_value, dict):
            return False

        loaded_context_key = str(loaded_value.get("$context_key") or "").strip()
        built_context_key = str(built_value.get("$context_key") or "").strip()
        if loaded_context_key != built_context_key:
            return False

        built_mapping_concept_id = str(
            built_value.get("$mapping_concept_id") or ""
        ).strip()
        if built_mapping_concept_id:
            loaded_mapping_concept_id = str(
                loaded_value.get("$mapping_concept_id") or ""
            ).strip()
            if loaded_mapping_concept_id != built_mapping_concept_id:
                return False

        ignored_keys = {"$context_key", "$mapping_concept_id"}
        compared_keys = set(loaded_value.keys()).union(built_value.keys()) - ignored_keys
        return all(loaded_value.get(key) == built_value.get(key) for key in compared_keys)

    loaded_actions = list(getattr(loaded_state, "actions", ()) or ())
    built_actions = list(getattr(built_state, "actions", ()) or ())
    if len(loaded_actions) != len(built_actions):
        return False

    built_metadata = _comparable_state_metadata(built_state)
    allow_subworkflow_input_subset = bool(built_metadata.get("subworkflow_contract"))

    for loaded_action, built_action in zip(loaded_actions, built_actions):
        if getattr(loaded_action, "action_id", None) != getattr(
            built_action, "action_id", None
        ):
            return False

        loaded_inputs = dict(getattr(loaded_action, "inputs", {}) or {})
        built_inputs = dict(getattr(built_action, "inputs", {}) or {})
        for key in list(loaded_inputs.keys()):
            if key.startswith("__parent_") and key not in built_inputs:
                loaded_inputs.pop(key, None)
        if loaded_inputs == built_inputs:
            continue
        if not allow_subworkflow_input_subset:
            if set(loaded_inputs.keys()) != set(built_inputs.keys()):
                return False
            if any(
                not _input_value_matches(
                    loaded_value=loaded_inputs.get(key),
                    built_value=built_inputs.get(key),
                )
                for key in loaded_inputs.keys()
            ):
                return False
            continue
        if any(key not in built_inputs for key in loaded_inputs):
            return False
        if any(
            not _input_value_matches(
                loaded_value=value,
                built_value=built_inputs.get(key),
            )
            for key, value in loaded_inputs.items()
        ):
            return False

    return True


def _graph_step_control_flow_conditions(
    *,
    graph: dict[str, Any],
    workflow_id: str,
    state_id: str,
) -> list[dict[str, Any]] | None:
    step_id = authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id=state_id,
    )
    for step in graph.get("steps", []):
        if isinstance(step, dict) and step.get("step_id") == step_id:
            control_flow = step.get("control_flow") or {}
            if isinstance(control_flow, dict):
                raw_conditions = control_flow.get("conditions")
                if isinstance(raw_conditions, list):
                    return raw_conditions
            return None
    return None


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
    from workflow_test_support import build_authoritative_test_workflow_definition

    registry = build_workflow_registry_read_only()
    expected_published_ids = {
        workflow_id
        for workflow_id in authority_service.CANONICAL_CHAT_WORKFLOW_IDS
        if workflow_id in authority_service._CANONICAL_WORKFLOW_PUBLICATION_SPECS
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
        built_in_definition = build_authoritative_test_workflow_definition(workflow_id)

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
            assert _built_action_inputs_match(
                loaded_state=loaded_state,
                built_state=built_state,
            )
            assert {transition.to_state for transition in loaded_state.transitions} == {
                authority_service._step_concept_id(
                    workflow_id=workflow_id,
                    state_id=transition.to_state,
                )
                for transition in built_state.transitions
            }
            assert _built_metadata_subset_matches(
                loaded_state=loaded_state,
                built_state=built_state,
            )

        is_executable, reason, detail = classify_workflow_concept_executability(
            workflow_id
        )
        assert is_executable is True, f"{workflow_id}: {reason}: {detail}"
        assert reason == EXECUTABILITY_EXECUTABLE_NOW


def test_bootstrap_publishes_explicit_step_conditions_for_canonical_durable_workflows(
    _reset_mock_workflow_graph_db,
):
    from src.backend.workflows.durable.file_copy_interpretation_workflow import (
        build_file_copy_interpretation_workflow_test_registration,
    )
    from src.backend.workflows.durable.file_copy_typing_workflow import (
        build_file_copy_typing_workflow_test_registration,
    )
    from src.backend.workflows.durable.file_copy_upload_classification_workflow import (
        build_file_copy_upload_classification_workflow_test_registration,
    )
    from src.backend.workflows.durable.file_copy_upload_handler_workflow import (
        FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
        build_file_copy_upload_handler_workflow_test_registration,
    )
    from src.backend.workflows.durable.planning_workflow import (
        PLANNING_WORKFLOW_ID,
    )
    from src.backend.workflows.durable.parent_specificity_rumination_workflow import (
        PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
    )
    from src.backend.workflows.durable.workflow_gap_recovery_workflow import (
        WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    )
    from src.backend.workflows.durable.registry_factory import build_workflow_registry_read_only
    from src.backend.workflows.vontology_loader import build_workflow_process_graph
    from workflow_test_support import bootstrap_authoritative_reasoning_recovery_workflows

    report = bootstrap_authoritative_reasoning_recovery_workflows()
    errors = (
        (report.get("graph_publication") or {}).get("errors_by_workflow_id") or {}
    )
    assert PLANNING_WORKFLOW_ID not in errors
    assert PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID not in errors
    assert WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID not in errors

    registry = build_workflow_registry_read_only()
    for registration in (
        build_file_copy_typing_workflow_test_registration(),
        build_file_copy_interpretation_workflow_test_registration(),
        build_file_copy_upload_classification_workflow_test_registration(),
        build_file_copy_upload_handler_workflow_test_registration(),
    ):
        registry.register(registration)

    report = authority_service.bootstrap_workflow_concepts(registry=cast(Any, registry))
    file_copy_errors = (
        (report.get("graph_publication") or {}).get("errors_by_workflow_id") or {}
    )
    assert FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID not in file_copy_errors

    planning_graph, planning_warnings = build_workflow_process_graph(
        PLANNING_WORKFLOW_ID
    )
    assert planning_graph is not None
    assert planning_warnings == []
    assert _graph_step_control_flow_conditions(
        graph=planning_graph,
        workflow_id=PLANNING_WORKFLOW_ID,
        state_id="assess",
    ) == [
        {
            "to": authority_service._step_concept_id(
                workflow_id=PLANNING_WORKFLOW_ID,
                state_id="infer",
            ),
            "reason": "context_ready",
            "condition": {
                "kind": "context_exists",
                "key": "planning_context",
                "expected": True,
            },
        },
        {
            "to": authority_service._step_concept_id(
                workflow_id=PLANNING_WORKFLOW_ID,
                state_id="failed",
            ),
            "reason": "missing_context",
            "condition": {"kind": "always"},
        },
    ]

    parent_graph, parent_warnings = build_workflow_process_graph(
        PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID
    )
    assert parent_graph is not None
    assert parent_warnings == []
    assert _graph_step_control_flow_conditions(
        graph=parent_graph,
        workflow_id=PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
        state_id="assess",
    ) == [
        {
            "to": authority_service._step_concept_id(
                workflow_id=PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
                state_id="prepare_candidate",
            ),
            "reason": "candidates_found",
            "condition": {
                "kind": "context_cardinality",
                "key": "candidate_ids",
                "operator": "gte",
                "value": 1,
            },
        },
        {
            "to": authority_service._step_concept_id(
                workflow_id=PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
                state_id="complete",
            ),
            "reason": "no_candidates",
            "condition": {"kind": "always"},
        },
    ]

    workflow_gap_graph, workflow_gap_warnings = build_workflow_process_graph(
        WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    )
    assert workflow_gap_graph is not None
    assert workflow_gap_warnings == []
    assert _graph_step_control_flow_conditions(
        graph=workflow_gap_graph,
        workflow_id=WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
        state_id="analyse_gap",
    ) == [
        {
            "to": authority_service._step_concept_id(
                workflow_id=WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
                state_id="prepare_candidate",
            ),
            "reason": "candidate_needed",
            "condition": {
                "kind": "context_flag",
                "key": "workflow_gap_should_create_candidate",
                "expected": True,
            },
        },
        {
            "to": authority_service._step_concept_id(
                workflow_id=WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
                state_id="complete",
            ),
            "reason": "no_candidate_needed",
            "condition": {"kind": "always"},
        },
    ]


def test_bootstrap_publishes_file_copy_upload_handler_dynamic_subworkflow_contract(
    _reset_mock_workflow_graph_db,
):
    from src.backend.workflows.durable.file_copy_interpretation_workflow import (
        FILE_COPY_INTERPRETATION_WORKFLOW_ID,
        build_file_copy_interpretation_workflow_test_registration,
    )
    from src.backend.workflows.durable.file_copy_typing_workflow import (
        build_file_copy_typing_workflow_test_registration,
    )
    from src.backend.workflows.durable.file_copy_upload_classification_workflow import (
        build_file_copy_upload_classification_workflow_test_registration,
    )
    from src.backend.workflows.durable.file_copy_upload_handler_workflow import (
        FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
        build_file_copy_upload_handler_workflow_test_registration,
    )
    from src.backend.workflows.vontology_loader import (
        load_workflow_definition_from_vontology,
    )
    from src.backend.workflows.workflow_registry import WorkflowRegistry

    registry = WorkflowRegistry()
    for registration in (
        build_file_copy_typing_workflow_test_registration(),
        build_file_copy_interpretation_workflow_test_registration(),
        build_file_copy_upload_classification_workflow_test_registration(),
        build_file_copy_upload_handler_workflow_test_registration(),
    ):
        registry.register(registration)

    report = authority_service.bootstrap_workflow_concepts(registry=cast(Any, registry))
    graph_publication = report.get("graph_publication") or {}
    errors = graph_publication.get("errors_by_workflow_id") or {}
    assert FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID not in errors

    loaded_definition = load_workflow_definition_from_vontology(
        FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID
    )
    assert loaded_definition is not None

    specialised_state_id = authority_service._step_concept_id(
        workflow_id=FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
        state_id="specialised",
    )
    specialised_state = loaded_definition.states[specialised_state_id]
    specialised_contract = specialised_state.metadata["subworkflow_contract"]
    assert specialised_contract["workflow_id"] == ""
    assert specialised_contract["workflow_id_context_key"] == "upload_target_workflow_id"
    assert set(specialised_contract["provided_inputs"]) == {
        "concept_id",
        "content_type",
        "original_filename",
        "size_bytes",
        "sha256",
        "blob_uri",
        "uploaded_at",
        "index_in_rag",
        "workflow_id",
        "file_copy_concept_id",
    }

    interpret_state_id = authority_service._step_concept_id(
        workflow_id=FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
        state_id="interpret",
    )
    interpret_state = loaded_definition.states[interpret_state_id]
    interpret_contract = interpret_state.metadata["subworkflow_contract"]
    assert interpret_contract["workflow_id"] == FILE_COPY_INTERPRETATION_WORKFLOW_ID
    assert "concept_id" in interpret_contract["provided_inputs"]
    assert "file_copy_concept_id" in interpret_contract["provided_inputs"]


def test_canonical_workflow_runtime_identity_matches_authoritative_loader(
    _reset_mock_workflow_graph_db,
):
    from src.backend.workflows.vontology_loader import (
        load_workflow_definition_from_vontology,
    )
    from src.backend.workflows.workflow_registry import WorkflowRegistry
    from workflow_test_support import register_authoritative_test_workflows

    registry = WorkflowRegistry()
    register_authoritative_test_workflows(registry)
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


def test_file_copy_workflow_runtime_identity_matches_authoritative_loader(
    _reset_mock_workflow_graph_db,
):
    from src.backend.workflows.durable.registry_factory import (
        build_workflow_registry_read_only,
    )
    from src.backend.workflows.vontology_loader import (
        load_workflow_definition_from_vontology,
    )
    from workflow_test_support import (
        AUTHORITATIVE_FILE_COPY_WORKFLOW_IDS,
        bootstrap_authoritative_file_copy_workflows,
    )

    bootstrap_authoritative_file_copy_workflows()
    runtime_registry = build_workflow_registry_read_only()

    for workflow_id in AUTHORITATIVE_FILE_COPY_WORKFLOW_IDS:
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


def test_reasoning_recovery_workflow_runtime_identity_matches_authoritative_loader(
    _reset_mock_workflow_graph_db,
):
    from src.backend.workflows.durable.registry_factory import (
        build_workflow_registry_read_only,
    )
    from src.backend.workflows.vontology_loader import (
        load_workflow_definition_from_vontology,
    )
    from workflow_test_support import (
        AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS,
        bootstrap_authoritative_reasoning_recovery_workflows,
    )

    bootstrap_authoritative_reasoning_recovery_workflows()
    runtime_registry = build_workflow_registry_read_only()

    for workflow_id in AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS:
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


def test_build_workflow_registry_loads_reasoning_recovery_family_after_explicit_publication(
    _reset_mock_workflow_graph_db,
    monkeypatch,
):
    from src.backend.db.mongo_client import get_db
    from src.backend.services.workflow_discovery_service import (
        invalidate_workflow_discovery_executability_caches,
    )
    from src.backend.workflows.durable.registry_factory import build_workflow_registry
    from workflow_test_support import (
        AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS,
        bootstrap_authoritative_reasoning_recovery_workflows,
    )

    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    authority_service.clear_workflow_type_resolution_cache()

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    bootstrap_authoritative_reasoning_recovery_workflows()
    invalidate_workflow_discovery_executability_caches()
    registry = build_workflow_registry()

    for workflow_id in AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS:
        registration = registry.get_registration(workflow_id)
        assert registration is not None
        assert str(registration.source or "").strip().lower() == "vontology"


def test_bootstrap_publishes_explicit_step_conditions_for_support_maintenance_workflows(
    _reset_mock_workflow_graph_db,
):
    from src.backend.workflows.durable.workflow_introspection_maintenance_workflow import (
        WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
    )
    from src.backend.workflows.durable.jira_task_incremental_import_workflow import (
        JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
    )
    from src.backend.workflows.vontology_loader import build_workflow_process_graph
    from workflow_test_support import (
        bootstrap_authoritative_support_maintenance_workflows,
    )

    report = bootstrap_authoritative_support_maintenance_workflows()
    errors = (
        (report.get("graph_publication") or {}).get("errors_by_workflow_id") or {}
    )
    assert WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID not in errors
    assert JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID not in errors

    maintenance_graph, maintenance_warnings = build_workflow_process_graph(
        WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID
    )
    assert maintenance_graph is not None
    assert maintenance_warnings == []
    assert _graph_step_control_flow_conditions(
        graph=maintenance_graph,
        workflow_id=WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
        state_id="assess",
    ) == [
        {
            "to": authority_service._step_concept_id(
                workflow_id=WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
                state_id="failed",
            ),
            "reason": "on_failure",
            "condition": {
                "kind": "context_flag",
                "key": "last_action_failed",
                "expected": True,
            },
        },
        {
            "to": authority_service._step_concept_id(
                workflow_id=WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
                state_id="diagnose",
            ),
            "reason": "evidence_ready",
            "condition": {
                "kind": "context_flag",
                "key": "maintenance_evidence",
                "expected": True,
            },
        },
        {
            "to": authority_service._step_concept_id(
                workflow_id=WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
                state_id="failed",
            ),
            "reason": "missing_evidence",
            "condition": {"kind": "always"},
        },
    ]

    jira_graph, jira_warnings = build_workflow_process_graph(
        JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID
    )
    assert jira_graph is not None
    assert jira_warnings == []
    assert _graph_step_control_flow_conditions(
        graph=jira_graph,
        workflow_id=JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
        state_id="run_sync",
    ) == [
        {
            "to": authority_service._step_concept_id(
                workflow_id=JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
                state_id="failed",
            ),
            "reason": "sync_failed",
            "condition": {
                "kind": "context_flag",
                "key": "last_action_failed",
                "expected": True,
            },
        },
        {
            "to": authority_service._step_concept_id(
                workflow_id=JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
                state_id="complete",
            ),
            "reason": "sync_complete",
            "condition": {"kind": "always"},
        },
    ]


def test_support_maintenance_workflow_runtime_identity_matches_authoritative_loader(
    _reset_mock_workflow_graph_db,
):
    from src.backend.workflows.durable.registry_factory import (
        build_workflow_registry_read_only,
    )
    from src.backend.workflows.vontology_loader import (
        load_workflow_definition_from_vontology,
    )
    from workflow_test_support import (
        AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS,
        bootstrap_authoritative_support_maintenance_workflows,
    )

    bootstrap_authoritative_support_maintenance_workflows()
    runtime_registry = build_workflow_registry_read_only()

    for workflow_id in AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS:
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


def test_build_workflow_registry_loads_support_maintenance_family_after_explicit_publication(
    _reset_mock_workflow_graph_db,
    monkeypatch,
):
    from src.backend.db.mongo_client import get_db
    from src.backend.services.workflow_discovery_service import (
        invalidate_workflow_discovery_executability_caches,
    )
    from src.backend.workflows.durable.registry_factory import build_workflow_registry
    from workflow_test_support import (
        AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS,
        bootstrap_authoritative_support_maintenance_workflows,
    )

    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    authority_service.clear_workflow_type_resolution_cache()

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    bootstrap_authoritative_support_maintenance_workflows()
    invalidate_workflow_discovery_executability_caches()
    registry = build_workflow_registry()

    for workflow_id in AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS:
        registration = registry.get_registration(workflow_id)
        assert registration is not None
        assert str(registration.source or "").strip().lower() == "vontology"


def test_publish_canonical_graphs_reports_validation_failures(
    _reset_mock_workflow_graph_db,
    monkeypatch,
):
    from src.backend.workflows.workflow_registry import WorkflowRegistry
    from workflow_test_support import register_authoritative_test_workflows

    registry = WorkflowRegistry()
    register_authoritative_test_workflows(registry)
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
