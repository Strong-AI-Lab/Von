from flask import Blueprint, request, redirect, session, url_for, jsonify

from ...auth_service import GoogleAuthService
from ...services.settings_service import (
    set_current_user_by_email,
)
from ...services.exceptions import MultipleUsersForEmailError
import time

auth_bp = Blueprint("auth_bp", __name__)

_google_auth_service: GoogleAuthService | None = None


def _get_google_auth_service() -> GoogleAuthService:
    """Lazily instantiate the Google auth service.

    Avoids constructing the OAuth flow at import time so unit tests can monkeypatch
    the Flow implementation before the service is created.
    """
    global _google_auth_service
    if _google_auth_service is None:
        _google_auth_service = GoogleAuthService()
    return _google_auth_service


def _set_google_auth_service(service: GoogleAuthService | None) -> None:
    """Allow tests to inject a stub service."""
    global _google_auth_service
    _google_auth_service = service


# In-memory state storage as backup for popup windows
# In production, this should be Redis or database-backed
_oauth_states = {}

_ACTOR_SCOPE_SESSION_KEYS = (
    "organisation_concept_id",
    "role_in_org",
    "namespace",
    "session_id",
)


def _normalise_session_identity(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _clear_scope_if_authenticated_identity_changes(
    *,
    new_user_concept_id: object,
    new_email: object,
) -> None:
    """Drop prior actor scope unless the new login proves the same identity."""
    previous_concept_id = _normalise_session_identity(
        session.get("user_concept_id")
    )
    previous_email = _normalise_session_identity(session.get("user_email"))
    next_concept_id = _normalise_session_identity(new_user_concept_id)
    next_email = _normalise_session_identity(new_email)

    same_actor = False
    if previous_concept_id and next_concept_id:
        same_actor = previous_concept_id == next_concept_id
    elif previous_email and next_email:
        same_actor = previous_email == next_email

    had_previous_identity = bool(previous_concept_id or previous_email)
    has_stale_scope_without_identity = not had_previous_identity and any(
        session.get(key) is not None for key in _ACTOR_SCOPE_SESSION_KEYS
    )
    if (had_previous_identity and not same_actor) or has_stale_scope_without_identity:
        for key in _ACTOR_SCOPE_SESSION_KEYS:
            session.pop(key, None)


def _build_auth_status_payload() -> dict:
    """Build the canonical auth-status payload for browser consumers."""
    try:
        from ...services.browser_test_auth_service import describe_browser_test_mode

        browser_test_mode = describe_browser_test_mode()
    except Exception:
        browser_test_mode = {
            "configured": False,
            "available": False,
            "reason": "unavailable",
        }

    user_email = session.get("user_email")
    user_info = session.get("google_user_info")
    user_concept_id = session.get("user_concept_id")
    auth_provider = session.get("auth_provider")
    if not isinstance(auth_provider, str) or not auth_provider.strip():
        auth_provider = (
            "browser_test_fixture"
            if session.get("browser_test_fixture_id")
            else ("google_oauth" if user_email else None)
        )

    return {
        "authenticated": bool(user_email),
        "email": user_email,
        "name": user_info.get("name") if isinstance(user_info, dict) else None,
        "user_concept_id": user_concept_id,
        "auth_provider": auth_provider,
        "browser_test_mode": browser_test_mode,
    }


@auth_bp.route("/api/auth/google/login")
def login():
    """Redirects the user to Google's OAuth 2.0 consent screen."""
    service = _get_google_auth_service()
    # Validate host matches configured redirect origin; optionally adjust for ngrok when enabled.
    try:
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        proto = (
            forwarded_proto.split(",")[0].strip() if forwarded_proto else request.scheme
        )
        proto = proto or request.scheme
        current_origin = f"{proto}://{request.host}"
        configured = getattr(service, "canonical_origin", None)
        if configured and current_origin != configured:
            if service.allows_dynamic_host(request.host):
                new_redirect = service.build_redirect_for_host(proto, request.host)
                service.update_redirect(new_redirect)
                print(
                    f"[auth_login] Dynamic redirect enabled host={request.host} redirect={new_redirect}"
                )
                configured = getattr(service, "canonical_origin", None)
            if configured and current_origin != configured:
                print(
                    f"[auth_login] Origin mismatch current={current_origin} configured={configured}; blocking OAuth start"
                )
                from flask import make_response

                html = f"""
                <!DOCTYPE html><html><head><title>Host Mismatch</title>
                <style>body {{ font-family: Arial, sans-serif; padding: 25px; }} code {{ background:#eee; padding:2px 4px; }} .warn {{ color:#c00; font-weight:bold; }}</style>
                </head><body>
                <h2>Host Mismatch Detected</h2>
                <p class='warn'>Google OAuth redirect is configured for <code>{configured}</code> but you are accessing the app via <code>{current_origin}</code>.</p>
                <p>This causes <code>redirect_uri_mismatch</code>. Please reopen the application using the canonical URL:</p>
                <p><a href='{configured}'>{configured}</a></p>
                <p>After switching, attempt Google Sign In again.</p>
                <p style='font-size:12px;color:#666'>Set <code>GOOGLE_OAUTH_REDIRECT_URI</code> or enable dynamic ngrok redirects by exporting <code>GOOGLE_OAUTH_ENABLE_DYNAMIC_REDIRECTS=true</code> before starting the server (see <code>scripts/start_ngrok_tunnel.ps1</code>).</p>
                </body></html>
                """
                resp = make_response(html, 400)
                resp.headers["Content-Type"] = "text/html"
                return resp
    except Exception as e:
        print(f"[auth_login] Host validation exception: {e}")

    authorization_url, state = service.get_authorization_url()
    # Keep a single concise log for traceability
    print(
        f"[auth_login] start state={authorization_url.split('state=')[1].split('&')[0] if 'state=' in authorization_url else 'unknown'} redirect={service.redirect_uri}"
    )

    # Store state in both session AND in-memory backup (for popup windows)
    session["oauth_state"] = state
    _oauth_states[state] = {"timestamp": time.time(), "used": False}

    # Minimal debug line (state + session keys only)
    print(f"[auth_login] state_saved={state} session_keys={list(session.keys())}")

    return redirect(authorization_url)


@auth_bp.route("/api/auth/google/callback")
def callback():
    """Handles the OAuth 2.0 callback from Google."""
    service = _get_google_auth_service()
    received_state = request.args.get("state")
    session_state = session.get("oauth_state")

    # Condensed debug
    print(
        f"[auth_callback] session_state={session_state} received_state={received_state} session_keys={list(session.keys())}"
    )

    # Check session first, then fallback to in-memory storage for popup windows
    valid_state = False
    if session_state and session_state == received_state:
        print("[auth_callback] State validated via session")
        valid_state = True
    elif received_state in _oauth_states:
        state_info = _oauth_states[received_state]
        # Check if state is not used and not too old (5 minutes)
        if not state_info["used"] and (time.time() - state_info["timestamp"]) < 300:
            print("[auth_callback] State validated via memory backup")
            valid_state = True
            # Mark as used
            _oauth_states[received_state]["used"] = True
        else:
            print("[auth_callback] Memory state expired or already used")

    if not valid_state:
        print("[auth_callback] Invalid state - no valid session or memory match")
        return (
            f"Invalid state. Session: {session_state}, Received: {received_state}",
            400,
        )

    try:
        print(f"[auth_callback] exchanging_code state={received_state}")
        id_info = service.exchange_code_for_tokens(request.url)
        print(
            f"[auth_callback] id_info_keys={list(id_info.keys()) if id_info else 'None'}"
        )

        email = id_info.get("email")
        name = id_info.get("name", "")
        print(f"[auth_callback] user_email={email}")

        if not email:
            print("[auth_callback] No email found in Google profile")
            return "Email not found in Google profile.", 400

        try:
            # Find or create the user in the Von database and set them as current.
            user = set_current_user_by_email(email, name)
        except MultipleUsersForEmailError as e:
            return (
                jsonify(
                    {
                        "error": "Multiple users found for this email",
                        "conflicting_users": e.user_ids,
                    }
                ),
                409,
            )

        # Never carry organisation/namespace/chat authority across accounts in
        # the same browser session.
        user_concept_id = user.get("concept_id") if user else None
        _clear_scope_if_authenticated_identity_changes(
            new_user_concept_id=user_concept_id,
            new_email=email,
        )

        # Set session variables for web authentication
        session["user_email"] = email
        session["google_user_info"] = {"name": name, "email": email}
        session["user_concept_id"] = user_concept_id
        session["auth_provider"] = "google_oauth"
        session.pop("browser_test_fixture_id", None)
        print(f"[auth_callback] session_updated user_email={email}")

        # Create a temporary auth token for the popup to send to the parent window
        import secrets

        temp_token = secrets.token_urlsafe(32)
        _oauth_states[temp_token] = {"timestamp": time.time(), "user": user}
        print(f"[auth_callback] temp_token_created len={len(temp_token)}")

        # Redirect to success page with the temp token
        return redirect(url_for("auth_bp.success", token=temp_token))

    except Exception as e:
        # Enhanced error logging
        import traceback

        print(f"[auth_callback] EXCEPTION: {type(e).__name__}: {str(e)}")
        print(f"[auth_callback] TRACEBACK: {traceback.format_exc()}")
        return (
            f"An error occurred during authentication: {type(e).__name__}: {str(e)}",
            500,
        )


@auth_bp.route("/success")
def success():
    """Simple success page for OAuth callback."""
    token = request.args.get("token")
    print(f"[auth_success] Success page loaded with token: {token}")

    # Properly escape the token for JavaScript
    js_token = token.replace("'", "\\'").replace('"', '\\"') if token else ""
    js_token_display = token[:20] + "..." if token else "None"

    response = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Login Successful</title>
        <style>
            body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; }}
            .success {{ color: #28a745; font-size: 18px; }}
        </style>
    </head>
    <body>
        <div class="success">
            <h2>✓ Login Successful!</h2>
            <p>You have been successfully logged in with Google.</p>
            <p>This window will close automatically...</p>
            <p style="font-size: 12px; color: #666;">Token: {js_token_display}</p>
        </div>

        <script>
            console.log('[auth_success] Success page loaded');
            console.log('[auth_success] Auth token length:', '{len(js_token)}');
            console.log('[auth_success] Window opener exists:', !!window.opener);

            // Close the popup window and signal success to parent
            try {{
                // Signal to parent window that login was successful with auth token
                if (window.opener) {{
                    console.log('[auth_success] Sending postMessage to parent window');
                    const message = {{
                        type: 'googleLoginSuccess',
                        success: true,
                        authToken: '{js_token}'
                    }};
                    console.log('[auth_success] Message to send (token length):', message.authToken.length);
                    window.opener.postMessage(message, window.location.origin);
                    console.log('[auth_success] Message sent successfully');
                }} else {{
                    console.warn('[auth_success] No window.opener available');
                }}

                // Close the popup after a short delay
                setTimeout(() => {{
                    console.log('[auth_success] Closing popup window');
                    window.close();
                }}, 2000);

            }} catch(e) {{
                console.error('[auth_success] Error communicating with parent window:', e);
                // Fallback: still try to close the window
                setTimeout(() => {{
                    window.close();
                }}, 3000);
            }}
        </script>
    </body>
    </html>
    """

    # Set headers to allow cross-origin communication
    from flask import make_response

    resp = make_response(response)
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    return resp


def cleanup_old_states():
    """Remove OAuth states older than 10 minutes to prevent memory leaks."""
    current_time = time.time()
    states_to_remove = []
    for state, info in _oauth_states.items():
        if current_time - info["timestamp"] > 600:  # 10 minutes
            states_to_remove.append(state)

    for state in states_to_remove:
        del _oauth_states[state]

    if states_to_remove:
        print(f"[auth_cleanup] Removed {len(states_to_remove)} expired states")


@auth_bp.route("/api/auth/exchange-token", methods=["POST"])
def exchange_auth_token():
    """Exchange a temporary auth token for a proper session."""
    data = request.get_json()
    token = data.get("token") if data else None

    if not token:
        return jsonify({"success": False, "error": "No token provided"}), 400

    # Look up the token in our temporary storage
    token_info = _oauth_states.get(token)
    if not token_info:
        return jsonify({"success": False, "error": "Invalid or expired token"}), 400

    # Check if token is not too old (10 minutes max)
    if time.time() - token_info["timestamp"] > 600:
        _oauth_states.pop(token, None)
        return jsonify({"success": False, "error": "Token expired"}), 400

    user = token_info.get("user")
    if not user:
        return (
            jsonify({"success": False, "error": "User information not found in token"}),
            400,
        )

    # The parent window may already hold another user's org/session context.
    _clear_scope_if_authenticated_identity_changes(
        new_user_concept_id=user.get("concept_id"),
        new_email=user.get("email"),
    )

    # Set session variables based on the user object
    session["user_email"] = user.get("email")
    session["google_user_info"] = {"name": user.get("name"), "email": user.get("email")}
    session["user_concept_id"] = user.get("concept_id")
    session["auth_provider"] = "google_oauth"
    session.pop("browser_test_fixture_id", None)

    # Provide a consistent slug-style user_id for downstream session-aware routes
    # (org selection, namespace derivation) that currently expect a plain slug.
    user_slug = None
    concept_id = user.get("concept_id") or ""
    if concept_id:
        user_slug = concept_id[3:] if concept_id.startswith("#V#") else concept_id
    elif user.get("email"):
        user_slug = user.get("email").split("@")[0]

    if user_slug:
        session["user_id"] = user_slug.strip().lower().replace(" ", "_")

    print(f"[auth_exchange] Successfully set session for {user.get('email')}")
    print(f"[auth_exchange] Session contents: {dict(session)}")

    # Clean up the temporary token
    _oauth_states.pop(token, None)

    return jsonify({"success": True, "authenticated": True, "user": user})


@auth_bp.route("/api/auth/status", methods=["GET"])
def get_auth_status():
    """Get current authentication status."""
    try:
        host_hdr = request.host
    except Exception:
        host_hdr = "UNKNOWN"
    cookie_keys = list(request.cookies.keys()) if request.cookies else []
    print(
        f"[auth_status] Host={host_hdr} Cookies={cookie_keys} Session={dict(session)}"
    )
    return jsonify(_build_auth_status_payload())


@auth_bp.route("/api/auth/browser-test-login", methods=["POST"])
def browser_test_login():
    """Establish a localhost-only pseudouser session for browser acceptance testing."""
    from ...services.browser_test_auth_service import (
        browser_test_auth_allowed_for_request,
        describe_browser_test_mode,
        login_browser_test_user,
    )

    allowed, reason = browser_test_auth_allowed_for_request()
    if not allowed:
        return (
            jsonify(
                {
                    "success": False,
                    "error": reason or "Browser-test auth is unavailable",
                    "browser_test_mode": describe_browser_test_mode(),
                }
            ),
            403,
        )

    data = request.get_json(silent=True) or {}
    window_session_id = request.headers.get("X-Von-Window-Session") or data.get(
        "window_session_id"
    )
    refresh_fixture = data.get("refresh_fixture")
    if not isinstance(refresh_fixture, bool):
        refresh_fixture = None
    try:
        result = login_browser_test_user(
            window_session_id=window_session_id,
            refresh_fixture=refresh_fixture,
        )
    except MultipleUsersForEmailError as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Multiple users found for browser-test email",
                    "conflicting_users": exc.user_ids,
                    "browser_test_mode": describe_browser_test_mode(),
                }
            ),
            409,
        )
    except Exception as exc:
        from ...services.window_session_context_service import (
            WindowSessionOwnershipError,
        )

        if isinstance(exc, WindowSessionOwnershipError):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "window_session_actor_mismatch",
                        "error_code": "window_session_actor_mismatch",
                        "browser_test_mode": describe_browser_test_mode(),
                    }
                ),
                403,
            )
        return (
            jsonify(
                {
                    "success": False,
                    "error": str(exc),
                    "browser_test_mode": describe_browser_test_mode(),
                }
            ),
            500,
        )

    payload = _build_auth_status_payload()
    payload.update(
        {
            "success": True,
            "fixture": result.get("fixture"),
            "organisation": result.get("organisation"),
            "namespace": result.get("namespace"),
            "window_session_id": result.get("window_session_id"),
            "active_chat_session_id": result.get("active_chat_session_id"),
        }
    )
    return jsonify(payload)


@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    """Log out the current user by clearing their session."""
    user_email = session.get("user_email")
    user_id = (
        session.get("user_concept_id")
        or session.get("user_id")
        or session.get("user_email")
    )

    from ...services.window_session_context_service import (
        delete_window_context_if_owned,
    )

    delete_window_context_if_owned(
        request.headers.get("X-Von-Window-Session"),
        user_id,
    )

    # Clear the entire session to ensure a clean logout
    session.clear()

    print(f"[auth_logout] User {user_email} logged out successfully")

    return jsonify({"success": True, "message": "Logged out successfully"})


@auth_bp.route("/test-popup")
def test_popup():
    """Test popup for debugging postMessage communication."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Test Popup</title>
    </head>
    <body>
        <h1>Test Popup</h1>
        <p>This is a test popup to debug postMessage.</p>
        <button onclick="sendTestMessage()">Send Test Message</button>
        <button onclick="window.close()">Close</button>

        <script>
            console.log('[test_popup] Test popup loaded');
            console.log('[test_popup] Window opener exists:', !!window.opener);

            function sendTestMessage() {
                if (window.opener) {
                    console.log('[test_popup] Sending test message to parent');
                    const message = {
                        type: 'testMessage',
                        success: true,
                        message: 'Hello from popup!'
                    };
                    window.opener.postMessage(message, window.location.origin);
                    console.log('[test_popup] Test message sent');
                } else {
                    console.warn('[test_popup] No window.opener available');
                }
            }

            // Auto-send message after 1 second
            setTimeout(sendTestMessage, 1000);
        </script>
    </body>
    </html>
    """
