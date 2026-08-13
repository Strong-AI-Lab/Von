"""Shared concurrency boundary for membership and semantic-role lifecycle writes.

Organisation membership is a prerequisite for exact-organisation ontology
authority.  Writers for the two representations therefore share one short
cross-process barrier.  The barrier is re-entrant within a request so the
one-time migration can hold it across inventory validation while continuing to
use the canonical membership and role services for each individual effect.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .ontology_publication_authority_service import (
    ontology_mutation_resource_lock,
)

_COORDINATION_RESOURCE_KEY = "ontology-authority-membership-lifecycle:v1"
_COORDINATION_DEPTH: ContextVar[int] = ContextVar(
    "ontology_authority_membership_coordination_depth",
    default=0,
)


@contextmanager
def ontology_authority_membership_mutation_barrier() -> Iterator[None]:
    """Serialise authority-relevant membership and semantic-role mutations."""

    depth = _COORDINATION_DEPTH.get()
    token = _COORDINATION_DEPTH.set(depth + 1)
    try:
        if depth:
            yield
            return
        with ontology_mutation_resource_lock(_COORDINATION_RESOURCE_KEY):
            yield
    finally:
        _COORDINATION_DEPTH.reset(token)


__all__ = ["ontology_authority_membership_mutation_barrier"]
