from flask import Blueprint, request, jsonify, current_app, session
from flask.typing import ResponseReturnValue
import json
from datetime import datetime
import datetime as dt
from typing import Any
from ...services import concept_service  # Import concept_service
from ...services.window_session_context_service import get_effective_context
from ...services.concept_service import (
    ConceptServiceError,
    InvalidConceptDataError,
    ConceptNotFoundError,
    import_concepts as import_concepts_service,
)
from ...services.text_value_service import (
    upsert_text_for_concept,
    get_texts_for_concept,
    RelationPredicate,
    update_text_relation_text,
    delete_text_relation,
)
from ...services.text_relation_predicate_validation_service import (
    TextRelationPredicateResolutionError,
    resolve_text_relation_predicate_for_write,
)
from ...services.rag_text_relation_change_hook_service import (
    maybe_delete_text_relation_doc_from_rag,
    maybe_sync_concept_text_relations_to_rag,
)
from ...services.ontology_mutation_command_service import (
    OntologyMutationCommandError,
    delete_legacy_name,
)
from ...services.paper_recommendation_profile_vontology_service import (
    load_paper_recommendation_profile,
    upsert_paper_recommendation_profile,
)
from ...services.paper_recommendation_workflow_vontology_service import (
    request_paper_recommendation_refresh,
)
from ...services.concept_summary_renderer_service import (
    load_concept_summary_renderer,
)
from ...services.concept_effective_assertion_service import (
    DEFAULT_PAGE_LIMIT as DEFAULT_EFFECTIVE_ASSERTION_PAGE_LIMIT,
    load_effective_assertion_by_id,
    load_concept_effective_assertions,
)
from ...vontology.utils_vontology import (
    get_concept_notes,
)  # Added getter import
from ...db.mongo_client import get_db

concept_bp = Blueprint("concepts", __name__)  # Define blueprint


def _get_trusted_interaction_user() -> str | None:
    """Return authenticated interaction actor; legacy identity headers are not authority."""

    from ...security.access_control import (
        LEGACY_IDENTITY_HEADER_ACTOR_SOURCE,
        get_effective_user_concept_id_with_source,
    )

    user_id, actor_source = get_effective_user_concept_id_with_source()
    if actor_source == LEGACY_IDENTITY_HEADER_ACTOR_SOURCE:
        return None
    return user_id


def _governed_mutation_response(
    result: dict[str, Any], *, success_status: int = 200
) -> ResponseReturnValue:
    """Render a shared governed-command outcome without recreating policy here."""

    if result.get("success") is not False and not result.get("error_code"):
        return jsonify(result), success_status

    error_code = str(result.get("error_code") or "")
    authority_denials = {
        "authenticated_actor_context_required",
        "client_supplied_identity_is_not_authority",
        "von_operator_is_not_semantic_ontology_authority",
        "global_ontology_admin_authority_required",
        "organisation_ontology_admin_authority_required",
        "ontology_publication_authority_required",
        "private_scope_canonical_publication_not_delegated",
        "dedicated_ontology_governance_operation_required",
        "explicit_scope_adoption_required",
        "ontology_mutation_target_not_accessible",
    }
    authority_decision = result.get("authority_decision")
    decision_denied = isinstance(authority_decision, dict) and (
        authority_decision.get("allowed") is False
    )
    if error_code in authority_denials or decision_denied:
        return jsonify(result), 403
    if error_code.endswith("not_found"):
        return jsonify(result), 404
    if error_code in {
        "ontology_authority_store_unavailable",
        "ontology_mutation_receipt_store_unavailable",
        "ontology_mutation_store_unavailable",
        "ontology_mutation_receipt_finalisation_failed",
        "ontology_mutation_postcondition_failed",
        "canonical_read_back_failed",
        "ontology_mutation_replay_projection_unavailable",
    }:
        return jsonify(result), 503
    if error_code in {
        "legacy_name_snapshot_precondition_failed",
        "legacy_name_selector_precondition_failed",
        "legacy_name_compare_and_set_failed",
        "canonical_has_name_precondition_failed",
    }:
        return jsonify(result), 409
    return jsonify(result), 400


def _execute_governed_http_mutation(
    *,
    method_name: str,
    arguments: dict[str, Any],
    mutate: Any,
    preview: bool = False,
) -> dict[str, Any]:
    """Use the canonical command boundary with Flask's trusted actor context."""

    from ...services.ontology_mutation_command_service import (
        execute_governed_ontology_method,
    )

    try:
        return execute_governed_ontology_method(
            method_name=method_name,
            arguments=arguments,
            mutate=mutate,
            preview=preview,
        )
    except LookupError:
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": "ontology_mutation_target_not_found",
            "error": "The requested ontology target was not found.",
        }
    except (ConnectionError, TimeoutError):
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": "ontology_mutation_store_unavailable",
            "error": "The ontology mutation store is temporarily unavailable.",
        }


def _get_request_namespace() -> str | None:
    window_session_id = request.headers.get("X-Von-Window-Session")
    user_concept_id = session.get("user_concept_id")
    effective = get_effective_context(window_session_id, dict(session), user_concept_id)
    effective_namespace = effective.get("namespace")
    if (
        isinstance(effective_namespace, str)
        and effective_namespace.strip()
        and effective_namespace.strip().startswith("#V#")
    ):
        return effective_namespace.strip()

    session_namespace = session.get("namespace")
    if (
        isinstance(session_namespace, str)
        and session_namespace.strip()
        and session_namespace.strip().startswith("#V#")
    ):
        return session_namespace.strip()

    user_concept_id = session.get("user_concept_id")
    if (
        isinstance(user_concept_id, str)
        and user_concept_id.strip()
        and user_concept_id.strip().startswith("#V#")
    ):
        return user_concept_id.strip()

    return None


def _get_effective_request_context() -> dict[str, Any]:
    window_session_id = request.headers.get("X-Von-Window-Session")
    user_concept_id = session.get("user_concept_id")
    effective = get_effective_context(window_session_id, dict(session), user_concept_id)
    return dict(effective) if isinstance(effective, dict) else {}


def _normalise_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned}"


def _get_current_user_concept_id() -> str | None:
    effective = _get_effective_request_context()
    return _normalise_concept_id(effective.get("user_id")) or _normalise_concept_id(
        session.get("user_concept_id")
    )


def _get_current_org_concept_id() -> str | None:
    effective = _get_effective_request_context()
    return _normalise_concept_id(
        effective.get("organisation_id")
    ) or _normalise_concept_id(session.get("organisation_concept_id"))


def _is_admin_or_owner_session() -> bool:
    effective = _get_effective_request_context()
    role = effective.get("role") or session.get("role_in_org")
    return isinstance(role, str) and role.strip().lower() in {"admin", "owner"}


def _can_rename_concept_id() -> bool:
    """Return whether this request may perform concept-ID rename previews/execution."""

    return _is_admin_or_owner_session()


def _coerce_json_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _can_edit_subject_recommendation_profile(subject_concept_id: str) -> bool:
    subject_id = _normalise_concept_id(subject_concept_id)
    if not subject_id:
        return False
    current_user_id = _get_current_user_concept_id()
    current_org_id = _get_current_org_concept_id()
    if current_user_id == subject_id:
        return True
    if current_org_id == subject_id:
        return True
    return _is_admin_or_owner_session()


