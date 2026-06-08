import time
import logging
import importlib
import os
import signal
import sys
from datetime import datetime, timezone

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

_connection_manager = importlib.import_module("src.backend.db.connection_manager")
_mongo_client = importlib.import_module("src.backend.db.mongo_client")
get_interaction_sessions_collection = _mongo_client.get_interaction_sessions_collection
health_summary = _connection_manager.health_summary
IndexingStatus = importlib.import_module(
    "src.backend.models.concept_models"
).IndexingStatus

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("rag_worker")

POLL_INTERVAL_SECONDS = int(os.getenv("RAG_INDEX_POLL_INTERVAL_SECONDS", "30"))
BATCH_SIZE = int(os.getenv("RAG_INDEX_BATCH_SIZE", "10"))
IDLE_HEARTBEAT_EVERY = int(os.getenv("RAG_INDEX_IDLE_HEARTBEAT_EVERY", "6"))

_shutdown_requested = False


def _flush():
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _signal_handler(signum, _frame):
    global _shutdown_requested
    logger.info("Received signal %s, shutting down worker loop...", signum)
    _shutdown_requested = True


def startup_diagnostics():
    logger.info("=== RAG Worker Startup Diagnostics ===")
    logger.info(f"PYTHONUNBUFFERED={os.getenv('PYTHONUNBUFFERED')}")
    logger.info(f"PYTHONPATH={os.getenv('PYTHONPATH')}")
    logger.info(f"RAG_EMBEDDING_MODEL={os.getenv('RAG_EMBEDDING_MODEL')}")
    logger.info(
        "POLL_INTERVAL_SECONDS=%s BATCH_SIZE=%s IDLE_HEARTBEAT_EVERY=%s",
        POLL_INTERVAL_SECONDS,
        BATCH_SIZE,
        IDLE_HEARTBEAT_EVERY,
    )
    try:
        hs = health_summary()
        logger.info(
            f"Mongo connected={hs.get('connected')} using_fallback={hs.get('using_fallback')} uri={hs.get('effective_uri')}"
        )
        # Collection-level counts
        coll = get_interaction_sessions_collection()
        if coll is not None:
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


def _get_llm_client():
    # Import lazily to avoid expensive model-provider bootstrap during module import.
    from src.backend.languagemodels.llm_interface import get_llm_client

    return get_llm_client()


def process_pending_interactions(loop_iteration: int) -> int:
    loop_start = time.time()
    interactions_coll = get_interaction_sessions_collection()
    if interactions_coll is None:
        logger.error("Could not connect to database")
        return 0

    db_name = getattr(getattr(interactions_coll, "database", None), "name", "unknown")
    pending_items = list(
        interactions_coll.find({"indexing_status": IndexingStatus.PENDING.value}).limit(
            BATCH_SIZE
        )
    )

    if not pending_items:
        if loop_iteration % max(IDLE_HEARTBEAT_EVERY, 1) == 0:
            try:
                pending_count = interactions_coll.count_documents(
                    {"indexing_status": IndexingStatus.PENDING.value}
                )
                logger.info(
                    "Idle heartbeat iteration=%s db=%s pending=%s",
                    loop_iteration,
                    db_name,
                    pending_count,
                )
            except Exception:
                logger.info("Idle heartbeat iteration=%s", loop_iteration)
        _flush()
        return 0

    logger.info(
        "Iteration %s processing %s pending interactions",
        loop_iteration,
        len(pending_items),
    )

    try:
        client = _get_llm_client()
    except Exception as e:
        logger.error(f"Failed to initialise LLM client: {e}")
        _flush()
        return 0

    embedding_model = os.getenv("RAG_EMBEDDING_MODEL")
    success_count = 0
    skipped_count = 0
    failed_count = 0

    for interaction in pending_items:
        if _shutdown_requested:
            break
        interaction_id = interaction["_id"]
        try:
            # 1. Extract text from interaction payloads.
            text_content = []
            interactions = interaction.get("interactions", [])
            for entry in interactions:
                details = entry.get("details", {})
                question = details.get("question")
                answer = details.get("answer") or details.get("answer_preview")
                if question and answer:
                    text_content.append(f"Q: {question}\nA: {answer}")

            # Fallback to legacy 'history' field if 'interactions' is empty.
            if not text_content:
                history = interaction.get("history", [])
                for entry in history:
                    role = entry.get("type")
                    content = entry.get("content")
                    if content:
                        if role == "system_question":
                            text_content.append(f"Q: {content}")
                        elif role == "user_answer":
                            text_content.append(f"A: {content}")
                        else:
                            text_content.append(f"{role}: {content}")

            full_text = "\n\n".join(text_content)
            if not full_text.strip():
                interactions_coll.update_one(
                    {"_id": interaction_id},
                    {
                        "$set": {
                            "indexing_status": IndexingStatus.SKIPPED.value,
                            "indexed_at": datetime.now(timezone.utc),
                        }
                    },
                )
                skipped_count += 1
                continue

            # 2. Generate embeddings and persist indexed status.
            embedding = client.get_embedding(full_text, model=embedding_model)
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
            success_count += 1

            # 3. Trigger per-session sync to chat RAG store (best-effort).
            try:
                from src.backend.services.rag_sync_service import sync_one_session

                sess_doc = interactions_coll.find_one(
                    {"_id": interaction_id}, {"namespace": 1}
                )
                namespace = sess_doc.get("namespace") if sess_doc else None
                if not namespace:
                    namespace = os.getenv("VON_DEFAULT_NAMESPACE")
                sync_result = sync_one_session(str(interaction_id), namespace=namespace)
                if not sync_result.get("success"):
                    logger.warning(
                        "RAG sync failed for %s: %s",
                        interaction_id,
                        sync_result.get("error"),
                    )
            except Exception as sync_err:
                logger.warning(f"RAG sync error for {interaction_id}: {sync_err}")

        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to index interaction {interaction_id}: {e}")
            interactions_coll.update_one(
                {"_id": interaction_id},
                {"$set": {"indexing_status": IndexingStatus.FAILED.value}},
            )

    elapsed = round(time.time() - loop_start, 3)
    logger.info(
        "Iteration %s batch complete in %ss (indexed=%s skipped=%s failed=%s)",
        loop_iteration,
        elapsed,
        success_count,
        skipped_count,
        failed_count,
    )
    _flush()
    return success_count + skipped_count


def main():
    global _shutdown_requested
    print("[stdout] RAG Indexing Worker starting...")  # direct stdout banner
    _flush()
    logger.info("Starting RAG Indexing Worker (enhanced diagnostics)")
    logger.info("Press Ctrl+C to stop")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    startup_diagnostics()
    iteration = 0
    while not _shutdown_requested:
        iteration += 1
        try:
            processed_count = process_pending_interactions(iteration)
        except KeyboardInterrupt:
            logger.info("Worker stopped by user")
            _flush()
            break
        except Exception as e:
            logger.error(f"Error in worker loop: {e}")
            _flush()
            processed_count = 0

        sleep_seconds = POLL_INTERVAL_SECONDS
        if processed_count > 0:
            sleep_seconds = max(1, min(POLL_INTERVAL_SECONDS, 5))

        sleep_remaining = sleep_seconds
        while sleep_remaining > 0 and not _shutdown_requested:
            time.sleep(min(1, sleep_remaining))
            sleep_remaining -= 1


if __name__ == "__main__":
    main()
