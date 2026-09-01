import base64
import hashlib
import json
import socket
from datetime import datetime, timezone
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_email_policy
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import mongomock
import pytest

from src.backend.integrations.google import gmail_service as gs
from src.backend.services import gmail_visible_disclosure_policy_service
from src.backend.services.gmail_outbound_mailbox_identity_service import (
    derive_canonical_gmail_mailbox_key,
)
from src.backend.services.settings_service import GmailOutboundRateLimitSettings


@pytest.fixture(autouse=True)
def _default_test_disclosure_policy_off(monkeypatch):
    monkeypatch.setattr(
        gmail_visible_disclosure_policy_service,
        "load_concepts",
        lambda _concept_ids: {},
    )


def _outbound_test_collections():
    database = mongomock.MongoClient().von_test
    deliveries = database.gmail_outbound_deliveries
    deliveries.create_index("delivery_fingerprint", unique=True)
    return database.gmail_outbound_quota, deliveries


def _enabled_outbound_settings(**overrides):
    values = {
        "enabled": True,
        "max_messages_per_10_minutes": 5,
        "max_messages_per_day": 25,
        "max_recipients_per_message": 10,
        "max_recipient_deliveries_per_day": 50,
    }
    values.update(overrides)
    return GmailOutboundRateLimitSettings(**values)


def _outbound_send_kwargs(
    *,
    quota_collection,
    delivery_collection,
    request_id,
    profile_resource_concept_id="#V#gmail_profile_zhan_gmail",
):
    return {
        "profile_resource_concept_id": profile_resource_concept_id,
        "request_id": request_id,
        "acting_user_concept_id": "#V#michael_witbrock",
        "organisation_concept_id": "#V#strong_ai_lab",
        "outbound_rate_limit_settings": _enabled_outbound_settings(),
        "outbound_quota_collection": quota_collection,
        "outbound_delivery_collection": delivery_collection,
    }


class _FakeSocket:
    def __init__(self, family, attempts, failures):
        self.family = family
        self.attempts = attempts
        self.failures = failures
        self.closed = False

    def settimeout(self, _timeout):
        return None

    def bind(self, _source_address):
        return None

    def connect(self, sockaddr):
        self.attempts.append((self.family, sockaddr))
        failure = self.failures.get(self.family)
        if failure is not None:
            raise failure

    def close(self):
        self.closed = True


def test_google_connection_prefers_ipv4_over_ipv6(monkeypatch):
    attempts = []
    failures = {}
    monkeypatch.setattr(
        gs.socket,
        "getaddrinfo",
        lambda *_args: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
        ],
    )
    monkeypatch.setattr(
        gs.socket,
        "socket",
        lambda family, *_args: _FakeSocket(family, attempts, failures),
    )

    sock = gs._create_connection_prefer_ipv4(  # noqa: SLF001
        ("gmail.googleapis.com", 443), 1.0
    )

    assert sock.family == socket.AF_INET
    assert attempts == [(socket.AF_INET, ("192.0.2.1", 443))]


def test_google_connection_retains_ipv6_fallback(monkeypatch):
    attempts = []
    failures = {socket.AF_INET: TimeoutError("IPv4 unavailable")}
    monkeypatch.setattr(
        gs.socket,
        "getaddrinfo",
        lambda *_args: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
        ],
    )
    monkeypatch.setattr(
        gs.socket,
        "socket",
        lambda family, *_args: _FakeSocket(family, attempts, failures),
    )

    sock = gs._create_connection_prefer_ipv4(  # noqa: SLF001
        ("gmail.googleapis.com", 443), 1.0
    )

    assert sock.family == socket.AF_INET6
    assert attempts == [
        (socket.AF_INET, ("192.0.2.1", 443)),
        (socket.AF_INET6, ("2001:db8::1", 443, 0, 0)),
    ]


def test_load_profiles_from_env_json_list(monkeypatch):
    payload = [
        {
            "profile_id": "von",
            "token_path": "/tmp/von_token.json",
            "user_id": "von_user",
            "label_filter": ["INBOX", "LABEL_X"],
            "query_prefix": "label:agent-inbox",
        },
        {
            "profile_id": "user",
            "token_path": "/tmp/user_token.json",
            "scopes": [gs.MUTATION_SCOPE],
        },
    ]
    monkeypatch.setenv(gs.PROFILES_ENV_VAR, json.dumps(payload))

    profiles = gs.load_profiles_from_env()

    assert set(profiles.keys()) == {"von", "user"}
    assert profiles["von"].label_filter == ["INBOX", "LABEL_X"]
    assert profiles["von"].query_prefix == "label:agent-inbox"
    assert profiles["user"].scopes == [gs.MUTATION_SCOPE]


def test_load_profiles_from_fallback_env(monkeypatch):
    monkeypatch.delenv(gs.PROFILES_ENV_VAR, raising=False)
    monkeypatch.setenv("VON_GMAIL_TOKEN_PATH", "/tmp/token.json")
    monkeypatch.setenv("VON_GMAIL_LABELS", "INBOX,IMPORTANT ")
    monkeypatch.setenv("VON_GMAIL_MUTATION", "true")

    profiles = gs.load_profiles_from_env()

    assert set(profiles.keys()) == {"von-service"}
    profile = profiles["von-service"]
    assert profile.label_filter == ["INBOX", "IMPORTANT"]
    assert gs.MUTATION_SCOPE in profile.scopes


def test_resolve_effective_profile_scopes_prefers_vontology(monkeypatch):
    profile = gs.GmailProfile(
        profile_id="p",
        token_path="/tmp/token.json",
        scopes=list(gs.DEFAULT_SCOPES),
    )
    monkeypatch.setattr(
        gs,
        "_resolve_vontology_profile_scopes",
        lambda profile_id: [gs.MUTATION_SCOPE],
    )

    assert gs.resolve_effective_profile_scopes(profile) == [gs.MUTATION_SCOPE]


def test_get_profile_missing(monkeypatch):
    monkeypatch.delenv(gs.PROFILES_ENV_VAR, raising=False)
    monkeypatch.delenv("VON_GMAIL_TOKEN_PATH", raising=False)
    with pytest.raises(RuntimeError):
        gs.get_profile("anything")


def test_compose_query_groups_profile_and_caller_clauses():
    profile = SimpleNamespace(query_prefix="to:owner@example.test OR from:owner@example.test")

    assert gs._compose_query(  # noqa: SLF001
        profile,
        'newer_than:2d subject:"booking confirmation"',
    ) == (
        "(to:owner@example.test OR from:owner@example.test) "
        '(newer_than:2d subject:"booking confirmation")'
    )


@patch("src.backend.integrations.google.gmail_service.build")
@patch(
    "src.backend.integrations.google.gmail_service.os.path.exists", return_value=True
)
@patch(
    "src.backend.integrations.google.gmail_service.Credentials.from_authorized_user_file"
)
def test_get_service_uses_existing_credentials(
    mock_creds_loader, mock_exists, mock_build, monkeypatch
):
    creds = MagicMock(valid=True, expired=False, refresh_token=None)
    mock_creds_loader.return_value = creds

    monkeypatch.setenv("VON_GMAIL_TOKEN_PATH", "/tmp/token.json")
    profiles = gs.load_profiles_from_env()

    service = gs.get_service(list(profiles.keys())[0], profiles)

    assert service == mock_build.return_value
    mock_build.assert_called_once()
    mock_creds_loader.assert_called_once()
    creds.refresh.assert_not_called()


