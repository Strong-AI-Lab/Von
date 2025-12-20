from flask import Blueprint, request, jsonify, current_app
from flask.typing import ResponseReturnValue
import json
from datetime import datetime
import datetime as dt
from ...services import concept_service  # Import concept_service
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
    delete_text_relation_by_predicate_and_text,
)
from ...vontology.utils_vontology import (
    get_vontology_node_and_descendant_ids,
    get_concept_notes,
)  # Added getter import
from ...db.mongo_client import get_db
from bson import (
    ObjectId,
)  # Required for converting string IDs if necessary, though service should handle

concept_bp = Blueprint("concepts", __name__)  # Define blueprint


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
    concept_id = data.get("concept_id")
    vontology_path = data.get("vontology_path")
    description = data.get("description")
    notes = data.get("notes")
    attributes = data.get("attributes")
    system_tags = data.get("system_tags")
    user_tags = data.get("user_tags")
    linked_concepts = data.get("linked_concepts")
    relationships = data.get("relationships")

    if not name:
        return jsonify(error="concept name is required"), 400
    if not concept_id and not vontology_path:
        return jsonify(error="Either concept_id or vontology_path is required"), 400

    try:
        new_concept = concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            vontology_path=vontology_path,
            description=description,
            notes=notes,
            attributes=attributes,
            system_tags=system_tags,
            user_tags=user_tags,
            linked_concepts=linked_concepts,
        )
        # Service layer should return id as string
        return jsonify(new_concept), 201
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

                # Use shared enrichment logic for names and descriptions
                # (includes migrate-on-read for legacy fields)
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
                else:
                    # Remove legacy field from response if no description in text relations
                    concept.pop("description", None)

            return jsonify(concept), 200
        except ConceptNotFoundError as e:
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
                success = concept_service.update_concept_notes(
                    concept_id, data["notes"]
                )
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
                success = concept_service.update_concept_description(
                    concept_id, data["description"]
                )
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
                updated_concept = concept_service.update_concept(concept_id, data)
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
            success = concept_service.delete_concept(concept_id)
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
        from ...security.access_control import get_effective_user_concept_id

        user_id = get_effective_user_concept_id()
        if not user_id:
            return (
                jsonify(
                    error="Missing user context: provide X-User-Client-ID header or establish session"
                ),
                400,
            )

        initial_notes = request.json.get("initial_notes", "") if request.json else ""

        result = concept_service.start_interaction_session(
            concept_id, user_id=user_id, initial_notes=initial_notes
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
        # Get optional initial_notes from request body
        initial_notes = request.json.get("initial_notes", "") if request.json else ""
        question = concept_service.generate_initial_question(
            concept_id, initial_notes=initial_notes
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
        success = concept_service.update_concept_notes(concept_id, data["notes"])
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
        success = concept_service.update_concept_description(
            concept_id, data["description"]
        )
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
    # user_client_id = request.headers.get('X-User-Client-ID', 'default_user') # Get client ID
    # user_client_id is not directly used in this route's call to service, but service might use it if passed

    if not interaction_id:
        return jsonify(error="interaction_id is required"), 400
    if answer is None:  # Allow empty string for answer, but not missing 'answer' key
        return jsonify(error="answer is required (can be an empty string)"), 400

    try:
        # The concept_id from the path might be redundant if interaction_id is globally unique
        # and contains enough context. However, it's good for namespacing/validation.
        # The service layer's submit_concept_answer currently takes interaction_id and user_answer.
        # It might internally fetch the session using interaction_id.
        # We need to ensure the service layer handles the session context correctly.
        # For now, we assume the service layer's submit_concept_answer can retrieve the session
        # or that the session details are passed if needed.
        # The current concept_service.submit_concept_answer expects `session` as a parameter.
        # This route does not have the session object. This indicates a mismatch.
        # For now, I will assume the service layer needs to be adapted or this route needs to fetch the session.
        # Let's proceed by calling the service as it is defined in the provided concept_service.py snippet,
        # which means this route is likely missing the logic to retrieve the session.
        # This is a potential issue to flag.
        #
        # REVISITING: The concept_service.submit_concept_answer signature is:
        # submit_concept_answer(interaction_id: str, user_answer: str, session: dict) -> dict:
        # This route does not have `session`. This is a problem.
        # For now, I will call it without `session` and assume the service might be updated,
        # or this will highlight the discrepancy.
        # A more robust solution would be to fetch the interaction session here first.
        # However, without a `get_interaction_session_by_id` in the service, that's not possible.

        # Placeholder for fetching session if it were available:
        # interaction_session = concept_service.get_interaction_session(interaction_id) # Fictional function
        # if not interaction_session:
        #    return jsonify(error="Interaction session not found"), 404
        # result = concept_service.submit_concept_answer(interaction_id, answer, session=interaction_session)

        # Given the current service signature, this call will fail if the service strictly requires `session`.
        # This is a simplification for now, focusing on route structure.
        # A proper implementation would require fetching the session or modifying the service.
        # For the purpose of this exercise, I will call a hypothetical version or assume the service handles it.
        # Let's assume the service layer's `submit_concept_answer` is updated to fetch the session itself
        # or that the `session` parameter is optional / handled internally if not provided.
        # The provided concept_service.py has `submit_concept_answer(interaction_id: str, user_answer: str, session: dict)`.
        # This route cannot fulfill that contract without fetching the session.
        #
        # Let's assume a simplified service call for now, or that the service is more flexible.
        # This is a point that needs clarification or refactoring in the service layer.
        # For now, I will construct a dummy session or indicate this problem.
        #
        # Given the constraints, I will proceed as if the service layer's `submit_concept_answer`
        # can function with just `interaction_id` and `user_answer`, or that the `session`
        # parameter in the service is meant to be populated by the service itself.
        # This is a common pattern where the route passes minimal info and the service handles the details.

        # If the service strictly requires the session object, this route is incomplete.
        # Let's assume for now the service can look up the session by interaction_id.
        # The call below would be more like:
        # result = concept_service.process_answer_for_interaction(interaction_id, answer, concept_id)
        # Since `submit_concept_answer` is what's in the service, we'll call that,
        # acknowledging the `session` parameter issue.        # Fetch the interaction session by ID
        session = concept_service.get_interaction_session_by_id(interaction_id)
        if not session:
            current_app.logger.warning(
                f"Interaction session not found for ID: {interaction_id}"
            )
            return jsonify(error="Interaction session not found"), 404

        # Call the service to submit the answer
        result = concept_service.submit_concept_answer(
            interaction_id=interaction_id, user_answer=answer, session=session
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
        from ...security.access_control import get_effective_user_concept_id

        user_id = get_effective_user_concept_id()
        if not user_id:
            return (
                jsonify(
                    error="Missing user context: provide X-User-Client-ID header or establish session"
                ),
                400,
            )
        active_interactions = concept_service.get_active_interactions_for_concept(
            concept_id, user_id
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

        interaction_id = data["interaction_id"]
        resumed_session = concept_service.resume_interaction_session(interaction_id)

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

    predicate = predicate.strip()
    lang = lang.strip()

    allowed_predicates = {
        RelationPredicate.HAS_NAME,
        RelationPredicate.HAS_NOTE,
        RelationPredicate.HAS_DESCRIPTION,
        RelationPredicate.HAS_INTERACTION,
        RelationPredicate.HAS_CONTENT,
    }
    if predicate not in allowed_predicates and not predicate.startswith("#V#"):
        return jsonify(error=f"Unsupported predicate '{predicate}'"), 400

    if predicate == RelationPredicate.HAS_NAME:
        if not isinstance(context, dict):
            context = {}
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
    elif not isinstance(context, dict):
        # Non-name predicates can omit context, normalise to empty dict for downstream consumers
        context = {}

    try:
        result = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            lang=lang,
            provenance=provenance,
            context=context,
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
    try:
        result = update_text_relation_text(
            subject_concept_id=concept_id,
            relation_id=relation_id,
            new_text=new_text,
            lang=lang,
            provenance={"source": "update_text_relation"},
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
    """Delete a text relation by predicate and text value.

    Body JSON:
      - predicate: string (required)
      - text: string (required)
    """
    payload = request.get_json(silent=True) or {}
    predicate = payload.get("predicate")
    text = payload.get("text")
    lang = payload.get("lang")
    context = (
        payload.get("context") if isinstance(payload.get("context"), dict) else None
    )

    if not isinstance(predicate, str) or not predicate.strip():
        return jsonify(error="predicate (string) is required"), 400
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="text (string) is required"), 400

    predicate = predicate.strip()

    try:
        result = delete_text_relation_by_predicate_and_text(
            concept_id,
            predicate,
            text,
            lang=lang,
            context=context,
        )
        return jsonify(result), 200
    except ValueError as ve:
        return jsonify(error=str(ve)), 404
    except Exception as e:
        current_app.logger.error(
            f"Error deleting text relation by predicate {predicate} and text '{text}' for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="Failed to delete text relation"), 500


@concept_bp.route("/<string:concept_id>/texts/<string:relation_id>", methods=["DELETE"])
def delete_concept_text_relation_route(concept_id: str, relation_id: str):
    """Delete a specific text relation (e.g., remove a note)."""
    try:
        result = delete_text_relation(concept_id, relation_id)
        return jsonify(result), 200
    except ValueError as ve:
        return jsonify(error=str(ve)), 404
    except Exception as e:
        current_app.logger.error(
            f"Error deleting text relation {relation_id} for concept {concept_id}: {e}",
            exc_info=True,
        )
        return jsonify(error="Failed to delete text relation"), 500
