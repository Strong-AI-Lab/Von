from __future__ import annotations

import logging
import time
from typing import Optional

from flask import Blueprint, jsonify, redirect, request, session

from ...services.agent_gmail_oauth_service import (
    AgentGmailOAuthService,
    AgentGmailOAuthError,
)
from ...services.agent_gmail_token_store import (
    get_agent_gmail_token_status,
    revoke_agent_gmail_tokens,
)
from ...integrations.google.gmail_service import list_profile_ids_from_env

agent_gmail_oauth_bp = Blueprint("agent_gmail_oauth_bp", __name__)

logger = logging.getLogger(__name__)

_agent_gmail_oauth_service: AgentGmailOAuthService | None = None

# In-memory backup for state tracking (mirrors auth_routes approach).
_agent_oauth_states: dict[str, dict[str, object]] = {}


def _get_service() -> AgentGmailOAuthService:
    global _agent_gmail_oauth_service
    if _agent_gmail_oauth_service is None:
        _agent_gmail_oauth_service = AgentGmailOAuthService()
    return _agent_gmail_oauth_service


def _validate_profile_id(profile_id: str):
    available_profiles = list_profile_ids_from_env()
    if not available_profiles:
        return (
            jsonify(
                {
                    "error": "gmail_profiles_not_configured",
                    "detail": "No Gmail profiles are configured on the server.",
                }
            ),
            400,
        )
    if profile_id not in available_profiles:
        return (
            jsonify(
                {
                    "error": "unknown_gmail_profile",
                    "detail": "gmail_profile is not configured on the server.",
                    "available_profiles": available_profiles,
                }
            ),
            400,
        )
    return None


@agent_gmail_oauth_bp.route("/api/agent/gmail/oauth/start")
def start_agent_gmail_oauth():
    profile_id = (request.args.get("profile_id") or "").strip()
    if not profile_id:
        return jsonify({"error": "missing_profile_id"}), 400

    validation_error = _validate_profile_id(profile_id)
    if validation_error:
        return validation_error

    service = _get_service()
    try:
        authorisation_url, state = service.get_authorisation_url(profile_id=profile_id)
    except AgentGmailOAuthError as exc:
        return jsonify({"error": "oauth_config_error", "detail": str(exc)}), 400

    session["agent_gmail_oauth_state"] = {"state": state, "profile_id": profile_id}
    _agent_oauth_states[state] = {
        "timestamp": time.time(),
        "used": False,
        "profile_id": profile_id,
    }

    return redirect(authorisation_url)