@patch("src.backend.integrations.google.gmail_service._persist_credentials")
@patch("src.backend.integrations.google.gmail_service.build")
@patch(
    "src.backend.integrations.google.gmail_service.os.path.exists", return_value=True
)
@patch(
    "src.backend.integrations.google.gmail_service.Credentials.from_authorized_user_file"
)
def test_get_service_refreshes_and_persists(
    mock_creds_loader, mock_exists, mock_build, mock_persist, monkeypatch
):
    creds = MagicMock(valid=False, expired=True, refresh_token="token")
    mock_creds_loader.return_value = creds

    monkeypatch.setenv("VON_GMAIL_TOKEN_PATH", "/tmp/token.json")
    profiles = gs.load_profiles_from_env()

    gs.get_service(list(profiles.keys())[0], profiles)

    creds.refresh.assert_called_once()
    mock_persist.assert_called_once()
    mock_build.assert_called_once()


@patch("src.backend.integrations.google.gmail_service.get_service")
@patch("src.backend.integrations.google.gmail_service.get_profile")
def test_list_and_get_message(mock_get_profile, mock_get_service):
    mock_profile = SimpleNamespace(
        profile_id="test",
        user_id="me",
        label_filter=["INBOX"],
        query_prefix="from:someone",
    )
    mock_get_profile.return_value = mock_profile

    messages_mock = MagicMock()
    users_mock = MagicMock()
    service_mock = MagicMock()
    service_mock.users.return_value = users_mock
    users_mock.messages.return_value = messages_mock
    messages_mock.list.return_value.execute.return_value = {"messages": []}
    messages_mock.get.return_value.execute.return_value = {"id": "123"}
    attachments_mock = MagicMock()
    messages_mock.attachments.return_value = attachments_mock
    attachments_mock.get.return_value.execute.return_value = {"data": "abc"}
    labels_mock = MagicMock()
    users_mock.labels.return_value = labels_mock
    labels_mock.list.return_value.execute.return_value = {"labels": []}
    mock_get_service.return_value = service_mock

    list_result = gs.list_messages("any", query="subject:test")
    message_result = gs.get_message("any", message_id="123")
    attachment_result = gs.get_attachment("any", message_id="123", attachment_id="att")
    labels_result = gs.list_labels("any")

    assert list_result == {"messages": []}
    messages_mock.list.assert_called_once()
    assert message_result == {"id": "123"}
    assert attachment_result == {"data": "abc"}
    assert labels_result == {"labels": []}


@patch("src.backend.integrations.google.gmail_service.get_service")
@patch("src.backend.integrations.google.gmail_service.get_profile")
def test_list_messages_passes_continuation_token_to_gmail(
    mock_get_profile, mock_get_service
):
    mock_get_profile.return_value = SimpleNamespace(
        profile_id="test",
        user_id="me",
        label_filter=None,
        query_prefix=None,
    )
    messages_mock = MagicMock()
    mock_get_service.return_value.users.return_value.messages.return_value = (
        messages_mock
    )
    messages_mock.list.return_value.execute.return_value = {
        "messages": [{"id": "message-26"}],
        "nextPageToken": "page-3",
    }

    result = gs.list_messages(
        "test",
        query="arxiv.org",
        max_results=25,
        page_token=" page-2 ",
    )

    assert result["nextPageToken"] == "page-3"
    messages_mock.list.assert_called_once_with(
        userId="me",
        q="arxiv.org",
        labelIds=None,
        maxResults=25,
        pageToken="page-2",
    )


@patch("src.backend.integrations.google.gmail_service.get_service")
@patch("src.backend.integrations.google.gmail_service.get_profile")
def test_list_messages_projects_requested_metadata_without_body(
    mock_get_profile, mock_get_service
):
    mock_get_profile.return_value = SimpleNamespace(
        profile_id="test",
        user_id="me",
        label_filter=None,
        query_prefix=None,
    )
    messages_mock = MagicMock()
    mock_get_service.return_value.users.return_value.messages.return_value = (
        messages_mock
    )
    messages_mock.list.return_value.execute.return_value = {
        "messages": [{"id": "message-1", "threadId": "thread-1"}],
        "resultSizeEstimate": 1,
    }
    metadata_requests = []

    def fake_metadata_get(**kwargs):
        metadata_requests.append(kwargs)
        request = MagicMock()
        request.execute.return_value = {
            "id": "message-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX"],
            "snippet": "Your booking is confirmed.",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Airline <travel@example.test>"},
                    {"name": "Subject", "value": "Booking confirmation"},
                    {"name": "Date", "value": "Mon, 10 Aug 2026 18:00:00 +0000"},
                ],
                "parts": [{"body": {"data": "must-not-escape"}}],
            },
            "body": "must-not-escape",
        }
        return request

    messages_mock.get.side_effect = fake_metadata_get

    result = gs.list_messages(
        "test",
        query='newer_than:2d subject:"booking confirmation"',
        max_results=3,
        include_metadata=[
            "id",
            "thread_id",
            "from",
            "subject",
            "date",
            "snippet",
            "label_ids",
        ],
    )

    assert result["messages"] == [
        {
            "id": "message-1",
            "message_id": "message-1",
            "threadId": "thread-1",
            "thread_id": "thread-1",
            "labelIds": ["INBOX"],
            "label_ids": ["INBOX"],
            "snippet": "Your booking is confirmed.",
            "sender": "Airline <travel@example.test>",
            "from": "Airline <travel@example.test>",
            "subject": "Booking confirmation",
            "date": "Mon, 10 Aug 2026 18:00:00 +0000",
        }
    ]
    assert metadata_requests == [
        {
            "userId": "me",
            "id": "message-1",
            "format": "metadata",
            "fields": "id,threadId,labelIds,snippet,payload(headers)",
            "metadataHeaders": ["From", "Subject", "Date"],
        }
    ]
    assert "payload" not in result["messages"][0]
    assert "body" not in result["messages"][0]


@patch("src.backend.integrations.google.gmail_service.get_service")
@patch("src.backend.integrations.google.gmail_service.get_profile")
def test_list_messages_preserves_ids_when_one_metadata_read_fails(
    mock_get_profile, mock_get_service
):
    mock_get_profile.return_value = SimpleNamespace(
        profile_id="test",
        user_id="me",
        label_filter=None,
        query_prefix=None,
    )
    messages_mock = MagicMock()
    mock_get_service.return_value.users.return_value.messages.return_value = (
        messages_mock
    )
    messages_mock.list.return_value.execute.return_value = {
        "messages": [
            {"id": "message-1", "threadId": "thread-1"},
            {"id": "message-2", "threadId": "thread-2"},
        ],
        "resultSizeEstimate": 2,
    }

    first_request = MagicMock()
    first_request.execute.return_value = {
        "id": "message-1",
        "threadId": "thread-1",
        "snippet": "Available",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "First message"},
            ]
        },
    }

    def fake_metadata_get(**kwargs):
        if kwargs["id"] == "message-1":
            return first_request
        raise TimeoutError("metadata read raced with a transient Gmail failure")

    messages_mock.get.side_effect = fake_metadata_get

    result = gs.list_messages(
        "test",
        max_results=2,
        include_metadata=["subject", "snippet"],
    )

    assert result["messages"][0] == {
        "id": "message-1",
        "message_id": "message-1",
        "threadId": "thread-1",
        "thread_id": "thread-1",
        "snippet": "Available",
        "subject": "First message",
    }
    assert result["messages"][1] == {
        "id": "message-2",
        "message_id": "message-2",
        "threadId": "thread-2",
        "thread_id": "thread-2",
        "metadata_error": {
            "code": "gmail_metadata_projection_unavailable",
            "exception_type": "TimeoutError",
        },
        "metadata_missing_fields": ["subject", "snippet"],
    }


