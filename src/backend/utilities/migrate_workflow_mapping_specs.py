#!/usr/bin/env python
"""Backfill workflow mapping concepts with structured mapping specs.

Usage examples (PowerShell):

    # Preview only (default dry-run)
    pdm run python src/backend/utilities/migrate_workflow_mapping_specs.py

    # Apply writes
    pdm run python src/backend/utilities/migrate_workflow_mapping_specs.py --apply

    # Restrict to one workflow
    pdm run python src/backend/utilities/migrate_workflow_mapping_specs.py --workflow-id #V#salient_predicate_governance_workflow --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


def _bootstrap_import_path() -> None:
    current_file = Path(__file__).resolve()
    for potential_root in [current_file.parent] + list(current_file.parents):
        if (potential_root / "pyproject.toml").exists():
            sys.path.insert(0, str(potential_root))
            return


def run_cli(argv: Sequence[str] | None = None) -> int:
    _bootstrap_import_path()

    from src.backend.services.workflow_mapping_migration_service import (
        migrate_workflow_mapping_specs,
    )

    parser = argparse.ArgumentParser(
        description=(
            "Migrate workflow mapping concepts to canonical "
            "concept_data.workflow_mapping_spec objects."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates (default is dry-run preview only).",
    )
    parser.add_argument(
        "--workflow-id",
        action="append",
        dest="workflow_ids",
        help=(
            "Explicit workflow ID to migrate. Repeat flag to include multiple "
            "workflows."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of distinct mapping concepts to process.",
    )
    parser.add_argument(
        "--rewrite-existing",
        action="store_true",
        help=(
            "Rewrite already-structured specs to canonical values when they "
            "differ from derived targets."
        ),
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args(argv)

    report = migrate_workflow_mapping_specs(
        workflow_ids=args.workflow_ids,
        dry_run=not bool(args.apply),
        limit=args.limit,
        rewrite_existing=bool(args.rewrite_existing),
    )
    if args.pretty:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(json.dumps(report))

    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())

