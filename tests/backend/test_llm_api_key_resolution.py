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
    assert configured["model_name"] == "gemini-2.0-flash"


def test_describe_image_with_gemini_accepts_legacy_google_api_key(monkeypatch):
    from src.backend.languagemodels import llm_interface
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
    monkeypatch.setattr(
        llm_interface,
        "assert_model_execution_allowed",
        lambda **_kwargs: {"allowed": True},
    )
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


def test_describe_image_with_openai_uses_central_default_model(monkeypatch):
    from src.backend.languagemodels import llm_interface
    from src.backend.services import file_copy_interpretation_service as service

    openai_mod = types.ModuleType("openai")
    captured = {}

    def _create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content="A labelled diagram of a research workflow."
                    )
                )
            ]
        )

    class _Client:
        def __init__(self, *, api_key):
            captured["api_key"] = api_key
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=_create)
            )

    setattr(openai_mod, "OpenAI", _Client)

    monkeypatch.setitem(sys.modules, "openai", openai_mod)
    monkeypatch.setattr(
        llm_interface,
        "assert_model_execution_allowed",
        lambda **_kwargs: {"allowed": True},
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    result = service._describe_image_with_openai(
        data_bytes=b"png-data",
        content_type="image/png",
        model=None,
        prompt="Describe the image.",
    )

    assert captured["api_key"] == "test-openai-key"
    assert captured["model"] == "gpt-5.5"
    assert result["method"] == "openai_vision"
    assert result["model"] == "gpt-5.5"


def test_describe_image_with_openai_denies_actorless_execution_before_provider(
    monkeypatch,
):
    from src.backend.languagemodels import llm_interface
    from src.backend.services import file_copy_interpretation_service as service

    openai_mod = types.ModuleType("openai")
    constructed = False

    class _Client:
        def __init__(self, *, api_key):
            del api_key
            nonlocal constructed
            constructed = True

    setattr(openai_mod, "OpenAI", _Client)
    monkeypatch.setitem(sys.modules, "openai", openai_mod)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(
        llm_interface,
        "_resolve_effective_llm_actor_scope",
        lambda *_args, **_kwargs: (None, None),
    )

    result = service._describe_image_with_openai(
        data_bytes=b"png-data",
        content_type="image/png",
        model="gpt-5.6-terra",
        prompt="Describe the image.",
    )

    assert result["description"] is None
    assert result["method"] == "openai_vision_not_enabled"
    assert result["failure_kind"] == "model_scope_required"
    assert "no authenticated user or organisation model scope" in result["error"]
    assert constructed is False