@agent_gmail_oauth_bp.route("/api/agent/gmail/oauth/callback")
def agent_gmail_oauth_callback():
    service = _get_service()
    received_state = request.args.get("state")

    def _render_popup_error(
        *, title: str, message: str, detail: str | None = None, status_code: int = 400
    ):
        from flask import make_response

        safe_title = title.replace('"', "&quot;")
        safe_message = message.replace('"', "&quot;")
        safe_detail = (detail or "").replace('"', "&quot;")

        response = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>{safe_title}</title>
            <style>
                body {{ font-family: Arial, sans-serif; text-align: center; padding: 40px; }}
                .err {{ color: #b00020; font-size: 18px; }}
                .detail {{ margin-top: 14px; color: #444; }}
                code {{ background:#eee; padding:2px 4px; }}
            </style>
        </head>
        <body>
            <div class="err">
                <h2>✗ {safe_title}</h2>
                <p>{safe_message}</p>
                {f'<p class="detail"><code>{safe_detail}</code></p>' if safe_detail else ''}
                <p>This window will close automatically...</p>
            </div>

            <script>
                try {{
                    if (window.opener) {{
                        window.opener.postMessage({{
                            type: 'agentGmailOAuthError',
                            success: false,
                            title: '{safe_title}',
                            message: '{safe_message}',
                            detail: '{safe_detail}'
                        }}, window.location.origin);
                    }}
                    setTimeout(() => window.close(), 1500);
                }} catch (e) {{
                    setTimeout(() => window.close(), 2000);
                }}
            </script>
        </body>
        </html>
        """

        resp = make_response(response, status_code)
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        return resp

    provider_error = (request.args.get("error") or "").strip()
    if provider_error:
        provider_error_description = (
            request.args.get("error_description") or ""
        ).strip()
        return _render_popup_error(
            title="Agent Gmail authorisation failed",
            message="Google returned an error during authorisation.",
            detail=f"{provider_error}: {provider_error_description}".strip(": "),
            status_code=400,
        )

    session_state = session.get("agent_gmail_oauth_state")
    session_state_value: Optional[str] = None
    session_profile_id: Optional[str] = None
    if isinstance(session_state, dict):
        raw_state = session_state.get("state")
        raw_profile = session_state.get("profile_id")
        session_state_value = raw_state if isinstance(raw_state, str) else None
        session_profile_id = raw_profile if isinstance(raw_profile, str) else None

    valid_state = False
    profile_id: Optional[str] = None

    state_info = _agent_oauth_states.get(received_state) if received_state else None
    if state_info is not None:
        raw_timestamp = state_info.get("timestamp")
        timestamp = 0.0
        if isinstance(raw_timestamp, (int, float)):
            timestamp = float(raw_timestamp)
        elif isinstance(raw_timestamp, str):
            try:
                timestamp = float(raw_timestamp)
            except ValueError:
                timestamp = 0.0

        if state_info.get("used"):
            return _render_popup_error(
                title="Authorisation link already used",
                message="Please click ‘Authorise Agent Gmail’ again to start a new authorisation.",
                status_code=400,
            )

        if (time.time() - timestamp) >= 300:
            return _render_popup_error(
                title="Authorisation link expired",
                message="Please click ‘Authorise Agent Gmail’ again to start a new authorisation.",
                status_code=400,
            )

    if session_state_value and received_state and session_state_value == received_state:
        valid_state = True
        profile_id = session_profile_id
        if state_info is not None:
            state_info["used"] = True
        session.pop("agent_gmail_oauth_state", None)
    elif received_state and state_info is not None:
        valid_state = True
        state_info["used"] = True
        raw_profile = state_info.get("profile_id")
        profile_id = raw_profile if isinstance(raw_profile, str) else None
        session.pop("agent_gmail_oauth_state", None)

    if not valid_state or not profile_id:
        return _render_popup_error(
            title="Invalid authorisation state",
            message="Please click ‘Authorise Agent Gmail’ again to start a new authorisation.",
            status_code=400,
        )

    try:
        result = service.exchange_code_for_tokens(
            profile_id=profile_id,
            authorisation_response_url=request.url,
        )
    except Exception as exc:
        logger.exception(
            "[agent_gmail_oauth] Token exchange failed (profile_id=%s, has_code=%s)",
            profile_id,
            bool(request.args.get("code")),
        )
        return _render_popup_error(
            title="Agent Gmail authorisation failed",
            message="Token exchange failed. Please try again.",
            detail=f"{type(exc).__name__}: {exc}",
            status_code=500,
        )

    # Minimal HTML for popup flow; JS posts a message to parent then closes.
    from flask import make_response

    email_preview = result.authorised_email or "(unknown)"
    response = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Agent Gmail Authorised</title>
        <style>
            body {{ font-family: Arial, sans-serif; text-align: center; padding: 40px; }}
            .ok {{ color: #28a745; font-size: 18px; }}
            code {{ background:#eee; padding:2px 4px; }}
        </style>
    </head>
    <body>
        <div class="ok">
            <h2>✓ Agent Gmail authorised</h2>
            <p>Profile: <code>{result.profile_id}</code></p>
            <p>Email: <code>{email_preview}</code></p>
            <p>This window will close automatically...</p>
        </div>

        <script>
            try {{
                if (window.opener) {{
                    window.opener.postMessage({{
                        type: 'agentGmailOAuthSuccess',
                        success: true,
                        profileId: '{result.profile_id}',
                        authorisedEmail: '{email_preview}'
                    }}, window.location.origin);
                }}
                setTimeout(() => window.close(), 1200);
            }} catch (e) {{
                setTimeout(() => window.close(), 2000);
            }}
        </script>
    </body>
    </html>
    """

    resp = make_response(response)
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    return resp


@agent_gmail_oauth_bp.route("/api/agent/gmail/oauth/status")
def agent_gmail_oauth_status():
    profile_id = (request.args.get("profile_id") or "").strip()
    if not profile_id:
        return jsonify({"error": "missing_profile_id"}), 400

    validation_error = _validate_profile_id(profile_id)
    if validation_error:
        return validation_error

    status = get_agent_gmail_token_status(profile_id)
    return jsonify(
        {
            "profile_id": status.profile_id,
            "has_tokens": status.has_tokens,
            "authorised_email": status.authorised_email,
            "expires_at": status.expires_at.isoformat() if status.expires_at else None,
            "scopes": status.scopes,
        }
    )


@agent_gmail_oauth_bp.route("/api/agent/gmail/oauth/revoke", methods=["POST"])
def agent_gmail_oauth_revoke():
    profile_id = (request.args.get("profile_id") or "").strip()
    if not profile_id:
        data = request.get_json(silent=True) or {}
        raw = data.get("profile_id")
        profile_id = raw.strip() if isinstance(raw, str) else ""

    if not profile_id:
        return jsonify({"error": "missing_profile_id"}), 400

    validation_error = _validate_profile_id(profile_id)
    if validation_error:
        return validation_error

    deleted = revoke_agent_gmail_tokens(profile_id)
    return jsonify({"success": True, "profile_id": profile_id, "revoked": deleted})
