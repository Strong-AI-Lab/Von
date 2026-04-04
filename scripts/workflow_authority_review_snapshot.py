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

_workflow_authority_review_snapshot_service = importlib.import_module(
    "src.backend.workflows.workflow_authority_review_snapshot_service"
)
WORKFLOW_AUTHORITY_REVIEW_OUTPUT_DIR = (
    _workflow_authority_review_snapshot_service.WORKFLOW_AUTHORITY_REVIEW_OUTPUT_DIR
)
diff_workflow_authority_review_snapshot = (
    _workflow_authority_review_snapshot_service.diff_workflow_authority_review_snapshot
)
write_workflow_authority_review_snapshot = (
    _workflow_authority_review_snapshot_service.write_workflow_authority_review_snapshot
)


def _resolve_output_dir(raw_value: str | None) -> Path | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    return Path(raw_value).expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export or diff generated review snapshots derived from authoritative "
            "Vontology workflow state."
        )
    )
    parser.add_argument(
        "--output-dir",
        default=str(WORKFLOW_AUTHORITY_REVIEW_OUTPUT_DIR),
        help=(
            "Directory for generated review snapshots. These files are derived and "
            "non-authoritative."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "export",
        help="Write deterministic generated review snapshots to the output directory.",
    )
    subparsers.add_parser(
        "diff",
        help=(
            "Diff the current authoritative Vontology state against the existing "
            "generated review snapshots."
        ),
    )
    args = parser.parse_args(argv)

    output_dir = _resolve_output_dir(args.output_dir)
    if args.command == "export":
        report = write_workflow_authority_review_snapshot(output_dir=output_dir)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    report = diff_workflow_authority_review_snapshot(output_dir=output_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report.get("has_differences") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
