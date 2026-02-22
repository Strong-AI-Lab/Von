from types import SimpleNamespace

import pytest

from src.backend.workflows.engine import WorkflowDefinition, WorkflowStateSpec
from src.backend.workflows.workflow_registry import WorkflowRegistration, WorkflowRegistry
from src.backend.workflows.durable import registry_factory


class _DummyRegistry:
    def __init__(self, workflow_ids: list[str], sources: dict[str, str]) -> None:
        self._workflow_ids = workflow_ids
        self._sources = sources

    def all_workflow_ids(self):
        return list(self._workflow_ids)

    def get_registration(self, workflow_id: str):
        source = self._sources.get(workflow_id)
        if source is None:
            return None
        return SimpleNamespace(source=source)


def _build_definition(workflow_id: str, purpose: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={"start": WorkflowStateSpec(state_id="start", terminal=True)},
        termination_states=("start",),
        purpose=purpose,
    )


def _build_registration(
    workflow_id: str,
    *,
    source: str = "built_in",
    purpose: str = "test purpose",
) -> WorkflowRegistration:
    definition = _build_definition(workflow_id, purpose)
    return WorkflowRegistration(
        workflow_id=workflow_id,
        definition=definition,
        purpose=purpose,
        source=source,
    )


def test_workflow_parity_inventory_includes_drift_reason_codes(monkeypatch):
    def _mock_build_graph(workflow_id: str):
        if workflow_id == "#V#wf_ok":
            return (
                {
                    "initial_step": "#V#step_1",
                    "steps": [{"step_id": "#V#step_1"}],
                },
                [],
            )
        if workflow_id == "#V#wf_identity":
            return ({}, ["workflow_has_no_steps"])
        raise RuntimeError("graph lookup failed")

    monkeypatch.setattr(
        registry_factory,
        "build_workflow_process_graph",
        _mock_build_graph,
    )

    dummy_registry = _DummyRegistry(
        workflow_ids=["#V#wf_registry_only", "#V#wf_ok"],
        sources={"#V#wf_registry_only": "built_in", "#V#wf_ok": "vontology"},
    )
    inventory = registry_factory._build_workflow_parity_inventory(
        registry=dummy_registry,  # type: ignore[arg-type]
        discovered_workflow_ids=["#V#wf_ok", "#V#wf_identity", "#V#wf_broken"],
    )

    diagnostics = inventory.get("diagnostics", {})
    assert diagnostics.get("drift_detected") is True
    assert diagnostics.get("severity") == "warning"
    reasons = diagnostics.get("reason_codes", [])
    assert "registry_only" in reasons
    assert "vontology_only" in reasons
    assert "identity_only" in reasons


def test_workflow_parity_policy_fail_mode_raises_on_drift(monkeypatch):
    inventory = {
        "summary_text": "Workflow parity summary",
        "diagnostics": {
            "drift_detected": True,
            "severity": "warning",
            "reason_codes": ["registry_only"],
        },
    }
    monkeypatch.setenv("VON_WORKFLOW_PARITY_ENFORCEMENT", "fail")

    with pytest.raises(RuntimeError, match="workflow_parity_drift_detected"):
        registry_factory._apply_workflow_parity_policy(inventory)


def test_workflow_parity_policy_no_drift_does_not_raise(monkeypatch):
    inventory = {
        "summary_text": "Workflow parity summary",
        "diagnostics": {
            "drift_detected": False,
            "severity": "ok",
            "reason_codes": [],
        },
    }
    monkeypatch.setenv("VON_WORKFLOW_PARITY_ENFORCEMENT", "fail")

    registry_factory._apply_workflow_parity_policy(inventory)
    policy = inventory.get("parity_policy", {})
    assert policy.get("mode") == "fail"
    assert policy.get("drift_detected") is False


