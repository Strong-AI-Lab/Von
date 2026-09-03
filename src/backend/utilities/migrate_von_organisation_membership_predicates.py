#!/usr/bin/env python
"""Audit or apply the narrow Von organisation membership migration.

Examples:
    python -m src.backend.utilities.migrate_von_organisation_membership_predicates
    python -m src.backend.utilities.migrate_von_organisation_membership_predicates \
      --role-override '#V#user=#V#organisation=owner'
    python -m src.backend.utilities.migrate_von_organisation_membership_predicates \
      --preserve-pre-cutover-authority \
      --role-override '#V#user=#V#organisation=member'
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


def _parse_role_override(raw: str) -> dict[str, str]:
    parts = [part.strip() for part in str(raw or "").split("=")]
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError(
            "role override must have the exact form USER_CONCEPT_ID=ORGANISATION_CONCEPT_ID=ROLE"
        )
    return {
        "user_concept_id": parts[0],
        "organisation_concept_id": parts[1],
        "role": parts[2],
    }


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
    parser.add_argument(
        "--preserve-pre-cutover-authority",
        action="store_true",
        help=(
            "Copy every legacy membership edge accepted by the pre-cutover "
            "reader, using its default member role when no role was stored. "
            "This does not make future generic edges authoritative."
        ),
    )
    parser.add_argument(
        "--role-override",
        action="append",
        type=_parse_role_override,
        default=[],
        metavar="USER_CONCEPT_ID=ORGANISATION_CONCEPT_ID=ROLE",
        help=(
            "Resolve one reported conflicting legacy role pair explicitly. "
            "The role must be one of the roles already evidenced for that exact pair. "
            "Repeat for every conflict; unrelated or invented overrides fail closed."
        ),
    )
    args = parser.parse_args()

    report = migrate_von_organisation_membership_predicates(
        dry_run=not args.apply,
        approved=args.approved,
        sample_limit=args.sample_limit,
        role_overrides=args.role_override,
        preserve_pre_cutover_authority=args.preserve_pre_cutover_authority,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
