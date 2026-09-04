"""Governed organisation-administrator lifecycle for Von login emails.

Login-email bindings authenticate a global Von person, so generic ontology
tools must neither reveal nor mutate them.  This service exposes the smallest
dedicated path: exact-organisation administrators can inspect a member's
canonical bindings, while a bind request (including global uniqueness
certification) or a removal that would change state additionally requires the
actor to administer every organisation the target can enter (or to hold the
separate global Von operational-administrator role).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_von_login_email_receipts_collection
from ..security.access_control import (
    LEGACY_IDENTITY_HEADER_ACTOR_SOURCE,
    get_effective_user_concept_id_with_source,
)
from ..security.role_resolver import get_effective_permissions
from .ontology_authority_membership_coordination_service import (
    ontology_authority_membership_mutation_barrier,
)
from .ontology_publication_authority_service import OntologyMutationResourceBusy
from .organisation_membership_service import (
    get_user_memberships,
    resolve_user_organisation_membership,
)
from .von_operational_administrator_service import (
    is_live_von_operational_administrator,
)
from .von_user_authentication_service import (
    LoginEmailBindingConflictError,
    bind_von_login_email,
    list_von_login_email_user_ids,
    list_von_login_emails_for_user,
    normalise_von_login_email,
    remove_von_login_email,
    von_login_email_binding_barrier,
)

READ_SCHEMA_VERSION = "von_login_email_bindings.v1"
MUTATION_SCHEMA_VERSION = "von_login_email_management.v1"
RECEIPT_SCHEMA_VERSION = "von_login_email_mutation_receipt.v1"
_SUPPORTED_ACTIONS = {"bind", "remove"}
_PENDING_RECONCILIATION_MIN_AGE = timedelta(seconds=35)
_RECEIPT_PHASE_PRE_EFFECT = "pre_effect"
_RECEIPT_PHASE_POSTCONDITION_PREEXISTING = "postcondition_preexisting"
_RECEIPT_PHASE_EFFECT_MAY_HAVE_STARTED = "effect_may_have_started"
_RECEIPT_PHASE_TERMINAL = "terminal"


def _now() -> datetime:
    return datetime.now(UTC)


def _normalise_concept_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned.removeprefix('#')}"


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error(
    code: str,
    message: str,
    *,
    schema_version: str = MUTATION_SCHEMA_VERSION,
    effect_status: str = "not_started",
    mutation_outcome: str = "not_started",
    changed: bool | None = False,
    **details: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "schema_version": schema_version,
        "success": False,
        "error_code": code,
        "error": message,
    }
    if schema_version == MUTATION_SCHEMA_VERSION:
        response.update(
            {
                "effect_status": effect_status,
                "mutation_outcome": mutation_outcome,
                "outcome_finality": (
                    "terminal_not_started"
                    if effect_status == "not_started"
                    else "requires_canonical_reconciliation"
                ),
                "changed": changed,
            }
        )
    response.update(details)
    return response


def _trusted_actor(
    acting_actor_concept_id: object = None,
) -> tuple[str | None, tuple[str, str] | None]:
    try:
        from ..integrations.internal_mcp.gateway import (
            internal_mcp_actor_context_is_untrusted_payload_fallback,
        )

        payload_identity_only = (
            internal_mcp_actor_context_is_untrusted_payload_fallback()
        )
    except Exception:  # noqa: BLE001 - optional transport provenance probe
        payload_identity_only = False
    actor_id, source = get_effective_user_concept_id_with_source()
    actor_id = _normalise_concept_id(actor_id)
    claimed_actor = _normalise_concept_id(acting_actor_concept_id)
    if payload_identity_only:
        return None, (
            "client_supplied_identity_is_not_authority",
            "Client or model supplied identity cannot authorise login-email access.",
        )
    if not actor_id or source == LEGACY_IDENTITY_HEADER_ACTOR_SOURCE:
        return None, (
            "authenticated_actor_context_required",
            "Von login-email access requires trusted actor provenance.",
        )
    if claimed_actor is not None and claimed_actor != actor_id:
        return None, (
            "actor_context_mismatch",
            "The supplied actor does not match the trusted server actor.",
        )
    return actor_id, None


def _exact_organisation_authority(
    *,
    actor_id: str,
    user_id: str,
    organisation_id: str,
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    operational_admin = is_live_von_operational_administrator(actor_id)
    actor_membership = resolve_user_organisation_membership(actor_id, organisation_id)
    actor_role = (
        str(actor_membership.get("role") or "member")
        if isinstance(actor_membership, Mapping)
        else None
    )
    actor_permissions = get_effective_permissions(actor_role or "")
    allowed = operational_admin or "MANAGE_MEMBERS" in actor_permissions
    decision = {
        "allowed": allowed,
        "actor_concept_id": actor_id,
        "organisation_concept_id": organisation_id,
        "actor_role": actor_role,
        "required_permissions": ["MANAGE_MEMBERS"],
        "global_operational_administrator": operational_admin,
        "authority_scope": "exact_organisation",
    }
    if not allowed:
        return decision, (
            "organisation_login_identity_management_authority_required",
            "A live organisation admin or owner must manage this member's login email.",
        )
    try:
        target_membership = resolve_user_organisation_membership(
            user_id, organisation_id
        )
    except (LookupError, ValueError):
        target_membership = None
    if not isinstance(target_membership, Mapping):
        return decision, (
            "target_organisation_membership_required",
            "The target must be a represented member of the selected organisation.",
        )
    return decision, None


def _global_mutation_authority(
    *,
    actor_id: str,
    user_id: str,
    exact_decision: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    if exact_decision.get("global_operational_administrator") is True:
        return {
            **dict(exact_decision),
            "allowed": True,
            "authority_scope": "global_operational_administrator",
        }, None

    membership_inventory = get_user_memberships(user_id)
    target_memberships = (
        membership_inventory.get("memberships")
        if isinstance(membership_inventory, Mapping)
        else None
    )
    expected_org_id = _normalise_concept_id(
        exact_decision.get("organisation_concept_id")
    )
    if not isinstance(target_memberships, list) or expected_org_id is None:
        return {
            **dict(exact_decision),
            "allowed": False,
            "authority_scope": "all_target_organisations",
            "all_target_organisations_covered": False,
            "membership_inventory_complete": False,
        }, (
            "cross_organisation_login_identity_authority_required",
            "Changing this global login identity requires member-management authority in every organisation the target can enter.",
        )

    represented_target_orgs: set[str] = set()
    for membership in target_memberships:
        if not isinstance(membership, Mapping):
            return {
                **dict(exact_decision),
                "allowed": False,
                "authority_scope": "all_target_organisations",
                "all_target_organisations_covered": False,
                "membership_inventory_complete": False,
            }, (
                "cross_organisation_login_identity_authority_required",
                "Changing this global login identity requires member-management authority in every organisation the target can enter.",
            )
        target_org_id = _normalise_concept_id(membership.get("organisation_concept_id"))
        if target_org_id is None:
            return {
                **dict(exact_decision),
                "allowed": False,
                "authority_scope": "all_target_organisations",
                "all_target_organisations_covered": False,
                "membership_inventory_complete": False,
            }, (
                "cross_organisation_login_identity_authority_required",
                "Changing this global login identity requires member-management authority in every organisation the target can enter.",
            )
        represented_target_orgs.add(target_org_id)
        actor_membership = resolve_user_organisation_membership(actor_id, target_org_id)
        actor_role = (
            str(actor_membership.get("role") or "member")
            if isinstance(actor_membership, Mapping)
            else None
        )
        if "MANAGE_MEMBERS" not in get_effective_permissions(actor_role or ""):
            return {
                **dict(exact_decision),
                "allowed": False,
                "authority_scope": "all_target_organisations",
                "all_target_organisations_covered": False,
                "membership_inventory_complete": True,
            }, (
                "cross_organisation_login_identity_authority_required",
                "Changing this global login identity requires member-management authority in every organisation the target can enter.",
            )
    if expected_org_id not in represented_target_orgs:
        return {
            **dict(exact_decision),
            "allowed": False,
            "authority_scope": "all_target_organisations",
            "all_target_organisations_covered": False,
            "membership_inventory_complete": False,
        }, (
            "cross_organisation_login_identity_authority_required",
            "Changing this global login identity requires member-management authority in every organisation the target can enter.",
        )

    return {
        **dict(exact_decision),
        "allowed": True,
        "authority_scope": "all_target_organisations",
        "all_target_organisations_covered": True,
        "membership_inventory_complete": True,
    }, None


def get_von_login_email_bindings(
    *,
    user_concept_id: object,
    organisation_concept_id: object,
    acting_actor_concept_id: object = None,
) -> dict[str, Any]:
    """Read one organisation member's canonical login-email allow-list."""

    actor_id, actor_error = _trusted_actor(acting_actor_concept_id)
    if actor_error is not None:
        return _error(*actor_error, schema_version=READ_SCHEMA_VERSION)
    assert actor_id is not None
    user_id = _normalise_concept_id(user_concept_id)
    organisation_id = _normalise_concept_id(organisation_concept_id)
    if user_id is None or organisation_id is None:
        return _error(
            "missing_parameter",
            "user_concept_id and organisation_concept_id are required.",
            schema_version=READ_SCHEMA_VERSION,
        )
    try:
        with ontology_authority_membership_mutation_barrier():
            authority_decision, denial = _exact_organisation_authority(
                actor_id=actor_id,
                user_id=user_id,
                organisation_id=organisation_id,
            )
            if denial is not None:
                return _error(
                    *denial,
                    schema_version=READ_SCHEMA_VERSION,
                    authority_decision=authority_decision,
                )
            emails = list_von_login_emails_for_user(user_id)
    except OntologyMutationResourceBusy:
        return _error(
            "ontology_mutation_resource_busy",
            "Organisation authority is changing; retry the login-email read.",
            schema_version=READ_SCHEMA_VERSION,
        )
    except (LookupError, ValueError):
        return _error(
            "target_organisation_membership_required",
            "The target must be a represented member of the selected organisation.",
            schema_version=READ_SCHEMA_VERSION,
        )
    canonical = {
        "user_concept_id": user_id,
        "organisation_concept_id": organisation_id,
        "login_emails": emails,
        "total_count": len(emails),
    }
    return {
        "schema_version": READ_SCHEMA_VERSION,
        "success": True,
        "actor_concept_id": actor_id,
        **canonical,
        "authority_decision": authority_decision,
        "canonical_read_back": canonical,
    }


