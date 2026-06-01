from flask import Flask


def test_history_llm_call_log_returns_paged_payload(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )

    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_llm_call_log_payload",
        lambda **_kwargs: {
            "schema_version": "turn_llm_call_log.v1",
            "request_id": "req-1",
            "derived_user_concept_id": "#V#test_user",
            "chat_session_id": "session-1",
            "entries": [
                {
                    "sequence_no": 1,
                    "call_type": "llm.generate",
                    "prompt": {"text": "Prompt A", "char_count": 8, "is_truncated": False},
                    "response": {"text": "Response A", "char_count": 10, "is_truncated": False},
                }
            ],
            "offset": 0,
            "limit": 20,
            "returned_count": 1,
            "total_count": 1,
            "has_more": False,
            "next_offset": None,
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    client = app.test_client()
    response = client.get(
        "/von/history/llm_call_log",
        query_string={"request_id": "req-1", "offset": 0, "limit": 20},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("success") is True
    assert body.get("request_id") == "req-1"
    assert body.get("returned_count") == 1
    assert isinstance(body.get("entries"), list)


def test_history_llm_call_log_rejects_non_shared_turn(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#viewer",
    )

    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_llm_call_log_payload",
        lambda **_kwargs: {
            "schema_version": "turn_llm_call_log.v1",
            "request_id": "req-2",
            "derived_user_concept_id": "#V#owner",
            "chat_session_id": "session-owner",
            "entries": [],
            "offset": 0,
            "limit": 20,
            "returned_count": 0,
            "total_count": 0,
            "has_more": False,
            "next_offset": None,
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    client = app.test_client()
    response = client.get(
        "/von/history/llm_call_log",
        query_string={"request_id": "req-2"},
    )

    assert response.status_code == 403
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("error") == "Not authorised for turn"
