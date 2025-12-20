import sys
import os
from datetime import datetime, timezone
from typing import Any, Dict

# Ensure src on path (run from repo root)
sys.path.append(os.path.abspath("src"))

from backend.db.connection_manager import get_db
from backend.models.concept_models import IndexingStatus

"""Backfill RAG Indexing Status

Marks interaction_session documents as 'pending' when:
 - indexing_status missing OR explicitly 'skipped'
 - Have usable text content: non-empty 'interactions' list with Q/A pairs OR non-empty 'history'
 - Not already indexed / failed / in-progress

This allows the rag_indexing_worker to pick them up.

Safe behaviour: only updates documents meeting content criteria. Provides summary counts.
"""


def has_text_payload(doc: Dict[str, Any]) -> bool:
    interactions = doc.get("interactions", []) or []
    for entry in interactions:
        details = entry.get("details", {})
        if details.get("question") and (
            details.get("answer") or details.get("answer_preview")
        ):
            return True
    history = doc.get("history", []) or []
    return any(h.get("content") for h in history if isinstance(h, dict))


def main():
    db = get_db()
    if db is None:
        print("Could not connect to database.")
        return
    coll = db["interaction_sessions"]

    # Gather stats
    total = coll.count_documents({})
    missing = coll.count_documents({"indexing_status": {"$exists": False}})
    skipped = coll.count_documents({"indexing_status": IndexingStatus.SKIPPED.value})
    pending = coll.count_documents({"indexing_status": IndexingStatus.PENDING.value})
    indexed = coll.count_documents({"indexing_status": IndexingStatus.INDEXED.value})
    failed = coll.count_documents({"indexing_status": IndexingStatus.FAILED.value})

    print("Before backfill:")
    print(
        f"  total={total} missing={missing} skipped={skipped} pending={pending} indexed={indexed} failed={failed}"
    )

    criteria = {
        "$or": [
            {"indexing_status": {"$exists": False}},
            {"indexing_status": IndexingStatus.SKIPPED.value},
            {"indexing_status": None},
        ],
        "indexing_status": {"$ne": IndexingStatus.INDEXED.value},
    }
    cursor = coll.find(criteria)

    to_mark = []
    for doc in cursor:
        if has_text_payload(doc):
            to_mark.append(doc["_id"])

    if not to_mark:
        print("No documents qualify for pending backfill.")
    else:
        result = coll.update_many(
            {"_id": {"$in": to_mark}},
            {
                "$set": {
                    "indexing_status": IndexingStatus.PENDING.value,
                    "pending_marked_at": datetime.now(timezone.utc),
                },
                "$unset": {"embedding": ""},  # ensure re-index if partial data existed
            },
        )
        print(
            f"Marked {result.modified_count} documents as pending (from candidates={len(to_mark)})."
        )

    # Post stats
    pending_after = coll.count_documents(
        {"indexing_status": IndexingStatus.PENDING.value}
    )
    skipped_after = coll.count_documents(
        {"indexing_status": IndexingStatus.SKIPPED.value}
    )
    missing_after = coll.count_documents({"indexing_status": {"$exists": False}})

    print("After backfill:")
    print(f"  pending={pending_after} skipped={skipped_after} missing={missing_after}")


if __name__ == "__main__":
    main()
