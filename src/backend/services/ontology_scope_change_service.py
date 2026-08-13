"""Dedicated, optimistic, provenance-bearing ontology scope transitions."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from ..security.access_control import can_access_concept
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
    execute_authorised_ontology_mutation,
    current_ontology_invocation,
    ontology_authority_denial_payload,
    ontology_authority_resource_keys,
    ontology_mutation_resource_lock,
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
class _PreparedScopeChange:
    concept_id: str
    before: Mapping[str, Any]
    source: PublicationContext
    destination: PublicationContext
    intent: OntologyMutationIntent


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise_concept_id(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"


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


def _scope_edges(read_back: Mapping[str, Any]) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    raw_edges = read_back.get("scope_edges")
    if not isinstance(raw_edges, Mapping):
        return edges
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
    return edges


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
    destination_kind: str,
    destination_concept_id: str | None,
    expected_scope_fingerprint: str,
    request_id: str,
    preview: bool,
    reason: str | None,
    invocation_tool_name: str,
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

    destination = _destination_context(
        destination_kind=destination_kind,
        destination_concept_id=destination_concept_id,
    )
    raw_source_context = before.get("publication_context")
    source_map = raw_source_context if isinstance(raw_source_context, Mapping) else {}
    source_kind = PublicationContextKind(
        str(source_map.get("kind") or PublicationContextKind.HISTORICAL.value)
    )
    source = PublicationContext(
        source_kind,
        _normalise_concept_id(source_map.get("concept_id")),
        str(source_map.get("source") or "scope_read_back"),
        tuple(source_map.get("historical_predicates") or ()),
    )
    tool_name = _clean_text(invocation_tool_name)
    if tool_name not in {
        PREVIEW_SCOPE_CHANGE_TOOL_NAME,
        EXECUTE_SCOPE_CHANGE_TOOL_NAME,
    }:
        raise ValueError("Unsupported scope-change invocation tool")
    intent = OntologyMutationIntent(
        operation="scope.change",
        publication_context=destination,
        source_contexts=(source,),
        target_concept_ids=tuple(
            item
            for item in (
                normalised_concept_id,
                destination.concept_id,
            )
            if item
        ),
        tool_name=tool_name,
        delta={
            "from": source.to_mapping(),
            "to": destination.to_mapping(),
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
        intent=intent,
    )


def build_concept_publication_scope_change_intent(
    *,
    concept_id: str,
    destination_kind: str,
    destination_concept_id: str | None = None,
    expected_scope_fingerprint: str,
    request_id: str,
    preview: bool,
    reason: str | None = None,
    invocation_tool_name: str,
    idempotency_key: str | None = None,
) -> OntologyMutationIntent:
    """Build the exact intent shared by delegation issuance and execution."""

    return _prepare_scope_change(
        concept_id=concept_id,
        destination_kind=destination_kind,
        destination_concept_id=destination_concept_id,
        expected_scope_fingerprint=expected_scope_fingerprint,
        request_id=request_id,
        preview=preview,
        reason=reason,
        invocation_tool_name=invocation_tool_name,
        idempotency_key=idempotency_key,
    ).intent


def change_concept_publication_scope(
    *,
    concept_id: str,
    destination_kind: str,
    destination_concept_id: str | None = None,
    expected_scope_fingerprint: str,
    request_id: str,
    preview: bool = True,
    reason: str | None = None,
    invocation_tool_name: str = EXECUTE_SCOPE_CHANGE_TOOL_NAME,
) -> dict[str, Any]:
    """Preview or perform one exact scope transition with exact authority."""

    try:
        prepared = _prepare_scope_change(
            concept_id=concept_id,
            destination_kind=destination_kind,
            destination_concept_id=destination_concept_id,
            expected_scope_fingerprint=expected_scope_fingerprint,
            request_id=request_id,
            preview=preview,
            reason=reason,
            invocation_tool_name=invocation_tool_name,
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
    intent = prepared.intent
    request_key = _clean_text(request_id)
    expected_fingerprint = _clean_text(expected_scope_fingerprint)

    def mutate() -> Mapping[str, Any]:
        new_edge = _destination_edge(destination)
        if preview:
            return {
                "success": True,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "preview": True,
                "from": source.to_mapping(),
                "to": destination.to_mapping(),
                "scope_delta": {
                    "remove": [
                        {"predicate": predicate, "target": target}
                        for predicate, target in _scope_edges(before)
                    ],
                    "add": (
                        [{"predicate": new_edge[0], "target": new_edge[1]}]
                        if new_edge
                        else []
                    ),
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

                removed: list[tuple[str, str]] = []
                destination_added = False
                try:
                    # Add a restrictive destination before removing any source
                    # restriction. A cross-org transition may be temporarily
                    # visible to both contexts, but it must never pass through
                    # an unintended globally visible interval.
                    if new_edge and new_edge not in _scope_edges(current):
                        result = add_relationship(
                            normalised_concept_id,
                            new_edge[0],
                            new_edge[1],
                        )
                        if not result.get("success"):
                            raise RuntimeError(
                                str(result.get("error") or "scope_edge_add_failed")
                            )
                        destination_added = bool(
                            result.get("forward_modified") or result.get("modified")
                        )

                    for predicate, target in _scope_edges(current):
                        if new_edge == (predicate, target):
                            continue
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
                        "changed": bool(removed or destination_added),
                        "from": source.to_mapping(),
                        "to": destination.to_mapping(),
                        "removed_scope_edges": [
                            {"predicate": predicate, "target": target}
                            for predicate, target in removed
                        ],
                        "destination_edge_added": destination_added,
                    }
                except Exception:
                    # Best-effort compensation keeps the old publication boundary
                    # if a later edge fails. The outer command still reports an
                    # indeterminate outcome and preserves canonical read-back.
                    compensation_edge = _destination_edge(destination)
                    if destination_added and compensation_edge:
                        remove_relationship(
                            source_id=normalised_concept_id,
                            predicate=compensation_edge[0],
                            target=compensation_edge[1],
                            dry_run=False,
                            confirmed=True,
                            reason="scope transition compensation",
                            request_id=f"{request_key}:compensate-destination",
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
            and (
                (
                    destination.kind == PublicationContextKind.GLOBAL
                    and not _scope_edges(state)
                )
                or (
                    destination.kind != PublicationContextKind.GLOBAL
                    and _scope_edges(state) == [_destination_edge(destination)]
                )
            )
        ),
        preview=preview,
    )


__all__ = [
    "EXECUTE_SCOPE_CHANGE_TOOL_NAME",
    "PREVIEW_SCOPE_CHANGE_TOOL_NAME",
    "OntologyScopeChangePreconditionError",
    "build_concept_publication_scope_change_intent",
    "change_concept_publication_scope",
    "get_concept_publication_scope",
]