@concept_bp.route("/<string:concept_id>/paper_recommendation_profile", methods=["GET"])
def get_concept_recommendation_profile(concept_id: str) -> ResponseReturnValue:
    """Return a subject-owned paper recommendation profile for one concept."""

    try:
        payload = load_paper_recommendation_profile(
            subject_concept_id=concept_id,
            create_if_missing=False,
        )
        payload["permissions"] = {
            "can_edit": _can_edit_subject_recommendation_profile(concept_id),
        }
        return jsonify(payload), 200
    except ConceptNotFoundError:
        return jsonify({"error": "Concept not found"}), 404
    except Exception as e:
        current_app.logger.error(
            "Failed to load paper recommendation profile for %s: %s",
            concept_id,
            e,
            exc_info=True,
        )
        return jsonify({"error": "Failed to load paper recommendation profile"}), 500


@concept_bp.route("/<string:concept_id>/paper_recommendation_profile", methods=["POST"])
def set_concept_recommendation_profile(concept_id: str) -> ResponseReturnValue:
    """Persist a subject-owned paper recommendation profile for one concept."""

    if not _can_edit_subject_recommendation_profile(concept_id):
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object body required"}), 400

    current_user_id = _get_current_user_concept_id()

    try:
        payload = upsert_paper_recommendation_profile(
            subject_concept_id=concept_id,
            recommendation_profile=data,
            provenance={"source": "concept_routes.paper_recommendation_profile"},
            context={"path": "concept_routes.paper_recommendation_profile"},
        )
        payload["permissions"] = {"can_edit": True}
        payload["recommendation_refresh"] = request_paper_recommendation_refresh(
            target_subject_concept_ids=[concept_id],
            trigger_source="concept_routes.paper_recommendation_profile",
            user_id=current_user_id or concept_id,
            event_payload={
                "subject_concept_id": concept_id,
                "source": "concept_routes.paper_recommendation_profile",
            },
        )
        return jsonify(payload), 200
    except ConceptNotFoundError:
        return jsonify({"error": "Concept not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        current_app.logger.error(
            "Failed to save paper recommendation profile for %s: %s",
            concept_id,
            e,
            exc_info=True,
        )
        return jsonify({"error": "Failed to save paper recommendation profile"}), 500


@concept_bp.route("/<string:concept_id>/summary_renderer", methods=["GET"])
def get_concept_summary_renderer(concept_id: str) -> ResponseReturnValue:
    """Return the concept-page summary renderer payload for one concept."""

    try:
        actor_user_id = _get_current_user_concept_id()
        payload = load_concept_summary_renderer(
            concept_id,
            actor_user_id=actor_user_id,
            actor_namespace=_get_request_namespace() if actor_user_id else None,
            organisation_concept_id=(
                _get_current_org_concept_id() if actor_user_id else None
            ),
        )
        return jsonify(payload), 200
    except ConceptNotFoundError:
        return jsonify({"error": "Concept not found"}), 404
    except Exception as e:
        current_app.logger.error(
            "Failed to load concept summary renderer for %s: %s",
            concept_id,
            e,
            exc_info=True,
        )
        return jsonify({"error": "Failed to load concept summary renderer"}), 500


@concept_bp.route("/<string:concept_id>/effective_assertions", methods=["GET"])
def get_concept_effective_assertions(concept_id: str) -> ResponseReturnValue:
    """Return one bounded actor-effective assertion page for a concept.

    The request cannot nominate a user, organisation, namespace, or scope. The
    scoped-assertion service derives visibility only from trusted ambient
    request context. Anonymous callers receive the base-only empty projection,
    without learning whether private assertions exist.
    """

    current_user_id = _get_current_user_concept_id()
    if not current_user_id:
        return (
            jsonify(
                {
                    "success": True,
                    "concept_id": concept_id,
                    "context_view": "base_publication",
                    "items": [],
                    "returned": 0,
                    "offset": 0,
                    "limit": DEFAULT_EFFECTIVE_ASSERTION_PAGE_LIMIT,
                    "has_more": False,
                    "next_offset": None,
                    "truncated": False,
                    "counts_are_lower_bounds": False,
                }
            ),
            200,
        )

    try:
        payload = load_concept_effective_assertions(
            concept_id=concept_id,
            limit=request.args.get(
                "limit",
                DEFAULT_EFFECTIVE_ASSERTION_PAGE_LIMIT,
            ),
            offset=request.args.get("offset", 0),
        )
        return jsonify(payload), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        current_app.logger.warning(
            "Actor-effective assertions unavailable for %s: %s",
            concept_id,
            exc,
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "error": "Actor-effective assertions are temporarily unavailable",
                    "error_code": "actor_effective_assertions_unavailable",
                    "retryable": True,
                }
            ),
            503,
        )
    except Exception as exc:
        current_app.logger.error(
            "Failed to load actor-effective assertions for %s: %s",
            concept_id,
            exc,
            exc_info=True,
        )
        return jsonify({"error": "Failed to load actor-effective assertions"}), 500


@concept_bp.route("/assertions/<string:assertion_id>", methods=["GET"])
def get_effective_assertion_by_id(assertion_id: str) -> ResponseReturnValue:
    """Return one exact assertion without disclosing inaccessible IDs."""

    if not _get_current_user_concept_id():
        return jsonify({"error": "assertion_not_found"}), 404
    try:
        payload = load_effective_assertion_by_id(assertion_id)
        if payload is None:
            return jsonify({"error": "assertion_not_found"}), 404
        return jsonify(payload), 200
    except (PermissionError, ValueError):
        return jsonify({"error": "assertion_not_found"}), 404
    except RuntimeError as exc:
        current_app.logger.warning(
            "Exact actor-effective assertion unavailable for %s: %s",
            assertion_id,
            exc,
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "error": "Assertion is temporarily unavailable",
                    "error_code": "actor_effective_assertion_unavailable",
                    "retryable": True,
                }
            ),
            503,
        )
    except Exception as exc:
        current_app.logger.error(
            "Failed to load exact actor-effective assertion %s: %s",
            assertion_id,
            exc,
            exc_info=True,
        )
        return jsonify({"error": "Failed to load assertion"}), 500


