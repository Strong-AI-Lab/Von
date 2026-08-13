"""Authenticated management surface for bounded ontology publication authority."""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Any

from flask import Blueprint, jsonify, request, session

from ...security.access_control import (
    can_access_concept,
    get_effective_organisation_concept_id,
)
from ...services.ontology_authority_role_migration_service import (
    ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
    OntologyAuthorityRoleMigrationError,
    apply_ontology_authority_role_migration,
    plan_ontology_authority_role_migration,
)
from ...services.ontology_authority_role_service import (
    grant_ontology_authority_role,
    list_manageable_ontology_authority_roles,
    revoke_ontology_authority_role,
)
from ...services.ontology_authority_vocabulary_service import (
    ONTOLOGY_AUTHORITY_VOCABULARY,
    bootstrap_first_semantic_authority,
)
from ...services.ontology_mutation_command_service import (
    OntologyMutationCommandError,
    build_ontology_mutation_intent,
    is_ontology_mutation_method,
    scope_read_back,
)
from ...services.ontology_publication_authority_service import (
    MAX_INTERACTIVE_DELEGATION_TTL_SECONDS,
    get_mutation_receipt_for_actor,
    issue_agent_delegation,
    list_actor_delegations,
    list_actor_semantic_authority,
    revoke_agent_delegation,
)
from ...services.ontology_scope_change_service import (
    change_concept_publication_scope,
)
from ...services.von_operational_administrator_service import (
    bind_von_operational_administrator_bootstrap,
    bootstrap_first_von_operational_administrator,
    grant_von_operational_administrator,
    revoke_von_operational_administrator,
    von_operational_administrator_read_back,
)
from ...services.window_session_context_service import (
    delete_all_window_contexts_owned_by,
)

ontology_authority_bp = Blueprint(
    "ontology_authority", __name__, url_prefix="/api/ontology-authority"
)


def _payload() -> Mapping[str, Any]:
    raw = request.get_json(silent=True)
    return raw if isinstance(raw, Mapping) else {}


def _authenticated_actor() -> str | None:
    """Return the session-authenticated actor for semantic-authority routes."""

    return _authenticated_session_actor()


def _authenticated_session_actor() -> str | None:
    """Return only login-session identity; legacy identity headers do not qualify."""

    actor_id = _clean(session.get("user_concept_id"))
    if not actor_id:
        return None
    return actor_id if actor_id.startswith("#") else f"#V#{actor_id}"


def _actor_required_response():
    return jsonify({"error": "authenticated_actor_context_required"}), 401


def _clean(value: object) -> str:
    return str(value or "").strip()


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _public_authority_error(exc: Exception, *, default: str) -> str:
    value = _clean(exc)
    return value if value and " " not in value else default


def _bootstrap_operator_authorised() -> bool:
    """Bootstrap is an explicit local operator action, never semantic authority."""

    if not _enabled(os.getenv("VON_ALLOW_ONTOLOGY_AUTHORITY_BOOTSTRAP")):
        return False
    expected = os.getenv("VON_ADMIN_TOKEN")
    provided = request.headers.get("X-Von-Admin-Token") or request.headers.get(
        "X-Admin-Token"
    )
    return bool(expected and provided and hmac.compare_digest(provided, expected))


def _intent_from_delegation_payload(payload: Mapping[str, Any]):
    method_name = _clean(payload.get("method_name"))
    arguments = payload.get("arguments")
    if not is_ontology_mutation_method(method_name):
        raise OntologyMutationCommandError(
            "ontology_mutation_method_not_governed",
            "Delegation is available only for a governed ontology mutation method.",
        )
    if not isinstance(arguments, Mapping):
        raise OntologyMutationCommandError(
            "ontology_mutation_arguments_required",
            "Delegation requires the exact governed method arguments.",
        )
    # The command builder derives actor, organisation, targets, and scope from
    # trusted server context and canonical reads.  Payload identities are unused.
    return build_ontology_mutation_intent(
        method_name=method_name,
        arguments=arguments,
        idempotency_key=_clean(payload.get("idempotency_key")) or None,
    )