@patch("src.backend.integrations.google.gmail_service.get_service")
@patch("src.backend.integrations.google.gmail_service.get_profile")
def test_list_messages_default_does_not_hydrate_rows(
    mock_get_profile, mock_get_service
):
    mock_get_profile.return_value = SimpleNamespace(
        profile_id="test",
        user_id="me",
        label_filter=None,
        query_prefix=None,
    )
    messages_mock = MagicMock()
    mock_get_service.return_value.users.return_value.messages.return_value = (
        messages_mock
    )
    raw_result = {
        "messages": [{"id": "message-1", "threadId": "thread-1"}],
        "resultSizeEstimate": 1,
    }
    messages_mock.list.return_value.execute.return_value = raw_result

    result = gs.list_messages("test", max_results=1)

    assert result == raw_result
    messages_mock.get.assert_not_called()


@patch("src.backend.integrations.google.gmail_service.get_service")
@patch("src.backend.integrations.google.gmail_service.get_profile")
def test_list_labels_resolves_one_exact_name(mock_get_profile, mock_get_service):
    mock_get_profile.return_value = SimpleNamespace(
        profile_id="test",
        user_id="me",
    )
    labels_mock = MagicMock()
    mock_get_service.return_value.users.return_value.labels.return_value = labels_mock
    labels_mock.list.return_value.execute.return_value = {
        "labels": [
            {"id": "Label_2", "name": "VON/PAPER/ARXIV"},
            {"id": "Label_3", "name": "VON/PAPER/REPRESENTED"},
        ]
    }

    result = gs.list_labels(
        "test",
        exact_name="VON/PAPER/REPRESENTED",
        require_exact_match=True,
    )

    assert result["exact_match_count"] == 1
    assert result["label_id"] == "Label_3"
    assert result["label_name"] == "VON/PAPER/REPRESENTED"


@patch("src.backend.integrations.google.gmail_service.get_service")
@patch("src.backend.integrations.google.gmail_service.get_profile")
def test_list_labels_required_exact_name_fails_closed(
    mock_get_profile, mock_get_service
):
    mock_get_profile.return_value = SimpleNamespace(
        profile_id="test",
        user_id="me",
    )
    labels_mock = MagicMock()
    mock_get_service.return_value.users.return_value.labels.return_value = labels_mock
    labels_mock.list.return_value.execute.return_value = {"labels": []}

    with pytest.raises(ValueError, match="matched 0 labels"):
        gs.list_labels(
            "test",
            exact_name="VON/PAPER/REPRESENTED",
            require_exact_match=True,
        )


def test_decode_attachment_bytes_accepts_gmail_base64url_without_padding():
    source = b"%PDF-1.7\nresearch description"
    encoded = base64.urlsafe_b64encode(source).decode("ascii").rstrip("=")

    assert gs.decode_attachment_bytes({"data": encoded}) == source


def test_decode_attachment_bytes_rejects_encoded_payload_before_decode(monkeypatch):
    encoded = base64.urlsafe_b64encode(b"too large").decode("ascii")
    decode_called = False

    def _decode(*_args, **_kwargs):
        nonlocal decode_called
        decode_called = True
        raise AssertionError("oversized payload must not be decoded")

    monkeypatch.setattr(base64, "b64decode", _decode)

    with pytest.raises(gs.GmailAttachmentSizeLimitError):
        gs.decode_attachment_bytes({"data": encoded}, max_bytes=3)

    assert decode_called is False


def test_decode_attachment_bytes_rejects_reported_size_before_decode(monkeypatch):
    decode_called = False

    def _decode(*_args, **_kwargs):
        nonlocal decode_called
        decode_called = True
        raise AssertionError("oversized payload must not be decoded")

    monkeypatch.setattr(base64, "b64decode", _decode)

    with pytest.raises(gs.GmailAttachmentSizeLimitError):
        gs.decode_attachment_bytes({"size": 100, "data": "YQ=="}, max_bytes=3)

    assert decode_called is False


def test_find_attachment_part_metadata_traverses_nested_mime_parts():
    message = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "filename": "research-summary.pdf",
                            "mimeType": "application/PDF",
                            "body": {"attachmentId": "attachment-1", "size": 321},
                        }
                    ],
                }
            ],
        }
    }

    assert gs.find_attachment_part_metadata(
        message,
        attachment_id="attachment-1",
    ) == {
        "attachment_id": "attachment-1",
        "filename": "research-summary.pdf",
        "content_type": "application/pdf",
        "reported_size_bytes": 321,
    }


def test_find_attachment_part_metadata_bounds_mime_traversal():
    message = {
        "payload": {
            "parts": [
                {
                    "filename": f"attachment-{index}.txt",
                    "body": {"attachmentId": f"attachment-{index}"},
                }
                for index in range(300)
            ]
        }
    }

    assert (
        gs.find_attachment_part_metadata(
            message,
            attachment_id="attachment-299",
        )
        == {}
    )


def test_send_message_rejects_html_that_could_hide_agent_disclosure():
    quota_collection, delivery_collection = _outbound_test_collections()

    with pytest.raises(ValueError, match="body_html is not supported"):
        gs.send_message(
            profile_id="zhan-gmail",
            to="recipient@example.test",
            subject="Agent message",
            body_text="Private research summary.",
            body_html="<style>p{display:none}</style><p>Hidden notice</p>",
            allow_send=True,
            profiles={},
            **_outbound_send_kwargs(
                quota_collection=quota_collection,
                delivery_collection=delivery_collection,
                request_id="turn-hidden-disclosure-1",
            ),
        )

    assert quota_collection.count_documents({}) == 0
    assert delivery_collection.count_documents({}) == 0


def test_turn_intent_identity_is_private_stable_and_normalises_recipient_headers():
    common = {
        "idempotency_scope": "turn-multiple-sends",
        "cc": [],
        "bcc": [],
        "reply_to": [],
        "subject": "Requested note",
        "body_text": "Private message body.",
        "body_html": None,
        "body_language": "en-NZ",
        "body_authorship": "von_drafted",
    }

    first = gs._outbound_turn_intent_request_id(
        to=["Researcher <PERSON@EXAMPLE.TEST>"],
        **common,
    )
    equivalent = gs._outbound_turn_intent_request_id(
        to=["person@example.test"],
        **{**common, "body_language": "en-nz"},
    )
    distinct = gs._outbound_turn_intent_request_id(
        to=["other@example.test"],
        **common,
    )

    assert first == equivalent
    assert first != distinct
    assert first.startswith("gmail_turn_intent:")
    assert "person@example.test" not in first
    assert "Private message body." not in first


