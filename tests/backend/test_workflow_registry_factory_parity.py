from types import SimpleNamespace

import pytest

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