@ontology_authority_bp.route("/me", methods=["GET"])
def current_semantic_authority():
    actor_id = _authenticated_actor()
    if not actor_id:
        return _actor_required_response()
    payload = list_actor_semantic_authority(actor_id)
    payload["vocabulary"] = ONTOLOGY_AUTHORITY_VOCABULARY
    return jsonify(payload)


@ontology_authority_bp.route("/operational-administrator/me", methods=["GET"])
def current_operational_administrator():
    """Read the caller's distinct operational role without exposing semantics."""

    actor_id = _authenticated_session_actor()
    if not actor_id:
        return _actor_required_response()
    return jsonify(von_operational_administrator_read_back(subject_concept_id=actor_id))


@ontology_authority_bp.route("/operational-administrators", methods=["POST"])
def grant_operational_administrator():
    """Use the dedicated lifecycle; generic ontology writes cannot grant it."""

    if not _authenticated_session_actor():
        return _actor_required_response()
    payload = _payload()
    try:
        if _bootstrap_operator_authorised():
            with bind_von_operational_administrator_bootstrap():
                result = bootstrap_first_von_operational_administrator(
                    subject_concept_id=_clean(payload.get("subject_concept_id")),
                )
        else:
            result = grant_von_operational_administrator(
                subject_concept_id=_clean(payload.get("subject_concept_id")),
                reason=_clean(payload.get("reason")) or None,
            )
    except ValueError as exc:
        return jsonify(
            {"error": "invalid_von_operational_administrator", "message": str(exc)}
        ), 400
    except PermissionError as exc:
        return jsonify(
            {
                "error": _public_authority_error(
                    exc,
                    default="von_operational_administrator_required",
                )
            }
        ), 403
    except RuntimeError:
        return jsonify({"error": "von_operational_administrator_read_back_failed"}), 503
    return jsonify(result), 201


@ontology_authority_bp.route("/operational-administrators/revoke", methods=["POST"])
def revoke_operational_administrator():
    if not _authenticated_session_actor():
        return _actor_required_response()
    payload = _payload()
    try:
        result = revoke_von_operational_administrator(
            subject_concept_id=_clean(payload.get("subject_concept_id")),
            reason=_clean(payload.get("reason")) or None,
        )
    except ValueError as exc:
        return jsonify(
            {"error": "invalid_von_operational_administrator", "message": str(exc)}
        ), 400
    except PermissionError as exc:
        return jsonify(
            {
                "error": _public_authority_error(
                    exc,
                    default="von_operational_administrator_required",
                )
            }
        ), 403
    except RuntimeError:
        return jsonify({"error": "von_operational_administrator_read_back_failed"}), 503
    return jsonify(result), 200


@ontology_authority_bp.route("/delegations", methods=["GET"])
def actor_delegations():
    actor_id = _authenticated_actor()
    if not actor_id:
        return _actor_required_response()
    include_inactive = _clean(
        request.args.get("include_inactive", "true")
    ).lower() not in {
        "0",
        "false",
        "no",
    }
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    return jsonify(
        {
            "delegations": list_actor_delegations(
                actor_concept_id=actor_id,
                include_inactive=include_inactive,
                limit=max(1, min(limit, 250)),
            )
        }
    )


@ontology_authority_bp.route("/delegations", methods=["POST"])
def issue_delegation():
    actor_id = _authenticated_actor()
    if not actor_id:
        return _actor_required_response()
    payload = _payload()
    try:
        intent = _intent_from_delegation_payload(payload)
        delegate_id = _clean(payload.get("delegate_concept_id"))
        audience = _clean(payload.get("audience"))
        effect_id = _clean(payload.get("effect_id"))
        if not delegate_id or not audience or not effect_id:
            return jsonify(
                {"error": "exact_delegate_audience_and_effect_required"}
            ), 400
        ttl_seconds = int(payload.get("ttl_seconds", 300))
        delegation = issue_agent_delegation(
            intent=intent,
            grantor_actor_concept_id=actor_id,
            grantor_organisation_concept_id=get_effective_organisation_concept_id(),
            delegate_concept_id=delegate_id,
            audience=audience,
            tool_name=intent.tool_name or _clean(payload.get("method_name")),
            effect_id=effect_id,
            ttl_seconds=ttl_seconds,
            turn_id=_clean(payload.get("turn_id")) or None,
            workflow_id=_clean(payload.get("workflow_id")) or None,
        )
    except OntologyMutationCommandError as exc:
        return jsonify({"error": exc.reason_code, "message": exc.public_message}), 400
    except ValueError as exc:
        return jsonify(
            {"error": "invalid_ontology_delegation", "message": str(exc)}
        ), 400
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503
    return jsonify(
        {
            "delegation": delegation,
            "max_ttl_seconds": MAX_INTERACTIVE_DELEGATION_TTL_SECONDS,
        }
    ), 201


