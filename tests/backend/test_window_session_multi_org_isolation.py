"""Tests for multi-org isolation between browser windows (JVNAUTOSCI-1011).

These tests validate that different browser windows (identified by X-Von-Window-Session
headers) can maintain independent organisation contexts without interfering with each
other, and that chat history operations use the correct window-scoped namespace.
"""

from __future__ import annotations

import types
import pytest


@pytest.fixture
def app_client(monkeypatch):
    """Provide a Flask test client with side effects stubbed out."""
    import sys

    # Stub Google auth dependencies
    fake_flow_module = types.ModuleType("google_auth_oauthlib.flow")

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

    fake_flow_module.Flow = _DummyFlow  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib"] = types.ModuleType("google_auth_oauthlib")
    sys.modules["google_auth_oauthlib"].flow = fake_flow_module  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module

    fake_id_token_module = types.ModuleType("google.oauth2.id_token")
    fake_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {  # type: ignore[attr-defined]
        "sub": "dummy-user"
    }

    fake_credentials_module = types.ModuleType("google.oauth2.credentials")

    class _DummyCredentials:
        def __init__(self, id_token: str = "dummy-token"):
            self.id_token = id_token

    fake_credentials_module.Credentials = _DummyCredentials  # type: ignore[attr-defined]

    fake_service_account_module = types.ModuleType("google.oauth2.service_account")

    class _DummyServiceAccountCredentials:
        def __init__(self, *args, **kwargs):
            self.project_id = kwargs.get("project_id")

    fake_service_account_module.Credentials = _DummyServiceAccountCredentials  # type: ignore[attr-defined]

    fake_oauth2_package = types.ModuleType("google.oauth2")
    fake_oauth2_package.id_token = fake_id_token_module  # type: ignore[attr-defined]
    fake_oauth2_package.credentials = fake_credentials_module  # type: ignore[attr-defined]
    fake_oauth2_package.service_account = fake_service_account_module  # type: ignore[attr-defined]

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

    # Reset window session store between tests
    import src.backend.services.window_session_context_service as wscs

    wscs._window_session_store = None

    with app.test_client() as client:
        yield app, client


class TestWindowSessionContextIsolation:
    """Test that window sessions maintain isolated organisation contexts."""

    def test_two_windows_can_have_different_orgs(self, app_client):
        """Different window sessions should maintain separate organisation contexts."""
        _, client = app_client

        window_a = "ws_window_a_test"
        window_b = "ws_window_b_test"

        with client.session_transaction() as sess:
            sess["user_id"] = "michael_witbrock"
            sess["user_concept_id"] = "#V#michael_witbrock"

        # Window A: Set to lab org
        resp_a = client.post(
            "/von/api/session/set_organisation",
            json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
            headers={"X-Von-Window-Session": window_a},
        )
        assert resp_a.status_code == 200
        data_a = resp_a.get_json()
        assert (
            data_a["namespace"]
            == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
        )

        # Window B: Set to household org
        resp_b = client.post(
            "/von/api/session/set_organisation",
            json={"organisation_concept_id": "the_lu_witbrock_household"},
            headers={"X-Von-Window-Session": window_b},
        )
        assert resp_b.status_code == 200
        data_b = resp_b.get_json()
        assert data_b["namespace"] == "#V#michael_witbrock@the_lu_witbrock_household"

        # Verify Window A still has lab context
        ctx_a = client.get(
            "/von/api/session/context",
            headers={"X-Von-Window-Session": window_a},
        )
        assert ctx_a.status_code == 200
        ctx_a_data = ctx_a.get_json()
        assert (
            ctx_a_data["organisation_id"] == "#V#university_of_auckland_strong_ai_lab"
        )
        assert (
            ctx_a_data["namespace"]
            == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
        )
        assert ctx_a_data["context_source"] == "window_session"

        # Verify Window B still has household context
        ctx_b = client.get(
            "/von/api/session/context",
            headers={"X-Von-Window-Session": window_b},
        )
        assert ctx_b.status_code == 200
        ctx_b_data = ctx_b.get_json()
        assert ctx_b_data["organisation_id"] == "#V#the_lu_witbrock_household"
        assert (
            ctx_b_data["namespace"] == "#V#michael_witbrock@the_lu_witbrock_household"
        )
        assert ctx_b_data["context_source"] == "window_session"

    def test_window_without_header_uses_flask_session(self, app_client):
        """Requests without X-Von-Window-Session header should fall back to Flask session."""
        _, client = app_client

        with client.session_transaction() as sess:
            sess["user_id"] = "michael_witbrock"
            sess["user_concept_id"] = "#V#michael_witbrock"
            sess["organisation_concept_id"] = "#V#flask_org"
            sess["namespace"] = "#V#michael_witbrock@flask_org"
            sess["role_in_org"] = "admin"

        # Request without window session header
        ctx = client.get("/von/api/session/context")
        assert ctx.status_code == 200
        ctx_data = ctx.get_json()
        assert ctx_data["organisation_id"] == "#V#flask_org"
        assert ctx_data["namespace"] == "#V#michael_witbrock@flask_org"
        # Note: context_source key is only in window session response, not flask session fallback


