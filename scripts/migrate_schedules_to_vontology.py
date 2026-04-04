"""
Migration script to move workflow schedules from MongoDB to Vontology.

Reads from 'workflow_schedules' collection.
Writes to Vontology using VontologyScheduleRepository.
"""

import sys
import importlib
from pathlib import Path

# Add src to path so we can import backend definition
repo_root = Path(__file__).parent.parent
sys.path.append(str(repo_root))

get_db = importlib.import_module("src.backend.db.mongo_client").get_db
VontologyScheduleRepository = importlib.import_module(
    "src.backend.workflows.durable.vontology_schedule_repository"
).VontologyScheduleRepository
WorkflowSchedule = importlib.import_module(
    "src.backend.workflows.durable.models"
).WorkflowSchedule


def migrate_schedules():
    db = get_db()
    if db is None:
        print("Error: Could not connect to MongoDB.")
        sys.exit(1)

    coll = db["workflow_schedules"]
    count = coll.count_documents({})
    print(f"Found {count} schedules to migrate.")

    repo = VontologyScheduleRepository()
    migrated = 0
    errors = 0

    cursor = coll.find({})
    for doc in cursor:
        try:
            # Parse existing doc into model
            schedule = WorkflowSchedule.from_doc(doc)
            print(
                f"Migrating schedule {schedule.schedule_id} ({schedule.workflow_id})..."
            )

            # Check if already migrated (optional, but good for safety)
            # The repo creates concepts with ID #V#schedule_{uuid_no_dashes}
            # We can just let create_schedule handle upsert or fail?
            # create_concept usually fails if exists.

            repo.create_schedule(schedule)
            migrated += 1
            print("  -> Success.")
        except Exception as e:
            print(f"  -> FAILED: {e}")
            errors += 1

    print("\nMigration complete.")
    print(f"Migrated: {migrated}")
    print(f"Errors:   {errors}")


if __name__ == "__main__":
    migrate_schedules()
