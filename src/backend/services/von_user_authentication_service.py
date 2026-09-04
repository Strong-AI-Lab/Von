"""Server-authoritative Von login-email bindings.

``#V#has_email`` is ordinary descriptive contact information.  It must never
select an authenticated actor.  Google OAuth resolves only the deliberately
narrow ``#V#hasVonLoginEmail`` predicate, whose mutation is reserved to this
operator-controlled lifecycle.

One user may have several login emails.  One normalised email may identify at
most one user; ambiguous or stale bindings fail closed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import bypass_access_control
from .exceptions import MultipleUsersForEmailError
from .ontology_publication_authority_service import ontology_mutation_resource_lock
from .text_value_service import (
    delete_text_relation_by_predicate_and_text,
    upsert_text_for_concept,
)

logger = logging.getLogger(__name__)

LOGIN_EMAIL_PREDICATE = "#V#hasVonLoginEmail"
GENERIC_EMAIL_PREDICATE = "#V#has_email"
PREDICATE_SPECIALISATION_PREDICATE = "#V#predicate_specialises_predicate"

_LOGIN_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")
_LOGIN_EMAIL_RESOURCE_PREFIX = "von-login-email-binding:v1:"
_LOGIN_EMAIL_RESOURCE_STACK: ContextVar[tuple[str, ...]] = ContextVar(
    "von_login_email_resource_stack",
    default=(),
)


class LoginEmailBindingConflictError(RuntimeError):
    """The requested email is already bound to a different Von user."""

    def __init__(self, email: str, existing_user_ids: list[str]):
        self.email = email
        self.existing_user_ids = list(existing_user_ids)
        super().__init__("Login email is already bound to another Von user")


def normalise_von_login_email(value: object) -> str:
    """Return the canonical form used for exact OAuth identity lookup."""

    email = str(value or "").strip().casefold()
    if not email or not _LOGIN_EMAIL_PATTERN.fullmatch(email):
        raise ValueError("login_email_invalid")
    return email


def login_email_log_fingerprint(value: object) -> str:
    """Return a non-reversible short identifier suitable for auth logs."""

    try:
        normalised = normalise_von_login_email(value)
    except ValueError:
        normalised = str(value or "").strip().casefold()
    return sha256(normalised.encode("utf-8")).hexdigest()[:12]


@contextmanager
def von_login_email_binding_barrier(email: object) -> Iterator[None]:
    """Serialise every writer of one normalised login identity.

    The context-local stack makes the barrier re-entrant so a governed caller
    can hold it across its authority/check/write/read-back boundary while the
    low-level bind or remove primitive independently protects migration and
    other trusted callers.
    """

    normalised_email = normalise_von_login_email(email)
    resource_hash = sha256(normalised_email.encode("utf-8")).hexdigest()
    resource_key = f"{_LOGIN_EMAIL_RESOURCE_PREFIX}{resource_hash}"
    stack = _LOGIN_EMAIL_RESOURCE_STACK.get()
    if resource_key in stack:
        yield
        return
    token = _LOGIN_EMAIL_RESOURCE_STACK.set((*stack, resource_key))
    try:
        with ontology_mutation_resource_lock(resource_key):
            yield
    finally:
        _LOGIN_EMAIL_RESOURCE_STACK.reset(token)


def _normalise_concept_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    concept_id = value.strip()
    return concept_id if concept_id.startswith("#V#") else f"#V#{concept_id}"


def _require_login_email_store() -> None:
    if (
        TextValuesRepository.collection() is None
        or TextRelationsRepository.collection() is None
    ):
        raise RuntimeError("von_login_email_store_unavailable")


def _mark_login_email_search_projections_stale(user_concept_id: str) -> None:
    """Best-effort removal of login identities from legacy concept embeddings."""

    try:
        from .concept_embedding_service import mark_concept_embedding_stale

        mark_concept_embedding_stale(user_concept_id)
        mark_concept_embedding_stale(LOGIN_EMAIL_PREDICATE)
    except Exception as exc:  # noqa: BLE001 - derived projection is best effort
        logger.warning(
            "Could not mark Von login-email search projections stale user=%s error=%s",
            user_concept_id,
            type(exc).__name__,
        )


def _login_email_text_value_ids(email: str) -> list[Any]:
    rows = TextValuesRepository.find(
        {"text": email},
        projection={"_id": 1},
    )
    identifiers: list[Any] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("_id") is None:
            continue
        raw_id = row["_id"]
        for candidate in (raw_id, str(raw_id)):
            if candidate not in identifiers:
                identifiers.append(candidate)
    return identifiers


def list_von_login_email_user_ids(email: object) -> list[str]:
    """Return every subject explicitly bound to the normalised login email."""

    normalised_email = normalise_von_login_email(email)
    _require_login_email_store()
    text_value_ids = _login_email_text_value_ids(normalised_email)
    if not text_value_ids:
        return []
    relations = TextRelationsRepository.find(
        {
            "predicate": LOGIN_EMAIL_PREDICATE,
            "object_text_id": {"$in": text_value_ids},
        },
        projection={"_id": 0, "subject_concept_id": 1},
    )
    user_ids = {
        concept_id
        for relation in relations
        if isinstance(relation, Mapping)
        and (concept_id := _normalise_concept_id(relation.get("subject_concept_id")))
        is not None
    }
    return sorted(user_ids)


def list_von_login_emails_for_user(user_concept_id: object) -> list[str]:
    """Return the canonical login-email allow-list for one exact Von user.

    This is a trusted identity-service read.  Callers must establish their own
    authority before exposing the result; ordinary concept visibility is
    deliberately not widened to reveal authentication identifiers.
    """

    normalised_user_id = _normalise_concept_id(user_concept_id)
    if normalised_user_id is None:
        raise ValueError("user_concept_id_required")
    _require_login_email_store()
    relations = TextRelationsRepository.find(
        {
            "subject_concept_id": normalised_user_id,
            "predicate": LOGIN_EMAIL_PREDICATE,
        },
        projection={"_id": 0, "object_text_id": 1},
    )
    emails: set[str] = set()
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        text_value = TextValuesRepository.find_one_by_id(
            relation.get("object_text_id"),
            projection={"_id": 0, "text": 1},
        )
        if not isinstance(text_value, Mapping):
            continue
        try:
            emails.add(normalise_von_login_email(text_value.get("text")))
        except ValueError:
            logger.error(
                "Ignoring invalid stored Von login-email binding user=%s",
                normalised_user_id,
            )
    return sorted(emails)


def find_user_concept_by_login_email(email: object) -> dict[str, Any] | None:
    """Resolve one explicit login binding without consulting contact email.

    This is a trusted pre-authentication read and therefore deliberately does
    not depend on the as-yet-unresolved actor's visibility context.
    """

    normalised_email = normalise_von_login_email(email)
    user_ids = list_von_login_email_user_ids(normalised_email)
    if len(user_ids) > 1:
        logger.critical(
            "Ambiguous Von login-email binding fingerprint=%s count=%d",
            login_email_log_fingerprint(normalised_email),
            len(user_ids),
        )
        raise MultipleUsersForEmailError(normalised_email, user_ids)
    if not user_ids:
        return None

    with bypass_access_control():
        user = ConceptsRepository.find_one({"concept_id": user_ids[0]})
    if not isinstance(user, Mapping):
        logger.error(
            "Stale Von login-email binding fingerprint=%s",
            login_email_log_fingerprint(normalised_email),
        )
        return None
    result = dict(user)
    result["concept_id"] = user_ids[0]
    return result


def bind_von_login_email(
    *,
    user_concept_id: str,
    email: object,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add one explicit operator-authorised login binding and read it back."""

    normalised_user_id = _normalise_concept_id(user_concept_id)
    if normalised_user_id is None:
        raise ValueError("user_concept_id_required")
    normalised_email = normalise_von_login_email(email)

    with bypass_access_control():
        user = ConceptsRepository.find_one(
            {"concept_id": normalised_user_id},
            projection={"_id": 1, "concept_id": 1},
        )
    if not isinstance(user, Mapping):
        raise ValueError(f"user_concept_not_found:{normalised_user_id}")

    with von_login_email_binding_barrier(normalised_email):
        existing_user_ids = list_von_login_email_user_ids(normalised_email)
        conflicting_user_ids = [
            concept_id
            for concept_id in existing_user_ids
            if concept_id != normalised_user_id
        ]
        if conflicting_user_ids:
            raise LoginEmailBindingConflictError(
                normalised_email,
                conflicting_user_ids,
            )

        with bypass_access_control():
            write_result = upsert_text_for_concept(
                subject_concept_id=normalised_user_id,
                predicate=LOGIN_EMAIL_PREDICATE,
                text=normalised_email,
                provenance={
                    "source": "von_login_email_binding",
                    **dict(provenance or {}),
                },
            )

        read_back_user_ids = list_von_login_email_user_ids(normalised_email)
        if read_back_user_ids != [normalised_user_id]:
            raise RuntimeError("von_login_email_binding_read_back_failed")
    _mark_login_email_search_projections_stale(normalised_user_id)
    return {
        "success": True,
        "user_concept_id": normalised_user_id,
        "email": normalised_email,
        "relation_created": bool(write_result.get("relation_created")),
        "read_back_user_concept_id": read_back_user_ids[0],
    }