def test_send_message_returns_verified_body_free_canonical_readback(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(gs, "_resolve_vontology_profile_scopes", lambda _profile: None)
    quota_collection, delivery_collection = _outbound_test_collections()
    request_id = "turn-verified-1"
    expected_fingerprint = gs._outbound_delivery_identity(
        canonical_mailbox_key=derive_canonical_gmail_mailbox_key("zhan@example.test"),
        request_id=request_id,
        to=["recipient@example.test"],
        cc=["copy@example.test"],
        bcc=["blind-copy@example.test"],
        reply_to=[],
        subject="Agent message",
        body_text="Private research summary.",
        body_html=None,
    )[0]
    canonical_message = EmailMessage()
    canonical_message["From"] = "Zhan (Von AI Agent) <zhan@example.test>"
    canonical_message["To"] = "recipient@example.test"
    canonical_message["Cc"] = "copy@example.test"
    canonical_message["Bcc"] = "blind-copy@example.test"
    canonical_message["Subject"] = "Agent message"
    canonical_message[gs.AI_AGENT_MACHINE_HEADER] = "Von; disclosure=off"
    canonical_message[gs.DELIVERY_FINGERPRINT_HEADER] = expected_fingerprint
    canonical_message["Message-ID"] = "<provider-rewritten-message-id@example.test>"
    canonical_message.set_content("Private research summary.")
    canonical_raw = base64.urlsafe_b64encode(canonical_message.as_bytes()).decode(
        "ascii"
    )

    class _Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class _Messages:
        def send(self, **kwargs):
            captured["send_kwargs"] = kwargs
            captured["send_count"] = captured.get("send_count", 0) + 1
            return _Request(
                {
                    "id": "sent-1",
                    "threadId": "thread-1",
                    "raw": "private-provider-body",
                    "payload": {"body": {"data": "private-provider-body"}},
                }
            )

        def get(self, **kwargs):
            captured["get_kwargs"] = kwargs
            return _Request(
                {
                    "id": "sent-1",
                    "threadId": "thread-1",
                    "labelIds": ["SENT"],
                    "raw": canonical_raw,
                }
            )

    class _Users:
        def __init__(self):
            self._messages = _Messages()

        def messages(self):
            return self._messages

        def getProfile(self, **kwargs):
            captured["profile_kwargs"] = kwargs
            return _Request({"emailAddress": "zhan@example.test"})

    class _Service:
        def __init__(self):
            self._users = _Users()

        def users(self):
            return self._users

    profiles = {
        "zhan-gmail": gs.GmailProfile(
            profile_id="zhan-gmail",
            token_path="/tmp/fake-token.json",
            scopes=[gs.MUTATION_SCOPE],
        )
    }
    monkeypatch.setattr(gs, "get_service", lambda *_args, **_kwargs: _Service())

    result = gs.send_message(
        profile_id="zhan-gmail",
        to="recipient@example.test",
        cc="copy@example.test",
        bcc="blind-copy@example.test",
        subject="Agent message",
        body_text="Private research summary.",
        allow_send=True,
        profiles=profiles,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id=request_id,
        ),
    )

    sent_message = BytesParser(policy=default_email_policy).parsebytes(
        base64.urlsafe_b64decode(captured["send_kwargs"]["body"]["raw"].encode("ascii"))
    )
    assert sent_message.get_body(preferencelist=("plain",)).get_content() == (
        "Private research summary.\n"
    )
    assert sent_message.get_body(preferencelist=("html",)) is None
    assert sent_message["From"] == "Von AI Agent <zhan@example.test>"
    assert captured["profile_kwargs"] == {"userId": "me"}
    assert captured["get_kwargs"] == {
        "userId": "me",
        "id": "sent-1",
        "format": "raw",
        "fields": "id,threadId,labelIds,raw",
    }

    readback = result["canonical_readback"]
    assert result["success"] is True
    assert result["status"] == "succeeded"
    assert result["gmail_send_state_verified"] is True
    assert result["effect_status"] == "succeeded"
    assert result["outcome_finality"] == "canonical_durable_readback"
    assert readback["verified"] is True
    assert readback["body_included"] is False
    assert readback["sent_label_verified"] is True
    assert readback["sender_address"] == "zhan@example.test"
    assert readback["sender_verified"] is True
    assert readback["to"] == ["recipient@example.test"]
    assert readback["cc"] == ["copy@example.test"]
    assert readback["bcc_count"] == 1
    assert readback["recipients_verified"] is True
    assert readback["subject_verified"] is True
    assert readback["resolved_disclosure_mode"] == "off"
    assert readback["visible_disclosure_count"] == 0
    assert readback["body_sha256_verified"] is True
    assert readback["disclosure_verified"] is True
    assert readback["machine_disclosure_verified"] is True
    assert readback["delivery_fingerprint_verified"] is True
    assert result["delivery_ledger_status"] == "succeeded"
    assert result["ai_agent_disclosure"]["resolved_mode"] == "off"
    assert result["ai_agent_disclosure"]["visible_text"] is None
    assert result["quota"]["allowed"] is True
    assert "Private research summary." not in json.dumps(readback)
    assert "raw" not in readback
    assert "private-provider-body" not in json.dumps(result)
    assert "payload" not in result

    replay = gs.send_message(
        profile_id="zhan-gmail",
        to="recipient@example.test",
        cc="copy@example.test",
        bcc="blind-copy@example.test",
        subject="Agent message",
        body_text="Private research summary.",
        allow_send=True,
        profiles=profiles,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id=request_id,
        ),
    )

    assert captured["send_count"] == 1
    assert replay["success"] is True
    assert replay["idempotent_replay"] is True
    assert replay["changed"] is False
    assert replay["outcome_finality"] == "durable_idempotent_replay"


