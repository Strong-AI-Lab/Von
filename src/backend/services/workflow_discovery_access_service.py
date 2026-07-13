"""Actor-scoped access support for workflow discovery.

Workflow descriptions and routing metadata remain represented authority.  This
module only propagates an authenticated/namespace actor into the existing
concept-visibility evaluator and exposes a batch result guard for derived
discovery surfaces such as the process-global capability index.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..security import access_control
from ..security.access_control import (
    cache_scope_key,
    filter_accessible_concept_ids,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
    override_current_actor,
)
from .namespace_service import (
    derive_actor_context_from_namespace,
    resolve_canonical_namespace,
)


class WorkflowDiscoveryActorScopeError(ValueError):
    """Raised when explicit discovery scope conflicts with actor authority."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "workflow_discovery_actor_scope_invalid")
        super().__init__(self.reason)


class WorkflowDiscoveryVisibilityAuthorityError(RuntimeError):
    """Raised when live workflow visibility cannot be re-evaluated safely."""

    def __init__(self, reason: str) -> None:
        self.reason = str(
            reason or "workflow_discovery_visibility_authority_unavailable"
        )
        super().__init__(self.reason)


@dataclass(frozen=True)
class WorkflowDiscoveryActorScope:
    """Verified actor scope used for one discovery or memo operation."""

    user_concept_id: str | None
    organisation_concept_id: str | None
    namespace: str | None
    cache_scope_key: str
    source: str


def filter_actor_accessible_workflow_ids(concept_ids: Iterable[Any]) -> set[str]:
    """Batch-filter workflow IDs through canonical concept visibility.

    The generic concept evaluator deliberately degrades open when its Mongo
    collection is unavailable.  That is unsafe for workflow discovery because
    its candidates can come from a warm process-global registry/capability
    index.  Actor-scoped discovery therefore requires live Vontology
    visibility authority and fails closed when acquisition or evaluation is
    unavailable.  Deliberately unscoped internal calls retain their existing
    trusted behaviour.
    """

    candidates = tuple(concept_ids)
    if not candidates:
        return set()
    if not access_control.should_enforce_access_control():
        return filter_accessible_concept_ids(candidates)
    try:
        if access_control.get_concepts_collection() is None:
            return set()
        return filter_accessible_concept_ids(candidates)
    except Exception:
        return set()


_DISCOVERY_WORKFLOW_ENTRY_KEYS = ("matches", "candidates", "routing_matches")
_DISCOVERY_WORKFLOW_DIAGNOSTIC_KEYS = ("routing_readiness_diagnostics",)
_DISCOVERY_WORKFLOW_ID_SCALAR_KEYS = (
    "selected_workflow_id",
    "workflow_id",
    "workflow_concept_id",
    "target_workflow_id",
    "preferred_workflow_id",
    "workflow_execute_target",
)
_DISCOVERY_WORKFLOW_ID_SEQUENCE_KEYS = (
    "selected_workflow_ids",
    "workflow_ids",
    "workflow_concept_ids",
    "turn_expected_workflow_concept_ids",
    "target_workflow_ids",
    "preferred_workflow_ids",
    "workflow_execute_targets",
)
_DISCOVERY_WORKFLOW_ID_MAPPING_KEYS = (
    "contract_projection",
    "turn_expected_outcome_contract",
    "turn_expected_outcome_contract_state",
)
_DISCOVERY_VISIBILITY_PROJECTION_SCHEMA = (
    "workflow_discovery_visibility_projection.v1"
)