def _receipt_collection():
    collection = get_von_login_email_receipts_collection()
    if collection is None:
        raise RuntimeError("Von login-email receipt store unavailable")
    return collection


def _receipt_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    def serialise_time(value: object) -> object:
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "schema_version": str(document.get("schema_version") or RECEIPT_SCHEMA_VERSION),
        "receipt_id": document.get("receipt_id"),
        "request_id": document.get("request_id"),
        "actor_concept_id": document.get("actor_concept_id"),
        "action": document.get("action"),
        "user_concept_id": document.get("user_concept_id"),
        "organisation_concept_id": document.get("organisation_concept_id"),
        "email": document.get("email"),
        "intent_fingerprint": document.get("intent_fingerprint"),
        "status": document.get("status"),
        "effect_phase": document.get("effect_phase"),
        "mutation_outcome": document.get("mutation_outcome"),
        "changed": document.get("changed"),
        "error_code": document.get("error_code"),
        "canonical_read_back": document.get("canonical_read_back"),
        "canonical_read_back_sha256": document.get("canonical_read_back_sha256"),
        "created_at": serialise_time(document.get("created_at")),
        "updated_at": serialise_time(document.get("updated_at")),
        "completed_at": serialise_time(document.get("completed_at")),
    }


def _begin_receipt(
    *,
    actor_id: str,
    request_id: str,
    action: str,
    user_id: str,
    organisation_id: str,
    email: str,
    reason: str | None,
    intent_fingerprint: str,
) -> tuple[dict[str, Any], bool]:
    collection = _receipt_collection()
    existing = collection.find_one(
        {"actor_concept_id": actor_id, "request_id": request_id}
    )
    if isinstance(existing, Mapping):
        return dict(existing), False
    now = _now()
    receipt_id = (
        "vlr_" + hashlib.sha256(f"{actor_id}\x1f{request_id}".encode()).hexdigest()
    )
    document = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "actor_concept_id": actor_id,
        "request_id": request_id,
        "action": action,
        "user_concept_id": user_id,
        "organisation_concept_id": organisation_id,
        "email": email,
        "reason": reason,
        "intent_fingerprint": intent_fingerprint,
        "status": "pending",
        "effect_phase": _RECEIPT_PHASE_PRE_EFFECT,
        "mutation_outcome": "not_started",
        "changed": False,
        "created_at": now,
        "updated_at": now,
    }
    try:
        collection.insert_one(document)
        return document, True
    except DuplicateKeyError:
        raced = collection.find_one(
            {"actor_concept_id": actor_id, "request_id": request_id}
        )
        if not isinstance(raced, Mapping):
            raise
        return dict(raced), False


