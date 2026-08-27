"""Shared concurrency boundary for membership and semantic-role lifecycle writes.

Organisation membership is a prerequisite for exact-organisation ontology
authority.  Writers for the two representations therefore share one short
cross-process barrier.  The barrier is re-entrant within a request so the
one-time migration can hold it across inventory validation while continuing to
use the canonical membership and role services for each individual effect.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .ontology_publication_authority_service import (
    OntologyMutationResourceBusy,
    ontology_mutation_resource_lock,
)

_COORDINATION_RESOURCE_KEY = "ontology-authority-membership-lifecycle:v1"
_COORDINATION_DEPTH: ContextVar[int] = ContextVar(
    "ontology_authority_membership_coordination_depth",
    default=0,
)
_SCOPE_COORDINATION_STACK: ContextVar[tuple[str, ...]] = ContextVar(
    "organisation_membership_scope_coordination_stack",
    default=(),
)
_SCOPE_RESOURCE_PREFIX = "organisation-membership-scope:v1:"
_SCOPE_LOCK_LEASE_SECONDS = 15
_SCOPE_LOCK_WAIT_SECONDS = 3.0
_SCOPE_LOCK_RETRY_SECONDS = 0.025


def _normalise_scope_component(value: str, *, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned}"


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


@contextmanager
def organisation_membership_scope_barrier(
    user_concept_id: str,
    organisation_concept_id: str,
) -> Iterator[None]:
    """Coordinate one actor/org membership mutation with scope recovery.

    Unlike the global authority lifecycle barrier, unrelated actor/org pairs
    use different leases and never contend.  A short bounded wait lets
    simultaneous cold-tab readers follow an in-flight mutation or one another
    instead of failing on the lock's first non-blocking acquisition attempt.
    """

    user_id = _normalise_scope_component(user_concept_id, field="user_concept_id")
    org_id = _normalise_scope_component(
        organisation_concept_id,
        field="organisation_concept_id",
    )
    resource_key = f"{_SCOPE_RESOURCE_PREFIX}{user_id}\x1f{org_id}"
    stack = _SCOPE_COORDINATION_STACK.get()
    if resource_key in stack:
        yield
        return

    deadline = time.monotonic() + _SCOPE_LOCK_WAIT_SECONDS
    lock_manager = None
    while lock_manager is None:
        candidate = ontology_mutation_resource_lock(
            resource_key,
            lease_seconds=_SCOPE_LOCK_LEASE_SECONDS,
        )
        try:
            candidate.__enter__()
        except OntologyMutationResourceBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_SCOPE_LOCK_RETRY_SECONDS)
        else:
            lock_manager = candidate

    token = _SCOPE_COORDINATION_STACK.set((*stack, resource_key))
    try:
        yield
    except BaseException as exc:
        suppress = lock_manager.__exit__(type(exc), exc, exc.__traceback__)
        if not suppress:
            raise
    else:
        lock_manager.__exit__(None, None, None)
    finally:
        _SCOPE_COORDINATION_STACK.reset(token)


__all__ = [
    "ontology_authority_membership_mutation_barrier",
    "organisation_membership_scope_barrier",
]
