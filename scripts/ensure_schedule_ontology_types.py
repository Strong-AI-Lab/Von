"""
Ensure Workflow Schedule types exist in Vontology.
"""

import sys
from pathlib import Path

# Add src to path
repo_root = Path(__file__).parent.parent
sys.path.append(str(repo_root))

from src.backend.services import concept_service

TYPE_WORKFLOW_SCHEDULE = "#V#workflow_schedule"
TYPE_CRON_SCHEDULE = "#V#cron_schedule"
TYPE_INTERVAL_SCHEDULE = "#V#interval_schedule"
TYPE_ONE_TIME_SCHEDULE = "#V#one_time_schedule"


def ensure_types():
    print("Checking Vontology Schedule Types...")
    try:
        # 1. Base Type
        base = None
        try:
            base = concept_service.get_concept_by_concept_id(TYPE_WORKFLOW_SCHEDULE)
        except Exception:
            pass

        if not base:
            print(f"Creating {TYPE_WORKFLOW_SCHEDULE}...")
            concept_service.create_concept(
                concept_id=TYPE_WORKFLOW_SCHEDULE,
                name="Workflow Schedule",
                description="A scheduled trigger for a durable workflow.",
                instance_of_type="#V#type",
            )
        else:
            print(f"  {TYPE_WORKFLOW_SCHEDULE} exists.")

        # 2. Subtypes
        subtypes = [
            (
                TYPE_CRON_SCHEDULE,
                "Cron Schedule",
                "A workflow schedule triggered by a cron expression.",
            ),
            (
                TYPE_INTERVAL_SCHEDULE,
                "Interval Schedule",
                "A workflow schedule triggered at a fixed interval.",
            ),
            (
                TYPE_ONE_TIME_SCHEDULE,
                "One-Time Schedule",
                "A workflow schedule triggered exactly once.",
            ),
        ]

        for type_id, name, desc in subtypes:
            concept = None
            try:
                concept = concept_service.get_concept_by_concept_id(type_id)
            except Exception:
                pass

            if not concept:
                print(f"Creating {type_id}...")
                concept_service.create_concept(
                    concept_id=type_id,
                    name=name,
                    description=desc,
                    instance_of_type="#V#type",
                    parent_concept_ids=[
                        TYPE_WORKFLOW_SCHEDULE
                    ],  # connects is_a_type_of
                )
            else:
                print(f"  {type_id} exists.")
                # Optional: Force parent link check if critical

        print("Ontology types verified.")

    except Exception as e:
        print(f"Error checking ontology: {e}")
        # If DB not running or other issue


if __name__ == "__main__":
    ensure_types()
