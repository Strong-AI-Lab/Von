"""Tests for moving conversations between organisations (JVNAUTOSCI-1039).

Tests the shared_conversation_service helpers for revoking invites
and the /von/api/session/move_chat_session_org endpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestRevokesInvitesForSession:
    """Tests for revoke_invites_for_session helper."""

    def test_revokes_pending_and_accepted_invites(self):
        """Test that both pending and accepted invites are revoked."""
        mock_coll = MagicMock()
        mock_coll.find.return_value = [
            {"invitee_user_id": "#V#user_a", "invite_id": "invite-1"},
            {"invitee_user_id": "#V#user_b", "invite_id": "invite-2"},
        ]
        mock_result = MagicMock()
        mock_result.modified_count = 2
        mock_coll.update_many.return_value = mock_result

        with patch(
            "src.backend.services.shared_conversation_service._get_collection",
            return_value=mock_coll,
        ):
            from src.backend.services.shared_conversation_service import (
                revoke_invites_for_session,
            )

            result = revoke_invites_for_session(
                session_id="test-session",
                reason="test_revocation",
            )

        assert result["revoked_count"] == 2
        assert set(result["invitee_ids"]) == {"#V#user_a", "#V#user_b"}

        # Check the correct query was used
        mock_coll.find.assert_called_once()
        query = mock_coll.find.call_args[0][0]
        assert query["session_id"] == "test-session"
        assert query["status"] == {"$in": ["pending", "accepted"]}

    def test_excludes_specified_users(self):
        """Test that excluded users keep their invites."""
        mock_coll = MagicMock()
        mock_coll.find.return_value = [
            {"invitee_user_id": "#V#user_b", "invite_id": "invite-2"},
        ]
        mock_result = MagicMock()
        mock_result.modified_count = 1
        mock_coll.update_many.return_value = mock_result

        with patch(
            "src.backend.services.shared_conversation_service._get_collection",
            return_value=mock_coll,
        ):
            from src.backend.services.shared_conversation_service import (
                revoke_invites_for_session,
            )

            result = revoke_invites_for_session(
                session_id="test-session",
                exclude_user_ids=["#V#user_a"],
                reason="test_revocation",
            )

        assert result["revoked_count"] == 1
        assert "#V#user_a" not in result["invitee_ids"]

        # Check exclusion is in query
        query = mock_coll.find.call_args[0][0]
        assert query["invitee_user_id"] == {"$nin": ["#V#user_a"]}

    def test_returns_empty_when_no_invites(self):
        """Test graceful handling when no invites to revoke."""
        mock_coll = MagicMock()
        mock_coll.find.return_value = []

        with patch(
            "src.backend.services.shared_conversation_service._get_collection",
            return_value=mock_coll,
        ):
            from src.backend.services.shared_conversation_service import (
                revoke_invites_for_session,
            )

            result = revoke_invites_for_session(
                session_id="test-session",
            )

        assert result["revoked_count"] == 0
        assert result["invitee_ids"] == []
        # update_many should not be called
        mock_coll.update_many.assert_not_called()

    def test_handles_database_unavailable(self):
        """Test handling when database is unavailable."""
        with patch(
            "src.backend.services.shared_conversation_service._get_collection",
            return_value=None,
        ):
            from src.backend.services.shared_conversation_service import (
                revoke_invites_for_session,
            )

            result = revoke_invites_for_session(
                session_id="test-session",
            )

        assert result["revoked_count"] == 0
        assert "error" in result


class TestListActiveInvitesForSession:
    """Tests for list_active_invites_for_session helper."""

    def test_returns_pending_and_accepted_invites(self):
        """Test that pending and accepted invites are returned."""
        mock_coll = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = [
            {"invite_id": "invite-1", "status": "pending"},
            {"invite_id": "invite-2", "status": "accepted"},
        ]
        mock_coll.find.return_value = mock_cursor

        with patch(
            "src.backend.services.shared_conversation_service._get_collection",
            return_value=mock_coll,
        ):
            from src.backend.services.shared_conversation_service import (
                list_active_invites_for_session,
            )

            result = list_active_invites_for_session(session_id="test-session")

        assert len(result) == 2
        assert result[0]["invite_id"] == "invite-1"
        assert result[1]["invite_id"] == "invite-2"

    def test_returns_empty_for_invalid_session_id(self):
        """Test that empty list is returned for invalid session_id."""
        from src.backend.services.shared_conversation_service import (
            list_active_invites_for_session,
        )

        result = list_active_invites_for_session(session_id="")
        assert result == []

        result = list_active_invites_for_session(session_id="   ")
        assert result == []


class TestMoveConversationEndpoint:
    """Tests for the move_chat_session_org API endpoint."""

    @pytest.fixture
    def app_client(self, monkeypatch):
        """Provide a Flask test client with mocked dependencies."""
        import sys
        import types

        # Stub Google auth dependencies
        fake_flow_module: Any = types.ModuleType("google_auth_oauthlib.flow")

        class _DummyFlow:
            def __init__(self, *args, **kwargs):
                self.credentials = types.SimpleNamespace(id_token="dummy-token")
                self.redirect_uri = kwargs.get("redirect_uri")
                self.client_config = {"web": {"redirect_uris": [self.redirect_uri]}}

            @classmethod
            def from_client_config(cls, *args, **kwargs):
                return cls(**kwargs)

            def authorization_url(self, *args, **kwargs):
                return "https://auth.example", "state-token"

            def fetch_token(self, *args, **kwargs):
                return None

        fake_flow_module.Flow = _DummyFlow
        fake_google_auth_oauthlib: Any = types.ModuleType("google_auth_oauthlib")
        fake_google_auth_oauthlib.flow = fake_flow_module
        sys.modules["google_auth_oauthlib"] = fake_google_auth_oauthlib
        sys.modules["google_auth_oauthlib.flow"] = fake_flow_module

        fake_id_token_module: Any = types.ModuleType("google.oauth2.id_token")
        fake_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {
            "sub": "dummy-user"
        }

        fake_credentials_module: Any = types.ModuleType("google.oauth2.credentials")

        class _DummyCredentials:
            def __init__(self, id_token: str = "dummy-token"):
                self.id_token = id_token

        fake_credentials_module.Credentials = _DummyCredentials

        fake_service_account_module: Any = types.ModuleType(
            "google.oauth2.service_account"
        )

        class _DummyServiceAccountCredentials:
            def __init__(self, *args, **kwargs):
                self.project_id = kwargs.get("project_id")

        fake_service_account_module.Credentials = _DummyServiceAccountCredentials

        fake_oauth2_package: Any = types.ModuleType("google.oauth2")
        fake_oauth2_package.id_token = fake_id_token_module
        fake_oauth2_package.credentials = fake_credentials_module
        fake_oauth2_package.service_account = fake_service_account_module

        sys.modules["google.oauth2"] = fake_oauth2_package
        sys.modules["google.oauth2.id_token"] = fake_id_token_module
        sys.modules["google.oauth2.credentials"] = fake_credentials_module
        sys.modules["google.oauth2.service_account"] = fake_service_account_module

        import src.backend.server.utils_flask as utils_flask

        monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
        monkeypatch.setattr(
            utils_flask,
            "prompt_concept_health_status",
            lambda: {"available": True, "source_field": "stub"},
        )

        app = utils_flask.create_flask_app(
            list_models_func=lambda: ["dummy-model"],
            generate_func=lambda prompt, context, model: "ok",
        )
        app.config["TESTING"] = True

        with app.test_client() as client:
            yield app, client

    def test_requires_authentication(self, app_client):
        """Test that unauthenticated requests are rejected."""
        _, client = app_client

        resp = client.post(
            "/von/api/session/move_chat_session_org",
            json={"session_id": "test-session", "target_organisation_id": "#V#org"},
        )

        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Not authenticated"

    def test_requires_session_id(self, app_client):
        """Test that session_id is required."""
        _, client = app_client

        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#test_user"

        resp = client.post(
            "/von/api/session/move_chat_session_org",
            json={"target_organisation_id": "#V#org"},
        )

        assert resp.status_code == 400
        assert "session_id" in resp.get_json()["error"]

    def test_requires_target_organisation_id(self, app_client):
        """Test that target_organisation_id is required."""
        _, client = app_client

        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#test_user"

        resp = client.post(
            "/von/api/session/move_chat_session_org",
            json={"session_id": "test-session"},
        )

        assert resp.status_code == 400
        assert "target_organisation_id" in resp.get_json()["error"]

    def test_rejects_non_member_of_target_org(self, app_client, monkeypatch):
        """Test that user must be a member of target organisation."""
        _, client = app_client

        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#test_user"

        # Mock get_user_memberships to return no memberships
        import src.backend.services.organisation_membership_service as org_svc

        monkeypatch.setattr(
            org_svc,
            "get_user_memberships",
            lambda user_concept_id: {"memberships": [], "total_memberships": 0},
        )

        resp = client.post(
            "/von/api/session/move_chat_session_org",
            json={
                "session_id": "test-session",
                "target_organisation_id": "#V#target_org",
            },
        )

        assert resp.status_code == 403
        assert "Not a member" in resp.get_json()["error"]

    def test_moves_conversation_successfully(self, app_client, monkeypatch):
        """Test successful conversation move."""
        _, client = app_client

        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#test_user"

        # Mock dependencies
        import src.backend.services.organisation_membership_service as org_svc
        import src.backend.services.chat_history_service as chat_svc
        import src.backend.services.shared_conversation_service as shared_svc
        import src.backend.services.episode_logging_service as episode_svc

        monkeypatch.setattr(
            org_svc,
            "get_user_memberships",
            lambda user_concept_id: {
                "memberships": [
                    {"organisation_concept_id": "#V#current_org"},
                    {"organisation_concept_id": "#V#target_org"},
                ],
                "total_memberships": 2,
            },
        )

        monkeypatch.setattr(
            org_svc,
            "get_organisation_members",
            lambda org_id: {
                "members": [{"user_concept_id": "#V#member_1"}],
            },
        )

        mock_coll = MagicMock()
        mock_doc = {
            "_id": "doc-id",
            "session_id": "test-session",
            "user_id": "#V#test_user",
            "namespace": "#V#test_user@current_org",
            "organisation_concept_id": "#V#current_org",
        }
        mock_coll.find_one.return_value = mock_doc
        mock_update_result = MagicMock()
        mock_update_result.matched_count = 1
        mock_update_result.modified_count = 1
        mock_coll.update_one.return_value = mock_update_result

        monkeypatch.setattr(
            chat_svc,
            "get_chat_history_collection_service",
            lambda **kwargs: mock_coll,
        )

        monkeypatch.setattr(
            shared_svc,
            "list_active_invites_for_session",
            lambda session_id: [],
        )

        monkeypatch.setattr(
            shared_svc,
            "revoke_invites_for_session",
            lambda **kwargs: {"revoked_count": 0, "invitee_ids": []},
        )

        monkeypatch.setattr(
            episode_svc,
            "log_episode",
            lambda **kwargs: None,
        )

        resp = client.post(
            "/von/api/session/move_chat_session_org",
            json={
                "session_id": "test-session",
                "target_organisation_id": "#V#target_org",
            },
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "moved"
        assert data["namespace"] == "#V#test_user@target_org"
        assert data["organisation_concept_id"] == "#V#target_org"
        assert data["previous_namespace"] == "#V#test_user@current_org"

    def test_revokes_invites_for_non_members(self, app_client, monkeypatch):
        """Test that invites for non-members are revoked during move."""
        _, client = app_client

        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#test_user"

        import src.backend.services.organisation_membership_service as org_svc
        import src.backend.services.chat_history_service as chat_svc
        import src.backend.services.shared_conversation_service as shared_svc
        import src.backend.services.episode_logging_service as episode_svc

        monkeypatch.setattr(
            org_svc,
            "get_user_memberships",
            lambda user_concept_id: {
                "memberships": [
                    {"organisation_concept_id": "#V#target_org"},
                ],
                "total_memberships": 1,
            },
        )

        # Only member_1 is in target org
        monkeypatch.setattr(
            org_svc,
            "get_organisation_members",
            lambda org_id: {
                "members": [{"user_concept_id": "#V#member_1"}],
            },
        )

        mock_coll = MagicMock()
        mock_doc = {
            "_id": "doc-id",
            "session_id": "test-session",
            "user_id": "#V#test_user",
            "namespace": None,
            "organisation_concept_id": None,
        }
        mock_coll.find_one.return_value = mock_doc
        mock_update_result = MagicMock()
        mock_update_result.matched_count = 1
        mock_update_result.modified_count = 1
        mock_coll.update_one.return_value = mock_update_result

        monkeypatch.setattr(
            chat_svc,
            "get_chat_history_collection_service",
            lambda **kwargs: mock_coll,
        )

        # There are invites for users not in target org
        monkeypatch.setattr(
            shared_svc,
            "list_active_invites_for_session",
            lambda session_id: [
                {"invitee_user_id": "#V#member_1", "status": "accepted"},
                {"invitee_user_id": "#V#outside_user", "status": "pending"},
            ],
        )

        revoke_called_with = {}

        def mock_revoke(**kwargs):
            revoke_called_with.update(kwargs)
            return {"revoked_count": 1, "invitee_ids": ["#V#outside_user"]}

        monkeypatch.setattr(shared_svc, "revoke_invites_for_session", mock_revoke)

        monkeypatch.setattr(
            episode_svc,
            "log_episode",
            lambda **kwargs: None,
        )

        resp = client.post(
            "/von/api/session/move_chat_session_org",
            json={
                "session_id": "test-session",
                "target_organisation_id": "#V#target_org",
            },
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["invites_revoked"] == 1
        assert "#V#outside_user" in data["revoked_invitee_ids"]
        # member_1 should be excluded from revocation
        assert "#V#member_1" in revoke_called_with.get("exclude_user_ids", [])