@ontology_authority_bp.route("/delegations/<delegation_id>/revoke", methods=["POST"])
def revoke_delegation(delegation_id: str):
    actor_id = _authenticated_actor()
    if not actor_id:
        return _actor_required_response()
    try:
        delegation = revoke_agent_delegation(
            delegation_id=delegation_id,
            acting_actor_concept_id=actor_id,
            reason=_clean(_payload().get("reason")) or "revoked_by_actor",
        )
    except LookupError:
        return jsonify({"error": "ontology_delegation_not_found"}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503
    return jsonify({"delegation": delegation})


@ontology_authority_bp.route("/receipts/<receipt_id>", methods=["GET"])
def mutation_receipt(receipt_id: str):
    actor_id = _authenticated_actor()
    if not actor_id:
        return _actor_required_response()
    try:
        receipt = get_mutation_receipt_for_actor(
            receipt_id=receipt_id,
            actor_concept_id=actor_id,
        )
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503
    if receipt is None:
        # Do not disclose whether another actor owns this receipt.
        return jsonify({"error": "ontology_mutation_receipt_not_found"}), 404
    return jsonify({"receipt": receipt})


@ontology_authority_bp.route("/roles", methods=["GET"])
def manageable_roles():
    if not _authenticated_actor():
        return _actor_required_response()
    try:
        roles = list_manageable_ontology_authority_roles()
    except PermissionError as exc:
        return jsonify(
            {
                "error": _public_authority_error(
                    exc, default="ontology_authority_required"
                )
            }
        ), 403
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503
    return jsonify({"roles": roles})


@ontology_authority_bp.route("/roles", methods=["POST"])
def grant_role():
    if not _authenticated_actor():
        return _actor_required_response()
    payload = _payload()
    try:
        result = grant_ontology_authority_role(
            subject_concept_id=_clean(payload.get("subject_concept_id")),
            role=_clean(payload.get("role")),
            organisation_concept_id=(
                _clean(payload.get("organisation_concept_id")) or None
            ),
            request_id=_clean(payload.get("request_id")),
            reason=_clean(payload.get("reason")) or None,
        )
    except ValueError as exc:
        return jsonify(
            {"error": "invalid_ontology_authority_role", "message": str(exc)}
        ), 400
    except PermissionError as exc:
        return jsonify(
            {
                "error": _public_authority_error(
                    exc, default="ontology_authority_required"
                )
            }
        ), 403
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503
    status = 201 if result.get("success") else 403
    return jsonify(result), status


@ontology_authority_bp.route("/roles/revoke", methods=["POST"])
def revoke_role():
    if not _authenticated_actor():
        return _actor_required_response()
    payload = _payload()
    try:
        result = revoke_ontology_authority_role(
            subject_concept_id=_clean(payload.get("subject_concept_id")),
            role=_clean(payload.get("role")),
            organisation_concept_id=(
                _clean(payload.get("organisation_concept_id")) or None
            ),
            request_id=_clean(payload.get("request_id")),
            reason=_clean(payload.get("reason")) or None,
        )
    except ValueError as exc:
        return jsonify(
            {"error": "invalid_ontology_authority_role", "message": str(exc)}
        ), 400
    except PermissionError as exc:
        return jsonify(
            {
                "error": _public_authority_error(
                    exc, default="ontology_authority_required"
                )
            }
        ), 403
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503
    return jsonify(result), (200 if result.get("success") else 409)


@ontology_authority_bp.route("/concepts/<path:concept_id>/scope", methods=["GET"])
def concept_scope(concept_id: str):
    if not _authenticated_actor():
        return _actor_required_response()
    if not can_access_concept(concept_id):
        return jsonify({"error": "ontology_scope_target_not_found"}), 404
    try:
        return jsonify(scope_read_back(concept_id))
    except LookupError:
        return jsonify({"error": "ontology_scope_target_not_found"}), 404
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503


@ontology_authority_bp.route("/concepts/<path:concept_id>/scope", methods=["POST"])
def change_concept_scope(concept_id: str):
    if not _authenticated_actor():
        return _actor_required_response()
    payload = _payload()
    try:
        result = change_concept_publication_scope(
            concept_id=concept_id,
            destination_kind=_clean(payload.get("destination_kind")),
            destination_concept_id=(
                _clean(payload.get("destination_concept_id")) or None
            ),
            expected_scope_fingerprint=_clean(
                payload.get("expected_scope_fingerprint")
            ),
            request_id=_clean(payload.get("request_id")),
            preview=bool(payload.get("preview", True)),
            reason=_clean(payload.get("reason")) or None,
        )
    except ValueError as exc:
        return jsonify(
            {"error": "invalid_ontology_scope_change", "message": str(exc)}
        ), 400
    except PermissionError:
        return jsonify({"error": "ontology_scope_target_not_found"}), 404
    except LookupError:
        return jsonify({"error": "ontology_scope_target_not_found"}), 404
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503
    return jsonify(result), (200 if result.get("success") else 409)


@ontology_authority_bp.route("/bootstrap", methods=["POST"])
def bootstrap_authority_vocabulary():
    """Opt-in operational bootstrap for one explicit first represented role."""

    if not _bootstrap_operator_authorised():
        return jsonify({"error": "ontology_authority_bootstrap_not_authorised"}), 403
    payload = _payload()
    try:
        result = bootstrap_first_semantic_authority(
            subject_concept_id=_clean(payload.get("subject_concept_id")),
            role=_clean(payload.get("role")),
            organisation_concept_id=_clean(payload.get("organisation_concept_id"))
            or None,
        )
    except ValueError as exc:
        return jsonify(
            {"error": "invalid_ontology_authority_bootstrap", "message": str(exc)}
        ), 400
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 409
    except RuntimeError:
        return jsonify({"error": "ontology_authority_bootstrap_read_back_failed"}), 503
    return jsonify(result), 201


@ontology_authority_bp.route("/bootstrap/role-migration", methods=["GET", "POST"])
def migrate_initial_administrator_roles():
    """Dry-run or apply the fixed, inventory-bound initial role migration."""

    if not _bootstrap_operator_authorised():
        return jsonify({"error": "ontology_authority_bootstrap_not_authorised"}), 403
    if request.method == "GET":
        try:
            return jsonify(plan_ontology_authority_role_migration())
        except RuntimeError:
            return jsonify({"error": "ontology_authority_store_unavailable"}), 503

    payload = _payload()
    if _clean(payload.get("mode")).lower() != "apply":
        return jsonify({"error": "ontology_authority_role_migration_mode_required"}), 400
    # The target is fixed in server code. Client-supplied actor or subject fields
    # are ignored and can neither redirect nor broaden the migration.
    if _authenticated_session_actor() != ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET:
        return jsonify(
            {"error": "ontology_authority_role_migration_target_actor_required"}
        ), 403
    try:
        # Only this route binds the already-verified operator bootstrap control.
        # The migration and lifecycle services cannot self-authorise it.
        with bind_von_operational_administrator_bootstrap():
            result = apply_ontology_authority_role_migration(
                expected_inventory_fingerprint=_clean(
                    payload.get("expected_inventory_fingerprint")
                )
            )
        session.pop("role_in_org", None)
        result["derived_role_cache_invalidation"] = {
            "flask_session_role_cleared": True,
            "window_contexts_deleted": delete_all_window_contexts_owned_by(
                ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET
            ),
        }
    except OntologyAuthorityRoleMigrationError as exc:
        status = (
            403
            if exc.reason_code
            in {
                "ontology_authority_role_migration_target_actor_required",
                "ontology_authority_role_migration_human_actor_required",
            }
            else 409
        )
        response: dict[str, Any] = {
            "error": exc.reason_code,
            "message": exc.public_message,
        }
        if exc.report is not None:
            response["report"] = exc.report
        return jsonify(response), status
    except RuntimeError:
        return jsonify({"error": "ontology_authority_store_unavailable"}), 503
    return jsonify(result), 200
