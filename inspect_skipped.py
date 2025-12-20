import sys
import os
from pymongo import MongoClient
import pprint

# Add src to path
sys.path.append(os.path.abspath("src"))

from backend.db.connection_manager import get_db
from backend.models.concept_models import IndexingStatus


def inspect_skipped():
    print("Inspecting skipped interactions...")
    db = get_db()
    if db is None:
        print("Could not connect to database.")
        return

    interactions_coll = db["interaction_sessions"]

    # Find one that has history but is skipped
    sample = interactions_coll.find_one(
        {
            "indexing_status": IndexingStatus.SKIPPED.value,
            "history": {"$exists": True, "$not": {"$size": 0}},
        }
    )
    if sample:
        print("Sample skipped interaction WITH history:")
        pprint.pprint(sample)
    else:
        print("No skipped interactions with history found.")

    # Check counts again
    skipped_count = interactions_coll.count_documents(
        {"indexing_status": IndexingStatus.SKIPPED.value}
    )
    print(f"Total skipped: {skipped_count}")


if __name__ == "__main__":
    inspect_skipped()
