"""Small request-scoped progress and cancellation mechanics."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any


class CancellationRequested(Exception):
    """Raised when a background request reaches a safe cancellation point."""

    def __init__(self, task_id: str | None = None) -> None:
        self.task_id = task_id
        super().__init__(f"Cancellation requested for task {task_id or 'unknown'}")


class ProgressTracker:
    """Keep progress timing and cancellation state isolated to one request."""

    def __init__(
        self,
        callback: Callable[[Mapping[str, Any]], None] | None = None,
        *,
        cancellation_checker: Callable[[], bool] | None = None,
        task_id: str | None = None,
    ) -> None:
        self._callback = callback
        self._cancellation_checker = cancellation_checker
        self._task_id = task_id
        self._start_time: float | None = time.perf_counter() if callback else None
        self._phase_start_time: float | None = None
        self._current_phase: str | None = None
        self._phase_history: list[dict[str, Any]] = []

    @staticmethod
    def _phase_label(phase: str) -> str:
        return phase.replace("_", " ").strip().capitalize()

    @property
    def callback(self) -> Callable[[Mapping[str, Any]], None] | None:
        return self._callback

    @property
    def current_phase(self) -> str | None:
        return self._current_phase

    @property
    def phase_history(self) -> list[dict[str, Any]]:
        return list(self._phase_history)

    @property
    def task_id(self) -> str | None:
        return self._task_id

    def is_cancellation_requested(self) -> bool:
        if self._cancellation_checker is None:
            return False
        try:
            return self._cancellation_checker()
        except Exception:
            return False

    def check_cancellation(self) -> None:
        if self.is_cancellation_requested():
            self.transition_phase("cancelled")
            raise CancellationRequested(task_id=self._task_id)

    def emit(self, info: Mapping[str, Any]) -> None:
        if self._callback is None:
            return
        try:
            enriched: dict[str, Any] = dict(info)
            if self._current_phase:
                enriched.setdefault("phase", self._current_phase)
                enriched.setdefault(
                    "phase_label",
                    self._phase_label(self._current_phase),
                )
            if self._start_time is not None:
                enriched["total_elapsed_ms"] = int(
                    (time.perf_counter() - self._start_time) * 1000
                )
            if self._phase_start_time is not None:
                enriched["phase_elapsed_ms"] = int(
                    (time.perf_counter() - self._phase_start_time) * 1000
                )
            self._callback(enriched)
        except Exception:
            # Progress is best-effort and must not disrupt the request.
            pass

    def transition_phase(
        self,
        phase: str,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        now = time.perf_counter()
        if self._current_phase and self._phase_start_time is not None:
            self._phase_history.append(
                {
                    "phase": self._current_phase,
                    "phase_label": self._phase_label(self._current_phase),
                    "duration_ms": int((now - self._phase_start_time) * 1000),
                }
            )

        self._current_phase = phase
        self._phase_start_time = now
        info: dict[str, Any] = {
            "status": "phase_transition",
            "phase": phase,
            "phase_label": self._phase_label(phase),
        }
        if extra:
            info.update(extra)
        self.emit(info)
