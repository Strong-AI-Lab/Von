"""Tests for LLMClient interface and factory."""

import asyncio
import logging
import types

import pytest

from src.backend.languagemodels.structured_tool_calling import (
    LLMClientConfig,
    StructuredToolProtocolError,
    ToolDefinition,
    get_llm_client,
)
from src.backend.languagemodels.structured_tool_calling.providers.ollama_client import (
    OllamaClient,
)


def _install_openai_temperature_registry(monkeypatch) -> dict[str, object]:
    import src.backend.services.model_registry_service as registry_module

    snapshot = {
        "source": "vontology_graph",
        "models": [
            {
                "model_id": "gpt-5.2",
                "provider": "openai",
                "concept_id": "#V#openai_gpt52",
                "registry_entry_id": "#V#openai_gpt52_registry_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#openai_gpt52_chat_completions_profile",
                        "api_surface": "chat_completions",
                        "parameter_constraints": [
                            {
                                "constraint_concept_id": "#V#openai_gpt52_temperature_omit_constraint",
                                "parameter_concept_id": "#V#temperature_parameter",
                                "parameter": "temperature",
                                "action": "omit",
                                "fixed_value": "1.0",
                            }
                        ],
                    }
                ],
            },
            {
                "model_id": "gpt-5-mini",
                "provider": "openai",
                "concept_id": "#V#openai_gpt_5_mini",
                "registry_entry_id": "#V#openai_gpt5_mini_registry_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#openai_gpt5_mini_chat_completions_profile",
                        "api_surface": "chat_completions",
                        "parameter_constraints": [
                            {
                                "constraint_concept_id": "#V#openai_gpt5_mini_temperature_omit_constraint",
                                "parameter_concept_id": "#V#temperature_parameter",
                                "parameter": "temperature",
                                "action": "omit",
                                "fixed_value": "1.0",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(
        registry_module,
        "get_model_registry_snapshot",
        lambda *, preferred_language=None: snapshot,
    )
    return snapshot


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
        setattr(
            fake_genai,
            "Client",
            lambda **kwargs: types.SimpleNamespace(
                models=types.SimpleNamespace(generate_content=lambda **kw: None)
            ),
        )
        setattr(
            fake_genai,
            "types",
            types.SimpleNamespace(
                GenerateContentConfig=lambda **kw: kw,
                Tool=lambda **kw: kw,
                FunctionDeclaration=lambda **kw: kw,
                Schema=lambda **kw: kw,
                Type=types.SimpleNamespace(OBJECT="OBJECT"),
            ),
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


class TestOllamaToolCallParsing:
    """Ollama text transport accepts every format its prompt advertises."""

    @staticmethod
    def _client() -> OllamaClient:
        client = OllamaClient.__new__(OllamaClient)
        client.config = LLMClientConfig(model="qwen3.5:27b")
        client.logger = logging.getLogger(__name__)
        return client

    @staticmethod
    def _tools() -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="turn_capabilities",
                description="List available capabilities.",
                input_schema={"type": "object", "properties": {}},
            ),
            ToolDefinition(
                name="turn_list_evidence",
                description="List gathered evidence.",
                input_schema={"type": "object", "properties": {}},
            ),
        ]

    def test_parses_newline_separated_calls_with_nested_payloads(self):
        response = self._client()._parse_response(
            """{
  "action": "call_tool",
  "tool": "turn_capabilities",
  "payload": {"names": ["create_concepts"], "filter": {"exact": true}}
}
{
  "action": "call_tool",
  "tool": "turn_list_evidence",
  "payload": {}
}""",
            self._tools(),
        )

        assert [call.tool_name for call in response.tool_calls] == [
            "turn_capabilities",
            "turn_list_evidence",
        ]
        assert response.tool_calls[0].payload == {
            "names": ["create_concepts"],
            "filter": {"exact": True},
        }
        assert response.text_response == ""

    def test_removes_fenced_tool_calls_but_preserves_natural_text(self):
        response = self._client()._parse_response(
            """Checking the available evidence.
```json
{"action":"call_tool","tool":"turn_list_evidence","payload":{}}
```""",
            self._tools(),
        )

        assert [call.tool_name for call in response.tool_calls] == [
            "turn_list_evidence"
        ]
        assert response.text_response == "Checking the available evidence."

    def test_preserves_unrelated_json_as_answer_text(self):
        response = self._client()._parse_response(
            '{"status":"no tool requested","payload":{"reason":"done"}}',
            self._tools(),
        )

        assert response.tool_calls == []
        assert response.text_response == (
            '{"status":"no tool requested","payload":{"reason":"done"}}'
        )

    def test_unknown_tool_is_removed_and_exposed_as_typed_diagnostic(self):
        response = self._client()._parse_response(
            '{"action":"call_tool","tool":"general_read","payload":{}}',
            self._tools(),
        )

        assert response.tool_calls == []
        assert response.text_response == ""
        assert response.tool_call_diagnostics == [
            {
                "schema_version": "tool_call_contract_validation.v1",
                "status": "invalid",
                "tool": "general_read",
                "error_code": "unknown_tool",
                "message": "Unknown tool requested: general_read",
            }
        ]

    def test_constrained_prompt_exposes_exact_tool_input_schema(self):
        client = self._client()
        prompt = client._build_constrained_prompt(
            "Find the exact capabilities needed.",
            [
                ToolDefinition(
                    name="turn_capabilities",
                    description="Inspect delegated capabilities.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "names": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 1,
                            }
                        },
                        "additionalProperties": False,
                    },
                )
            ],
            system_message=None,
        )

        assert "Input JSON schema:" in prompt
        assert '"names": {"items": {"type": "string"}' in prompt
        assert '"maxItems": 1' in prompt
        assert '"additionalProperties": false' in prompt
        assert '"payload": {' in prompt
        assert '"payload": "<tool_arguments>"' not in prompt
        assert "do not add undeclared fields" in prompt
        assert "emit a separate tool-call" in prompt


