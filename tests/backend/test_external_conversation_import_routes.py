from __future__ import annotations

import io

from flask import Flask


def _make_app(monkeypatch) -> Flask:
    from src.backend.server.routes import von_routes

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda _window, _session, _user: {
            "organisation_id": "#V#org",
            "namespace": "#V#user@org",
            "role": "member",
        },
    )
    return app


def _authenticate(client) -> None:
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#user"


def test_import_route_requires_authentication(monkeypatch):
    app = _make_app(monkeypatch)

    response = app.test_client().post(
        "/von/api/session/import_external_conversation",
        data={"file": (io.BytesIO(b"{}"), "conversation.json")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 401
    assert response.get_json()["error_code"] == "not_authenticated"


def test_import_route_previews_without_switching_session(monkeypatch):
    from src.backend.server.routes import von_routes

    app = _make_app(monkeypatch)
    captured = {}

    def _import(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "status": "dry_run",
            "dry_run": True,
            "preview": {
                "session_id": "external-session",
                "provider": "codex",
                "loss_report": {"projected_message_count": 2},
            },
            "canonical_read_back": None,
        }

    monkeypatch.setattr(von_routes, "import_external_conversation_file", _import)
    client = app.test_client()
    _authenticate(client)

    response = client.post(
        "/von/api/session/import_external_conversation",
        data={
            "dry_run": "true",
            "file": (io.BytesIO(b'{"example": true}'), "conversation.json"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["dry_run"] is True
    assert captured["custodian_user_id"] == "#V#user"
    assert captured["namespace"] == "#V#user@org"
    assert captured["dry_run"] is True
    with client.session_transaction() as flask_session:
        assert flask_session.get("session_id") is None


def test_import_route_executes_and_switches_only_after_success(monkeypatch):
    from src.backend.server.routes import von_routes

    app = _make_app(monkeypatch)
    monkeypatch.setattr(
        von_routes,
        "import_external_conversation_file",
        lambda **_kwargs: {
            "success": True,
            "status": "new",
            "dry_run": False,
            "preview": {"session_id": "external-session"},
            "projection": {"session_id": "external-session", "action": "new"},
            "canonical_read_back": {
                "session_id": "external-session",
                "external_conversation": {"read_only": True},
            },
        },
    )
    client = app.test_client()
    _authenticate(client)

    response = client.post(
        "/von/api/session/import_external_conversation",
        data={
            "dry_run": "false",
            "file": (io.BytesIO(b'{"example": true}'), "conversation.json"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert (
        response.get_json()["canonical_read_back"]["external_conversation"]["read_only"]
        is True
    )
    with client.session_transaction() as flask_session:
        assert flask_session["session_id"] == "external-session"


def test_continue_route_creates_and_switches_to_native_fork(monkeypatch):
    from src.backend.server.routes import von_routes

    app = _make_app(monkeypatch)
    monkeypatch.setattr(
        von_routes,
        "continue_external_conversation",
        lambda **_kwargs: {
            "success": True,
            "status": "created",
            "session_id": "native-continuation",
            "session_name": "Imported — continued",
            "history_message_count": 2,
        },
    )
    client = app.test_client()
    _authenticate(client)

    response = client.post(
        "/von/api/session/continue_external_conversation",
        json={"session_id": "external-session"},
    )

    assert response.status_code == 201
    assert response.get_json()["session_id"] == "native-continuation"
    with client.session_transaction() as flask_session:
        assert flask_session["session_id"] == "native-continuation"


def test_queue_route_rejects_imported_read_only_session(monkeypatch):
    from src.backend.server.routes import von_routes

    app = _make_app(monkeypatch)
    created = []
    monkeypatch.setattr(
        von_routes,
        "_get_current_chat_prompt_queue_scope",
        lambda: {
            "user_concept_id": "#V#user",
            "organisation_concept_id": "#V#org",
            "namespace": "#V#user@org",
        },
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "is_external_conversation_read_only",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        von_routes.chat_prompt_queue_service,
        "create_queue_record",
        lambda **kwargs: created.append(kwargs),
    )

    response = app.test_client().post(
        "/von/api/chat_prompt_queue",
        json={"session_id": "external-session", "prompt_raw": "Do more"},
    )

    assert response.status_code == 409
    assert response.get_json()["error_code"] == "external_conversation_read_only"
    assert created == []