@concept_bp.route("/", methods=["GET"])
def list_concepts_route():
    """
    API endpoint to list concepts.
    Can be filtered by 'vontology_path' or 'concept_id' query parameters.
    If 'concept_id' is provided, the service layer will also fetch concepts of all descendant concepts.
    """
    try:
        vontology_path = request.args.get("vontology_path")
        concept_id = request.args.get("concept_id")
        search_term = request.args.get("search_term")
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)
        sort_by = request.args.get("sort_by", "updated_at")
        sort_order_str = request.args.get("sort_order", "desc")
        sort_order = -1 if sort_order_str.lower() == "desc" else 1

        if page <= 0:
            return jsonify(error="Page number must be positive"), 400
        if per_page <= 0:
            return jsonify(error="Per_page value must be positive"), 400

        concepts, total_count = concept_service.list_concepts(
            vontology_path=vontology_path,
            concept_id=concept_id,  # Pass the single concept_id directly to the service
            search_term=search_term,
            page=page,
            per_page=per_page,
            sort_by=sort_by,
            sort_order=sort_order,
        )

        return (
            jsonify(
                {
                    "concepts": concepts,
                    "total_count": total_count,
                    "page": page,
                    "per_page": per_page,
                }
            ),
            200,
        )

    except InvalidConceptDataError as e:
        current_app.logger.warning(f"Invalid data for listing concepts: {e}")
        return jsonify(error=str(e)), 400
    except ConceptServiceError as e:
        current_app.logger.error(f"Service error listing concepts: {e}", exc_info=True)
        return jsonify(error=str(e)), 500
    except ValueError as e:
        current_app.logger.warning(f"Invalid parameter value: {e}")
        return jsonify(error=f"Invalid parameter value: {e}"), 400
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error listing concepts: {e}", exc_info=True
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/", methods=["POST"])
def create_concept_route():
    """API endpoint to create a new concept."""
    data = request.get_json()
    if not data:
        return jsonify(error="Request body must be JSON"), 400

    name = data.get("name")
    kind = data.get("kind")
    concept_id = data.get("concept_id")
    vontology_path = data.get("vontology_path")
    description = data.get("description")
    notes = data.get("notes")
    attributes = data.get("attributes")
    system_tags = data.get("system_tags")
    user_tags = data.get("user_tags")
    linked_concepts = data.get("linked_concepts")
    parent_concept_ids = data.get("parent_concept_ids")

    if not name:
        return jsonify(error="concept name is required"), 400
    if parent_concept_ids is not None and not isinstance(parent_concept_ids, list):
        return jsonify(error="parent_concept_ids must be a list"), 400

    try:
        trusted_actor = _get_current_user_concept_id()
        trusted_org = _get_current_org_concept_id()
        scope_mode = data.get("visibility_scope_mode") or data.get("scope_mode")
        from ...services.ontology_mutation_command_service import (
            resolve_governed_ontology_arguments,
        )

        create_arguments = resolve_governed_ontology_arguments(
            "create_concepts",
            {
                "concepts": [
                    {
                        "concept_id": concept_id,
                        "name": name,
                        "kind": kind,
                        "description": description,
                        "notes": notes,
                        "attributes": attributes,
                        "system_tags": system_tags,
                        "user_tags": user_tags,
                        "linked_concepts": linked_concepts,
                        "instance_of_type": data.get("instance_of_type"),
                        "vontology_path": vontology_path,
                    }
                ],
                "scope_mode": scope_mode,
                "parent_id": (
                    (parent_concept_ids or [None])[0]
                    if isinstance(parent_concept_ids, list)
                    else None
                ),
                "parent_concept_ids": parent_concept_ids,
                "instance_of_type": data.get("instance_of_type"),
                "linked_concepts": linked_concepts,
                "attributes": attributes,
                "system_tags": system_tags,
                "user_tags": user_tags,
                "vontology_path": vontology_path,
                "description": description,
                "notes": notes,
                "request_id": data.get("request_id"),
            },
        )
        resolved_concept = create_arguments["concepts"][0]
        resolved_parent_ids = create_arguments.get("parent_concept_ids")
        resolved_kind = resolved_concept.get("kind")
        result = _execute_governed_http_mutation(
            method_name="create_concepts",
            arguments=create_arguments,
            mutate=lambda: concept_service.create_concept(
                name=resolved_concept.get("name"),
                concept_id=resolved_concept.get("concept_id"),
                vontology_path=resolved_concept.get("vontology_path"),
                description=resolved_concept.get("description"),
                notes=resolved_concept.get("notes"),
                attributes=resolved_concept.get("attributes"),
                system_tags=resolved_concept.get("system_tags"),
                user_tags=resolved_concept.get("user_tags"),
                linked_concepts=resolved_concept.get("linked_concepts"),
                parent_concept_ids=resolved_parent_ids,
                create_as_instance=resolved_kind in {"instance", "predicate"},
                instance_of_type=resolved_concept.get("instance_of_type"),
                created_by_concept_id=trusted_actor,
                organisation_concept_id=trusted_org,
                event_namespace=_get_request_namespace(),
                visibility_scope_mode=create_arguments.get("scope_mode"),
                maintain_relationship_inverses=False,
                resolve_visibility_from_event_namespace=False,
            ),
        )
        return _governed_mutation_response(result, success_status=201)
    except OntologyMutationCommandError as e:
        return (
            jsonify(
                {
                    "success": False,
                    "effect_status": "not_started",
                    "mutation_outcome": "not_started",
                    "changed": False,
                    "error_code": e.reason_code,
                    "error": e.public_message,
                }
            ),
            400,
        )
    except InvalidConceptDataError as e:
        current_app.logger.warning(f"Invalid data for creating concept: {e}")
        return jsonify(error=str(e)), 400
    except ConceptServiceError as e:
        current_app.logger.error(f"Service error creating concept: {e}", exc_info=True)
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error creating concept: {e}", exc_info=True
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>", methods=["GET", "PUT", "DELETE"])
def single_concept_route(concept_id: str) -> ResponseReturnValue:
    """
    API endpoint to get, update, or delete a single concept by its ID.
    """
    if request.method == "GET":
        try:
            # Accept either a MongoDB ObjectId or an ontological concept_id (e.g., '#V#person')
            if concept_id.startswith("#V#"):
                concept = concept_service.get_concept_by_concept_id(concept_id)
            else:
                concept = concept_service.get_concept_by_id(concept_id)
            # Ensure notes field uses proper getter function for consistency
            if concept:
                concept["notes"] = get_concept_notes(concept)

                # Use shared, non-mutating enrichment logic for names and
                # descriptions.
                from ...services.concept_service import (
                    enrich_concept_with_text_relations,
                )

                concept = enrich_concept_with_text_relations(
                    concept, logger=current_app.logger
                )

                # Fetch description from text relations (authoritative source)
                descriptions_from_relations = get_texts_for_concept(
                    subject_concept_id=concept.get("concept_id", concept_id),
                    predicate="hasDescription",
                    limit=10,
                )
                if descriptions_from_relations:
                    # Use the first description (typically there's only one)
                    concept["description"] = descriptions_from_relations[0].get(
                        "text", ""
                    )
                # When no relation exists, retain any readable legacy description
                # in the response. Migration is an explicit maintenance action.

            return jsonify(concept), 200
        except ConceptNotFoundError as e:
            if concept_id.startswith("#V#"):
                try:
                    from ...security.access_control import describe_concept_access

                    access = describe_concept_access(concept_id)
                    if (
                        access.get("exists") is True
                        and access.get("accessible") is False
                    ):
                        current_app.logger.info(
                            "concept access denied (ID: %s): %s", concept_id, access
                        )
                        return (
                            jsonify(
                                error=(
                                    f"Concept '{concept_id}' is not accessible in the current context."
                                ),
                                error_code="access_denied",
                                access=access,
                            ),
                            403,
                        )
                except Exception:
                    pass
            current_app.logger.info(f"concept not found (ID: {concept_id}): {e}")
            return jsonify(error=str(e)), 404
        except (
            InvalidConceptDataError
        ) as e:  # Should not happen for GET by ID if ID format is validated by service
            current_app.logger.warning(
                f"Invalid data for getting concept (ID: {concept_id}): {e}"
            )
            return jsonify(error=str(e)), 400
        except ConceptServiceError as e:
            current_app.logger.error(
                f"Service error getting concept {concept_id}: {e}", exc_info=True
            )
            return jsonify(error=str(e)), 500
        except Exception as e:
            current_app.logger.error(
                f"Unexpected error getting concept {concept_id}: {e}", exc_info=True
            )
            return jsonify(error="An unexpected error occurred"), 500

    elif request.method == "PUT":
        # NOTE: Names are stored in TEXT RELATIONS (text_relations collection), NOT in the concept document.
        # To update names, use the dedicated /api/concepts/<id>/texts endpoints (POST/PATCH/DELETE).
        # Attempting to update a 'names' field via PUT will not persist, as names are not stored in concepts.
        data = request.get_json()
        if not data:
            return jsonify(error="Request body must be JSON and non-empty"), 400
        try:
            # Check if this is a notes-only or description-only update and handle it properly
            if "notes" in data and len(data) == 1:
                # This is a notes-only update, use the proper notes update function
                governed = _execute_governed_http_mutation(
                    method_name="upsert_singleton_text_relation",
                    arguments={
                        "concept_id": concept_id,
                        "predicate": "hasNote",
                        "text": data["notes"],
                    },
                    mutate=lambda: {
                        "success": bool(
                            concept_service.update_concept_notes(
                                concept_id, data["notes"]
                            )
                        )
                    },
                )
                if governed.get("success") is False:
                    return _governed_mutation_response(governed)
                success = True
                if success:
                    # Return the updated concept with proper notes access
                    if concept_id.startswith("#V#"):
                        updated_concept = concept_service.get_concept_by_concept_id(
                            concept_id
                        )
                    else:
                        updated_concept = concept_service.get_concept_by_id(concept_id)
                    if updated_concept:
                        updated_concept["notes"] = get_concept_notes(updated_concept)
                        return jsonify(updated_concept), 200
                    else:
                        return jsonify(error="Updated concept not found"), 500
                else:
                    return jsonify(error="Failed to update notes"), 500
            elif "description" in data and len(data) == 1:
                governed = _execute_governed_http_mutation(
                    method_name="upsert_singleton_text_relation",
                    arguments={
                        "concept_id": concept_id,
                        "predicate": "hasDescription",
                        "text": data["description"],
                    },
                    mutate=lambda: {
                        "success": bool(
                            concept_service.update_concept_description(
                                concept_id, data["description"]
                            )
                        )
                    },
                )
                if governed.get("success") is False:
                    return _governed_mutation_response(governed)
                success = True
                if success:
                    if concept_id.startswith("#V#"):
                        updated_concept = concept_service.get_concept_by_concept_id(
                            concept_id
                        )
                    else:
                        updated_concept = concept_service.get_concept_by_id(concept_id)
                    if updated_concept:
                        if "_id" in updated_concept:
                            updated_concept["id"] = str(updated_concept.pop("_id"))
                        return jsonify(updated_concept), 200
                    return jsonify(error="Updated concept not found"), 500
                else:
                    return jsonify(error="Failed to update description"), 500
            else:
                # For other updates, use the generic function but fix notes field after
                governed = _execute_governed_http_mutation(
                    method_name="update_concept",
                    arguments={"concept_id": concept_id, "update_data": data},
                    mutate=lambda: {
                        "success": True,
                        "updated_concept": concept_service.update_concept(
                            concept_id, data
                        ),
                    },
                )
                if governed.get("success") is False:
                    return _governed_mutation_response(governed)
                updated_concept = governed.get("updated_concept")
                # Ensure notes field uses proper getter function for consistency
                if updated_concept and "notes" in updated_concept:
                    updated_concept["notes"] = get_concept_notes(updated_concept)
                return jsonify(updated_concept), 200
        except ConceptNotFoundError as e:
            current_app.logger.info(
                f"concept not found for update (ID: {concept_id}): {e}"
            )
            return jsonify(error=str(e)), 404
        except InvalidConceptDataError as e:
            current_app.logger.warning(
                f"Invalid data for updating concept (ID: {concept_id}): {e}"
            )
            return jsonify(error=str(e)), 400
        except ConceptServiceError as e:
            current_app.logger.error(
                f"Service error updating concept {concept_id}: {e}", exc_info=True
            )
            return jsonify(error=str(e)), 500
        except Exception as e:
            current_app.logger.error(
                f"Unexpected error updating concept {concept_id}: {e}", exc_info=True
            )
            return jsonify(error="An unexpected error occurred"), 500

    elif request.method == "DELETE":
        try:
            governed = _execute_governed_http_mutation(
                method_name="delete_concept",
                arguments={"concept_id": concept_id, "simulate": False},
                mutate=lambda: {
                    "success": bool(concept_service.delete_concept(concept_id))
                },
            )
            if governed.get("success") is False:
                return _governed_mutation_response(governed)
            success = True
            if success:
                return (
                    jsonify(message="concept deleted successfully"),
                    200,
                )  # Or 204 No Content
            else:
                # This case should ideally be covered by ConceptNotFoundError if service handles it
                current_app.logger.warning(
                    f"concept not found for deletion or delete failed (ID: {concept_id})"
                )
                return (
                    jsonify(error="concept not found or delete operation failed"),
                    404,
                )
        except ConceptNotFoundError as e:
            current_app.logger.info(
                f"concept not found for deletion (ID: {concept_id}): {e}"
            )
            return jsonify(error=str(e)), 404
        except (
            InvalidConceptDataError
        ) as e:  # Should not happen for DELETE by ID if ID format is validated
            current_app.logger.warning(
                f"Invalid data for deleting concept (ID: {concept_id}): {e}"
            )
            return jsonify(error=str(e)), 400
        except ConceptServiceError as e:
            current_app.logger.error(
                f"Service error deleting concept {concept_id}: {e}", exc_info=True
            )
            return jsonify(error=str(e)), 500
        except Exception as e:
            current_app.logger.error(
                f"Unexpected error deleting concept {concept_id}: {e}", exc_info=True
            )
            return jsonify(error="An unexpected error occurred"), 500

    # Fallback for static analyzers to ensure all code paths return a value
    return jsonify(error="Method not allowed"), 405


