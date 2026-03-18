import json

from src.backend.workflows.durable import registry_factory
from src.backend.workflows.durable.registry_factory import (
    build_workflow_purity_registry_snapshot,
)
from src.backend.workflows.workflow_purity_report import (
    WORKFLOW_PURITY_BASELINE_PATH,
    build_workflow_purity_report,
)


def test_workflow_purity_baseline_has_no_regression() -> None:
    registry = build_workflow_purity_registry_snapshot()
    report = build_workflow_purity_report(registry=registry)
    comparison = report["baseline"]["comparison"]

    assert comparison["baseline_available"] is True, (
        "Workflow-purity baseline is missing. Refresh it deliberately with "
        "`pdm run python scripts/workflow_purity_report.py --refresh-baseline` "
        f"and commit {WORKFLOW_PURITY_BASELINE_PATH.as_posix()}."
    )
    assert comparison["regression_detected"] is False, (
        "Workflow purity counters regressed relative to the checked-in baseline.\n"
        f"{json.dumps(report, indent=2, sort_keys=True)}"
    )


def test_workflow_purity_registry_snapshot_skips_live_vontology_resolution(
    monkeypatch,
) -> None:
    def _unexpected(*args, **kwargs):
        raise AssertionError("purity snapshot must not call live Vontology helpers")

    monkeypatch.setattr(
        registry_factory,
        "_build_expected_authoritative_workflow_report",
        _unexpected,
    )
    monkeypatch.setattr(registry_factory, "discover_workflow_ids", _unexpected)
    monkeypatch.setattr(registry_factory, "_launch_deferred_registry_work", _unexpected)

    registry = build_workflow_purity_registry_snapshot()

    assert list(registry.all_workflow_ids()) == []
