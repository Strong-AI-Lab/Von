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


def test_get_shared_workflow_registry_read_only_starts_capability_index_warmup(
    monkeypatch,
):
    sentinel_registry = object()
    observed: list[tuple[bool, object | None]] = []

    monkeypatch.setattr(registry_factory, "_shared_workflow_registry", None)
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_registry_read_only",
        lambda defer_parity_work=True, start_deferred_registry_work=False: (
            sentinel_registry
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.prewarm_workflow_capability_index",
        lambda *, force_refresh=False, workflow_registry=None: (
            observed.append((bool(force_refresh), workflow_registry)) or True
        ),
    )

    result = registry_factory.get_shared_workflow_registry_read_only(
        defer_parity_work=True,
        force_rebuild=True,
    )

    assert result is sentinel_registry
    assert observed == [(False, sentinel_registry)]


def test_invalidate_shared_workflow_registry_read_only_resets_capability_index(
    monkeypatch,
):
    observed: list[bool] = []

    monkeypatch.setattr(registry_factory, "_shared_workflow_registry", object())
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.invalidate_workflow_capability_index",
        lambda: observed.append(True) or {"success": True},
    )

    result = registry_factory.invalidate_shared_workflow_registry_read_only()

    assert result["success"] is True
    assert result["had_cached_value"] is True
    assert result["capability_index_invalidated"] is True
    assert observed == [True]


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
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_purity_report",
        lambda registry: {
            "counters": {
                "built_in_registration_count": 1,
                "remaining_python_workflow_family_count": 1,
                "direct_instance_create_callsite_count": 3,
                "env_event_binding_count": 0,
                "legacy_selector_mode_count": 1,
                "builtin_capability_override_count": 1,
                "non_vontology_discoverable_workflow_count": 1,
            },
            "baseline": {
                "comparison": {
                    "regression_detected": False,
                }
            },
        },
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
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_purity_report",
        lambda registry: {
            "counters": {
                "built_in_registration_count": 1,
                "remaining_python_workflow_family_count": 1,
                "direct_instance_create_callsite_count": 3,
                "env_event_binding_count": 0,
                "legacy_selector_mode_count": 1,
                "builtin_capability_override_count": 1,
                "non_vontology_discoverable_workflow_count": 1,
            },
            "baseline": {
                "comparison": {
                    "regression_detected": False,
                }
            },
        },
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


def test_workflow_parity_inventory_includes_workflow_purity_regression_reason(
    monkeypatch,
):
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_process_graph",
        lambda _workflow_id: (
            {"initial_step": "#V#step_1", "steps": [{"step_id": "#V#step_1"}]},
            [],
        ),
    )
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_purity_report",
        lambda registry: {
            "summary_text": "Workflow purity summary",
            "counters": {
                "built_in_registration_count": 2,
                "remaining_python_workflow_family_count": 3,
                "direct_instance_create_callsite_count": 3,
                "env_event_binding_count": 0,
                "legacy_selector_mode_count": 1,
                "builtin_capability_override_count": 11,
                "non_vontology_discoverable_workflow_count": 12,
            },
            "baseline": {
                "comparison": {
                    "regression_detected": True,
                }
            },
        },
    )
    dummy_registry = _DummyRegistry(
        workflow_ids=["#V#wf_ok"],
        sources={"#V#wf_ok": "built_in"},
    )

    inventory = registry_factory._build_workflow_parity_inventory(
        registry=dummy_registry,  # type: ignore[arg-type]
        discovered_workflow_ids=["#V#wf_ok"],
    )

    assert inventory["workflow_purity"]["baseline"]["comparison"]["regression_detected"] is True
    reasons = inventory.get("diagnostics", {}).get("reason_codes", [])
    assert "workflow_purity_regression" in reasons


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

    def _register_python_defined_workflows(registry: WorkflowRegistry) -> None:
        registry.register(
            _build_registration(
                overlap_workflow_id,
                source="built_in",
                purpose="Built-in workflow purpose",
            )
        )

    monkeypatch.setattr(
        registry_factory,
        "_register_python_defined_workflows",
        _register_python_defined_workflows,
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
    monkeypatch.setattr(
        registry_factory,
        "_launch_deferred_registry_work",
        lambda **kwargs: setattr(
            registry_factory,
            "_last_inventory_snapshot",
            registry_factory._build_workflow_parity_inventory(
                registry=kwargs["registry"],
                discovered_workflow_ids=kwargs["discovered_workflow_ids"],
                authority_report=registry_factory.build_workflow_concept_authority_report(
                    registry=kwargs["registry"]
                ),
            ),
        ),
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


def test_runtime_registry_bootstrap_excludes_representation_publication_reports(
    monkeypatch,
):
    from src.backend.services import workflow_capability_service

    class _InlineThread:
        def __init__(self, *, target, name, daemon):
            self._target = target

        def start(self):
            self._target()

    class _FakeCapabilityIndex:
        def index_from_registry(self, registry):
            return len(list(registry.all_workflow_ids()))

    monkeypatch.setattr(registry_factory, "_register_python_defined_workflows", lambda registry: None)
    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [])
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
        "_build_expected_authoritative_workflow_report",
        lambda *, workflow_ids: {
            "enabled": False,
            "reason": "runtime_bootstrap_removed",
            "workflow_ids": list(workflow_ids),
            "resolved_workflow_ids": list(workflow_ids),
            "missing_workflow_ids": [],
            "counts": {
                "required": len(workflow_ids),
                "resolved": len(workflow_ids),
                "missing": 0,
            },
        },
    )
    monkeypatch.setattr(
        workflow_capability_service,
        "get_workflow_capability_index",
        lambda: _FakeCapabilityIndex(),
    )
    monkeypatch.setattr(registry_factory.threading, "Thread", _InlineThread)
    monkeypatch.setattr(registry_factory, "_apply_workflow_parity_policy", lambda inventory_snapshot: None)
    monkeypatch.setattr(registry_factory, "_last_inventory_snapshot", None)

    registry_factory._build_workflow_registry(allow_bootstrap=True)
    snapshot = registry_factory.get_workflow_registry_inventory_snapshot()

    authority_report = snapshot.get("workflow_authority", {})
    assert "paper_representation_bootstrap" not in authority_report
    assert "talk_representation_bootstrap" not in authority_report
    assert authority_report.get("bootstrap", {}).get("enabled") is False
    assert authority_report.get("bootstrap", {}).get("requested") is True
    assert authority_report.get("bootstrap", {}).get("reason") == "runtime_bootstrap_removed"

    reasoning_recovery = authority_report.get(
        "expected_authoritative_reasoning_recovery_workflows", {}
    )
    assert reasoning_recovery.get("enabled") is False
    assert reasoning_recovery.get("reason") == "runtime_bootstrap_removed"
    assert "#V#planning_workflow" in reasoning_recovery.get("workflow_ids", [])

    file_copy = authority_report.get("expected_authoritative_file_copy_workflows", {})
    assert file_copy.get("enabled") is False
    assert file_copy.get("reason") == "runtime_bootstrap_removed"
    assert "#V#file_copy_upload_handler_workflow" in file_copy.get("workflow_ids", [])


