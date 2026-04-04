from __future__ import annotations

import argparse
import importlib
import os
import sys
from dataclasses import dataclass
from typing import Any

# Ensure project root is on sys.path so `import src...` works when this file is
# executed directly.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

get_db = importlib.import_module("src.backend.db.connection_manager").get_db
derive_namespace = importlib.import_module(
    "src.backend.services.namespace_service"
).derive_namespace
sync_one_session = importlib.import_module(
    "src.backend.services.rag_sync_service"
).sync_one_session


@dataclass(frozen=True)
class ResyncStats:
    scanned: int
    updated_namespace: int
    synced_ok: int
    synced_failed: int


def _strip_v_prefix(concept_id: str) -> str:
    if concept_id.startswith("#V#"):
        return concept_id[3:]
    return concept_id


def resynchronise_user_org_sessions(
    *,
    user_concept_id: str,
    organisation_concept_id: str,
    limit: int,
    dry_run: bool,
    match_namespaces: list[str] | None = None,
) -> ResyncStats:
    db = get_db()
    if db is None:
        raise RuntimeError("Database unavailable")

    coll = db["interaction_sessions"]

    target_namespace = derive_namespace(
        _strip_v_prefix(user_concept_id),
        _strip_v_prefix(organisation_concept_id),
    )

    query: dict[str, Any] = {"indexing_status": "indexed"}
    if match_namespaces:
        query["namespace"] = {"$in": match_namespaces}
    else:
        query["user_id"] = user_concept_id
        query["organisation_concept_id"] = organisation_concept_id

    cursor = coll.find(query, {"_id": 1, "namespace": 1}).sort("indexed_at", -1)

    if limit > 0:
        cursor = cursor.limit(limit)

    scanned = 0
    updated_namespace = 0
    synced_ok = 0
    synced_failed = 0

    for doc in cursor:
        scanned += 1
        session_id = str(doc.get("_id"))
        current_ns = doc.get("namespace")

        if current_ns != target_namespace:
            if not dry_run:
                coll.update_one(
                    {"_id": doc.get("_id")},
                    {"$set": {"namespace": target_namespace}},
                )
            updated_namespace += 1

        if not dry_run:
            result: dict[str, Any] = sync_one_session(session_id)
            if result.get("success") is True:
                synced_ok += 1
            else:
                synced_failed += 1

    return ResyncStats(
        scanned=scanned,
        updated_namespace=updated_namespace,
        synced_ok=synced_ok,
        synced_failed=synced_failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Resynchronise already-indexed interaction sessions into the RAG store "
            "under the correct composite namespace (user@organisation)."
        )
    )
    parser.add_argument(
        "--user-concept-id",
        required=True,
        help='User concept id, e.g. "#V#michael_witbrock"',
    )
    parser.add_argument(
        "--organisation-concept-id",
        required=True,
        help='Organisation concept id, e.g. "#V#university_of_auckland_strong_ai_lab"',
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum number of sessions to resynchronise (0 means no limit)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report only; do not write or sync",
    )
    parser.add_argument(
        "--match-namespace",
        action="append",
        default=None,
        help=(
            "Only resynchronise sessions whose current namespace matches this value. "
            "Repeatable. If omitted, falls back to filtering by user_id + organisation_concept_id."
        ),
    )

    args = parser.parse_args()

    stats = resynchronise_user_org_sessions(
        user_concept_id=args.user_concept_id,
        organisation_concept_id=args.organisation_concept_id,
        limit=args.limit,
        dry_run=args.dry_run,
        match_namespaces=args.match_namespace,
    )

    target_namespace = derive_namespace(
        _strip_v_prefix(args.user_concept_id),
        _strip_v_prefix(args.organisation_concept_id),
    )

    print(
        "Resynchronisation complete:\n"
        f"- target namespace: {target_namespace}\n"
        f"- scanned: {stats.scanned}\n"
        f"- namespace updated: {stats.updated_namespace}\n"
        f"- synced ok: {stats.synced_ok}\n"
        f"- synced failed: {stats.synced_failed}\n"
    )


if __name__ == "__main__":
    main()
