"""Actor-bound discovery and mutation for organisation membership.

The generic ontology mutation surface deliberately reserves membership and
role predicates.  This service is their dedicated operational lifecycle: it
derives the actor from trusted server context, checks represented organisation
permissions, serialises authority-sensitive changes, records an actor-bound
idempotency receipt, and returns the canonical represented postcondition.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_organisation_membership_receipts_collection
from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import (
    LEGACY_IDENTITY_HEADER_ACTOR_SOURCE,
    bypass_access_control,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id_with_source,
)
from ..security.role_resolver import (
    AVAILABLE_ROLES,
    get_effective_permissions,
)
from ..vontology.utils_vontology import (
    get_concept_display_name_with_names_fallback,
    get_vontology_node_and_descendant_ids,
)
from .ontology_authority_membership_coordination_service import (
    ontology_authority_membership_mutation_barrier,
    organisation_membership_scope_barrier,
)
from .organisation_membership_service import (
    create_organisation_membership,
    get_organisation_members,
    get_user_memberships,
    remove_organisation_membership,
    resolve_user_organisation_membership,
    update_user_role,
)

DISCOVERY_SCHEMA_VERSION = "identity_organisation_options.v1"
RECEIPT_SCHEMA_VERSION = "organisation_membership_mutation_receipt.v1"
MUTATION_SCHEMA_VERSION = "organisation_membership_mutation.v1"
VON_USER_ORGANISATION_TYPE_ID = "#V#von_user_organisation"
_SUPPORTED_ACTIONS = {"add", "remove", "set_role"}

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _normalise_concept_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    if cleaned.startswith("#V#"):
        return cleaned
    return f"#V#{cleaned.removeprefix('#')}"


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
    effect_status: str = "not_started",
    mutation_outcome: str = "not_started",
    changed: bool | None = False,
    **details: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "schema_version": MUTATION_SCHEMA_VERSION,
        "success": False,
        "effect_status": effect_status,
        "mutation_outcome": mutation_outcome,
        "outcome_finality": (
            "terminal_not_started"
            if effect_status == "not_started"
            else "requires_canonical_reconciliation"
        ),
        "changed": changed,
        "error_code": code,
        "error": message,
    }
    response.update(details)
    return response


def _trusted_actor(
    acting_actor_concept_id: object = None,
) -> tuple[str | None, dict[str, Any] | None]:
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
        return None, _error(
            "client_supplied_identity_is_not_authority",
            "Client or model supplied identity cannot authorise organisation membership access.",
        )
    if not actor_id or source == LEGACY_IDENTITY_HEADER_ACTOR_SOURCE:
        return None, _error(
            "authenticated_actor_context_required",
            "Organisation membership access requires trusted actor provenance.",
        )
    if claimed_actor is not None and claimed_actor != actor_id:
        return None, _error(
            "actor_context_mismatch",
            "The supplied actor does not match the trusted server actor.",
        )
    return actor_id, None


def _relationship_values(concept: Mapping[str, Any], predicate: str) -> list[str]:
    relationships = concept.get("relationships")
    if not isinstance(relationships, Mapping):
        return []
    raw = relationships.get(predicate)
    values = raw if isinstance(raw, list) else [raw]
    return [
        normalised
        for value in values
        if (normalised := _normalise_concept_id(value)) is not None
    ]


def list_identity_organisation_options(
    *,
    acting_actor_concept_id: object = None,
) -> dict[str, Any]:
    """Return the complete represented organisation selector set for the actor."""

    actor_id, actor_error = _trusted_actor(acting_actor_concept_id)
    if actor_error is not None:
        return {
            **actor_error,
            "schema_version": DISCOVERY_SCHEMA_VERSION,
            "complete": False,
        }
    assert actor_id is not None

    represented = get_user_memberships(actor_id)
    with bypass_access_control():
        eligible_type_ids = set(
            get_vontology_node_and_descendant_ids(VON_USER_ORGANISATION_TYPE_ID) or []
        )
        organisations: list[dict[str, Any]] = []
        for membership in represented.get("memberships", []):
            if not isinstance(membership, Mapping):
                continue
            organisation_id = _normalise_concept_id(
                membership.get("organisation_concept_id")
            )
            if organisation_id is None:
                continue
            concept = ConceptsRepository.find_one({"concept_id": organisation_id})
            if not isinstance(concept, dict):
                # The relationship remains authoritative even if its target is
                # temporarily unavailable; expose that fact instead of silently
                # shortening an allegedly complete selector list.
                organisations.append(
                    {
                        "concept_id": organisation_id,
                        "name": organisation_id,
                        "role": str(membership.get("role") or "member"),
                        "selectable": True,
                        "target_resolved": False,
                        "is_von_user_organisation": False,
                    }
                )
                continue
            direct_types = set(_relationship_values(concept, "is_an_instance_of"))
            organisations.append(
                {
                    "concept_id": organisation_id,
                    "name": get_concept_display_name_with_names_fallback(concept),
                    "role": str(membership.get("role") or "member"),
                    "selectable": True,
                    "target_resolved": True,
                    "is_von_user_organisation": bool(
                        direct_types.intersection(eligible_type_ids)
                    ),
                }
            )

    current_organisation_id = _normalise_concept_id(
        get_effective_organisation_concept_id()
    )
    for organisation in organisations:
        organisation["is_current"] = (
            organisation["concept_id"] == current_organisation_id
        )
    return {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "success": True,
        "actor_concept_id": actor_id,
        "current_organisation_concept_id": current_organisation_id,
        "personal_option_available": True,
        "organisations": organisations,
        "total_count": len(organisations),
        "complete": True,
        "classification_policy": "descriptive_not_selector_gate",
    }


def _receipt_collection():
    collection = get_organisation_membership_receipts_collection()
    if collection is None:
        raise RuntimeError("organisation membership receipt store unavailable")
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
        "requested_role": document.get("requested_role"),
        "intent_fingerprint": document.get("intent_fingerprint"),
        "status": document.get("status"),
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
    role: str | None,
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
        "omr_" + hashlib.sha256(f"{actor_id}\x1f{request_id}".encode()).hexdigest()
    )
    document = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "actor_concept_id": actor_id,
        "request_id": request_id,
        "action": action,
        "user_concept_id": user_id,
        "organisation_concept_id": organisation_id,
        "requested_role": role,
        "reason": reason,
        "intent_fingerprint": intent_fingerprint,
        "status": "pending",
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
        raise RuntimeError(  # noqa: TRY004 - missing receipt, not invalid caller type
            "organisation membership receipt finalisation failed"
        )
    return dict(updated)


def _response_with_receipt(
    response: Mapping[str, Any], document: Mapping[str, Any]
) -> dict[str, Any]:
    return {**dict(response), "governance_receipt": _receipt_projection(document)}


def _replay_or_conflict(
    receipt: Mapping[str, Any], intent_fingerprint: str
) -> dict[str, Any] | None:
    if receipt.get("intent_fingerprint") != intent_fingerprint:
        return _response_with_receipt(
            _error(
                "idempotency_request_conflict",
                "This request_id is already bound to a different membership intent.",
            ),
            receipt,
        )
    status = str(receipt.get("status") or "pending")
    if status == "pending":
        return _response_with_receipt(
            _error(
                "organisation_membership_mutation_in_progress",
                "The matching membership effect is still in progress.",
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
            "organisation_membership_receipt_incomplete",
            "The matching receipt exists but has no response projection.",
            effect_status="indeterminate",
            mutation_outcome="indeterminate",
            changed=None,
        ),
        receipt,
    )


def _canonical_membership(user_id: str, organisation_id: str) -> dict[str, Any]:
    membership = resolve_user_organisation_membership(user_id, organisation_id)
    return {
        "user_concept_id": user_id,
        "organisation_concept_id": organisation_id,
        "membership_present": isinstance(membership, Mapping),
        "role": (
            str(membership.get("role") or "member")
            if isinstance(membership, Mapping)
            else None
        ),
    }


def _postcondition_holds(
    action: str,
    role: str | None,
    canonical_state: Mapping[str, Any],
) -> bool:
    present = canonical_state.get("membership_present") is True
    if action == "remove":
        return not present
    return present and canonical_state.get("role") == role


def _last_owner_reduction_denied(
    *,
    action: str,
    current_role: str | None,
    requested_role: str | None,
    organisation_id: str,
) -> bool:
    reducing_owner = current_role == "owner" and (
        action == "remove" or (action == "set_role" and requested_role != "owner")
    )
    if not reducing_owner:
        return False
    owners = get_organisation_members(organisation_id, role_filter="owner").get(
        "members", []
    )
    return len(owners) <= 1


def manage_organisation_membership(
    *,
    action: object,
    user_concept_id: object,
    organisation_concept_id: object,
    request_id: object,
    role: object = None,
    reason: object = None,
    acting_actor_concept_id: object = None,
) -> dict[str, Any]:
    """Execute one exact represented membership effect with a durable receipt."""

    actor_id, actor_error = _trusted_actor(acting_actor_concept_id)
    if actor_error is not None:
        return actor_error
    assert actor_id is not None

    action_value = _clean_text(action)
    user_id = _normalise_concept_id(user_concept_id)
    organisation_id = _normalise_concept_id(organisation_concept_id)
    request_value = _clean_text(request_id)
    reason_value = _clean_text(reason)
    role_value = _clean_text(role)
    if action_value not in _SUPPORTED_ACTIONS:
        return _error(
            "invalid_membership_action",
            "action must be one of add, remove, or set_role.",
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
        return _error(
            "invalid_reason",
            "reason must be at most 1000 characters.",
        )
    if action_value == "add" and role_value is None:
        role_value = "member"
    if action_value == "set_role" and role_value is None:
        return _error("role_required", "set_role requires an explicit role.")
    if action_value == "remove":
        role_value = None
    if role_value is not None and role_value not in AVAILABLE_ROLES:
        return _error(
            "invalid_organisation_role",
            "role must be viewer, member, contributor, admin, or owner.",
        )

    intent = {
        "action": action_value,
        "user_concept_id": user_id,
        "organisation_concept_id": organisation_id,
        "role": role_value,
    }
    intent_fingerprint = _sha256_json(intent)
    receipt, created = _begin_receipt(
        actor_id=actor_id,
        request_id=request_value,
        action=action_value,
        user_id=user_id,
        organisation_id=organisation_id,
        role=role_value,
        reason=reason_value,
        intent_fingerprint=intent_fingerprint,
    )
    if not created:
        replay = _replay_or_conflict(receipt, intent_fingerprint)
        assert replay is not None
        return replay

    receipt_id = str(receipt["receipt_id"])
    mutation_started = False
    authority_decision: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    try:
        with (
            ontology_authority_membership_mutation_barrier(),
            organisation_membership_scope_barrier(user_id, organisation_id),
        ):
            actor_membership = resolve_user_organisation_membership(
                actor_id,
                organisation_id,
            )
            actor_role = (
                str(actor_membership.get("role") or "member")
                if isinstance(actor_membership, Mapping)
                else None
            )
            actor_permissions = get_effective_permissions(actor_role or "")
            required_permissions = ["MANAGE_MEMBERS"]
            if action_value == "set_role" or (
                action_value == "add" and role_value != "member"
            ):
                required_permissions.append("MANAGE_ROLES")
            allowed = all(
                permission in actor_permissions for permission in required_permissions
            )
            authority_decision = {
                "allowed": allowed,
                "actor_concept_id": actor_id,
                "organisation_concept_id": organisation_id,
                "actor_role": actor_role,
                "required_permissions": required_permissions,
                "authority_source": "represented_organisation_membership_role",
                "semantic_ontology_authority_considered": False,
            }
            if not allowed:
                response = _error(
                    "organisation_membership_management_authority_required",
                    "The actor lacks the required operational membership permission for this organisation.",
                    authority_decision=authority_decision,
                )
                final = _finalise_receipt(
                    receipt_id,
                    status="denied",
                    mutation_outcome="not_started",
                    changed=False,
                    canonical_read_back=None,
                    authority_decision=authority_decision,
                    response=response,
                    error_code=str(response["error_code"]),
                )
                return _response_with_receipt(response, final)

            before = _canonical_membership(user_id, organisation_id)
            if (
                action_value == "add"
                and before["membership_present"]
                and before["role"] != role_value
            ):
                response = _error(
                    "membership_already_exists_with_different_role",
                    "The membership already exists with a different role; use set_role explicitly.",
                    canonical_read_back=before,
                    authority_decision=authority_decision,
                )
                final = _finalise_receipt(
                    receipt_id,
                    status="failed",
                    mutation_outcome="not_started",
                    changed=False,
                    canonical_read_back=before,
                    authority_decision=authority_decision,
                    response=response,
                    error_code=str(response["error_code"]),
                )
                return _response_with_receipt(response, final)
            if action_value == "set_role" and not before["membership_present"]:
                response = _error(
                    "organisation_membership_required",
                    "The user is not a member of the organisation.",
                    canonical_read_back=before,
                    authority_decision=authority_decision,
                )
                final = _finalise_receipt(
                    receipt_id,
                    status="failed",
                    mutation_outcome="not_started",
                    changed=False,
                    canonical_read_back=before,
                    authority_decision=authority_decision,
                    response=response,
                    error_code=str(response["error_code"]),
                )
                return _response_with_receipt(response, final)
            if _last_owner_reduction_denied(
                action=action_value,
                current_role=(
                    str(before["role"]) if before.get("role") is not None else None
                ),
                requested_role=role_value,
                organisation_id=organisation_id,
            ):
                response = _error(
                    "last_organisation_owner_required",
                    "The organisation's last owner cannot be removed or demoted.",
                    canonical_read_back=before,
                    authority_decision=authority_decision,
                )
                final = _finalise_receipt(
                    receipt_id,
                    status="denied",
                    mutation_outcome="not_started",
                    changed=False,
                    canonical_read_back=before,
                    authority_decision=authority_decision,
                    response=response,
                    error_code=str(response["error_code"]),
                )
                return _response_with_receipt(response, final)

            if not _postcondition_holds(action_value, role_value, before):
                mutation_started = True
                if action_value == "add":
                    create_organisation_membership(
                        user_id,
                        organisation_id,
                        role=role_value or "member",
                    )
                elif action_value == "remove":
                    remove_organisation_membership(user_id, organisation_id)
                else:
                    update_user_role(
                        user_id,
                        organisation_id,
                        role_value or "member",
                    )
            canonical = _canonical_membership(user_id, organisation_id)
            if not _postcondition_holds(action_value, role_value, canonical):
                raise RuntimeError("membership canonical read-back mismatch")

            changed = canonical != before
            response = {
                "schema_version": MUTATION_SCHEMA_VERSION,
                "success": True,
                "effect_status": "succeeded",
                "mutation_outcome": "succeeded",
                "outcome_finality": "terminal_canonical_read_back",
                "changed": changed,
                "action": action_value,
                "actor_concept_id": actor_id,
                "user_concept_id": user_id,
                "organisation_concept_id": organisation_id,
                "role": role_value,
                "authority_decision": authority_decision,
                "canonical_read_back": canonical,
            }
            final = _finalise_receipt(
                receipt_id,
                status="succeeded",
                mutation_outcome="succeeded",
                changed=changed,
                canonical_read_back=canonical,
                authority_decision=authority_decision,
                response=response,
            )
            return _response_with_receipt(response, final)
    except Exception as exc:  # noqa: BLE001 - reconcile after possible effect
        try:
            canonical = _canonical_membership(user_id, organisation_id)
        except Exception:  # noqa: BLE001 - receipt still records uncertainty
            canonical = None
        known_precondition_error = str(exc).strip() in {
            "organisation_ontology_authority_must_be_revoked_before_membership",
            "organisation_ontology_authority_revocation_not_verified",
        }
        postcondition_verified = bool(
            canonical is not None
            and _postcondition_holds(action_value, role_value, canonical)
        )
        if mutation_started and postcondition_verified:
            status = "succeeded"
            outcome = "succeeded"
            changed: bool | None = canonical != before
            response: dict[str, Any] = {
                "schema_version": MUTATION_SCHEMA_VERSION,
                "success": True,
                "effect_status": "succeeded",
                "mutation_outcome": "succeeded",
                "outcome_finality": "terminal_canonical_read_back",
                "changed": changed,
                "action": action_value,
                "actor_concept_id": actor_id,
                "user_concept_id": user_id,
                "organisation_concept_id": organisation_id,
                "role": role_value,
                "authority_decision": authority_decision,
                "canonical_read_back": canonical,
                "reconciled_after_exception": type(exc).__name__,
            }
            error_code = None
        else:
            effect_may_have_crossed = mutation_started and not known_precondition_error
            status = "indeterminate" if effect_may_have_crossed else "failed"
            outcome = "indeterminate" if effect_may_have_crossed else "not_started"
            changed = None if effect_may_have_crossed else False
            known_error = str(exc).strip()
            error_code = (
                known_error
                if known_error
                in {
                    "organisation_ontology_authority_must_be_revoked_before_membership",
                    "organisation_ontology_authority_revocation_not_verified",
                    "ontology_mutation_resource_busy",
                }
                else "organisation_membership_mutation_failed"
            )
            response = _error(
                error_code,
                (
                    "The membership effect may have started and requires canonical reconciliation."
                    if effect_may_have_crossed
                    else "The membership effect did not start."
                ),
                effect_status=(
                    "indeterminate" if effect_may_have_crossed else "not_started"
                ),
                mutation_outcome=outcome,
                changed=changed,
                authority_decision=authority_decision,
                canonical_read_back=canonical,
                exception_type=type(exc).__name__,
            )
        try:
            final = _finalise_receipt(
                receipt_id,
                status=status,
                mutation_outcome=outcome,
                changed=changed,
                canonical_read_back=canonical,
                authority_decision=authority_decision,
                response=response,
                error_code=error_code,
            )
        except Exception as receipt_exc:  # noqa: BLE001 - effect already crossed
            return _response_with_receipt(
                {
                    **response,
                    "receipt_finalisation": "pending_reconciliation",
                    "receipt_finalisation_error": type(receipt_exc).__name__,
                    "outcome_finality": (
                        "canonical_effect_verified_receipt_pending"
                        if response.get("success") is True
                        else "requires_receipt_and_canonical_reconciliation"
                    ),
                },
                receipt,
            )
        return _response_with_receipt(response, final)


def get_organisation_membership_receipt(
    *,
    receipt_id: object,
    acting_actor_concept_id: object = None,
) -> dict[str, Any]:
    """Read one durable membership receipt, confined to its trusted actor."""

    actor_id, actor_error = _trusted_actor(acting_actor_concept_id)
    if actor_error is not None:
        return actor_error
    assert actor_id is not None
    receipt_value = _clean_text(receipt_id)
    if receipt_value is None:
        return _error("receipt_id_required", "receipt_id is required.")
    document = _receipt_collection().find_one(
        {"receipt_id": receipt_value, "actor_concept_id": actor_id}
    )
    if not isinstance(document, Mapping):
        return _error(
            "organisation_membership_receipt_not_found",
            "No actor-visible organisation membership receipt was found.",
        )
    if str(document.get("status") or "") == "pending":
        user_id = _normalise_concept_id(document.get("user_concept_id"))
        organisation_id = _normalise_concept_id(document.get("organisation_concept_id"))
        action = _clean_text(document.get("action"))
        role = _clean_text(document.get("requested_role"))
        if user_id and organisation_id and action in _SUPPORTED_ACTIONS:
            try:
                with (
                    ontology_authority_membership_mutation_barrier(),
                    organisation_membership_scope_barrier(user_id, organisation_id),
                ):
                    canonical = _canonical_membership(user_id, organisation_id)
                postcondition_verified = _postcondition_holds(
                    action,
                    role,
                    canonical,
                )
                reconciled_response = {
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
                    "role": role,
                    "canonical_read_back": canonical,
                    "reconciled_from_pending_receipt": True,
                }
                document = _finalise_receipt(
                    receipt_value,
                    status=("succeeded" if postcondition_verified else "indeterminate"),
                    mutation_outcome=(
                        "succeeded" if postcondition_verified else "indeterminate"
                    ),
                    changed=None,
                    canonical_read_back=canonical,
                    authority_decision=None,
                    response=reconciled_response,
                    error_code=(
                        None
                        if postcondition_verified
                        else "membership_postcondition_not_verified"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - preserve durable pending state
                logger.warning(
                    "Could not reconcile pending membership receipt %s: %s",
                    receipt_value,
                    type(exc).__name__,
                )
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
    "get_organisation_membership_receipt",
    "list_identity_organisation_options",
    "manage_organisation_membership",
]
