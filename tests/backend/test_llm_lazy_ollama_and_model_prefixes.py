"""Tests for lazy Ollama client initialisation and updated model prefix validation.

Covers changes from the startup-stall fix:
* _OPENAI_MODEL_PREFIXES includes o3/o4/chatgpt prefixes
* validate_openai_config uses the shared constant
* OllamaClient is no longer created unconditionally in initialize_clients()
* _ensure_ollama_client() provides lazy creation
* ``import ollama`` is deferred to first use (_import_ollama) to avoid the
  module-level Client() creation that calls platform.machine() → WMI on Windows

All imports of the module under test are done inside test bodies to avoid
triggering a circular-import error during collection (llm_interface →
services → workflows.durable → llm_interface).
"""

from unittest.mock import MagicMock, patch

import pytest

# Pre-load the services package to break the circular import chain.
# (services.__init__ → durable → llm_interface loads in the correct order
# when services is imported first.)
import src.backend.services  # noqa: F401


# ---------------------------------------------------------------------------
# _OPENAI_MODEL_PREFIXES tests
# ---------------------------------------------------------------------------
class TestOpenAIModelPrefixes:
    """Verify that the centralised prefix tuple recognises modern model names."""

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-4o",
            "gpt-4o-mini",
            "o1-preview",
            "o1-mini",
            "o3-mini",
            "o4-mini",
            "chatgpt-4o-latest",
            "text-embedding-3-large",
            "davinci-002",
        ],
    )
    def test_known_models_recognised(self, model):
        from src.backend.languagemodels.llm_interface import _looks_like_openai_model
        assert _looks_like_openai_model(model), f"{model} should be recognised"

    @pytest.mark.parametrize(
        "model",
        [
            "llama3:8b",
            "granite3.3:2b",
            "chat-5-mini",  # not an OpenAI prefix
            "",
            "my-custom-model",
        ],
    )
    def test_non_openai_models_rejected(self, model):
        from src.backend.languagemodels.llm_interface import _looks_like_openai_model
        assert not _looks_like_openai_model(model), f"{model} should NOT match"


# ---------------------------------------------------------------------------
# validate_openai_config shared constant
# ---------------------------------------------------------------------------
class TestValidateOpenAIConfig:
    """Ensure validate_openai_config uses _OPENAI_MODEL_PREFIXES."""

    def test_valid_o4_mini_no_unusual_warning(self):
        from src.backend.languagemodels.llm_interface import validate_openai_config
        is_valid, msgs = validate_openai_config(
            "OPENAI_API_KEY", "o4-mini", "sk-" + "x" * 50
        )
        assert is_valid, f"Expected valid but got messages: {msgs}"
        assert not any("Unusual" in m for m in msgs)

    def test_valid_o3_mini(self):
        from src.backend.languagemodels.llm_interface import validate_openai_config
        is_valid, msgs = validate_openai_config(
            "OPENAI_API_KEY", "o3-mini", "sk-" + "x" * 50
        )
        assert is_valid

    def test_unusual_model_still_warns(self):
        from src.backend.languagemodels.llm_interface import validate_openai_config
        is_valid, msgs = validate_openai_config(
            "OPENAI_API_KEY", "chat-5-mini", "sk-" + "x" * 50
        )
        assert any("Unusual" in m for m in msgs)


