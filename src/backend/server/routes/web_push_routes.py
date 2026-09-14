"""Interactive, session-authenticated notification controls; no caller actor IDs."""

import secrets
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request, session, send_from_directory
from itsdangerous import BadSignature, URLSafeSerializer

from ...security.authentication_assurance import (
    session_has_required_authentication_assurance,
)
from ...services import web_push_service as push

web_push_bp = Blueprint("web_push", __name__)
COOKIE = "von_push_device"


def device_id():
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        value = URLSafeSerializer(current_app.secret_key, salt=COOKIE).loads(raw)
        return value if isinstance(value, str) and len(value) == 64 else None
    except BadSignature:
        return None


def revoke_browser_notifications():
    """Used before logout/account replacement. Failure must remain observable."""
    device = device_id()
    if device:
        push.revoke_device(device)


def actor_id():
    actor = session.get("user_concept_id")
    if (
        session.get("user_email")
        and isinstance(actor, str)
        and session_has_required_authentication_assurance(session)
    ):
        return actor
    return None


@web_push_bp.before_request
def protect_controls():
    if not request.path.startswith("/api/notifications"):
        return None
    if not actor_id():
        return jsonify(error="Sign in to manage notifications"), 401
    if request.method != "GET":
        if request.content_length and request.content_length > 8192:
            return jsonify(error="Subscription payload too large"), 413
        origin = request.headers.get("Origin")
        expected = urlsplit(request.host_url)
        actual = urlsplit(origin or "")
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            return jsonify(error="Same-origin request required"), 403
        if not request.is_json:
            return jsonify(error="JSON required"), 415
    return None


@web_push_bp.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@web_push_bp.errorhandler(Exception)
def unavailable(exc):
    # Provider and database exceptions can contain endpoint credentials.
    if isinstance(exc, PermissionError):
        return jsonify(error=str(exc)), 403
    if isinstance(exc, ValueError):
        return jsonify(error=str(exc)), 400
    current_app.logger.warning("Notification operation unavailable")
    return jsonify(error="Notifications unavailable; try again later"), 503


def scope():
    from .message_routes import _get_current_org_concept_id

    organisation = _get_current_org_concept_id(actor_id(), require_known_window=True)
    if not push.scope_allowed(actor_id(), organisation):
        raise PermissionError("Organisation access required")
    return organisation


@web_push_bp.get("/api/notifications/status")
def status():
    config = push.configuration()
    if not config["configured"]:
        return jsonify(**config, state="unavailable")
    device = device_id() or secrets.token_hex(32)
    response = jsonify(**config, **push.status(actor_id(), device, scope()))
    response.set_cookie(
        COOKIE,
        URLSafeSerializer(current_app.secret_key, salt=COOKIE).dumps(device),
        secure=request.is_secure,
        httponly=True,
        samesite="Strict",
        max_age=365 * 86400,
        path="/",
    )
    return response


@web_push_bp.post("/api/notifications/subscribe")
def subscribe():
    if not push.configuration()["configured"] or not device_id():
        return jsonify(error="Refresh notification status first"), 409
    payload = request.get_json() or {}
    return jsonify(
        push.subscribe(actor_id(), device_id(), scope(), payload.get("subscription"))
    )


@web_push_bp.post("/api/notifications/confirm")
def confirm():
    payload = request.get_json() or {}
    identifier, challenge = payload.get("id"), payload.get("challenge")
    if (
        not isinstance(identifier, str)
        or not isinstance(challenge, str)
        or len(challenge) > 128
    ):
        raise ValueError("Invalid confirmation")
    if not push.confirm(actor_id(), device_id(), identifier, challenge):
        return jsonify(error="Confirmation expired or belongs to another device"), 403
    return jsonify(state="active")


@web_push_bp.post("/api/notifications/disable")
def disable():
    # Device-wide opt-out also covers prior actor/scope records. The signed
    # HttpOnly device cookie is the authority, never a supplied endpoint.
    revoke_browser_notifications()
    return jsonify(state="disabled")


@web_push_bp.post("/api/notifications/test")
def test():
    payload = request.get_json() or {}
    identifier = payload.get("id")
    if not isinstance(identifier, str):
        raise ValueError("Invalid subscription")
    return jsonify(push.test_notification(actor_id(), device_id(), identifier))


@web_push_bp.post("/api/notifications/validate")
def validate_delivery():
    payload = request.get_json() or {}
    identifier = payload.get("id")
    generation = payload.get("generation")
    if not isinstance(identifier, str) or not isinstance(generation, str):
        raise ValueError("Invalid subscription")
    row = push.collection().find_one(
        {
            "_id": identifier,
            "actor": actor_id(),
            "device": device_id(),
            "state": "active",
            "generation": generation,
            "expires_at": {"$gt": push.now()},
        }
    )
    if not row or not push.scope_allowed(actor_id(), row["organisation"]):
        return jsonify(error="Notification subscription no longer active"), 403
    return jsonify(active=True)


@web_push_bp.get("/api/notifications/open/<receipt_id>")
def open_message(receipt_id):
    from ...services.message_service import (
        get_message_for_user,
        PREDICATE_RECIPIENT,
        PREDICATE_SENDER,
    )
    from ...services.message_catalogue_service import exchange_id
    from ...security.access_control import override_current_actor

    actor = actor_id()
    receipt = push.collection(push.RECEIPTS).find_one(
        {
            "_id": receipt_id,
            "actor": actor,
            "expires_at": {"$gt": push.now()},
        }
    )
    if not receipt or not push.scope_allowed(actor, receipt["organisation"]):
        return jsonify(error="Message unavailable for this account"), 404
    with override_current_actor(actor, receipt["organisation"]):
        message = get_message_for_user(receipt["message_id"], actor)
    if (
        not message
        or message.get("concept_data", {}).get("deleted")
        or actor not in message.get("relationships", {}).get(PREDICATE_RECIPIENT, [])
    ):
        return jsonify(error="Message unavailable for this account"), 404
    rel = message["relationships"]
    participants = sorted(
        set(rel.get(PREDICATE_SENDER, []) + rel.get(PREDICATE_RECIPIENT, []))
    )
    return jsonify(
        session_id=exchange_id(participants, receipt["organisation"]),
        source_kind="message_exchange",
        participant_ids=participants,
        other_participant_ids=[p for p in participants if p != actor],
        viewer_id=actor,
        organisation_concept_id=receipt["organisation"],
        session_name="Von messages",
        last_message_id=message["concept_id"],
    )


@web_push_bp.get("/von/service-worker.js")
def service_worker():
    response = send_from_directory(current_app.static_folder, "service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/von/"
    return response


@web_push_bp.get("/von/manifest.webmanifest")
def manifest():
    return jsonify(
        id="/von/",
        name="Von",
        short_name="Von",
        start_url="/von/",
        scope="/von/",
        display="standalone",
        background_color="#ffffff",
        theme_color="#245e47",
        icons=[
            {
                "src": f"/static/pwa-icon-{size}.png",
                "sizes": f"{size}x{size}",
                "type": "image/png",
                "purpose": "any",
            }
            for size in (192, 512)
        ],
    )
