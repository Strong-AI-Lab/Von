"""Workflow scheduling service.

Periodically checks for due schedules and creates workflow instances.
Supports one-time, interval, and cron-based scheduling.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .instance_manager import WorkflowInstanceManager
from .models import WorkflowSchedule, ScheduleType
from .workflow_instance_submission_service import submit_verified_workflow_instance

logger = logging.getLogger(__name__)


def _parse_cron_expression(expression: str) -> dict[str, Any] | None:
    """Parse a simple cron expression.

    Supports 5-field format: minute hour day month weekday
    Fields can be:
    - * (any)
    - number (specific value)
    - */n (every n)
    - n-m (range)
    - n,m,o (list)

    Returns a dict with {minute, hour, day, month, weekday} fields,
    each containing a set of valid values or None for "any".
    """
    try:
        parts = expression.strip().split()
        if len(parts) != 5:
            return None

        result = {}
        field_names = ["minute", "hour", "day", "month", "weekday"]
        ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

        for i, (part, (min_val, max_val)) in enumerate(zip(parts, ranges)):
            if part == "*":
                result[field_names[i]] = None  # Any value
            elif part.startswith("*/"):
                step = int(part[2:])
                result[field_names[i]] = set(range(min_val, max_val + 1, step))
            elif "-" in part:
                start, end = part.split("-", 1)
                result[field_names[i]] = set(range(int(start), int(end) + 1))
            elif "," in part:
                result[field_names[i]] = set(int(v) for v in part.split(","))
            else:
                result[field_names[i]] = {int(part)}

        return result
    except Exception as e:
        logger.warning("[scheduler] Invalid cron expression '%s': %s", expression, e)
        return None


def _calculate_next_cron_run(
    cron_fields: dict[str, Any],
    after: datetime,
) -> datetime:
    """Calculate the next run time for a cron expression.

    Args:
        cron_fields: Parsed cron fields.
        after: Calculate next run after this time.

    Returns:
        Next run datetime.
    """
    # Start from the next minute
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # Search for up to 366 days (to handle annual jobs)
    max_iterations = 366 * 24 * 60
    for _ in range(max_iterations):
        # Check each field
        minute_ok = (
            cron_fields["minute"] is None or candidate.minute in cron_fields["minute"]
        )
        hour_ok = cron_fields["hour"] is None or candidate.hour in cron_fields["hour"]
        day_ok = cron_fields["day"] is None or candidate.day in cron_fields["day"]
        month_ok = (
            cron_fields["month"] is None or candidate.month in cron_fields["month"]
        )
        weekday_ok = (
            cron_fields["weekday"] is None
            or candidate.weekday() in cron_fields["weekday"]
        )

        if minute_ok and hour_ok and day_ok and month_ok and weekday_ok:
            return candidate

        # Advance by one minute
        candidate += timedelta(minutes=1)

    # Couldn't find a match, return far future
    return after + timedelta(days=365)


class WorkflowScheduler:
    """Service that triggers scheduled workflows.

    Polls for due schedules and creates workflow instances.
    Supports one-time, interval, and cron-based scheduling.
    """

    def __init__(
        self,
        instance_manager: WorkflowInstanceManager,
        *,
        check_interval_seconds: float = 60.0,
    ) -> None:
        """Initialise the scheduler.

        Args:
            instance_manager: Manager for creating instances and updating schedules.
            check_interval_seconds: Interval between schedule checks.
        """
        self._instance_manager = instance_manager
        self._check_interval = check_interval_seconds
        self._running = False
        self._shutdown_event = threading.Event()

        # Callbacks for observability
        self._on_schedule_triggered: Callable[[WorkflowSchedule, str], None] | None = (
            None
        )
        self._poll_count = 0
        self._due_schedules_seen_total = 0
        self._last_poll_metrics: dict[str, Any] = {
            "poll_count": 0,
            "due_schedules_seen_total": 0,
            "due_count": 0,
            "triggered_count": 0,
            "lookup_ms": 0.0,
            "total_ms": 0.0,
            "polled_at": None,
        }

    def set_callbacks(
        self,
        *,
        on_triggered: Callable[[WorkflowSchedule, str], None] | None = None,
    ) -> None:
        """Set observability callbacks.

        Args:
            on_triggered: Called when a schedule triggers an instance.
        """
        self._on_schedule_triggered = on_triggered

    @property
    def is_running(self) -> bool:
        """Return True if the scheduler is running."""
        return self._running

    def get_poll_metrics(self) -> dict[str, Any]:
        """Return latest schedule polling telemetry snapshot."""
        return dict(self._last_poll_metrics)

    def start(self) -> None:
        """Start the scheduler in the current thread (blocking)."""
        self._running = True
        self._shutdown_event.clear()
        logger.info("[scheduler] Starting workflow scheduler")

        try:
            while self._running:
                try:
                    self._process_due_schedules()
                except Exception as e:
                    logger.exception("[scheduler] Error processing schedules: %s", e)

                self._shutdown_event.wait(timeout=self._check_interval)
        finally:
            self._running = False
            logger.info("[scheduler] Scheduler stopped")

    def start_background(self) -> threading.Thread:
        """Start the scheduler in a background thread.

        Returns:
            The scheduler thread.
        """
        thread = threading.Thread(
            target=self.start,
            name="workflow_scheduler",
            daemon=True,
        )
        thread.start()
        return thread

    def stop(self) -> None:
        """Request graceful shutdown."""
        logger.info("[scheduler] Shutdown requested")
        self._running = False
        self._shutdown_event.set()

    def _process_due_schedules(self) -> None:
        """Find and trigger due schedules."""
        poll_started = time.perf_counter()
        lookup_started = time.perf_counter()
        due_schedules = self._instance_manager.find_due_schedules(limit=50)
        lookup_ms = (time.perf_counter() - lookup_started) * 1000.0
        now = datetime.now(timezone.utc)
        triggered_count = 0

        for schedule in due_schedules:
            try:
                self._trigger_schedule(schedule, now)
                triggered_count += 1
            except Exception as e:
                logger.exception(
                    "[scheduler] Failed to trigger schedule %s: %s",
                    schedule.schedule_id,
                    e,
                )

        total_ms = (time.perf_counter() - poll_started) * 1000.0
        self._poll_count += 1
        self._due_schedules_seen_total += len(due_schedules)
        self._last_poll_metrics = {
            "poll_count": self._poll_count,
            "due_schedules_seen_total": self._due_schedules_seen_total,
            "due_count": len(due_schedules),
            "triggered_count": triggered_count,
            "lookup_ms": round(lookup_ms, 3),
            "total_ms": round(total_ms, 3),
            "polled_at": now.isoformat(),
        }

        if due_schedules:
            logger.info(
                "[scheduler] Poll metrics: due_count=%s triggered=%s lookup_ms=%.3f total_ms=%.3f",
                len(due_schedules),
                triggered_count,
                lookup_ms,
                total_ms,
            )

    def _trigger_schedule(self, schedule: WorkflowSchedule, now: datetime) -> None:
        """Trigger a single schedule.

        Args:
            schedule: The schedule to trigger.
            now: Current timestamp.
        """
        # Create workflow instance via canonical verification pipeline.
        submission = submit_verified_workflow_instance(
            manager=self._instance_manager,
            workflow_id=schedule.workflow_id,
            user_id=schedule.user_id,
            org_id=schedule.org_id,
            namespace=schedule.namespace,
            inputs=schedule.default_inputs,
            schedule_id=schedule.schedule_id,
        )
        if not submission.success or not submission.instance_id:
            raise RuntimeError(
                "schedule_trigger_workflow_not_runnable:"
                f"{schedule.workflow_id}:{submission.error_code or 'unknown'}"
            )
        instance_id = submission.instance_id

        logger.info(
            "[scheduler] Triggered schedule %s: created instance %s",
            schedule.schedule_id,
            instance_id,
        )

        # Calculate next run time
        next_run = self._calculate_next_run(schedule, now)

        # Update schedule
        self._instance_manager.update_schedule_after_run(
            schedule.schedule_id,
            next_run_at=next_run,
        )

        # Notify callback
        if self._on_schedule_triggered:
            try:
                self._on_schedule_triggered(schedule, instance_id)
            except Exception:
                pass

    def _calculate_next_run(
        self,
        schedule: WorkflowSchedule,
        after: datetime,
    ) -> datetime | None:
        """Calculate the next run time for a schedule.

        Args:
            schedule: The schedule.
            after: Calculate next run after this time.

        Returns:
            Next run datetime, or None for one-time schedules.
        """
        if schedule.schedule_type == ScheduleType.ONCE:
            # One-time schedules don't repeat
            return None

        elif schedule.schedule_type == ScheduleType.INTERVAL:
            # Simple interval
            if schedule.interval_seconds:
                return after + timedelta(seconds=schedule.interval_seconds)
            return None

        elif schedule.schedule_type == ScheduleType.CRON:
            # Parse and calculate cron
            if not schedule.cron_expression:
                return None

            cron_fields = _parse_cron_expression(schedule.cron_expression)
            if cron_fields is None:
                logger.warning(
                    "[scheduler] Invalid cron expression for schedule %s: %s",
                    schedule.schedule_id,
                    schedule.cron_expression,
                )
                return None

            return _calculate_next_cron_run(cron_fields, after)

        return None

    def trigger_schedule_now(self, schedule_id: str) -> str | None:
        """Manually trigger a schedule immediately.

        Args:
            schedule_id: The schedule to trigger.

        Returns:
            Instance ID if triggered, None if schedule not found.
        """
        schedule = self._instance_manager.get_schedule(schedule_id)
        if schedule is None:
            return None

        now = datetime.now(timezone.utc)
        self._trigger_schedule(schedule, now)

        # Return the instance ID (we need to look it up)
        instances = self._instance_manager.list_instances(
            user_id=schedule.user_id,
            workflow_id=schedule.workflow_id,
            limit=1,
        )
        return instances[0].instance_id if instances else None


class AsyncWorkflowScheduler:
    """Async wrapper for WorkflowScheduler.

    Provides async start/stop methods for integration with async frameworks.
    """

    def __init__(
        self, instance_manager: WorkflowInstanceManager, **kwargs: Any
    ) -> None:
        """Initialise with same args as WorkflowScheduler."""
        self._scheduler = WorkflowScheduler(instance_manager, **kwargs)
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """Return True if the scheduler is running."""
        return self._scheduler.is_running

    async def start(self) -> None:
        """Start the scheduler in a background thread."""
        self._thread = self._scheduler.start_background()
        # Give it a moment to start
        await asyncio.sleep(0.1)

    async def stop(self) -> None:
        """Stop the scheduler gracefully."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._scheduler.stop)
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    async def trigger_now(self, schedule_id: str) -> str | None:
        """Manually trigger a schedule.

        Args:
            schedule_id: The schedule to trigger.

        Returns:
            Instance ID if triggered.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._scheduler.trigger_schedule_now,
            schedule_id,
        )
