"""Repository for managing workflow schedules in Vontology.

Replaces the MongoDB-based schedule persistence with a Vontology-based one.
Maps WorkflowSchedule domain models to Vontology concepts and relationships.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId

from ...db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
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

# Query-optimised relation context keys (WS5 / JVNAUTOSCI-1091).
CTX_ENABLED_BOOL = "enabled_bool"
CTX_NEXT_RUN_EPOCH_MS = "next_run_epoch_ms"
CTX_SCHEDULE_TYPE = "schedule_type"

SCHEDULE_TYPE_IDS = (
    TYPE_WORKFLOW_SCHEDULE,
    TYPE_CRON_SCHEDULE,
    TYPE_INTERVAL_SCHEDULE,
    TYPE_ONE_TIME_SCHEDULE,
)


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
                    "schedule_origin": schedule.origin,
                    "schedule_definition_identity": schedule.definition_identity,
                    "schedule_creation_context": schedule.creation_context,
                    "schedule_run_at": (
                        schedule.run_at.isoformat() if schedule.run_at else None
                    ),
                    "schedule_initialisation_complete": False,
                },
            )
        except Exception as e:
            logger.error(f"Failed to create schedule concept {concept_id}: {e}")
            raise RuntimeError(f"Vontology creation failed: {e}")

        # 3. Add Text Relations (properties)
        try:
            # Enabled
            # Publish enablement only after every execution property exists.
            self._set_enabled_state(concept_id, False)

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
                self._set_next_run_timestamp(
                    concept_id,
                    schedule.next_run_at,
                    schedule_type_id=subtype,
                )
            if schedule.last_run_at:
                self._set_text(
                    concept_id,
                    PRED_LAST_RUN,
                    self._normalise_utc_datetime(schedule.last_run_at).isoformat(),
                )

            # Inputs
            if schedule.default_inputs:
                self._set_text(
                    concept_id, PRED_INPUTS, json.dumps(schedule.default_inputs)
                )
            self._set_enabled_state(concept_id, schedule.enabled)
            concept_service.update_concept(
                concept_id, {"attributes.schedule_initialisation_complete": True}
            )

        except Exception as e:
            logger.error(
                f"Failed to set properties for schedule concept {concept_id}: {e}"
            )
            try:
                concept_service.delete_concept(concept_id)
            except Exception as cleanup_error:
                logger.error(
                    "Failed to clean up partial schedule concept %s: %s",
                    concept_id,
                    cleanup_error,
                )
            raise RuntimeError(
                f"Vontology schedule property creation failed: {e}"
            ) from e

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
        workflow_id: str | None = None,
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
        workflow_id_clean = str(workflow_id or "").strip()

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

                if workflow_id_clean:
                    relationships = concept.get("relationships", {})
                    links = (
                        relationships.get("linked_to", [])
                        if isinstance(relationships, dict)
                        else []
                    )
                    if not any(
                        link.get("predicate") == PRED_TRIGGERS_WORKFLOW
                        and str(link.get("target_id") or "").strip()
                        == workflow_id_clean
                        for link in links
                        if isinstance(link, dict)
                    ):
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
        if limit <= 0:
            return []

        now = datetime.now(timezone.utc)
        now_epoch_ms = int(now.timestamp() * 1000)
        due_schedule_ids: list[str] = []
        seen_schedule_ids: set[str] = set()
        enabled_cache: dict[str, bool] = {}
        schedule_type_cache: dict[str, bool] = {}

        # Query-first path: this targets schedules with WS5 context metadata and
        # avoids broad concept scans on every scheduler poll.
        fast_limit = max(limit * 4, 100)
        fast_cursor = TextRelationsRepository.find(
            {
                "predicate": PRED_NEXT_RUN,
                f"context.{CTX_NEXT_RUN_EPOCH_MS}": {"$lte": now_epoch_ms},
                f"context.{CTX_SCHEDULE_TYPE}": {"$in": list(SCHEDULE_TYPE_IDS)},
            },
            projection={"subject_concept_id": 1},
            sort=[(f"context.{CTX_NEXT_RUN_EPOCH_MS}", 1)],
            limit=fast_limit,
        )
        for relation in fast_cursor:
            schedule_id = str(relation.get("subject_concept_id") or "").strip()
            if not schedule_id or schedule_id in seen_schedule_ids:
                continue
            if not self._is_schedule_enabled(
                schedule_id=schedule_id, enabled_cache=enabled_cache
            ):
                continue
            due_schedule_ids.append(schedule_id)
            seen_schedule_ids.add(schedule_id)
            if len(due_schedule_ids) >= limit:
                break

        # Compatibility for WS5 records with epoch metadata but no schedule-type
        # context. Keep this bounded and verify type by concept lookup.
        if len(due_schedule_ids) < limit:
            compatibility_cursor = TextRelationsRepository.find(
                {
                    "predicate": PRED_NEXT_RUN,
                    f"context.{CTX_NEXT_RUN_EPOCH_MS}": {"$lte": now_epoch_ms},
                    f"context.{CTX_SCHEDULE_TYPE}": {"$exists": False},
                },
                projection={"subject_concept_id": 1},
                sort=[(f"context.{CTX_NEXT_RUN_EPOCH_MS}", 1)],
                limit=fast_limit,
            )
            for relation in compatibility_cursor:
                schedule_id = str(relation.get("subject_concept_id") or "").strip()
                if not schedule_id or schedule_id in seen_schedule_ids:
                    continue
                if not self._is_workflow_schedule_concept(
                    schedule_id=schedule_id, schedule_type_cache=schedule_type_cache
                ):
                    continue
                if not self._is_schedule_enabled(
                    schedule_id=schedule_id, enabled_cache=enabled_cache
                ):
                    continue
                due_schedule_ids.append(schedule_id)
                seen_schedule_ids.add(schedule_id)
                if len(due_schedule_ids) >= limit:
                    break

        # Legacy compatibility path: old schedules may not carry context metadata.
        # We still avoid concept list scans by reading only next_run relations.
        if len(due_schedule_ids) < limit:
            for relation in TextRelationsRepository.find(
                {
                    "predicate": PRED_NEXT_RUN,
                    f"context.{CTX_NEXT_RUN_EPOCH_MS}": {"$exists": False},
                },
                projection={"subject_concept_id": 1, "object_text_id": 1},
                limit=max(limit * 20, 1000),
            ):
                schedule_id = str(relation.get("subject_concept_id") or "").strip()
                if not schedule_id or schedule_id in seen_schedule_ids:
                    continue
                if not self._is_workflow_schedule_concept(
                    schedule_id=schedule_id, schedule_type_cache=schedule_type_cache
                ):
                    continue
                if not self._is_schedule_enabled(
                    schedule_id=schedule_id, enabled_cache=enabled_cache
                ):
                    continue

                next_run_text = self._get_text_value_for_relation_object(
                    relation.get("object_text_id")
                )
                next_run = self._parse_iso_datetime(next_run_text)
                if next_run is None or next_run > now:
                    continue

                due_schedule_ids.append(schedule_id)
                seen_schedule_ids.add(schedule_id)
                if len(due_schedule_ids) >= limit:
                    break

        due_schedules: list[WorkflowSchedule] = []
        for schedule_id in due_schedule_ids:
            try:
                concept = concept_service.get_concept_by_concept_id(schedule_id)
                if not concept:
                    continue
                schedule = self._map_concept_to_schedule(concept)
                if schedule is None or not schedule.enabled:
                    continue
                if schedule.next_run_at and self._normalise_utc_datetime(
                    schedule.next_run_at
                ) <= now:
                    due_schedules.append(schedule)
            except Exception as e:
                logger.warning(
                    "Error loading due schedule %s: %s",
                    schedule_id,
                    e,
                )

            if len(due_schedules) >= limit:
                break

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
                self._set_next_run_timestamp(
                    schedule_id,
                    next_run_at,
                    schedule_type_id=self._resolve_schedule_type_concept_id(schedule_id),
                )
            else:
                # Disable if no next run
                self._set_enabled_state(schedule_id, False)
                # Also unset next_run? We just leave it as old value or set to empty string?
                # Setting to "None" string or similar might be confusing.
                # Just marking disabled is typically enough.

            return True
        except Exception as e:
            logger.error(f"Failed to update schedule {schedule_id} after run: {e}")
            return False

    def update_schedule_next_run(
        self,
        schedule_id: str,
        *,
        next_run_at: datetime | None,
    ) -> bool:
        """Advance a schedule without claiming that a workflow run occurred."""

        try:
            if next_run_at:
                self._set_next_run_timestamp(
                    schedule_id,
                    next_run_at,
                    schedule_type_id=self._resolve_schedule_type_concept_id(
                        schedule_id
                    ),
                )
            else:
                self._set_enabled_state(schedule_id, False)
            return True
        except Exception as e:
            logger.error(f"Failed to advance schedule {schedule_id}: {e}")
            return False

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        """Enable or disable a schedule."""
        try:
            self._set_enabled_state(schedule_id, enabled)
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

    def _set_text(
        self,
        concept_id: str,
        predicate: str,
        text: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ):
        """Helper to upsert a singleton text relation."""
        text_value_service.upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            policy="replace_others",
            lang="en",  # specific lang not critical for logic values, default to en
            context=context,
        )

    def _set_enabled_state(self, concept_id: str, enabled: bool) -> None:
        """Persist enabled flag with queryable context metadata."""
        self._set_text(
            concept_id,
            PRED_ENABLED,
            "true" if enabled else "false",
            context={CTX_ENABLED_BOOL: bool(enabled)},
        )

    def _set_next_run_timestamp(
        self,
        concept_id: str,
        next_run_at: datetime,
        *,
        schedule_type_id: Optional[str] = None,
    ) -> None:
        """Persist next run timestamp with UTC-normalised text and epoch context."""
        next_run_utc = self._normalise_utc_datetime(next_run_at)
        relation_context: Dict[str, Any] = {
            CTX_NEXT_RUN_EPOCH_MS: int(next_run_utc.timestamp() * 1000)
        }
        if isinstance(schedule_type_id, str) and schedule_type_id in SCHEDULE_TYPE_IDS:
            relation_context[CTX_SCHEDULE_TYPE] = schedule_type_id
        self._set_text(
            concept_id,
            PRED_NEXT_RUN,
            next_run_utc.isoformat(),
            context=relation_context,
        )

    def _normalise_utc_datetime(self, value: datetime) -> datetime:
        """Normalise a datetime to timezone-aware UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _parse_iso_datetime(self, raw: Optional[str]) -> Optional[datetime]:
        """Parse an ISO timestamp string to a UTC-aware datetime."""
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return self._normalise_utc_datetime(parsed)

    def _resolve_schedule_type_concept_id(self, schedule_id: str) -> Optional[str]:
        """Resolve canonical schedule type for a schedule concept ID."""
        try:
            concept = concept_service.get_concept_by_concept_id(schedule_id)
        except Exception:
            return None
        if not isinstance(concept, dict):
            return None

        relationships = concept.get("relationships") or {}
        if not isinstance(relationships, dict):
            return None
        instances_raw = relationships.get("is_an_instance_of", [])
        if isinstance(instances_raw, str):
            instance_type_ids = [instances_raw] if instances_raw else []
        elif isinstance(instances_raw, list):
            instance_type_ids = [item for item in instances_raw if isinstance(item, str)]
        else:
            instance_type_ids = []

        for type_id in (
            TYPE_CRON_SCHEDULE,
            TYPE_INTERVAL_SCHEDULE,
            TYPE_ONE_TIME_SCHEDULE,
            TYPE_WORKFLOW_SCHEDULE,
        ):
            if type_id in instance_type_ids:
                return type_id
        return None

    def _get_text_value_for_relation_object(self, object_text_id: Any) -> Optional[str]:
        """Resolve relation object_text_id to raw text value."""
        if object_text_id is None:
            return None

        if isinstance(object_text_id, ObjectId):
            return self._get_text_value_for_object_id(object_text_id)
        if isinstance(object_text_id, str):
            try:
                return self._get_text_value_for_object_id(ObjectId(object_text_id))
            except (InvalidId, TypeError):
                doc = TextValuesRepository.find_one({"_id": object_text_id})
                if doc:
                    text = doc.get("text")
                    return text if isinstance(text, str) else None
        return None

    def _get_text_value_for_object_id(self, object_id: ObjectId) -> Optional[str]:
        doc = TextValuesRepository.find_one({"_id": object_id}, {"text": 1})
        if not doc:
            return None
        text = doc.get("text")
        return text if isinstance(text, str) else None

    def _is_schedule_enabled(
        self,
        *,
        schedule_id: str,
        enabled_cache: Optional[dict[str, bool]] = None,
    ) -> bool:
        """Resolve enabled state using context metadata first, then text fallback."""
        if enabled_cache is not None and schedule_id in enabled_cache:
            return enabled_cache[schedule_id]

        relation = TextRelationsRepository.find_one(
            {
                "subject_concept_id": schedule_id,
                "predicate": PRED_ENABLED,
            },
            projection={"context": 1, "object_text_id": 1},
        )
        if not relation:
            if enabled_cache is not None:
                enabled_cache[schedule_id] = False
            return False

        context = relation.get("context")
        if isinstance(context, dict) and isinstance(context.get(CTX_ENABLED_BOOL), bool):
            is_enabled = bool(context.get(CTX_ENABLED_BOOL))
            if enabled_cache is not None:
                enabled_cache[schedule_id] = is_enabled
            return is_enabled

        enabled_text = self._get_text_value_for_relation_object(
            relation.get("object_text_id")
        )
        is_enabled = isinstance(enabled_text, str) and enabled_text.strip().lower() == "true"
        if enabled_cache is not None:
            enabled_cache[schedule_id] = is_enabled
        return is_enabled

    def _is_workflow_schedule_concept(
        self,
        *,
        schedule_id: str,
        schedule_type_cache: Optional[dict[str, bool]] = None,
    ) -> bool:
        """Return True when schedule_id resolves to a workflow schedule concept."""
        if schedule_type_cache is not None and schedule_id in schedule_type_cache:
            return schedule_type_cache[schedule_id]

        schedule_type_id = self._resolve_schedule_type_concept_id(schedule_id)
        is_schedule = bool(
            isinstance(schedule_type_id, str) and schedule_type_id in SCHEDULE_TYPE_IDS
        )
        if schedule_type_cache is not None:
            schedule_type_cache[schedule_id] = is_schedule
        return is_schedule

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
        if (
            concept.get("attributes", {}).get("schedule_initialisation_complete")
            is False
        ):
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
        next_run_at = self._parse_iso_datetime(next_run_s)
        last_run_at = self._parse_iso_datetime(last_run_s)

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
            enabled=(enabled_s or "").strip().lower() == "true",
            next_run_at=next_run_at,
            last_run_at=last_run_at,
            default_inputs=default_inputs,
            description=description,
            run_at=self._parse_iso_datetime(attrs.get("schedule_run_at")),
            origin=attrs.get("schedule_origin", "legacy_unmanaged"),
            definition_identity=attrs.get("schedule_definition_identity", {}),
            creation_context=attrs.get("schedule_creation_context", {}),
            # Timestamps
            created_at=datetime.now(timezone.utc),  # TODO: parse concept created_at
            updated_at=datetime.now(timezone.utc),
        )
