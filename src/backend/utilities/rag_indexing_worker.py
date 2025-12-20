import time
import logging
import sys
import os
from datetime import datetime, timezone
from pymongo import MongoClient
from bson import ObjectId

"""RAG Indexing Worker

Debug enhancements added:
 - Immediate stdout banner with environment details (unbuffered assurance)
 - Explicit flush after each top-level log batch
 - Startup diagnostics for: PYTHONUNBUFFERED, RAG_EMBEDDING_MODEL, effective mongo URI
 - Safety catch around get_llm_client() to detect blocking initialisation
 - Per-iteration timing + heartbeat if idle (no pending items) to prove liveness
These additions are temporary and may be reduced once stability confirmed.
"""

# Add src to path so we can import backend modules
# Assuming this script is in src/backend/utilities/
# Add project root (for src.backend imports)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
# Add src directory (for backend imports)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.backend.db.connection_manager import get_db, health_summary
from src.backend.models.concept_models import IndexingStatus
from src.backend.languagemodels.llm_interface import get_llm_client

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("rag_worker")


def _flush():
    try:
        sys.stdout.flush()
    except Exception:
        pass


def startup_diagnostics():
    logger.info("=== RAG Worker Startup Diagnostics ===")
    logger.info(f"PYTHONUNBUFFERED={os.getenv('PYTHONUNBUFFERED')}")
    logger.info(f"PYTHONPATH={os.getenv('PYTHONPATH')}")
    logger.info(f"RAG_EMBEDDING_MODEL={os.getenv('RAG_EMBEDDING_MODEL')}")
    try:
        hs = health_summary()
        logger.info(
            f"Mongo connected={hs.get('connected')} using_fallback={hs.get('using_fallback')} uri={hs.get('effective_uri')}"
        )
        # Collection-level counts
        db = get_db()
        if db is not None:
            coll = db["interaction_sessions"]
            indexed = coll.count_documents(
                {"indexing_status": IndexingStatus.INDEXED.value}
            )
            pending = coll.count_documents(
                {"indexing_status": IndexingStatus.PENDING.value}
            )
            failed = coll.count_documents(
                {"indexing_status": IndexingStatus.FAILED.value}
            )
            skipped = coll.count_documents(
                {"indexing_status": IndexingStatus.SKIPPED.value}
            )
            total = coll.count_documents({})
            logger.info(
                f"RAG counts: total={total} indexed={indexed} pending={pending} failed={failed} skipped={skipped}"
            )
    except Exception as e:
        logger.warning(f"Mongo health_summary failed: {e}")
    _flush()


