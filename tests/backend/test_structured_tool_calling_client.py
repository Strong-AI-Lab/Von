"""Tests for LLMClient interface and factory."""

import pytest

from src.backend.languagemodels.structured_tool_calling import (
    LLMClient,
    LLMClientConfig,
    ToolDefinition,
    LLMResponse,
    get_llm_client,
)


class TestLLMClientConfig:
    """Tests for LLMClientConfig."""

    def test_minimal_config(self):
        """Test creating minimal LLMClientConfig."""
        config = LLMClientConfig(model="gpt-4")

        assert config.model == "gpt-4"
        assert config.api_key is None
        assert config.temperature == 0.7
        assert config.enable_structured_calling is True

    def test_full_config(self):
        """Test creating full LLMClientConfig."""
        config = LLMClientConfig(
            model="gpt-4",
            api_key="sk-...",
            base_url="https://api.openai.com/v1",
            temperature=0.5,
            max_tokens=1024,
            enable_structured_calling=True,
            fallback_to_json_text=False,
        )

        assert config.model == "gpt-4"
        assert config.api_key == "sk-..."
        assert config.temperature == 0.5
        assert config.max_tokens == 1024


class TestGetLLMClient:
    """Tests for get_llm_client factory."""

    def test_openai_client_creation(self):
        """Test factory creates OpenAIClient for OpenAI models."""
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        config = LLMClientConfig(model="gpt-4")
        client = get_llm_client(config)

        assert isinstance(client, OpenAIClient)

    def test_gemini_client_creation(self, monkeypatch):
        """Test factory creates GeminiClient for Gemini models."""
        from src.backend.languagemodels.structured_tool_calling.providers import (
            GeminiClient,
        )

        # Mock the google.genai.Client to avoid requiring an actual API key
        import sys
        import types

        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = lambda **kwargs: types.SimpleNamespace(
            models=types.SimpleNamespace(generate_content=lambda **kw: None)
        )
        fake_genai.types = types.SimpleNamespace(
            GenerateContentConfig=lambda **kw: kw,
            Tool=lambda **kw: kw,
            FunctionDeclaration=lambda **kw: kw,
            Schema=lambda **kw: kw,
            Type=types.SimpleNamespace(OBJECT="OBJECT"),
        )

        sys.modules["google.genai"] = fake_genai
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

        config = LLMClientConfig(model="gemini-pro", api_key="test-key")
        client = get_llm_client(config)

        assert isinstance(client, GeminiClient)

    def test_ollama_client_creation(self):
        """Test factory creates OllamaClient for Ollama models."""
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OllamaClient,
        )

        config = LLMClientConfig(model="llama2")
        client = get_llm_client(config)

        assert isinstance(client, OllamaClient)

    def test_unknown_model_defaults_to_ollama(self):
        """Test that unknown models default to Ollama."""
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OllamaClient,
        )

        config = LLMClientConfig(model="unknown-model-v1")
        client = get_llm_client(config)

        assert isinstance(client, OllamaClient)


class TestLLMClientBaseValidation:
    """Tests for LLMClient validation methods."""

    def test_validate_input_schema_valid(self):
        """Test that valid schemas pass validation."""
        config = LLMClientConfig(model="gpt-4")

        # Use OpenAIClient as concrete implementation
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)

        # Valid schema with 'type'
        schema_with_type = {"type": "object", "properties": {}}
        client._validate_input_schema(
            ToolDefinition(
                name="test",
                description="Test",
                input_schema=schema_with_type,
            )
        )  # Should not raise

    def test_validate_input_schema_with_properties(self):
        """Test that schema with 'properties' passes validation."""
        config = LLMClientConfig(model="gpt-4")
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)

        # Valid schema with 'properties'
        schema_with_props = {
            "properties": {"arg": {"type": "string"}},
        }
        client._validate_input_schema(
            ToolDefinition(
                name="test",
                description="Test",
                input_schema=schema_with_props,
            )
        )  # Should not raise

    def test_validate_input_schema_invalid(self):
        """Test that invalid schemas raise ValueError."""
        config = LLMClientConfig(model="gpt-4")
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)

        # Invalid schema (neither 'type' nor 'properties')
        invalid_schema = {"description": "No type or properties"}

        with pytest.raises(ValueError, match="must have 'type' or 'properties'"):
            client._validate_input_schema(
                ToolDefinition(
                    name="test",
                    description="Test",
                    input_schema=invalid_schema,
                )
            )
