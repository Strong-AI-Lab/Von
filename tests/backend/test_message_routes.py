"""Route tests for inter-user messaging safety and attribution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask

import src.backend.server.routes.message_routes as message_routes


def _authorise_test_direct_message(monkeypatch) -> None:
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_alice",
    )
    monkeypatch.setattr(
        message_routes,
        "_get_current_org_concept_id",
        lambda _user_id, **_kwargs: "#V#org_test",
    )
    monkeypatch.setattr(
        message_routes,
        "_authorise_sender_and_recipients_for_org",
        lambda **_kwargs: (True, []),
    )


@pytest.fixture
def app_client():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(message_routes.message_bp, url_prefix="/api/messages")

    with app.test_client() as client:
        yield app, client


def test_send_message_requires_authentication(monkeypatch, app_client):
    _, client = app_client
    monkeypatch.setattr(message_routes, "_get_current_user_concept_id", lambda: None)

    response = client.post(
        "/api/messages/",
        json={"recipient_ids": ["#V#user_bob"], "content": "Kia ora"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


@pytest.mark.parametrize(
    ("exception_type", "expected_status", "expected_code"),
    [
        (
            message_routes.WindowSessionContextUnavailable,
            409,
            "window_context_unavailable",
        ),
        (
            message_routes.WindowSessionContextRecoveryUnavailable,
            503,
            "window_context_recovery_unavailable",
        ),
    ],
)
def test_send_message_rejects_unavailable_selected_window_without_org_fallback(
    monkeypatch,
    app_client,
    exception_type,
    expected_status,
    expected_code,
):
    _, client = app_client
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_alice",
    )
    effective_context_calls: list[dict[str, object]] = []

    def _unavailable_context(
        window_session_id,
        flask_session_snapshot,
        user_concept_id,
        *,
        require_known_window=False,
    ):
        effective_context_calls.append(
            {
                "window_session_id": window_session_id,
                "flask_session_snapshot": flask_session_snapshot,
                "user_concept_id": user_concept_id,
                "require_known_window": require_known_window,
            }
        )
        raise exception_type("window context unavailable")

    monkeypatch.setattr(message_routes, "get_effective_context", _unavailable_context)
    monkeypatch.setattr(
        message_routes,
        "create_message",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("create_message should not be called")
        ),
    )
    with client.session_transaction() as browser_session:
        browser_session["organisation_concept_id"] = "#V#wrong_browser_org"

    response = client.post(
        "/api/messages/",
        headers={"X-Von-Window-Session": "tab-selected-org"},
        json={"recipient_ids": ["#V#user_bob"], "content": "Kia ora"},
    )

    assert response.status_code == expected_status
    payload = response.get_json()
    assert payload["error_code"] == expected_code
    assert payload["retryable"] is True
    assert effective_context_calls == [
        {
            "window_session_id": "tab-selected-org",
            "flask_session_snapshot": {
                "organisation_concept_id": "#V#wrong_browser_org"
            },
            "user_concept_id": "#V#user_alice",
            "require_known_window": True,
        }
    ]


def test_send_message_without_window_selector_keeps_legacy_org_fallback(
    monkeypatch,
    app_client,
):
    _, client = app_client
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_alice",
    )
    seen_require_known_window: list[bool] = []

    def _legacy_context(
        _window_session_id,
        flask_session_snapshot,
        _user_concept_id,
        *,
        require_known_window=False,
    ):
        seen_require_known_window.append(require_known_window)
        return {
            "organisation_id": flask_session_snapshot.get("organisation_concept_id")
        }

    monkeypatch.setattr(message_routes, "get_effective_context", _legacy_context)
    monkeypatch.setattr(
        message_routes,
        "_authorise_sender_and_recipients_for_org",
        lambda **_kwargs: (True, []),
    )
    monkeypatch.setattr(
        message_routes,
        "create_message",
        lambda **_kwargs: {
            "concept_id": "#V#message_legacy_client",
            "concept_data": {"sent_at": "2026-08-28T12:00:00+00:00"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.episode_logging_service.log_episode",
        lambda **_kwargs: "episode-legacy-client",
    )
    with client.session_transaction() as browser_session:
        browser_session["organisation_concept_id"] = "#V#legacy_org"

    response = client.post(
        "/api/messages/",
        json={"recipient_ids": ["#V#user_bob"], "content": "Kia ora"},
    )

    assert response.status_code == 201
    assert seen_require_known_window == [False]


def test_get_single_message_requires_actor_participation(monkeypatch, app_client):
    _, client = app_client
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_charlie",
    )
    seen: list[tuple[str, str]] = []

    def _fake_get_message_for_user(message_id: str, user_id: str):
        seen.append((message_id, user_id))
        return None

    monkeypatch.setattr(
        message_routes,
        "get_message_for_user",
        _fake_get_message_for_user,
    )

    response = client.get("/api/messages/%23V%23message_private")

    assert response.status_code == 404
    assert seen == [("#V#message_private", "#V#user_charlie")]


def test_send_message_rejects_recipient_outside_organisation(monkeypatch, app_client):
    _, client = app_client
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_alice",
    )
    monkeypatch.setattr(
        message_routes,
        "_get_current_org_concept_id",
        lambda _user_id, **_kwargs: "#V#org_test",
    )

    import src.backend.services.organisation_membership_service as membership_service

    monkeypatch.setattr(
        membership_service,
        "get_organisation_members",
        lambda _org_id: {
            "members": [
                {"user_concept_id": "#V#user_alice"},
                {"user_concept_id": "#V#user_charlie"},
            ]
        },
    )
    monkeypatch.setattr(
        membership_service,
        "get_user_memberships",
        lambda user_concept_id: (
            {
                "memberships": [
                    {
                        "organisation_concept_id": "#V#org_test",
                        "role": "admin",
                    },
                    {
                        "organisation_concept_id": "#V#org_shared",
                        "role": "researcher",
                    },
                ]
            }
            if user_concept_id == "#V#user_alice"
            else {
                "memberships": [
                    {
                        "organisation_concept_id": "#V#org_shared",
                        "role": "member",
                    }
                ]
            }
        ),
    )
    import src.backend.services.concept_service as concept_service

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id",
        lambda concept_id: None,
    )
    monkeypatch.setattr(
        message_routes,
        "create_message",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("create_message should not be called")
        ),
    )

    response = client.post(
        "/api/messages/",
        json={"recipient_ids": ["#V#user_bob"], "content": "Review this please"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "Sender/recipient must share the current organisation"
    assert "#V#user_bob" in payload["invalid_concept_ids"]
    assert payload["organisation_concept_id"] == "#V#org_test"
    assert payload["common_organisation_options"] == [
        {
            "concept_id": "#V#org_shared",
            "name": "Org Shared",
            "role": "researcher",
        }
    ]


def test_send_message_accepts_explicit_send_organisation_context(
    monkeypatch, app_client
):
    _, client = app_client
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_alice",
    )
    monkeypatch.setattr(
        message_routes,
        "_get_current_org_concept_id",
        lambda _user_id, **_kwargs: (_ for _ in ()).throw(
            AssertionError(
                "explicit authorised organisation recovery must not read a stale window"
            )
        ),
    )

    import src.backend.services.organisation_membership_service as membership_service

    monkeypatch.setattr(
        membership_service,
        "get_organisation_members",
        lambda organisation_concept_id: (
            {
                "members": [
                    {"user_concept_id": "#V#user_alice"},
                    {"user_concept_id": "#V#user_bob"},
                ]
            }
            if organisation_concept_id == "#V#org_shared"
            else {
                "members": [
                    {"user_concept_id": "#V#user_alice"},
                ]
            }
        ),
    )

    captured_create_args: dict[str, object] = {}

    def _fake_create_message(**kwargs):
        captured_create_args.update(kwargs)
        return {
            "concept_id": "#V#message_explicit_org",
            "concept_data": {"sent_at": "2026-04-23T08:30:00+00:00"},
        }

    monkeypatch.setattr(message_routes, "create_message", _fake_create_message)

    import src.backend.services.episode_logging_service as episode_logging_service

    episode_calls: list[dict] = []

    def _fake_log_episode(**kwargs):
        episode_calls.append(dict(kwargs))
        return "episode-explicit-org"

    monkeypatch.setattr(episode_logging_service, "log_episode", _fake_log_episode)

    response = client.post(
        "/api/messages/",
        headers={"X-Von-Window-Session": "stale-tab-selector"},
        json={
            "recipient_ids": ["#V#user_bob"],
            "content": "Switch to our shared lab organisation for this message.",
            "organisation_concept_id": "#V#org_shared",
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["message_id"] == "#V#message_explicit_org"
    assert captured_create_args["org_id"] == "#V#org_shared"
    assert episode_calls[0]["organisation_concept_id"] == "#V#org_shared"


def test_send_message_success_logs_episode_and_returns_attribution(
    monkeypatch, app_client
):
    _, client = app_client
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_alice",
    )
    monkeypatch.setattr(
        message_routes,
        "_get_current_org_concept_id",
        lambda _user_id, **_kwargs: "#V#org_test",
    )

    import src.backend.services.organisation_membership_service as membership_service

    monkeypatch.setattr(
        membership_service,
        "get_organisation_members",
        lambda _org_id: {
            "members": [
                {"user_concept_id": "#V#user_alice"},
                {"user_concept_id": "#V#user_bob"},
            ]
        },
    )

    captured_create_args: dict[str, object] = {}

    def _fake_create_message(**kwargs):
        captured_create_args.update(kwargs)
        return {
            "concept_id": "#V#message_test_1",
            "concept_data": {"sent_at": "2026-02-24T10:00:00+00:00"},
        }

    monkeypatch.setattr(message_routes, "create_message", _fake_create_message)

    import src.backend.services.episode_logging_service as episode_logging_service

    episode_calls: list[dict] = []

    def _fake_log_episode(**kwargs):
        episode_calls.append(dict(kwargs))
        return "episode-1"

    monkeypatch.setattr(episode_logging_service, "log_episode", _fake_log_episode)

    response = client.post(
        "/api/messages/",
        json={
            "recipient_ids": ["#V#user_bob"],
            "content": "Please review the workflow output.",
            "metadata": {"intent": "review_request"},
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["message_id"] == "#V#message_test_1"
    assert payload["attribution"] == "Sent by Von on behalf of #V#user_alice"

    assert captured_create_args["sender_id"] == "#V#user_alice"
    assert captured_create_args["recipient_ids"] == ["#V#user_bob"]
    assert captured_create_args["org_id"] == "#V#org_test"
    metadata = captured_create_args["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["intent"] == "review_request"
    assert metadata["delivery_channel"] == "interuser_message"
    assert metadata["attribution"] == "Sent by Von on behalf of #V#user_alice"

    assert len(episode_calls) == 1
    assert episode_calls[0]["episode_type"] == "interuser_message_sent"
    assert episode_calls[0]["actor_user_id"] == "#V#user_alice"
    assert episode_calls[0]["organisation_concept_id"] == "#V#org_test"
    assert episode_calls[0]["payload"]["message_id"] == "#V#message_test_1"


def test_send_message_returns_explicit_reused_receipt_without_duplicate_episode(
    monkeypatch,
    app_client,
):
    _, client = app_client
    _authorise_test_direct_message(monkeypatch)
    captured: dict[str, object] = {}

    def _fake_create_idempotently(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            reused=True,
            message={
                "concept_id": "#V#message_existing",
                "concept_data": {
                    "sent_at": "2026-08-28T05:00:00+00:00",
                    "attribution": "Sent by Von on behalf of #V#user_alice",
                },
            },
        )

    monkeypatch.setattr(
        message_routes,
        "create_message_idempotently",
        _fake_create_idempotently,
    )
    monkeypatch.setattr(
        message_routes,
        "create_message",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy create should not run for an idempotent request")
        ),
    )

    import src.backend.services.episode_logging_service as episode_logging_service

    episode_calls: list[dict] = []
    monkeypatch.setattr(
        episode_logging_service,
        "log_episode",
        lambda **kwargs: episode_calls.append(dict(kwargs)),
    )

    response = client.post(
        "/api/messages/",
        headers={"Idempotency-Key": "browser-send-1"},
        json={
            "_id": "client-controlled-document-id",
            "recipient_ids": ["#V#user_bob"],
            "content": "Please review the update.",
            "delivery_idempotency_key": "browser-send-1",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    conversation = body.pop("conversation")
    assert conversation["participant_ids"] == ["#V#user_alice", "#V#user_bob"]
    assert conversation["organisation_concept_id"] == "#V#org_test"
    assert conversation["source_kind"] == "message_exchange"
    assert body == {
        "success": True,
        "effect_status": "succeeded",
        "changed": False,
        "reused": True,
        "idempotent_replay": True,
        "delivery_status": "reused",
        "message_id": "#V#message_existing",
        "sent_at": "2026-08-28T05:00:00+00:00",
        "attribution": "Sent by Von on behalf of #V#user_alice",
    }
    assert captured["delivery_idempotency_key"] == "browser-send-1"
    assert captured["sender_id"] == "#V#user_alice"
    assert captured["organisation_concept_id"] == "#V#org_test"
    assert captured["recipient_ids"] == ["#V#user_bob"]
    assert "_id" not in captured
    assert "_server_owned_document_id" not in captured
    assert episode_calls == []


def test_send_message_maps_changed_payload_for_same_key_to_conflict(
    monkeypatch,
    app_client,
):
    _, client = app_client
    _authorise_test_direct_message(monkeypatch)

    def _raise_conflict(**_kwargs):
        raise message_routes.DirectMessageIdempotencyConflict(
            "delivery_idempotency_key was already used for a different message payload"
        )

    monkeypatch.setattr(
        message_routes,
        "create_message_idempotently",
        _raise_conflict,
    )

    response = client.post(
        "/api/messages/",
        json={
            "recipient_ids": ["#V#user_bob"],
            "content": "Changed payload",
            "delivery_idempotency_key": "browser-send-1",
        },
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "error": (
            "delivery_idempotency_key was already used for a different message payload"
        ),
        "error_code": "idempotency_key_reused_with_different_payload",
    }


def test_send_message_requires_matching_body_and_header_delivery_keys(
    monkeypatch,
    app_client,
):
    _, client = app_client
    _authorise_test_direct_message(monkeypatch)
    monkeypatch.setattr(
        message_routes,
        "create_message_idempotently",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("mismatched keys must fail before persistence")
        ),
    )

    response = client.post(
        "/api/messages/",
        headers={"Idempotency-Key": "header-key"},
        json={
            "recipient_ids": ["#V#user_bob"],
            "content": "Please review the update.",
            "delivery_idempotency_key": "body-key",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error_code"] == "idempotency_key_mismatch"


def test_send_message_rejects_ambiguous_legacy_idempotency_field(
    monkeypatch,
    app_client,
):
    _, client = app_client
    _authorise_test_direct_message(monkeypatch)

    response = client.post(
        "/api/messages/",
        json={
            "recipient_ids": ["#V#user_bob"],
            "content": "Please review the update.",
            "idempotency_key": "ambiguous-key",
        },
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "Use delivery_idempotency_key for direct-message retries",
        "error_code": "unsupported_idempotency_key_field",
    }
