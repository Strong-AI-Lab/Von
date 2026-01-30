#!/usr/bin/env python
"""Backfill missing namespaces in chat_history collection.

This script finds all chat_history documents with null/missing/empty namespace
and derives the correct namespace from the user_id field (personal context).

This fixes JVNAUTOSCI-1015 by ensuring legacy conversations have proper namespaces,
allowing `include_legacy=False` to be safely enabled in the /history/sessions endpoint.

Usage:
    pdm run python utilities/backfill_chat_history_namespace.py --dry-run
    pdm run python utilities/backfill_chat_history_namespace.py

Examples:
    # Preview what would be updated
    pdm run python utilities/backfill_chat_history_namespace.py --dry-run

    # Execute the migration
    pdm run python utilities/backfill_chat_history_namespace.py

    # Limit to first 100 documents
    pdm run python utilities/backfill_chat_history_namespace.py --limit 100
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path so `import src...` works when this file is
# executed directly.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load .env BEFORE importing db modules so MONGO_URI is available
try:
    from dotenv import load_dotenv

    env_path = Path(project_root) / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
        print(f"Loaded environment from {env_path}")
except ImportError:
    pass

from src.backend.db.connection_manager import get_db
from src.backend.services.namespace_service import derive_namespace


@dataclass
class BackfillStats:
    """Statistics from the backfill operation."""

    scanned: int = 0
    updated: int = 0
    skipped_no_user: int = 0
    errors: int = 0
    sample_updates: list[dict[str, Any]] = field(default_factory=list)


def _normalise_user_slug(user_id: str) -> str:
    """Convert user_id to a safe slug for namespace derivation.

    Matches the normalisation in von_routes.py set_organisation().
    """
    user_slug = str(user_id)
    if user_slug.startswith("#V#"):
        user_slug = user_slug[3:]
    if "@" in user_slug:
        user_slug = user_slug.split("@", 1)[0]
    if "+" in user_slug:
        user_slug = user_slug.split("+", 1)[0]
    user_slug = re.sub(r"[^a-z0-9]+", user_slug.strip().lower(), "_").strip("_")
    return user_slug


def _derive_personal_namespace(user_id: str) -> str | None:
    """Derive the personal namespace for a user_id.

    Returns None if user_id is invalid.
    """
    if not user_id or not isinstance(user_id, str):
        return None

    user_slug = _normalise_user_slug(user_id)
    if not user_slug:
        return None

    return derive_namespace(user_slug)


def backfill_chat_history_namespaces(
    *,
    dry_run: bool = True,
    limit: int = 0,
    verbose: bool = False,
) -> BackfillStats:
    """Find and update chat_history documents with missing namespaces.

    Args:
        dry_run: If True, only report what would be updated without making changes.
        limit: Maximum number of documents to process (0 = no limit).
        verbose: If True, print each document being processed.

    Returns:
        BackfillStats with counts of documents processed.
    """
    db = get_db()
    if db is None:
        raise RuntimeError("Database unavailable")

    coll = db["chat_history"]
    stats = BackfillStats()

    # Query for documents with missing/null/empty namespace
    query: dict[str, Any] = {
        "$or": [
            {"namespace": {"$exists": False}},
            {"namespace": None},
            {"namespace": ""},
            {"namespace": " "},
        ]
    }

    # Project only fields needed for namespace derivation
    projection = {"_id": 1, "session_id": 1, "user_id": 1, "namespace": 1}

    cursor = coll.find(query, projection)
    if limit > 0:
        cursor = cursor.limit(limit)

    for doc in cursor:
        stats.scanned += 1
        doc_id = doc.get("_id")
        session_id = doc.get("session_id", str(doc_id))
        user_id = doc.get("user_id")
        current_ns = doc.get("namespace")

        # Derive the expected namespace
        new_namespace = _derive_personal_namespace(user_id)

        if not new_namespace:
            stats.skipped_no_user += 1
            if verbose:
                print(f"  SKIP {session_id}: no valid user_id ({user_id!r})")
            continue

        if verbose:
            print(f"  {session_id}: {current_ns!r} -> {new_namespace!r}")

        # Track sample updates for dry-run reporting
        if len(stats.sample_updates) < 10:
            stats.sample_updates.append(
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "old_namespace": current_ns,
                    "new_namespace": new_namespace,
                }
            )

        if not dry_run:
            try:
                coll.update_one(
                    {"_id": doc_id},
                    {"$set": {"namespace": new_namespace}},
                )
                stats.updated += 1
            except Exception as e:
                stats.errors += 1
                if verbose:
                    print(f"  ERROR updating {session_id}: {e}")
        else:
            stats.updated += 1  # Count as "would be updated" in dry-run

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill missing namespaces in chat_history collection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be updated without making changes (default: True)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute the updates (required to make changes)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of documents to process (0 = no limit)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print each document being processed",
    )

    args = parser.parse_args()

    # Default to dry-run unless --execute is explicitly passed
    dry_run = not args.execute

    if dry_run:
        print("=== DRY RUN MODE (use --execute to make changes) ===\n")
    else:
        print("=== EXECUTING UPDATES ===\n")

    stats = backfill_chat_history_namespaces(
        dry_run=dry_run,
        limit=args.limit,
        verbose=args.verbose,
    )

    print(f"\nBackfill {'preview' if dry_run else 'complete'}:")
    print(f"  - Documents scanned: {stats.scanned}")
    print(f"  - Documents {'would update' if dry_run else 'updated'}: {stats.updated}")
    print(f"  - Skipped (no valid user_id): {stats.skipped_no_user}")
    if not dry_run:
        print(f"  - Errors: {stats.errors}")

    if stats.sample_updates:
        print("\nSample updates:")
        for sample in stats.sample_updates[:5]:
            print(
                f"  {sample['session_id']}: "
                f"{sample['old_namespace']!r} -> {sample['new_namespace']!r} "
                f"(user: {sample['user_id']!r})"
            )


if __name__ == "__main__":
    main()
