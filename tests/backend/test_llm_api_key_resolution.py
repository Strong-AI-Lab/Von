import sys
import types

from src.backend.services.llm_api_key_resolution import get_gemini_api_key


def test_get_gemini_api_key_prefers_primary_env_var(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "legacy-key")
    monkeypatch.setenv("GEMINI_API_KEY", "preferred-key")

    assert get_gemini_api_key() == "preferred-key"


def test_gemini_client_accepts_legacy_google_api_key(monkeypatch):
    from src.backend.languagemodels import llm_interface

    configured = {}

    fake_genai = types.SimpleNamespace()

    def _configure(*, api_key):
        configured["api_key"] = api_key

    class _Model:
        def __init__(self, model_name):
            configured["model_name"] = model_name

    fake_genai.configure = _configure
    fake_genai.GenerativeModel = _Model

    monkeypatch.setattr(llm_interface, "genai", fake_genai)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "legacy-key")

    client = llm_interface.GeminiClient()

    assert client.api_key == "legacy-key"
    assert configured["api_key"] == "legacy-key"
    assert configured["model_name"] == "gemini-pro"


def test_describe_image_with_gemini_accepts_legacy_google_api_key(monkeypatch):
    from src.backend.services import file_copy_interpretation_service as service

    google_mod = types.ModuleType("google")
    configured = {}

    def _configure(*, api_key):
        configured["api_key"] = api_key

    class _Response:
        text = "A labelled diagram of a research workflow."

    class _Model:
        def __init__(self, model_name):
            configured["model_name"] = model_name

        def generate_content(self, _parts):
            return _Response()

    setattr(
        google_mod,
        "genai",
        types.SimpleNamespace(
            configure=_configure,
            GenerativeModel=_Model,
        ),
    )

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "legacy-key")

    result = service._describe_image_with_gemini(
        data_bytes=b"png-data",
        content_type="image/png",
        model=None,
        prompt="Describe the image.",
    )

    assert configured["api_key"] == "legacy-key"
    assert configured["model_name"] == "gemini-2.0-flash"
    assert result["method"] == "gemini_vision"
    assert result["description"] == "A labelled diagram of a research workflow."