@concept_bp.route("/export", methods=["GET"])
def export_concepts_route():
    """
    API endpoint to export concepts in JSON format.
    Supports filtering by concept_id and controlling descendant inclusion.
    Returns all matching concepts without pagination.

    Query Parameters:
        - concept_id (optional): Filter by specific concept (e.g., "#V#person")
        - include_descendants (optional): Include concepts of descendant concepts (true/false, default: true)
        - format (optional): Output format, default "json"

    Returns:
        200 OK with JSON object containing concepts array and metadata
        400 Bad Request for invalid parameters
        500 Internal Server Error for service errors
    """
    try:
        concept_id = request.args.get("concept_id")
        include_descendants_str = request.args.get(
            "include_descendants", "true"
        ).lower()
        format_param = request.args.get("format", "json")

        # Parse include_descendants parameter
        if include_descendants_str in ["true", "1", "yes"]:
            include_descendants = True
        elif include_descendants_str in ["false", "0", "no"]:
            include_descendants = False
        else:
            return (
                jsonify(
                    error="include_descendants parameter must be 'true' or 'false'"
                ),
                400,
            )

        # Log the export request
        current_app.logger.info(
            f"concept export requested: concept_id={concept_id}, include_descendants={include_descendants}, format={format_param}"
        )

        concepts, total_count = concept_service.export_concepts(
            concept_id=concept_id,
            include_descendants=include_descendants,
            format=format_param,
        )

        return (
            jsonify(
                {
                    "concepts": concepts,
                    "total_count": total_count,
                    "export_metadata": {
                        "concept_id": concept_id,
                        "include_descendants": include_descendants,
                        "format": format_param,
                        "exported_at": datetime.now(dt.timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                }
            ),
            200,
        )

    except InvalidConceptDataError as e:
        current_app.logger.warning(f"Invalid data for exporting concepts: {e}")
        return jsonify(error=str(e)), 400
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error exporting concepts: {e}", exc_info=True
        )
        return jsonify(error=str(e)), 500
    except ValueError as e:
        current_app.logger.warning(f"Invalid parameter value in export: {e}")
        return jsonify(error=f"Invalid parameter value: {e}"), 400
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error exporting concepts: {e}", exc_info=True
        )
        return jsonify(error="An unexpected error occurred during export"), 500


