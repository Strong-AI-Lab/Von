"""Explicit per-login landing preferences, never membership or role grants.

Trusted callers supply the authenticated person and email. Represented JSON is
a small per-person preference map; arbitrary contact/domain facts are not read.
"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from ..security.access_control import bypass_access_control
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from .organisation_membership_service import resolve_user_organisation_membership
from .von_user_authentication_service import (
    find_user_concept_by_login_email, normalise_von_login_email,
)

PREDICATE = "#V#hasVonLoginOrganisationPreferences"


def read_preferences(user_concept_id: str) -> dict:
    with bypass_access_control():
        rows = get_texts_for_concept(user_concept_id, predicate=PREDICATE, limit=2)
    if not rows:
        return {}
    if len(rows) != 1:
        raise ValueError("ambiguous_login_organisation_preferences")
    value = json.loads(rows[0]["text"])
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or (v is not None and (
            not isinstance(v, str) or not v.startswith("#V#")
        )) for k, v in value.items()
    ):
        raise ValueError("invalid_login_organisation_preferences")
    return value


def set_login_organisation_preference(*, user_concept_id: str, email: str,
                                     organisation_concept_id: str | None,
                                     provenance: dict) -> dict:
    """Canonical primitive for an authorised owner/operator; grants no access."""
    from .ontology_publication_authority_service import ontology_mutation_resource_lock
    email = normalise_von_login_email(email)
    user = find_user_concept_by_login_email(email)
    if not user or user.get("concept_id") != user_concept_id:
        raise ValueError("login_email_subject_mismatch")
    if organisation_concept_id and not resolve_user_organisation_membership(
        user_concept_id, organisation_concept_id
    ):
        raise ValueError("organisation_membership_required")
    with ontology_mutation_resource_lock(f"login-organisation-preference:{user_concept_id}"):
        preferences = read_preferences(user_concept_id)
        preferences[email] = organisation_concept_id
        with bypass_access_control():
            upsert_singleton_text_relation(
                subject_concept_id=user_concept_id, predicate=PREDICATE,
                text=json.dumps(preferences, sort_keys=True), provenance=provenance,
            )
        if read_preferences(user_concept_id) != preferences:
            raise RuntimeError("login_organisation_preference_readback_failed")
    return {"user_concept_id": user_concept_id, "email": email,
            "organisation_concept_id": organisation_concept_id}


def initialise_login_context(session) -> dict:
    """Return a membership-validated default and a login/bootstrap generation.

    Called by authenticated status before browser features initialise. Existing
    deliberate tab choices survive reload; email/default changes start fresh.
    Missing/revoked preferences mean Personal, never the first membership.
    """
    actor = session["user_concept_id"]
    email = normalise_von_login_email(session["user_email"])
    desired = read_preferences(actor).get(email)
    membership = resolve_user_organisation_membership(actor, desired) if desired else None
    selected = desired if membership else None
    reason = "explicit_login_preference" if selected else (
        "preferred_membership_unavailable" if desired else "personal_default"
    )
    fingerprint = hashlib.sha256(json.dumps([actor, email, selected]).encode()).hexdigest()
    if session.get("login_context_fingerprint") != fingerprint:
        for key in ("organisation_concept_id", "role_in_org", "namespace", "session_id"):
            session.pop(key, None)
        session["login_context_fingerprint"] = fingerprint
        session["login_context_generation"] = uuid4().hex
        if selected:
            session["organisation_concept_id"] = selected.removeprefix("#V#")
            session["role_in_org"] = membership["role"]
    return {"generation": session["login_context_generation"],
            "organisation_concept_id": selected, "reason": reason}
