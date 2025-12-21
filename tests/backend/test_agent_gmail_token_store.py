import importlib
import os

import pytest


@pytest.fixture()
def mock_db_env(monkeypatch):
    # Must be set before importing mongo_client so USE_MOCK_DB is computed correctly.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    # Provide a stable test encryption key
    from cryptography.fernet import Fernet

    monkeypatch.setenv("GMAIL_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    # Ensure fresh module state after env var changes
    import backend.db.mongo_client as mongo_client

    importlib.reload(mongo_client)

    yield

    # Cleanup for safety
    monkeypatch.delenv("VON_USE_MOCK_DB", raising=False)
    monkeypatch.delenv("GMAIL_TOKEN_ENCRYPTION_KEY", raising=False)


def test_roundtrip_store_and_load(mock_db_env):
    import backend.services.agent_gmail_token_store as store

    importlib.reload(store)

    profile_id = "von-mail"
    payload = {"access_token": "abc", "refresh_token": "def", "token_type": "Bearer"}

    store.upsert_agent_gmail_tokens(
        profile_id=profile_id,
        token_payload=payload,
        authorised_email="von-mail@gmail.com",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        expires_at=None,
    )

    loaded = store.get_agent_gmail_token_payload(profile_id)
    assert loaded == payload

    status = store.get_agent_gmail_token_status(profile_id)
    assert status.profile_id == profile_id
    assert status.has_tokens is True
    assert status.authorised_email == "von-mail@gmail.com"
    assert status.scopes == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_revoke_deletes_tokens(mock_db_env):
    import backend.services.agent_gmail_token_store as store

    importlib.reload(store)

    profile_id = "zhan-gmail"
    store.upsert_agent_gmail_tokens(profile_id=profile_id, token_payload={"access_token": "x"})

    assert store.get_agent_gmail_token_payload(profile_id) == {"access_token": "x"}

    assert store.revoke_agent_gmail_tokens(profile_id) is True
    assert store.get_agent_gmail_token_payload(profile_id) is None


def test_missing_key_raises(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    import backend.db.mongo_client as mongo_client

    importlib.reload(mongo_client)

    import backend.services.agent_gmail_token_store as store

    importlib.reload(store)

    monkeypatch.delenv("GMAIL_TOKEN_ENCRYPTION_KEY", raising=False)

    with pytest.raises(Exception) as exc:
        store.upsert_agent_gmail_tokens(profile_id="p", token_payload={"access_token": "x"})

    # Avoid asserting exact message, but ensure it's key-related.
    assert "GMAIL_TOKEN_ENCRYPTION_KEY" in str(exc.value)
