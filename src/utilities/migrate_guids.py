import sys
import os
import uuid
import logging

# Add src to path
# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
# Add src directory so 'backend' can be imported directly if needed
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.backend.db.mongo_client import get_concepts_collection
from src.backend.services.text_value_service import upsert_text_for_concept

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate_guids():
    logger.info("Starting GUID migration...")

    concepts_coll = get_concepts_collection()
    if concepts_coll is None:
        logger.error("Could not get concepts collection")
        return

    # Find concepts without a guid field
    query = {"guid": {"$exists": False}}
    total_to_migrate = concepts_coll.count_documents(query)
    logger.info(f"Found {total_to_migrate} concepts missing GUIDs")

    cursor = concepts_coll.find(query)

    migrated_count = 0
    error_count = 0

    for concept in cursor:
        try:
            concept_id = concept.get("concept_id")
            if not concept_id:
                logger.warning(
                    f"Skipping document with no concept_id: {concept.get('_id')}"
                )
                continue

            new_guid = str(uuid.uuid4())

            # Update document
            concepts_coll.update_one(
                {"_id": concept["_id"]}, {"$set": {"guid": new_guid}}
            )

            # Register as name
            upsert_text_for_concept(
                subject_concept_id=concept_id,
                predicate="hasName",
                text=new_guid,
                lang="en-NZ",
                context={"name_type": "CODE"},
            )

            migrated_count += 1
            if migrated_count % 100 == 0:
                logger.info(f"Migrated {migrated_count} concepts...")

        except Exception as e:
            logger.error(f"Error migrating concept {concept.get('_id')}: {e}")
            error_count += 1

    logger.info(
        f"Migration complete. Migrated: {migrated_count}, Errors: {error_count}"
    )


if __name__ == "__main__":
    migrate_guids()
