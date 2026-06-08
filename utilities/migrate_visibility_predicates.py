#!/usr/bin/env python3
"""Audit or migrate legacy visibility predicate storage.

Default mode is read-only. Use ``--apply`` only after reviewing dry-run output.
The output is intentionally aggregate plus bounded concept-id samples; it does
not print concept bodies.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.services.visibility_predicate_migration_service import (  # noqa: E402
    audit_visibility_predicate_storage,
    migrate_visibility_predicate_storage,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit or migrate legacy specific_to visibility predicates.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply canonical merge/removal. Omit for dry-run.",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Only report aggregate storage counts; do not build migration changes.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="Maximum concept-id samples to include.",
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=0,
        help="Maximum matching concepts to scan. Zero means no limit.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    scan_limit = args.scan_limit if args.scan_limit and args.scan_limit > 0 else None
    if args.audit_only:
        payload = audit_visibility_predicate_storage(
            sample_limit=args.sample_limit,
            scan_limit=scan_limit,
        )
    else:
        payload = migrate_visibility_predicate_storage(
            dry_run=not args.apply,
            sample_limit=args.sample_limit,
            scan_limit=scan_limit,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("error_count", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
