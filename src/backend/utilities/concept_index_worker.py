"""Concept Index Worker (JVNAUTOSCI-680).

Background worker that polls for concepts needing (re)indexing and updates
the concept vector store in LlamaIndex.

This worker:
1. Polls for concepts where embedding_status != "indexed" or updated_at > embedding_updated_at
2. Builds aggregated searchable text for each concept (kind-specific)
3. Generates embeddings via LlamaIndex RAG service
4. Updates embedding_status and embedding_updated_at on success

Run with: pdm run python -m src.backend.utilities.concept_index_worker
"""

import time
import logging
import sys
import os
import signal

# Add src to path so we can import backend modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.backend.db.connection_manager import health_summary
from src.backend.services.concept_embedding_service import (
    CONCEPT_EMBEDDING_NAMESPACE,
    EMBEDDING_STATUS_INDEXED,
    EMBEDDING_STATUS_FAILED,
    build_concept_searchable_text,
    build_concept_embedding_metadata,
    build_concept_document_id,
    get_concepts_needing_indexing,
    count_concepts_needing_indexing,
    update_concept_embedding_status,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("concept_index_worker")

# Configuration
POLL_INTERVAL_SECONDS = int(os.getenv("CONCEPT_INDEX_POLL_INTERVAL", "30"))
BATCH_SIZE = int(os.getenv("CONCEPT_INDEX_BATCH_SIZE", "50"))

# Global flag for graceful shutdown
_shutdown_requested = False


def _flush():
    """Force stdout flush for immediate visibility."""
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown_requested
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    _shutdown_requested = True


def startup_diagnostics():
    """Log startup information for debugging."""
    logger.info("=== Concept Index Worker Startup Diagnostics ===")
    logger.info(f"POLL_INTERVAL_SECONDS={POLL_INTERVAL_SECONDS}")
    logger.info(f"BATCH_SIZE={BATCH_SIZE}")
    logger.info(f"CONCEPT_EMBEDDING_NAMESPACE={CONCEPT_EMBEDDING_NAMESPACE}")

    try:
        hs = health_summary()
        mongo_location = hs.get("effective_uri_sanitized") or hs.get("effective_uri")
        logger.info(
            "Mongo connected=%s using_fallback=%s location=%s",
            hs.get("connected"),
            hs.get("using_fallback"),
            mongo_location,
        )

        # Report initial queue state
        pending_count = count_concepts_needing_indexing()
        logger.info(f"Concepts needing indexing: {pending_count}")

    except Exception as e:
        logger.warning(f"Startup diagnostics failed: {e}")

    _flush()


def get_rag_service():
    """Get or create the RAG service for concept embeddings.

    Returns:
        LlamaIndexRAGService instance or None if unavailable
    """
    try:
        from src.backend.services.rag_backends.llamaindex_backend import (
            LlamaIndexRAGService,
        )

        return LlamaIndexRAGService()
    except ImportError as e:
        logger.error(f"LlamaIndex not available: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to initialise RAG service: {e}")
        return None


def process_concept_batch(rag_service, iteration: int) -> int:
    """Process a batch of concepts needing indexing.

    Args:
        rag_service: LlamaIndex RAG service instance
        iteration: Current loop iteration number

    Returns:
        Number of concepts successfully processed
    """
    loop_start = time.time()

    # Get concepts needing indexing
    concepts = get_concepts_needing_indexing(batch_size=BATCH_SIZE)

    if not concepts:
        # Heartbeat log when idle
        pending = count_concepts_needing_indexing()
        logger.info(
            f"[Iteration {iteration}] No pending concepts. " f"Queue depth: {pending}"
        )
        _flush()
        return 0

    pending_total = count_concepts_needing_indexing()
    logger.info(
        f"[Iteration {iteration}] Processing {len(concepts)} concepts. "
        f"Total queue depth: {pending_total}"
    )

    success_count = 0
    fail_count = 0

    for concept in concepts:
        if _shutdown_requested:
            logger.info("Shutdown requested, stopping batch processing")
            break

        concept_id = concept.get("concept_id", "")
        if not concept_id:
            logger.warning("Skipping concept with no concept_id")
            continue

        try:
            # Build searchable text
            searchable_text = build_concept_searchable_text(concept)

            if not searchable_text.strip():
                logger.warning(
                    f"No searchable text for {concept_id}, marking as indexed"
                )
                update_concept_embedding_status(concept_id, EMBEDDING_STATUS_INDEXED)
                success_count += 1
                continue

            # Build metadata
            metadata = build_concept_embedding_metadata(concept)

            # Build document for RAG
            doc_id = build_concept_document_id(concept_id)
            doc = {
                "id": doc_id,
                "text": searchable_text,
                "metadata": metadata,
            }

            # Upsert to RAG service
            success, failed = rag_service.upsert_documents(
                [doc],
                namespace=CONCEPT_EMBEDDING_NAMESPACE,
            )

            if failed > 0:
                raise RuntimeError(f"RAG upsert failed for {concept_id}")

            # Update status
            update_concept_embedding_status(concept_id, EMBEDDING_STATUS_INDEXED)
            logger.debug(f"Indexed concept {concept_id}")
            success_count += 1

        except Exception as e:
            logger.error(f"Failed to index concept {concept_id}: {e}")
            update_concept_embedding_status(
                concept_id, EMBEDDING_STATUS_FAILED, error=str(e)[:500]
            )
            fail_count += 1

    elapsed = round(time.time() - loop_start, 3)
    logger.info(
        f"[Iteration {iteration}] Batch complete in {elapsed}s. "
        f"Success: {success_count}, Failed: {fail_count}"
    )
    _flush()

    return success_count


def main():
    """Main worker loop."""
    global _shutdown_requested

    print("[stdout] Concept Index Worker starting...")
    _flush()

    # Set up signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("Starting Concept Index Worker")
    logger.info("Press Ctrl+C to stop")

    startup_diagnostics()

    # Initialise RAG service
    rag_service = get_rag_service()
    if rag_service is None:
        logger.error("Cannot start worker: RAG service unavailable")
        return 1

    logger.info("RAG service initialised successfully")
    _flush()

    iteration = 0
    while not _shutdown_requested:
        iteration += 1

        try:
            process_concept_batch(rag_service, iteration)
        except KeyboardInterrupt:
            logger.info("Worker stopped by user")
            break
        except Exception as e:
            logger.error(f"Error in worker loop: {e}")
            _flush()

        # Sleep with interruptible check
        sleep_remaining = POLL_INTERVAL_SECONDS
        while sleep_remaining > 0 and not _shutdown_requested:
            time.sleep(min(1, sleep_remaining))
            sleep_remaining -= 1

    logger.info("Concept Index Worker stopped")
    _flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