class TestSetChatSessionUsesWindowContext:
    """Test that set_chat_session uses the window-scoped namespace."""

    def test_set_chat_session_uses_window_namespace(self, monkeypatch, app_client):
        """set_chat_session should query sessions using window session's namespace."""
        _, client = app_client
        captured_namespace_args: list[str] = []

        import src.backend.services.chat_history_service as chat_history_service

        original_build_query = chat_history_service.build_chat_history_query

        def capture_build_query(*args, **kwargs):
            ns = kwargs.get("namespace")
            if ns:
                captured_namespace_args.append(ns)
            return original_build_query(*args, **kwargs)

        monkeypatch.setattr(
            chat_history_service, "build_chat_history_query", capture_build_query
        )

        # Stub get_chat_history_collection_service to return a fake collection
        class _FakeColl:
            def find_one(self, query, projection=None):
                return {
                    "session_id": "lab_session_1",
                    "session_name": "Lab Session",
                    "history": [{"role": "user", "content": "lab context"}],
                }

        monkeypatch.setattr(
            chat_history_service,
            "get_chat_history_collection_service",
            lambda **kwargs: _FakeColl(),
        )

        # Stub shared conversation resolver to return no shared invite
        import src.backend.server.routes.von_routes as von_routes

        monkeypatch.setattr(
            von_routes,
            "_resolve_shared_conversation_owner",
            lambda **kwargs: (None, None),
        )

        window_a = "ws_lab_window"

        with client.session_transaction() as sess:
            sess["user_id"] = "michael_witbrock"
            sess["user_concept_id"] = "#V#michael_witbrock"

        # Set window A to lab org
        client.post(
            "/von/api/session/set_organisation",
            json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
            headers={"X-Von-Window-Session": window_a},
        )

        # Try to load a session in window A
        resp = client.post(
            "/von/api/session/set_chat_session",
            json={"session_id": "lab_session_1"},
            headers={"X-Von-Window-Session": window_a},
        )

        assert resp.status_code == 200
        # Verify that build_chat_history_query was called with the window's namespace
        assert any(
            "michael_witbrock@university_of_auckland_strong_ai_lab" in ns
            for ns in captured_namespace_args
        ), f"Expected lab namespace in queries, got: {captured_namespace_args}"

    def test_set_chat_session_respects_different_window_namespaces(
        self, monkeypatch, app_client
    ):
        """Different windows should query their own namespaces for sessions."""
        _, client = app_client
        captured_namespace_args: list[str] = []

        import src.backend.services.chat_history_service as chat_history_service

        original_build_query = chat_history_service.build_chat_history_query

        def capture_build_query(*args, **kwargs):
            ns = kwargs.get("namespace")
            if ns:
                captured_namespace_args.append(ns)
            return original_build_query(*args, **kwargs)

        monkeypatch.setattr(
            chat_history_service, "build_chat_history_query", capture_build_query
        )

        # Stub get_chat_history_collection_service to return a fake collection
        class _FakeColl:
            def find_one(self, query, projection=None):
                return {
                    "session_id": "session_x",
                    "session_name": "Test Session",
                    "history": [{"role": "user", "content": "test"}],
                }

        monkeypatch.setattr(
            chat_history_service,
            "get_chat_history_collection_service",
            lambda **kwargs: _FakeColl(),
        )

        # Stub shared conversation resolver to return no shared invite
        import src.backend.server.routes.von_routes as von_routes

        monkeypatch.setattr(
            von_routes,
            "_resolve_shared_conversation_owner",
            lambda **kwargs: (None, None),
        )

        window_lab = "ws_lab_window"
        window_home = "ws_home_window"

        with client.session_transaction() as sess:
            sess["user_id"] = "michael_witbrock"
            sess["user_concept_id"] = "#V#michael_witbrock"

        # Set up both windows
        client.post(
            "/von/api/session/set_organisation",
            json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
            headers={"X-Von-Window-Session": window_lab},
        )
        client.post(
            "/von/api/session/set_organisation",
            json={"organisation_concept_id": "the_lu_witbrock_household"},
            headers={"X-Von-Window-Session": window_home},
        )

        # Clear captured namespaces from setup
        captured_namespace_args.clear()

        # Load session in lab window
        resp_lab = client.post(
            "/von/api/session/set_chat_session",
            json={"session_id": "session_x"},
            headers={"X-Von-Window-Session": window_lab},
        )

        lab_namespaces = captured_namespace_args.copy()
        captured_namespace_args.clear()

        # Load session in home window
        resp_home = client.post(
            "/von/api/session/set_chat_session",
            json={"session_id": "session_x"},
            headers={"X-Von-Window-Session": window_home},
        )

        home_namespaces = captured_namespace_args.copy()

        assert resp_lab.status_code == 200
        assert resp_home.status_code == 200

        # Verify each window queried its own namespace
        assert any(
            "strong_ai_lab" in ns for ns in lab_namespaces
        ), f"Lab should query lab namespace: {lab_namespaces}"
        assert any(
            "household" in ns for ns in home_namespaces
        ), f"Home should query household namespace: {home_namespaces}"