@concept_bp.route("/import", methods=["POST"])
def import_concepts_route():
    """API endpoint to import concepts from a JSON file.

    Accepts multipart/form-data with fields:
      - file: JSON file containing an array of concepts or an object with 'concepts' (or legacy 'entities') array
      - conflict_resolution: 'update' | 'create_new' | 'skip' (default: 'update')
      - validate_concepts: 'true' | 'false' (default: 'true')
      - dry_run: 'true' | 'false' (default: 'false')
    """
    return (
        jsonify(
            {
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "error_code": "governed_ontology_mutation_required",
                "error": "Concept imports require a governed ontology import command.",
            }
        ),
        410,
    )

    # Retained below temporarily for source-history readability; unreachable.
    try:
        if "file" not in request.files:
            return jsonify(error="Missing 'file' in multipart form-data"), 400

        uploaded = request.files["file"]
        if not uploaded or uploaded.filename == "":
            return jsonify(error="Empty file upload"), 400

        conflict_resolution = (
            (request.form.get("conflict_resolution") or "update").strip().lower()
        )
        validate_flag = (
            request.form.get("validate_concepts") or "true"
        ).strip().lower() in ["1", "true", "yes", "on"]
        dry_run = (request.form.get("dry_run") or "false").strip().lower() in [
            "1",
            "true",
            "yes",
            "on",
        ]

        try:
            raw_text = uploaded.read().decode("utf-8")
            payload = json.loads(raw_text) if raw_text else []
        except Exception as e:
            current_app.logger.warning(f"Invalid JSON in uploaded concepts file: {e}")
            return jsonify(error="Invalid JSON in uploaded file"), 400

        result = import_concepts_service(
            data=payload,
            conflict_resolution=conflict_resolution,
            validate_concepts=validate_flag,
            dry_run=dry_run,
        )

        status_code = 200
        return jsonify(result), status_code
    except InvalidConceptDataError as e:
        current_app.logger.warning(f"Invalid data for importing concepts: {e}")
        return jsonify(error=str(e)), 400
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error importing concepts: {e}", exc_info=True
        )
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error importing concepts: {e}", exc_info=True
        )
        return jsonify(error="An unexpected error occurred during import"), 500


@concept_bp.route("/import/legacy-metrics", methods=["GET"])
def get_legacy_import_metrics():
    """Return aggregated legacy field usage counters (import + read, backward compatible).

    Document schemas in system_metrics:
      { _id: 'legacy_import_counters', legacy_import_fields: { field: count, ... }, updated_at, last_batch_total }
      { _id: 'legacy_read_counters', legacy_read_fields: { field: { source_path: count } }, updated_at }

    Query params:
      include (optional): comma list; if contains 'reads' include read-path counters (default: include if any exist)
    """
    try:
        db = get_db()
        if db is None:
            return jsonify({"success": False, "error": "Database unavailable"}), 503
        include_param = request.args.get("include", "")
        include_reads_requested = "reads" in [
            p.strip().lower() for p in include_param.split(",") if p
        ]
        coll = db.get_collection("system_metrics")
        import_doc = coll.find_one({"_id": "legacy_import_counters"}) or {}
        read_doc = coll.find_one({"_id": "legacy_read_counters"}) or {}

        # Import in-memory fallback if present and no DB counters
        if not read_doc.get("legacy_read_fields"):
            try:
                from ...vontology.utils_vontology import _LEGACY_READ_FALLBACK_DOC  # type: ignore

                if _LEGACY_READ_FALLBACK_DOC:
                    # Transform structure to match expected nested mapping
                    read_doc = {
                        "legacy_read_fields": {
                            k: v for k, v in _LEGACY_READ_FALLBACK_DOC.items() if v
                        }
                    }
            except Exception:
                pass

        import_fields = import_doc.get("legacy_import_fields") or {}
        read_fields = read_doc.get("legacy_read_fields") or {}

        # Flatten nested Mongo-created path documents (e.g., metadata: { description: 1 }) -> {'metadata.description': 1}
        def _flatten_paths(node, prefix=""):
            flat = {}
            if isinstance(node, dict):
                for k, v in node.items():
                    new_prefix = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, dict):
                        flat.update(_flatten_paths(v, new_prefix))
                    else:
                        flat[new_prefix] = v
            return flat

        flattened_read_fields = {}
        for field, src_map in read_fields.items():
            if isinstance(src_map, dict):
                flattened_read_fields[field] = _flatten_paths(src_map)
            else:  # Unexpected scalar
                flattened_read_fields[field] = {"_": src_map}
        read_fields = flattened_read_fields

        updated_at_import = import_doc.get("updated_at")
        if isinstance(updated_at_import, datetime):
            updated_at_import = updated_at_import.isoformat()
        updated_at_reads = read_doc.get("updated_at")
        if isinstance(updated_at_reads, datetime):
            updated_at_reads = updated_at_reads.isoformat()

        payload = {
            "success": True,
            # Backward compatibility: existing clients expect 'fields' to be import counters
            "fields": import_fields,
            "updated_at": updated_at_import,
            "last_batch_total": import_doc.get("last_batch_total"),
        }
        # Add nested structure for richer consumers
        # Always include if explicitly requested or counters exist
        if include_reads_requested or read_fields:
            payload["reads"] = read_fields
            payload["reads_updated_at"] = updated_at_reads
        return jsonify(payload), 200
    except Exception as e:  # pragma: no cover
        current_app.logger.error(f"Error fetching legacy metrics: {e}")
        return (
            jsonify({"success": False, "error": "Failed to fetch legacy metrics"}),
            500,
        )


