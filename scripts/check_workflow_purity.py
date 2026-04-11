#!/usr/bin/env python3
"""ASCII-safe workflow purity gate for launcher and hook entry points.

This script delegates to the structured workflow-purity report so that local
launchers, git hooks, and ad-hoc checks all use the same authority surface as
the pytest regression gate.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
repo_root_str = str(REPO_ROOT)
if repo_root_str not in sys.path:
    sys.path.insert(0, repo_root_str)

build_workflow_purity_registry_snapshot = importlib.import_module(
    "src.backend.workflows.durable.registry_factory"
).build_workflow_purity_registry_snapshot
_workflow_purity_report = importlib.import_module(
    "src.backend.workflows.workflow_purity_report"
)
WORKFLOW_PURITY_BASELINE_PATH = _workflow_purity_report.WORKFLOW_PURITY_BASELINE_PATH
WORKFLOW_PURITY_BASELINE_REFRESH_COMMAND = (
    _workflow_purity_report.WORKFLOW_PURITY_BASELINE_REFRESH_COMMAND
)
build_workflow_purity_report = _workflow_purity_report.build_workflow_purity_report
write_workflow_purity_baseline = _workflow_purity_report.write_workflow_purity_baseline


def _print_regression_summary(
    report: dict[str, object],
    verbose: bool = False,
    quiet: bool = False,
) -> None:
    if not quiet:
        print("Running workflow purity gate...")
    
    summary_text = str(report.get("summary_text") or "Workflow purity report generated.")
    print(summary_text)

    if quiet:
        return

    baseline = report.get("baseline")
    comparison = baseline.get("comparison") if isinstance(baseline, dict) else {}
    if not isinstance(comparison, dict):
        comparison = {}

    regression_detected = bool(comparison.get("regression_detected"))

    increased = comparison.get("increased_counters")
    if isinstance(increased, dict) and increased:
        print("Regressed counters:")
        for key in sorted(increased):
            delta = increased.get(key)
            if not isinstance(delta, dict):
                continue
            print(
                f" - {key}: baseline={delta.get('baseline')} "
                f"current={delta.get('current')} delta={delta.get('delta')}"
            )

    missing = comparison.get("missing_counter_keys")
    if isinstance(missing, list) and missing:
        print("Missing baseline counters:")
        for key in missing:
            print(f" - {key}")

    if regression_detected:
        print(
            "Workflow purity gate failed. "
            f"Refresh only if the new baseline is deliberate: {WORKFLOW_PURITY_BASELINE_REFRESH_COMMAND}"
        )
        print(
            f"Baseline file: {Path(WORKFLOW_PURITY_BASELINE_PATH).as_posix()}"
        )
        if verbose:
            print("Full report:")
            print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Workflow purity gate passed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the structured workflow-purity gate and exit non-zero on regression."
        )
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=".",
        help="Optional repository root to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "--refresh-baseline",
        action="store_true",
        help="Overwrite the checked-in baseline with the current counters.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print full report JSON on failure.",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Only print the summary line.",
    )
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).resolve()
    
    # Try to include Vontology-backed workflows if DB is available
    # so we can track synthesized launch contracts.
    import os
    if os.getenv("VON_DB_NAME"):
        from src.backend.workflows.durable.registry_factory import build_vontology_workflow_registry_snapshot
        try:
            registry = build_vontology_workflow_registry_snapshot()
        except Exception:
            registry = build_workflow_purity_registry_snapshot()
    else:
        registry = build_workflow_purity_registry_snapshot()

    report = build_workflow_purity_report(registry=registry, project_root=project_root)

    if args.refresh_baseline:
        path = write_workflow_purity_baseline(report)
        print(f"Refreshed workflow-purity baseline: {path.as_posix()}")
        return 0

    _print_regression_summary(report, verbose=args.verbose, quiet=args.quiet)
    comparison = ((report.get("baseline") or {}).get("comparison") or {})
    if isinstance(comparison, dict) and comparison.get("regression_detected"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
