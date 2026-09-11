from unittest.mock import MagicMock

import mongomock
import pytest
from flask import Flask

from src.backend.server.routes import speech_routes as routes
from src.backend.services import speech_session_service as media
from src.backend.services import speech_telemetry_service as telemetry


@pytest.fixture
def media_provider(monkeypatch):
    monkeypatch.setattr(
        media,
        "resolve_enabled_llm_settings",
        lambda **kw: [{"provider": "openai", "model": "gpt-live-transcribe"}],
    )
    monkeypatch.setattr(media, "get_openai_env_var", lambda: "TEST_MEDIA_KEY")
    monkeypatch.setenv("TEST_MEDIA_KEY", "fixture-key")
    gate = MagicMock()
    monkeypatch.setattr(media, "assert_model_execution_allowed", gate)
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value.text = "v=0\r\nanswer"
    monkeypatch.setattr(media.httpx, "Client", lambda **kw: client)
    return client, gate


def test_connection_is_transcription_only_scoped_and_contextual(media_provider):
    import json

    client, gate = media_provider
    result = media.create_transcription_connection(
        actor="#V#a",
        organisation="#V#org",
        sdp="v=0\r\noffer",
        vocabulary=["Von", "Vontology"],
        language="en-NZ",
        context="Current task",
    )
    config = json.loads(client.post.call_args.kwargs["files"]["session"][1])
    assert config["type"] == "transcription"
    assert config["audio"]["input"]["turn_detection"] is None
    assert "tools" not in config
    assert config["audio"]["input"]["transcription"]["languages"] == ["en"]
    assert config["audio"]["input"]["transcription"]["keywords"] == ["Von", "Vontology"]
    assert result["sdp"] == "v=0\r\nanswer"
    assert gate.call_args.kwargs["user_concept_id"] == "#V#a"


def test_model_denial_prevents_media_request(media_provider):
    client, gate = media_provider
    gate.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        media.create_transcription_connection(
            actor="#V#a", organisation=None, sdp="v=0\r\noffer"
        )
    client.post.assert_not_called()


@pytest.fixture
def app(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "fixture"
    app.config["TESTING"] = True
    app.register_blueprint(routes.speech_bp)
    db = mongomock.MongoClient().speech_fixture
    monkeypatch.setattr(telemetry, "get_db", lambda: db)
    return app


def test_media_routes_do_not_accept_payload_actor_claim(app, media_provider):
    client, _ = media_provider
    for path in ("connection", "speak", "attempts/fixture-attempt"):
        response = app.test_client().post(
            "/api/speech/" + path,
            json={"user_id": "#V#a", "sdp": "v=0\r\noffer", "text": "hello"},
        )
        assert response.status_code == 401
    client.post.assert_not_called()


def test_attempt_durable_readback_omits_content_and_separates_actor_and_organisation(
    app, monkeypatch
):
    scope = ["#V#a", "#V#org"]
    monkeypatch.setattr(routes, "_speech_actor", lambda: tuple(scope))
    client = app.test_client()
    payload = {
        "conversation_id": "conv",
        "client_context": {
            "device_model": "Pixel 8",
            "user_agent": "Chrome/140.0.0.0",
            "secret": "omit",
        },
        "event": {
            "name": "error",
            "sequence": 1,
            "reason": "network",
            "transcript": "private text",
            "audio": "private audio",
        },
    }
    assert (
        client.post(
            "/api/speech/attempts/fixture-attempt",
            json=payload,
            headers={"User-Agent": "Chrome/140.0.0.0"},
        ).json["stored"]
        is True
    )
    result = client.get("/api/speech/attempts/fixture-attempt").json
    assert result["client_context"]["device_model"] == "Pixel 8"
    assert result["client_context"]["user_agent_summary"]["browser_family"] == "Chrome"
    assert "private" not in str(result) and "omit" not in str(result)
    scope[0] = "#V#b"
    assert client.get("/api/speech/attempts/fixture-attempt").status_code == 404
    scope[:] = ["#V#a", "#V#another_org"]
    assert client.get("/api/speech/attempts/fixture-attempt").status_code == 404


def test_client_context_survives_queue_sanitisation():
    from src.backend.services.chat_prompt_queue_service import (
        _normalise_execution_envelope,
    )

    result = _normalise_execution_envelope(
        {
            "client_context": {
                "user_agent": "Chrome/140.0.0.0",
                "speech_item_id": "item_1",
                "speech_attempt_ids": ["attempt"],
            }
        }
    )
    twice = telemetry.sanitise_client_context(result["client_context"])
    assert twice["speech_item_id"] == "item_1"
    assert twice["speech_attempt_ids"] == ["attempt"]
    assert twice["user_agent_summary"]["major_version"] == 140


def test_connection_uses_authenticated_session_not_payload_actor(app, media_provider):
    _, gate = media_provider
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_concept_id"] = "#V#speaker"
    response = client.post(
        "/api/speech/connection", json={"sdp": "v=0\r\noffer", "user_id": "#V#other"}
    )
    assert response.status_code == 200
    assert gate.call_args.kwargs["user_concept_id"] == "#V#speaker"


@pytest.mark.parametrize("consume", [True, False])
def test_pcm_stream_closes_provider_on_completion_or_disconnect(monkeypatch, consume):
    import openai

    monkeypatch.setattr(media, "_authorise", lambda *args: "tts-1")
    monkeypatch.setattr(media, "get_openai_env_var", lambda: "TEST_MEDIA_KEY")
    monkeypatch.setenv("TEST_MEDIA_KEY", "fixture-key")
    client = MagicMock()
    manager = client.audio.speech.with_streaming_response.create.return_value
    manager.__enter__.return_value.iter_bytes.return_value = iter([b"first", b"second"])
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
    chunks, model, close = media.open_spoken_audio(
        actor="#V#a", organisation=None, text="Hello"
    )
    assert model == "tts-1"
    if consume:
        assert next(chunks) == b"first"
        chunks.close()
    close()
    manager.__exit__.assert_called_once()
    client.close.assert_called_once()
    assert (
        client.audio.speech.with_streaming_response.create.call_args.kwargs["voice"]
        == "alloy"
    )


def test_history_persists_request_client_snapshot_in_debug_only(app, monkeypatch):
    from src.backend.services import chat_history_service as history

    collection = MagicMock()
    monkeypatch.setattr(
        history, "get_chat_history_collection_service", lambda **kw: collection
    )
    monkeypatch.setattr(history, "get_session_context", lambda: {"namespace": "#V#a"})
    monkeypatch.setattr(history, "get_rag_service", None)
    debug = {"model": "fixture"}
    with app.test_request_context(
        json={
            "client_context": {
                "device_model": "Pixel 8",
                "speech_attempt_ids": ["fixture-attempt"],
            }
        }
    ):
        history.add_message_to_history(
            "#V#a",
            "conv",
            {"role": "user", "content": "Hello"},
            debug,
            broadcast_to_shared=False,
            skip_rag_indexing=True,
        )
    entry = collection.update_one.call_args.args[1]["$push"]["history"]
    assert entry["llm_debug_data"]["client_context"]["device_model"] == "Pixel 8"
    assert debug["client_context"]["speech_attempt_ids"] == ["fixture-attempt"]
    assert "client_context" not in entry
