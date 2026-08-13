"""Dedicated represented lifecycle for Von operational administrators.

This is deliberately not part of ontology publication authority.  A live
assignment permits access to bounded operational control-plane surfaces only;
it neither changes concept visibility nor grants semantic ontology authority.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import can_access_concept, get_effective_user_concept_id
from .ontology_authority_membership_coordination_service import (
    ontology_authority_membership_mutation_barrier,
)
from .ontology_publication_authority_service import (
    OntologyMutationResourceBusy,
    current_ontology_invocation,
    ontology_mutation_resource_lock,
)
from .text_value_service import delete_text_relation, upsert_text_for_concept

VON_OPERATIONAL_ADMINISTRATOR_PREDICATE = "#V#has_von_operational_administrator"
VON_OPERATIONAL_ADMINISTRATOR_ROLE = "von_operational_administrator"
_ROLE_STORAGE_TEXT = "von_operational_administrator::global"
_BOOTSTRAP_CONTEXT: ContextVar[bool] = ContextVar(
    "von_operational_administrator_bootstrap", default=False
)
_LIFECYCLE_RESOURCE_KEY = "von-operational-administrator-lifecycle:v1"


def _normalise_concept_id(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if text.startswith("#") else f"#V#{text}"


def _current_direct_human_actor() -> str:
    actor_id = _normalise_concept_id(get_effective_user_concept_id())
    if not actor_id:
        raise PermissionError("authenticated_actor_context_required")
    invocation = current_ontology_invocation()
    if invocation and invocation.executing_agent_concept_id:
        raise PermissionError("von_operational_administrator_human_required")
    return actor_id


@contextmanager
def bind_von_operational_administrator_bootstrap() -> Iterator[None]:
    """Bind a route-verified existing operator credential for first grant only."""

    token = _BOOTSTRAP_CONTEXT.set(True)
    try:
        yield
    finally:
        _BOOTSTRAP_CONTEXT.reset(token)


def _role_rows(subject_concept_id: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"predicate": VON_OPERATIONAL_ADMINISTRATOR_PREDICATE}
    if subject_concept_id:
        query["subject_concept_id"] = subject_concept_id
    rows: list[dict[str, Any]] = []
    for relation in TextRelationsRepository.find(query):
        if not isinstance(relation, Mapping):
            continue
        object_text_id = relation.get("object_text_id")
        text = None
        candidates: list[object] = []
        if object_text_id is not None:
            try:
                candidates.append(ObjectId(str(object_text_id)))
            except (InvalidId, TypeError):
                pass
            if object_text_id not in candidates:
                candidates.append(object_text_id)
        for candidate in candidates:
            text = TextValuesRepository.find_one({"_id": candidate})
            if isinstance(text, Mapping):
                break
        if not isinstance(text, Mapping) or text.get("text") != _ROLE_STORAGE_TEXT:
            continue
        subject_id = _normalise_concept_id(relation.get("subject_concept_id"))
        relation_id = str(relation.get("_id") or "")
        if subject_id and relation_id:
            rows.append(
                {
                    "subject_concept_id": subject_id,
                    "relation_id": relation_id,
                    "updated_at": str(relation.get("updated_at") or "") or None,
                    "grant_provenance": (
                        dict(relation.get("context", {}).get("grant_provenance"))
                        if isinstance(relation.get("context"), Mapping)
                        and isinstance(
                            relation.get("context", {}).get("grant_provenance"),
                            Mapping,
                        )
                        else None
                    ),
                }
            )
    return sorted(
        rows,
        key=lambda item: (item["subject_concept_id"], item["relation_id"]),
    )


def von_operational_administrator_read_back(
    *, subject_concept_id: str
) -> dict[str, Any]:
    """Return only exact operational-role evidence for one named subject."""

    subject_id = _normalise_concept_id(subject_concept_id)
    if not subject_id:
        raise ValueError("subject_concept_id is required")
    grants = _role_rows(subject_id)
    return {
        "subject_concept_id": subject_id,
        "role": VON_OPERATIONAL_ADMINISTRATOR_ROLE,
        "active": bool(grants),
        "grants": grants,
    }


def list_von_operational_administrators() -> list[dict[str, Any]]:
    """Inventory all exact represented operational-role assignments.

    This is evidence only: reading the inventory grants no operational or
    semantic authority.  Migration preflight uses it to enforce the authorised
    sole-holder premise before any elevation begins.
    """

    return _role_rows()


def is_live_von_operational_administrator(actor_concept_id: object) -> bool:
    """Resolve the dedicated role without widening the caller's data scope."""

    actor_id = _normalise_concept_id(actor_concept_id)
    return bool(actor_id and _role_rows(actor_id))


def _can_manage_operational_administrators(actor_id: str) -> bool:
    return _BOOTSTRAP_CONTEXT.get() or is_live_von_operational_administrator(actor_id)