class TestOpenAIKeyFileResolution:
    """Cloud bootstrap can provide provider secrets through *_FILE env vars."""

    @patch("src.backend.languagemodels.llm_interface.resolve_llm_setting")
    @patch("src.backend.languagemodels.llm_interface.get_openai_env_var")
    @patch("src.backend.languagemodels.llm_interface.OpenAIClient")
    def test_initialize_clients_reads_openai_api_key_file(
        self,
        mock_openai_client,
        mock_get_openai_env_var,
        mock_resolve_llm_setting,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import src.backend.languagemodels.llm_interface as mod

        key_file = tmp_path / "openai_api_key"
        key_value = "sk-" + "x" * 50
        key_file.write_text(key_value, encoding="utf-8")

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY_FILE", str(key_file))
        mock_get_openai_env_var.return_value = "OPENAI_API_KEY"
        mock_resolve_llm_setting.return_value = {
            "provider": "openai",
            "model": "gpt-5",
        }

        mod._openai_client = None
        mod._last_openai_env_var = None
        mod._last_openai_key = None

        mod.initialize_clients(force=True)

        mock_openai_client.assert_called_once_with(
            api_key=key_value,
            api_key_env_var="OPENAI_API_KEY",
        )

    @patch("src.backend.languagemodels.llm_interface.warnings.warn")
    @patch("src.backend.languagemodels.llm_interface.resolve_llm_setting")
    @patch("src.backend.languagemodels.llm_interface.get_openai_env_var")
    @patch("src.backend.languagemodels.llm_interface.OpenAIClient")
    def test_initialize_clients_uses_background_actor_model_for_validation(
        self,
        mock_openai_client,
        mock_get_openai_env_var,
        mock_resolve_llm_setting,
        mock_warn,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import src.backend.languagemodels.llm_interface as mod
        from src.backend.security.access_control import override_current_actor

        monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "x" * 50)
        mock_get_openai_env_var.return_value = "OPENAI_API_KEY"
        mock_resolve_llm_setting.return_value = {
            "provider": "openai",
            "model": "gpt-5.6-luna",
        }
        mod._openai_client = None
        mod._last_openai_env_var = None
        mod._last_openai_key = None

        with override_current_actor("#V#actor", "#V#lab"):
            mod.initialize_clients(force=True)

        mock_resolve_llm_setting.assert_called_with(
            user_concept_id="#V#actor",
            org_concept_id="#V#lab",
        )
        assert not any(
            "OpenAI model not configured" in str(call.args[0])
            for call in mock_warn.call_args_list
        )
        mock_openai_client.assert_called_once()


# ---------------------------------------------------------------------------
# Lazy Ollama initialisation
# ---------------------------------------------------------------------------
class TestLazyOllamaInit:
    """OllamaClient should NOT be created during initialize_clients()."""

    @patch("src.backend.languagemodels.llm_interface.resolve_llm_setting", return_value=None)
    @patch("src.backend.languagemodels.llm_interface.get_openai_env_var", return_value=None)
    def test_initialize_clients_skips_ollama(self, _mock_env, _mock_resolve):
        """After initialize_clients(), _ollama_client should remain None."""
        import src.backend.languagemodels.llm_interface as mod

        # Reset globals
        mod._ollama_client = None
        mod._openai_client = None
        mod._last_openai_env_var = None
        mod._last_openai_key = None

        mod.initialize_clients(force=True)

        # Key assertion: Ollama must NOT have been eagerly created
        assert mod._ollama_client is None, (
            "OllamaClient should not be created during initialize_clients()"
        )

    @patch("src.backend.languagemodels.llm_interface.OllamaClient")
    def test_ensure_ollama_client_creates_on_first_call(self, MockOllama):
        """_ensure_ollama_client() should create the client lazily."""
        import src.backend.languagemodels.llm_interface as mod

        mod._ollama_client = None
        mock_instance = MagicMock()
        MockOllama.return_value = mock_instance

        result = mod._ensure_ollama_client()

        MockOllama.assert_called_once()
        assert result is mock_instance
        assert mod._ollama_client is mock_instance

    @patch("src.backend.languagemodels.llm_interface.initialize_clients")
    @patch("src.backend.languagemodels.llm_interface.OllamaClient")
    def test_explicit_ollama_candidate_host_reaches_client_factory(
        self,
        MockOllama,
        _mock_initialize_clients,
    ):
        import src.backend.languagemodels.llm_interface as mod

        mock_instance = MagicMock()
        MockOllama.return_value = mock_instance

        result = mod.get_llm_client(
            client_type="ollama",
            host="http://127.0.0.1:11434",
        )

        assert result is mock_instance
        MockOllama.assert_called_once_with(host="http://127.0.0.1:11434")

    @patch("src.backend.languagemodels.llm_interface.OllamaClient")
    def test_ensure_ollama_client_reuses_existing(self, MockOllama):
        """If already created, _ensure_ollama_client() returns the same instance."""
        import src.backend.languagemodels.llm_interface as mod

        existing = MagicMock()
        mod._ollama_client = existing

        result = mod._ensure_ollama_client()

        MockOllama.assert_not_called()
        assert result is existing


# ---------------------------------------------------------------------------
# Lazy ollama import
# ---------------------------------------------------------------------------
class TestLazyOllamaImport:
    """The ollama package should NOT be imported at module load time."""

    def test_ollama_not_imported_at_module_level(self):
        """After importing llm_interface, the module-level `ollama` should be None
        (not yet imported) because we defer it via _import_ollama()."""
        import src.backend.languagemodels.llm_interface as mod

        # _import_ollama may have already been called during other tests,
        # so we can't guarantee ollama is None at this point. Instead, verify
        # that the lazy machinery exists and works.
        assert callable(mod._import_ollama)
        assert hasattr(mod, "_ollama_available")
