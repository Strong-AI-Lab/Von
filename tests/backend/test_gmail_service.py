import json
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


def test_get_profile_missing(monkeypatch):
    monkeypatch.delenv(gs.PROFILES_ENV_VAR, raising=False)
    monkeypatch.delenv("VON_GMAIL_TOKEN_PATH", raising=False)
    with pytest.raises(RuntimeError):
        gs.get_profile("anything")


@patch("src.backend.integrations.google.gmail_service.build")
@patch("src.backend.integrations.google.gmail_service.os.path.exists", return_value=True)
@patch("src.backend.integrations.google.gmail_service.Credentials.from_authorized_user_file")
def test_get_service_uses_existing_credentials(mock_creds_loader, mock_exists, mock_build, monkeypatch):
    creds = MagicMock(valid=True, expired=False, refresh_token=None)
    mock_creds_loader.return_value = creds

    monkeypatch.setenv("VON_GMAIL_TOKEN_PATH", "/tmp/token.json")
    profiles = gs.load_profiles_from_env()

    service = gs.get_service("von-service", profiles)

    assert service == mock_build.return_value
    mock_build.assert_called_once()
    mock_creds_loader.assert_called_once()
    creds.refresh.assert_not_called()


@patch("src.backend.integrations.google.gmail_service._persist_credentials")
@patch("src.backend.integrations.google.gmail_service.build")
@patch("src.backend.integrations.google.gmail_service.os.path.exists", return_value=True)
@patch("src.backend.integrations.google.gmail_service.Credentials.from_authorized_user_file")
def test_get_service_refreshes_and_persists(mock_creds_loader, mock_exists, mock_build, mock_persist, monkeypatch):
    creds = MagicMock(valid=False, expired=True, refresh_token="token")
    mock_creds_loader.return_value = creds

    monkeypatch.setenv("VON_GMAIL_TOKEN_PATH", "/tmp/token.json")
    profiles = gs.load_profiles_from_env()

    gs.get_service("von-service", profiles)

    creds.refresh.assert_called_once()
    mock_persist.assert_called_once()
    mock_build.assert_called_once()


@patch("src.backend.integrations.google.gmail_service.get_service")
@patch("src.backend.integrations.google.gmail_service.get_profile")
def test_list_and_get_message(mock_get_profile, mock_get_service):
    mock_profile = SimpleNamespace(user_id="me", label_filter=["INBOX"], query_prefix="from:someone")
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
        gs.modify_labels("p", "mid", add_labels=["X"], allow_mutation=False, profiles=profiles)

    profiles["p"].scopes = [gs.MUTATION_SCOPE]
    with patch("src.backend.integrations.google.gmail_service.get_service") as mock_service:
        svc = MagicMock()
        mock_service.return_value = svc
        svc.users.return_value.messages.return_value.modify.return_value.execute.return_value = {"id": "mid"}

        result = gs.modify_labels("p", "mid", add_labels=["X"], allow_mutation=True, profiles=profiles)

        assert result == {"id": "mid"}
        svc.users.return_value.messages.return_value.modify.assert_called_once()