def remove_von_login_email(
    *,
    user_concept_id: str,
    email: object,
) -> dict[str, Any]:
    """Revoke one exact login binding without deleting contact information."""

    normalised_user_id = _normalise_concept_id(user_concept_id)
    if normalised_user_id is None:
        raise ValueError("user_concept_id_required")
    normalised_email = normalise_von_login_email(email)
    with von_login_email_binding_barrier(normalised_email):
        existing_user_ids = list_von_login_email_user_ids(normalised_email)
        if normalised_user_id not in existing_user_ids:
            return {
                "success": True,
                "user_concept_id": normalised_user_id,
                "email": normalised_email,
                "removed": False,
            }

        with bypass_access_control():
            delete_text_relation_by_predicate_and_text(
                normalised_user_id,
                LOGIN_EMAIL_PREDICATE,
                normalised_email,
                garbage_collect=False,
            )
        if normalised_user_id in list_von_login_email_user_ids(normalised_email):
            raise RuntimeError("von_login_email_revocation_read_back_failed")
    _mark_login_email_search_projections_stale(normalised_user_id)
    return {
        "success": True,
        "user_concept_id": normalised_user_id,
        "email": normalised_email,
        "removed": True,
    }


__all__ = [
    "GENERIC_EMAIL_PREDICATE",
    "LOGIN_EMAIL_PREDICATE",
    "PREDICATE_SPECIALISATION_PREDICATE",
    "LoginEmailBindingConflictError",
    "bind_von_login_email",
    "find_user_concept_by_login_email",
    "list_von_login_email_user_ids",
    "list_von_login_emails_for_user",
    "login_email_log_fingerprint",
    "normalise_von_login_email",
    "remove_von_login_email",
    "von_login_email_binding_barrier",
]
