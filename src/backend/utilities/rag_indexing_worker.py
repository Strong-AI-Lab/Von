import time
import logging
import sys
import os
from datetime import datetime, timezone
from pymongo import MongoClient
from bson import ObjectId

# Add src to path so we can import backend modules
# Assuming this script is in src/backend/utilities/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from src.backend.db.connection_manager import get_db
from src.backend.models.concept_models import IndexingStatus

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("rag_worker")

def process_pending_interactions():
    db = get_db()
    if db is None:
        logger.error("Could not connect to database")
        return

    interactions_coll = db['interaction_sessions']

    # Find pending interactions
    query = {"indexing_status": IndexingStatus.PENDING.value}

    # Limit to a batch size to avoid holding cursor too long if many items
    batch_size = 10
    cursor = interactions_coll.find(query).limit(batch_size)

    # Convert to list to avoid cursor timeout issues during processing
    pending_items = list(cursor)

    if not pending_items:
        return

    logger.info(f"Found {len(pending_items)} pending interactions to index")

    for interaction in pending_items:
        interaction_id = interaction['_id']
        try:
            logger.info(f"Processing interaction {interaction_id}")

            # TODO: Implement actual embedding generation and vector store insertion here
            # 1. Extract text from history (Q&A pairs)
            # 2. Generate embeddings using LLM/Embedding model
            # 3. Insert into Vector DB (e.g. Chroma, Pinecone, or Mongo Atlas Vector Search)

            # For now, we simulate success to verify the workflow
            time.sleep(0.5) # Simulate work

            # Update status to INDEXED
            interactions_coll.update_one(
                {"_id": interaction_id},
                {
                    "$set": {
                        "indexing_status": IndexingStatus.INDEXED.value,
                        "indexed_at": datetime.now(timezone.utc)
                    }
                }
            )
            logger.info(f"Successfully indexed interaction {interaction_id}")

        except Exception as e:
            logger.error(f"Failed to index interaction {interaction_id}: {e}")
            # Update status to FAILED
            interactions_coll.update_one(
                {"_id": interaction_id},
                {
                    "$set": {
                        "indexing_status": IndexingStatus.FAILED.value
                    }
                }
            )

def main():
    logger.info("Starting RAG Indexing Worker")
    logger.info("Press Ctrl+C to stop")

    while True:
        try:
            process_pending_interactions()
        except KeyboardInterrupt:
            logger.info("Worker stopped by user")
            break
        except Exception as e:
            logger.error(f"Error in worker loop: {e}")

        # Sleep for a bit before next poll
        time.sleep(5)

if __name__ == "__main__":
    main()
