#!/usr/bin/env python3
"""One-off cleanup of the event-storm instance backlog (JVNAUTOSCI-2507).

Self-sustaining event cascades (paper_recommendation_evaluation_workflow and
episode_evaluation_workflow re-trigger themselves via their own
text_relation.* / relationship.* / workflow.instance_terminal bindings) left a
large backlog of pending / paused / orphaned-running instances that never drain.

This script cancels that backlog with a *direct* bulk ``update_many`` on the
workflow_instances collection. It deliberately does NOT route through
WorkflowInstanceManager.mark_cancelled, because that path emits a
``workflow.instance_terminal`` event which would spawn a fresh
episode_evaluation instance per cancellation — i.e. cancelling the backlog the
normal way would feed the very storm being drained. A direct update emits no
events (there is no change-stream watcher on the collection), so it is safe.

Dry-run by default: prints the counts it WOULD cancel and changes nothing.
Pass --execute to perform the cancellation.

Examples:
    # Inspect scope (read-only)
    .venv/bin/python scripts/cleanup_event_storm_backlog.py

    # Cancel pending + paused backlog for the two storm workflows
    .venv/bin/python scripts/cleanup_event_storm_backlog.py --execute

    # Also cancel orphaned running instances (expired / missing lock)
    .venv/bin/python scripts/cleanup_event_storm_backlog.py \
        --include-stale-running --execute
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

# Allow running from the repo root without installation.
sys.path.insert(0, ".")

# The shared Atlas cluster is heavily loaded by the very storm this script
# drains, so the default 5-10s socket timeout trips on large indexed counts.
# Raise it for this one-off maintenance run (before the client is built).
os.environ.setdefault("MONGO_SOCKET_TIMEOUT_MS", "120000")
os.environ.setdefault("MONGO_CONNECT_TIMEOUT_MS", "20000")

from src.backend.db.mongo_client import get_db  # noqa: E402

# Server-side cap so a slow count cannot pin a cluster connection indefinitely.
_COUNT_MAX_TIME_MS = 90000

WORKFLOW_INSTANCES_COLLECTION = "workflow_instances"

DEFAULT_STORM_WORKFLOWS = (
    "#V#paper_recommendation_evaluation_workflow",
    "#V#episode_evaluation_workflow",
)
DEFAULT_STATUSES = ("pending", "paused")
RUNNING_STATUS = "running"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow-id",
        action="append",
        dest="workflow_ids",
        help=(
            "Workflow concept id to clean up (repeatable). "
            f"Defaults to: {', '.join(DEFAULT_STORM_WORKFLOWS)}"
        ),
    )
    parser.add_argument(
        "--status",
        action="append",
        dest="statuses",
        help=(
            "Instance status to cancel (repeatable). "
            f"Defaults to: {', '.join(DEFAULT_STATUSES)}"
        ),
    )
    parser.add_argument(
        "--include-stale-running",
        action="store_true",
        help=(
            "Also cancel running instances whose lock has expired or is absent "
            "(orphaned mid-flight). Running instances with a live lock are never "
            "touched."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=2000,
        help="Max documents per update_many batch (default 2000).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the cancellation. Without this flag the script is read-only.",
    )
    return parser.parse_args()


def _cancel_set(now: datetime) -> dict:
    return {
        "$set": {
            "status": "cancelled",
            "completed_at": now,
            "locked_by": None,
            "lock_expires_at": None,
            "progress_message": "cancelled:event_storm_backlog_cleanup:JVNAUTOSCI-2507",
            "progress_updated_at": now,
        }
    }


def _safe_count(coll, query: dict) -> int | None:
    """Best-effort count; returns None if the loaded cluster cannot serve it."""
    try:
        return int(coll.count_documents(query, maxTimeMS=_COUNT_MAX_TIME_MS))
    except Exception as exc:  # noqa: BLE001 - report and continue
        print(f"    (count unavailable: {type(exc).__name__})")
        return None


def _bulk_cancel(
    coll, query: dict, *, batch_size: int, execute: bool
) -> tuple[int | None, int]:
    """Return (matched_count_or_None, cancelled_count).

    Dry-run reports the (best-effort) match count and cancels nothing. Execute
    cancels in index-bounded ``_id`` batches; because each batch flips the
    documents out of the matching set, the loop drains without needing an
    up-front full count (which the loaded cluster may not be able to serve).
    Uses a direct update_many, NOT mark_cancelled, so no instance_terminal
    events are emitted (that would re-spawn episode_evaluation instances).
    """
    if not execute:
        return _safe_count(coll, query), 0
    now = datetime.now(timezone.utc)
    cancelled = 0
    batch_no = 0
    while True:
        ids = [doc["_id"] for doc in coll.find(query, {"_id": 1}).limit(batch_size)]
        if not ids:
            break
        result = coll.update_many({"_id": {"$in": ids}}, _cancel_set(now))
        cancelled += int(result.modified_count)
        batch_no += 1
        print(f"    batch {batch_no}: +{result.modified_count} (running {cancelled})",
              flush=True)
        if len(ids) < batch_size:
            break
    return cancelled, cancelled


def main() -> int:
    args = _parse_args()
    workflow_ids = list(args.workflow_ids or DEFAULT_STORM_WORKFLOWS)
    statuses = list(args.statuses or DEFAULT_STATUSES)

    db = get_db()
    if db is None:
        print("ERROR: database unavailable", file=sys.stderr)
        return 1
    coll = db[WORKFLOW_INSTANCES_COLLECTION]

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"=== event-storm backlog cleanup [{mode}] ===")
    print(f"workflows: {workflow_ids}")
    print(f"statuses:  {statuses}"
          f"{' + stale running' if args.include_stale_running else ''}")
    print()

    now = datetime.now(timezone.utc)
    verb = "cancelled" if args.execute else "would cancel"
    grand_total = 0

    def _report(label: str, matched: int | None, cancelled: int) -> None:
        nonlocal grand_total
        if args.execute:
            grand_total += cancelled
            print(f"  {label}: {verb} {cancelled}")
        else:
            shown = "unknown" if matched is None else matched
            if isinstance(matched, int):
                grand_total += matched
            print(f"  {label}: {verb} {shown}")

    for workflow_id in workflow_ids:
        query = {"workflow_id": workflow_id, "status": {"$in": statuses}}
        matched, cancelled = _bulk_cancel(
            coll, query, batch_size=args.batch_size, execute=args.execute
        )
        _report(f"{workflow_id} [{','.join(statuses)}]", matched, cancelled)

        if args.include_stale_running:
            stale_query = {
                "workflow_id": workflow_id,
                "status": RUNNING_STATUS,
                "$or": [
                    {"lock_expires_at": {"$lt": now}},
                    {"lock_expires_at": None},
                    {"lock_expires_at": {"$exists": False}},
                ],
            }
            s_matched, s_cancelled = _bulk_cancel(
                coll, stale_query, batch_size=args.batch_size, execute=args.execute
            )
            _report(f"{workflow_id} [stale running]", s_matched, s_cancelled)

    print()
    print(f"TOTAL {'cancelled' if args.execute else 'to cancel (best-effort)'}: "
          f"{grand_total}")
    if not args.execute:
        print("\n(dry run — nothing changed; re-run with --execute to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