@concept_bp.route("/<string:concept_id>/start_interaction", methods=["POST"])
def start_concept_interaction_route(concept_id: str):
    """
    API endpoint to start an interaction session with an concept.
    """
    try:
        # Use the centralized and secure method to get the effective user concept ID
        from ...security.access_control import get_effective_organisation_concept_id

        user_id = _get_trusted_interaction_user()
        if not user_id:
            return (
                jsonify(error="Concept Q&A requires authenticated user context"),
                401,
            )

        organisation_concept_id = get_effective_organisation_concept_id()
        data = request.get_json(silent=True) or {}
        initial_notes = data.get("initial_notes", "")

        result = concept_service.start_interaction_session(
            concept_id,
            user_id=user_id,
            initial_notes=initial_notes,
            organisation_concept_id=organisation_concept_id,
            model_provider=data.get("model_provider"),
            model=data.get("model"),
            model_parameters=data.get("model_parameters"),
        )
        return jsonify(result), 200
    except ConceptNotFoundError as e:
        current_app.logger.info(
            f"concept not found for starting interaction (ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 404
    except InvalidConceptDataError as e:
        current_app.logger.warning(
            f"Invalid data for starting interaction with concept (ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 400
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error starting interaction with concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error starting interaction with concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>/generate_initial_question", methods=["POST"])
def generate_initial_question_route(concept_id: str):
    """
    API endpoint to generate an initial question for an concept based on its current state.
    """
    try:
        from ...security.access_control import get_effective_organisation_concept_id

        user_id = _get_trusted_interaction_user()
        if not user_id:
            return jsonify(error="Concept Q&A requires authenticated user context"), 401

        data = request.get_json(silent=True) or {}
        interaction_id = data.get("interaction_id")
        if not isinstance(interaction_id, str) or not interaction_id.strip():
            return jsonify(error="interaction_id is required"), 400

        organisation_concept_id = get_effective_organisation_concept_id()
        interaction_session = concept_service.get_interaction_session_by_id(
            interaction_id,
            user_id=user_id,
            organisation_concept_id=organisation_concept_id,
        )
        if not interaction_session:
            return jsonify(error="Interaction session not found"), 404

        initial_notes = data.get("initial_notes", "")
        question = concept_service.generate_initial_question(
            str(interaction_session["concept_id"]),
            initial_notes=initial_notes,
            interaction_session=interaction_session,
        )
        return jsonify({"question": question}), 200
    except ConceptNotFoundError as e:
        current_app.logger.info(
            f"concept not found for generating initial question (ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 404
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error generating initial question for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error generating initial question for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>/notes", methods=["PATCH"])
def update_concept_notes_route(concept_id: str):
    """API endpoint to update notes for an concept."""
    data = request.get_json() or {}
    if "notes" not in data or not isinstance(data["notes"], str):
        return (
            jsonify(error="Request body must be JSON with a 'notes' field (string)"),
            400,
        )
    try:
        # Use the specific notes update function instead of generic update_concept
        governed = _execute_governed_http_mutation(
            method_name="upsert_singleton_text_relation",
            arguments={
                "concept_id": concept_id,
                "predicate": "hasNote",
                "text": data["notes"],
            },
            mutate=lambda: {
                "success": bool(
                    concept_service.update_concept_notes(concept_id, data["notes"])
                )
            },
        )
        if governed.get("success") is False:
            return _governed_mutation_response(governed)
        success = True
        if success:
            # Return the updated concept with proper notes access
            if concept_id.startswith("#V#"):
                updated_concept = concept_service.get_concept_by_concept_id(concept_id)
            else:
                updated_concept = concept_service.get_concept_by_id(concept_id)
            if updated_concept:
                # Use consistent id field and proper notes getter
                updated_concept["notes"] = get_concept_notes(updated_concept)
                if "_id" in updated_concept:
                    updated_concept["id"] = str(updated_concept.pop("_id"))
                return jsonify(updated_concept), 200
            else:
                return jsonify(error="Updated concept not found"), 500
        else:
            return jsonify(error="Failed to update notes"), 500
    except ConceptNotFoundError as e:
        current_app.logger.info(
            f"concept not found for notes update (ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 404
    except InvalidConceptDataError as e:
        current_app.logger.warning(
            f"Invalid data for notes update (ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 400
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error updating notes for concept {concept_id}: {e}", exc_info=True
        )
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error updating notes for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>/description", methods=["PATCH"])
def update_concept_description_route(concept_id: str):
    """API endpoint to update description for a concept (dual-write)."""
    data = request.get_json() or {}
    if "description" not in data or not isinstance(data["description"], str):
        return (
            jsonify(
                error="Request body must be JSON with a 'description' field (string)"
            ),
            400,
        )
    try:
        governed = _execute_governed_http_mutation(
            method_name="upsert_singleton_text_relation",
            arguments={
                "concept_id": concept_id,
                "predicate": "hasDescription",
                "text": data["description"],
            },
            mutate=lambda: {
                "success": bool(
                    concept_service.update_concept_description(
                        concept_id, data["description"]
                    )
                )
            },
        )
        if governed.get("success") is False:
            return _governed_mutation_response(governed)
        success = True
        if success:
            if concept_id.startswith("#V#"):
                updated_concept = concept_service.get_concept_by_concept_id(concept_id)
            else:
                updated_concept = concept_service.get_concept_by_id(concept_id)
            if updated_concept:
                if "_id" in updated_concept:
                    updated_concept["id"] = str(updated_concept.pop("_id"))
                return jsonify(updated_concept), 200
            return jsonify(error="Updated concept not found"), 500
        return jsonify(error="Failed to update description"), 500
    except ConceptNotFoundError as e:
        current_app.logger.info(
            f"concept not found for description update (ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 404
    except InvalidConceptDataError as e:
        current_app.logger.warning(
            f"Invalid data for description update (ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 400
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error updating description for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error updating description for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>/rename", methods=["POST"])
def rename_concept_route(concept_id: str):
    """API endpoint to rename a concept's ID (JVNAUTOSCI-945).

    Request body:
        {
            "new_id": "#V#new_concept_name",
            "simulate": true/false (optional, default true),
            "preserve_alias": true/false (optional, default true)
        }

    The concept's GUID remains unchanged, ensuring stable references.
    The old concept_id is preserved as a CODE alias for backwards compatibility.
    All relationship references are automatically updated.
    """
    from ...services.concept_rename_service import rename_concept

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error="JSON object body required"), 400
    new_id = data.get("new_id")
    if not new_id or not isinstance(new_id, str):
        return (
            jsonify(error="Request body must include 'new_id' field (string)"),
            400,
        )

    simulate = _coerce_json_bool(data.get("simulate"), default=True)
    preserve_alias = _coerce_json_bool(data.get("preserve_alias"), default=True)
    skip_inaccessible = _coerce_json_bool(data.get("skip_inaccessible"), default=False)

    try:
        result = _execute_governed_http_mutation(
            method_name="rename_concept",
            arguments={
                "concept_id": concept_id,
                "new_id": new_id,
                "simulate": simulate,
                "request_id": data.get("request_id"),
            },
            mutate=lambda: rename_concept(
                old_id=concept_id,
                new_id=new_id,
                simulate=simulate,
                preserve_alias=preserve_alias,
                skip_inaccessible=skip_inaccessible,
            ),
            preview=simulate,
        )

        if result.get("success") is False and (
            result.get("authority_decision") or result.get("error_code")
        ):
            return _governed_mutation_response(result)

        if not result.get("success"):
            errors = result.get("errors", [])
            error_msg = errors[0] if errors else "Rename operation failed"
            # Determine appropriate status code
            if "not found" in error_msg.lower():
                return jsonify(error=error_msg, details=result), 404
            if (
                "protected" in error_msg.lower()
                or "already exists" in error_msg.lower()
            ):
                return jsonify(error=error_msg, details=result), 409
            return jsonify(error=error_msg, details=result), 400

        return jsonify(result), 200

    except Exception as e:
        current_app.logger.error(
            f"Unexpected error renaming concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>/submit_answer", methods=["POST"])
def submit_concept_answer_route(concept_id: str):
    """
    API endpoint to submit an answer during an interaction session with an concept.
    """
    data = request.get_json()
    if not data:
        return jsonify(error="Request body must be JSON"), 400

    interaction_id = data.get("interaction_id")
    answer = data.get("answer")
    notes_input = data.get("notes_input", "")
    # user_client_id = request.headers.get('X-User-Client-ID', 'default_user') # Get client ID
    # user_client_id is not directly used in this route's call to service, but service might use it if passed

    if not interaction_id:
        return jsonify(error="interaction_id is required"), 400
    if answer is None:  # Allow empty string for answer, but not missing 'answer' key
        return jsonify(error="answer is required (can be an empty string)"), 400
    if not isinstance(answer, str):
        return jsonify(error="answer must be a string"), 400
    if not isinstance(notes_input, str):
        return jsonify(error="notes_input must be a string"), 400
    if not answer.strip() and not notes_input.strip():
        return jsonify(error="answer or notes_input must contain text"), 400

    try:
        from ...security.access_control import get_effective_organisation_concept_id

        user_id = _get_trusted_interaction_user()
        if not user_id:
            return (
                jsonify(error="Concept Q&A requires authenticated user context"),
                401,
            )

        # Fetch the interaction session by ID (scoped to effective user/org)
        organisation_concept_id = get_effective_organisation_concept_id()
        session = concept_service.get_interaction_session_by_id(
            interaction_id,
            user_id=user_id,
            organisation_concept_id=organisation_concept_id,
        )
        if not session:
            current_app.logger.warning(
                f"Interaction session not found for ID: {interaction_id}"
            )
            return jsonify(error="Interaction session not found"), 404

        # Call the service to submit the answer
        result = concept_service.submit_concept_answer(
            interaction_id=interaction_id,
            user_answer=answer,
            user_notes_input=notes_input,
            session=session,
        )
        return jsonify(result), 200

    except ConceptNotFoundError as e:
        current_app.logger.info(
            f"concept or Interaction not found for submitting answer (concept ID: {concept_id}, Interaction ID: {interaction_id}): {e}"
        )
        return jsonify(error=str(e)), 404
    except InvalidConceptDataError as e:
        current_app.logger.warning(
            f"Invalid data for submitting answer (concept ID: {concept_id}, Interaction ID: {interaction_id}): {e}"
        )
        return jsonify(error=str(e)), 400
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error submitting answer for concept {concept_id}, interaction {interaction_id}: {e}",
            exc_info=True,
        )
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error submitting answer for concept {concept_id}, interaction {interaction_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>/active_interactions", methods=["GET"])
def get_active_interactions_route(concept_id: str):
    """
    API endpoint to get active interaction sessions for a specific concept.
    """
    try:
        # Use the centralized and secure method to get the effective user concept ID
        from ...security.access_control import (
            get_effective_organisation_concept_id,
            get_effective_user_concept_id,
        )

        user_id = get_effective_user_concept_id()
        if not user_id:
            return (
                jsonify(
                    error="Missing user context: provide X-User-Client-ID header or establish session"
                ),
                400,
            )
        organisation_concept_id = get_effective_organisation_concept_id()
        active_interactions = concept_service.get_active_interactions_for_concept(
            concept_id,
            user_id,
            organisation_concept_id=organisation_concept_id,
        )

        return (
            jsonify(
                {
                    "active_interactions": active_interactions,
                    "count": len(active_interactions),
                }
            ),
            200,
        )

    except ConceptNotFoundError as e:
        current_app.logger.info(
            f"concept not found for active interactions check (ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 404
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error getting active interactions for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error getting active interactions for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>/resume_interaction", methods=["POST"])
def resume_interaction_route(concept_id: str):
    """
    API endpoint to resume an existing interaction session.
    """
    try:
        data = request.get_json()
        if not data or "interaction_id" not in data:
            return jsonify(error="interaction_id is required"), 400

        from ...security.access_control import (
            get_effective_organisation_concept_id,
            get_effective_user_concept_id,
        )

        user_id = get_effective_user_concept_id()
        if not user_id:
            return (
                jsonify(
                    error="Missing user context: provide X-User-Client-ID header or establish session"
                ),
                400,
            )

        interaction_id = data["interaction_id"]
        organisation_concept_id = get_effective_organisation_concept_id()
        resumed_session = concept_service.resume_interaction_session(
            interaction_id,
            user_id=user_id,
            organisation_concept_id=organisation_concept_id,
        )

        return jsonify(resumed_session), 200

    except ConceptNotFoundError as e:
        current_app.logger.info(
            f"concept or interaction not found for resume (concept ID: {concept_id}): {e}"
        )
        return jsonify(error=str(e)), 404
    except ConceptServiceError as e:
        current_app.logger.error(
            f"Service error resuming interaction for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error=str(e)), 500
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error resuming interaction for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="An unexpected error occurred"), 500


@concept_bp.route("/<string:concept_id>/texts", methods=["GET"])
def get_concept_texts_route(concept_id: str):
    """Fetch language-tagged texts linked to a concept instance.

    Query params:
      - predicate (optional): filter by predicate (e.g., hasName, hasNote)
      - lang (optional): filter by language tag (e.g., en, fr)
      - limit (optional): max items (default 50)
    """
    try:
        predicate = request.args.get("predicate")
        lang = request.args.get("lang")
        limit = request.args.get("limit", default=50, type=int)
        if limit <= 0:
            return jsonify(error="limit must be positive"), 400

        results = get_texts_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            lang=lang,
            limit=limit,
        )
        return jsonify({"texts": results, "count": len(results)}), 200
    except Exception as e:
        current_app.logger.error(
            f"Error fetching texts for concept {concept_id}: {e}", exc_info=True
        )
        return jsonify(error="Failed to fetch texts"), 500


@concept_bp.route("/<string:concept_id>/texts", methods=["POST"])
def upsert_concept_text_route(concept_id: str):
    """Create or link a language-tagged text to a concept instance.

    Body JSON:
      - predicate: string (required), recommended values in RelationPredicate
      - text: string (required)
      - lang: string (optional, default 'en')
      - provenance: object (optional)
      - context: object (optional)
    """
    payload = request.get_json(silent=True) or {}
    predicate = payload.get("predicate")
    text = payload.get("text")
    lang = payload.get("lang") or "en"
    provenance = payload.get("provenance") or {}
    context = payload.get("context") or {}

    if not isinstance(predicate, str) or not predicate.strip():
        return jsonify(error="predicate (string) is required"), 400
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="text (string) is required"), 400
    if not isinstance(lang, str) or not lang.strip():
        return jsonify(error="lang must be a non-empty string if provided"), 400
    if not isinstance(provenance, dict):
        return jsonify(error="provenance must be an object if provided"), 400
    if not isinstance(context, dict):
        return jsonify(error="context must be an object if provided"), 400

    predicate = predicate.strip()
    text = text.strip()
    lang = lang.strip()
    provenance = dict(provenance)
    context = dict(context)

    allowed_predicates = {
        RelationPredicate.HAS_NAME,
        RelationPredicate.HAS_NOTE,
        RelationPredicate.HAS_DESCRIPTION,
        RelationPredicate.HAS_INTERACTION,
        RelationPredicate.HAS_CONTENT,
    }
    if predicate not in allowed_predicates and not predicate.startswith("#V#"):
        return jsonify(error=f"Unsupported predicate '{predicate}'"), 400

    if predicate in {RelationPredicate.HAS_NAME, "#V#hasName"}:
        name_type = context.get("name_type")
        if name_type is None:
            context["name_type"] = "NL"
        else:
            name_type_normalised = str(name_type).strip().upper()
            if name_type_normalised not in {"NL", "ABBR", "CODE"}:
                return (
                    jsonify(error="context.name_type must be one of NL, ABBR, CODE"),
                    400,
                )
            context["name_type"] = name_type_normalised
    try:
        predicate_resolution = resolve_text_relation_predicate_for_write(predicate)
        canonical_predicate = predicate_resolution.storage_predicate
        result = _execute_governed_http_mutation(
            method_name="upsert_text_relation",
            arguments={
                "concept_id": concept_id,
                "predicate": canonical_predicate,
                "text": text,
                "language": lang,
                "provenance": provenance,
                "context": context,
                "request_id": payload.get("request_id"),
            },
            mutate=lambda: upsert_text_for_concept(
                subject_concept_id=concept_id,
                predicate=canonical_predicate,
                text=text,
                lang=lang,
                provenance=provenance,
                context=context,
            ),
        )

        if result.get("success") is False:
            return _governed_mutation_response(result)

        maybe_sync_concept_text_relations_to_rag(
            namespace=_get_request_namespace(),
            concept_id=concept_id,
            predicate=canonical_predicate,
        )
        status = 201 if result.get("relation_created") else 200
        if result.get("context_updated"):
            status = 200
        result["status"] = (
            "created"
            if result.get("relation_created")
            else ("context-updated" if result.get("context_updated") else "exists")
        )
        return jsonify(result), status
    except TextRelationPredicateResolutionError as exc:
        return jsonify(error_code=exc.error_code, error=str(exc)), 400
    except PermissionError as pe:
        return jsonify(error=str(pe)), 403
    except Exception as e:
        current_app.logger.error(
            f"Error upserting text for concept {concept_id}: {e}", exc_info=True
        )
        return jsonify(error="Failed to upsert text"), 500


@concept_bp.route("/<string:concept_id>/texts/<string:relation_id>", methods=["PATCH"])
def update_concept_text_relation_route(concept_id: str, relation_id: str):
    """Update the text behind a specific relation (e.g., edit a note)."""
    payload = request.get_json(silent=True) or {}
    new_text = payload.get("text")
    lang = payload.get("lang") or "en"
    if not isinstance(new_text, str) or not new_text.strip():
        return jsonify(error="text (string) is required"), 400
    if not isinstance(lang, str) or not lang.strip():
        return jsonify(error="lang must be a non-empty string if provided"), 400
    new_text = new_text.strip()
    lang = lang.strip()
    provenance = {"source": "update_text_relation"}
    try:
        result = _execute_governed_http_mutation(
            method_name="update_text_relation",
            arguments={
                "concept_id": concept_id,
                "relation_id": relation_id,
                "new_text": new_text,
                "language": lang,
                "provenance": provenance,
                "request_id": payload.get("request_id"),
            },
            mutate=lambda: update_text_relation_text(
                subject_concept_id=concept_id,
                relation_id=relation_id,
                new_text=new_text,
                lang=lang,
                provenance=provenance,
            ),
        )

        if result.get("success") is False:
            return _governed_mutation_response(result)

        maybe_sync_concept_text_relations_to_rag(
            namespace=_get_request_namespace(),
            concept_id=concept_id,
        )
        return jsonify(result), 200
    except ValueError as ve:
        return jsonify(error=str(ve)), 404
    except Exception as e:
        current_app.logger.error(
            f"Error updating text relation {relation_id} for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="Failed to update text relation"), 500


@concept_bp.route("/<string:concept_id>/texts", methods=["DELETE"])
def delete_concept_text_by_predicate_route(concept_id: str):
    """Fail closed: predicate/text is not an exact mutation selector."""

    return (
        jsonify(
            success=False,
            effect_status="not_started",
            mutation_outcome="not_started",
            changed=False,
            error_code="exact_text_relation_id_required",
            error=(
                "Predicate and text do not identify one immutable relation. "
                "List the concept's text relations, then delete the selected "
                "relation by its exact relation ID."
            ),
            concept_id=concept_id,
            recovery_affordances=[
                {
                    "action_type": "list_text_relations",
                    "method": "GET",
                    "path": f"/api/concepts/{concept_id}/texts",
                },
                {
                    "action_type": "delete_exact_text_relation",
                    "method": "DELETE",
                    "path_template": f"/api/concepts/{concept_id}/texts/<relation_id>",
                },
            ],
        ),
        410,
    )


@concept_bp.route("/<string:concept_id>/texts/<string:relation_id>", methods=["DELETE"])
def delete_concept_text_relation_route(concept_id: str, relation_id: str):
    """Delete a specific text relation (e.g., remove a note)."""
    try:
        result = _execute_governed_http_mutation(
            method_name="delete_text_relation",
            arguments={
                "concept_id": concept_id,
                "relation_id": relation_id,
                "request_id": (request.get_json(silent=True) or {}).get("request_id"),
            },
            mutate=lambda: delete_text_relation(concept_id, relation_id),
        )

        if result.get("success") is False:
            return _governed_mutation_response(result)

        maybe_delete_text_relation_doc_from_rag(
            namespace=_get_request_namespace(),
            relation_id=relation_id,
        )
        return jsonify(result), 200
    except ValueError as ve:
        return jsonify(error=str(ve)), 404
    except Exception as e:
        current_app.logger.error(
            f"Error deleting text relation {relation_id} for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="Failed to delete text relation"), 500


@concept_bp.route("/<string:concept_id>/legacy-names", methods=["DELETE"])
def delete_concept_legacy_name_route(concept_id: str):
    """Remove one exact legacy inline name through semantic authority."""

    from ...security.access_control import (
        AUTHENTICATED_SESSION_ACTOR_SOURCE,
        AUTHENTICATED_SESSION_DERIVED_ACTOR_SOURCE,
        get_effective_user_concept_id_with_source,
    )

    actor_id, actor_source = get_effective_user_concept_id_with_source()
    if not actor_id or actor_source not in {
        AUTHENTICATED_SESSION_ACTOR_SOURCE,
        AUTHENTICATED_SESSION_DERIVED_ACTOR_SOURCE,
    }:
        return _governed_mutation_response(
            {
                "success": False,
                "effect_status": "not_started",
                "mutation_outcome": "not_started",
                "changed": False,
                "error_code": "authenticated_actor_context_required",
                "error": "A trusted signed-in actor is required.",
            }
        )
    payload = request.get_json(silent=True) or {}
    selector = payload.get("legacy_name_selector")
    if not isinstance(selector, dict):
        return (
            jsonify(
                success=False,
                effect_status="not_started",
                mutation_outcome="not_started",
                changed=False,
                error_code="exact_legacy_name_selector_required",
                error="legacy_name_selector must be an exact selector object.",
            ),
            400,
        )
    try:
        result = delete_legacy_name(
            concept_id=concept_id,
            legacy_name_selector=selector,
            request_id=payload.get("request_id"),
        )
        return _governed_mutation_response(result)
    except Exception:
        current_app.logger.exception(
            "Error deleting a legacy inline name for %s",
            concept_id,
        )
        return jsonify(error="Failed to delete legacy name"), 500
