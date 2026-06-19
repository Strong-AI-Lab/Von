import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.backend.integrations.google import gmail_service as gs


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
