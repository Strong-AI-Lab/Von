"""Dedicated, optimistic, provenance-bearing ontology scope transitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from ..security.access_control import (
    can_access_concept,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from ..security.visibility_predicates import (
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
    CANONICAL_SPECIFIC_TO_USER_PREDICATE,
)
from .ontology_mutation_command_service import scope_read_back
from .ontology_publication_authority_service import (
    OntologyMutationIntent,
    OntologyMutationResourceBusy,
    PublicationContext,
    PublicationContextKind,
    authorise_ontology_mutation,
    current_ontology_invocation,
    execute_authorised_ontology_mutation,
    expand_publication_contexts,
    ontology_authority_denial_payload,
    ontology_authority_resource_keys,
    ontology_mutation_resource_lock,
    publication_context_from_mapping,
)
from .relationship_removal_service import remove_relationship
from .relationship_write_service import add_relationship

PREVIEW_SCOPE_CHANGE_TOOL_NAME = "preview_concept_publication_scope_change"
EXECUTE_SCOPE_CHANGE_TOOL_NAME = "change_concept_publication_scope"


@dataclass(frozen=True)
class OntologyScopeChangePreconditionError(Exception):
    """Typed no-effect result when an optimistic scope snapshot is stale."""

    expected_scope_fingerprint: str
    actual_scope_fingerprint: str
    canonical_read_back: Mapping[str, Any]

    @property
    def reason_code(self) -> str:
        return "scope_precondition_failed"

    @property
    def public_message(self) -> str:
        return "The concept scope changed after the preview was obtained."


@dataclass(frozen=True)
class OntologyScopeChangeRequestError(ValueError):
    """Typed no-effect result for an invalid exact scope edit contract."""

    reason_code: str
    public_message: str

    def __str__(self) -> str:
        return self.public_message


@dataclass(frozen=True)
class _PreparedScopeChange:
    concept_id: str
    before: Mapping[str, Any]
    source: PublicationContext
    destination: PublicationContext
    before_edges: tuple[tuple[str, str], ...]
    destination_edges: tuple[tuple[str, str], ...]
    resolved_scope_edit: Mapping[str, Any] | None
    intent: OntologyMutationIntent


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise_concept_id(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"


def _exact_concept_id(value: Any) -> str | None:
    cleaned = _clean_text(value)
    return cleaned if cleaned.startswith("#V#") else None


def _destination_context(
    *,
    destination_kind: str,
    destination_concept_id: str | None,
) -> PublicationContext:
    kind = _clean_text(destination_kind).lower().replace("-", "_")
    if kind in {"global", "base", "base_publication"}:
        return PublicationContext.global_context(source="scope_change_request")
    if kind in {"organisation", "organization", "org"}:
        organisation_id = _normalise_concept_id(destination_concept_id)
        if not organisation_id:
            raise ValueError("Organisation destination requires a concept ID")
        return PublicationContext.organisation(
            organisation_id,
            source="scope_change_request",
        )
    if kind in {"user", "private_user"}:
        user_id = _normalise_concept_id(destination_concept_id)
        if not user_id:
            raise ValueError("User destination requires a concept ID")
        return PublicationContext.user(user_id, source="scope_change_request")
    raise ValueError("Unsupported destination_kind")


def _scope_edges(read_back: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    edges: list[tuple[str, str]] = []
    raw_edges = read_back.get("scope_edges")
    if not isinstance(raw_edges, Mapping):
        return ()
    for predicate, targets in raw_edges.items():
        values = targets if isinstance(targets, list) else [targets]
        for target in values:
            if (
                isinstance(predicate, str)
                and isinstance(target, str)
                and predicate.strip()
                and target.strip()
            ):
                edges.append((predicate.strip(), target.strip()))
    return tuple(sorted(set(edges)))


def _scope_edges_mapping(
    edges: Sequence[tuple[str, str]],
) -> dict[str, list[str]]:
    payload: dict[str, list[str]] = {}
    for predicate, target in sorted(edges):
        payload.setdefault(predicate, []).append(target)
    return payload


def _destination_edge(
    destination: PublicationContext,
) -> tuple[str, str] | None:
    if destination.kind == PublicationContextKind.GLOBAL:
        return None
    if destination.kind == PublicationContextKind.ORGANISATION:
        return (
            CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
            str(destination.concept_id),
        )
    if destination.kind == PublicationContextKind.USER:
        return (
            CANONICAL_SPECIFIC_TO_USER_PREDICATE,
            str(destination.concept_id),
        )
    raise ValueError("Historical or mixed destination is not valid")


def _destination_edges(
    destination: PublicationContext,
) -> tuple[tuple[str, str], ...]:
    if destination.kind == PublicationContextKind.COMPOSITE:
        edges = tuple(
            edge
            for component in expand_publication_contexts((destination,))
            if (edge := _destination_edge(component)) is not None
        )
        if len(edges) != 2:
            raise ValueError("Malformed composite publication destination")
        return tuple(sorted(edges))
    edge = _destination_edge(destination)
    return (edge,) if edge else ()


def _context_for_edges(
    edges: Sequence[tuple[str, str]],
    *,
    source: str,
) -> PublicationContext:
    user_targets = sorted(
        target
        for predicate, target in edges
        if predicate == CANONICAL_SPECIFIC_TO_USER_PREDICATE
    )
    organisation_targets = sorted(
        target
        for predicate, target in edges
        if predicate == CANONICAL_SPECIFIC_TO_ORG_PREDICATE
    )
    if len(user_targets) > 1 or len(organisation_targets) > 1:
        raise OntologyScopeChangeRequestError(
            "ambiguous_publication_scope_transition",
            "The quick scope edit requires at most one exact user and organisation target.",
        )
    if any(
        predicate
        not in {
            CANONICAL_SPECIFIC_TO_USER_PREDICATE,
            CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
        }
        or not _exact_concept_id(target)
        for predicate, target in edges
    ):
        raise OntologyScopeChangeRequestError(
            "ambiguous_publication_scope_transition",
            "Historical or malformed scope cannot be changed by the quick controls.",
        )
    if user_targets and organisation_targets:
        return PublicationContext.composite(
            user_concept_id=user_targets[0],
            organisation_concept_id=organisation_targets[0],
            source=source,
        )
    if user_targets:
        return PublicationContext.user(user_targets[0], source=source)
    if organisation_targets:
        return PublicationContext.organisation(
            organisation_targets[0],
            source=source,
        )
    return PublicationContext.global_context(source=source)


def _canonical_scope_edges_for_edit(
    before: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    edges = _scope_edges(before)
    # Parsing through the exact context validates canonical predicates, target
    # identifiers, and the one-user/one-organisation ceiling.
    _context_for_edges(edges, source="scope_edit_source")
    return edges


def _resolve_scope_edit(
    *,
    before: Mapping[str, Any],
    scope_edit: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[tuple[str, str], ...]]:
    kind = _clean_text(scope_edit.get("kind")).lower().replace("-", "_")
    if kind in {"organization", "org"}:
        kind = "organisation"
    if kind not in {"user", "organisation"}:
        raise OntologyScopeChangeRequestError(
            "invalid_scope_edit_kind",
            "scope_edit.kind must be user or organisation.",
        )
    enabled = scope_edit.get("enabled")
    if not isinstance(enabled, bool):
        raise OntologyScopeChangeRequestError(
            "invalid_scope_edit_enabled",
            "scope_edit.enabled must be a boolean.",
        )
    before_edges = _canonical_scope_edges_for_edit(before)
    predicate = (
        CANONICAL_SPECIFIC_TO_USER_PREDICATE
        if kind == "user"
        else CANONICAL_SPECIFIC_TO_ORG_PREDICATE
    )
    active_targets = [
        target for edge_predicate, target in before_edges if edge_predicate == predicate
    ]
    supplied_target = _exact_concept_id(scope_edit.get("concept_id"))
    if scope_edit.get("concept_id") is not None and supplied_target is None:
        raise OntologyScopeChangeRequestError(
            "invalid_scope_edit_target",
            "scope_edit.concept_id must be an exact #V# concept identifier.",
        )

    if enabled:
        if active_targets:
            resolved_target = active_targets[0]
        elif kind == "user":
            resolved_target = _normalise_concept_id(get_effective_user_concept_id())
            if not resolved_target:
                raise OntologyScopeChangeRequestError(
                    "authenticated_actor_context_required",
                    "Trusted actor context is required to add user scope.",
                )
        else:
            resolved_target = _normalise_concept_id(
                get_effective_organisation_concept_id()
            )
            if not resolved_target:
                raise OntologyScopeChangeRequestError(
                    "organisation_context_required",
                    "Choose an organisation context before adding organisation scope.",
                )
        if supplied_target and supplied_target != resolved_target:
            raise OntologyScopeChangeRequestError(
                "scope_edit_target_mismatch",
                "The requested scope target does not match trusted actor context.",
            )
        destination_edges = tuple(sorted({*before_edges, (predicate, resolved_target)}))
    else:
        if len(active_targets) != 1:
            raise OntologyScopeChangeRequestError(
                "scope_edit_target_not_active",
                "The selected publication restriction is not active.",
            )
        resolved_target = active_targets[0]
        if supplied_target and supplied_target != resolved_target:
            raise OntologyScopeChangeRequestError(
                "scope_edit_target_mismatch",
                "The requested scope target changed after preview.",
            )
        destination_edges = tuple(
            edge for edge in before_edges if edge != (predicate, resolved_target)
        )

    return (
        {
            "kind": kind,
            "enabled": enabled,
            "concept_id": resolved_target,
        },
        destination_edges,
    )


def get_concept_publication_scope(concept_id: str) -> dict[str, Any]:
    """Read one exact actor-visible scope snapshot without widening access."""

    normalised_concept_id = _normalise_concept_id(concept_id)
    if not normalised_concept_id:
        raise ValueError("concept_id is required")
    if not can_access_concept(normalised_concept_id):
        raise PermissionError("ontology_scope_target_not_accessible")
    return scope_read_back(normalised_concept_id)


def _prepare_scope_change(
    *,
    concept_id: str,
    expected_scope_fingerprint: str,
    request_id: str,
    preview: bool,
    reason: str | None,
    invocation_tool_name: str,
    destination_kind: str | None = None,
    destination_concept_id: str | None = None,
    scope_edit: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> _PreparedScopeChange:
    normalised_concept_id = _normalise_concept_id(concept_id)
    if not normalised_concept_id:
        raise ValueError("concept_id is required")
    if not can_access_concept(normalised_concept_id):
        raise PermissionError("ontology_scope_target_not_accessible")
    expected_fingerprint = _clean_text(expected_scope_fingerprint)
    if not expected_fingerprint:
        raise ValueError("expected_scope_fingerprint is required")
    request_key = _clean_text(request_id)
    if not request_key:
        raise ValueError("request_id is required")

    before = scope_read_back(normalised_concept_id)
    actual_fingerprint = _clean_text(before.get("scope_fingerprint"))
    if actual_fingerprint != expected_fingerprint:
        raise OntologyScopeChangePreconditionError(
            expected_scope_fingerprint=expected_fingerprint,
            actual_scope_fingerprint=actual_fingerprint,
            canonical_read_back=before,
        )

    has_destination = bool(_clean_text(destination_kind))
    has_scope_edit = scope_edit is not None
    if has_destination == has_scope_edit:
        raise OntologyScopeChangeRequestError(
            "scope_change_contract_ambiguous",
            "Provide exactly one of destination_kind or scope_edit.",
        )

    before_edges = _scope_edges(before)
    resolved_scope_edit: Mapping[str, Any] | None = None
    if has_scope_edit:
        if not isinstance(scope_edit, Mapping):
            raise OntologyScopeChangeRequestError(
                "invalid_scope_edit",
                "scope_edit must be an object.",
            )
        resolved_scope_edit, destination_edges = _resolve_scope_edit(
            before=before,
            scope_edit=scope_edit,
        )
        destination = _context_for_edges(
            destination_edges,
            source="scope_edit_request",
        )
    else:
        destination = _destination_context(
            destination_kind=_clean_text(destination_kind),
            destination_concept_id=destination_concept_id,
        )
        destination_edges = _destination_edges(destination)

    raw_source_context = before.get("publication_context")
    source_map = raw_source_context if isinstance(raw_source_context, Mapping) else {}
    try:
        source = publication_context_from_mapping(source_map)
    except ValueError:
        source = PublicationContext(
            PublicationContextKind.HISTORICAL,
            None,
            "invalid_scope_read_back",
        )
    tool_name = _clean_text(invocation_tool_name)
    if tool_name not in {
        PREVIEW_SCOPE_CHANGE_TOOL_NAME,
        EXECUTE_SCOPE_CHANGE_TOOL_NAME,
    }:
        raise ValueError("Unsupported scope-change invocation tool")
    removed_edges = tuple(sorted(set(before_edges) - set(destination_edges)))
    added_edges = tuple(sorted(set(destination_edges) - set(before_edges)))
    context_target_ids = tuple(
        context.concept_id
        for context in expand_publication_contexts((source, destination))
        if context.concept_id
    )
    intent = OntologyMutationIntent(
        operation="scope.change",
        publication_context=destination,
        source_contexts=(source,),
        target_concept_ids=tuple(
            dict.fromkeys((normalised_concept_id, *context_target_ids))
        ),
        tool_name=tool_name,
        delta={
            "from": source.to_mapping(),
            "to": destination.to_mapping(),
            "contract_mode": "scope_edit" if has_scope_edit else "destination",
            "resolved_scope_edit": dict(resolved_scope_edit or {}),
            "source_scope_edges": _scope_edges_mapping(before_edges),
            "destination_scope_edges": _scope_edges_mapping(destination_edges),
            "scope_delta": {
                "remove": [
                    {"predicate": predicate, "target": target}
                    for predicate, target in removed_edges
                ],
                "add": [
                    {"predicate": predicate, "target": target}
                    for predicate, target in added_edges
                ],
            },
            "expected_scope_fingerprint": expected_fingerprint,
            "reason": _clean_text(reason) or None,
        },
        # A preview is an auditable decision but not the claimed write. Keep
        # its receipt distinct so a subsequent execution can use the same
        # caller request ID without replaying the preview.
        idempotency_key=(
            (
                _clean_text(current_ontology_invocation().effect_id)
                if current_ontology_invocation()
                and current_ontology_invocation().executing_agent_concept_id
                else ""
            )
            or _clean_text(idempotency_key)
            or (f"{request_key}:preview" if preview else request_key)
        ),
    )
    return _PreparedScopeChange(
        concept_id=normalised_concept_id,
        before=before,
        source=source,
        destination=destination,
        before_edges=before_edges,
        destination_edges=destination_edges,
        resolved_scope_edit=resolved_scope_edit,
        intent=intent,
    )


def build_concept_publication_scope_change_intent(
    *,
    concept_id: str,
    expected_scope_fingerprint: str,
    request_id: str,
    preview: bool,
    invocation_tool_name: str,
    destination_kind: str | None = None,
    destination_concept_id: str | None = None,
    scope_edit: Mapping[str, Any] | None = None,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> OntologyMutationIntent:
    """Build the exact intent shared by delegation issuance and execution."""

    return _prepare_scope_change(
        concept_id=concept_id,
        expected_scope_fingerprint=expected_scope_fingerprint,
        request_id=request_id,
        preview=preview,
        reason=reason,
        invocation_tool_name=invocation_tool_name,
        destination_kind=destination_kind,
        destination_concept_id=destination_concept_id,
        scope_edit=scope_edit,
        idempotency_key=idempotency_key,
    ).intent


def change_concept_publication_scope(
    *,
    concept_id: str,
    expected_scope_fingerprint: str,
    request_id: str,
    destination_kind: str | None = None,
    destination_concept_id: str | None = None,
    scope_edit: Mapping[str, Any] | None = None,
    preview: bool = True,
    reason: str | None = None,
    invocation_tool_name: str = EXECUTE_SCOPE_CHANGE_TOOL_NAME,
) -> dict[str, Any]:
    """Preview or perform one exact scope transition with exact authority."""

    try:
        prepared = _prepare_scope_change(
            concept_id=concept_id,
            expected_scope_fingerprint=expected_scope_fingerprint,
            request_id=request_id,
            preview=preview,
            reason=reason,
            invocation_tool_name=invocation_tool_name,
            destination_kind=destination_kind,
            destination_concept_id=destination_concept_id,
            scope_edit=scope_edit,
        )
    except OntologyScopeChangePreconditionError as exc:
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": exc.reason_code,
            "error": exc.public_message,
            "expected_scope_fingerprint": exc.expected_scope_fingerprint,
            "actual_scope_fingerprint": exc.actual_scope_fingerprint,
            "canonical_read_back": dict(exc.canonical_read_back),
        }
    normalised_concept_id = prepared.concept_id
    before = prepared.before
    source = prepared.source
    destination = prepared.destination
    before_edges = prepared.before_edges
    destination_edges = prepared.destination_edges
    resolved_scope_edit = prepared.resolved_scope_edit
    intent = prepared.intent
    request_key = _clean_text(request_id)
    expected_fingerprint = _clean_text(expected_scope_fingerprint)
    removed_plan = tuple(sorted(set(before_edges) - set(destination_edges)))
    added_plan = tuple(sorted(set(destination_edges) - set(before_edges)))

    def mutate() -> Mapping[str, Any]:
        if preview:
            return {
                "success": True,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "preview": True,
                "operational_state_effect": True,
                "semantic_effect": False,
                "from": source.to_mapping(),
                "to": destination.to_mapping(),
                "resolved_scope_edit": (
                    dict(resolved_scope_edit) if resolved_scope_edit else None
                ),
                "destination_scope_edges": _scope_edges_mapping(destination_edges),
                "scope_delta": {
                    "remove": [
                        {"predicate": predicate, "target": target}
                        for predicate, target in removed_plan
                    ],
                    "add": [
                        {"predicate": predicate, "target": target}
                        for predicate, target in added_plan
                    ],
                },
            }

        try:
            with ExitStack() as locks:
                resource_keys = {
                    f"ontology-publication-scope:{normalised_concept_id}",
                    *ontology_authority_resource_keys(intent),
                }
                for resource_key in sorted(resource_keys):
                    locks.enter_context(ontology_mutation_resource_lock(resource_key))
                current = scope_read_back(normalised_concept_id)
                current_fingerprint = _clean_text(current.get("scope_fingerprint"))
                if current_fingerprint != expected_fingerprint:
                    return {
                        "success": False,
                        "effect_status": "not_started",
                        "mutation_outcome": "not_started",
                        "changed": False,
                        "error_code": "scope_precondition_failed",
                        "error": (
                            "The concept scope changed after authority was resolved."
                        ),
                        "expected_scope_fingerprint": expected_fingerprint,
                        "actual_scope_fingerprint": current_fingerprint,
                    }
                live_decision = authorise_ontology_mutation(intent)
                if not live_decision.allowed:
                    return ontology_authority_denial_payload(live_decision, intent)

                current_edges = _scope_edges(current)
                edges_to_add = tuple(
                    sorted(set(destination_edges) - set(current_edges))
                )
                edges_to_remove = tuple(
                    sorted(set(current_edges) - set(destination_edges))
                )
                removed: list[tuple[str, str]] = []
                added: list[tuple[str, str]] = []
                try:
                    # Add every restrictive destination before removing a source
                    # restriction, so replacement never passes through an
                    # unintended globally visible interval.
                    for predicate, target in edges_to_add:
                        result = add_relationship(
                            normalised_concept_id,
                            predicate,
                            target,
                        )
                        if not result.get("success"):
                            raise RuntimeError(
                                str(result.get("error") or "scope_edge_add_failed")
                            )
                        if bool(
                            result.get("forward_modified") or result.get("modified")
                        ):
                            added.append((predicate, target))

                    for predicate, target in edges_to_remove:
                        result = remove_relationship(
                            source_id=normalised_concept_id,
                            predicate=predicate,
                            target=target,
                            mode="soft_delete",
                            cascade="warn",
                            dry_run=False,
                            confirmed=True,
                            reason=_clean_text(reason) or "ontology scope transition",
                            request_id=f"{request_key}:remove:{len(removed)}",
                        )
                        if not result.get("success"):
                            raise RuntimeError(
                                str(
                                    result.get("error_code")
                                    or "scope_edge_remove_failed"
                                )
                            )
                        if result.get("removed"):
                            removed.append((predicate, target))

                    return {
                        "success": True,
                        "effect_status": "succeeded",
                        "mutation_outcome": "succeeded",
                        "changed": bool(removed or added),
                        "from": source.to_mapping(),
                        "to": destination.to_mapping(),
                        "resolved_scope_edit": (
                            dict(resolved_scope_edit) if resolved_scope_edit else None
                        ),
                        "destination_scope_edges": _scope_edges_mapping(
                            destination_edges
                        ),
                        "removed_scope_edges": [
                            {"predicate": predicate, "target": target}
                            for predicate, target in removed
                        ],
                        "destination_scope_edges_added": [
                            {"predicate": predicate, "target": target}
                            for predicate, target in added
                        ],
                        # Retain the pre-composite response bit for callers that
                        # only need to know whether any destination edge was added.
                        "destination_edge_added": bool(added),
                    }
                except Exception:
                    # Best-effort compensation keeps the old publication boundary
                    # if a later edge fails. The outer command still reports an
                    # indeterminate outcome and preserves canonical read-back.
                    for index, (predicate, target) in enumerate(reversed(added)):
                        remove_relationship(
                            source_id=normalised_concept_id,
                            predicate=predicate,
                            target=target,
                            dry_run=False,
                            confirmed=True,
                            reason="scope transition compensation",
                            request_id=f"{request_key}:compensate-destination:{index}",
                        )
                    for predicate, target in removed:
                        add_relationship(normalised_concept_id, predicate, target)
                    raise
        except OntologyMutationResourceBusy:
            return {
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "retryable": True,
                "error_code": "ontology_mutation_resource_busy",
                "error": "Another scope transition is already in progress.",
            }

    return execute_authorised_ontology_mutation(
        intent=intent,
        mutate=mutate,
        read_before=lambda: before,
        read_back=lambda: scope_read_back(normalised_concept_id),
        verify_read_back=lambda _result, state: (
            isinstance(state, Mapping)
            and _scope_edges(state) == tuple(sorted(destination_edges))
        ),
        preview=preview,
    )


__all__ = [
    "EXECUTE_SCOPE_CHANGE_TOOL_NAME",
    "PREVIEW_SCOPE_CHANGE_TOOL_NAME",
    "OntologyScopeChangePreconditionError",
    "OntologyScopeChangeRequestError",
    "build_concept_publication_scope_change_intent",
    "change_concept_publication_scope",
    "get_concept_publication_scope",
]
