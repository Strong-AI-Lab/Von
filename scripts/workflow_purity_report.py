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
build_workflow_purity_report = _workflow_purity_report.build_workflow_purity_report
write_workflow_purity_baseline = _workflow_purity_report.write_workflow_purity_baseline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit the structured workflow-purity report and manage its baseline."
    )
    parser.add_argument(
        "--refresh-baseline",
        action="store_true",
        help="Overwrite the checked-in baseline with the current report counters.",
    )
    args = parser.parse_args(argv)

    registry = build_workflow_purity_registry_snapshot()
    report = build_workflow_purity_report(registry=registry)
    print(json.dumps(report, indent=2, sort_keys=True))

    if args.refresh_baseline:
        path = write_workflow_purity_baseline(report)
        print(f"Refreshed workflow-purity baseline: {path}", file=sys.stderr)
        return 0

    comparison = report.get("baseline", {}).get("comparison", {})
    if isinstance(comparison, dict) and comparison.get("regression_detected"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
