"""Repository for managing workflow schedules in Vontology.

Replaces the MongoDB-based schedule persistence with a Vontology-based one.
Maps WorkflowSchedule domain models to Vontology concepts and relationships.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ...services import concept_service, text_value_service
from .models import WorkflowSchedule, ScheduleType

logger = logging.getLogger(__name__)

# --- Vontology Constants ---
TYPE_WORKFLOW_SCHEDULE = "#V#workflow_schedule"
TYPE_CRON_SCHEDULE = "#V#cron_schedule"
TYPE_INTERVAL_SCHEDULE = "#V#interval_schedule"
TYPE_ONE_TIME_SCHEDULE = "#V#one_time_schedule"

# Predicates (Text Relations)
PRED_HAS_CRON = "#V#has_cron_expression"
PRED_HAS_INTERVAL = "#V#has_interval_seconds"
PRED_NEXT_RUN = "#V#next_run_scheduled_for"
PRED_LAST_RUN = "#V#last_run_occurred_at"
PRED_INPUTS = "#V#has_workflow_input_payload"
PRED_ENABLED = "#V#is_schedule_enabled"
PRED_DESCRIPTION = "hasDescription"

# Predicates (Concept Relationships)
PRED_TRIGGERS_WORKFLOW = "#V#triggers_workflow"  # Usage in linked_to: {"predicate": "triggers workflow", "target_id": "#V#..."}


class VontologyScheduleRepository:
    """Manages persistence of WorkflowSchedule objects using Vontology."""

    def create_schedule(self, schedule: WorkflowSchedule) -> str:
        """Create a new schedule concept in Vontology.

        Args:
            schedule: The schedule domain object.

        Returns:
            The created concept_id (not the GUID, but the #V# or unique identifier).
        """
        # 1. Determine subtype
        subtype = TYPE_WORKFLOW_SCHEDULE
        if schedule.schedule_type == ScheduleType.CRON:
            subtype = TYPE_CRON_SCHEDULE
        elif schedule.schedule_type == ScheduleType.INTERVAL:
            subtype = TYPE_INTERVAL_SCHEDULE
        elif schedule.schedule_type == ScheduleType.ONCE:
            subtype = TYPE_ONE_TIME_SCHEDULE

        # 2. Prepare basic concept data
        sanitized_uuid = schedule.schedule_id.replace("-", "")
        concept_id = f"#V#schedule_{sanitized_uuid}"

        # Relationships
        # triggers workflow -> schedule.workflow_id
        linked_concepts = []
        if schedule.workflow_id:
            linked_concepts.append(
                {
                    "predicate": PRED_TRIGGERS_WORKFLOW,
                    "target_id": schedule.workflow_id,
                }
            )

        # Create the concept
        try:
            concept_service.create_concept(
                name=f"Schedule for {schedule.workflow_id}",
                concept_id=concept_id,
                description=schedule.description,
                instance_of_type=subtype,
                linked_concepts=linked_concepts,
                attributes={
                    "user_id": schedule.user_id,
                    "org_id": schedule.org_id,
                    "namespace": schedule.namespace,
                },
            )
        except Exception as e:
            logger.error(f"Failed to create schedule concept {concept_id}: {e}")
            raise RuntimeError(f"Vontology creation failed: {e}")

        # 3. Add Text Relations (properties)
        try:
            # Enabled
            self._set_text(concept_id, PRED_ENABLED, str(schedule.enabled).lower())

            # Schedule specific properties
            if schedule.schedule_type == ScheduleType.CRON and schedule.cron_expression:
                self._set_text(concept_id, PRED_HAS_CRON, schedule.cron_expression)
            elif (
                schedule.schedule_type == ScheduleType.INTERVAL
                and schedule.interval_seconds is not None
            ):
                self._set_text(
                    concept_id, PRED_HAS_INTERVAL, str(schedule.interval_seconds)
                )

            # Execution Timestamps
            if schedule.next_run_at:
                self._set_text(
                    concept_id, PRED_NEXT_RUN, schedule.next_run_at.isoformat()
                )
            if schedule.last_run_at:
                self._set_text(
                    concept_id, PRED_LAST_RUN, schedule.last_run_at.isoformat()
                )

            # Inputs
            if schedule.default_inputs:
                self._set_text(
                    concept_id, PRED_INPUTS, json.dumps(schedule.default_inputs)
                )

        except Exception as e:
            # Cleanup if partial failure? For now just log.
            logger.error(
                f"Failed to set properties for schedule concept {concept_id}: {e}"
            )

        return concept_id

    def get_schedule(self, schedule_id: str) -> Optional[WorkflowSchedule]:
        """Load a schedule by its concept ID.

        Args:
            schedule_id: The concept ID (e.g. #V#schedule_...)

        Returns:
            WorkflowSchedule or None.
        """
        try:
            concept = concept_service.get_concept_by_concept_id(schedule_id)
            if not concept:
                return None
            return self._map_concept_to_schedule(concept)
        except Exception as e:
            logger.warning(f"Error fetching schedule {schedule_id}: {e}")
            return None

    def list_schedules(
        self,
        *,
        user_id: str | None = None,
        enabled_only: bool = False,
        limit: int = 50,
    ) -> List[WorkflowSchedule]:
        """List workflow schedules.

        Args:
            user_id: Filter by user attribute.
            enabled_only: Filter by enabled status.
            limit: Max results.

        Returns:
            List of matching schedules.
        """
        # Note: Efficient filtering by attribute/text relation is tricky in current Vontology.
        # Ideally we'd use semantic search or filtered list.
        # For now, we fetch candidates and filter in memory.

        concepts, _ = concept_service.list_concepts(
            concept_id=TYPE_WORKFLOW_SCHEDULE,
            include_descendants=True,
            per_page=1000,
        )

        results = []
        for concept in concepts:
            try:
                # 1. User Filter (attribute)
                if user_id:
                    attrs = concept.get("attributes", {})
                    if attrs.get("user_id") != user_id:
                        continue

                # 2. Enabled Filter (Text Relation)
                # This is expensive (n calls).
                # Optimization: Could include text relations in list_concepts in future.
                concept_id = concept.get("concept_id", "")
                enabled_text = self._get_text(concept_id, PRED_ENABLED)
                is_enabled = enabled_text == "true"

                if enabled_only and not is_enabled:
                    continue

                schedule = self._map_concept_to_schedule(concept)
                if schedule:
                    results.append(schedule)

                if len(results) >= limit:
                    break
            except Exception as e:
                logger.warning(
                    f"Error listing schedule {concept.get('concept_id')}: {e}"
                )
                continue

        return results

    def find_due_schedules(self, limit: int = 100) -> List[WorkflowSchedule]:
        """Find schedules that are enabled and due to run.

        Returns:
            List of schedules where next_run_at <= now.
        """
        # 1. Fetch all schedule concepts
        # We assume the number of active schedules is manageable (< 1000s) for now.
        concepts, _ = concept_service.list_concepts(
            concept_id=TYPE_WORKFLOW_SCHEDULE,
            include_descendants=True,
            per_page=1000,
        )

        due_schedules = []
        now = datetime.now(timezone.utc)

        for concept in concepts:
            try:
                con_id = concept.get("concept_id")
                if not con_id:
                    continue

                # Manual filtering based on predicates
                # Ideally we would query this efficiently, but for now we iterate.

                # Check Enabled
                enabled_text = self._get_text(con_id, PRED_ENABLED)
                if enabled_text != "true":
                    continue

                # Check Next Run
                next_run_str = self._get_text(con_id, PRED_NEXT_RUN)
                if not next_run_str:
                    continue

                try:
                    next_run = datetime.fromisoformat(next_run_str)
                except ValueError:
                    continue

                # Compare timestamps (ensure timezone awareness)
                if next_run <= now:
                    schedule = self._map_concept_to_schedule(concept)
                    if schedule:
                        due_schedules.append(schedule)

                if len(due_schedules) >= limit:
                    break

            except Exception as e:
                logger.warning(
                    f"Error processing schedule candidate {concept.get('concept_id')}: {e}"
                )
                continue

        return due_schedules

    def update_schedule_after_run(
        self,
        schedule_id: str,
        *,
        next_run_at: datetime | None,
    ) -> bool:
        """Update last_run and next_run timestamps."""
        try:
            now = datetime.now(timezone.utc)
            self._set_text(schedule_id, PRED_LAST_RUN, now.isoformat())

            if next_run_at:
                self._set_text(schedule_id, PRED_NEXT_RUN, next_run_at.isoformat())
            else:
                # Disable if no next run
                self._set_text(schedule_id, PRED_ENABLED, "false")
                # Also unset next_run? We just leave it as old value or set to empty string?
                # Setting to "None" string or similar might be confusing.
                # Just marking disabled is typically enough.

            return True
        except Exception as e:
            logger.error(f"Failed to update schedule {schedule_id} after run: {e}")
            return False

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        """Enable or disable a schedule."""
        try:
            val = "true" if enabled else "false"
            self._set_text(schedule_id, PRED_ENABLED, val)
            return True
        except Exception as e:
            logger.error(f"Failed to set enabled={enabled} for {schedule_id}: {e}")
            return False

    def delete_schedule(self, schedule_id: str) -> bool:
        try:
            return concept_service.delete_concept(schedule_id)
        except Exception as e:
            logger.error(f"Failed to delete schedule {schedule_id}: {e}")
            return False

    # --- Helpers ---

    def _set_text(self, concept_id: str, predicate: str, text: str):
        """Helper to upsert a singleton text relation."""
        text_value_service.upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            policy="replace_others",
            lang="en",  # specific lang not critical for logic values, default to en
        )

    def _get_text(self, concept_id: str, predicate: str) -> Optional[str]:
        """Helper to get single text value."""
        results = text_value_service.get_texts_for_concept(
            subject_concept_id=concept_id, predicate=predicate, limit=1
        )
        if results:
            return results[0].get("text")
        return None

    def _map_concept_to_schedule(
        self, concept: Dict[str, Any]
    ) -> Optional[WorkflowSchedule]:
        """Convert Vontology concept to WorkflowSchedule object."""
        concept_id = concept.get("concept_id")
        if not concept_id:
            return None

        has_cron = self._get_text(concept_id, PRED_HAS_CRON)
        has_interval = self._get_text(concept_id, PRED_HAS_INTERVAL)
        next_run_s = self._get_text(concept_id, PRED_NEXT_RUN)
        last_run_s = self._get_text(concept_id, PRED_LAST_RUN)
        inputs_s = self._get_text(concept_id, PRED_INPUTS)
        enabled_s = self._get_text(concept_id, PRED_ENABLED)
        description = self._get_text(concept_id, PRED_DESCRIPTION)

        # Detect Type
        rels = concept.get("relationships", {})
        instances = rels.get("is_an_instance_of", [])

        sched_type = ScheduleType.ONCE  # Default
        if TYPE_CRON_SCHEDULE in instances:
            sched_type = ScheduleType.CRON
        elif TYPE_INTERVAL_SCHEDULE in instances:
            sched_type = ScheduleType.INTERVAL

        # Check explicit props if type is generic
        if has_cron:
            sched_type = ScheduleType.CRON
        elif has_interval:
            sched_type = ScheduleType.INTERVAL

        # Workflow ID from linked_to
        workflow_id = ""
        links = rels.get("linked_to", [])
        for link in links:
            if link.get("predicate") == PRED_TRIGGERS_WORKFLOW:
                target = link.get("target_id")
                if target:
                    workflow_id = target
                    break

        if not workflow_id:
            logger.warning(f"Schedule {concept_id} has no linked workflow")

        # Timestamps
        next_run_at = None
        if next_run_s:
            try:
                next_run_at = datetime.fromisoformat(next_run_s)
            except ValueError:
                pass

        last_run_at = None
        if last_run_s:
            try:
                last_run_at = datetime.fromisoformat(last_run_s)
            except ValueError:
                pass

        # Inputs
        default_inputs = {}
        if inputs_s:
            try:
                default_inputs = json.loads(inputs_s)
            except json.JSONDecodeError:
                pass

        # Metadata from attributes
        attrs = concept.get("attributes", {})

        return WorkflowSchedule(
            schedule_id=concept_id,
            workflow_id=workflow_id,
            user_id=attrs.get("user_id", ""),
            org_id=attrs.get("org_id", ""),
            namespace=attrs.get("namespace", ""),
            schedule_type=sched_type,
            interval_seconds=int(has_interval) if has_interval else None,
            cron_expression=has_cron,
            enabled=(enabled_s == "true"),
            next_run_at=next_run_at,
            last_run_at=last_run_at,
            default_inputs=default_inputs,
            description=description,
            # Timestamps
            created_at=datetime.now(timezone.utc),  # TODO: parse concept created_at
            updated_at=datetime.now(timezone.utc),
        )
