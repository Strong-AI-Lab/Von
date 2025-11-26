import sys
import os
import time
from datetime import datetime, timezone
from bson import ObjectId

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.backend.db.connection_manager import get_db
from src.backend.models.concept_models import IndexingStatus
from src.backend.utilities.rag_indexing_worker import process_pending_interactions

def test_rag_worker():
    db = get_db()
    if db is None:
        print("Could not connect to database")
        return

    interactions_coll = db['interaction_sessions']

    # Create a dummy interaction
    interaction_id = ObjectId()
    dummy_interaction = {
        "_id": interaction_id,
        "user_identifier": "test_user",
        "session_id": "test_session",
        "concept_identifier": "test_concept",
        "concept_vontology_path": ["Concept", "Test"],
        "concept_kind": "individual",
        "concept_name_for_display": "Test Concept",
        "interactions": [
            {
                "interaction_type": "ask_question",
                "timestamp": datetime.now(timezone.utc),
                "details": {
                    "question": "What is the meaning of life?",
                    "answer": "42"
                }
            }
        ],
        "created_at": datetime.now(timezone.utc),
        "last_updated_at": datetime.now(timezone.utc),
        "indexing_status": IndexingStatus.PENDING.value
    }

    print(f"Inserting dummy interaction {interaction_id}...")
    interactions_coll.insert_one(dummy_interaction)

    print("Running worker process...")
    try:
        process_pending_interactions()
    except Exception as e:
        print(f"Worker failed: {e}")

    # Check result
    updated_interaction = interactions_coll.find_one({"_id": interaction_id})
    if updated_interaction:
        status = updated_interaction.get("indexing_status")
        embedding = updated_interaction.get("embedding")
        print(f"Updated status: {status}")
        if status == IndexingStatus.INDEXED.value:
            if embedding and len(embedding) > 0:
                print(f"Success! Embedding generated with length {len(embedding)}")
            else:
                print("Failed: Status is indexed but no embedding found.")
        else:
            print(f"Failed: Status is {status}")
    else:
        print("Failed: Interaction not found.")

    # Cleanup
    interactions_coll.delete_one({"_id": interaction_id})

if __name__ == "__main__":
    test_rag_worker()