def test_send_message_turn_scope_allows_distinct_messages_and_deduplicates_replays(
    monkeypatch,
):
    monkeypatch.setattr(gs, "_resolve_vontology_profile_scopes", lambda _profile: None)
    quota_collection, delivery_collection = _outbound_test_collections()
    captured: dict[str, object] = {"send_count": 0, "raw_by_id": {}}

    class _Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class _Messages:
        def send(self, **kwargs):
            captured["send_count"] = int(captured["send_count"]) + 1
            message_id = f"sent-{captured['send_count']}"
            raw_by_id = cast(dict[str, str], captured["raw_by_id"])
            raw_by_id[message_id] = kwargs["body"]["raw"]
            return _Request({"id": message_id, "threadId": f"thread-{message_id}"})

        def get(self, **kwargs):
            message_id = kwargs["id"]
            raw_by_id = cast(dict[str, str], captured["raw_by_id"])
            return _Request(
                {
                    "id": message_id,
                    "threadId": f"thread-{message_id}",
                    "labelIds": ["SENT"],
                    "raw": raw_by_id[message_id],
                }
            )

    class _Users:
        def __init__(self):
            self._messages = _Messages()

        def messages(self):
            return self._messages

        def getProfile(self, **_kwargs):
            return _Request({"emailAddress": "zhan@example.test"})

    class _Service:
        def __init__(self):
            self._users = _Users()

        def users(self):
            return self._users

    profiles = {
        "zhan-gmail": gs.GmailProfile(
            profile_id="zhan-gmail",
            token_path="/tmp/fake-token.json",
            scopes=[gs.MUTATION_SCOPE],
        )
    }
    monkeypatch.setattr(gs, "get_service", lambda *_args, **_kwargs: _Service())
    shared = {
        "profile_id": "zhan-gmail",
        "subject": "Google Scholar profile and ORCID iD",
        "allow_send": True,
        "profiles": profiles,
        "idempotency_scope": "turn-multiple-gmail-sends",
    }

    def send(*, recipient: str, body: str):
        return gs.send_message(
            to=recipient,
            body_text=body,
            **shared,
            **_outbound_send_kwargs(
                quota_collection=quota_collection,
                delivery_collection=delivery_collection,
                request_id="turn-multiple-gmail-sends",
            ),
        )

    first = send(
        recipient="first@example.test",
        body="Could you send your profile link?",
    )
    second = send(
        recipient="second@example.test",
        body="Could you send your profile link?",
    )
    first_replay = send(
        recipient="first@example.test",
        body="Could you send your profile link?",
    )
    second_replay = send(
        recipient="second@example.test",
        body="Could you send your profile link?",
    )
    legacy_first = gs.send_message(
        profile_id="zhan-gmail",
        to="legacy-first@example.test",
        subject="Legacy direct request",
        body_text="One exact direct delivery.",
        allow_send=True,
        profiles=profiles,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id="legacy-direct-request-id",
        ),
    )
    legacy_conflict = gs.send_message(
        profile_id="zhan-gmail",
        to="legacy-second@example.test",
        subject="Legacy direct request",
        body_text="One exact direct delivery.",
        allow_send=True,
        profiles=profiles,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id="legacy-direct-request-id",
        ),
    )

    assert first["effect_status"] == "succeeded"
    assert second["effect_status"] == "succeeded"
    assert first["delivery_fingerprint"] != second["delivery_fingerprint"]
    assert first_replay["idempotent_replay"] is True
    assert second_replay["idempotent_replay"] is True
    assert first_replay["delivery_fingerprint"] == first["delivery_fingerprint"]
    assert second_replay["delivery_fingerprint"] == second["delivery_fingerprint"]
    assert legacy_first["effect_status"] == "succeeded"
    assert legacy_conflict["error_code"] == "gmail_outbound_idempotency_conflict"
    assert legacy_conflict["effect_status"] == "not_started"
    assert captured["send_count"] == 3
    assert delivery_collection.count_documents({}) == 3
    quota_document = quota_collection.find_one({})
    assert quota_document["messages_in_day"] == 3
    persisted = json.dumps(list(delivery_collection.find({})), default=str)
    assert "first@example.test" not in persisted
    assert "second@example.test" not in persisted
    assert "legacy-first@example.test" not in persisted
    assert "legacy-second@example.test" not in persisted
    assert "Could you send your profile link?" not in persisted


def test_send_message_required_policy_is_localised_exact_once_and_read_back(
    monkeypatch,
):
    captured: dict = {}
    quota_collection, delivery_collection = _outbound_test_collections()
    profile_resource_id = "#V#gmail_profile_zhan_gmail"
    notice = "由 Von 这个人工智能助理发送（文本由用户提供）。"
    represented_policy = {
        "schema_version": (
            gmail_visible_disclosure_policy_service.GMAIL_VISIBLE_DISCLOSURE_POLICY_SCHEMA_VERSION
        ),
        "policy_id": "mailbox-required-zh",
        "policy_version": "2026-08-26",
        "mode": "required",
        "binding": True,
        "default_language": "zh-Hans",
        "templates": {
            "zh-Hans": {
                "von_sent": "由 Von 这个人工智能助理发送。",
                "von_drafted_and_sent": "由 Von 这个人工智能助理起草并发送。",
                "user_authored_von_sent": notice,
            }
        },
    }
    monkeypatch.setattr(
        gmail_visible_disclosure_policy_service,
        "load_concepts",
        lambda _concept_ids: {
            profile_resource_id: {
                "attributes": {
                    gmail_visible_disclosure_policy_service.GMAIL_VISIBLE_DISCLOSURE_POLICY_ATTRIBUTE: represented_policy
                }
            }
        },
    )
    monkeypatch.setattr(gs, "_resolve_vontology_profile_scopes", lambda _profile: None)

    class _Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class _Messages:
        def send(self, **kwargs):
            captured["raw"] = kwargs["body"]["raw"]
            captured["send_count"] = captured.get("send_count", 0) + 1
            return _Request({"id": "sent-required-1", "threadId": "thread-1"})

        def get(self, **_kwargs):
            return _Request(
                {
                    "id": "sent-required-1",
                    "threadId": "thread-1",
                    "labelIds": ["SENT"],
                    "raw": captured["raw"],
                }
            )

    class _Users:
        def messages(self):
            return _Messages()

        def getProfile(self, **_kwargs):
            return _Request({"emailAddress": "zhan@example.test"})

    class _Service:
        def users(self):
            return _Users()

    profiles = {
        "zhan-gmail": gs.GmailProfile(
            profile_id="zhan-gmail",
            token_path="/tmp/fake-token.json",
            scopes=[gs.MUTATION_SCOPE],
        )
    }
    monkeypatch.setattr(gs, "get_service", lambda *_args, **_kwargs: _Service())
    body_text = f"研究摘要。\n\n{notice}"

    result = gs.send_message(
        profile_id="zhan-gmail",
        to="recipient@example.test",
        subject="研究摘要",
        body_text=body_text,
        body_language="zh-Hans",
        body_authorship="user_supplied",
        allow_send=True,
        profiles=profiles,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id="turn-required-zh-1",
            profile_resource_concept_id=profile_resource_id,
        ),
    )

    sent_message = BytesParser(policy=default_email_policy).parsebytes(
        base64.urlsafe_b64decode(captured["raw"].encode("ascii"))
    )
    sent_body = sent_message.get_body(preferencelist=("plain",)).get_content()
    assert sent_body.count(notice) == 1
    assert sent_message[gs.AI_AGENT_MACHINE_HEADER] == "Von; disclosure=required"
    assert result["success"] is True
    assert result["canonical_readback"]["visible_disclosure_count"] == 1
    assert result["canonical_readback"]["body_sha256_verified"] is True
    assert result["ai_agent_disclosure"] == {
        "schema_version": "gmail_visible_ai_agent_disclosure.v2",
        "requested_policies": [
            {
                "mode": "required",
                "policy_id": "mailbox-required-zh",
                "policy_version": "2026-08-26",
                "authority_scope": "mailbox",
                "authority_concept_id": profile_resource_id,
                "binding": True,
            }
        ],
        "resolved_mode": "required",
        "policy_id": "mailbox-required-zh",
        "policy_version": "2026-08-26",
        "authority_scope": "mailbox",
        "authority_concept_id": profile_resource_id,
        "binding": True,
        "selection_reason": "binding_required",
        "requested_language": "zh-Hans",
        "resolved_language": "zh-hans",
        "body_authorship": "user_supplied",
        "template_role": "user_authored_von_sent",
        "visible_text": notice,
        "plain_text_part": True,
        "html_part": False,
        "machine_header": "X-Von-AI-Agent",
    }
    assert "研究摘要。" not in json.dumps(result, ensure_ascii=False)


