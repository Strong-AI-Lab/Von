"""Gmail utility layer for MCP/email workflows.

Provides profile-based access to Gmail with read-focused helpers suitable
for Von's agent and user mailboxes. Profiles are loaded from environment
variables to avoid hardcoding credentials. The helpers are intentionally
read-only and require explicit opt-in for any mutation scopes.
"""

from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

DEFAULT_SCOPES: List[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

MUTATION_SCOPE: str = "https://www.googleapis.com/auth/gmail.modify"

PROFILES_ENV_VAR = "VON_GMAIL_PROFILES"


@dataclass
class GmailProfile:
    """Configuration for a Gmail access profile."""

    profile_id: str
    token_path: str
    credentials_path: str = ""
    user_id: str = "me"
    scopes: List[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    label_filter: Optional[List[str]] = None
    query_prefix: Optional[str] = None

    def ensure_credentials(self) -> Credentials:
        """Load and refresh credentials from the token file.

        The helper never launches interactive OAuth flows. If the token file is
        missing or cannot be refreshed, the caller must provision a token
        out-of-band to avoid unexpected prompts in headless environments.
        """

        if not self.token_path:
            raise ValueError("token_path is required for Gmail profile")

        if not os.path.exists(self.token_path):
            raise FileNotFoundError(
                f"Token file not found for profile '{self.profile_id}': {self.token_path}"
            )

        creds = Credentials.from_authorized_user_file(
            self.token_path, scopes=self.scopes
        )

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _persist_credentials(self.token_path, creds)
        elif not creds or not creds.valid:
            raise RuntimeError(
                "Invalid or non-refreshable Gmail credentials. Provide a valid token file"
                f" for profile '{self.profile_id}'."
            )

        return creds


def _persist_credentials(token_path: str, creds: Credentials) -> None:
    with open(token_path, "w", encoding="utf-8") as token_file:
        token_file.write(creds.to_json())


def _log_gmail_audit(
    action: str,
    *,
    profile_id: str,
    audit_context: Optional[Mapping[str, object]] = None,
    query: Optional[str] = None,
    label_ids: Optional[List[str]] = None,
    message_id: Optional[str] = None,
    attachment_id: Optional[str] = None,
    allow_mutation: Optional[bool] = None,
    max_results: Optional[int] = None,
) -> None:
    """Record a minimal audit trail for Gmail tool calls without leaking content.

    Only structural identifiers and small metadata are logged; message bodies and
    attachments are never logged.
    """

    try:
        safe_labels = list(label_ids[:10]) if label_ids else None
        trimmed_query = (query or "")[:100] if query else None
        record: Dict[str, object] = {
            "profile_id": profile_id,
            "query_preview": trimmed_query,
            "label_ids": safe_labels,
            "message_id": message_id,
            "attachment_id": attachment_id,
            "allow_mutation": allow_mutation,
            "max_results": max_results,
        }
        record = {k: v for k, v in record.items() if v is not None}
        if audit_context:
            record["audit_context"] = {
                k: v for k, v in audit_context.items() if v is not None
            }
        logger.info("[gmail_audit] action=%s payload=%s", action, record)
    except Exception:  # pragma: no cover - audit should never break callers
        logger.exception("[gmail_audit] Failed to log Gmail action: %s", action)


def _parse_profiles(raw: str) -> Dict[str, GmailProfile]:
    parsed = json.loads(raw)
    profiles: Dict[str, GmailProfile] = {}

    if isinstance(parsed, dict):
        entries: Iterable = parsed.values()
    elif isinstance(parsed, list):
        entries = parsed
    else:
        raise ValueError("Profiles payload must be a list or object")

    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each profile entry must be an object")

        profile_id = entry.get("profile_id") or entry.get("id")
        token_path = entry.get("token_path") or entry.get("token")
        if not profile_id or not token_path:
            raise ValueError("Each profile requires profile_id and token_path")

        credentials_path = entry.get("credentials_path") or entry.get(
            "client_secret_path", ""
        )
        scopes = entry.get("scopes") or list(DEFAULT_SCOPES)
        label_filter = entry.get("label_filter")
        query_prefix = entry.get("query_prefix")

        profiles[profile_id] = GmailProfile(
            profile_id=profile_id,
            token_path=token_path,
            credentials_path=credentials_path,
            user_id=entry.get("user_id", "me"),
            scopes=scopes,
            label_filter=label_filter if isinstance(label_filter, list) else None,
            query_prefix=query_prefix,
        )

    return profiles


def load_profiles_from_env() -> Dict[str, GmailProfile]:
    """Load Gmail profiles from environment variables.

    Preferred: VON_GMAIL_PROFILES containing JSON (list or object). Each entry
    should specify at minimum profile_id and token_path. Optional fields:
    credentials_path, user_id, scopes, label_filter, query_prefix.

    Fallback: VON_GMAIL_TOKEN_PATH (mandatory) and optional
    VON_GMAIL_CLIENT_SECRET_PATH / GOOGLE_CLIENT_SECRET_PATH,
    VON_GMAIL_USER, VON_GMAIL_LABELS (comma-separated),
    VON_GMAIL_QUERY_PREFIX. This populates a single profile named
    "von-service".
    """

    env_profiles = os.getenv(PROFILES_ENV_VAR)
    if env_profiles:
        return _parse_profiles(env_profiles)

    token_path = os.getenv("VON_GMAIL_TOKEN_PATH")
    if not token_path:
        return {}

    labels_raw = os.getenv("VON_GMAIL_LABELS")
    label_filter = (
        [label.strip() for label in labels_raw.split(",") if label.strip()]
        if labels_raw
        else None
    )

    return {
        "von-service": GmailProfile(
            profile_id="von-service",
            token_path=token_path,
            credentials_path=os.getenv("VON_GMAIL_CLIENT_SECRET_PATH")
            or os.getenv("GOOGLE_CLIENT_SECRET_PATH")
            or "",
            user_id=os.getenv("VON_GMAIL_USER", "me"),
            scopes=_pick_scopes(os.getenv("VON_GMAIL_MUTATION", "false")),
            label_filter=label_filter,
            query_prefix=os.getenv("VON_GMAIL_QUERY_PREFIX"),
        )
    }


def _pick_scopes(request_mutation: str) -> List[str]:
    wants_mutation = request_mutation.lower() in {"1", "true", "yes"}
    if wants_mutation:
        return [MUTATION_SCOPE, *DEFAULT_SCOPES]
    return list(DEFAULT_SCOPES)


def get_profile(
    profile_id: str, profiles: Optional[Dict[str, GmailProfile]] = None
) -> GmailProfile:
    candidates = profiles or load_profiles_from_env()
    if not candidates:
        raise RuntimeError(
            "No Gmail profiles configured. Set VON_GMAIL_PROFILES or VON_GMAIL_TOKEN_PATH."
        )

    profile = candidates.get(profile_id)
    if not profile:
        raise KeyError(
            f"Profile '{profile_id}' not found. Available: {sorted(candidates.keys())}"
        )
    return profile


def get_service(profile_id: str, profiles: Optional[Dict[str, GmailProfile]] = None):
    profile = get_profile(profile_id, profiles)
    creds = profile.ensure_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _compose_query(profile: GmailProfile, query: Optional[str]) -> Optional[str]:
    parts = []
    if profile.query_prefix:
        parts.append(profile.query_prefix)
    if query:
        parts.append(query)
    if not parts:
        return None
    return " ".join(parts)


def list_messages(
    profile_id: str,
    query: Optional[str] = None,
    label_ids: Optional[List[str]] = None,
    max_results: int = 25,
    profiles: Optional[Dict[str, GmailProfile]] = None,
    audit_context: Optional[Mapping[str, object]] = None,
) -> Dict:
    profile = get_profile(profile_id, profiles)
    _log_gmail_audit(
        "list_messages",
        profile_id=profile.profile_id,
        audit_context=audit_context,
        query=query,
        label_ids=label_ids,
        max_results=max_results,
    )
    service = get_service(profile_id, profiles)

    label_ids = label_ids or profile.label_filter or None
    composed_query = _compose_query(profile, query)

    request = (
        service.users()
        .messages()
        .list(
            userId=profile.user_id,
            q=composed_query,
            labelIds=label_ids,
            maxResults=max_results,
        )
    )
    return request.execute() or {}


def get_message(
    profile_id: str,
    message_id: str,
    format: str = "metadata",
    profiles: Optional[Dict[str, GmailProfile]] = None,
    audit_context: Optional[Mapping[str, object]] = None,
) -> Dict:
    profile = get_profile(profile_id, profiles)
    _log_gmail_audit(
        "get_message",
        profile_id=profile.profile_id,
        audit_context=audit_context,
        message_id=message_id,
    )
    service = get_service(profile_id, profiles)

    request = (
        service.users()
        .messages()
        .get(userId=profile.user_id, id=message_id, format=format)
    )
    return request.execute() or {}


def get_attachment(
    profile_id: str,
    message_id: str,
    attachment_id: str,
    profiles: Optional[Dict[str, GmailProfile]] = None,
    audit_context: Optional[Mapping[str, object]] = None,
) -> Dict:
    profile = get_profile(profile_id, profiles)
    _log_gmail_audit(
        "get_attachment",
        profile_id=profile.profile_id,
        audit_context=audit_context,
        message_id=message_id,
        attachment_id=attachment_id,
    )
    service = get_service(profile_id, profiles)

    request = (
        service.users()
        .messages()
        .attachments()
        .get(userId=profile.user_id, messageId=message_id, id=attachment_id)
    )
    return request.execute() or {}


def list_labels(
    profile_id: str,
    profiles: Optional[Dict[str, GmailProfile]] = None,
    audit_context: Optional[Mapping[str, object]] = None,
) -> Dict:
    profile = get_profile(profile_id, profiles)
    _log_gmail_audit(
        "list_labels",
        profile_id=profile.profile_id,
        audit_context=audit_context,
    )
    service = get_service(profile_id, profiles)

    request = service.users().labels().list(userId=profile.user_id)
    return request.execute() or {}


def modify_labels(
    profile_id: str,
    message_id: str,
    add_labels: Optional[List[str]] = None,
    remove_labels: Optional[List[str]] = None,
    allow_mutation: bool = False,
    profiles: Optional[Dict[str, GmailProfile]] = None,
    audit_context: Optional[Mapping[str, object]] = None,
) -> Dict:
    """Modify labels on a message when mutation is explicitly enabled.

    To avoid accidental writes, callers must set allow_mutation=True and ensure
    the profile includes the gmail.modify scope. Otherwise, a ValueError is
    raised. This function does not mark read/unread implicitly; callers choose
    labels (e.g., 'UNREAD').
    """

    if not allow_mutation:
        raise ValueError("Label mutation requires allow_mutation=True")

    profile = get_profile(profile_id, profiles)
    _log_gmail_audit(
        "modify_labels",
        profile_id=profile.profile_id,
        audit_context=audit_context,
        message_id=message_id,
        label_ids=(add_labels or []) + (remove_labels or []),
        allow_mutation=allow_mutation,
    )
    if MUTATION_SCOPE not in profile.scopes:
        raise PermissionError("Profile scopes do not include gmail.modify")

    service = get_service(profile_id, profiles)
    request = (
        service.users()
        .messages()
        .modify(
            userId=profile.user_id,
            id=message_id,
            body={
                "addLabelIds": add_labels or [],
                "removeLabelIds": remove_labels or [],
            },
        )
    )
    return request.execute() or {}
