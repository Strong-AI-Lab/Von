from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
repo_root_str = str(REPO_ROOT)
if repo_root_str not in sys.path:
    sys.path.insert(0, repo_root_str)

from src.backend.workflows.workflow_repo_seed_export_service import (
    diff_repo_seed_workflow_bundle_from_authority,
    write_repo_seed_workflow_bundle_from_authority,
)


def _resolve_path(raw_value: str) -> Path:
    return Path(raw_value).expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh or diff a repo-side workflow seed bundle from authoritative "
            "Vontology workflow state."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser(
        "export",
        help="Rewrite the bundle as a generated snapshot from authoritative Vontology.",
    )
    export_parser.add_argument(
        "--asset-path",
        required=True,
        help="Path to the repo-seed workflow bundle JSON file to refresh.",
    )
    diff_parser = subparsers.add_parser(
        "diff",
        help="Show any differences between the current bundle and authoritative Vontology.",
    )
    diff_parser.add_argument(
        "--asset-path",
        required=True,
        help="Path to the repo-seed workflow bundle JSON file to diff.",
    )
    args = parser.parse_args(argv)

    asset_path = _resolve_path(args.asset_path)
    if args.command == "export":
        report = write_repo_seed_workflow_bundle_from_authority(asset_path=asset_path)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    report = diff_repo_seed_workflow_bundle_from_authority(asset_path=asset_path)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report.get("has_differences") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
