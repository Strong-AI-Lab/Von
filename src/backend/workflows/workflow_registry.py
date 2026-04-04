"""Registry for workflow definitions backed by Vontology concept metadata.

Supports both eager and lazy registration.  Eager registrations carry a
fully-loaded ``WorkflowDefinition``.  Lazy registrations record only the
workflow ID and lightweight metadata; the full definition is resolved
on first access via a pluggable *definition_loader* callback.  This
enables fast startup (~30 DB queries) while deferring the expensive
per-workflow graph materialisation (~4-8 DB queries each) until the
workflow is actually needed.

See JVNAUTOSCI-1424 Phase 1 for the design.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Dict, Iterable, Optional

from .engine import WorkflowDefinition

logger = logging.getLogger(__name__)


@dataclass
class WorkflowRegistration:
    workflow_id: str
    definition: WorkflowDefinition
    purpose: str | None = None
    source: str | None = None


@dataclass
class LazyWorkflowRegistration:
    """Placeholder for a Vontology workflow whose definition is not yet loaded.

    The ``purpose`` field carries lightweight metadata available at
    discovery time.  The full ``WorkflowDefinition`` is materialised
    on first access via the registry's *definition_loader*.
    """

    workflow_id: str
    purpose: str | None = None
    source: str | None = None
    # Populated after first successful resolution.
    _resolved: WorkflowRegistration | None = field(default=None, repr=False)


class WorkflowRegistry:
    """Store and retrieve workflow definitions by ID.

    Supports a *definition_loader* callback for lazy resolution of
    Vontology-sourced workflows.  When set, ``get()`` and
    ``get_registration()`` will transparently load the definition on
    first access rather than requiring it at registration time.
    """

    def __init__(
        self,
        *,
        definition_loader: Callable[[str], Optional[WorkflowDefinition]] | None = None,
    ) -> None:
        self._workflows: Dict[str, WorkflowRegistration] = {}
        # Lazy registrations indexed by workflow_id.
        self._lazy: Dict[str, LazyWorkflowRegistration] = {}
        self._definition_loader = definition_loader
        self._lazy_resolve_lock = Lock()

    # ------------------------------------------------------------------
    # Definition loader configuration
    # ------------------------------------------------------------------

    def set_definition_loader(
        self, loader: Callable[[str], Optional[WorkflowDefinition]] | None
    ) -> None:
        """Set or replace the lazy definition loader callback."""
        self._definition_loader = loader

    # ------------------------------------------------------------------
    # Eager registration (unchanged contract)
    # ------------------------------------------------------------------

    def register(self, registration: WorkflowRegistration) -> None:
        if registration.workflow_id in self._workflows:
            raise ValueError(f"workflow already registered: {registration.workflow_id}")
        self._workflows[registration.workflow_id] = registration
        # Evict any lazy placeholder now that we have a full registration.
        self._lazy.pop(registration.workflow_id, None)

    def register_if_absent(self, registration: WorkflowRegistration) -> bool:
        """Register *registration* only if its workflow_id is not present.

        Returns True if registered, False if skipped.
        """
        if registration.workflow_id in self._workflows:
            return False
        self._workflows[registration.workflow_id] = registration
        self._lazy.pop(registration.workflow_id, None)
        return True

    def register_or_replace(self, registration: WorkflowRegistration) -> bool:
        """Register *registration*, replacing any existing workflow_id entry.

        Returns True when an existing registration was replaced.
        """
        replaced = registration.workflow_id in self._workflows
        self._workflows[registration.workflow_id] = registration
        self._lazy.pop(registration.workflow_id, None)
        return replaced

    # ------------------------------------------------------------------
    # Lazy registration (new — JVNAUTOSCI-1424)
    # ------------------------------------------------------------------

    def register_lazy(self, lazy_reg: LazyWorkflowRegistration) -> bool:
        """Record a lazy placeholder if no eager registration exists.

        Returns True if the placeholder was stored, False if an eager
        registration already covers this workflow_id.
        """
        if lazy_reg.workflow_id in self._workflows:
            return False
        self._lazy[lazy_reg.workflow_id] = lazy_reg
        return True

    # ------------------------------------------------------------------
    # Lazy resolution (internal)
    # ------------------------------------------------------------------

    def _resolve_lazy(self, workflow_id: str) -> WorkflowRegistration | None:
        """Attempt to resolve a lazy placeholder into a full registration.

        Thread-safe: only one resolution per workflow_id at a time.
        Returns the resolved ``WorkflowRegistration`` or None.
        """
        lazy = self._lazy.get(workflow_id)
        if lazy is None:
            return None

        # Fast path: already resolved in a prior call.
        if lazy._resolved is not None:
            return lazy._resolved

        if self._definition_loader is None:
            return None

        with self._lazy_resolve_lock:
            # Double-check after acquiring lock.
            if lazy._resolved is not None:
                return lazy._resolved
            # Check if another thread eagerly registered while we waited.
            if workflow_id in self._workflows:
                lazy._resolved = self._workflows[workflow_id]
                return lazy._resolved

            t0 = time.monotonic()
            try:
                definition = self._definition_loader(workflow_id)
            except Exception as exc:
                logger.warning(
                    "[workflow_registry] Lazy load failed for %s: %s",
                    workflow_id,
                    exc,
                )
                return None
            elapsed_ms = (time.monotonic() - t0) * 1000

            if definition is None:
                logger.debug(
                    "[workflow_registry] Lazy load returned None for %s (%.0fms)",
                    workflow_id,
                    elapsed_ms,
                )
                return None

            registration = WorkflowRegistration(
                workflow_id=workflow_id,
                definition=definition,
                purpose=lazy.purpose or definition.purpose,
                source=lazy.source,
            )
            # Promote to eager so subsequent lookups are free.
            self._workflows[workflow_id] = registration
            lazy._resolved = registration
            logger.info(
                "[workflow_registry] Lazy-loaded workflow %s (%.0fms)",
                workflow_id,
                elapsed_ms,
            )
            return registration

    # ------------------------------------------------------------------
    # Lookup (lazy-aware)
    # ------------------------------------------------------------------

    def has(self, workflow_id: str) -> bool:
        """Return True if *workflow_id* is registered (eager or lazy)."""
        return workflow_id in self._workflows or workflow_id in self._lazy

    def get(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        entry = self._workflows.get(workflow_id)
        if entry is not None:
            return entry.definition
        # Attempt lazy resolution.
        resolved = self._resolve_lazy(workflow_id)
        return resolved.definition if resolved is not None else None

    def get_registration(self, workflow_id: str) -> Optional[WorkflowRegistration]:
        """Return the full ``WorkflowRegistration`` for *workflow_id*.

        For lazy registrations, this triggers on-demand loading of the
        workflow definition from Vontology.
        """
        reg = self._workflows.get(workflow_id)
        if reg is not None:
            return reg
        return self._resolve_lazy(workflow_id)

    def peek_registration(
        self,
        workflow_id: str,
    ) -> WorkflowRegistration | LazyWorkflowRegistration | None:
        """Return eager or lazy registration metadata without resolving lazily."""

        reg = self._workflows.get(workflow_id)
        if reg is not None:
            return reg
        return self._lazy.get(workflow_id)

    def get_registration_source(
        self,
        workflow_id: str,
        *,
        resolve_lazy: bool = False,
    ) -> str | None:
        """Return the registration source for *workflow_id*.

        Introspection/reporting call paths often need source metadata without
        triggering lazy definition resolution. By default this consults eager
        registrations first, then lazy placeholders, and only resolves lazily
        when ``resolve_lazy`` is explicitly requested.
        """

        reg = self._workflows.get(workflow_id)
        if reg is not None:
            source = reg.source
            if isinstance(source, str) and source.strip():
                return source.strip()
            return None

        lazy = self._lazy.get(workflow_id)
        if lazy is not None and not resolve_lazy:
            source = lazy.source
            if isinstance(source, str) and source.strip():
                return source.strip()
            return None

        resolved = self._resolve_lazy(workflow_id) if resolve_lazy else None
        if resolved is None:
            return None
        source = resolved.source
        if isinstance(source, str) and source.strip():
            return source.strip()
        return None

    def all_workflow_ids(self) -> Iterable[str]:
        """Return all known workflow IDs (both eager and lazy)."""
        ids = set(self._workflows.keys())
        ids.update(self._lazy.keys())
        return sorted(ids)

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def eager_workflow_ids(self) -> Iterable[str]:
        """Return only eagerly-loaded workflow IDs."""
        return list(self._workflows.keys())

    def lazy_workflow_ids(self) -> Iterable[str]:
        """Return workflow IDs that are registered lazily (not yet loaded)."""
        return [wid for wid in self._lazy if wid not in self._workflows]

    def lazy_registration_count(self) -> int:
        """Number of lazy placeholders that have not yet been resolved."""
        return sum(1 for wid in self._lazy if wid not in self._workflows)
