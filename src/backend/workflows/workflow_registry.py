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

    def get(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        entry = self._workflows.get(workflow_id)
        return entry.definition if entry else None

    def all_workflow_ids(self) -> Iterable[str]:
        return list(self._workflows.keys())
