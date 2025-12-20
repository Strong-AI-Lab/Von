"""Quick check of namespace isolation in RAG indexed sessions."""

from src.backend.db.connection_manager import get_db


def main():
    db = get_db()
    if db is None:
        print("Database not available")
        return

    coll = db["interaction_sessions"]

    # Count by indexing status
    total = coll.count_documents({})
    indexed = coll.count_documents({"indexing_status": "indexed"})
    pending = coll.count_documents({"indexing_status": "pending"})

    print(f"Total sessions: {total}")
    print(f"Indexed: {indexed}")
    print(f"Pending: {pending}\n")

    # Check namespace distribution for indexed sessions
    pipeline = [
        {"$match": {"indexing_status": "indexed"}},
        {"$group": {"_id": "$namespace", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    by_namespace = list(coll.aggregate(pipeline))

    print("Indexed sessions by namespace:")
    for item in by_namespace:
        ns = item["_id"] or "(no namespace)"
        count = item["count"]
        print(f"  {ns}: {count}")

    # Check if sessions without namespace exist
    no_ns = coll.count_documents(
        {
            "$or": [
                {"namespace": {"$exists": False}},
                {"namespace": None},
                {"namespace": ""},
            ]
        }
    )
    if no_ns > 0:
        print(f"\n⚠️  {no_ns} sessions have no namespace set!")
        print(
            "Run backfill: pdm run python src/backend/utilities/backfill_session_namespace.py"
        )


if __name__ == "__main__":
    main()