def test_get_or_build_inventory_snapshot_returns_pending_when_sync_build_disabled(
    monkeypatch,
):
    registry = WorkflowRegistry()
    registry.register(
        _build_registration(
            "#V#meeting_invitation_testing_workflow",
            source="vontology",
            purpose="Meeting invitation testing workflow",
        )
    )
    monkeypatch.setattr(registry_factory, "_last_inventory_snapshot", None)
    monkeypatch.setattr(
        registry_factory,
        "build_workflow_concept_authority_report",
        lambda registry: (_ for _ in ()).throw(
            AssertionError("allow_sync_build=False should not trigger authority scans")
        ),
    )

    snapshot = registry_factory.get_or_build_workflow_registry_inventory_snapshot(
        registry=registry,
        allow_sync_build=False,
    )

    assert snapshot["build_state"] == "pending_background_build"
    assert snapshot["registry_workflow_ids"] == ["#V#meeting_invitation_testing_workflow"]
    assert snapshot["vontology_discovered_workflow_ids"] == [
        "#V#meeting_invitation_testing_workflow"
    ]
    assert snapshot["diagnostics"]["reason_codes"] == [
        "inventory_pending_background_build"
    ]


def test_read_only_registry_defaults_to_no_deferred_parity_work(monkeypatch):
    observed: list[tuple[bool, bool]] = []

    def _fake_build_workflow_registry(
        *,
        allow_bootstrap,
        force_background_deferred_work,
        start_deferred_registry_work,
    ):
        observed.append(
            (
                bool(force_background_deferred_work),
                bool(start_deferred_registry_work),
            )
        )
        return WorkflowRegistry()

    monkeypatch.setattr(
        registry_factory,
        "_build_workflow_registry",
        _fake_build_workflow_registry,
    )

    registry = registry_factory.build_workflow_registry_read_only()

    assert isinstance(registry, WorkflowRegistry)
    assert observed == [(True, False)]


def test_read_only_registry_can_opt_into_background_deferred_work(monkeypatch):
    observed: list[tuple[bool, bool]] = []

    def _fake_build_workflow_registry(
        *,
        allow_bootstrap,
        force_background_deferred_work,
        start_deferred_registry_work,
    ):
        observed.append(
            (
                bool(force_background_deferred_work),
                bool(start_deferred_registry_work),
            )
        )
        return WorkflowRegistry()

    monkeypatch.setattr(
        registry_factory,
        "_build_workflow_registry",
        _fake_build_workflow_registry,
    )

    registry = registry_factory.build_workflow_registry_read_only(
        start_deferred_registry_work=True
    )

    assert isinstance(registry, WorkflowRegistry)
    assert observed == [(True, True)]
