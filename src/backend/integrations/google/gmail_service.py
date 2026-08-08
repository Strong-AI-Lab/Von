"""Gmail utility layer for MCP/email workflows.

Provides profile-based access to Gmail with read helpers and guarded
send/mutation helpers suitable for Von's agent and user mailboxes. Profiles are
loaded from environment variables to avoid hardcoding credentials. Read-only is
the default posture; mutations require explicit opt-in flags and OAuth scopes.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import socket
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from typing import Any, Dict, Iterable, List, Mapping, Optional

import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build
from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

try:  # Optional dependency: agent Gmail OAuth token store.
    from ...services.agent_gmail_token_store import (
        get_agent_gmail_token_payload as _get_agent_gmail_token_payload,
        get_agent_gmail_token_status as _get_agent_gmail_token_status,
        upsert_agent_gmail_tokens as _upsert_agent_gmail_tokens,
    )
except Exception:  # pragma: no cover
    _get_agent_gmail_token_payload = None  # type: ignore
    _get_agent_gmail_token_status = None  # type: ignore
    _upsert_agent_gmail_tokens = None  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_SCOPES: List[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

MUTATION_SCOPE: str = "https://www.googleapis.com/auth/gmail.modify"
SEND_SCOPE: str = "https://www.googleapis.com/auth/gmail.send"
COMPOSE_SCOPE: str = "https://www.googleapis.com/auth/gmail.compose"
FULL_MAIL_SCOPE: str = "https://mail.google.com/"
SEND_CAPABLE_SCOPES: set[str] = {
    SEND_SCOPE,
    COMPOSE_SCOPE,
    MUTATION_SCOPE,
    FULL_MAIL_SCOPE,
}
LABEL_LIST_VISIBILITY_VALUES: set[str] = {
    "labelShow",
    "labelShowIfUnread",
    "labelHide",
}
MESSAGE_LIST_VISIBILITY_VALUES: set[str] = {
    "show",
    "hide",
}

PROFILES_ENV_VAR = "VON_GMAIL_PROFILES"


def _create_connection_prefer_ipv4(
    address,
    timeout=None,
    source_address=None,
    *,
    all_errors: bool = False,
):
    """Create a connection with IPv4-first, IPv6-fallback address ordering.

    Python's standard HTTP transports try ``getaddrinfo`` results serially.
    On dual-stack hosts with a non-functional IPv6 route, an IPv6-first DNS
    answer can therefore consume the entire socket timeout before a reachable
    IPv4 address is attempted. Keep this policy local to Google API transport;
    do not mutate process-wide resolver or socket defaults.
    """

    host, port = address
    address_info = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    address_info.sort(key=lambda row: 0 if row[0] == socket.AF_INET else 1)
    exceptions: list[OSError] = []

    for family, socktype, proto, _canonname, sockaddr in address_info:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            exceptions.clear()
            return sock
        except OSError as exc:
            if not all_errors:
                exceptions.clear()
            exceptions.append(exc)
            if sock is not None:
                sock.close()

    if exceptions:
        try:
            if not all_errors:
                raise exceptions[0]
            raise ExceptionGroup("create_connection failed", exceptions)
        finally:
            exceptions.clear()
    raise OSError("getaddrinfo returned no addresses")


class _GoogleApiHTTPSConnection(httplib2.HTTPSConnectionWithTimeout):
    """HTTPS connection with bounded, local dual-stack fallback semantics."""

    def connect(self):
        proxy_info = self.proxy_info
        if proxy_info and proxy_info.isgood() and proxy_info.applies_to(self.host):
            return super().connect()

        original_create_connection = self._create_connection
        self._create_connection = _create_connection_prefer_ipv4
        try:
            return http.client.HTTPSConnection.connect(self)
        finally:
            self._create_connection = original_create_connection


class _GoogleApiHttp(httplib2.Http):
    """Google API HTTP transport using the local dual-stack connection."""

    def request(
        self,
        uri,
        method="GET",
        body=None,
        headers=None,
        redirections=httplib2.DEFAULT_MAX_REDIRECTS,
        connection_type=None,
    ):
        if connection_type is None and str(uri).lower().startswith("https://"):
            connection_type = _GoogleApiHTTPSConnection
        return super().request(
            uri,
            method=method,
            body=body,
            headers=headers,
            redirections=redirections,
            connection_type=connection_type,
        )


def _build_authorized_google_http(creds: Credentials):
    http_transport = _GoogleApiHttp(timeout=DEFAULT_HTTP_TIMEOUT_SEC)
    return google_auth_httplib2.AuthorizedHttp(creds, http=http_transport)


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

        effective_scopes = resolve_effective_profile_scopes(self)

        if callable(_get_agent_gmail_token_payload):
            try:
                token_payload = _get_agent_gmail_token_payload(self.profile_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[gmail_service] DB Gmail token lookup failed for profile %s: %s",
                    self.profile_id,
                    exc,
                )
                token_payload = None
            if token_payload:
                creds = Credentials.from_authorized_user_info(
                    token_payload, scopes=effective_scopes
                )

                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    if callable(_upsert_agent_gmail_tokens):
                        authorised_email = None
                        if callable(_get_agent_gmail_token_status):
                            status = _get_agent_gmail_token_status(self.profile_id)
                            authorised_email = status.authorised_email
                        _upsert_agent_gmail_tokens(
                            profile_id=self.profile_id,
                            token_payload=json.loads(creds.to_json()),
                            authorised_email=authorised_email,
                            scopes=list(effective_scopes),
                            expires_at=getattr(creds, "expiry", None),
                        )
                elif not creds or not creds.valid:
                    raise RuntimeError(
                        "Invalid or non-refreshable Gmail credentials stored for "
                        f"profile '{self.profile_id}'."
                    )

                return creds

        if not self.token_path:
            raise ValueError("token_path is required for Gmail profile")

        if not os.path.exists(self.token_path):
            raise FileNotFoundError(
                f"Token file not found for profile '{self.profile_id}': {self.token_path}"
            )

        creds = Credentials.from_authorized_user_file(
            self.token_path, scopes=effective_scopes
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


def _resolve_vontology_profile_scopes(profile_id: str) -> list[str] | None:
    """Read represented OAuth scopes for a Gmail profile, if available."""

    try:
        from ...services import concept_service
        from ...services.mail_profile_resource_vontology_service import (
            gmail_profile_resource_concept_id,
        )

        concept_doc = concept_service.get_concept_by_concept_id(
            gmail_profile_resource_concept_id(profile_id)
        )
        if not isinstance(concept_doc, Mapping):
            return None
        raw_scopes = (concept_doc.get("attributes") or {}).get("oauth_scopes")
        if not isinstance(raw_scopes, list):
            return None
        scopes = [scope for scope in raw_scopes if isinstance(scope, str) and scope]
        return scopes or None
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[gmail_service] Vontology scope lookup failed for profile %s: %s",
            profile_id,
            exc,
        )
        return None


def resolve_effective_profile_scopes(profile: GmailProfile) -> list[str]:
    """Return represented OAuth scopes, falling back to env profile scopes."""

    represented_scopes = _resolve_vontology_profile_scopes(profile.profile_id)
    if represented_scopes:
        return represented_scopes
    return list(profile.scopes or [])


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
    label_name: Optional[str] = None,
    label_list_visibility: Optional[str] = None,
    message_list_visibility: Optional[str] = None,
    message_id: Optional[str] = None,
    attachment_id: Optional[str] = None,
    allow_mutation: Optional[bool] = None,
    max_results: Optional[int] = None,
    recipient_count: Optional[int] = None,
) -> None:
    """Record a minimal audit trail for Gmail tool calls without leaking content.

    Only structural identifiers and small metadata are logged; message bodies and
    attachments are never logged.
    """

    try:
        safe_labels = list(label_ids[:10]) if label_ids else None
        record: Dict[str, object] = {
            "profile_id": profile_id,
            "query_length": len(query) if query else None,
            "label_ids": safe_labels,
            "label_name": label_name,
            "label_list_visibility": label_list_visibility,
            "message_list_visibility": message_list_visibility,
            "message_id": message_id,
            "attachment_id": attachment_id,
            "allow_mutation": allow_mutation,
            "max_results": max_results,
            "recipient_count": recipient_count,
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


def list_profile_ids_from_env() -> List[str]:
    """Return configured Gmail profile IDs without exposing sensitive paths."""

    try:
        profiles = load_profiles_from_env()
    except Exception as exc:
        logger.warning("[gmail_service] Failed to load Gmail profiles: %s", exc)
        return []

    return sorted(profiles.keys())


def _resolve_profile_by_authorised_email(
    candidates: Dict[str, GmailProfile], email: str
) -> Optional[GmailProfile]:
    """Best-effort resolution from authorised Gmail address to configured profile.

    Consults the agent Gmail token store (``agent_gmail_tokens``) for each
    configured profile and returns the first profile whose stored
    ``authorised_email`` matches ``email`` case-insensitively. Returns ``None``
    if the token store is unavailable or no profile matches.
    """

    if not callable(_get_agent_gmail_token_status):
        return None

    target = email.strip().lower()
    if "@" not in target:
        return None

    for profile_id, profile in candidates.items():
        try:
            status = _get_agent_gmail_token_status(profile_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "[gmail_service] token status lookup failed for %s: %s",
                profile_id,
                exc,
            )
            continue
        authorised = (status.authorised_email or "").strip().lower()
        if authorised and authorised == target:
            return profile
    return None


def list_profile_summaries(
    profiles: Optional[Dict[str, GmailProfile]] = None,
) -> List[Dict[str, Optional[str]]]:
    """Return non-sensitive summaries of configured Gmail profiles.

    Each entry contains ``profile_id`` and the ``authorised_email`` from the
    token store when available. Suitable for surfacing in tool descriptions
    and discovery surfaces; never exposes secrets or token paths.
    """

    candidates = profiles or load_profiles_from_env()
    summaries: List[Dict[str, Optional[str]]] = []
    for profile_id in sorted(candidates.keys()):
        authorised: Optional[str] = None
        if callable(_get_agent_gmail_token_status):
            try:
                authorised = _get_agent_gmail_token_status(profile_id).authorised_email
            except Exception:  # pragma: no cover - defensive
                authorised = None
        summaries.append({"profile_id": profile_id, "authorised_email": authorised})
    return summaries


def get_profile(
    profile_id: str, profiles: Optional[Dict[str, GmailProfile]] = None
) -> GmailProfile:
    candidates = profiles or load_profiles_from_env()
    if not candidates:
        raise RuntimeError(
            "No Gmail profiles configured. Set VON_GMAIL_PROFILES or VON_GMAIL_TOKEN_PATH."
        )

    profile = candidates.get(profile_id)
    if profile:
        return profile

    # Allow callers (notably LLMs) to pass the authorised Gmail address itself
    # rather than the configured alias.
    resolved = _resolve_profile_by_authorised_email(candidates, profile_id)
    if resolved is not None:
        return resolved

    summaries = list_profile_summaries(candidates)
    available_aliases = sorted(candidates.keys())
    known_emails = sorted(
        {
            (s.get("authorised_email") or "").lower()
            for s in summaries
            if s.get("authorised_email")
        }
    )
    raise KeyError(
        f"Profile '{profile_id}' not found. "
        f"Available aliases: {available_aliases}. "
        f"Known authorised emails: {known_emails}."
    )


def get_service(profile_id: str, profiles: Optional[Dict[str, GmailProfile]] = None):
    profile = get_profile(profile_id, profiles)
    creds = profile.ensure_credentials()
    return build(
        "gmail",
        "v1",
        http=_build_authorized_google_http(creds),
        cache_discovery=False,
    )


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
    bypass_profile_query_prefix: bool = False,
) -> Dict:
    """List Gmail messages for ``profile_id``.

    When ``bypass_profile_query_prefix`` is True, the profile's configured
    ``query_prefix`` and ``label_filter`` are not applied; only caller-supplied
    ``query`` / ``label_ids`` constrain the listing. This lets callers obtain
    an unfiltered mailbox view when the profile-level filter would silently
    exclude relevant mail (e.g. mailing-list deliveries that do not match a
    ``to:``/``from:`` prefix).
    """

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

    if bypass_profile_query_prefix:
        effective_label_ids = list(label_ids) if label_ids else None
        composed_query = query or None
    else:
        effective_label_ids = label_ids or profile.label_filter or None
        composed_query = _compose_query(profile, query)

    request = (
        service.users()
        .messages()
        .list(
            userId=profile.user_id,
            q=composed_query,
            labelIds=effective_label_ids,
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


def decode_attachment_bytes(attachment: Mapping[str, Any]) -> bytes:
    """Decode Gmail's base64url attachment payload into source bytes."""

    raw_data = attachment.get("data")
    if not isinstance(raw_data, str) or not raw_data.strip():
        raise ValueError("Gmail attachment response did not contain data")
    compact = "".join(raw_data.split())
    padding = "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(
            (compact + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(
            "Gmail attachment response contained invalid base64url data"
        ) from exc


def find_attachment_part_metadata(
    message: Mapping[str, Any],
    *,
    attachment_id: str,
) -> Dict[str, Any]:
    """Return trusted MIME metadata for one attachment in a full Gmail message."""

    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return {}

    pending: list[Mapping[str, Any]] = [payload]
    while pending:
        part = pending.pop()
        children = part.get("parts")
        if isinstance(children, list):
            pending.extend(
                child for child in reversed(children) if isinstance(child, Mapping)
            )

        body = part.get("body")
        if not isinstance(body, Mapping) or body.get("attachmentId") != attachment_id:
            continue

        metadata: Dict[str, Any] = {"attachment_id": attachment_id}
        filename = part.get("filename")
        if isinstance(filename, str) and filename.strip():
            metadata["filename"] = filename.strip()
        mime_type = part.get("mimeType")
        if isinstance(mime_type, str) and mime_type.strip():
            metadata["content_type"] = mime_type.strip().lower()
        size = body.get("size")
        if isinstance(size, int) and size >= 0:
            metadata["reported_size_bytes"] = size
        return metadata

    return {}


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


def _normalise_optional_visibility(
    value: str | None,
    *,
    field_name: str,
    allowed_values: set[str],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string when provided")
    normalised = value.strip()
    if normalised not in allowed_values:
        allowed = ", ".join(sorted(allowed_values))
        raise ValueError(f"{field_name} must be one of: {allowed}")
    return normalised


def create_label(
    profile_id: str,
    name: str,
    *,
    label_list_visibility: str | None = None,
    message_list_visibility: str | None = None,
    allow_mutation: bool = False,
    profiles: Optional[Dict[str, GmailProfile]] = None,
    audit_context: Optional[Mapping[str, object]] = None,
) -> Dict:
    """Create a Gmail label for a configured profile.

    This is an additive external-system write. Callers must set
    ``allow_mutation=True`` after explicit user request or workflow authority,
    and the profile must include the Gmail modify scope.
    """

    if not allow_mutation:
        raise ValueError("Label creation requires allow_mutation=True")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")

    label_name = name.strip()
    label_list_visibility = _normalise_optional_visibility(
        label_list_visibility,
        field_name="label_list_visibility",
        allowed_values=LABEL_LIST_VISIBILITY_VALUES,
    )
    message_list_visibility = _normalise_optional_visibility(
        message_list_visibility,
        field_name="message_list_visibility",
        allowed_values=MESSAGE_LIST_VISIBILITY_VALUES,
    )

    profile = get_profile(profile_id, profiles)
    if MUTATION_SCOPE not in resolve_effective_profile_scopes(profile):
        raise PermissionError("Profile scopes do not include gmail.modify")

    _log_gmail_audit(
        "create_label",
        profile_id=profile.profile_id,
        audit_context=audit_context,
        label_name=label_name,
        label_list_visibility=label_list_visibility,
        message_list_visibility=message_list_visibility,
        allow_mutation=allow_mutation,
    )

    body: dict[str, Any] = {"name": label_name}
    if label_list_visibility is not None:
        body["labelListVisibility"] = label_list_visibility
    if message_list_visibility is not None:
        body["messageListVisibility"] = message_list_visibility

    service = get_service(profile_id, profiles)
    result = (
        service.users()
        .labels()
        .create(userId=profile.user_id, body=body)
        .execute()
        or {}
    )
    payload = dict(result)
    label_id = payload.get("id")
    if isinstance(label_id, str) and label_id.strip():
        payload.setdefault("label_id", label_id.strip())
    payload.setdefault("name", label_name)
    payload.setdefault("profile", profile.profile_id)
    payload.setdefault("created", True)
    return payload


def _normalise_address_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    raw_values: list[str] = []
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, list):
        raw_values = [item for item in value if isinstance(item, str)]
    else:
        raise ValueError(f"{field_name} must be a string or list of strings")

    parsed = getaddresses(raw_values)
    addresses: list[str] = []
    for display_name, address in parsed:
        clean_address = address.strip()
        if not clean_address:
            continue
        if display_name:
            addresses.append(formataddr((display_name.strip(), clean_address)))
        else:
            addresses.append(clean_address)
    return addresses


def _build_raw_message(
    *,
    to: list[str],
    subject: str,
    body_text: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: list[str] | None = None,
    body_html: str | None = None,
) -> str:
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    if reply_to:
        message["Reply-To"] = ", ".join(reply_to)

    message.set_content(body_text)
    if isinstance(body_html, str) and body_html.strip():
        message.add_alternative(body_html, subtype="html")

    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def send_message(
    profile_id: str,
    to: str | list[str],
    subject: str,
    body_text: str,
    *,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    reply_to: str | list[str] | None = None,
    body_html: str | None = None,
    allow_send: bool = False,
    profiles: Optional[Dict[str, GmailProfile]] = None,
    audit_context: Optional[Mapping[str, object]] = None,
) -> Dict:
    """Send an email through a configured Gmail profile.

    Callers must set ``allow_send=True`` after an explicit user request or an
    authorised workflow gate. The helper also checks that the profile declares a
    Gmail scope accepted by ``users.messages.send`` before invoking the API.
    """

    if not allow_send:
        raise ValueError("Sending email requires allow_send=True")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("subject is required")
    if not isinstance(body_text, str) or not body_text.strip():
        raise ValueError("body_text is required")

    to_addresses = _normalise_address_list(to, "to")
    cc_addresses = _normalise_address_list(cc, "cc")
    bcc_addresses = _normalise_address_list(bcc, "bcc")
    reply_to_addresses = _normalise_address_list(reply_to, "reply_to")
    if not to_addresses:
        raise ValueError("At least one recipient is required in to")

    profile = get_profile(profile_id, profiles)
    profile_scopes = set(resolve_effective_profile_scopes(profile))
    if not profile_scopes.intersection(SEND_CAPABLE_SCOPES):
        raise PermissionError(
            "Profile scopes do not include a Gmail send-capable scope"
        )

    recipient_count = len(to_addresses) + len(cc_addresses) + len(bcc_addresses)
    _log_gmail_audit(
        "send_message",
        profile_id=profile.profile_id,
        audit_context=audit_context,
        allow_mutation=allow_send,
        recipient_count=recipient_count,
    )

    raw_message = _build_raw_message(
        to=to_addresses,
        cc=cc_addresses,
        bcc=bcc_addresses,
        reply_to=reply_to_addresses,
        subject=subject.strip(),
        body_text=body_text,
        body_html=body_html,
    )

    service = get_service(profile_id, profiles)
    result = (
        service.users()
        .messages()
        .send(userId=profile.user_id, body={"raw": raw_message})
        .execute()
        or {}
    )
    payload = dict(result)
    message_id = payload.get("id")
    if isinstance(message_id, str) and message_id.strip():
        payload.setdefault("message_id", message_id.strip())
    payload.setdefault("profile", profile.profile_id)
    payload.setdefault("to", to_addresses)
    if cc_addresses:
        payload.setdefault("cc", cc_addresses)
    if bcc_addresses:
        payload.setdefault("bcc_count", len(bcc_addresses))
    payload.setdefault("recipient_count", recipient_count)
    payload.setdefault("subject", subject.strip())
    return payload


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
    if MUTATION_SCOPE not in resolve_effective_profile_scopes(profile):
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
