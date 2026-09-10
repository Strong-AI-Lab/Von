"""Opt-in Cloudflare Access SSO; raw identity headers never grant authority."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from flask import g, jsonify, redirect, request, session
from google.auth import jwt
import requests

from .authentication_assurance import (
    AUTHENTICATION_ASSURANCE_SESSION_KEY,
    GOOGLE_OAUTH_LOGIN_EMAIL_ASSURANCE,
    session_has_required_authentication_assurance,
)
from ..services.von_user_authentication_service import (
    find_user_concept_by_login_email,
    normalise_von_login_email,
)

PROVIDER = "cloudflare_access"


class AccessVerifier:
    """Validate signatures with rotating, deployment-pinned Cloudflare keys."""

    def __init__(self, team_domain: str, audience: str):
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        self._keys: dict[str, str] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _certificates(self, kid: str) -> dict[str, str]:
        with self._lock:
            age = time.monotonic() - self._fetched_at
            # Refresh unknown keys for rotation, but not on every forged kid.
            if not self._keys or age >= 300 or (kid not in self._keys and age >= 10):
                response = requests.get(
                    f"{self.issuer}/cdn-cgi/access/certs",
                    timeout=(3, 5),
                    allow_redirects=False,
                )
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("access_keys_unavailable")
                rows = response.json().get("public_certs", [])
                keys = {
                    row["kid"]: row["cert"]
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get("kid"), str)
                    and isinstance(row.get("cert"), str)
                }
                if not keys:
                    raise ValueError("access_keys_unavailable")
                self._keys = keys
                self._fetched_at = time.monotonic()
            return dict(self._keys)

    def verify(self, token: str) -> dict:
        if not token or len(token) > 32768:
            raise ValueError("access_token_missing_or_invalid")
        header = jwt.decode_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("access_token_invalid")
        # google-auth verifies signature, iat and exp. Access uses a list-valued
        # audience, so validate that explicitly after signature verification.
        claims = jwt.decode(token, certs=self._certificates(header["kid"]))
        aud = claims.get("aud")
        if not isinstance(aud, list) or self.audience not in aud:
            raise ValueError("access_audience_invalid")
        if claims.get("iss") != self.issuer or claims.get("type") != "app":
            raise ValueError("access_issuer_or_type_invalid")
        for name in ("iat", "exp"):
            if type(claims.get(name)) is not int:
                raise ValueError("access_time_invalid")
        if claims["exp"] <= time.time() or claims["exp"] <= claims["iat"]:
            raise ValueError("access_token_expired")
        if "nbf" in claims and (
            type(claims["nbf"]) is not int or claims["nbf"] > time.time()
        ):
            raise ValueError("access_token_not_yet_valid")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError("access_subject_missing")
        # Service tokens lack a human email; they must never become a user.
        if not isinstance(claims.get("email"), str):
            raise ValueError("access_email_missing")
        claims["email"] = normalise_von_login_email(claims["email"])
        return claims


def install_cloudflare_access(app) -> None:
    """Install before identity-consuming middleware; disabled by default."""
    enabled = os.getenv("VON_CLOUDFLARE_ACCESS_ENABLED", "").lower() in {
        "1", "true", "yes", "on"
    }
    hostname = os.getenv("VON_CLOUDFLARE_ACCESS_HOSTNAME", "").strip().lower()
    team = os.getenv("VON_CLOUDFLARE_ACCESS_TEAM_DOMAIN", "").strip().lower()
    audience = os.getenv("VON_CLOUDFLARE_ACCESS_AUDIENCE", "").strip()
    if enabled and (
        not re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", team)
        or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9-]+)+", hostname)
        or not audience
        or len(str(app.secret_key or "")) < 32
        or app.secret_key == "von-dev-secret-key-change-in-production"
    ):
        raise RuntimeError("Cloudflare Access SSO requires a hostname, team domain, audience and non-default Flask secret of at least 32 characters")
    verifier = AccessVerifier(team, audience) if enabled else None
    app.extensions["cloudflare_access_verifier"] = verifier

    @app.before_request
    def authenticate_access_request():
        # Do not let an SSO-derived Von cookie authenticate a direct/Tailscale
        # request or survive disabling SSO without its original issuer proof.
        public_request = enabled and request.host.lower() == hostname
        if not public_request:
            if session.get("auth_provider") == PROVIDER:
                session.clear()
            return None

        try:
            token = request.headers.get("Cf-Access-Jwt-Assertion", "")
            claims = verifier.verify(token)
            fingerprint = hashlib.sha256(token.encode()).hexdigest()
            email = claims["email"]
            if not (
                session.get("auth_provider") == PROVIDER
                and session.get("cloudflare_assertion_hash") == fingerprint
                and session.get("user_email") == email
                and session.get("user_concept_id")
                and session_has_required_authentication_assurance(session)
            ):
                user = find_user_concept_by_login_email(email)
                if not user or not user.get("concept_id"):
                    raise ValueError("von_login_email_not_authorised")
                actor = user["concept_id"]
                if session.get("user_concept_id") != actor:
                    session.clear()
                session.update(
                    user_email=email,
                    user_concept_id=actor,
                    user_id=actor.removeprefix("#V#").lower().replace(" ", "_"),
                    google_user_info={"email": email, "name": user.get("name", "")},
                    auth_provider=PROVIDER,
                    cloudflare_assertion_hash=fingerprint,
                )
                session[AUTHENTICATION_ASSURANCE_SESSION_KEY] = GOOGLE_OAUTH_LOGIN_EMAIL_ASSURANCE
                session.pop("browser_test_fixture_id", None)
            g.cloudflare_access_authenticated = True
        except Exception as exc:
            session.clear()
            # Never log token, claims, email or provider response bodies.
            app.logger.warning("Cloudflare SSO denied (%s)", type(exc).__name__)
            response = jsonify(
                authenticated=False,
                error="Cloudflare identity could not be verified or is not bound to a Von user.",
                error_code="cloudflare_access_denied",
            )
            response.status_code = 403
            response.headers["Cache-Control"] = "no-store"
            return response

        # Existing login endpoints cannot replace this request's verified actor.
        if request.endpoint == "auth_bp.login":
            return redirect("/von/")
        if request.endpoint in {
            "auth_bp.callback", "auth_bp.exchange_auth_token", "auth_bp.browser_test_login"
        }:
            return jsonify(error_code="cloudflare_identity_authoritative"), 403
        return None
