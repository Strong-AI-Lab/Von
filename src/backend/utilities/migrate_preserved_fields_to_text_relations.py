#!/usr/bin/env python
"""Backfill concept_data.preserved_fields text into canonical text relations.

Usage examples (PowerShell):

    # Inventory only
    pdm run python src/backend/utilities/migrate_preserved_fields_to_text_relations.py --inventory --pretty

    # Dry-run migration preview (default)
    pdm run python src/backend/utilities/migrate_preserved_fields_to_text_relations.py --pretty

    # Apply migration and purge migrated keys
    pdm run python src/backend/utilities/migrate_preserved_fields_to_text_relations.py --apply --purge-migrated-keys --pretty
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

    from src.backend.services.preserved_fields_decommission_service import (
        inventory_preserved_fields,
        migrate_preserved_fields_to_text_relations,
    )

    parser = argparse.ArgumentParser(
        description=(
            "Migrate concept_data.preserved_fields text keys into canonical "
            "text relations."
        )
    )
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="Report preserved_fields key inventory without migration.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates (default is dry-run preview).",
    )
    parser.add_argument(
        "--purge-migrated-keys",
        action="store_true",
        help="When applying, unset migrated preserved_fields keys.",
    )
    parser.add_argument(
        "--concept-id",
        action="append",
        dest="concept_ids",
        help="Restrict to one concept_id (repeat for multiple).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of concept documents to scan.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args(argv)

    if args.inventory:
        report = inventory_preserved_fields(
            concept_ids=args.concept_ids,
            limit=args.limit,
        )
    else:
        report = migrate_preserved_fields_to_text_relations(
            concept_ids=args.concept_ids,
            dry_run=not bool(args.apply),
            limit=args.limit,
            purge_migrated_keys=bool(args.purge_migrated_keys),
        )

    if args.pretty:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(json.dumps(report))

    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())