def test_send_message_aliases_share_mailbox_delivery_and_quota_identity(monkeypatch):
    """Two authorised aliases for one Gmail mailbox cannot bypass controls."""

    quota_collection, delivery_collection = _outbound_test_collections()
    monkeypatch.setattr(gs, "_resolve_vontology_profile_scopes", lambda _profile: None)
    canonical_address = "shared-mailbox@example.test"
    mailbox_key = derive_canonical_gmail_mailbox_key(canonical_address)
    request_id = "turn-same-mailbox-two-aliases-1"
    expected_fingerprint = gs._outbound_delivery_identity(
        canonical_mailbox_key=mailbox_key,
        request_id=request_id,
        to=["recipient@example.test"],
        cc=[],
        bcc=[],
        reply_to=[],
        subject="Alias-safe agent message",
        body_text="Private research summary.",
        body_html=None,
    )[0]
    canonical_message = EmailMessage()
    canonical_message["From"] = f"Von AI Agent <{canonical_address}>"
    canonical_message["To"] = "recipient@example.test"
    canonical_message["Subject"] = "Alias-safe agent message"
    canonical_message[gs.AI_AGENT_MACHINE_HEADER] = "Von; disclosure=off"
    canonical_message[gs.DELIVERY_FINGERPRINT_HEADER] = expected_fingerprint
    canonical_message["Message-ID"] = "<provider-rewritten-message-id@example.test>"
    canonical_message.set_content("Private research summary.")
    canonical_raw = base64.urlsafe_b64encode(canonical_message.as_bytes()).decode(
        "ascii"
    )
    captured = {"send_count": 0, "messages_calls": 0, "profile_reads": 0}

    class _Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class _Messages:
        def send(self, **_kwargs):
            captured["send_count"] += 1
            return _Request({"id": "sent-alias-1", "threadId": "thread-alias-1"})

        def get(self, **_kwargs):
            return _Request(
                {
                    "id": "sent-alias-1",
                    "threadId": "thread-alias-1",
                    "labelIds": ["SENT"],
                    "raw": canonical_raw,
                }
            )

    class _Users:
        def __init__(self):
            self._messages = _Messages()

        def getProfile(self, **_kwargs):
            captured["profile_reads"] += 1
            return _Request({"emailAddress": canonical_address})

        def messages(self):
            captured["messages_calls"] += 1
            return self._messages

    class _Service:
        def __init__(self):
            self._users = _Users()

        def users(self):
            return self._users

    profiles = {
        "alias-a": gs.GmailProfile(
            profile_id="alias-a",
            token_path="/tmp/fake-a.json",
            scopes=[gs.MUTATION_SCOPE],
        ),
        "alias-b": gs.GmailProfile(
            profile_id="alias-b",
            token_path="/tmp/fake-b.json",
            scopes=[gs.MUTATION_SCOPE],
        ),
    }
    service = _Service()
    monkeypatch.setattr(gs, "get_service", lambda *_args, **_kwargs: service)

    common = {
        "to": "recipient@example.test",
        "subject": "Alias-safe agent message",
        "body_text": "Private research summary.",
        "allow_send": True,
        "profiles": profiles,
    }
    first = gs.send_message(
        profile_id="alias-a",
        **common,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id=request_id,
            profile_resource_concept_id="#V#gmail_profile_alias_a",
        ),
    )
    replay = gs.send_message(
        profile_id="alias-b",
        **common,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id=request_id,
            profile_resource_concept_id="#V#gmail_profile_alias_b",
        ),
    )
    rate_limited_kwargs = _outbound_send_kwargs(
        quota_collection=quota_collection,
        delivery_collection=delivery_collection,
        request_id="turn-same-mailbox-two-aliases-2",
        profile_resource_concept_id="#V#gmail_profile_alias_b",
    )
    rate_limited_kwargs["outbound_rate_limit_settings"] = (
        _enabled_outbound_settings(max_messages_per_10_minutes=1)
    )
    denied = gs.send_message(
        profile_id="alias-b",
        **common,
        **rate_limited_kwargs,
    )

    assert first["effect_status"] == "succeeded"
    assert replay["success"] is True
    assert replay["idempotent_replay"] is True
    assert replay["delivery_fingerprint"] == first["delivery_fingerprint"]
    assert denied["error_code"] == "gmail_outbound_rate_limit_exceeded"
    assert denied["effect_status"] == "not_started"
    assert captured["send_count"] == 1
    assert captured["profile_reads"] == 4
    assert quota_collection.count_documents({}) == 1
    quota_document = quota_collection.find_one({})
    assert quota_document["messages_in_day"] == 1
    assert quota_document["canonical_mailbox_key"] == mailbox_key
    assert delivery_collection.count_documents({}) == 2
    delivery_document = delivery_collection.find_one(
        {"delivery_fingerprint": first["delivery_fingerprint"]}
    )
    assert delivery_document["canonical_mailbox_key"] == mailbox_key
    assert delivery_document["observed_profile_resource_concept_ids"] == [
        "#V#gmail_profile_alias_a",
        "#V#gmail_profile_alias_b",
    ]
    assert delivery_document["observed_profile_alias_digests"] == [
        hashlib.sha256(b"alias-a").hexdigest(),
        hashlib.sha256(b"alias-b").hexdigest(),
    ]
    assert canonical_address not in str(quota_document)
    assert canonical_address not in str(list(delivery_collection.find({})))


def test_send_message_canonical_mailbox_preflight_failure_has_no_effect(
    monkeypatch,
):
    quota_collection, delivery_collection = _outbound_test_collections()
    monkeypatch.setattr(gs, "_resolve_vontology_profile_scopes", lambda _profile: None)
    captured = {"messages_calls": 0}

    class _FailedRequest:
        def execute(self):
            raise TimeoutError("canonical Gmail profile lookup timed out")

    class _Users:
        def getProfile(self, **_kwargs):
            return _FailedRequest()

        def messages(self):
            captured["messages_calls"] += 1
            raise AssertionError("messages resource must not be accessed")

    class _Service:
        def users(self):
            return _Users()

    profiles = {
        "alias-a": gs.GmailProfile(
            profile_id="alias-a",
            token_path="/tmp/fake-a.json",
            scopes=[gs.MUTATION_SCOPE],
        )
    }
    monkeypatch.setattr(gs, "get_service", lambda *_args, **_kwargs: _Service())

    result = gs.send_message(
        profile_id="alias-a",
        to="recipient@example.test",
        subject="Preflight failure",
        body_text="This must not be sent.",
        allow_send=True,
        profiles=profiles,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id="turn-preflight-failure-1",
            profile_resource_concept_id="#V#gmail_profile_alias_a",
        ),
    )

    assert result["success"] is False
    assert result["error_code"] == "gmail_canonical_mailbox_preflight_failed"
    assert result["effect_status"] == "not_started"
    assert result["provider_dispatch_attempted"] is False
    assert "delivery_fingerprint" not in result
    assert captured["messages_calls"] == 0
    assert quota_collection.count_documents({}) == 0
    assert delivery_collection.count_documents({}) == 0