class TestCreateChatSessionUsesWindowContext:
    """Test that create_chat_session uses the window-scoped namespace."""

    def test_create_session_uses_window_namespace(self, monkeypatch, app_client):
        """Creating a session should use the window session's namespace."""
        _, client = app_client
        created_sessions: list[dict] = []

        import src.backend.services.chat_history_service as chat_history_service
        import src.backend.services.window_session_context_service as wscs

        def capture_create(*args, **kwargs):
            created_sessions.append(kwargs.copy())
            # Return a mock result
            return {
                "session_id": kwargs.get("session_id", "new_session"),
                "session_name": kwargs.get("session_name", "New Session"),
            }

        monkeypatch.setattr(chat_history_service, "create_chat_session", capture_create)

        window_a = "ws_create_test"

        with client.session_transaction() as sess:
            sess["user_id"] = "michael_witbrock"
            sess["user_concept_id"] = "#V#michael_witbrock"

        # Set window to lab org
        client.post(
            "/von/api/session/set_organisation",
            json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
            headers={"X-Von-Window-Session": window_a},
        )

        # Create a new session
        resp = client.post(
            "/von/api/session/create_chat_session",
            json={"session_name": "Lab Discussion"},
            headers={"X-Von-Window-Session": window_a},
        )

        assert resp.status_code == 200
        payload = resp.get_json()
        assert isinstance(payload, dict)

        # Verify the session was created with the window's namespace
        assert len(created_sessions) == 1
        assert (
            created_sessions[0].get("namespace")
            == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
        )
        assert (
            created_sessions[0].get("organisation_concept_id")
            == "university_of_auckland_strong_ai_lab"
        )
        assert created_sessions[0].get("is_agent_created") is None
        window_ctx = wscs.get_window_context(window_a)
        assert window_ctx is not None
        assert window_ctx.chat_session_id == payload["session_id"]

        monkeypatch.setattr(
            "src.backend.security.access_control.get_effective_user_concept_id",
            lambda: "#V#michael_witbrock",
        )
        monkeypatch.setattr(
            chat_history_service,
            "get_chat_history_session_summaries",
            lambda *args, **kwargs: [
                {
                    "session_id": payload["session_id"],
                    "session_name": payload["session_name"],
                    "last_message_at": "2026-04-13T06:00:00Z",
                    "namespace": "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
                }
            ],
        )
        monkeypatch.setattr(
            chat_history_service,
            "has_chat_history_session",
            lambda user_id, session_id, namespace=None: session_id
            == payload["session_id"],
        )

        import src.backend.services.shared_conversation_service as shared_conversation_service

        monkeypatch.setattr(
            shared_conversation_service,
            "list_accepted_invites_for_user",
            lambda **kwargs: [],
        )
        monkeypatch.setattr(
            shared_conversation_service,
            "list_outgoing_accepted_invites_for_user",
            lambda **kwargs: [],
        )
        monkeypatch.setattr(
            shared_conversation_service,
            "resolve_conversation_owner",
            lambda **kwargs: None,
        )

        history_resp = client.get(
            "/von/history/sessions?limit=50&summary=light",
            headers={"X-Von-Window-Session": window_a},
        )

        assert history_resp.status_code == 200
        history_payload = history_resp.get_json()
        assert history_payload["active_session_id"] == payload["session_id"]

    def test_create_session_marks_browser_test_fixture_sessions(
        self, monkeypatch, app_client
    ):
        _, client = app_client
        created_sessions: list[dict] = []

        import src.backend.services.chat_history_service as chat_history_service

        def capture_create(*args, **kwargs):
            created_sessions.append(kwargs.copy())
            return {
                "session_id": kwargs.get("session_id", "new_session"),
                "session_name": kwargs.get("session_name", "New Session"),
                "origin_kind": kwargs.get("origin_kind"),
                "created_by_actor_concept_id": kwargs.get(
                    "created_by_actor_concept_id"
                ),
                "created_by_actor_type": kwargs.get("created_by_actor_type"),
                "is_agent_created": kwargs.get("is_agent_created"),
                "test_artifact_kind": kwargs.get("test_artifact_kind"),
            }

        monkeypatch.setattr(chat_history_service, "create_chat_session", capture_create)

        with client.session_transaction() as sess:
            sess["user_id"] = "browser_test_fixture"
            sess["user_concept_id"] = "#V#codex_browser_fixture"
            sess["auth_provider"] = "browser_test_fixture"
            sess["browser_test_fixture_id"] = "browser_user_view.v1"

        resp = client.post(
            "/von/api/session/create_chat_session",
            json={"session_name": "Browser fixture chat"},
        )

        assert resp.status_code == 200
        payload = resp.get_json()
        assert created_sessions[0]["origin_kind"] == "browser_test_fixture"
        assert created_sessions[0]["is_agent_created"] is True
        assert (
            created_sessions[0]["test_artifact_kind"]
            == "browser_test_authenticated_chat_session"
        )
        assert payload["is_agent_created"] is True


