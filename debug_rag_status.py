import sys
import os
from pymongo import MongoClient

# Add src to path
sys.path.append(os.path.abspath('src'))

from backend.db.connection_manager import get_db
from backend.models.concept_models import IndexingStatus

def debug_rag_status():
    print("Debugging RAG status...")
    db = get_db()
    if db is None:
        print("Could not connect to database.")
        return

    interactions_coll = db['interaction_sessions']

    # Check counts for each status
    statuses = [s.value for s in IndexingStatus]
    print(f"Checking statuses: {statuses}")

    for status in statuses:
        count = interactions_coll.count_documents({"indexing_status": status})
        print(f"Status '{status}': {count}")

    # Check for missing status
    missing_count = interactions_coll.count_documents({"indexing_status": {"$exists": False}})
    print(f"Missing status: {missing_count}")

    # Check total
    total = interactions_coll.count_documents({})
    print(f"Total documents: {total}")

    # Check if IndexingStatus.PENDING.value matches what we expect
    print(f"IndexingStatus.PENDING.value = '{IndexingStatus.PENDING.value}'")

    # Dump one pending document if exists
    pending = interactions_coll.find_one({"indexing_status": IndexingStatus.PENDING.value})
    if pending:
        print("Found pending document:")
        print(f"_id: {pending['_id']}")
        print(f"indexing_status: {pending.get('indexing_status')}")
    else:
        print("No pending document found via find_one")

if __name__ == "__main__":
    debug_rag_status()
