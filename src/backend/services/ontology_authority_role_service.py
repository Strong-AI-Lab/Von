"""Dedicated lifecycle for represented semantic ontology administrator roles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import (
    can_access_concept,
    get_effective_user_concept_id,
)
from .ontology_authority_membership_coordination_service import (
    ontology_authority_membership_mutation_barrier,
)
from .ontology_publication_authority_service import (
    AUTHORITY_ROLE_PREDICATE,
    GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
    OntologyMutationIntent,
    OntologyMutationResourceBusy,
    PublicationContext,
    authorise_ontology_mutation,
    authority_role_storage_text,
    current_ontology_invocation,
    execute_authorised_ontology_mutation,
    ontology_mutation_resource_lock,
    parse_authority_role_storage_text,
    resolve_live_semantic_roles,
)
from .organisation_membership_service import is_user_member_of_organisation
from .text_value_service import delete_text_relation, upsert_text_for_concept


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalise_concept_id(value: object) -> str | None:
    cleaned = _clean(value)
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"


def _normalise_role(
    role: object,
    organisation_concept_id: object = None,
) -> tuple[str, str | None]:
    role_value = _clean(role).lower()
    organisation_id = _normalise_concept_id(organisation_concept_id)
    if role_value == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE:
        if organisation_id:
            raise ValueError("global authority may not have an organisation context")
        return role_value, None
    if role_value == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE:
        if not organisation_id:
            raise ValueError(
                "organisation_concept_id is required for organisation authority"
            )
        return role_value, organisation_id
    raise ValueError("unsupported_ontology_authority_role")


def _assert_direct_human_administrator() -> str:
    actor_id = _normalise_concept_id(get_effective_user_concept_id())
    if not actor_id:
        raise PermissionError("authenticated_actor_context_required")
    invocation = current_ontology_invocation()
    if invocation and invocation.executing_agent_concept_id:
        raise PermissionError("ontology_authority_role_human_administrator_required")
    return actor_id


def _role_context(role: str, organisation_id: str | None) -> dict[str, str]:
    if role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE:
        return {"authority_scope": "global"}
    return {"organisation_concept_id": str(organisation_id)}


def _grant_provenance(
    *,
    actor_concept_id: str,
    request_id: str,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "source": "ontology_authority_role_grant",
        "granted_by_actor_concept_id": actor_concept_id,
        "request_id": request_id,
        "reason": _clean(reason) or None,
    }


def _role_resource_key(role: str, organisation_id: str | None) -> str:
    return f"ontology-authority-role:{role}:{organisation_id or 'global'}"


def _role_resource_busy() -> dict[str, Any]:
    return {
        "success": False,
        "effect_status": "not_started",
        "mutation_outcome": "not_started",
        "changed": False,
        "retryable": True,
        "error_code": "ontology_mutation_resource_busy",
        "error": "Another role lifecycle effect is already in progress.",
    }


def _live_role_reauthorisation_failure(
    intent: OntologyMutationIntent,
) -> dict[str, Any] | None:
    """Re-check the exact lifecycle authority while holding its root lock."""

    decision = authorise_ontology_mutation(intent)
    if decision.allowed:
        return None
    return {
        "success": False,
        "effect_status": "not_started",
        "mutation_outcome": "not_started",
        "changed": False,
        "error_code": decision.reason_code,
        "error": decision.message,
        "authority_decision": decision.public_projection(),
    }


def _role_publication_context(
    role: str,
    organisation_id: str | None,
) -> PublicationContext:
    if role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE:
        return PublicationContext.global_context(source="authority_role_lifecycle")
    return PublicationContext.organisation(
        str(organisation_id),
        source="authority_role_lifecycle",
    )


def _role_rows(subject_concept_id: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"predicate": AUTHORITY_ROLE_PREDICATE}
    if subject_concept_id:
        query["subject_concept_id"] = subject_concept_id
    rows: list[dict[str, Any]] = []
    for relation in TextRelationsRepository.find(query):
        if not isinstance(relation, Mapping):
            continue
        text_value = TextValuesRepository.find_one_by_id(relation.get("object_text_id"))
        if not isinstance(text_value, Mapping):
            continue
        relation_context = (
            relation.get("context")
            if isinstance(relation.get("context"), Mapping)
            else {}
        )
        role, organisation_id = parse_authority_role_storage_text(
            text_value.get("text"),
            relation_context,
        )
        if not role:
            continue
        rows.append(
            {
                "relation_id": str(relation.get("_id") or ""),
                "subject_concept_id": _normalise_concept_id(
                    relation.get("subject_concept_id")
                ),
                "role": role,
                "organisation_concept_id": organisation_id,
                "context": dict(relation_context),
                "grant_provenance": (
                    dict(relation_context.get("grant_provenance"))
                    if isinstance(relation_context.get("grant_provenance"), Mapping)
                    else None
                ),
                "updated_at": str(relation.get("updated_at") or "") or None,
            }
        )
    rows.sort(
        key=lambda item: (
            str(item.get("role") or ""),
            str(item.get("organisation_concept_id") or ""),
            str(item.get("subject_concept_id") or ""),
            str(item.get("relation_id") or ""),
        )
    )
    return rows


def _matching_role_rows(
    *,
    subject_concept_id: str,
    role: str,
    organisation_concept_id: str | None,
) -> list[dict[str, Any]]:
    return [
        row
        for row in _role_rows(subject_concept_id)
        if row["role"] == role
        and row.get("organisation_concept_id") == organisation_concept_id
    ]


def authority_role_read_back(
    *,
    subject_concept_id: str,
    role: str,
    organisation_concept_id: str | None,
) -> dict[str, Any]:
    rows = _matching_role_rows(
        subject_concept_id=subject_concept_id,
        role=role,
        organisation_concept_id=organisation_concept_id,
    )
    return {
        "subject_concept_id": subject_concept_id,
        "role": role,
        "organisation_concept_id": organisation_concept_id,
        "active": bool(rows),
        "grants": rows,
    }


def _role_intent(
    *,
    operation: str,
    subject_concept_id: str,
    role: str,
    organisation_concept_id: str | None,
    request_id: str,
    reason: str | None,
) -> OntologyMutationIntent:
    targets = tuple(
        value for value in (subject_concept_id, organisation_concept_id) if value
    )
    return OntologyMutationIntent(
        operation=operation,
        publication_context=_role_publication_context(role, organisation_concept_id),
        target_concept_ids=targets,
        tool_name=(
            "grant_ontology_authority_role"
            if operation == "authority.role.grant"
            else "revoke_ontology_authority_role"
        ),
        predicate=AUTHORITY_ROLE_PREDICATE,
        delta={
            "subject_concept_id": subject_concept_id,
            "role": role,
            "organisation_concept_id": organisation_concept_id,
            "reason": _clean(reason) or None,
        },
        idempotency_key=request_id,
    )


def _validate_role_change(
    *,
    subject_concept_id: object,
    role: object,
    organisation_concept_id: object,
    request_id: object,
) -> tuple[str, str, str | None, str]:
    _assert_direct_human_administrator()
    subject_id = _normalise_concept_id(subject_concept_id)
    if not subject_id:
        raise ValueError("subject_concept_id is required")
    role_value, organisation_id = _normalise_role(role, organisation_concept_id)
    request_key = _clean(request_id)
    if not request_key:
        raise ValueError("request_id is required")
    if not can_access_concept(subject_id):
        raise PermissionError("ontology_authority_role_target_not_accessible")
    if organisation_id and not can_access_concept(organisation_id):
        raise PermissionError("ontology_authority_role_target_not_accessible")
    return subject_id, role_value, organisation_id, request_key


def grant_ontology_authority_role(
    *,
    subject_concept_id: str,
    role: str,
    organisation_concept_id: str | None = None,
    request_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Grant one represented role without allowing semantic amplification."""

    actor_id = _assert_direct_human_administrator()
    subject_id, role_value, organisation_id, request_key = _validate_role_change(
        subject_concept_id=subject_concept_id,
        role=role,
        organisation_concept_id=organisation_concept_id,
        request_id=request_id,
    )
    intent = _role_intent(
        operation="authority.role.grant",
        subject_concept_id=subject_id,
        role=role_value,
        organisation_concept_id=organisation_id,
        request_id=request_key,
        reason=reason,
    )
    expected_grant_provenance = _grant_provenance(
        actor_concept_id=actor_id,
        request_id=request_key,
        reason=reason,
    )

    def mutate() -> Mapping[str, Any]:
        try:
            with ontology_authority_membership_mutation_barrier():  # noqa: SIM117
                with ontology_mutation_resource_lock(
                    _role_resource_key(role_value, organisation_id)
                ):
                    live_denial = _live_role_reauthorisation_failure(intent)
                    if live_denial is not None:
                        return live_denial
                    if organisation_id and not is_user_member_of_organisation(
                        subject_id,
                        organisation_id,
                    ):
                        return {
                            "success": False,
                            "effect_status": "not_started",
                            "mutation_outcome": "not_started",
                            "changed": False,
                            "error_code": (
                                "organisation_membership_required_for_"
                                "ontology_authority"
                            ),
                            "error": (
                                "Exact organisation membership is required before "
                                "granting ontology publication authority."
                            ),
                        }
                    result = upsert_text_for_concept(
                        subject_concept_id=subject_id,
                        predicate=AUTHORITY_ROLE_PREDICATE,
                        text=authority_role_storage_text(role_value, organisation_id),
                        lang="en-NZ",
                        # Text values are globally deduplicated by text/language,
                        # so grant-specific evidence belongs on this subject's
                        # relation context rather than the shared value.
                        provenance={"source": "ontology_authority_role_vocabulary"},
                        context={
                            **_role_context(role_value, organisation_id),
                            "grant_provenance": expected_grant_provenance,
                        },
                    )
        except OntologyMutationResourceBusy:
            return _role_resource_busy()
        changed = bool(result.get("relation_created") or result.get("context_updated"))
        return {
            "success": True,
            "effect_status": "succeeded",
            "mutation_outcome": "succeeded",
            "changed": changed,
            "relation_id": result.get("relation_id"),
            "subject_concept_id": subject_id,
            "role": role_value,
            "organisation_concept_id": organisation_id,
            "grant_provenance": expected_grant_provenance,
        }

    return execute_authorised_ontology_mutation(
        intent=intent,
        mutate=mutate,
        read_before=lambda: authority_role_read_back(
            subject_concept_id=subject_id,
            role=role_value,
            organisation_concept_id=organisation_id,
        ),
        read_back=lambda: authority_role_read_back(
            subject_concept_id=subject_id,
            role=role_value,
            organisation_concept_id=organisation_id,
        ),
        verify_read_back=lambda result, state: (
            isinstance(state, Mapping)
            and state.get("active") is True
            and any(
                isinstance(row, Mapping)
                and _clean(row.get("relation_id")) == _clean(result.get("relation_id"))
                and row.get("grant_provenance") == expected_grant_provenance
                for row in (state.get("grants") or [])
            )
        ),
    )


