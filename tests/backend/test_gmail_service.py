import base64
import json
import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.backend.integrations.google import gmail_service as gs


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
