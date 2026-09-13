import io
from unittest.mock import MagicMock

import pytest
from flask import Flask

from src.backend.server.routes import speech_routes as routes
from src.backend.services import speech_transcription_service as speech


@pytest.fixture
def provider(monkeypatch):
    from src.backend.services import model_registry_service

    monkeypatch.setattr(
        model_registry_service, "get_model_registry_snapshot", lambda: {"models": []}
    )
    monkeypatch.setattr(
        speech,
        "resolve_enabled_llm_settings",
        lambda **kw: [{"provider": "openai", "model": "gpt-transcribe"}],
    )
    monkeypatch.setattr(speech, "get_openai_env_var", lambda: "TEST_SPEECH_KEY")
    monkeypatch.setenv("TEST_SPEECH_KEY", "test-only-key")
    monkeypatch.delenv("VON_TRANSCRIPTION_MODEL", raising=False)
    gate = MagicMock()
    monkeypatch.setattr(speech, "assert_model_execution_allowed", gate)
    client = MagicMock()
    client.__enter__.return_value = client
    client.audio.transcriptions.create.return_value.text = "Von uses Vontology."
    import openai

    monkeypatch.setattr(openai, "OpenAI", MagicMock(return_value=client))
    return client, gate


@pytest.mark.parametrize(
    "mime,extension", [("audio/mp4", "mp4"), ("audio/webm;codecs=opus", "webm")]
)
def test_recorded_audio_carries_vocabulary_and_language(provider, mime, extension):
    client, gate = provider
    result = speech.transcribe_audio(
        actor="#V#speaker",
        organisation="#V#org",
        audio=b"fixture",
        mime_type=mime,
        context="Discuss Vontology.",
        vocabulary=["Vontology", "Wikidata", "wikidata"],
        language="en-NZ",
    )
    args = client.audio.transcriptions.create.call_args.kwargs
    assert args["file"][0] == "dictation." + extension
    assert args["extra_body"] == {
        "keywords": ["Vontology", "Wikidata"],
        "languages": ["en"],
    }
    assert "Discuss Vontology" in args["prompt"]
    assert result["text"] == "Von uses Vontology."
    assert result["language_hint"] == "en-NZ"
    gate.assert_called_once_with(
        provider="openai",
        model="gpt-transcribe",
        user_concept_id="#V#speaker",
        org_concept_id="#V#org",
        allow_ambient_actor_scope=False,
    )


def test_model_gate_prevents_audio_request(provider):
    client, gate = provider
    gate.side_effect = PermissionError("model denied")
    with pytest.raises(PermissionError):
        speech.transcribe_audio(
            actor="#V#a", organisation=None, audio=b"fixture", mime_type="audio/mp4"
        )
    client.audio.transcriptions.create.assert_not_called()


def test_non_transcription_and_sol_models_cannot_be_selected(provider, monkeypatch):
    monkeypatch.setattr(
        speech,
        "resolve_enabled_llm_settings",
        lambda **kw: [{"provider": "openai", "model": "gpt-5.6-sol"}],
    )
    monkeypatch.setenv("VON_TRANSCRIPTION_MODEL", "gpt-5.6-sol")
    with pytest.raises(speech.SpeechUnavailable):
        speech.transcription_capability("#V#a", None)


def test_context_is_bounded_literal_vocabulary():
    context, terms = speech.normalise_context(
        "x" * 10000, ["<term>\r\nnext", "a" * 200] * 100
    )
    assert len(context) == 6000
    assert terms == ["term   next", "a" * 100]


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-only-session")
    app.register_blueprint(routes.speech_bp)
    return app


def test_normal_authenticated_session_and_forged_payload_identity(app, provider):
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_concept_id"] = "#V#speaker"
        response = client.post(
            "/api/speech/transcribe",
            data={
                "audio": (io.BytesIO(b"fixture"), "clip.mp4", "audio/mp4"),
                "user_concept_id": "#V#other",
            },
        )
    assert response.status_code == 200
    assert provider[1].call_args.kwargs["user_concept_id"] == "#V#speaker"
    assert response.headers["Cache-Control"] == "private, no-store"


def test_unauthenticated_upload_is_denied(app, provider):
    response = app.test_client().post(
        "/api/speech/transcribe", data={"user_concept_id": "#V#speaker"}
    )
    assert response.status_code == 401
    provider[0].audio.transcriptions.create.assert_not_called()


def test_provider_failure_is_safe_and_retryable(app, provider):
    provider[0].audio.transcriptions.create.side_effect = RuntimeError(
        "secret-provider-token"
    )
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_concept_id"] = "#V#speaker"
        response = client.post(
            "/api/speech/transcribe",
            data={"audio": (io.BytesIO(b"fixture"), "clip.mp4", "audio/mp4")},
        )
    assert response.status_code == 502
    assert b"secret-provider-token" not in response.data
    assert "retry" in response.json["message"]


@pytest.mark.parametrize("data,mime", [(b"", "audio/mp4"), (b"a", "text/plain")])
def test_bad_recording_never_reaches_provider(provider, data, mime):
    with pytest.raises(ValueError):
        speech.transcribe_audio(
            actor="#V#a", organisation=None, audio=data, mime_type=mime
        )
    provider[0].audio.transcriptions.create.assert_not_called()


def test_explicit_metadata_model_selection_is_checked_against_effective_pool(
    provider, monkeypatch
):
    from src.backend.services import model_registry_service

    monkeypatch.setattr(
        model_registry_service,
        "get_model_registry_snapshot",
        lambda: {
            "models": [
                {
                    "provider": "openai",
                    "model_id": "future-transcription",
                    "capabilities": {"audio_transcription": True},
                }
            ]
        },
    )
    monkeypatch.setattr(
        speech,
        "resolve_enabled_llm_settings",
        lambda **kw: [
            {"provider": "openai", "model": "future-transcription"},
            {"provider": "openai", "model": "general-chat"},
        ],
    )
    assert (
        speech.transcription_model("#V#speaker", None, "future-transcription")
        == "future-transcription"
    )
    for model in ("general-chat", "removed-transcription", "gpt-4o-transcribe"):
        with pytest.raises(speech.SpeechUnavailable, match="not an enabled"):
            speech.transcription_model("#V#speaker", None, model)
    client, _ = provider
    speech.transcribe_audio(
        actor="#V#speaker",
        organisation=None,
        audio=b"fixture",
        mime_type="audio/webm",
        model="future-transcription",
    )
    assert (
        client.audio.transcriptions.create.call_args.kwargs["model"]
        == "future-transcription"
    )


def test_disabled_provider_cannot_transcribe_even_with_enabled_model(
    provider, monkeypatch
):
    monkeypatch.delenv("TEST_SPEECH_KEY")
    with pytest.raises(speech.SpeechUnavailable, match="OpenAI key"):
        speech.transcription_capability("#V#speaker", None)