def revoke_ontology_authority_role(
    *,
    subject_concept_id: str,
    role: str,
    organisation_concept_id: str | None = None,
    request_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Revoke one exact role and preserve at least one global recovery root."""

    subject_id, role_value, organisation_id, request_key = _validate_role_change(
        subject_concept_id=subject_concept_id,
        role=role,
        organisation_concept_id=organisation_concept_id,
        request_id=request_id,
    )
    intent = _role_intent(
        operation="authority.role.revoke",
        subject_concept_id=subject_id,
        role=role_value,
        organisation_concept_id=organisation_id,
        request_id=request_key,
        reason=reason,
    )

    def mutate() -> Mapping[str, Any]:
        try:
            with ontology_authority_membership_mutation_barrier():  # noqa: SIM117
                with ontology_mutation_resource_lock(
                    _role_resource_key(role_value, organisation_id)
                ):
                    live_denial = _live_role_reauthorisation_failure(intent)
                    if live_denial is not None:
                        return live_denial
                    matches = _matching_role_rows(
                        subject_concept_id=subject_id,
                        role=role_value,
                        organisation_concept_id=organisation_id,
                    )
                    if role_value == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE and matches:
                        all_global = [
                            row
                            for row in _role_rows()
                            if row["role"] == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
                        ]
                        if len(all_global) <= len(matches):
                            return {
                                "success": False,
                                "effect_status": "not_started",
                                "mutation_outcome": "not_started",
                                "changed": False,
                                "error_code": (
                                    "last_global_ontology_administrator_cannot_be_revoked"
                                ),
                                "error": (
                                    "At least one represented global semantic administrator "
                                    "is required."
                                ),
                            }
                    deleted_ids: list[str] = []
                    for row in matches:
                        relation_id = _clean(row.get("relation_id"))
                        if not relation_id:
                            continue
                        result = delete_text_relation(
                            subject_concept_id=subject_id,
                            relation_id=relation_id,
                            garbage_collect=False,
                        )
                        if result.get("deleted"):
                            deleted_ids.append(relation_id)
        except OntologyMutationResourceBusy:
            return _role_resource_busy()
        return {
            "success": True,
            "effect_status": "succeeded",
            "mutation_outcome": "succeeded",
            "changed": bool(deleted_ids),
            "deleted_relation_ids": deleted_ids,
            "subject_concept_id": subject_id,
            "role": role_value,
            "organisation_concept_id": organisation_id,
            "reason": _clean(reason) or None,
        }

    return execute_authorised_ontology_mutation(
        intent=intent,
        mutate=mutate,
        read_before=lambda: authority_role_read_back(
            subject_concept_id=subject_id,
            role=role_value,
            organisation_concept_id=organisation_id,
        ),
        read_back=lambda: authority_role_read_back(
            subject_concept_id=subject_id,
            role=role_value,
            organisation_concept_id=organisation_id,
        ),
        verify_read_back=lambda _result, state: (
            isinstance(state, Mapping) and state.get("active") is False
        ),
    )


def list_manageable_ontology_authority_roles() -> list[dict[str, Any]]:
    """List only role grants in contexts the current human actor administers."""

    actor_id = _assert_direct_human_administrator()
    evidence = resolve_live_semantic_roles(actor_id)
    manages_global = any(
        item.role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE for item in evidence
    )
    managed_organisations = {
        item.organisation_concept_id
        for item in evidence
        if item.role == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE
        and item.organisation_concept_id
    }
    rows: list[dict[str, Any]] = []
    for row in _role_rows():
        role = row.get("role")
        organisation_id = row.get("organisation_concept_id")
        manageable = (
            role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE and manages_global
        ) or (
            role == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE
            and organisation_id in managed_organisations
        )
        subject_id = _normalise_concept_id(row.get("subject_concept_id"))
        if manageable and subject_id and can_access_concept(subject_id):
            rows.append(row)
    return rows


__all__ = [
    "authority_role_read_back",
    "grant_ontology_authority_role",
    "list_manageable_ontology_authority_roles",
    "revoke_ontology_authority_role",
]