class TestTemperatureGuards:
    """Tests for model-specific temperature safeguards."""

    @pytest.mark.parametrize(
        ("model", "temperature", "expected"),
        [
            ("gpt-4", 0.7, 0.7),
            ("gpt-5-mini", 0.7, None),
            ("gpt-5-mini-2026-03-01", 0.7, None),
            ("gpt-5.2-chat-latest", 0.7, None),
        ],
    )
    def test_resolve_safe_temperature_for_model(
        self, monkeypatch, model, temperature, expected
    ):
        _install_openai_temperature_registry(monkeypatch)
        from src.backend.languagemodels.structured_tool_calling.client import (
            resolve_safe_temperature_for_model,
        )

        assert resolve_safe_temperature_for_model(model, temperature) == expected

    def test_openai_provider_omits_temperature_for_gpt5_mini(self, monkeypatch):
        _install_openai_temperature_registry(monkeypatch)
        from src.backend.languagemodels.structured_tool_calling.providers import (
            openai_client as provider_module,
        )
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        captured_kwargs: dict[str, object] = {}

        async def _create(**kwargs):
            captured_kwargs.update(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=None,
                model="gpt-5-mini",
            )

        monkeypatch.setattr(
            provider_module.openai,
            "AsyncOpenAI",
            lambda **_kwargs: types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=_create)
                )
            ),
        )
        monkeypatch.setattr(
            provider_module.openai,
            "OpenAI",
            lambda **_kwargs: object(),
        )

        client = OpenAIClient(
            LLMClientConfig(model="gpt-5-mini", api_key="test-key", temperature=0.7)
        )

        result = asyncio.run(
            client.generate_with_tools(
                prompt="hello",
                available_tools=[],
            )
        )

        assert result.text_response == "ok"
        assert "temperature" not in captured_kwargs

    def test_openai_provider_accepts_model_override_kwarg(self, monkeypatch):
        """The orchestrator passes a resolved model through the generic interface."""
        _install_openai_temperature_registry(monkeypatch)
        from src.backend.languagemodels.structured_tool_calling.providers import (
            openai_client as provider_module,
        )
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        captured_kwargs: dict[str, object] = {}

        async def _create(**kwargs):
            captured_kwargs.update(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=None,
                model="gpt-4.1",
            )

        monkeypatch.setattr(
            provider_module.openai,
            "AsyncOpenAI",
            lambda **_kwargs: types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=_create)
                )
            ),
        )
        monkeypatch.setattr(
            provider_module.openai,
            "OpenAI",
            lambda **_kwargs: object(),
        )

        client = OpenAIClient(
            LLMClientConfig(model="configured-model", api_key="test-key")
        )

        result = asyncio.run(
            client.generate_with_tools(
                prompt="hello",
                available_tools=[],
                model="gpt-4.1",
            )
        )

        assert result.text_response == "ok"
        assert captured_kwargs["model"] == "gpt-4.1"

    def test_openai_provider_maps_reasoning_effort_to_chat_completions(
        self, monkeypatch
    ):
        from src.backend.languagemodels.structured_tool_calling.providers import (
            openai_client as provider_module,
        )
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        captured_kwargs: dict[str, object] = {}

        async def _create(**kwargs):
            captured_kwargs.update(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=None,
                model="gpt-5.5",
            )

        monkeypatch.setattr(
            provider_module.openai,
            "AsyncOpenAI",
            lambda **_kwargs: types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=_create)
                )
            ),
        )
        monkeypatch.setattr(
            provider_module.openai,
            "OpenAI",
            lambda **_kwargs: object(),
        )

        client = OpenAIClient(
            LLMClientConfig(model="gpt-5.5", api_key="test-key")
        )

        result = asyncio.run(
            client.generate_with_tools(
                prompt="hello",
                available_tools=[],
                llm_params={"reasoning_effort": "low"},
            )
        )

        assert result.text_response == "ok"
        assert captured_kwargs["reasoning_effort"] == "low"

    def test_openai_provider_omits_reasoning_effort_for_chat_tool_calls(
        self, monkeypatch
    ):
        from src.backend.languagemodels.structured_tool_calling.providers import (
            openai_client as provider_module,
        )
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        captured_kwargs: dict[str, object] = {}

        async def _create(**kwargs):
            captured_kwargs.update(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=None,
                model="gpt-5.5",
            )

        monkeypatch.setattr(
            provider_module.openai,
            "AsyncOpenAI",
            lambda **_kwargs: types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=_create)
                )
            ),
        )
        monkeypatch.setattr(
            provider_module.openai,
            "OpenAI",
            lambda **_kwargs: object(),
        )

        client = OpenAIClient(LLMClientConfig(model="gpt-5.5", api_key="test-key"))
        tool = ToolDefinition(
            name="gmail_get_auth_config",
            description="Read Gmail auth configuration",
            input_schema={"type": "object", "properties": {}},
        )

        result = asyncio.run(
            client.generate_with_tools(
                prompt="hello",
                available_tools=[tool],
                llm_params={"reasoning_effort": "low"},
            )
        )

        assert result.text_response == "ok"
        assert "reasoning_effort" not in captured_kwargs
        assert captured_kwargs["tools"]

    def test_openai_provider_preserves_none_effort_for_chat_compatible_tool_calls(
        self, monkeypatch
    ):
        """Explicit none disables the reasoning mode that conflicts with tools."""
        from src.backend.languagemodels.structured_tool_calling.providers import (
            openai_client as provider_module,
        )
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        captured_kwargs: dict[str, object] = {}

        async def _create(**kwargs):
            captured_kwargs.update(kwargs)
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=None,
                model="gpt-5.5",
            )

        monkeypatch.setattr(
            provider_module.openai,
            "AsyncOpenAI",
            lambda **_kwargs: types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=_create)
                )
            ),
        )
        monkeypatch.setattr(
            provider_module.openai,
            "OpenAI",
            lambda **_kwargs: object(),
        )

        client = OpenAIClient(
            LLMClientConfig(model="gpt-5.5", api_key="test-key")
        )
        tool = ToolDefinition(
            name="fetch_concept",
            description="Fetch a represented concept",
            input_schema={"type": "object", "properties": {}},
        )

        result = asyncio.run(
            client.generate_with_tools(
                prompt="hello",
                available_tools=[tool],
                llm_params={"reasoning_effort": "none"},
            )
        )

        assert result.text_response == "ok"
        assert captured_kwargs["reasoning_effort"] == "none"
        assert captured_kwargs["tools"]

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

    def test_tool_definition_to_dict_closes_and_stricts_compatible_schema(self):
        """Closed all-required contracts should be sent as strict tool definitions."""
        config = LLMClientConfig(model="gpt-4")
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)
        payload = client._tool_definition_to_dict(
            ToolDefinition(
                name="lookup",
                description="Lookup a record.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                    "x-von-argument-aliases": {"q": "query"},
                },
            )
        )

        function_payload = payload["function"]
        assert function_payload["strict"] is True
        assert function_payload["parameters"]["additionalProperties"] is False
        assert "x-von-argument-aliases" not in function_payload["parameters"]

    def test_tool_definition_to_dict_stricts_recursively_closed_schema(self):
        """Closed objects remain strict through arrays and composition branches."""
        config = LLMClientConfig(model="gpt-4")
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)
        payload = client._tool_definition_to_dict(
            ToolDefinition(
                name="nested_lookup",
                description="Run a nested lookup.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "filters": {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {},
                                        "required": [],
                                        "additionalProperties": False,
                                    },
                                ]
                            },
                        }
                    },
                    "required": ["filters"],
                    "additionalProperties": False,
                },
            )
        )

        assert payload["function"]["strict"] is True

    def test_tool_definition_to_dict_does_not_strict_unsupported_schema(self):
        """A closed schema still stays non-strict outside the known-safe subset."""
        config = LLMClientConfig(model="gpt-4")
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)
        payload = client._tool_definition_to_dict(
            ToolDefinition(
                name="conditional_lookup",
                description="Run a conditional lookup.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "allOf": [{"type": "string"}],
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            )
        )

        assert "strict" not in payload["function"]

    @pytest.mark.parametrize(
        "item_schema",
        ({}, {"description": "Unconstrained."}, {"type": "made-up"}),
    )
    def test_tool_definition_to_dict_does_not_strict_invalid_nested_schema(
        self, item_schema
    ):
        """Unconstrained or invalid nested schemas remain usable as non-strict."""
        config = LLMClientConfig(model="gpt-4")
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)
        payload = client._tool_definition_to_dict(
            ToolDefinition(
                name="generic_list",
                description="Accept a generic list.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "values": {
                            "type": "array",
                            "items": item_schema,
                        }
                    },
                    "required": ["values"],
                    "additionalProperties": False,
                },
            )
        )

        assert "strict" not in payload["function"]

    def test_tool_definition_to_dict_does_not_strict_optional_schema(self):
        """Optional arguments stay closed but avoid provider strict mode."""
        config = LLMClientConfig(model="gpt-4")
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)
        payload = client._tool_definition_to_dict(
            ToolDefinition(
                name="list_messages",
                description="List messages.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string"},
                        "max_results": {"type": "integer"},
                    },
                    "required": ["profile"],
                    "additionalProperties": False,
                },
            )
        )

        function_payload = payload["function"]
        assert "strict" not in function_payload
        assert function_payload["parameters"]["additionalProperties"] is False

    def test_openai_parse_fails_closed_with_bounded_invalid_call_diagnostics(self):
        """Invalid Chat calls surface a typed blocker with bounded diagnostics."""
        config = LLMClientConfig(model="gpt-4")
        from src.backend.languagemodels.structured_tool_calling.providers import (
            OpenAIClient,
        )

        client = OpenAIClient(config)
        response = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content=None,
                        tool_calls=[
                            types.SimpleNamespace(
                                id="call_1",
                                function=types.SimpleNamespace(
                                    name="lookup",
                                    arguments="{bad json",
                                ),
                            ),
                            types.SimpleNamespace(
                                id="call_2",
                                function=types.SimpleNamespace(
                                    name="missing_tool",
                                    arguments="{}",
                                ),
                            ),
                        ],
                    )
                )
            ],
            usage=None,
            model="gpt-4",
        )

        with pytest.raises(StructuredToolProtocolError) as exc_info:
            client._parse_response(
                response,
                [
                    ToolDefinition(
                        name="lookup",
                        description="Lookup a record.",
                        input_schema={"type": "object", "properties": {}},
                    )
                ],
            )

        assert exc_info.value.decision["failure_kind"] == (
            "chat_provider_tool_call_rejected"
        )
        assert exc_info.value.decision["provider_tool_call_diagnostic_codes"] == [
            "provider_tool_call_parse_error",
            "unknown_tool",
        ]