class TestChatSessionLinksUsesWindowContext:
    """Test that chat_session_links uses the window-scoped namespace."""

    def test_session_links_uses_window_namespace(self, monkeypatch, app_client):
        """Session links should be fetched using the window's namespace."""
        _, client = app_client
        captured_namespaces: list[str | None] = []

        import src.backend.services.chat_history_service as chat_history_service

        def fake_get_links(*, user_id, session_id, namespace=None, **kwargs):
            captured_namespaces.append(namespace)
            return {
                "programmes": [],
                "projects": [],
                "activities": [],
                "modalities": [],
            }

        monkeypatch.setattr(
            chat_history_service, "get_chat_session_links", fake_get_links
        )

        # Stub has_chat_history_session to return True
        monkeypatch.setattr(
            chat_history_service,
            "has_chat_history_session",
            lambda *args, **kwargs: True,
        )

        window_a = "ws_links_test"

        with client.session_transaction() as sess:
            sess["user_id"] = "michael_witbrock"
            sess["user_concept_id"] = "#V#michael_witbrock"

        # Set window to lab org
        client.post(
            "/von/api/session/set_organisation",
            json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
            headers={"X-Von-Window-Session": window_a},
        )

        # Get session links
        resp = client.get(
            "/von/api/session/chat_session_links?session_id=test_session",
            headers={"X-Von-Window-Session": window_a},
        )

        assert resp.status_code == 200
        assert (
            "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
            in captured_namespaces
        )


class TestHistoryBackfillUsesWindowContext:
    """Test that history/backfill_spoken uses the window-scoped namespace."""

    def test_backfill_spoken_uses_window_namespace(self, monkeypatch, app_client):
        """Backfill spoken should verify session access using the window session's namespace."""
        _, client = app_client
        captured_namespace_checks: list[str | None] = []

        import src.backend.services.chat_history_service as chat_history_service

        def fake_has_session(user_id, session_id, namespace=None, **kwargs):
            captured_namespace_checks.append(namespace)
            return True  # Session exists

        def fake_get_collection(**kwargs):
            class _FakeColl:
                def find_one(self, query, projection=None):
                    return {
                        "history": [
                            {"role": "user", "content": "test prompt"},
                            {"role": "assistant", "content": "test response"},
                        ]
                    }

                def update_one(self, *args, **kwargs):
                    return type("Result", (), {"modified_count": 1})()

            return _FakeColl()

        monkeypatch.setattr(
            chat_history_service, "has_chat_history_session", fake_has_session
        )
        monkeypatch.setattr(
            chat_history_service,
            "get_chat_history_collection_service",
            fake_get_collection,
        )

        window_a = "ws_backfill_test"

        with client.session_transaction() as sess:
            sess["user_id"] = "michael_witbrock"
            sess["user_concept_id"] = "#V#michael_witbrock"

        # Set window to lab org
        client.post(
            "/von/api/session/set_organisation",
            json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
            headers={"X-Von-Window-Session": window_a},
        )

        # Request backfill (it will fail later in the method, but we're testing namespace check)
        client.post(
            "/von/history/backfill_spoken",
            json={"session_id": "test_session", "history_index": 1},
            headers={"X-Von-Window-Session": window_a},
        )

        # The namespace check should have used the window's namespace
        assert (
            "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
            in captured_namespace_checks
        )


