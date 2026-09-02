#!/usr/bin/env python
"""Audit or apply the narrow Von organisation membership migration.

Examples:
    python -m src.backend.utilities.migrate_von_organisation_membership_predicates
    python -m src.backend.utilities.migrate_von_organisation_membership_predicates --apply --approved

The default is a read-only dry run.  Applying requires both flags so an
operator cannot turn generic ontology membership facts into authority by
accident.
"""

from __future__ import annotations

import argparse
import json

from src.backend.services.organisation_membership_predicate_migration_service import (
    migrate_von_organisation_membership_predicates,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate evidenced Von memberships to narrow predicates."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration; omission performs a read-only dry run.",
    )
    parser.add_argument(
        "--approved",
        action="store_true",
        help="Explicitly approve the authority-bearing migration.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="Maximum detailed records to include in the JSON report.",
    )
    args = parser.parse_args()

    report = migrate_von_organisation_membership_predicates(
        dry_run=not args.apply,
        approved=args.approved,
        sample_limit=args.sample_limit,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