def _finalise_receipt(
    receipt_id: str,
    *,
    status: str,
    mutation_outcome: str,
    changed: bool | None,
    canonical_read_back: Mapping[str, Any] | None,
    authority_decision: Mapping[str, Any] | None,
    response: Mapping[str, Any],
    error_code: str | None = None,
) -> dict[str, Any]:
    now = _now()
    updated = _receipt_collection().find_one_and_update(
        {"receipt_id": receipt_id, "status": "pending"},
        {
            "$set": {
                "status": status,
                "effect_phase": _RECEIPT_PHASE_TERMINAL,
                "mutation_outcome": mutation_outcome,
                "changed": changed,
                "canonical_read_back": (
                    dict(canonical_read_back)
                    if isinstance(canonical_read_back, Mapping)
                    else None
                ),
                "canonical_read_back_sha256": (
                    _sha256_json(canonical_read_back)
                    if isinstance(canonical_read_back, Mapping)
                    else None
                ),
                "authority_decision": (
                    dict(authority_decision)
                    if isinstance(authority_decision, Mapping)
                    else None
                ),
                "response_projection": dict(response),
                "error_code": error_code,
                "updated_at": now,
                "completed_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not isinstance(updated, Mapping):
        updated = _receipt_collection().find_one({"receipt_id": receipt_id})
    if not isinstance(updated, Mapping):
        raise RuntimeError(  # noqa: TRY004 - missing receipt, not caller type
            "Von login-email receipt finalisation failed"
        )
    return dict(updated)


def _checkpoint_pending_receipt(
    receipt: Mapping[str, Any],
    *,
    effect_phase: str,
    authority_decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Durably record the authority/effect phase before returning or writing."""

    now = _now()
    updated = _receipt_collection().find_one_and_update(
        {
            "receipt_id": receipt.get("receipt_id"),
            "status": "pending",
        },
        {
            "$set": {
                "effect_phase": effect_phase,
                "authority_decision": dict(authority_decision),
                "effect_phase_updated_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not isinstance(updated, Mapping):
        raise RuntimeError(  # noqa: TRY004 - missing receipt, not caller type
            "Von login-email receipt checkpoint failed"
        )
    return dict(updated)


def _response_with_receipt(
    response: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    return {**dict(response), "governance_receipt": _receipt_projection(document)}


def _response_with_finalisation(
    *,
    initial_receipt: Mapping[str, Any],
    response: Mapping[str, Any],
    status: str,
    mutation_outcome: str,
    changed: bool | None,
    canonical_read_back: Mapping[str, Any] | None,
    authority_decision: Mapping[str, Any] | None,
    error_code: str | None = None,
) -> dict[str, Any]:
    try:
        final = _finalise_receipt(
            str(initial_receipt["receipt_id"]),
            status=status,
            mutation_outcome=mutation_outcome,
            changed=changed,
            canonical_read_back=canonical_read_back,
            authority_decision=authority_decision,
            response=response,
            error_code=error_code,
        )
    except Exception as exc:  # noqa: BLE001 - canonical outcome still matters
        pending_response = {
            **dict(response),
            "receipt_finalisation": "pending_reconciliation",
            "receipt_finalisation_error": type(exc).__name__,
            "outcome_finality": (
                "canonical_effect_verified_receipt_pending"
                if response.get("success") is True
                else "requires_receipt_and_canonical_reconciliation"
            ),
        }
        return _response_with_receipt(pending_response, initial_receipt)
    return _response_with_receipt(response, final)


def _pending_receipt_is_stale(receipt: Mapping[str, Any]) -> bool:
    updated_at = receipt.get("updated_at") or receipt.get("created_at")
    if not isinstance(updated_at, datetime):
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    else:
        updated_at = updated_at.astimezone(UTC)
    return _now() - updated_at >= _PENDING_RECONCILIATION_MIN_AGE


def _reconcile_pending_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if str(receipt.get("status") or "") != "pending":
        return dict(receipt)
    if not _pending_receipt_is_stale(receipt):
        return dict(receipt)
    effect_phase = str(receipt.get("effect_phase") or _RECEIPT_PHASE_PRE_EFFECT)
    if effect_phase not in {
        _RECEIPT_PHASE_POSTCONDITION_PREEXISTING,
        _RECEIPT_PHASE_EFFECT_MAY_HAVE_STARTED,
    }:
        response = _error(
            "von_login_email_mutation_not_started",
            "The pending receipt did not cross a verified login-email effect boundary.",
            reconciled_from_pending_receipt=True,
        )
        try:
            return _finalise_receipt(
                str(receipt["receipt_id"]),
                status="failed",
                mutation_outcome="not_started",
                changed=False,
                canonical_read_back=None,
                authority_decision=None,
                response=response,
                error_code="von_login_email_mutation_not_started",
            )
        except Exception:  # noqa: BLE001 - preserve the durable pending record
            return dict(receipt)

    user_id = _normalise_concept_id(receipt.get("user_concept_id"))
    organisation_id = _normalise_concept_id(receipt.get("organisation_concept_id"))
    actor_id = _normalise_concept_id(receipt.get("actor_concept_id"))
    action = _clean_text(receipt.get("action"))
    recorded_authority = receipt.get("authority_decision")
    try:
        email = normalise_von_login_email(receipt.get("email"))
    except ValueError:
        return dict(receipt)
    if (
        user_id is None
        or organisation_id is None
        or actor_id is None
        or action not in _SUPPORTED_ACTIONS
        or not isinstance(recorded_authority, Mapping)
        or recorded_authority.get("allowed") is not True
    ):
        return dict(receipt)
    try:
        with ontology_authority_membership_mutation_barrier():
            reconciliation_authority, denial = _exact_organisation_authority(
                actor_id=actor_id,
                user_id=user_id,
                organisation_id=organisation_id,
            )
            if denial is not None:
                response = _error(
                    "von_login_email_receipt_reconciliation_authority_required",
                    "Current exact-organisation authority is required to reconcile this login-email receipt.",
                    effect_status="indeterminate",
                    mutation_outcome="indeterminate",
                    changed=None,
                    authority_decision=reconciliation_authority,
                    canonical_read_back=None,
                    reconciled_from_pending_receipt=True,
                )
                return _finalise_receipt(
                    str(receipt["receipt_id"]),
                    status="indeterminate",
                    mutation_outcome="indeterminate",
                    changed=None,
                    canonical_read_back=None,
                    authority_decision=dict(recorded_authority),
                    response=response,
                    error_code=(
                        "von_login_email_receipt_reconciliation_authority_required"
                    ),
                )
            with von_login_email_binding_barrier(email):
                canonical, _ = _binding_observation(user_id, email)
        postcondition_verified = _postcondition_holds(action, canonical)
        response = {
            "schema_version": MUTATION_SCHEMA_VERSION,
            "success": postcondition_verified,
            "effect_status": (
                "succeeded" if postcondition_verified else "indeterminate"
            ),
            "mutation_outcome": (
                "succeeded" if postcondition_verified else "indeterminate"
            ),
            "outcome_finality": (
                "terminal_canonical_read_back"
                if postcondition_verified
                else "requires_canonical_reconciliation"
            ),
            "changed": None,
            "action": action,
            "actor_concept_id": actor_id,
            "user_concept_id": user_id,
            "organisation_concept_id": organisation_id,
            "email": email,
            "canonical_read_back": canonical,
            "reconciled_from_pending_receipt": True,
            "reconciliation_authority_decision": reconciliation_authority,
        }
        return _finalise_receipt(
            str(receipt["receipt_id"]),
            status="succeeded" if postcondition_verified else "indeterminate",
            mutation_outcome=(
                "succeeded" if postcondition_verified else "indeterminate"
            ),
            changed=None,
            canonical_read_back=canonical,
            authority_decision=dict(recorded_authority),
            response=response,
            error_code=(
                None
                if postcondition_verified
                else "von_login_email_postcondition_not_verified"
            ),
        )
    except Exception:  # noqa: BLE001 - preserve the durable pending record
        return dict(receipt)


def _replay_or_conflict(
    receipt: Mapping[str, Any], intent_fingerprint: str
) -> dict[str, Any]:
    if receipt.get("intent_fingerprint") != intent_fingerprint:
        return _response_with_receipt(
            _error(
                "idempotency_request_conflict",
                "This request_id is already bound to a different login-email intent.",
            ),
            receipt,
        )
    if receipt.get("status") == "pending":
        receipt = _reconcile_pending_receipt(receipt)
    if receipt.get("status") == "pending":
        return _response_with_receipt(
            _error(
                "von_login_email_mutation_in_progress",
                "The matching login-email effect is still in progress.",
                effect_status="partial",
                mutation_outcome="partial",
                changed=None,
            ),
            receipt,
        )
    projection = receipt.get("response_projection")
    if isinstance(projection, Mapping):
        return _response_with_receipt(projection, receipt)
    return _response_with_receipt(
        _error(
            "von_login_email_receipt_incomplete",
            "The matching receipt exists but has no response projection.",
            effect_status="indeterminate",
            mutation_outcome="indeterminate",
            changed=None,
        ),
        receipt,
    )


def _binding_observation(
    user_id: str,
    email: str,
) -> tuple[dict[str, Any], bool]:
    owners = list_von_login_email_user_ids(email)
    binding_present = user_id in owners
    canonical = {
        "user_concept_id": user_id,
        "email": email,
        "binding_present": binding_present,
    }
    if binding_present:
        canonical["binding_unambiguous"] = owners == [user_id]
    return canonical, any(owner_id != user_id for owner_id in owners)


def _postcondition_holds(action: str, canonical_state: Mapping[str, Any]) -> bool:
    if action == "bind":
        return (
            canonical_state.get("binding_present") is True
            and canonical_state.get("binding_unambiguous") is True
        )
    return canonical_state.get("binding_present") is False


def _complete_failure(
    *,
    initial_receipt: Mapping[str, Any],
    code: str,
    message: str,
    authority_decision: Mapping[str, Any] | None = None,
    canonical_read_back: Mapping[str, Any] | None = None,
    receipt_status: str = "failed",
) -> dict[str, Any]:
    response = _error(
        code,
        message,
        authority_decision=authority_decision,
        canonical_read_back=canonical_read_back,
    )
    return _response_with_finalisation(
        initial_receipt=initial_receipt,
        response=response,
        status=receipt_status,
        mutation_outcome="not_started",
        changed=False,
        canonical_read_back=canonical_read_back,
        authority_decision=authority_decision,
        error_code=code,
    )


def _successful_response(
    *,
    action: str,
    actor_id: str,
    user_id: str,
    organisation_id: str,
    email: str,
    changed: bool,
    authority_decision: Mapping[str, Any],
    canonical_read_back: Mapping[str, Any],
    reconciled_after_exception: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "schema_version": MUTATION_SCHEMA_VERSION,
        "success": True,
        "effect_status": "succeeded",
        "mutation_outcome": "succeeded",
        "outcome_finality": "terminal_canonical_read_back",
        "changed": changed,
        "action": action,
        "actor_concept_id": actor_id,
        "user_concept_id": user_id,
        "organisation_concept_id": organisation_id,
        "email": email,
        "authority_decision": dict(authority_decision),
        "canonical_read_back": dict(canonical_read_back),
    }
    if not changed:
        response["postcondition_preexisting"] = True
    if reconciled_after_exception:
        response["reconciled_after_exception"] = reconciled_after_exception
    return response


def manage_von_login_email(
    *,
    action: object,
    user_concept_id: object,
    organisation_concept_id: object,
    email: object,
    request_id: object,
    reason: object = None,
    acting_actor_concept_id: object = None,
) -> dict[str, Any]:
    """Bind or remove one member login email with authority and read-back."""

    actor_id, actor_error = _trusted_actor(acting_actor_concept_id)
    if actor_error is not None:
        return _error(*actor_error)
    assert actor_id is not None
    action_value = _clean_text(action)
    user_id = _normalise_concept_id(user_concept_id)
    organisation_id = _normalise_concept_id(organisation_concept_id)
    request_value = _clean_text(request_id)
    reason_value = _clean_text(reason)
    if action_value not in _SUPPORTED_ACTIONS:
        return _error(
            "invalid_von_login_email_action",
            "action must be bind or remove.",
        )
    if user_id is None or organisation_id is None or request_value is None:
        return _error(
            "missing_parameter",
            "user_concept_id, organisation_concept_id, and request_id are required.",
        )
    if len(request_value) > 256:
        return _error(
            "invalid_request_id",
            "request_id must be at most 256 characters.",
        )
    if reason_value is not None and len(reason_value) > 1000:
        return _error("invalid_reason", "reason must be at most 1000 characters.")
    try:
        email_value = normalise_von_login_email(email)
    except ValueError:
        return _error("login_email_invalid", "A valid login email is required.")

    intent = {
        "action": action_value,
        "user_concept_id": user_id,
        "organisation_concept_id": organisation_id,
        "email": email_value,
    }
    intent_fingerprint = _sha256_json(intent)
    receipt, created = _begin_receipt(
        actor_id=actor_id,
        request_id=request_value,
        action=action_value,
        user_id=user_id,
        organisation_id=organisation_id,
        email=email_value,
        reason=reason_value,
        intent_fingerprint=intent_fingerprint,
    )
    if not created:
        return _replay_or_conflict(receipt, intent_fingerprint)

    mutation_started = False
    authority_decision: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    try:
        with ontology_authority_membership_mutation_barrier():
            authority_decision, denial = _exact_organisation_authority(
                actor_id=actor_id,
                user_id=user_id,
                organisation_id=organisation_id,
            )
            if denial is not None:
                return _complete_failure(
                    initial_receipt=receipt,
                    code=denial[0],
                    message=denial[1],
                    authority_decision=authority_decision,
                    receipt_status="denied",
                )

            with von_login_email_binding_barrier(email_value):
                target_bound = email_value in list_von_login_emails_for_user(user_id)
                before = {
                    "user_concept_id": user_id,
                    "email": email_value,
                    "binding_present": target_bound,
                }
                if action_value == "bind" or target_bound:
                    authority_decision, cross_org_denial = _global_mutation_authority(
                        actor_id=actor_id,
                        user_id=user_id,
                        exact_decision=authority_decision,
                    )
                    if cross_org_denial is not None:
                        return _complete_failure(
                            initial_receipt=receipt,
                            code=cross_org_denial[0],
                            message=cross_org_denial[1],
                            authority_decision=authority_decision,
                            canonical_read_back=before,
                            receipt_status="denied",
                        )

                    before, other_binding = _binding_observation(user_id, email_value)
                    if action_value == "bind" and other_binding:
                        return _complete_failure(
                            initial_receipt=receipt,
                            code="von_login_email_binding_unavailable",
                            message=(
                                "The requested login-email binding cannot be applied."
                            ),
                            authority_decision=authority_decision,
                            canonical_read_back=before,
                            receipt_status="denied",
                        )

                requested_state_preexists = (
                    action_value == "bind" and before["binding_present"] is True
                ) or (action_value == "remove" and before["binding_present"] is False)
                if requested_state_preexists:
                    receipt = _checkpoint_pending_receipt(
                        receipt,
                        effect_phase=_RECEIPT_PHASE_POSTCONDITION_PREEXISTING,
                        authority_decision=authority_decision,
                    )
                    response = _successful_response(
                        action=action_value,
                        actor_id=actor_id,
                        user_id=user_id,
                        organisation_id=organisation_id,
                        email=email_value,
                        changed=False,
                        authority_decision=authority_decision,
                        canonical_read_back=before,
                    )
                    return _response_with_finalisation(
                        initial_receipt=receipt,
                        response=response,
                        status="succeeded",
                        mutation_outcome="succeeded",
                        changed=False,
                        canonical_read_back=before,
                        authority_decision=authority_decision,
                    )

                receipt = _checkpoint_pending_receipt(
                    receipt,
                    effect_phase=_RECEIPT_PHASE_EFFECT_MAY_HAVE_STARTED,
                    authority_decision=authority_decision,
                )
                mutation_started = True
                if action_value == "bind":
                    write_result = bind_von_login_email(
                        user_concept_id=user_id,
                        email=email_value,
                        provenance={
                            "authorised_by_actor_concept_id": actor_id,
                            "organisation_concept_id": organisation_id,
                            "request_id": request_value,
                            "reason": reason_value,
                        },
                    )
                    changed = bool(write_result.get("relation_created"))
                else:
                    write_result = remove_von_login_email(
                        user_concept_id=user_id,
                        email=email_value,
                    )
                    changed = bool(write_result.get("removed"))
                canonical, _ = _binding_observation(user_id, email_value)
                if not _postcondition_holds(action_value, canonical):
                    raise RuntimeError("von_login_email_postcondition_not_met")
                response = _successful_response(
                    action=action_value,
                    actor_id=actor_id,
                    user_id=user_id,
                    organisation_id=organisation_id,
                    email=email_value,
                    changed=changed,
                    authority_decision=authority_decision,
                    canonical_read_back=canonical,
                )
                return _response_with_finalisation(
                    initial_receipt=receipt,
                    response=response,
                    status="succeeded",
                    mutation_outcome="succeeded",
                    changed=changed,
                    canonical_read_back=canonical,
                    authority_decision=authority_decision,
                )
    except LoginEmailBindingConflictError:
        return _complete_failure(
            initial_receipt=receipt,
            code="von_login_email_binding_unavailable",
            message="The requested login-email binding cannot be applied.",
            authority_decision=authority_decision,
            canonical_read_back=before,
        )
    except Exception as exc:  # noqa: BLE001 - reconcile after possible effect
        canonical = None
        if mutation_started:
            try:
                canonical, _ = _binding_observation(user_id, email_value)
            except Exception:  # noqa: BLE001 - receipt records uncertainty
                canonical = None
        if (
            mutation_started
            and isinstance(canonical, Mapping)
            and _postcondition_holds(action_value, canonical)
        ):
            changed = canonical != before
            response = _successful_response(
                action=action_value,
                actor_id=actor_id,
                user_id=user_id,
                organisation_id=organisation_id,
                email=email_value,
                changed=changed,
                authority_decision=authority_decision or {},
                canonical_read_back=canonical,
                reconciled_after_exception=type(exc).__name__,
            )
            return _response_with_finalisation(
                initial_receipt=receipt,
                response=response,
                status="succeeded",
                mutation_outcome="succeeded",
                changed=changed,
                canonical_read_back=canonical,
                authority_decision=authority_decision,
            )

        effect_may_have_crossed = mutation_started
        error_code = (
            "ontology_mutation_resource_busy"
            if isinstance(exc, OntologyMutationResourceBusy)
            else "von_login_email_mutation_failed"
        )
        response = _error(
            error_code,
            (
                "The login-email resource is busy; retry with the same request."
                if isinstance(exc, OntologyMutationResourceBusy)
                else "The login-email effect could not be canonically verified."
            ),
            effect_status="indeterminate" if effect_may_have_crossed else "not_started",
            mutation_outcome=(
                "indeterminate" if effect_may_have_crossed else "not_started"
            ),
            changed=None if effect_may_have_crossed else False,
            authority_decision=authority_decision,
            canonical_read_back=canonical,
        )
        return _response_with_finalisation(
            initial_receipt=receipt,
            response=response,
            status="indeterminate" if effect_may_have_crossed else "failed",
            mutation_outcome=(
                "indeterminate" if effect_may_have_crossed else "not_started"
            ),
            changed=None if effect_may_have_crossed else False,
            canonical_read_back=canonical,
            authority_decision=authority_decision,
            error_code=error_code,
        )


def get_von_login_email_management_receipt(
    *,
    receipt_id: object,
    acting_actor_concept_id: object = None,
) -> dict[str, Any]:
    """Read one actor-owned login-email management receipt."""

    actor_id, actor_error = _trusted_actor(acting_actor_concept_id)
    if actor_error is not None:
        return _error(*actor_error)
    assert actor_id is not None
    receipt_value = _clean_text(receipt_id)
    if receipt_value is None:
        return _error("receipt_id_required", "receipt_id is required.")
    document = _receipt_collection().find_one(
        {"receipt_id": receipt_value, "actor_concept_id": actor_id}
    )
    if not isinstance(document, Mapping):
        return _error(
            "von_login_email_management_receipt_not_found",
            "No matching actor-owned login-email receipt was found.",
        )
    if str(document.get("status") or "") == "pending":
        document = _reconcile_pending_receipt(document)
    response = document.get("response_projection")
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "success": True,
        "governance_receipt": _receipt_projection(document),
        "response_projection": (
            dict(response) if isinstance(response, Mapping) else None
        ),
    }


__all__ = [
    "MUTATION_SCHEMA_VERSION",
    "READ_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "get_von_login_email_bindings",
    "get_von_login_email_management_receipt",
    "manage_von_login_email",
]