class TestWindowSessionStoreIsolation:
    """Direct tests for the window session store isolation."""

    def test_store_maintains_separate_contexts(self):
        """WindowSessionStore should maintain separate contexts per window ID."""
        from src.backend.services.window_session_context_service import (
            WindowSessionStore,
        )

        store = WindowSessionStore()

        # Create two window contexts
        ctx_a = store.get_or_create("window_a", "user_1")
        ctx_a.organisation_concept_id = "org_a"
        ctx_a.namespace = "#V#user_1@org_a"
        store.set(ctx_a)

        ctx_b = store.get_or_create("window_b", "user_1")
        ctx_b.organisation_concept_id = "org_b"
        ctx_b.namespace = "#V#user_1@org_b"
        store.set(ctx_b)

        # Verify isolation
        retrieved_a = store.get("window_a")
        retrieved_b = store.get("window_b")

        assert retrieved_a is not None
        assert retrieved_b is not None
        assert retrieved_a.organisation_concept_id == "org_a"
        assert retrieved_b.organisation_concept_id == "org_b"
        assert retrieved_a.namespace != retrieved_b.namespace

    def test_get_effective_context_prefers_window_session(self):
        """get_effective_context should prefer window session over Flask session."""
        from src.backend.services.window_session_context_service import (
            get_effective_context,
            get_window_session_store,
            WindowSessionContext,
        )

        store = get_window_session_store()

        # Set up window context
        ctx = WindowSessionContext(
            window_session_id="test_window",
            user_id="user_1",
            organisation_concept_id="window_org",
            namespace="#V#user_1@window_org",
            role_in_org="admin",
        )
        store.set(ctx)

        # Flask session has different org
        flask_session = {
            "organisation_concept_id": "flask_org",
            "namespace": "#V#user_1@flask_org",
            "role_in_org": "member",
        }

        # get_effective_context should return window context
        effective = get_effective_context("test_window", flask_session, "user_1")

        assert effective["organisation_id"] == "window_org"
        assert effective["namespace"] == "#V#user_1@window_org"
        assert effective["role"] == "admin"
        assert effective["source"] == "window_session"

    def test_get_effective_context_falls_back_to_flask(self):
        """get_effective_context should fall back to Flask session when no window session."""
        from src.backend.services.window_session_context_service import (
            get_effective_context,
        )

        flask_session = {
            "organisation_concept_id": "flask_org",
            "namespace": "#V#user_1@flask_org",
            "role_in_org": "member",
        }

        # No window session
        effective = get_effective_context(None, flask_session, "user_1")

        assert effective["organisation_id"] == "flask_org"
        assert effective["namespace"] == "#V#user_1@flask_org"
        assert effective["role"] == "member"
        assert effective["source"] == "flask_session"

    def test_get_effective_context_unknown_window_falls_back(self):
        """get_effective_context should fall back when window session ID is unknown."""
        from src.backend.services.window_session_context_service import (
            get_effective_context,
        )

        flask_session = {
            "organisation_concept_id": "flask_org",
            "namespace": "#V#user_1@flask_org",
            "role_in_org": "member",
        }

        # Unknown window session ID
        effective = get_effective_context("unknown_window_id", flask_session, "user_1")

        assert effective["organisation_id"] == "flask_org"
        assert effective["source"] == "flask_session"

    def test_get_effective_context_ignores_partial_window_scope(self):
        """A partial window entry should not erase richer Flask org scope."""
        from src.backend.services.window_session_context_service import (
            get_effective_context,
            get_window_session_store,
        )

        store = get_window_session_store()
        partial_ctx = store.get_or_create("partial_window", user_id="user_1")
        partial_ctx.chat_session_id = "chat_123"
        store.set(partial_ctx)

        flask_session = {
            "organisation_concept_id": "flask_org",
            "namespace": "#V#user_1@flask_org",
            "role_in_org": "member",
            "session_id": "flask_chat",
        }

        effective = get_effective_context("partial_window", flask_session, "user_1")

        assert effective["organisation_id"] == "flask_org"
        assert effective["namespace"] == "#V#user_1@flask_org"
        assert effective["role"] == "member"
        assert effective["chat_session_id"] == "chat_123"
        assert effective["source"] == "flask_session"
