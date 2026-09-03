#!/usr/bin/env python
"""Validate or apply an explicit Von login-email allow-list.

Examples:
    python -m src.backend.utilities.migrate_von_login_email_bindings \
      --binding '#V#alice=alice@example.org'

    python -m src.backend.utilities.migrate_von_login_email_bindings \
      --binding '#V#alice=alice@example.org' --apply --approved

    python -m src.backend.utilities.migrate_von_login_email_bindings \
      --preserve-pre-cutover-authority

    python -m src.backend.utilities.migrate_von_login_email_bindings \
      --preserve-pre-cutover-authority --apply --approved

The normal mode never discovers or copies ``#V#has_email`` contact values.
The explicit pre-cutover compatibility mode copies only the unambiguous legacy
bindings that the old login reader already accepted.
"""

from __future__ import annotations

import argparse
import json

from src.backend.services.von_login_email_migration_service import (
    migrate_explicit_von_login_email_bindings,
    migrate_legacy_von_login_email_bindings,
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
            "Populate hasVonLoginEmail from an explicit allow-list or the "
            "pre-cutover compatibility inventory."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--binding",
        action="append",
        type=_parse_binding,
        metavar="USER_CONCEPT_ID=EMAIL",
        help=(
            "Exact user/email login binding. Repeat for every permitted address; "
            "ordinary has_email values are never copied."
        ),
    )
    mode.add_argument(
        "--preserve-pre-cutover-authority",
        action="store_true",
        help=(
            "Copy every unambiguous has_email binding accepted by the "
            "pre-cutover login reader. Future has_email assertions remain "
            "non-authoritative."
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
        help="Explicitly approve the selected authority-bearing migration.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="Maximum detailed legacy records to include in the JSON report.",
    )
    args = parser.parse_args()

    if args.preserve_pre_cutover_authority:
        report = migrate_legacy_von_login_email_bindings(
            dry_run=not args.apply,
            approved=args.approved,
            sample_limit=args.sample_limit,
        )
    else:
        report = migrate_explicit_von_login_email_bindings(
            bindings=args.binding,
            dry_run=not args.apply,
            approved=args.approved,
        )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
