#!/usr/bin/env python3
"""CLI utility to manually trigger concept embedding reindexing.

This script allows administrators to:
- View current embedding index statistics
- Reindex all concepts that need indexing (pending/stale)
- Force reindex specific concepts by ID
- Clear the embedding index and reindex everything

Usage:
    # Show stats
    pdm run python scripts/reindex_concepts.py --stats

    # Reindex all pending/stale concepts
    pdm run python scripts/reindex_concepts.py --reindex-pending

    # Force reindex specific concepts
    pdm run python scripts/reindex_concepts.py --reindex <concept_id1> <concept_id2>

    # Force reindex ALL concepts (clears existing embeddings)
    pdm run python scripts/reindex_concepts.py --reindex-all

    # Show status for specific concept
    pdm run python scripts/reindex_concepts.py --status <concept_id>
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import List, Optional

# Add project root to path
sys.path.insert(0, str(__file__).rsplit("scripts", 1)[0].rstrip("/\\"))

from dotenv import load_dotenv

load_dotenv()

from src.backend.services.concept_embedding_service import (
    get_concept_embedding_stats,
    get_concept_embedding_status,
    get_concepts_needing_indexing,
    update_concept_embedding_status,
    build_concept_searchable_text,
    build_concept_embedding_metadata,
    CONCEPT_EMBEDDING_NAMESPACE,
    EMBEDDING_STATUS_PENDING,
    EMBEDDING_STATUS_INDEXED,
    EMBEDDING_STATUS_STALE,
    EMBEDDING_STATUS_FAILED,
)
from src.backend.db.repositories.concepts_repository import ConceptsRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def show_stats() -> None:
    """Display current embedding index statistics."""
    stats = get_concept_embedding_stats()

    print("\n=== Concept Embedding Index Statistics ===")
    print(f"Namespace: {stats.get('namespace', CONCEPT_EMBEDDING_NAMESPACE)}")
    print(f"Total concepts: {stats.get('total_concepts', 0)}")
    print()

    status_counts = stats.get("status_counts", {})
    print("Status breakdown:")
    print(f"  - Pending:  {status_counts.get(EMBEDDING_STATUS_PENDING, 0)}")
    print(f"  - Indexed:  {status_counts.get(EMBEDDING_STATUS_INDEXED, 0)}")
    print(f"  - Stale:    {status_counts.get(EMBEDDING_STATUS_STALE, 0)}")
    print(f"  - Failed:   {status_counts.get(EMBEDDING_STATUS_FAILED, 0)}")
    print(f"  - Missing:  {status_counts.get('missing', 0)}")
    print()
    print(f"Needing indexing: {stats.get('needing_indexing', 0)}")
    print()


def show_concept_status(concept_id: str) -> None:
    """Show embedding status for a specific concept."""
    status = get_concept_embedding_status(concept_id)

    if status is None:
        print(f"Concept not found: {concept_id}")
        return

    print(f"\n=== Embedding Status for {concept_id} ===")
    print(f"Status: {status.get('embedding_status', 'unknown')}")
    print(f"Last indexed: {status.get('embedding_updated_at', 'never')}")
    print(f"Last updated: {status.get('updated_at', 'unknown')}")

    error = status.get("embedding_error")
    if error:
        print(f"Error: {error}")
    print()


def reindex_concepts(
    concept_ids: Optional[List[str]] = None,
    *,
    batch_size: int = 50,
    force: bool = False,
) -> int:
    """Reindex specified concepts or all pending/stale concepts.

    Args:
        concept_ids: Specific concepts to reindex, or None for pending/stale
        batch_size: How many concepts to process per batch
        force: If True, reindex even if already indexed

    Returns:
        Number of concepts successfully indexed
    """
    try:
        from src.backend.services.rag_service import (
            get_rag_service,
            RAGBackendUnavailable,
        )
    except ImportError:
        logger.error("Cannot import RAG service. Make sure dependencies are installed.")
        return 0

    try:
        rag_service = get_rag_service()
    except RAGBackendUnavailable as e:
        logger.error(f"RAG backend not available: {e}")
        return 0

    indexed_count = 0

    if concept_ids:
        # Reindex specific concepts
        concepts_to_process = []
        for cid in concept_ids:
            doc = ConceptsRepository.find_one({"concept_id": cid})
            if doc:
                concepts_to_process.append(doc)
            else:
                logger.warning(f"Concept not found: {cid}")
    else:
        # Get all concepts needing indexing
        concepts_to_process = get_concepts_needing_indexing(batch_size=batch_size * 100)

    if not concepts_to_process:
        logger.info("No concepts need indexing.")
        return 0

    total = len(concepts_to_process)
    logger.info(f"Processing {total} concepts...")

    # Process in batches
    for i in range(0, total, batch_size):
        batch = concepts_to_process[i : i + batch_size]
        logger.info(
            f"Processing batch {i // batch_size + 1} ({len(batch)} concepts)..."
        )

        for concept_doc in batch:
            concept_id = concept_doc.get("concept_id")
            if not concept_id:
                continue

            try:
                # Build searchable text
                searchable_text = build_concept_searchable_text(concept_doc)
                if not searchable_text or not searchable_text.strip():
                    logger.warning(f"Empty searchable text for {concept_id}, skipping")
                    update_concept_embedding_status(
                        concept_id,
                        EMBEDDING_STATUS_FAILED,
                        error="Empty searchable text",
                    )
                    continue

                # Build metadata
                metadata = build_concept_embedding_metadata(concept_doc)

                # Index via RAG service using upsert_documents
                doc_to_index = {
                    "id": concept_id,
                    "text": searchable_text,
                    **metadata,
                }
                success_count, failure_count = rag_service.upsert_documents(
                    [doc_to_index],
                    namespace=CONCEPT_EMBEDDING_NAMESPACE,
                )

                if failure_count > 0:
                    raise RuntimeError(f"Failed to upsert document")

                # Update status
                update_concept_embedding_status(concept_id, EMBEDDING_STATUS_INDEXED)
                indexed_count += 1

            except Exception as e:
                logger.error(f"Failed to index {concept_id}: {e}")
                update_concept_embedding_status(
                    concept_id, EMBEDDING_STATUS_FAILED, error=str(e)
                )

    logger.info(f"Successfully indexed {indexed_count}/{total} concepts")
    return indexed_count


def reindex_all(batch_size: int = 50) -> int:
    """Force reindex ALL concepts regardless of status.

    This marks all concepts as pending first, then reindexes them.

    Args:
        batch_size: How many concepts to process per batch

    Returns:
        Number of concepts successfully indexed
    """
    logger.warning("Marking ALL concepts as pending for reindexing...")

    # Mark all concepts as pending
    result = ConceptsRepository.update_many(
        {},
        {"$set": {"embedding_status": EMBEDDING_STATUS_PENDING}},
    )
    logger.info(f"Marked {result.modified_count} concepts as pending")

    # Now reindex
    return reindex_concepts(batch_size=batch_size)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Manage concept embedding index",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show embedding index statistics",
    )
    parser.add_argument(
        "--status",
        metavar="CONCEPT_ID",
        help="Show embedding status for a specific concept",
    )
    parser.add_argument(
        "--reindex-pending",
        action="store_true",
        help="Reindex all pending/stale concepts",
    )
    parser.add_argument(
        "--reindex",
        nargs="+",
        metavar="CONCEPT_ID",
        help="Force reindex specific concepts",
    )
    parser.add_argument(
        "--reindex-all",
        action="store_true",
        help="Force reindex ALL concepts (marks all as pending first)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Batch size for processing (default: 50)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Execute requested action
    if args.stats:
        show_stats()
    elif args.status:
        show_concept_status(args.status)
    elif args.reindex_pending:
        count = reindex_concepts(batch_size=args.batch_size)
        print(f"\nReindexed {count} concepts.")
    elif args.reindex:
        count = reindex_concepts(args.reindex, batch_size=args.batch_size)
        print(f"\nReindexed {count} concepts.")
    elif args.reindex_all:
        confirm = input("This will reindex ALL concepts. Continue? [y/N] ")
        if confirm.lower() == "y":
            count = reindex_all(batch_size=args.batch_size)
            print(f"\nReindexed {count} concepts.")
        else:
            print("Cancelled.")
    else:
        # Default: show stats
        show_stats()


if __name__ == "__main__":
    main()
