#!/usr/bin/env python
"""Validate or apply an explicit Von login-email allow-list.

Examples:
    python -m src.backend.utilities.migrate_von_login_email_bindings \
      --binding '#V#alice=alice@example.org'

    python -m src.backend.utilities.migrate_von_login_email_bindings \
      --binding '#V#alice=alice@example.org' --apply --approved

The command never discovers or copies ``#V#has_email`` contact values.  Every
authority-bearing login address must appear explicitly as ``USER=EMAIL``.
"""

from __future__ import annotations

import argparse
import json

from src.backend.services.von_login_email_migration_service import (
    migrate_explicit_von_login_email_bindings,
)


def _parse_binding(raw: str) -> dict[str, str]:
    user_concept_id, separator, email = str(raw or "").partition("=")
    if not separator or not user_concept_id.strip() or not email.strip():
        raise argparse.ArgumentTypeError(
            "binding must have the exact form USER_CONCEPT_ID=EMAIL"
        )
    return {
        "user_concept_id": user_concept_id.strip(),
        "email": email.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Populate hasVonLoginEmail only from an explicit reviewed allow-list."
        )
    )
    parser.add_argument(
        "--binding",
        action="append",
        type=_parse_binding,
        required=True,
        metavar="USER_CONCEPT_ID=EMAIL",
        help=(
            "Exact user/email login binding. Repeat for every permitted address; "
            "ordinary has_email values are never copied."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the bindings; omission performs a read-only dry run.",
    )
    parser.add_argument(
        "--approved",
        action="store_true",
        help="Explicitly approve this authority-bearing allow-list.",
    )
    args = parser.parse_args()

    report = migrate_explicit_von_login_email_bindings(
        bindings=args.binding,
        dry_run=not args.apply,
        approved=args.approved,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