def test_workflow_parity_inventory_includes_authority_drift_reason(monkeypatch):
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_process_graph",
        lambda _workflow_id: (
            {"initial_step": "#V#step_1", "steps": [{"step_id": "#V#step_1"}]},
            [],
        ),
    )
    dummy_registry = _DummyRegistry(
        workflow_ids=["#V#wf_ok"],
        sources={"#V#wf_ok": "built_in"},
    )
    authority_report = {
        "drift_detected": True,
        "counts": {"missing_concepts": 1, "missing_required_type": 1},
    }

    inventory = registry_factory._build_workflow_parity_inventory(
        registry=dummy_registry,  # type: ignore[arg-type]
        discovered_workflow_ids=["#V#wf_ok"],
        authority_report=authority_report,
    )

    reasons = inventory.get("diagnostics", {}).get("reason_codes", [])
    assert "workflow_authority" in reasons
    counts = inventory.get("counts", {})
    assert counts.get("authority_missing_concepts") == 1
    assert counts.get("authority_missing_required_type") == 1


def test_register_workflow_from_vontology_replaces_existing_registration(monkeypatch):
    workflow_id = "#V#overlap_workflow"
    registry = WorkflowRegistry()
    registry.register(
        _build_registration(
            workflow_id,
            source="built_in",
            purpose="Built-in purpose",
        )
    )
    vontology_definition = _build_definition(workflow_id, "Vontology purpose")
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        lambda concept_id: vontology_definition if concept_id == workflow_id else None,
    )

    registered, error_code = registry_factory.register_workflow_from_vontology(
        registry=registry,
        workflow_id=workflow_id,
        replace_existing=True,
    )

    assert registered is True
    assert error_code is None
    registration = registry.get_registration(workflow_id)
    assert registration is not None
    assert registration.source == "vontology"
    assert registration.purpose == "Vontology purpose"
    assert registration.definition.purpose == "Vontology purpose"


def test_build_registry_prioritises_vontology_on_overlap(monkeypatch):
    overlap_workflow_id = "#V#tool_calling_workflow"

    def _register_defaults(registry: WorkflowRegistry) -> None:
        registry.register(
            _build_registration(
                overlap_workflow_id,
                source="built_in",
                purpose="Built-in workflow purpose",
            )
        )

    monkeypatch.setattr(registry_factory, "register_default_workflows", _register_defaults)
    monkeypatch.setattr(
        registry_factory,
        "get_rag_sync_workflow_registration",
        lambda: _build_registration("#V#rag_sync", purpose="rag"),
    )
    monkeypatch.setattr(
        registry_factory,
        "get_enrichment_workflow_registration",
        lambda: _build_registration("#V#enrichment", purpose="enrichment"),
    )
    monkeypatch.setattr(
        registry_factory,
        "get_rumination_workflow_registration",
        lambda: _build_registration("#V#rumination", purpose="rumination"),
    )
    monkeypatch.setattr(
        registry_factory,
        "get_planning_workflow_registration",
        lambda: _build_registration("#V#planning", purpose="planning"),
    )
    monkeypatch.setattr(
        registry_factory,
        "discover_workflow_ids",
        lambda: [overlap_workflow_id],
    )
    monkeypatch.setattr(
        registry_factory,
        "load_workflow_definition_from_vontology",
        lambda workflow_id: (
            _build_definition(overlap_workflow_id, "Vontology workflow purpose")
            if workflow_id == overlap_workflow_id
            else None
        ),
    )
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_concept_authority_report",
        lambda registry: {
            "drift_detected": False,
            "counts": {
                "missing_concepts": 0,
                "missing_required_type": 0,
            },
        },
    )
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_process_graph",
        lambda workflow_id: (
            {
                "initial_step": "#V#step_start",
                "steps": [{"step_id": "#V#step_start"}],
            },
            [],
        ),
    )
    monkeypatch.setattr(
        registry_factory,
        "_apply_workflow_parity_policy",
        lambda inventory_snapshot: None,
    )

    registry = registry_factory._build_workflow_registry(allow_bootstrap=False)
    overlap_registration = registry.get_registration(overlap_workflow_id)
    assert overlap_registration is not None
    assert overlap_registration.source == "vontology"
    assert overlap_registration.purpose == "Vontology workflow purpose"

    snapshot = registry_factory.get_workflow_registry_inventory_snapshot()
    source_by_workflow_id = (
        snapshot.get("registry_sources", {}).get("source_by_workflow_id", {})
    )
    assert source_by_workflow_id.get(overlap_workflow_id) == "vontology"
