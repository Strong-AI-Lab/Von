"""Registry for workflow definitions backed by Vontology concept metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from .engine import WorkflowDefinition


@dataclass
class WorkflowRegistration:
    workflow_id: str
    definition: WorkflowDefinition
    purpose: str | None = None
    source: str | None = None


class WorkflowRegistry:
    """Store and retrieve workflow definitions by ID."""

    def __init__(self) -> None:
        self._workflows: Dict[str, WorkflowRegistration] = {}

    def register(self, registration: WorkflowRegistration) -> None:
        if registration.workflow_id in self._workflows:
            raise ValueError(f"workflow already registered: {registration.workflow_id}")
        self._workflows[registration.workflow_id] = registration

    def register_if_absent(self, registration: WorkflowRegistration) -> bool:
        """Register *registration* only if its workflow_id is not present.

        Returns True if registered, False if skipped.
        """
        if registration.workflow_id in self._workflows:
            return False
        self._workflows[registration.workflow_id] = registration
        return True

    def register_or_replace(self, registration: WorkflowRegistration) -> bool:
        """Register *registration*, replacing any existing workflow_id entry.

        Returns True when an existing registration was replaced.
        """
        replaced = registration.workflow_id in self._workflows
        self._workflows[registration.workflow_id] = registration
        return replaced

    def has(self, workflow_id: str) -> bool:
        """Return True if *workflow_id* is registered."""
        return workflow_id in self._workflows

    def get(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        entry = self._workflows.get(workflow_id)
        return entry.definition if entry else None

    def get_registration(self, workflow_id: str) -> Optional[WorkflowRegistration]:
        """Return the full ``WorkflowRegistration`` for *workflow_id*."""
        return self._workflows.get(workflow_id)

    def all_workflow_ids(self) -> Iterable[str]:
        return list(self._workflows.keys())
