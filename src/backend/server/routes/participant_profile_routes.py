"""Common profile API for Settings, concepts, conversations and agent tools."""

import io
import logging

from flask import jsonify, request, send_file, session

from ...services import participant_profile_service as profiles
from ...services.conversation_image_service import MAX_IMAGE_BYTES

_log = logging.getLogger(__name__)


def register_participant_profile_routes(blueprint):
    def perform(action):
        try:
            return jsonify(success=True, **action())
        except PermissionError as exc:
            return jsonify(error="profile_unavailable", message=str(exc)), 403
        except ValueError as exc:
            return jsonify(error="invalid_profile_image", message=str(exc)), 400
        except Exception:  # noqa: BLE001 - provider/storage failures are reported at the HTTP boundary
            _log.exception("Participant profile operation failed")
            return jsonify(
                error="profile_operation_failed",
                message="The profile operation failed. Your existing avatar is unchanged unless saving completed; refresh to check.",
            ), 503

    @blueprint.route("/api/participants/profile", methods=["GET"])
    def participant_profile():
        return perform(
            lambda: {"profile": profiles.get_profile(request.args.get("concept_id"))}
        )

    @blueprint.route("/api/participants/profiles", methods=["POST"])
    def participant_profiles():
        return perform(
            lambda: {
                "profiles": profiles.get_profiles(
                    (request.get_json(silent=True) or {}).get("concept_ids", [])
                )
            }
        )

    @blueprint.route("/api/participants/avatar", methods=["POST"])
    def participant_avatar_save():
        payload = request.get_json(silent=True) or request.form
        uploaded = request.files.get("file")
        return perform(
            lambda: {
                "profile": profiles.set_avatar(
                    concept_id=payload.get("concept_id"),
                    image_concept_id=payload.get("image_concept_id"),
                    remove=payload.get("remove") is True,
                    scope=payload.get("scope", "global_general"),
                    data=uploaded.read(MAX_IMAGE_BYTES + 1) if uploaded else None,
                )
            }
        )

    @blueprint.route("/api/participants/avatar/generate", methods=["POST"])
    def participant_avatar_generate():
        payload = request.get_json(silent=True) or {}
        return perform(
            lambda: {
                "image": profiles.generate_avatar(
                    concept_id=payload.get("concept_id"),
                    prompt=payload.get("prompt"),
                )
            }
        )

    @blueprint.route("/api/participants/<path:concept_id>/avatar", methods=["GET"])
    def participant_avatar_image(concept_id):
        try:
            from ...security.access_control import (
                get_effective_user_concept_id,
                override_current_actor,
            )
            from ...services.window_session_context_service import get_effective_context

            actor = get_effective_user_concept_id()
            window_id = request.args.get("window_session_id") or request.headers.get(
                "X-Von-Window-Session"
            )
            context = get_effective_context(
                window_id, dict(session), actor, require_known_window=True
            )
            with override_current_actor(actor, context.get("organisation_id")):
                data = profiles.load_avatar(concept_id)
        except (PermissionError, ValueError):
            return jsonify(error="avatar_unavailable"), 404
        response = send_file(io.BytesIO(data), mimetype="image/png", max_age=0)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
