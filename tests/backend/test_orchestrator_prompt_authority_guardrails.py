from __future__ import annotations

import json

from src.backend.workflows.workflow_purity_report import (
    CORE_SUPPORT_POLICY_CONTRACTS,
    CORE_SUPPORT_PROMPT_SOURCE_FILES,
    build_workflow_purity_report,
)


def test_core_support_prompt_sources_have_no_drift() -> None:
    report = build_workflow_purity_report(registry=None)
    sources = report["details"]["python_authored_support_prompt_sources"]

    assert report["counters"]["python_authored_support_prompt_source_count"] == 0, (
        "Core support surfaces must not embed authored prompt bodies in Python. "
        f"Guarded files: {list(CORE_SUPPORT_PROMPT_SOURCE_FILES)}. "
        f"Detected sources: {json.dumps(sources, indent=2, sort_keys=True)}"
    )


def test_core_support_policy_contracts_have_no_violations() -> None:
    report = build_workflow_purity_report(registry=None)
    contracts = report["details"]["support_surface_policy_contracts"]

    assert report["counters"]["support_surface_policy_contract_violation_count"] == 0, (
        "Core support-surface policy contracts regressed. "
        f"Contracts: {json.dumps(list(CORE_SUPPORT_POLICY_CONTRACTS), default=str)}. "
        f"Violations: {json.dumps(contracts, indent=2, sort_keys=True)}"
    )
