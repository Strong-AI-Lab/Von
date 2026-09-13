from flask import Flask, jsonify
import pytest
from src.backend.services.model_audio_metadata import recorded_transcription_supported
from src.backend.services import model_registry_service
from src.backend.server.routes import settings_routes


def test_metadata_extends_and_disables_compatibility_without_guessing_from_names():
    registry = [
        {
            "provider": "openai",
            "model_id": "new-audio",
            "capabilities": {"audio_transcription": True},
        },
        {
            "provider": "openai",
            "model_id": "gpt-transcribe",
            "capabilities": {"audio_transcription": False},
        },
        {
            "provider": "openrouter",
            "model_id": "other-audio",
            "capabilities": {"audio_transcription": True},
        },
    ]
    assert recorded_transcription_supported(
        "openai", "new-audio", registry_models=registry
    )
    assert not recorded_transcription_supported(
        "openai", "gpt-transcribe", registry_models=registry
    )
    assert not recorded_transcription_supported(
        "openai", "gpt-realtime-whisper", registry_models=registry
    )
    assert not recorded_transcription_supported(
        "openai", "general-chat", registry_models=registry
    )
    assert not recorded_transcription_supported(
        "openrouter", "other-audio", registry_models=registry
    )
    for model in ("gpt-transcribe", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"):
        assert recorded_transcription_supported("openai", model, registry_models=[])


@pytest.mark.parametrize(
    "provider,loader,payload",
    [
        ("openai", "get_openai_models", ["gpt-4o-transcribe", "chat"]),
        ("openrouter", "get_openrouter_models", ["vendor/chat"]),
        (
            "ollama",
            "get_ollama_models_from_all_hosts",
            {"models": [{"name": "local", "host_url": "http://localhost:11434"}]},
        ),
        ("gemini", "get_gemini_models", ["gemini-model"]),
        ("meta", "get_meta_models", ["meta-model"]),
    ],
)
def test_inventory_preserves_provider_discovery_and_host_metadata(
    monkeypatch, provider, loader, payload
):
    monkeypatch.setattr(
        model_registry_service, "get_model_registry_snapshot", lambda: {"models": []}
    )
    monkeypatch.setattr(settings_routes, loader, lambda **kw: (jsonify(payload), 200))
    app = Flask(__name__)
    with app.test_request_context():
        response = app.make_response(settings_routes.get_model_inventory(provider))
        data = response.get_json()
    assert data["available"]
    assert all(model["provider"] == provider for model in data["models"])
    if provider == "ollama":
        assert data["models"][0]["host"] == "http://localhost:11434"
    if provider == "openai":
        models = {model["model"]: model for model in data["models"]}
        assert models["gpt-4o-transcribe"]["audio_transcription"]
        assert not models["gpt-transcribe"]["available"]
        assert not models["chat"]["audio_transcription"]


def test_disabled_provider_has_actionable_failure_and_no_available_models(monkeypatch):
    monkeypatch.setattr(
        model_registry_service, "get_model_registry_snapshot", lambda: {"models": []}
    )
    monkeypatch.setattr(
        settings_routes,
        "get_openai_models",
        lambda **kw: (jsonify(error="private provider details"), 400),
    )
    app = Flask(__name__)
    with app.test_request_context():
        data = app.make_response(
            settings_routes.get_model_inventory("openai")
        ).get_json()
    assert not data["available"]
    assert "openai" in data["error"] and "check" in data["error"]
    assert "private provider details" not in data["error"]
    assert not any(model["available"] for model in data["models"])
