from __future__ import annotations

from flask import Flask

from src.backend.server.routes.client_capabilities_routes import client_capabilities_bp


def test_client_capabilities_get_defaults_to_none():
    app = Flask(__name__)
    app.config.update(SECRET_KEY="test")
    app.register_blueprint(client_capabilities_bp)

    with app.test_client() as client:
        res = client.get("/api/client_capabilities")
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["capabilities"] is None


def test_client_capabilities_post_sanitises_and_persists_in_session():
    app = Flask(__name__)
    app.config.update(SECRET_KEY="test")
    app.register_blueprint(client_capabilities_bp)

    payload = {
        "client_reported_timestamp": " 2026-01-01T00:00:00Z ",
        "speech_synthesis": {
            "supported": True,
            "voices_count": 999_999,
            "voices_sample": [
                {
                    "name": "n" * 250,
                    "lang": "en-NZ",
                    "localService": "notbool",
                    "extra": "ignored",
                }
            ]
            * 20,
            "default_voice_lang": "not-a-lang!",
            "last_error": "e" * 500,
            "settings": {
                "rate": "fast",
                "pitch": 1.2,
                "volume": 0.8,
                "voice_name": "v" * 250,
                "voice_uri": "uri-ignored",
            },
        },
        "audio_output": {
            "document_muted": "yes",
            "autoplay_policy_hint": "a" * 100,
            "user_activation": 1,
        },
        "other": {
            "speech_recognition_supported": "true",
            "visibility_state": "visible" * 20,
        },
        "unknown_top_level": {"secret": "nope"},
    }

    with app.test_client() as client:
        res = client.post("/api/client_capabilities", json=payload)
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        stored = data["stored"]
        assert stored["kind"] == "client_capabilities"
        assert stored["client_reported"] is True

        speech = stored["speech_synthesis"]
        assert speech["supported"] is True
        assert speech["voices_count"] == 5000
        assert isinstance(speech["voices_sample"], list)
        assert len(speech["voices_sample"]) == 8
        assert speech["voices_sample"][0]["lang"] == "en-NZ"
        assert speech["voices_sample"][0]["name"] is not None
        assert len(speech["voices_sample"][0]["name"]) <= 120
        assert speech["default_voice_lang"] is None
        assert speech["last_error"] is not None
        assert len(speech["last_error"]) <= 200
        assert speech["settings"]["rate"] is None
        assert speech["settings"]["pitch"] == 1.2
        assert speech["settings"]["volume"] == 0.8
        assert speech["settings"]["voice_name"] is not None
        assert len(speech["settings"]["voice_name"]) <= 120

        audio = stored["audio_output"]
        assert audio["autoplay_policy_hint"] is not None
        assert len(audio["autoplay_policy_hint"]) <= 40
        assert audio["user_activation"] is True

        other = stored["other"]
        assert other["speech_recognition_supported"] is True
        assert other["visibility_state"] is not None
        assert len(other["visibility_state"]) <= 30

        res2 = client.get("/api/client_capabilities")
        assert res2.status_code == 200
        data2 = res2.get_json()
        assert data2["success"] is True
        assert data2["capabilities"] == stored
