"""Server-authoritative Von login-email bindings.

``#V#has_email`` is ordinary descriptive contact information.  It must never
select an authenticated actor.  Google OAuth resolves only the deliberately
narrow ``#V#hasVonLoginEmail`` predicate, whose mutation is reserved to this
operator-controlled lifecycle.

One user may have several login emails.  One normalised email may identify at
most one user; ambiguous or stale bindings fail closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import logging
import re
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import bypass_access_control
from .exceptions import MultipleUsersForEmailError
from .text_value_service import (
    delete_text_relation_by_predicate_and_text,
    upsert_text_for_concept,
)

logger = logging.getLogger(__name__)

LOGIN_EMAIL_PREDICATE = "#V#hasVonLoginEmail"
GENERIC_EMAIL_PREDICATE = "#V#has_email"
PREDICATE_SPECIALISATION_PREDICATE = "#V#predicate_specialises_predicate"

_LOGIN_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")


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


def _normalise_concept_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    concept_id = value.strip()
    return concept_id if concept_id.startswith("#V#") else f"#V#{concept_id}"


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
    return {
        "success": True,
        "user_concept_id": normalised_user_id,
        "email": normalised_email,
        "removed": True,
    }


__all__ = [
    "GENERIC_EMAIL_PREDICATE",
    "LOGIN_EMAIL_PREDICATE",
    "LoginEmailBindingConflictError",
    "PREDICATE_SPECIALISATION_PREDICATE",
    "bind_von_login_email",
    "find_user_concept_by_login_email",
    "list_von_login_email_user_ids",
    "login_email_log_fingerprint",
    "normalise_von_login_email",
    "remove_von_login_email",
]