def test_send_message_reports_completed_send_with_failed_readback_as_indeterminate(
    monkeypatch,
):
    quota_collection, delivery_collection = _outbound_test_collections()
    monkeypatch.setattr(gs, "_resolve_vontology_profile_scopes", lambda _profile: None)
    class _Request:
        def __init__(self, payload=None, error=None):
            self.payload = payload
            self.error = error

        def execute(self):
            if self.error is not None:
                raise self.error
            return self.payload

    class _Messages:
        def send(self, **_kwargs):
            return _Request({"id": "sent-1"})

        def get(self, **_kwargs):
            return _Request(error=TimeoutError("read-back timed out"))

    class _Users:
        def messages(self):
            return _Messages()

        def getProfile(self, **_kwargs):
            return _Request({"emailAddress": "zhan@example.test"})

    class _Service:
        def users(self):
            return _Users()

    profiles = {
        "zhan-gmail": gs.GmailProfile(
            profile_id="zhan-gmail",
            token_path="/tmp/fake-token.json",
            scopes=[gs.MUTATION_SCOPE],
        )
    }
    monkeypatch.setattr(gs, "get_service", lambda *_args, **_kwargs: _Service())

    result = gs.send_message(
        profile_id="zhan-gmail",
        to="recipient@example.test",
        subject="Agent message",
        body_text="Private research summary.",
        allow_send=True,
        profiles=profiles,
        **_outbound_send_kwargs(
            quota_collection=quota_collection,
            delivery_collection=delivery_collection,
            request_id="turn-indeterminate-1",
        ),
    )

    assert result["changed"] is True
    assert result["success"] is False
    assert result["status"] == "indeterminate"
    assert result["gmail_send_state_verified"] is False
    assert result["effect_status"] == "indeterminate"
    assert result["mutation_outcome"] == "unknown"
    assert result["outcome_finality"] == "canonical_readback_indeterminate"
    assert result["delivery_ledger_status"] == "indeterminate"
    assert result["canonical_readback"] == {
        "status": "unavailable",
        "verified": False,
        "body_included": False,
        "message_id": "sent-1",
        "error_code": "gmail_sent_message_readback_unavailable",
        "exception_type": "TimeoutError",
    }


def test_send_message_reconciles_lost_provider_response_without_redispatch(
    monkeypatch,
):
    quota_collection, delivery_collection = _outbound_test_collections()
    monkeypatch.setattr(gs, "_resolve_vontology_profile_scopes", lambda _profile: None)
    request_id = "turn-provider-response-lost-1"
    expected_fingerprint = gs._outbound_delivery_identity(
        canonical_mailbox_key=derive_canonical_gmail_mailbox_key("zhan@example.test"),
        request_id=request_id,
        to=["recipient@example.test"],
        cc=[],
        bcc=[],
        reply_to=[],
        subject="Reconciled agent message",
        body_text="Private research summary.",
        body_html=None,
    )[0]
    canonical_message = EmailMessage()
    canonical_message["From"] = "Von AI Agent <zhan@example.test>"
    canonical_message["To"] = "recipient@example.test"
    canonical_message["Subject"] = "Reconciled agent message"
    canonical_message[gs.AI_AGENT_MACHINE_HEADER] = "Von; disclosure=off"
    canonical_message[gs.DELIVERY_FINGERPRINT_HEADER] = expected_fingerprint
    canonical_message["Message-ID"] = "<provider-rewritten-message-id@example.test>"
    canonical_message.set_content("Private research summary.")
    canonical_raw = base64.urlsafe_b64encode(canonical_message.as_bytes()).decode(
        "ascii"
    )
    captured = {"send_count": 0, "list_count": 0, "metadata_get_count": 0}

    class _Request:
        def __init__(self, payload=None, error=None):
            self.payload = payload
            self.error = error

        def execute(self):
            if self.error is not None:
                raise self.error
            return self.payload

    class _Messages:
        def send(self, **_kwargs):
            captured["send_count"] += 1
            return _Request(error=TimeoutError("provider response lost"))

        def list(self, **kwargs):
            captured["list_count"] += 1
            captured["list_kwargs"] = kwargs
            return _Request({"messages": [{"id": "sent-1"}]})

        def get(self, **kwargs):
            if kwargs.get("format") == "metadata":
                captured["metadata_get_count"] += 1
                return _Request(
                    {
                        "id": "sent-1",
                        "threadId": "thread-1",
                        "labelIds": ["SENT"],
                        "payload": {
                            "headers": [
                                {
                                    "name": gs.DELIVERY_FINGERPRINT_HEADER,
                                    "value": expected_fingerprint,
                                }
                            ]
                        },
                    }
                )
            return _Request(
                {
                    "id": "sent-1",
                    "threadId": "thread-1",
                    "labelIds": ["SENT"],
                    "raw": canonical_raw,
                }
            )

    class _Users:
        def messages(self):
            return _Messages()

        def getProfile(self, **_kwargs):
            return _Request({"emailAddress": "zhan@example.test"})

    class _Service:
        def users(self):
            return _Users()

    profiles = {
        "zhan-gmail": gs.GmailProfile(
            profile_id="zhan-gmail",
            token_path="/tmp/fake-token.json",
            scopes=[gs.MUTATION_SCOPE],
        )
    }
    monkeypatch.setattr(gs, "get_service", lambda *_args, **_kwargs: _Service())

    send_kwargs = _outbound_send_kwargs(
        quota_collection=quota_collection,
        delivery_collection=delivery_collection,
        request_id=request_id,
    )
    result = gs.send_message(
        profile_id="zhan-gmail",
        to="recipient@example.test",
        subject="Reconciled agent message",
        body_text="Private research summary.",
        allow_send=True,
        profiles=profiles,
        **send_kwargs,
    )
    replay = gs.send_message(
        profile_id="zhan-gmail",
        to="recipient@example.test",
        subject="Reconciled agent message",
        body_text="Private research summary.",
        allow_send=True,
        profiles=profiles,
        **send_kwargs,
    )

    assert captured["send_count"] == 1
    assert captured["list_count"] == 1
    assert captured["metadata_get_count"] == 1
    assert "rfc822msgid:" not in captured["list_kwargs"]["q"]
    assert "subject:\"Reconciled agent message\"" in (
        captured["list_kwargs"]["q"]
    )
    assert "to:recipient@example.test" in captured["list_kwargs"]["q"]
    assert result["effect_status"] == "succeeded"
    assert result["outcome_finality"] == (
        "gmail_sent_reconciled_after_provider_error"
    )
    assert result["delivery_ledger_status"] == "succeeded"
    assert result["delivery_reconciliation"]["verified"] is True
    assert replay["idempotent_replay"] is True
    assert replay["changed"] is False


def test_modify_labels_guard(monkeypatch):
    monkeypatch.setenv("VON_GMAIL_TOKEN_PATH", "/tmp/token.json")
    profiles = {
        "p": gs.GmailProfile(
            profile_id="p",
            token_path="/tmp/token.json",
            scopes=list(gs.DEFAULT_SCOPES),
        )
    }

    with pytest.raises(ValueError):
        gs.modify_labels(
            "p", "mid", add_labels=["X"], allow_mutation=False, profiles=profiles
        )

    profiles["p"].scopes = [gs.MUTATION_SCOPE]
    with patch(
        "src.backend.integrations.google.gmail_service.get_service"
    ) as mock_service:
        svc = MagicMock()
        mock_service.return_value = svc
        svc.users.return_value.messages.return_value.modify.return_value.execute.return_value = {
            "id": "mid"
        }

        result = gs.modify_labels(
            "p", "mid", add_labels=["X"], allow_mutation=True, profiles=profiles
        )

        assert result == {"id": "mid"}
        svc.users.return_value.messages.return_value.modify.assert_called_once()


