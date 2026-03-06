# src/backend/server/routes/elicitation_routes.py
from flask import Blueprint, request, jsonify, current_app
from ...services.relation_elicitation_service import RelationElicitationService

elicitation_bp = Blueprint("elicitation_bp", __name__)
service = RelationElicitationService()


def _extract_user_context(data):
    """Extract user context from request payload for request-scoped identity.

    Returns dict with user_id, org_id, language. All fields optional.
    Frontend should send: {context: {user_id, org_id, language}} with each request.
    """
    ctx = data.get("context", {}) if isinstance(data, dict) else {}
    return {
        "user_id": ctx.get("user_id"),
        "org_id": ctx.get("org_id") or ctx.get("organisation_id"),
        "language": ctx.get("language") or ctx.get("preferred_language"),
    }


@elicitation_bp.route("/opportunities/<instance_id>", methods=["GET"])
def get_opportunities(instance_id):
    """
    Gets a list of predicates that are suggested for an instance but not yet filled.
    """
    try:
        opportunities = service.get_elicitation_opportunities(instance_id)
        return jsonify({"opportunities": opportunities}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@elicitation_bp.route("/ask", methods=["POST"])
def ask_question():
    """
    Generates the natural language question to ask the user for a given relation.
    """
    data = request.get_json()
    instance_id = data.get("instance_id")
    predicate = data.get("predicate")

    # Extract user context for request-scoped identity
    user_ctx = _extract_user_context(data)

    if not instance_id or not predicate:
        return jsonify({"error": "instance_id and predicate are required"}), 400

    try:
        # Log context for visibility
        current_app.logger.debug(
            f"[elicitation/ask] user={user_ctx.get('user_id')} org={user_ctx.get('org_id')} lang={user_ctx.get('language')}"
        )

        question = service.generate_question_for_elicit(instance_id, predicate)
        if question:
            return jsonify({"question": question}), 200
        else:
            return jsonify({"error": "Could not generate question"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@elicitation_bp.route("/submit", methods=["POST"])
def submit_response():
    """
    Takes a user's response, processes it, and creates a hypothesis.
    """
    data = request.get_json()
    instance_id = data.get("instance_id")
    predicate = data.get("predicate")
    answer = data.get("answer")

    # Extract user context for request-scoped identity
    user_ctx = _extract_user_context(data)

    if not all([instance_id, predicate, answer]):
        return (
            jsonify({"error": "instance_id, predicate, and answer are required"}),
            400,
        )

    try:
        # Log context for visibility and future audit trail
        current_app.logger.info(
            f"[elicitation/submit] user={user_ctx.get('user_id')} org={user_ctx.get('org_id')} instance={instance_id} predicate={predicate}"
        )

        hypothesis = service.process_and_store_hypothesis(
            instance_id, predicate, answer
        )
        if hypothesis:
            return jsonify(hypothesis), 201
        else:
            return jsonify({"error": "Could not create hypothesis"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
