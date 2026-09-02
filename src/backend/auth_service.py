"""Handles Google OAuth 2.0 authentication flow."""

import os
from contextlib import contextmanager
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import urlparse

from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from .services.google_oauth_config import (
    configure_oauthlib_insecure_transport,
    load_secret_from_env_or_file,
)

_DEFAULT_GOOGLE_OAUTH_CLOCK_SKEW_SECONDS = 10
_MAX_GOOGLE_OAUTH_CLOCK_SKEW_SECONDS = 300


@runtime_checkable
class CredentialsProtocol(Protocol):  # Minimal shape we rely on for static checking
    id_token: str | None  # attribute provided when openid scope included


try:  # pragma: no cover - defensive import
    from google.oauth2.credentials import Credentials as OAuthCredentials  # type: ignore
except Exception:  # pragma: no cover
    OAuthCredentials = Any  # type: ignore


class GoogleAuthService:
    def __init__(self):
        self.client_id = load_secret_from_env_or_file(
            "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID_FILE"
        )
        self.client_secret = load_secret_from_env_or_file(
            "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET_FILE"
        )
        # Default now prefers localhost (previously 127.0.0.1) for consistency with docs & Google console entries.
        self.redirect_uri = os.getenv(
            "GOOGLE_OAUTH_REDIRECT_URI",
            "http://localhost:5000/von/api/auth/google/callback",
        )
        configure_oauthlib_insecure_transport(self.redirect_uri)
        # Optional tolerance for minor system clock differences when verifying ID tokens.
        # Google callbacks can arrive a few seconds before local time catches token iat.
        try:
            skew_raw = os.getenv(
                "GOOGLE_OAUTH_CLOCK_SKEW_SECONDS",
                str(_DEFAULT_GOOGLE_OAUTH_CLOCK_SKEW_SECONDS),
            ).strip()
            self.clock_skew_seconds = max(
                0, min(int(skew_raw or "0"), _MAX_GOOGLE_OAUTH_CLOCK_SKEW_SECONDS)
            )
        except Exception:
            self.clock_skew_seconds = _DEFAULT_GOOGLE_OAUTH_CLOCK_SKEW_SECONDS
        parsed_redirect = urlparse(self.redirect_uri)
        # Derive canonical host (scheme://host:port) from configured redirect for validation
        if parsed_redirect.scheme and parsed_redirect.netloc:
            self.canonical_origin = (
                f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"
            )
            self._canonical_netloc = parsed_redirect.netloc.casefold()
        else:
            self.canonical_origin = None
            self._canonical_netloc = None
        # Persist base path/query so dynamic hosts can reuse the callback path safely.
        self._redirect_path = parsed_redirect.path or "/von/api/auth/google/callback"
        self._redirect_query = (
            f"?{parsed_redirect.query}" if parsed_redirect.query else ""
        )

        # Allow runtime redirect adjustments (ngrok) when explicitly enabled via env var.
        flag_value = os.getenv("GOOGLE_OAUTH_ENABLE_DYNAMIC_REDIRECTS", "")
        self.dynamic_redirects_enabled = flag_value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        raw_suffixes = os.getenv("GOOGLE_OAUTH_DYNAMIC_REDIRECT_SUFFIXES", "")
        if raw_suffixes.strip():
            suffix_candidates = [
                suffix.strip().lower()
                for suffix in raw_suffixes.split(",")
                if suffix.strip()
            ]
        else:
            suffix_candidates = [".ngrok-free.app", ".ngrok.app", ".ngrok.io"]
        self.allowed_dynamic_suffixes = tuple(suffix_candidates)

        # The client_secrets.json file is not used directly. Instead, the flow is
        # configured with the client ID, client secret, and scopes.
        self.flow = Flow.from_client_config(
            client_config={
                "web": {
                    "client_id": self.client_id or "",
                    "client_secret": self.client_secret or "",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [self.redirect_uri],
                }
            },
            scopes=[
                "https://www.googleapis.com/auth/userinfo.profile",
                "https://www.googleapis.com/auth/userinfo.email",
                "openid",
            ],
            redirect_uri=self.redirect_uri,
        )

    def update_redirect(self, new_redirect_uri: str):
        """Update redirect URI at runtime to match current host/port.
        Needed so that the callback uses the exact same host (e.g. localhost vs 127.0.0.1)
        that the user is accessing in the parent window, ensuring cookie sharing.
        """
        if not new_redirect_uri or new_redirect_uri == self.redirect_uri:
            return
        self.redirect_uri = new_redirect_uri
        # Rebuild flow with updated redirect to ensure Google's library uses it
        self.flow.redirect_uri = self.redirect_uri
        try:
            client_config = getattr(self.flow, "client_config", None)
            if isinstance(client_config, dict):
                web_config = client_config.setdefault("web", {})
                if isinstance(web_config, dict):
                    web_config["redirect_uris"] = [self.redirect_uri]
        except Exception:
            # Best-effort update; fall back to flow.redirect_uri if client_config missing.
            pass
        # Update canonical origin as well
        parsed_redirect = urlparse(self.redirect_uri)
        if parsed_redirect.scheme and parsed_redirect.netloc:
            self.canonical_origin = (
                f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"
            )
            self._canonical_netloc = parsed_redirect.netloc.casefold()
        else:
            self.canonical_origin = None
            self._canonical_netloc = None

    def request_host_matches_canonical_origin(self, host: str) -> bool:
        """Return whether ``host`` exactly names the configured OAuth origin."""
        return bool(
            host
            and self._canonical_netloc
            and host.casefold() == self._canonical_netloc
        )

    def canonicalise_authorization_response_url(self, request_url: str) -> str:
        """Restore the configured external scheme after trusted TLS termination.

        The rewrite is deliberately limited to the configured callback path on
        the exact configured host. Requests for any other host or path retain
        their observed URL and continue through the existing failure path.
        """
        configured = urlparse(self.redirect_uri)
        observed = urlparse(request_url)
        if (
            not self.request_host_matches_canonical_origin(observed.netloc)
            or observed.path != configured.path
        ):
            return request_url
        return observed._replace(
            scheme=configured.scheme,
            netloc=configured.netloc,
        ).geturl()

    def allows_dynamic_host(self, host: str) -> bool:
        """Return True when dynamic redirects are enabled and the host is trusted."""
        if not self.dynamic_redirects_enabled or not host:
            return False
        host_only = host.split(":", 1)[0].lower()
        return any(
            host_only.endswith(suffix) for suffix in self.allowed_dynamic_suffixes
        )

    def build_redirect_for_host(self, scheme: str, host: str) -> str:
        """Construct a redirect URI for the provided scheme/host using the default path."""
        effective_scheme = scheme or urlparse(self.redirect_uri).scheme or "https"
        return f"{effective_scheme}://{host}{self._redirect_path}{self._redirect_query}"

    def get_authorization_url(self):
        """Generates the Google authorization URL."""
        authorization_url, state = self.flow.authorization_url(
            access_type="offline", include_granted_scopes="true"
        )
        return authorization_url, state

    def exchange_code_for_tokens(self, authorization_response_url: str):
        """Exchanges the authorization code for tokens."""
        @contextmanager
        def _temporary_env(var_name: str, value: str):
            previous = os.environ.get(var_name)
            os.environ[var_name] = value
            try:
                yield
            finally:
                if previous is None:
                    os.environ.pop(var_name, None)
                else:
                    os.environ[var_name] = previous

        # Google can return a token scope set that is a superset of requested scopes
        # (for example when include_granted_scopes is enabled). Accept this rather
        # than failing the callback with a warning-style exception.
        with _temporary_env("OAUTHLIB_RELAX_TOKEN_SCOPE", "1"):
            self.flow.fetch_token(authorization_response=authorization_response_url)
        credentials = self.flow.credentials
        # Cast so static type checkers know we expect an OAuth credentials object.
        # Help static analysis: treat credentials as at least our protocol
        try:
            credentials = cast(CredentialsProtocol, credentials)
        except Exception:  # pragma: no cover
            pass
        # Safely obtain id_token attribute (pyright previously flagged direct access).
        raw_id_token = getattr(credentials, "id_token", None)
        if not isinstance(raw_id_token, str) or not raw_id_token:
            raise RuntimeError(
                "Missing id_token on OAuth credentials after token exchange"
            )
        id_info = id_token.verify_oauth2_token(
            id_token=raw_id_token,
            request=Request(),
            audience=self.client_id,
            # Allow a tiny tolerance for clock drift to avoid 'Token used too early'.
            clock_skew_in_seconds=self.clock_skew_seconds,
        )
        return id_info
