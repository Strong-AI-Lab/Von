"""Validate, publish, and verify the KR materialisation workflow seed family.

Workflow graph, routing, success/effect contracts, and prompt references live in
the KR repo-seed bundle as bootstrap data and are materialised into Vontology.
Prompt bodies are seeded as Vontology prompt concepts. This script remains a
thin operational wrapper around those authority surfaces.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from src.backend.services.kr_materialisation_workflow_vontology_service import (  # noqa: E402
    KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
    KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID,
    bootstrap_canonical_kr_materialisation_workflows,
    diff_kr_materialisation_workflow_repo_seed_bundle,
    export_kr_materialisation_workflow_repo_seed_bundle,
    validate_kr_materialisation_seed_bundle,
    verify_canonical_kr_materialisation_workflows,
)

_CHILD_WORKFLOW_IDS = (
    KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
    KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID,
)


def _print_report(report: dict[str, Any]) -> None:
    print(json.dumps(report, indent=2, sort_keys=True))


def _require_live_db_for_write(command: str) -> None:
    if os.environ.get("VON_DB_NAME") != "von_db":
        raise SystemExit(
            f"{command} mutates live Vontology workflow authority; set "
            "VON_DB_NAME=von_db explicitly."
        )


def _target_workflow_ids(command: str) -> Sequence[str] | None:
    if command.endswith("-children"):
        return _CHILD_WORKFLOW_IDS
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operate the KR materialisation Vontology workflow seed family."
    )
    parser.add_argument(
        "command",
        choices=(
            "validate-children",
            "publish-children",
            "validate-all",
            "publish-all",
            "verify",
            "export",
            "diff",
        ),
    )
    parser.add_argument(
        "--force-republish",
        action="store_true",
        help="Republish even when the live Vontology materialisation validates as current.",
    )
    args = parser.parse_args(argv)

    command = str(args.command)
    if command.startswith("validate"):
        report = validate_kr_materialisation_seed_bundle(
            target_workflow_ids=_target_workflow_ids(command)
        )
        _print_report(report)
        return 0 if report.get("success") else 1

    if command.startswith("publish"):
        _require_live_db_for_write(command)
        report = bootstrap_canonical_kr_materialisation_workflows(
            force_republish=bool(args.force_republish),
            target_workflow_ids=_target_workflow_ids(command),
        )
        _print_report(report)
        return 0 if report.get("success") else 1

    if command == "verify":
        report = verify_canonical_kr_materialisation_workflows()
        _print_report(report)
        return 0 if report.get("success") else 1

    if command == "export":
        report = export_kr_materialisation_workflow_repo_seed_bundle()
        _print_report(report)
        return 0

    if command == "diff":
        report = diff_kr_materialisation_workflow_repo_seed_bundle()
        _print_report(report)
        return 1 if report.get("has_differences") else 0

    raise SystemExit(f"Unsupported command: {command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))