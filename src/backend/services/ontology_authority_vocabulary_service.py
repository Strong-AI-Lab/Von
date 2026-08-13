"""Vocabulary and one-time global bootstrap for ontology publication authority.

The vocabulary itself is a read-only code registry.  The only persistent
bootstrap operation is deliberately narrow: when no global semantic
administrator exists, a locally enabled trusted operator may record the first
global role. Represented administrators then establish organisation roles
through the normal lifecycle. An operator token is not otherwise semantic
publication authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import bypass_access_control
from .ontology_authority_membership_coordination_service import (
    ontology_authority_membership_mutation_barrier,
)
from .ontology_publication_authority_service import (
    AUTHORITY_ROLE_PREDICATE,
    GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
    OntologyMutationResourceBusy,
    authority_role_storage_text,
    ontology_mutation_resource_lock,
    parse_authority_role_storage_text,
    resolve_live_semantic_roles,
)
from .text_value_service import delete_text_relation, upsert_text_for_concept

ONTOLOGY_AUTHORITY_VOCABULARY = {
    "predicate": AUTHORITY_ROLE_PREDICATE,
    "roles": {
        "organisation_ontology_administrator": ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        "global_ontology_administrator": GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    },
    "von_administrator": {
        "concept_id": "#V#von_administrator",
        "authority": "operational_only",
        "implies_semantic_ontology_authority": False,
    },
}


def _clean_identifier(value: object) -> str | None:
    candidate = str(value or "").strip()
    return candidate or None


def _normalise_concept_id(value: object) -> str | None:
    candidate = _clean_identifier(value)
    if not candidate:
        return None
    return candidate if candidate.startswith("#") else f"#V#{candidate}"


def _recognised_role_texts() -> set[str]:
    return {
        ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    }


def semantic_authority_has_been_bootstrapped() -> bool:
    """Return whether any recognised represented semantic role already exists."""

    relations = TextRelationsRepository.find({"predicate": AUTHORITY_ROLE_PREDICATE})
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        text_id = relation.get("object_text_id")
        text_value = TextValuesRepository.find_one_by_id(text_id)
        if not isinstance(text_value, Mapping):
            continue
        role, _organisation_id = parse_authority_role_storage_text(
            text_value.get("text"),
            relation.get("context")
            if isinstance(relation.get("context"), Mapping)
            else {},
        )
        if role in _recognised_role_texts():
            return True
    return False


def semantic_authority_scope_has_been_bootstrapped(
    *,
    role: str,
    organisation_concept_id: str | None,
) -> bool:
    """Return whether this independent semantic authority root has a grant."""

    role_value = str(role or "").strip().lower()
    organisation_id = _clean_identifier(organisation_concept_id)
    relations = TextRelationsRepository.find({"predicate": AUTHORITY_ROLE_PREDICATE})
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        text_value = TextValuesRepository.find_one_by_id(relation.get("object_text_id"))
        if not isinstance(text_value, Mapping):
            continue
        stored_role, stored_org = parse_authority_role_storage_text(
            text_value.get("text"),
            relation.get("context")
            if isinstance(relation.get("context"), Mapping)
            else {},
        )
        if stored_role != role_value:
            continue
        if role_value == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE or (
            stored_org == organisation_id
        ):
            return True
    return False


def bootstrap_first_semantic_authority(
    *,
    subject_concept_id: str,
    role: str,
    organisation_concept_id: str | None = None,
) -> dict[str, Any]:
    """Persist exactly the first global semantic administrator.

    This is intentionally not a general role-assignment API. Once a global
    root exists, every further global or organisation role change uses the
    represented administrator lifecycle.
    """

    subject_id = _normalise_concept_id(subject_concept_id)
    role_value = str(role or "").strip().lower()
    organisation_id = _clean_identifier(organisation_concept_id)
    if not subject_id:
        raise ValueError("subject_concept_id is required")
    if role_value not in _recognised_role_texts():
        raise ValueError("unsupported_ontology_authority_role")
    if role_value != GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE:
        raise ValueError("bootstrap_requires_global_ontology_administrator")
    if role_value == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE and organisation_id:
        raise ValueError("global authority may not have an organisation context")
    try:
        with ontology_authority_membership_mutation_barrier():  # noqa: SIM117
            with ontology_mutation_resource_lock(
                f"ontology-authority-role:{role_value}:{organisation_id or 'global'}"
            ):
                if semantic_authority_has_been_bootstrapped():
                    raise PermissionError(
                        "ontology_authority_bootstrap_already_completed"
                    )
                with bypass_access_control():
                    subject = ConceptsRepository.find_one({"concept_id": subject_id})
                if not isinstance(subject, Mapping):
                    raise ValueError(  # noqa: TRY004
                        "ontology_authority_bootstrap_subject_not_found"
                    )

                context = (
                    {"organisation_concept_id": organisation_id}
                    if organisation_id
                    else {"authority_scope": "global"}
                )
                result = upsert_text_for_concept(
                    subject_concept_id=subject_id,
                    predicate=AUTHORITY_ROLE_PREDICATE,
                    text=authority_role_storage_text(role_value, organisation_id),
                    lang="en-NZ",
                    provenance={
                        "source": "ontology_authority_role_vocabulary",
                    },
                    context={
                        **context,
                        "grant_provenance": {
                            "source": "ontology_authority_bootstrap",
                            "operator_authority": "bootstrap_only",
                        },
                    },
                )
                relation_id = str(result.get("relation_id") or "")
                try:
                    matching_roles = [
                        item
                        for item in resolve_live_semantic_roles(subject_id)
                        if item.role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
                        and item.relation_id == relation_id
                    ]
                except Exception as exc:
                    if relation_id:
                        try:
                            delete_text_relation(
                                subject_concept_id=subject_id,
                                relation_id=relation_id,
                                garbage_collect=False,
                            )
                        except Exception as compensation_exc:
                            raise RuntimeError(
                                "ontology_authority_bootstrap_reconciliation_required"
                            ) from compensation_exc
                    raise RuntimeError(
                        "ontology_authority_bootstrap_read_back_failed"
                    ) from exc
                if not relation_id or not matching_roles:
                    if relation_id:
                        delete_text_relation(
                            subject_concept_id=subject_id,
                            relation_id=relation_id,
                            garbage_collect=False,
                        )
                    raise RuntimeError("ontology_authority_bootstrap_read_back_failed")
    except OntologyMutationResourceBusy as exc:
        raise PermissionError("ontology_mutation_resource_busy") from exc
    return {
        "vocabulary": ONTOLOGY_AUTHORITY_VOCABULARY,
        "first_grant": {
            "subject_concept_id": subject_id,
            "role": role_value,
            "organisation_concept_id": organisation_id,
            "relation_id": result.get("relation_id"),
            "revision": matching_roles[0].revision,
        },
    }


__all__ = [
    "ONTOLOGY_AUTHORITY_VOCABULARY",
    "bootstrap_first_semantic_authority",
    "semantic_authority_has_been_bootstrapped",
    "semantic_authority_scope_has_been_bootstrapped",
]