def process_pending_interactions(loop_iteration: int):
    loop_start = time.time()
    logger.info(f"Checking for pending interactions... iteration={loop_iteration}")
    db = get_db()
    if db is None:
        logger.error("Could not connect to database")
        return

    interactions_coll = db["interaction_sessions"]

    # Diagnostic: report database name and raw pending count prior to query
    try:
        raw_pending_count = interactions_coll.count_documents(
            {"indexing_status": IndexingStatus.PENDING.value}
        )
        logger.info(f"DB name={db.name} raw_pending_count={raw_pending_count}")
    except Exception as e:
        logger.warning(f"Unable to count pending documents: {e}")

    # Find pending interactions
    query = {"indexing_status": IndexingStatus.PENDING.value}
    logger.info(f"Querying with: {query}")

    # Limit to a batch size to avoid holding cursor too long if many items
    batch_size = 10
    scanned = interactions_coll.count_documents({})
    eligible = interactions_coll.count_documents(
        {
            "$or": [
                {"history": {"$exists": True, "$ne": []}},
                {"summary": {"$exists": True, "$type": "string", "$ne": ""}},
            ]
        }
    )
    cursor = interactions_coll.find(query).limit(batch_size)

    # Convert to list to avoid cursor timeout issues during processing
    pending_items = list(cursor)

    if not pending_items:
        logger.info(
            "No pending items found (heartbeat). "
            f"Loop {loop_iteration}: scanned={scanned} eligible={eligible} "
            f"indexed={interactions_coll.count_documents({'indexing_status': IndexingStatus.INDEXED.value})} "
            f"pending=0 failed={interactions_coll.count_documents({'indexing_status': IndexingStatus.FAILED.value})} "
            f"skipped={interactions_coll.count_documents({'indexing_status': IndexingStatus.SKIPPED.value})}"
        )
        _flush()
        return

    logger.info(
        f"Found {len(pending_items)} pending interactions to index. "
        f"Loop {loop_iteration}: scanned={scanned} eligible={eligible} "
        f"indexed={interactions_coll.count_documents({'indexing_status': IndexingStatus.INDEXED.value})} "
        f"pending={len(pending_items)} failed={interactions_coll.count_documents({'indexing_status': IndexingStatus.FAILED.value})} "
        f"skipped={interactions_coll.count_documents({'indexing_status': IndexingStatus.SKIPPED.value})}"
    )

    # Initialize LLM client once per batch
    try:
        logger.info("Initialising LLM client...")
        client = get_llm_client()
        logger.info("LLM client initialised.")
    except Exception as e:
        logger.error(f"Failed to initialise LLM client: {e}")
        _flush()
        return

    embedding_model = os.getenv("RAG_EMBEDDING_MODEL")

    for interaction in pending_items:
        interaction_id = interaction["_id"]
        try:
            logger.info(f"Processing interaction {interaction_id}")

            # 1. Extract text from history (Q&A pairs)
            text_content = []
            interactions = interaction.get("interactions", [])
            for entry in interactions:
                details = entry.get("details", {})
                question = details.get("question")
                answer = details.get("answer") or details.get("answer_preview")
                if question and answer:
                    text_content.append(f"Q: {question}\nA: {answer}")

            # Fallback to 'history' field if 'interactions' is empty
            if not text_content:
                history = interaction.get("history", [])
                logger.info(
                    f"Checking history for {interaction_id}: found {len(history)} entries"
                )
                for entry in history:
                    role = entry.get("type")
                    content = entry.get("content")
                    logger.info(
                        f"Entry: role={role}, content_len={len(content) if content else 0}"
                    )
                    if content:
                        if role == "system_question":
                            text_content.append(f"Q: {content}")
                        elif role == "user_answer":
                            text_content.append(f"A: {content}")
                        else:
                            text_content.append(f"{role}: {content}")

            full_text = "\n\n".join(text_content)
            logger.info(f"Full text length: {len(full_text)}")

            if not full_text.strip():
                logger.warning(
                    f"No text content found for interaction {interaction_id}"
                )
                interactions_coll.update_one(
                    {"_id": interaction_id},
                    {
                        "$set": {
                            "indexing_status": IndexingStatus.SKIPPED.value,
                            "indexed_at": datetime.now(timezone.utc),
                        }
                    },
                )
                continue

            # 2. Generate embeddings using LLM/Embedding model
            embedding = client.get_embedding(full_text, model=embedding_model)

            # 3. Insert into Vector DB (Store in Mongo document for now)
            interactions_coll.update_one(
                {"_id": interaction_id},
                {
                    "$set": {
                        "embedding": embedding,
                        "indexing_status": IndexingStatus.INDEXED.value,
                        "indexed_at": datetime.now(timezone.utc),
                    }
                },
            )
            logger.info(
                f"Successfully indexed interaction {interaction_id}. "
                f"Loop {loop_iteration}: scanned={scanned} eligible={eligible} "
                f"indexed={interactions_coll.count_documents({'indexing_status': IndexingStatus.INDEXED.value})} "
                f"pending={len(pending_items)} failed={interactions_coll.count_documents({'indexing_status': IndexingStatus.FAILED.value})} "
                f"skipped={interactions_coll.count_documents({'indexing_status': IndexingStatus.SKIPPED.value})}"
            )

            # 4. Trigger per-session sync to chat RAG store (best-effort)
            try:
                from src.backend.services.rag_sync_service import sync_one_session

                # Determine namespace: prefer session namespace, else env
                try:
                    sess_doc = interactions_coll.find_one(
                        {"_id": interaction_id}, {"namespace": 1}
                    )
                    ns = sess_doc.get("namespace") if sess_doc else None
                except Exception:
                    ns = None
                if not ns:
                    ns = os.getenv("VON_DEFAULT_NAMESPACE")
                sync_result = sync_one_session(str(interaction_id), namespace=ns)
                if not sync_result.get("success"):
                    logger.warning(
                        f"RAG sync failed for {interaction_id}: {sync_result.get('error')}"
                    )
                else:
                    logger.info(
                        f"RAG sync ok for {interaction_id} namespace={sync_result.get('namespace')}"
                    )
            except Exception as sync_err:
                logger.warning(f"RAG sync error for {interaction_id}: {sync_err}")

        except Exception as e:
            logger.error(f"Failed to index interaction {interaction_id}: {e}")
            # Update status to FAILED
            interactions_coll.update_one(
                {"_id": interaction_id},
                {"$set": {"indexing_status": IndexingStatus.FAILED.value}},
            )
    elapsed = round(time.time() - loop_start, 3)
    logger.info(f"Iteration {loop_iteration} batch complete in {elapsed}s")
    _flush()


def main():
    print("[stdout] RAG Indexing Worker starting...")  # direct stdout banner
    _flush()
    logger.info("Starting RAG Indexing Worker (enhanced diagnostics)")
    logger.info("Press Ctrl+C to stop")
    startup_diagnostics()
    iteration = 0
    while True:
        iteration += 1
        try:
            process_pending_interactions(iteration)
        except KeyboardInterrupt:
            logger.info("Worker stopped by user")
            _flush()
            break
        except Exception as e:
            logger.error(f"Error in worker loop: {e}")
            _flush()
        time.sleep(5)


if __name__ == "__main__":
    main()
