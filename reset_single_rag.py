import sys
import os
from pymongo import MongoClient
from bson import ObjectId

# Add src to path
sys.path.append(os.path.abspath("src"))

from backend.db.connection_manager import get_db
from backend.models.concept_models import IndexingStatus


def reset_single_interaction():
    target_id = ObjectId("682deaa0537fd5421647e5a3")
    print(f"Resetting interaction {target_id} to PENDING...")
    db = get_db()
    if db is None:
        print("Could not connect to database.")
        return

    interactions_coll = db["interaction_sessions"]

    result = interactions_coll.update_one(
        {"_id": target_id}, {"$set": {"indexing_status": IndexingStatus.PENDING.value}}
    )

    print(f"Modified {result.modified_count} documents.")


if __name__ == "__main__":
    reset_single_interaction()