def test_modify_labels_verifies_canonical_state(monkeypatch):
    profiles = {
        "p": gs.GmailProfile(
            profile_id="p",
            token_path="/tmp/token.json",
            scopes=[gs.MUTATION_SCOPE],
        )
    }
    with patch(
        "src.backend.integrations.google.gmail_service.get_service"
    ) as mock_service:
        svc = MagicMock()
        messages = svc.users.return_value.messages.return_value
        messages.modify.return_value.execute.return_value = {"id": "mid"}
        messages.get.return_value.execute.return_value = {
            "id": "mid",
            "labelIds": ["Label_3", "UNREAD"],
        }
        mock_service.return_value = svc

        result = gs.modify_labels(
            "p",
            "mid",
            add_labels=["Label_3"],
            remove_labels=["INBOX"],
            allow_mutation=True,
            verify_after=True,
            profiles=profiles,
        )

    assert result["success"] is True
    assert result["gmail_label_state_verified"] is True
    assert result["readback_label_ids"] == ["Label_3", "UNREAD"]
    assert result["modify_reconciled_after_error"] is False
    messages.get.assert_called_once_with(userId="me", id="mid", format="minimal")


def test_modify_labels_reconciles_lost_modify_response(monkeypatch):
    profiles = {
        "p": gs.GmailProfile(
            profile_id="p",
            token_path="/tmp/token.json",
            scopes=[gs.MUTATION_SCOPE],
        )
    }
    with patch(
        "src.backend.integrations.google.gmail_service.get_service"
    ) as mock_service:
        svc = MagicMock()
        messages = svc.users.return_value.messages.return_value
        messages.modify.return_value.execute.side_effect = TimeoutError(
            "response lost"
        )
        messages.get.return_value.execute.return_value = {
            "id": "mid",
            "labelIds": ["Label_3"],
        }
        mock_service.return_value = svc

        result = gs.modify_labels(
            "p",
            "mid",
            add_labels=["Label_3"],
            remove_labels=["INBOX"],
            allow_mutation=True,
            verify_after=True,
            profiles=profiles,
        )

    assert result["gmail_label_state_verified"] is True
    assert result["modify_reconciled_after_error"] is True


def test_create_label_guard_and_calls_gmail_api(monkeypatch):
    monkeypatch.setenv("VON_GMAIL_TOKEN_PATH", "/tmp/token.json")
    profiles = {
        "p": gs.GmailProfile(
            profile_id="p",
            token_path="/tmp/token.json",
            scopes=[gs.MUTATION_SCOPE],
        ),
        "read-only": gs.GmailProfile(
            profile_id="read-only",
            token_path="/tmp/token.json",
            scopes=list(gs.DEFAULT_SCOPES),
        ),
    }

    with pytest.raises(ValueError, match="allow_mutation"):
        gs.create_label("p", "VON/PAPER", allow_mutation=False, profiles=profiles)
    with pytest.raises(ValueError, match="name"):
        gs.create_label("p", " ", allow_mutation=True, profiles=profiles)
    with pytest.raises(PermissionError, match="gmail.modify"):
        gs.create_label(
            "read-only",
            "VON/PAPER",
            allow_mutation=True,
            profiles=profiles,
        )

    with patch(
        "src.backend.integrations.google.gmail_service.get_service"
    ) as mock_service:
        svc = MagicMock()
        mock_service.return_value = svc
        svc.users.return_value.labels.return_value.create.return_value.execute.return_value = {
            "id": "Label_1",
            "name": "VON/PAPER",
            "type": "user",
        }

        result = gs.create_label(
            "p",
            " VON/PAPER ",
            label_list_visibility="labelShow",
            message_list_visibility="show",
            allow_mutation=True,
            profiles=profiles,
        )

        assert result["id"] == "Label_1"
        assert result["label_id"] == "Label_1"
        assert result["name"] == "VON/PAPER"
        assert result["profile"] == "p"
        assert result["created"] is True
        svc.users.return_value.labels.return_value.create.assert_called_once_with(
            userId="me",
            body={
                "name": "VON/PAPER",
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )


@patch(
    "src.backend.integrations.google.gmail_service.Credentials.from_authorized_user_file"
)
@patch(
    "src.backend.integrations.google.gmail_service.Credentials.from_authorized_user_info"
)
@patch("src.backend.integrations.google.gmail_service.os.path.exists")
@patch("src.backend.integrations.google.gmail_service._get_agent_gmail_token_payload")
def test_ensure_credentials_prefers_db_tokens(
    mock_get_payload, mock_exists, mock_info_loader, mock_file_loader, monkeypatch
):
    mock_exists.side_effect = AssertionError(
        "Should not check token file when DB tokens exist"
    )
    mock_file_loader.side_effect = AssertionError(
        "Should not load token file when DB tokens exist"
    )
    monkeypatch.setattr(
        gs,
        "_resolve_vontology_profile_scopes",
        lambda profile_id: [gs.MUTATION_SCOPE],
    )

    mock_get_payload.return_value = {
        "token": "access-token",
        "refresh_token": "refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scopes": list(gs.DEFAULT_SCOPES),
        "expiry": "2099-01-01T00:00:00Z",
    }
    creds = MagicMock(valid=True, expired=False, refresh_token=None)
    creds.token = "access-token"
    mock_info_loader.return_value = creds

    profile = gs.GmailProfile(
        profile_id="p", token_path="", scopes=list(gs.DEFAULT_SCOPES)
    )
    loaded = profile.ensure_credentials()

    assert loaded.token == "access-token"
    mock_info_loader.assert_called_once_with(
        mock_get_payload.return_value, scopes=[gs.MUTATION_SCOPE]
    )


@patch(
    "src.backend.integrations.google.gmail_service.Credentials.from_authorized_user_file"
)
@patch("src.backend.integrations.google.gmail_service._upsert_agent_gmail_tokens")
@patch("src.backend.integrations.google.gmail_service._get_agent_gmail_token_status")
@patch("src.backend.integrations.google.gmail_service._get_agent_gmail_token_payload")
@patch(
    "src.backend.integrations.google.gmail_service.Credentials.from_authorized_user_info"
)
def test_ensure_credentials_refreshes_db_tokens_and_persists(
    mock_info_loader,
    mock_get_payload,
    mock_status,
    mock_upsert,
    mock_file_loader,
    monkeypatch,
):
    mock_file_loader.side_effect = AssertionError(
        "Should not load token file when DB tokens exist"
    )
    monkeypatch.setattr(
        gs,
        "_resolve_vontology_profile_scopes",
        lambda profile_id: [gs.MUTATION_SCOPE],
    )

    mock_get_payload.return_value = {
        "token": "access-token",
        "refresh_token": "refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scopes": list(gs.DEFAULT_SCOPES),
        "expiry": "2000-01-01T00:00:00Z",
    }

    creds = MagicMock()
    creds.expired = True
    creds.refresh_token = "refresh-token"
    creds.valid = False
    creds.expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)
    creds.to_json.return_value = json.dumps({"token": "new-token"})
    mock_info_loader.return_value = creds

    mock_status.return_value = SimpleNamespace(authorised_email="test@example.com")

    profile = gs.GmailProfile(
        profile_id="p", token_path="/tmp/unused.json", scopes=list(gs.DEFAULT_SCOPES)
    )
    profile.ensure_credentials()

    mock_info_loader.assert_called_once_with(
        mock_get_payload.return_value, scopes=[gs.MUTATION_SCOPE]
    )
    creds.refresh.assert_called_once()
    mock_upsert.assert_called_once()
    assert mock_upsert.call_args.kwargs["scopes"] == [gs.MUTATION_SCOPE]
