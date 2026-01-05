from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from google_auth_oauthlib.flow import Flow

if TYPE_CHECKING:  # pragma: no cover
    from google.oauth2.credentials import Credentials

from googleapiclient.discovery import build

from ..integrations.google.gmail_service import (
    GmailProfile,
    get_profile,
    load_profiles_from_env,
)
from .agent_gmail_token_store import upsert_agent_gmail_tokens

logger = logging.getLogger(__name__)


DEFAULT_AGENT_GMAIL_OAUTH_REDIRECT_URI = (
    "http://localhost:5000/von/api/agent/gmail/oauth/callback"
)

AGENT_GMAIL_OAUTH_REDIRECT_URI_ENV_VAR = "VON_AGENT_GMAIL_OAUTH_REDIRECT_URI"
AGENT_GMAIL_OAUTH_CLIENT_SECRET_PATH_ENV_VAR = (
    "VON_AGENT_GMAIL_OAUTH_CLIENT_SECRET_PATH"
)
AGENT_GMAIL_OAUTH_PROMPT_ENV_VAR = "VON_AGENT_GMAIL_OAUTH_PROMPT"


@dataclass(frozen=True)
class AgentGmailOAuthResult:
    profile_id: str
    authorised_email: Optional[str]
    scopes: list[str]
    expires_at: Optional[datetime]


class AgentGmailOAuthError(RuntimeError):
    pass


class AgentGmailOAuthService:
    """Runs a dedicated Gmail-scoped OAuth flow for agent mailbox access.

    This is intentionally separate from the existing user login OAuth flow.
    """

    def __init__(self, *, profiles: Optional[dict[str, GmailProfile]] = None):
        self._profiles = profiles

    def _get_profile(self, profile_id: str) -> GmailProfile:
        return get_profile(profile_id, self._profiles or load_profiles_from_env())

    def _get_redirect_uri(self) -> str:
        return (
            os.getenv(AGENT_GMAIL_OAUTH_REDIRECT_URI_ENV_VAR)
            or DEFAULT_AGENT_GMAIL_OAUTH_REDIRECT_URI
        )

    def _get_client_secret_path(self, *, profile_id: str) -> str:
        override = os.getenv(AGENT_GMAIL_OAUTH_CLIENT_SECRET_PATH_ENV_VAR)
        if override:
            return override

        profile = self._get_profile(profile_id)
        if getattr(profile, "credentials_path", ""):
            return profile.credentials_path

        raise AgentGmailOAuthError(
            "No client secret JSON path configured. Set VON_AGENT_GMAIL_OAUTH_CLIENT_SECRET_PATH "
            "or ensure the Gmail profile includes credentials_path."
        )

    def _build_flow(self, *, profile_id: str) -> Flow:
        profile = self._get_profile(profile_id)
        redirect_uri = self._get_redirect_uri()
        secret_path = self._get_client_secret_path(profile_id=profile_id)

        if not os.path.isfile(secret_path):
            raise AgentGmailOAuthError(
                f"Client secret JSON not found for profile '{profile_id}': {secret_path}"
            )

        return Flow.from_client_secrets_file(
            secret_path,
            scopes=list(getattr(profile, "scopes", []) or []),
            redirect_uri=redirect_uri,
        )

    def get_authorisation_url(self, *, profile_id: str) -> tuple[str, str]:
        flow = self._build_flow(profile_id=profile_id)
        prompt = (
            os.getenv(AGENT_GMAIL_OAUTH_PROMPT_ENV_VAR, "consent").strip() or "consent"
        )

        authorisation_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt=prompt,
        )
        return authorisation_url, state

    def exchange_code_for_tokens(
        self, *, profile_id: str, authorisation_response_url: str
    ) -> AgentGmailOAuthResult:
        """Exchange callback code for tokens and persist via DB token store."""

        flow = self._build_flow(profile_id=profile_id)

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

        # oauthlib raises a Warning exception when the provider returns a
        # superset of the requested scopes (e.g., Google returning OIDC scopes).
        # This is benign for our use case; accept and continue.
        with _temporary_env("OAUTHLIB_RELAX_TOKEN_SCOPE", "1"):
            flow.fetch_token(authorization_response=authorisation_response_url)

        creds = flow.credentials
        if creds is None:
            raise AgentGmailOAuthError("OAuth flow produced no credentials")

        token_payload: dict[str, Any]
        try:
            token_payload = json.loads(creds.to_json())
        except Exception as exc:
            raise AgentGmailOAuthError(
                f"Failed to serialise OAuth credentials: {exc}"
            ) from exc

        authorised_email = self._get_authorised_email(creds)
        expires_at = getattr(creds, "expiry", None)

        profile = self._get_profile(profile_id)
        scopes = list(getattr(profile, "scopes", []) or [])

        upsert_agent_gmail_tokens(
            profile_id=profile_id,
            token_payload=token_payload,
            authorised_email=authorised_email,
            scopes=scopes,
            expires_at=expires_at if isinstance(expires_at, datetime) else None,
        )

        logger.info(
            "[agent_gmail_oauth] Stored tokens for profile_id=%s email=%s",
            profile_id,
            authorised_email or "(unknown)",
        )

        return AgentGmailOAuthResult(
            profile_id=profile_id,
            authorised_email=authorised_email,
            scopes=scopes,
            expires_at=expires_at if isinstance(expires_at, datetime) else None,
        )

    def _get_authorised_email(self, creds: object) -> Optional[str]:
        """Query Gmail API to determine which mailbox the token belongs to."""

        try:
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            profile = service.users().getProfile(userId="me").execute() or {}
            email = profile.get("emailAddress")
            return email if isinstance(email, str) and email else None
        except Exception:
            # Best-effort: do not block token storage if email lookup fails.
            logger.exception("[agent_gmail_oauth] Failed to fetch Gmail profile")
            return None