def _workflow_id_from_discovery_entry(entry: Any) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    for key in ("concept_id", "workflow_id", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _workflow_id_from_discovery_diagnostic(entry: Any) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("workflow_id") or entry.get("concept_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _fresh_actor_accessible_workflow_ids(concept_ids: Iterable[Any]) -> set[str]:
    """Resolve workflow visibility without reusing an earlier positive cache."""

    candidates = tuple(concept_ids)
    if not candidates:
        return set()
    if not access_control.should_enforce_access_control():
        return filter_accessible_concept_ids(candidates)
    try:
        if access_control.get_concepts_collection() is None:
            raise WorkflowDiscoveryVisibilityAuthorityError(
                "workflow_discovery_visibility_authority_unavailable"
            )
        access_control.invalidate_current_access_evaluator()
        return filter_accessible_concept_ids(candidates)
    except WorkflowDiscoveryVisibilityAuthorityError:
        raise
    except Exception as exc:
        raise WorkflowDiscoveryVisibilityAuthorityError(
            "workflow_discovery_visibility_authority_unavailable"
        ) from exc


def _filter_discovery_rows(
    rows: Any,
    *,
    accessible_ids: set[str],
    diagnostic: bool = False,
) -> list[Any]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return []
    identify = (
        _workflow_id_from_discovery_diagnostic
        if diagnostic
        else _workflow_id_from_discovery_entry
    )
    return [
        copy.deepcopy(item)
        for item in rows
        if (workflow_id := identify(item)) is not None
        and workflow_id in accessible_ids
    ]


def _iter_workflow_ids_from_mapping(mapping: Mapping[str, Any]) -> Iterator[str]:
    for key in _DISCOVERY_WORKFLOW_ID_SCALAR_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            yield value.strip()
    for key in _DISCOVERY_WORKFLOW_ID_SEQUENCE_KEYS:
        values = mapping.get(key)
        if isinstance(values, Sequence) and not isinstance(
            values, (str, bytes, bytearray)
        ):
            for value in values:
                if isinstance(value, str) and value.strip():
                    yield value.strip()


def _filter_workflow_id_fields(
    mapping: dict[str, Any],
    *,
    accessible_ids: set[str],
) -> None:
    for key in _DISCOVERY_WORKFLOW_ID_SCALAR_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip() not in accessible_ids:
            mapping.pop(key, None)
    for key in _DISCOVERY_WORKFLOW_ID_SEQUENCE_KEYS:
        values = mapping.get(key)
        if isinstance(values, Sequence) and not isinstance(
            values, (str, bytes, bytearray)
        ):
            mapping[key] = [
                copy.deepcopy(value)
                for value in values
                if isinstance(value, str) and value.strip() in accessible_ids
            ]


def _remove_workflow_id_fields(mapping: dict[str, Any]) -> None:
    for key in _DISCOVERY_WORKFLOW_ID_SCALAR_KEYS:
        mapping.pop(key, None)
    for key in _DISCOVERY_WORKFLOW_ID_SEQUENCE_KEYS:
        if key in mapping:
            mapping[key] = []


def _synchronise_projected_candidate_count(
    payload: dict[str, Any],
    *,
    candidate_count: int,
) -> None:
    cache_metadata = payload.get("workflow_discovery_cache")
    if not isinstance(cache_metadata, Mapping):
        return
    cache_copy = copy.deepcopy(dict(cache_metadata))
    cache_copy["candidate_count"] = max(0, int(candidate_count))
    payload["workflow_discovery_cache"] = cache_copy


def _empty_discovery_workflow_metadata(
    payload: Mapping[str, Any],
    *,
    reason: str,
    status: str,
    input_candidate_count: int,
) -> dict[str, Any]:
    projected = copy.deepcopy(dict(payload))
    for key in _DISCOVERY_WORKFLOW_ENTRY_KEYS:
        projected[key] = []
    for key in _DISCOVERY_WORKFLOW_DIAGNOSTIC_KEYS:
        if key in projected:
            projected[key] = []
    _remove_workflow_id_fields(projected)
    for key in _DISCOVERY_WORKFLOW_ID_MAPPING_KEYS:
        nested = projected.get(key)
        if isinstance(nested, Mapping):
            nested_copy = copy.deepcopy(dict(nested))
            _remove_workflow_id_fields(nested_copy)
            projected[key] = nested_copy
    projected["candidate_count"] = 0
    projected["match_count"] = 0
    _synchronise_projected_candidate_count(projected, candidate_count=0)
    projected["match_absence_reason"] = reason
    projected["blocker_reason"] = reason
    projected["workflow_discovery_visibility_projection"] = {
        "schema_version": _DISCOVERY_VISIBILITY_PROJECTION_SCHEMA,
        "status": status,
        "reason": reason,
        "input_candidate_count": max(0, int(input_candidate_count)),
        "candidate_count": 0,
        "match_count": 0,
    }
    return projected


def project_workflow_discovery_payload_for_current_actor(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-project derived discovery metadata through live actor visibility.

    The cached or carried payload is evidence, never authority.  Candidate rows
    and their routing diagnostics are retained only when their workflow ID is
    currently visible to the bound actor.  Actor-scoped authority acquisition
    failures remove the complete derived candidate surface rather than leaking
    a previously visible workflow.
    """

    projected = copy.deepcopy(dict(payload))
    if not access_control.should_enforce_access_control():
        return projected

    workflow_ids: set[str] = set()
    input_candidate_count = 0
    has_candidate_rows_field = isinstance(
        projected.get("candidates"), Sequence
    ) and not isinstance(projected.get("candidates"), (str, bytes, bytearray))
    has_match_rows_field = isinstance(projected.get("matches"), Sequence) and not (
        isinstance(projected.get("matches"), (str, bytes, bytearray))
    )
    for key in _DISCOVERY_WORKFLOW_ENTRY_KEYS:
        rows = projected.get(key)
        if isinstance(rows, Sequence) and not isinstance(
            rows, (str, bytes, bytearray)
        ):
            input_candidate_count = max(input_candidate_count, len(rows))
            for row in rows:
                workflow_id = _workflow_id_from_discovery_entry(row)
                if workflow_id:
                    workflow_ids.add(workflow_id)
    for key in _DISCOVERY_WORKFLOW_DIAGNOSTIC_KEYS:
        rows = projected.get(key)
        if isinstance(rows, Sequence) and not isinstance(
            rows, (str, bytes, bytearray)
        ):
            for row in rows:
                workflow_id = _workflow_id_from_discovery_diagnostic(row)
                if workflow_id:
                    workflow_ids.add(workflow_id)
    workflow_ids.update(_iter_workflow_ids_from_mapping(projected))
    for key in _DISCOVERY_WORKFLOW_ID_MAPPING_KEYS:
        nested = projected.get(key)
        if isinstance(nested, Mapping):
            workflow_ids.update(_iter_workflow_ids_from_mapping(nested))

    if input_candidate_count == 0:
        raw_count = projected.get("candidate_count")
        try:
            input_candidate_count = max(0, int(raw_count))
        except (TypeError, ValueError):
            input_candidate_count = 0

    if not workflow_ids:
        has_unidentified_rows = any(
            isinstance(projected.get(key), Sequence)
            and not isinstance(projected.get(key), (str, bytes, bytearray))
            and bool(projected.get(key))
            for key in _DISCOVERY_WORKFLOW_ENTRY_KEYS
        )
        if has_unidentified_rows or input_candidate_count > 0:
            return _empty_discovery_workflow_metadata(
                projected,
                reason="workflow_discovery_candidate_identity_unavailable",
                status="rejected",
                input_candidate_count=input_candidate_count,
            )
        projected["candidate_count"] = 0
        projected["match_count"] = 0
        _synchronise_projected_candidate_count(projected, candidate_count=0)
        projected["workflow_discovery_visibility_projection"] = {
            "schema_version": _DISCOVERY_VISIBILITY_PROJECTION_SCHEMA,
            "status": "no_candidates",
            "input_candidate_count": input_candidate_count,
            "candidate_count": 0,
            "match_count": 0,
        }
        return projected

    try:
        accessible_ids = _fresh_actor_accessible_workflow_ids(workflow_ids)
    except WorkflowDiscoveryVisibilityAuthorityError as exc:
        return _empty_discovery_workflow_metadata(
            projected,
            reason=exc.reason,
            status="authority_unavailable",
            input_candidate_count=input_candidate_count,
        )

    for key in _DISCOVERY_WORKFLOW_ENTRY_KEYS:
        projected[key] = _filter_discovery_rows(
            projected.get(key),
            accessible_ids=accessible_ids,
        )
    for key in _DISCOVERY_WORKFLOW_DIAGNOSTIC_KEYS:
        if key in projected:
            projected[key] = _filter_discovery_rows(
                projected.get(key),
                accessible_ids=accessible_ids,
                diagnostic=True,
            )
    _filter_workflow_id_fields(projected, accessible_ids=accessible_ids)
    for key in _DISCOVERY_WORKFLOW_ID_MAPPING_KEYS:
        nested = projected.get(key)
        if isinstance(nested, Mapping):
            nested_copy = copy.deepcopy(dict(nested))
            _filter_workflow_id_fields(
                nested_copy,
                accessible_ids=accessible_ids,
            )
            projected[key] = nested_copy

    candidate_rows = projected.get("candidates") or []
    if not has_candidate_rows_field:
        candidate_rows = projected.get("matches") or projected.get(
            "routing_matches"
        ) or []
    match_rows = projected.get("matches") or []
    if not has_match_rows_field:
        match_rows = projected.get("routing_matches") or candidate_rows
    candidate_count = len(candidate_rows)
    match_count = len(match_rows)
    projected["candidate_count"] = candidate_count
    projected["match_count"] = match_count
    _synchronise_projected_candidate_count(
        projected,
        candidate_count=candidate_count,
    )
    removed_count = max(0, input_candidate_count - candidate_count)
    projection_status = "filtered" if removed_count else "revalidated"
    projected["workflow_discovery_visibility_projection"] = {
        "schema_version": _DISCOVERY_VISIBILITY_PROJECTION_SCHEMA,
        "status": projection_status,
        "input_candidate_count": input_candidate_count,
        "removed_candidate_count": removed_count,
        "candidate_count": candidate_count,
        "match_count": match_count,
    }
    if input_candidate_count > 0 and candidate_count == 0:
        reason = "workflow_discovery_candidates_no_longer_visible"
        projected["match_absence_reason"] = reason
        projected["blocker_reason"] = reason
    return projected


def _resolved_actor_scope(
    namespace: Any,
) -> tuple[str | None, str | None, str | None, str]:
    explicit_namespace = (
        namespace.strip() if isinstance(namespace, str) and namespace.strip() else None
    )
    namespace_user, namespace_org = derive_actor_context_from_namespace(
        explicit_namespace
    )
    if explicit_namespace and not namespace_user and not namespace_org:
        raise WorkflowDiscoveryActorScopeError("workflow_discovery_namespace_invalid")

    ambient_user = get_effective_user_concept_id()
    ambient_org = get_effective_organisation_concept_id()
    if ambient_user or ambient_org:
        if namespace_user is not None and namespace_user != ambient_user:
            raise WorkflowDiscoveryActorScopeError(
                "workflow_discovery_actor_namespace_mismatch"
            )
        if namespace_org is not None and namespace_org != ambient_org:
            # A namespace is a scope claim, not authority to add an
            # organisation that the authenticated actor does not have.
            raise WorkflowDiscoveryActorScopeError(
                "workflow_discovery_actor_namespace_mismatch"
            )
        resolved_user = ambient_user
        resolved_org = ambient_org
        canonical_namespace = resolve_canonical_namespace(
            None,
            resolved_user,
            resolved_org,
        )
        source = "ambient_actor"
    else:
        resolved_user = namespace_user
        resolved_org = namespace_org
        canonical_namespace = resolve_canonical_namespace(
            explicit_namespace,
            resolved_user,
            resolved_org,
        )
    if not (ambient_user or ambient_org) and (namespace_user or namespace_org):
        source = "namespace_actor"
    elif not (ambient_user or ambient_org):
        source = "unscoped_internal"
    return resolved_user, resolved_org, canonical_namespace, source


@contextmanager
def bind_workflow_discovery_actor(
    namespace: Any,
) -> Iterator[WorkflowDiscoveryActorScope]:
    """Bind one verified actor around discovery and memo-cache access.

    Existing authenticated/manual context remains primary.  A canonical
    namespace can supply missing context for durable/background execution, but
    conflicting explicit scope fails closed before discovery or cache lookup.
    Calls with neither actor nor namespace retain the existing trusted internal
    behaviour; request contexts still enforce anonymous visibility through the
    canonical access-control service.
    """

    resolved_user, resolved_org, canonical_namespace, source = _resolved_actor_scope(
        namespace
    )
    ambient_user = get_effective_user_concept_id()
    ambient_org = get_effective_organisation_concept_id()

    if resolved_user == ambient_user and resolved_org == ambient_org:
        yield WorkflowDiscoveryActorScope(
            user_concept_id=resolved_user,
            organisation_concept_id=resolved_org,
            namespace=canonical_namespace,
            cache_scope_key=cache_scope_key(),
            source=source,
        )
        return

    with override_current_actor(resolved_user, resolved_org):
        yield WorkflowDiscoveryActorScope(
            user_concept_id=resolved_user,
            organisation_concept_id=resolved_org,
            namespace=canonical_namespace,
            cache_scope_key=cache_scope_key(),
            source=source,
        )


def build_workflow_discovery_actor_scope_failure(
    *,
    query: Any,
    requested_query: Any = None,
    reason: str,
    origin: str,
) -> dict[str, Any]:
    """Return a metadata-free typed blocker for invalid actor propagation."""

    clean_query = str(query or "").strip()
    clean_requested_query = str(requested_query or "").strip() or clean_query
    reason_text = str(reason or "workflow_discovery_actor_scope_invalid")
    return {
        "matches": [],
        "candidates": [],
        "routing_matches": [],
        "match_count": 0,
        "candidate_count": 0,
        "query": clean_query,
        "requested_query": clean_requested_query,
        "errors": [reason_text],
        "match_absence_reason": reason_text,
        "blocker_reason": reason_text,
        "budget_exhausted": False,
        "discovery_payload_origin": origin,
        "workflow_discovery_actor_scope": {
            "status": "rejected",
            "reason": reason_text,
        },
    }


__all__ = [
    "WorkflowDiscoveryActorScope",
    "WorkflowDiscoveryActorScopeError",
    "WorkflowDiscoveryVisibilityAuthorityError",
    "bind_workflow_discovery_actor",
    "build_workflow_discovery_actor_scope_failure",
    "filter_actor_accessible_workflow_ids",
    "project_workflow_discovery_payload_for_current_actor",
]