def _grant(
    *, subject_concept_id: str, reason: str | None, bootstrap_only: bool
) -> dict[str, Any]:
    actor_id = _current_direct_human_actor()
    subject_id = _normalise_concept_id(subject_concept_id)
    if not subject_id:
        raise ValueError("subject_concept_id is required")
    if not can_access_concept(subject_id):
        raise PermissionError("von_operational_administrator_target_not_accessible")
    if bootstrap_only:
        if not _BOOTSTRAP_CONTEXT.get():
            raise PermissionError("von_operational_administrator_bootstrap_required")
        if _role_rows():
            raise PermissionError("von_operational_administrator_already_initialised")
    elif not _can_manage_operational_administrators(actor_id):
        raise PermissionError("von_operational_administrator_required")
    try:
        with ontology_authority_membership_mutation_barrier():  # noqa: SIM117
            with ontology_mutation_resource_lock(_LIFECYCLE_RESOURCE_KEY):
                if bootstrap_only and _role_rows():
                    raise PermissionError(
                        "von_operational_administrator_already_initialised"
                    )
                result = upsert_text_for_concept(
                    subject_concept_id=subject_id,
                    predicate=VON_OPERATIONAL_ADMINISTRATOR_PREDICATE,
                    text=_ROLE_STORAGE_TEXT,
                    lang="en-NZ",
                    provenance={"source": "von_operational_administrator_lifecycle"},
                    context={
                        "operational_scope": "von",
                        "grant_provenance": {
                            "source": "von_operational_administrator_lifecycle",
                            "granted_by_actor_concept_id": actor_id,
                            "reason": str(reason or "").strip() or None,
                        },
                    },
                )
    except OntologyMutationResourceBusy as exc:
        raise RuntimeError("von_operational_administrator_resource_busy") from exc
    read_back = von_operational_administrator_read_back(subject_concept_id=subject_id)
    relation_id = str(result.get("relation_id") or "")
    if not relation_id or not any(
        row.get("relation_id") == relation_id for row in read_back["grants"]
    ):
        raise RuntimeError("von_operational_administrator_read_back_failed")
    return {
        "success": True,
        "changed": bool(
            result.get("relation_created") or result.get("context_updated")
        ),
        "effect_status": "succeeded",
        "relation_id": relation_id,
        "canonical_read_back": read_back,
    }


def bootstrap_first_von_operational_administrator(
    *, subject_concept_id: str
) -> dict[str, Any]:
    """Establish the first root only inside a route-verified token context."""

    return _grant(
        subject_concept_id=subject_concept_id,
        reason="bootstrap",
        bootstrap_only=True,
    )


def grant_von_operational_administrator(
    *, subject_concept_id: str, reason: str | None = None
) -> dict[str, Any]:
    return _grant(
        subject_concept_id=subject_concept_id,
        reason=reason,
        bootstrap_only=False,
    )


def revoke_von_operational_administrator(
    *, subject_concept_id: str, reason: str | None = None
) -> dict[str, Any]:
    actor_id = _current_direct_human_actor()
    if not _can_manage_operational_administrators(actor_id):
        raise PermissionError("von_operational_administrator_required")
    subject_id = _normalise_concept_id(subject_concept_id)
    if not subject_id:
        raise ValueError("subject_concept_id is required")
    if not can_access_concept(subject_id):
        raise PermissionError("von_operational_administrator_target_not_accessible")
    rows = _role_rows(subject_id)
    all_rows = _role_rows()
    if rows and len(all_rows) <= len(rows):
        raise PermissionError("last_von_operational_administrator_cannot_be_revoked")
    deleted: list[str] = []
    try:
        with ontology_authority_membership_mutation_barrier():  # noqa: SIM117
            with ontology_mutation_resource_lock(_LIFECYCLE_RESOURCE_KEY):
                rows = _role_rows(subject_id)
                all_rows = _role_rows()
                if rows and len(all_rows) <= len(rows):
                    raise PermissionError(
                        "last_von_operational_administrator_cannot_be_revoked"
                    )
                for row in rows:
                    result = delete_text_relation(
                        subject_concept_id=subject_id,
                        relation_id=row["relation_id"],
                        garbage_collect=False,
                    )
                    if result.get("deleted"):
                        deleted.append(row["relation_id"])
    except OntologyMutationResourceBusy as exc:
        raise RuntimeError("von_operational_administrator_resource_busy") from exc
    read_back = von_operational_administrator_read_back(subject_concept_id=subject_id)
    if read_back["active"]:
        raise RuntimeError("von_operational_administrator_read_back_failed")
    return {
        "success": True,
        "changed": bool(deleted),
        "effect_status": "succeeded",
        "deleted_relation_ids": deleted,
        "reason": str(reason or "").strip() or None,
        "canonical_read_back": read_back,
    }


__all__ = [
    "VON_OPERATIONAL_ADMINISTRATOR_PREDICATE",
    "VON_OPERATIONAL_ADMINISTRATOR_ROLE",
    "bind_von_operational_administrator_bootstrap",
    "bootstrap_first_von_operational_administrator",
    "grant_von_operational_administrator",
    "is_live_von_operational_administrator",
    "list_von_operational_administrators",
    "revoke_von_operational_administrator",
    "von_operational_administrator_read_back",
]
