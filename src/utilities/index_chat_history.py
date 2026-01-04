import sys
import os
import logging
import uuid
from datetime import datetime, timezone

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.backend.db.mongo_client import get_db
from src.backend.services.rag_service import get_rag_service
from src.backend.models.chat_history_model import chat_history_collection_name

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def index_all_chat_history():
    logger.info("Starting chat history indexing...")

    db = get_db()
    if db is None:
        logger.error("Could not connect to database.")
        return

    chat_coll = db[chat_history_collection_name]
    rag = get_rag_service()

    cursor = chat_coll.find({})
    total_docs = 0
    total_messages = 0

    batch = []
    BATCH_SIZE = 100

    for session_doc in cursor:
        user_id = session_doc.get("user_id")
        session_id = session_doc.get("session_id")
        history = session_doc.get("history", [])

        if not history:
            continue

        for msg in history:
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                # Truncate if too large
                if len(content) > 5000:
                    content = content[:5000]

                # Use a deterministic ID if possible to avoid duplicates on re-run?
                # But messages don't have IDs.
                # We'll generate a new UUID. Duplicates might happen if we run this multiple times
                # without clearing, but RAG usually handles updates if ID matches.
                # Since we can't match ID, we might duplicate.
                # Ideally we should hash the content+timestamp to get a stable ID.

                # Simple stable ID generation:
                import hashlib

                msg_timestamp = msg.get("timestamp")
                if msg_timestamp:
                    if isinstance(msg_timestamp, datetime):
                        ts_str = msg_timestamp.isoformat()
                    else:
                        ts_str = str(msg_timestamp)
                else:
                    ts_str = "no_timestamp"

                unique_string = f"{user_id}_{session_id}_{ts_str}_{content[:100]}"
                doc_id = hashlib.md5(unique_string.encode("utf-8")).hexdigest()

                doc = {
                    "id": doc_id,
                    "text": content,
                    "metadata": {
                        "user_id": user_id,
                        "session_id": session_id,
                        "role": msg.get("role", "unknown"),
                        "timestamp": ts_str,
                        "type": "chat_message",
                    },
                }
                batch.append(doc)
                total_messages += 1

                if len(batch) >= BATCH_SIZE:
                    logger.info(f"Indexing batch of {len(batch)} messages...")
                    rag.upsert_documents(batch, namespace="chat_history")
                    batch = []

        total_docs += 1

    if batch:
        logger.info(f"Indexing final batch of {len(batch)} messages...")
        rag.upsert_documents(batch, namespace="chat_history")

    logger.info(
        f"Finished indexing. Processed {total_docs} sessions and {total_messages} messages."
    )


if __name__ == "__main__":
    index_all_chat_history()
