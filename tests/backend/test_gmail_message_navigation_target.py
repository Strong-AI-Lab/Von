from types import SimpleNamespace
from unittest.mock import MagicMock

from src.backend.integrations.google import gmail_service


def _gmail_service_with_thread(thread_id: str) -> MagicMock:
    service = MagicMock()
    message_get = service.users.return_value.messages.return_value.get
    message_get.return_value.execute.return_value = {"threadId": thread_id}
    return service


def test_navigation_target_uses_minimal_message_read_and_token_identity(
    monkeypatch,
) -> None:
    profile = gmail_service.GmailProfile(
        profile_id="research-gmail",
        token_path="unused",
    )
    service = _gmail_service_with_thread("thread-123")
    monkeypatch.setattr(gmail_service, "get_profile", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(gmail_service, "get_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(
        gmail_service,
        "_get_agent_gmail_token_status",
        lambda _profile_id: SimpleNamespace(authorised_email="person@example.test"),
    )

    target = gmail_service.resolve_message_thread_navigation_target(
        "research-gmail",
        "message-123",
        audit_context={"source": "test"},
    )

    assert target == {
        "profile_id": "research-gmail",
        "message_id": "message-123",
        "thread_id": "thread-123",
        "authorised_email": "person@example.test",
    }
    service.users.return_value.messages.return_value.get.assert_called_once_with(
        userId="me",
        id="message-123",
        format="minimal",
        fields="threadId",
    )
    service.users.return_value.getProfile.assert_not_called()


def test_resolve_message_thread_navigation_target_falls_back_to_gmail_profile_identity(
    monkeypatch,
) -> None:
    profile = gmail_service.GmailProfile(
        profile_id="research-gmail",
        token_path="unused",
    )
    service = _gmail_service_with_thread("thread-123")
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "fallback@example.test"
    }
    monkeypatch.setattr(gmail_service, "get_profile", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(gmail_service, "get_service", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(gmail_service, "_get_agent_gmail_token_status", None)

    target = gmail_service.resolve_message_thread_navigation_target(
        "research-gmail", "message-123"
    )

    assert target["authorised_email"] == "fallback@example.test"
    service.users.return_value.getProfile.assert_called_once_with(userId="me")
